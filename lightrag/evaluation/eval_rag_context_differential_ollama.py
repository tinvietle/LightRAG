#!/usr/bin/env python3
"""
RAGAS context-recall evaluation for LightRAG differential diagnosis workflows.

This evaluator keeps the current LightRAG evaluation/export style where practical,
but it is intentionally narrower than ``eval_rag_quality.py``:

- differential-diagnosis query prompt instead of single-answer QA
- no disease-label parsing or forced answer normalization
- native Ollama evaluator LLM wrapper
- RAGAS ``ContextRecall`` only
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from dotenv import load_dotenv
from langchain_core.outputs import Generation, LLMResult
from ollama import AsyncClient as OllamaAsyncClient
from ollama import Client as OllamaClient
from ragas.llms.base import BaseRagasLLM
from ragas.run_config import RunConfig

from lightrag.utils import logger

if TYPE_CHECKING:
    from langchain_core.callbacks import Callbacks
    from langchain_core.prompt_values import PromptValue


sys.path.insert(0, str(Path(__file__).parent.parent.parent))
load_dotenv(dotenv_path=".env", override=False)

try:
    from datasets import Dataset
    from ragas import evaluate
    from ragas.metrics import ContextRecall
    from tqdm.auto import tqdm

    RAGAS_AVAILABLE = True
except ImportError:
    RAGAS_AVAILABLE = False
    Dataset = None
    evaluate = None


CONNECT_TIMEOUT_SECONDS = 360.0
READ_TIMEOUT_SECONDS = 360.0
TOTAL_TIMEOUT_SECONDS = 360.0
MAX_QUERY_IMAGES = 10
DEFAULT_RESULTS_BASENAME = "results"


def _is_nan(value: Any) -> bool:
    if value is None:
        return True
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return True
    return math.isnan(numeric_value) or math.isinf(numeric_value)


def _sanitize_metric_value(value: Any) -> float | None:
    if _is_nan(value):
        return None
    return float(value)


def _normalize_ollama_host(host: str | None) -> str:
    normalized_host = (
        host or os.getenv("OLLAMA_HOST") or "http://localhost:11434"
    ).rstrip("/")
    if normalized_host.endswith("/v1"):
        return normalized_host[:-3]
    return normalized_host


def _parse_ollama_think(value: str | None) -> bool | str:
    normalized_value = (value or "").strip().lower()
    if not normalized_value:
        return False
    if normalized_value in {"0", "false", "no", "off"}:
        return False
    if normalized_value in {"1", "true", "yes", "on"}:
        return True
    if normalized_value in {"low", "medium", "high"}:
        return normalized_value
    logger.warning(
        "Invalid EVAL_OLLAMA_THINK=%s. Falling back to think=False.",
        value,
    )
    return False


def _extract_ollama_content(response: Any) -> str:
    message = getattr(response, "message", None)
    if message is None and isinstance(response, dict):
        message = response.get("message", {})
    if isinstance(message, dict):
        return str(message.get("content", ""))
    return str(getattr(message, "content", ""))


def _extract_ollama_done_reason(response: Any) -> str | None:
    done_reason = getattr(response, "done_reason", None)
    if done_reason is not None:
        return str(done_reason)
    if isinstance(response, dict):
        raw_done_reason = response.get("done_reason")
        if raw_done_reason is not None:
            return str(raw_done_reason)
    return None


def _coerce_image_paths(test_case: dict[str, Any]) -> list[str]:
    raw_image_paths = test_case.get("image_paths")
    if raw_image_paths is None:
        raw_image_paths = test_case.get("image_path", [])
    if isinstance(raw_image_paths, str):
        return [raw_image_paths]
    if isinstance(raw_image_paths, list):
        return [str(item) for item in raw_image_paths if item]
    return []


@dataclass
class EvalCaseResult:
    test_number: int
    question: str
    answer: str
    ground_truth: str
    retrieved_contexts: list[str]
    missing_metrics: list[str]
    project: str
    image_count: int
    image_paths: list[str]
    metrics: dict[str, float | None]
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
            "missing_metrics": self.missing_metrics,
            "project": self.project,
            "image_count": self.image_count,
            "image_paths": self.image_paths,
            "error": self.error,
            "metrics": self.metrics,
            "timestamp": self.timestamp,
        }


@dataclass(kw_only=True)
class OllamaRagasLLM(BaseRagasLLM):
    """Minimal native Ollama wrapper for RAGAS."""

    model: str
    host: str
    timeout: int
    think: bool | str = False

    def __post_init__(self) -> None:
        super().__post_init__()
        self.sync_client = OllamaClient(host=self.host, timeout=self.timeout)
        self.async_client = OllamaAsyncClient(host=self.host, timeout=self.timeout)

    def _chat_options(
        self,
        *,
        temperature: float,
        stop: list[str] | None,
    ) -> dict[str, Any]:
        options: dict[str, Any] = {"temperature": temperature}
        if stop:
            options["stop"] = stop
        return options

    @staticmethod
    def _prompt_to_text(prompt: "PromptValue") -> str:
        return prompt.to_string()

    @staticmethod
    def _build_result(responses: list[Any]) -> LLMResult:
        return LLMResult(
            generations=[
                [
                    Generation(
                        text=_extract_ollama_content(response),
                        generation_info={
                            "finish_reason": _extract_ollama_done_reason(response)
                            or "stop"
                        },
                    )
                    for response in responses
                ]
            ]
        )

    def generate_text(
        self,
        prompt: "PromptValue",
        n: int = 1,
        temperature: float = 0.01,
        stop: list[str] | None = None,
        callbacks: "Callbacks" = None,
    ) -> LLMResult:
        del callbacks
        prompt_text = self._prompt_to_text(prompt)
        responses = [
            self.sync_client.chat(
                model=self.model,
                messages=[{"role": "user", "content": prompt_text}],
                stream=False,
                think=self.think,
                options=self._chat_options(temperature=temperature, stop=stop),
            )
            for _ in range(n)
        ]
        return self._build_result(responses)

    async def agenerate_text(
        self,
        prompt: "PromptValue",
        n: int = 1,
        temperature: float | None = 0.01,
        stop: list[str] | None = None,
        callbacks: "Callbacks" = None,
    ) -> LLMResult:
        del callbacks
        prompt_text = self._prompt_to_text(prompt)
        effective_temperature = 0.01 if temperature is None else temperature
        responses: list[Any] = []
        for _ in range(n):
            response = await self.async_client.chat(
                model=self.model,
                messages=[{"role": "user", "content": prompt_text}],
                stream=False,
                think=self.think,
                options=self._chat_options(
                    temperature=effective_temperature,
                    stop=stop,
                ),
            )
            responses.append(response)
        return self._build_result(responses)

    @staticmethod
    def is_finished(response: LLMResult) -> bool:
        valid_finish_reasons = {"stop", "STOP", "eos_token", "load"}
        for generation_group in response.generations:
            for generation in generation_group:
                finish_reason = None
                if generation.generation_info is not None:
                    finish_reason = generation.generation_info.get("finish_reason")
                if (
                    finish_reason is not None
                    and finish_reason not in valid_finish_reasons
                ):
                    return False
        return True


class RAGEvaluator:
    """Evaluate LightRAG retrieval quality for differential diagnosis."""

    def __init__(self, test_dataset_path: str | None = None, rag_api_url: str | None = None):
        if not RAGAS_AVAILABLE:
            raise ImportError(
                "RAGAS dependencies not installed. Install with: pip install ragas datasets"
            )

        self.repo_root = Path(__file__).resolve().parents[2]
        self.results_dir = Path(__file__).parent / "results"
        self.results_dir.mkdir(exist_ok=True)

        self.eval_model = os.getenv("EVAL_LLM_MODEL", "gpt-oss:120b-cloud")
        self.eval_llm_base_url = _normalize_ollama_host(
            os.getenv("EVAL_LLM_BINDING_HOST")
        )
        self.eval_timeout = int(os.getenv("EVAL_LLM_TIMEOUT", "180"))
        self.eval_think = _parse_ollama_think(os.getenv("EVAL_OLLAMA_THINK"))
        self.eval_max_retries = int(os.getenv("EVAL_LLM_MAX_RETRIES", "5"))
        self.query_top_k = int(os.getenv("EVAL_QUERY_TOP_K", "10"))
        self.max_async = int(os.getenv("EVAL_MAX_CONCURRENT", "2"))
        self.query_mode = os.getenv("EVAL_QUERY_MODE", "hybrid")
        self.response_type = os.getenv("EVAL_RESPONSE_TYPE", "Multiple Paragraphs")
        self.user_prompt = os.getenv(
            "EVAL_DIFFERENTIAL_USER_PROMPT",
            (
                "Given the clinical case, identify the differential diagnosis and "
                "provide relevant supporting information from the retrieved contexts."
            ),
        )
        self.api_key = os.getenv("LIGHTRAG_API_KEY")

        self.eval_llm = OllamaRagasLLM(
            model=self.eval_model,
            host=self.eval_llm_base_url,
            timeout=self.eval_timeout,
            think=self.eval_think,
            run_config=RunConfig(timeout=self.eval_timeout),
        )

        if test_dataset_path is None:
            test_dataset_path = str(Path(__file__).parent / "sample_dataset.json")
        if rag_api_url is None:
            rag_api_url = os.getenv("LIGHTRAG_API_URL", "http://localhost:9621")

        self.test_dataset_path = self._resolve_existing_path(test_dataset_path)
        self.rag_api_url = rag_api_url.rstrip("/")
        self.test_cases = self._load_test_dataset()

        self._display_configuration()

    def _display_configuration(self) -> None:
        logger.info("Evaluation Model:")
        logger.info("  • LLM Model:            %s", self.eval_model)
        logger.info("  • LLM Endpoint:         %s", self.eval_llm_base_url)
        logger.info("  • Ollama Think Mode:    %s", self.eval_think)

        logger.info("Concurrency & Rate Limiting:")
        logger.info("  • Query Mode:           %s", self.query_mode)
        logger.info("  • Query Top-K:          %s Entities/Relations", self.query_top_k)
        logger.info("  • LLM Max Retries:      %s", self.eval_max_retries)
        logger.info("  • LLM Timeout:          %s seconds", self.eval_timeout)
        logger.info("  • Eval Concurrency:     %s", self.max_async)

        logger.info("Test Configuration:")
        logger.info("  • Total Test Cases:     %s", len(self.test_cases))
        logger.info("  • Test Dataset:         %s", self.test_dataset_path.name)
        logger.info("  • LightRAG API:         %s", self.rag_api_url)
        logger.info("  • Results Directory:    %s", self.results_dir.name)

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
        raise FileNotFoundError(
            f"Image file not found: {image_path} "
            f"(tried {Path(image_path)} and {self.repo_root / Path(image_path)})"
        )

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

        normalized_cases = [case for case in test_cases if isinstance(case, dict)]
        if len(normalized_cases) != len(test_cases):
            logger.warning(
                "Skipped %s non-object test case entries while loading %s",
                len(test_cases) - len(normalized_cases),
                self.test_dataset_path,
            )
        return normalized_cases

    async def generate_rag_response(
        self,
        *,
        question: str,
        client: httpx.AsyncClient,
        image_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        try:
            payload: dict[str, Any] = {
                "query": question,
                "mode": self.query_mode,
                "include_references": True,
                "include_chunk_content": True,
                "response_type": self.response_type,
                "top_k": self.query_top_k,
                "user_prompt": self.user_prompt,
            }
            if image_paths:
                payload["images"] = await self._encode_image_paths(image_paths)

            headers = {"X-API-Key": self.api_key} if self.api_key else None
            response = await client.post(
                f"{self.rag_api_url}/query",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()

            result = response.json()
            answer = result.get("response", "No response generated")
            references = result.get("references", [])

            contexts: list[str] = []
            for reference in references:
                content = reference.get("content", [])
                if isinstance(content, list):
                    contexts.extend(str(item) for item in content if item)
                elif isinstance(content, str) and content:
                    contexts.append(content)

            return {"answer": answer, "contexts": contexts}

        except httpx.ConnectError as exc:
            raise RuntimeError(
                f"Cannot connect to LightRAG API at {self.rag_api_url}\n"
                "Make sure LightRAG server is running:\n"
                "python -m lightrag.api.lightrag_server"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"LightRAG API error {exc.response.status_code}: {exc.response.text}"
            ) from exc
        except httpx.ReadTimeout as exc:
            raise RuntimeError(
                f"Request timeout while waiting for LightRAG response for: {question[:100]}"
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                f"Error calling LightRAG API: {type(exc).__name__}: {exc}"
            ) from exc

    @staticmethod
    def _build_failed_case_result(
        *,
        idx: int,
        question: str,
        ground_truth: str,
        project: str,
        image_paths: list[str],
        error: str,
        answer: str = "",
        retrieved_contexts: list[str] | None = None,
    ) -> dict[str, Any]:
        result = EvalCaseResult(
            test_number=idx,
            question=question,
            answer=answer,
            ground_truth=ground_truth,
            retrieved_contexts=retrieved_contexts or [],
            missing_metrics=[],
            project=project,
            image_count=len(image_paths),
            image_paths=image_paths,
            metrics={},
            timestamp=datetime.now().isoformat(),
            error=error,
        )
        return result.to_dict()

    async def evaluate_single_case(
        self,
        idx: int,
        test_case: dict[str, Any],
        rag_semaphore: asyncio.Semaphore,
        eval_semaphore: asyncio.Semaphore,
        client: httpx.AsyncClient,
        position_pool: asyncio.Queue[int],
        pbar_creation_lock: asyncio.Lock,
    ) -> dict[str, Any]:
        async with rag_semaphore:
            question = str(test_case["question"])
            ground_truth = str(test_case["ground_truth"])
            project = str(test_case.get("project", "unknown"))
            image_paths = _coerce_image_paths(test_case)

            try:
                rag_response = await self.generate_rag_response(
                    question=question,
                    client=client,
                    image_paths=image_paths,
                )
            except Exception as exc:
                logger.error("Error generating response for test %s: %s", idx, exc)
                return self._build_failed_case_result(
                    idx=idx,
                    question=question,
                    ground_truth=ground_truth,
                    project=project,
                    image_paths=image_paths,
                    error=str(exc),
                )

            answer = rag_response["answer"]
            retrieved_contexts = rag_response["contexts"]
            eval_dataset = Dataset.from_dict(
                {
                    "question": [question],
                    "answer": [answer],
                    "contexts": [retrieved_contexts],
                    "ground_truth": [ground_truth],
                }
            )

            async with eval_semaphore:
                pbar = None
                position = None
                try:
                    position = await position_pool.get()
                    async with pbar_creation_lock:
                        pbar = tqdm(
                            total=1,
                            desc=f"Eval-{idx:02d}",
                            position=position,
                            leave=False,
                        )
                        await asyncio.sleep(0.05)

                    eval_results = evaluate(
                        dataset=eval_dataset,
                        metrics=[ContextRecall()],
                        llm=self.eval_llm,
                        _pbar=pbar,
                    )
                    scores_row = eval_results.to_pandas().iloc[0]

                    context_recall = _sanitize_metric_value(
                        scores_row.get("context_recall")
                    )
                    metrics = {"context_recall": context_recall}
                    missing_metrics = [
                        metric_name
                        for metric_name, metric_value in metrics.items()
                        if metric_value is None
                    ]

                    result = EvalCaseResult(
                        test_number=idx,
                        question=question,
                        answer=answer,
                        ground_truth=ground_truth,
                        retrieved_contexts=retrieved_contexts,
                        missing_metrics=missing_metrics,
                        project=project,
                        image_count=len(image_paths),
                        image_paths=image_paths,
                        metrics=metrics,
                        timestamp=datetime.now().isoformat(),
                    )
                    return result.to_dict()
                except Exception as exc:
                    logger.error("Error evaluating test %s: %s", idx, exc)
                    return self._build_failed_case_result(
                        idx=idx,
                        question=question,
                        ground_truth=ground_truth,
                        project=project,
                        image_paths=image_paths,
                        answer=answer,
                        retrieved_contexts=retrieved_contexts,
                        error=str(exc),
                    )
                finally:
                    if pbar is not None:
                        pbar.close()
                    if position is not None:
                        await position_pool.put(position)

    async def evaluate_responses(self) -> list[dict[str, Any]]:
        logger.info("%s", "=" * 70)
        logger.info("Starting differential-diagnosis context recall evaluation")
        logger.info("RAGAS Evaluation (Stage 2): %s concurrent", self.max_async)
        logger.info("%s", "=" * 70)

        rag_semaphore = asyncio.Semaphore(self.max_async * 2)
        eval_semaphore = asyncio.Semaphore(self.max_async)
        position_pool: asyncio.Queue[int] = asyncio.Queue()
        for index in range(self.max_async):
            await position_pool.put(index)
        pbar_creation_lock = asyncio.Lock()

        timeout = httpx.Timeout(
            TOTAL_TIMEOUT_SECONDS,
            connect=CONNECT_TIMEOUT_SECONDS,
            read=READ_TIMEOUT_SECONDS,
        )
        limits = httpx.Limits(
            max_connections=(self.max_async + 1) * 2,
            max_keepalive_connections=self.max_async + 1,
        )

        async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
            tasks = [
                self.evaluate_single_case(
                    idx,
                    test_case,
                    rag_semaphore,
                    eval_semaphore,
                    client,
                    position_pool,
                    pbar_creation_lock,
                )
                for idx, test_case in enumerate(self.test_cases, start=1)
            ]
            return list(await asyncio.gather(*tasks))

    @staticmethod
    def _csv_metric(value: Any) -> str:
        sanitized = _sanitize_metric_value(value)
        if sanitized is None:
            return "N/A"
        return f"{sanitized:.4f}"

    @staticmethod
    def _format_metric(value: Any, width: int = 6) -> str:
        sanitized = _sanitize_metric_value(value)
        if sanitized is None:
            return "N/A".center(width)
        return f"{sanitized:.4f}".rjust(width)

    def _export_to_csv(self, results: list[dict[str, Any]]) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = self.results_dir / f"{DEFAULT_RESULTS_BASENAME}_{timestamp}.csv"
        fieldnames = [
            "test_number",
            "question",
            "answer",
            "ground_truth",
            "predicted_disease",
            "retrieved_contexts",
            "missing_metrics",
            "project",
            "image_count",
            "image_paths",
            "faithfulness",
            "answer_relevance",
            "context_recall",
            "context_precision",
            "status",
            "timestamp",
        ]

        with csv_path.open("w", newline="", encoding="utf-8") as file_handle:
            writer = csv.DictWriter(file_handle, fieldnames=fieldnames)
            writer.writeheader()
            for result in results:
                metrics = result.get("metrics", {})
                has_metrics = bool(metrics)
                missing_metrics = result.get("missing_metrics", [])
                writer.writerow(
                    {
                        "test_number": result.get("test_number", ""),
                        "question": result.get("question", ""),
                        "answer": result.get("answer", ""),
                        "ground_truth": result.get("ground_truth", ""),
                        "predicted_disease": "",
                        "retrieved_contexts": json.dumps(
                            result.get("retrieved_contexts", []), ensure_ascii=False
                        ),
                        "missing_metrics": json.dumps(
                            missing_metrics, ensure_ascii=False
                        ),
                        "project": result.get("project", "unknown"),
                        "image_count": result.get("image_count", 0),
                        "image_paths": json.dumps(
                            result.get("image_paths", []), ensure_ascii=False
                        ),
                        "faithfulness": "N/A",
                        "answer_relevance": "N/A",
                        "context_recall": self._csv_metric(
                            metrics.get("context_recall")
                        ),
                        "context_precision": "N/A",
                        "status": (
                            "success"
                            if has_metrics and not missing_metrics
                            else "partial"
                            if has_metrics
                            else "error"
                        ),
                        "timestamp": result.get("timestamp", ""),
                    }
                )

        return csv_path

    def _display_results_table(self, results: list[dict[str, Any]]) -> None:
        logger.info("")
        logger.info("%s", "=" * 90)
        logger.info("EVALUATION RESULTS SUMMARY")
        logger.info("%s", "=" * 90)
        logger.info(
            "%-4s | %-58s | %6s | %6s",
            "#",
            "Question",
            "CtxRec",
            "Status",
        )
        logger.info("%s", "-" * 90)

        for result in results:
            test_number = result.get("test_number", 0)
            question = str(result.get("question", ""))
            question_display = (
                question[:55] + "..." if len(question) > 58 else question
            )
            metrics = result.get("metrics", {})
            if metrics:
                status = "!" if result.get("missing_metrics") else "✓"
                logger.info(
                    "%-4d | %-58s | %s | %6s",
                    test_number,
                    question_display,
                    self._format_metric(metrics.get("context_recall"), 6),
                    status,
                )
                continue

            error = str(result.get("error", "Unknown error"))
            error_display = error[:20] + "..." if len(error) > 23 else error
            logger.info(
                "%-4d | %-58s | %6s | ✗ %s",
                test_number,
                question_display,
                "N/A",
                error_display,
            )

        logger.info("%s", "=" * 90)

    def _calculate_benchmark_stats(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        valid_results = [result for result in results if result.get("metrics")]
        total_tests = len(results)
        successful_tests = len(valid_results)
        failed_tests = total_tests - successful_tests

        if not valid_results:
            return {
                "total_tests": total_tests,
                "successful_tests": 0,
                "failed_tests": failed_tests,
                "success_rate": 0.0,
                "average_metrics": {
                    "faithfulness": 0.0,
                    "answer_relevance": 0.0,
                    "context_recall": 0.0,
                    "context_precision": 0.0,
                },
            }

        context_recall_values = [
            value
            for value in (
                _sanitize_metric_value(
                    result.get("metrics", {}).get("context_recall")
                )
                for result in valid_results
            )
            if value is not None
        ]
        avg_context_recall = (
            round(sum(context_recall_values) / len(context_recall_values), 4)
            if context_recall_values
            else 0.0
        )

        return {
            "total_tests": total_tests,
            "successful_tests": successful_tests,
            "failed_tests": failed_tests,
            "success_rate": round(
                (successful_tests / total_tests * 100) if total_tests else 0.0, 2
            ),
            "average_metrics": {
                "faithfulness": 0.0,
                "answer_relevance": 0.0,
                "context_recall": avg_context_recall,
                "context_precision": 0.0,
            },
        }

    async def run(self) -> dict[str, Any]:
        start_time = time.time()
        results = await self.evaluate_responses()
        elapsed_time = time.time() - start_time

        benchmark_stats = self._calculate_benchmark_stats(results)
        summary = {
            "timestamp": datetime.now().isoformat(),
            "total_tests": len(results),
            "elapsed_time_seconds": round(elapsed_time, 2),
            "benchmark_stats": benchmark_stats,
            "results": results,
        }

        self._display_results_table(results)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_path = self.results_dir / f"{DEFAULT_RESULTS_BASENAME}_{timestamp}.json"
        with json_path.open("w", encoding="utf-8") as file_handle:
            json.dump(summary, file_handle, indent=2, allow_nan=False)

        csv_path = self._export_to_csv(results)

        logger.info("")
        logger.info("%s", "=" * 70)
        logger.info("EVALUATION COMPLETE")
        logger.info("%s", "=" * 70)
        logger.info("Total Tests:    %s", len(results))
        logger.info("Successful:     %s", benchmark_stats["successful_tests"])
        logger.info("Failed:         %s", benchmark_stats["failed_tests"])
        logger.info("Success Rate:   %.2f%%", benchmark_stats["success_rate"])
        logger.info("Elapsed Time:   %.2f seconds", elapsed_time)
        if results:
            logger.info("Avg Time/Test:  %.2f seconds", elapsed_time / len(results))

        logger.info("")
        logger.info("%s", "=" * 70)
        logger.info("BENCHMARK RESULTS (Average)")
        logger.info("%s", "=" * 70)
        avg = benchmark_stats["average_metrics"]
        logger.info("Average Context Recall:    %.4f", avg["context_recall"])

        logger.info("")
        logger.info("%s", "=" * 70)
        logger.info("GENERATED FILES")
        logger.info("%s", "=" * 70)
        logger.info("Results Dir:    %s", self.results_dir.absolute())
        logger.info("   • CSV:  %s", csv_path.name)
        logger.info("   • JSON: %s", json_path.name)
        logger.info("%s", "=" * 70)

        return summary


async def main() -> None:
    try:
        parser = argparse.ArgumentParser(
            description=(
                "RAGAS context recall evaluation for LightRAG differential diagnosis"
            ),
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
Examples:
  python lightrag/evaluation/eval_rag_context_differential_ollama.py
  python lightrag/evaluation/eval_rag_context_differential_ollama.py --dataset my_test.json
  python lightrag/evaluation/eval_rag_context_differential_ollama.py --ragendpoint http://localhost:9621
            """,
        )
        parser.add_argument(
            "--dataset",
            "-d",
            type=str,
            default=None,
            help=(
                "Path to test dataset JSON file "
                "(default: sample_dataset.json in evaluation directory)"
            ),
        )
        parser.add_argument(
            "--ragendpoint",
            "-r",
            type=str,
            default=None,
            help=(
                "LightRAG API endpoint URL "
                "(default: http://localhost:9621 or $LIGHTRAG_API_URL)"
            ),
        )
        args = parser.parse_args()

        logger.info("%s", "=" * 70)
        logger.info("Context Recall Evaluation - Using Real LightRAG API")
        logger.info("%s", "=" * 70)

        evaluator = RAGEvaluator(
            test_dataset_path=args.dataset,
            rag_api_url=args.ragendpoint,
        )
        await evaluator.run()
    except Exception as exc:
        logger.exception("Error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
