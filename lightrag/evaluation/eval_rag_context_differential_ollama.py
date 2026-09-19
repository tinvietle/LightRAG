#!/usr/bin/env python3
"""Collect LightRAG answers and retrieval contexts for later evaluation.

Despite the historical filename, this script does not call Ollama or RAGAS. It
sends each clinical question to LightRAG's ``/query`` endpoint and stores the
answer and referenced chunks in the evaluator's established JSON shape. Metrics
are intentionally computed later so the captured output can be scored repeatedly
without rerunning retrieval.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from tqdm.auto import tqdm

from lightrag.utils import logger


sys.path.insert(0, str(Path(__file__).parent.parent.parent))
load_dotenv(dotenv_path=".env", override=False)

CONNECT_TIMEOUT_SECONDS = 360.0
READ_TIMEOUT_SECONDS = 360.0
TOTAL_TIMEOUT_SECONDS = 360.0
MAX_QUERY_IMAGES = 10
DEFAULT_RESULTS_BASENAME = "retrieval_contexts"


def _optional_int(name: str) -> int | None:
    value = os.getenv(name)
    return int(value) if value not in (None, "") else None


def _optional_bool(name: str) -> bool | None:
    value = os.getenv(name)
    if value in (None, ""):
        return None
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _with_ranks(items: Any) -> list[dict[str, Any]]:
    """Copy API records and add their one-based retrieval rank."""
    if not isinstance(items, list):
        return []
    return [
        {"rank": rank, **item}
        for rank, item in enumerate(items, start=1)
        if isinstance(item, dict)
    ]


def _file_sha256(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for block in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_revision(repo_root: Path) -> str | None:
    configured = os.getenv("LIGHTRAG_REVISION")
    if configured:
        return configured
    try:
        process = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return process.stdout.strip() or None


def _coerce_image_paths(test_case: dict[str, Any]) -> list[str]:
    raw_image_paths = test_case.get("image_paths")
    if raw_image_paths is None:
        raw_image_paths = test_case.get("image_path", [])
    if isinstance(raw_image_paths, str):
        return [raw_image_paths]
    if isinstance(raw_image_paths, list):
        return [str(item) for item in raw_image_paths if item]
    return []


@dataclass(slots=True)
class RetrievalCaseResult:
    """One test case in the legacy evaluator shape, without RAGAS fields."""

    test_number: int
    question: str
    answer: str
    ground_truth: str
    retrieved_contexts: list[str]
    retrieved_chunks: list[dict[str, Any]]
    references: list[dict[str, Any]]
    retrieval: dict[str, list[dict[str, Any]]]
    retrieval_metadata: dict[str, Any]
    query_configuration: dict[str, Any]
    experiment: dict[str, Any]
    request_latency_seconds: float
    project: str
    file_name: str
    image_count: int
    image_paths: list[str]
    timestamp: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_number": self.test_number,
            "question": self.question,
            "answer": self.answer,
            "ground_truth": self.ground_truth,
            "predicted_disease": "",
            "retrieved_contexts": self.retrieved_contexts,
            "retrieved_chunks": self.retrieved_chunks,
            "references": self.references,
            "retrieval": self.retrieval,
            "retrieval_metadata": self.retrieval_metadata,
            "query_configuration": self.query_configuration,
            "experiment": self.experiment,
            "request_latency_seconds": self.request_latency_seconds,
            "project": self.project,
            "file_name": self.file_name,
            "image_count": self.image_count,
            "image_paths": self.image_paths,
            "timestamp": self.timestamp,
            "error": self.error,
        }


class RAGContextCollector:
    """Collect raw retrieval evidence without calculating quality metrics."""

    def __init__(
        self,
        test_dataset_path: str | None = None,
        rag_api_url: str | None = None,
        include_images: bool = True,
        output_path: str | None = None,
        experiment_name: str | None = None,
        workspace: str | None = None,
        merge_plan_path: str | None = None,
    ) -> None:
        self.repo_root = Path(__file__).resolve().parents[2]
        self.results_dir = Path(__file__).parent / "results"
        self.results_dir.mkdir(exist_ok=True)

        self.query_top_k = int(os.getenv("EVAL_QUERY_TOP_K", "40"))
        self.chunk_top_k = int(os.getenv("EVAL_CHUNK_TOP_K", "10"))
        self.max_async = int(os.getenv("EVAL_MAX_CONCURRENT", "2"))
        self.query_mode = os.getenv("EVAL_QUERY_MODE", "hybrid")
        self.enable_rerank = _optional_bool("EVAL_ENABLE_RERANK")
        self.max_entity_tokens = _optional_int("EVAL_MAX_ENTITY_TOKENS")
        self.max_relation_tokens = _optional_int("EVAL_MAX_RELATION_TOKENS")
        self.max_total_tokens = _optional_int("EVAL_MAX_TOTAL_TOKENS")
        self.response_type = os.getenv("EVAL_RESPONSE_TYPE", "Multiple Paragraphs")
        self.user_prompt = os.getenv("EVAL_DIFFERENTIAL_USER_PROMPT") or None
        self.include_images = include_images
        self.api_key = os.getenv("LIGHTRAG_API_KEY")
        merge_plan_value = merge_plan_path or os.getenv("EVAL_MERGE_PLAN")
        self.merge_plan_path = Path(merge_plan_value) if merge_plan_value else None
        self.query_configuration = {
            "mode": self.query_mode,
            "top_k": self.query_top_k,
            "chunk_top_k": self.chunk_top_k,
            "enable_rerank": self.enable_rerank,
            "max_entity_tokens": self.max_entity_tokens,
            "max_relation_tokens": self.max_relation_tokens,
            "max_total_tokens": self.max_total_tokens,
            "response_type": self.response_type,
            "include_images": self.include_images,
        }
        self.experiment = {
            "name": experiment_name
            or os.getenv("EVAL_EXPERIMENT_NAME", "unspecified"),
            "workspace": workspace if workspace is not None else os.getenv("WORKSPACE", ""),
            "merge_plan": str(self.merge_plan_path) if self.merge_plan_path else None,
            "merge_plan_sha256": _file_sha256(self.merge_plan_path),
            "lightrag_revision": _git_revision(self.repo_root),
        }

        if test_dataset_path is None:
            test_dataset_path = str(Path(__file__).parent / "sample_dataset.json")
        if rag_api_url is None:
            rag_api_url = os.getenv("LIGHTRAG_API_URL", "http://localhost:9621")

        self.test_dataset_path = self._resolve_existing_path(test_dataset_path)
        self.rag_api_url = rag_api_url.rstrip("/")
        self.output_path = Path(output_path) if output_path else None
        self.test_cases = self._load_test_dataset()

        self._display_configuration()

    def _display_configuration(self) -> None:
        logger.info("Retrieval collection configuration:")
        logger.info("  • Query Mode:           %s", self.query_mode)
        logger.info("  • Query Top-K:          %s Entities/Relations", self.query_top_k)
        logger.info("  • Chunk Top-K:          %s Chunks", self.chunk_top_k)
        logger.info("  • Query Images:         %s", self.include_images)
        logger.info("  • API Concurrency:      %s", self.max_async)
        logger.info("  • Total Test Cases:     %s", len(self.test_cases))
        logger.info("  • Test Dataset:         %s", self.test_dataset_path)
        logger.info("  • LightRAG API:         %s", self.rag_api_url)

    def _resolve_existing_path(self, raw_path: Path | str) -> Path:
        candidate = Path(raw_path)
        if candidate.exists():
            return candidate
        if not candidate.is_absolute():
            script_candidate = Path(__file__).resolve().parent / candidate
            if script_candidate.exists():
                return script_candidate
            repo_candidate = self.repo_root / candidate
            if repo_candidate.exists():
                return repo_candidate
        return candidate

    def _resolve_image_path(self, image_path: str) -> Path:
        resolved_path = self._resolve_existing_path(image_path)
        if resolved_path.exists():
            return resolved_path
        raise FileNotFoundError(f"Image file not found: {image_path}")

    async def _encode_image_paths(self, image_paths: list[str]) -> list[str]:
        encoded_images: list[str] = []
        for image_path in image_paths[:MAX_QUERY_IMAGES]:
            resolved_path = self._resolve_image_path(image_path)
            image_bytes = await asyncio.to_thread(resolved_path.read_bytes)
            encoded_images.append(base64.b64encode(image_bytes).decode("utf-8"))
        return encoded_images

    def _load_test_dataset(self) -> list[dict[str, Any]]:
        if not self.test_dataset_path.exists():
            raise FileNotFoundError(f"Test dataset not found: {self.test_dataset_path}")

        with self.test_dataset_path.open(encoding="utf-8") as file_handle:
            data = json.load(file_handle)

        if isinstance(data, list):
            test_cases = data
        elif isinstance(data, dict):
            test_cases = data.get("test_cases", [])
        else:
            raise ValueError(
                "Unsupported dataset format. Expected a list or an object with test_cases."
            )

        if not isinstance(test_cases, list):
            raise ValueError("Dataset test_cases must be a list.")
        normalized_cases = [case for case in test_cases if isinstance(case, dict)]
        if len(normalized_cases) != len(test_cases):
            logger.warning(
                "Skipped %s non-object test cases in %s",
                len(test_cases) - len(normalized_cases),
                self.test_dataset_path,
            )
        return normalized_cases

    async def retrieve_context(
        self,
        *,
        question: str,
        client: httpx.AsyncClient,
        image_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        """Call ``/query/full`` and preserve the answer and retrieval evidence."""
        payload: dict[str, Any] = {
            "query": question,
            "mode": self.query_mode,
            "response_type": self.response_type,
            "top_k": self.query_top_k,
            "chunk_top_k": self.chunk_top_k,
        }
        if self.user_prompt is not None:
            payload["user_prompt"] = self.user_prompt
        optional_parameters = {
            "enable_rerank": self.enable_rerank,
            "max_entity_tokens": self.max_entity_tokens,
            "max_relation_tokens": self.max_relation_tokens,
            "max_total_tokens": self.max_total_tokens,
        }
        payload.update(
            {key: value for key, value in optional_parameters.items() if value is not None}
        )
        if self.include_images and image_paths:
            payload["images"] = await self._encode_image_paths(image_paths)

        headers = {"X-API-Key": self.api_key} if self.api_key else None
        try:
            response = await client.post(
                f"{self.rag_api_url}/query/full",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            result = response.json()
        except httpx.ConnectError as exc:
            raise RuntimeError(
                f"Cannot connect to LightRAG API at {self.rag_api_url}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"LightRAG API error {exc.response.status_code}: {exc.response.text}"
            ) from exc
        except httpx.ReadTimeout as exc:
            raise RuntimeError(
                f"LightRAG request timed out for: {question[:100]}"
            ) from exc
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError("LightRAG API returned invalid JSON") from exc

        if not isinstance(result, dict):
            raise RuntimeError("LightRAG API returned a non-object JSON response")

        data = result.get("data", {})
        metadata = result.get("metadata", {})
        llm_response = result.get("llm_response", {})
        if not all(isinstance(value, dict) for value in (data, metadata, llm_response)):
            raise RuntimeError("LightRAG /query/full returned an invalid response shape")

        retrieval = {
            "entities": _with_ranks(data.get("entities", [])),
            "relationships": _with_ranks(data.get("relationships", [])),
            "chunks": _with_ranks(data.get("chunks", [])),
            "references": _with_ranks(data.get("references", [])),
        }
        return {
            "answer": str(llm_response.get("content", "")),
            "contexts": [
                str(chunk.get("content", ""))
                for chunk in retrieval["chunks"]
                if chunk.get("content")
            ],
            "retrieved_chunks": retrieval["chunks"],
            "references": retrieval["references"],
            "retrieval": retrieval,
            "retrieval_metadata": metadata,
        }

    async def collect_single_case(
        self,
        idx: int,
        test_case: dict[str, Any],
        semaphore: asyncio.Semaphore,
        client: httpx.AsyncClient,
    ) -> dict[str, Any]:
        question = str(test_case.get("question", ""))
        ground_truth = str(test_case.get("ground_truth", ""))
        project = str(test_case.get("project", "unknown"))
        file_name = str(test_case.get("file_name", ""))
        image_paths = _coerce_image_paths(test_case)
        image_count = min(len(image_paths), MAX_QUERY_IMAGES) if self.include_images else 0
        request_started_at = time.perf_counter()

        try:
            if not question.strip():
                raise ValueError("Test case has an empty question")
            async with semaphore:
                response = await self.retrieve_context(
                    question=question,
                    client=client,
                    image_paths=image_paths,
                )
            request_latency = round(time.perf_counter() - request_started_at, 4)
            result = RetrievalCaseResult(
                test_number=idx,
                question=question,
                answer=response["answer"],
                ground_truth=ground_truth,
                retrieved_contexts=response["contexts"],
                retrieved_chunks=response["retrieved_chunks"],
                references=response["references"],
                retrieval=response["retrieval"],
                retrieval_metadata=response["retrieval_metadata"],
                query_configuration=dict(self.query_configuration),
                experiment=dict(self.experiment),
                request_latency_seconds=request_latency,
                project=project,
                file_name=file_name,
                image_count=image_count,
                image_paths=image_paths,
                timestamp=datetime.now().isoformat(),
            )
        except (RuntimeError, ValueError, FileNotFoundError) as exc:
            request_latency = round(time.perf_counter() - request_started_at, 4)
            logger.error("Context collection failed for case %s: %s", idx, exc)
            result = RetrievalCaseResult(
                test_number=idx,
                question=question,
                answer="",
                ground_truth=ground_truth,
                retrieved_contexts=[],
                retrieved_chunks=[],
                references=[],
                retrieval={
                    "entities": [],
                    "relationships": [],
                    "chunks": [],
                    "references": [],
                },
                retrieval_metadata={},
                query_configuration=dict(self.query_configuration),
                experiment=dict(self.experiment),
                request_latency_seconds=request_latency,
                project=project,
                file_name=file_name,
                image_count=image_count,
                image_paths=image_paths,
                timestamp=datetime.now().isoformat(),
                error=str(exc),
            )
        return result.to_dict()

    async def collect(self) -> list[dict[str, Any]]:
        logger.info("Starting raw retrieval-context collection")
        semaphore = asyncio.Semaphore(self.max_async)
        timeout = httpx.Timeout(
            TOTAL_TIMEOUT_SECONDS,
            connect=CONNECT_TIMEOUT_SECONDS,
            read=READ_TIMEOUT_SECONDS,
        )
        limits = httpx.Limits(
            max_connections=self.max_async + 1,
            max_keepalive_connections=self.max_async,
        )
        async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
            tasks = [
                asyncio.create_task(
                    self.collect_single_case(idx, test_case, semaphore, client)
                )
                for idx, test_case in enumerate(self.test_cases, start=1)
            ]
            results: list[dict[str, Any]] = []
            successful = 0
            failed = 0
            with tqdm(
                total=len(tasks),
                desc="Collecting cases",
                unit="case",
                dynamic_ncols=True,
            ) as progress:
                for completed_task in asyncio.as_completed(tasks):
                    result = await completed_task
                    results.append(result)
                    if result.get("error"):
                        failed += 1
                    else:
                        successful += 1
                    progress.set_postfix(ok=successful, failed=failed, refresh=False)
                    progress.update()

            # Completion order varies with concurrency; preserve dataset order
            # in the artifact so baseline and merged files align by index.
            return sorted(results, key=lambda item: int(item["test_number"]))

    def _resolve_output_path(self) -> Path:
        if self.output_path is not None:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            return self.output_path
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.results_dir / f"{DEFAULT_RESULTS_BASENAME}_{timestamp}.json"

    async def run(self) -> dict[str, Any]:
        started_at = time.perf_counter()
        results = await self.collect()
        elapsed = round(time.perf_counter() - started_at, 2)
        successful = sum(not result.get("error") for result in results)
        failed = len(results) - successful
        summary = {
            "schema_version": 1,
            "collection_type": "lightrag_answers_and_contexts",
            "metrics_computed": False,
            "timestamp": datetime.now().isoformat(),
            "dataset": str(self.test_dataset_path),
            "rag_api_url": self.rag_api_url,
            "query_configuration": {
                **self.query_configuration,
                "max_concurrent": self.max_async,
            },
            "experiment": self.experiment,
            "total_tests": len(results),
            "total_cases": len(results),
            "successful_cases": successful,
            "failed_cases": failed,
            "benchmark_stats": {
                "total_tests": len(results),
                "successful_tests": successful,
                "failed_tests": failed,
                "success_rate": round(
                    successful / len(results) * 100 if results else 0.0, 2
                ),
            },
            "elapsed_time_seconds": elapsed,
            "results": results,
        }

        output_path = self._resolve_output_path()
        temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
        with temporary_path.open("w", encoding="utf-8") as file_handle:
            json.dump(summary, file_handle, indent=2, ensure_ascii=False)
        temporary_path.replace(output_path)

        logger.info("Context collection complete")
        logger.info("  • Total:      %s", len(results))
        logger.info("  • Successful: %s", successful)
        logger.info("  • Failed:     %s", failed)
        logger.info("  • Elapsed:    %.2f seconds", elapsed)
        logger.info("  • JSON:       %s", output_path.absolute())
        return summary


# Preserve the old import name for callers that imported this script directly.
RAGEvaluator = RAGContextCollector


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Collect raw structured LightRAG contexts without RAGAS or answer scoring"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python lightrag/evaluation/eval_rag_context_differential_ollama.py \\
    --dataset lightrag/evaluation/fold1_test_subset_0_416.json
  python lightrag/evaluation/eval_rag_context_differential_ollama.py \\
    --dataset my_test.json --ragendpoint http://localhost:9621 --no-images
  python lightrag/evaluation/eval_rag_context_differential_ollama.py \\
    --dataset my_test.json --output results/baseline_contexts.json
    python eval_rag_context_differential_ollama.py --dataset fold1_test_subset_0_416.json --output results/baseline_contexts.json --experiment-name baseline
        """,
    )
    parser.add_argument(
        "--dataset",
        "-d",
        default=None,
        help="Dataset JSON path (default: evaluation/sample_dataset.json)",
    )
    parser.add_argument(
        "--ragendpoint",
        "-r",
        default=None,
        help="LightRAG API URL (default: $LIGHTRAG_API_URL or localhost:9621)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Exact output JSON path (default: timestamped file in results/)",
    )
    parser.add_argument(
        "--no-images",
        action="store_false",
        dest="include_images",
        help="Do not send dataset images to the LightRAG query API",
    )
    parser.add_argument(
        "--experiment-name",
        default=None,
        help="Experiment label, for example baseline or merged",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="Storage workspace identifier recorded in the result",
    )
    parser.add_argument(
        "--merge-plan",
        default=None,
        help="Merge-plan path; its SHA-256 is recorded for reproducibility",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    collector = RAGContextCollector(
        test_dataset_path=args.dataset,
        rag_api_url=args.ragendpoint,
        include_images=args.include_images,
        output_path=args.output,
        experiment_name=args.experiment_name,
        workspace=args.workspace,
        merge_plan_path=args.merge_plan,
    )
    await collector.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        logger.exception("Context collection failed: %s", exc)
        sys.exit(1)
