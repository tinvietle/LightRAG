#!/usr/bin/env python3
"""
RAGAS context-recall evaluation for LightRAG differential diagnosis workflows.

This variant keeps the current evaluator flow and export structure where practical, but it:
- aligns the query prompt with differential diagnosis output
- does not force or parse a single disease label from the answer
- evaluates only the RAGAS `ContextRecall` metric
"""

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
from typing import TYPE_CHECKING, Any, Dict, List

import httpx
from dotenv import load_dotenv
from lightrag.utils import logger
from langchain_core.outputs import Generation, LLMResult
from ollama import AsyncClient as OllamaAsyncClient
from ollama import Client as OllamaClient
from ragas.llms.base import BaseRagasLLM
from ragas.run_config import RunConfig

if TYPE_CHECKING:
    from langchain_core.callbacks import Callbacks
    from langchain_core.prompt_values import PromptValue

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Use the local .env for the current LightRAG instance.
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


def _is_nan(value: Any) -> bool:
    """Return True when a metric value is missing, NaN, or infinite."""
    if value is None:
        return True
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return True
    return math.isnan(numeric_value) or math.isinf(numeric_value)


def _normalize_ollama_host(host: str | None) -> str:
    """Convert OpenAI-style Ollama hosts to the native Ollama base URL."""
    normalized_host = (host or os.getenv("OLLAMA_HOST") or "http://localhost:11434").rstrip(
        "/"
    )
    if normalized_host.endswith("/v1"):
        return normalized_host[:-3]
    return normalized_host


def _parse_ollama_think(value: str | None) -> bool | str:
    """Parse native Ollama think mode from environment."""
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
    """Read assistant content from an Ollama chat response."""
    message = getattr(response, "message", None)
    if message is None and isinstance(response, dict):
        message = response.get("message", {})
    if isinstance(message, dict):
        return str(message.get("content", ""))
    return str(getattr(message, "content", ""))


def _extract_ollama_done_reason(response: Any) -> str | None:
    """Read finish metadata from an Ollama chat response."""
    done_reason = getattr(response, "done_reason", None)
    if done_reason is not None:
        return str(done_reason)
    if isinstance(response, dict):
        done_reason = response.get("done_reason")
        if done_reason is not None:
            return str(done_reason)
    return None


def _sanitize_metric_value(value: Any) -> float | None:
    """Convert a metric to a finite float or None."""
    if _is_nan(value):
        return None
    return float(value)


@dataclass(kw_only=True)
class OllamaRagasLLM(BaseRagasLLM):
    """Minimal native Ollama wrapper for RAGAS."""

    model: str
    host: str
    timeout: int
    think: bool | str = False

    def __post_init__(self):
        super().__post_init__()
        self.sync_client = OllamaClient(host=self.host, timeout=self.timeout)
        self.async_client = OllamaAsyncClient(host=self.host, timeout=self.timeout)

    def _chat_options(
        self,
        temperature: float,
        stop: List[str] | None,
    ) -> Dict[str, Any]:
        options: Dict[str, Any] = {"temperature": temperature}
        if stop:
            options["stop"] = stop
        return options

    def _prompt_to_text(self, prompt: "PromptValue") -> str:
        return prompt.to_string()

    def _build_result(self, responses: List[Any]) -> LLMResult:
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
        stop: List[str] | None = None,
        callbacks: "Callbacks" = None,
    ) -> LLMResult:
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
        stop: List[str] | None = None,
        callbacks: "Callbacks" = None,
    ) -> LLMResult:
        prompt_text = self._prompt_to_text(prompt)
        responses = []
        effective_temperature = 0.01 if temperature is None else temperature
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

    def is_finished(self, response: LLMResult) -> bool:
        valid_finish_reasons = {"stop", "STOP", "eos_token", "load"}
        for generation_group in response.generations:
            for generation in generation_group:
                finish_reason = None
                if generation.generation_info is not None:
                    finish_reason = generation.generation_info.get("finish_reason")
                if finish_reason is not None and finish_reason not in valid_finish_reasons:
                    return False
        return True


class RAGEvaluator:
    """Evaluate LightRAG retrieval quality for differential diagnosis."""

    def __init__(self, test_dataset_path: str = None, rag_api_url: str = None):
        if not RAGAS_AVAILABLE:
            raise ImportError(
                "RAGAS dependencies not installed. "
                "Install with: pip install ragas datasets"
            )

        eval_model = os.getenv("EVAL_LLM_MODEL", "gpt-oss:120b-cloud")
        eval_llm_base_url = _normalize_ollama_host(os.getenv("EVAL_LLM_BINDING_HOST"))
        eval_timeout = int(os.getenv("EVAL_LLM_TIMEOUT", "180"))
        eval_think = _parse_ollama_think(os.getenv("EVAL_OLLAMA_THINK"))

        self.eval_llm = OllamaRagasLLM(
            model=eval_model,
            host=eval_llm_base_url,
            timeout=eval_timeout,
            think=eval_think,
            run_config=RunConfig(timeout=eval_timeout),
        )

        if test_dataset_path is None:
            test_dataset_path = Path(__file__).parent / "sample_dataset.json"

        if rag_api_url is None:
            rag_api_url = os.getenv("LIGHTRAG_API_URL", "http://localhost:9621")

        self.test_dataset_path = Path(test_dataset_path)
        self.rag_api_url = rag_api_url.rstrip("/")
        self.results_dir = Path(__file__).parent / "results"
        self.results_dir.mkdir(exist_ok=True)
        self.repo_root = Path(__file__).resolve().parents[2]

        self.test_cases = self._load_test_dataset()

        self.eval_model = eval_model
        self.eval_llm_base_url = eval_llm_base_url
        self.eval_max_retries = int(os.getenv("EVAL_LLM_MAX_RETRIES", "5"))
        self.eval_timeout = eval_timeout
        self.eval_think = eval_think

        self._display_configuration()

    def _display_configuration(self):
        """Display evaluation configuration."""
        logger.info("Evaluation Model:")
        logger.info("  • LLM Model:            %s", self.eval_model)
        logger.info("  • LLM Endpoint:         %s", self.eval_llm_base_url)
        logger.info("  • Ollama Think Mode:    %s", self.eval_think)

        logger.info("Concurrency & Rate Limiting:")
        query_top_k = int(os.getenv("EVAL_QUERY_TOP_K", "10"))
        logger.info("  • Query Top-K:          %s Entities/Relations", query_top_k)
        logger.info("  • LLM Max Retries:      %s", self.eval_max_retries)
        logger.info("  • LLM Timeout:          %s seconds", self.eval_timeout)

        logger.info("Test Configuration:")
        logger.info("  • Total Test Cases:     %s", len(self.test_cases))
        logger.info("  • Test Dataset:         %s", self.test_dataset_path.name)
        logger.info("  • LightRAG API:         %s", self.rag_api_url)
        logger.info("  • Results Directory:    %s", self.results_dir.name)

    def _resolve_existing_path(self, raw_path: Path | str) -> Path:
        """Resolve paths relative to the current working directory or repo root."""
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
        """Resolve a dataset image path and fail fast when it is missing."""
        resolved_path = self._resolve_existing_path(image_path)
        if not resolved_path.exists():
            raise FileNotFoundError(
                f"Image file not found: {image_path} "
                f"(tried {Path(image_path)} and {self.repo_root / Path(image_path)})"
            )
        return resolved_path

    async def _encode_image_paths(self, image_paths: List[str]) -> List[str]:
        """Read image files from disk and return base64 payloads."""
        encoded_images: List[str] = []
        for image_path in image_paths[:MAX_QUERY_IMAGES]:
            resolved_path = self._resolve_image_path(image_path)
            image_bytes = await asyncio.to_thread(resolved_path.read_bytes)
            encoded_images.append(base64.b64encode(image_bytes).decode("utf-8"))
        return encoded_images

    def _load_test_dataset(self) -> List[Dict[str, Any]]:
        """Load test cases from JSON file."""
        dataset_path = self._resolve_existing_path(self.test_dataset_path)
        if not dataset_path.exists():
            raise FileNotFoundError(f"Test dataset not found: {self.test_dataset_path}")

        self.test_dataset_path = dataset_path

        with open(dataset_path, encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            test_cases = data
        elif isinstance(data, dict):
            test_cases = data.get("test_cases", [])
        else:
            raise ValueError(
                "Unsupported dataset format. Expected a list or an object with test_cases."
            )

        normalized_cases: List[Dict[str, Any]] = [
            case for case in test_cases if isinstance(case, dict)
        ]
        if len(normalized_cases) != len(test_cases):
            logger.warning(
                "Skipped %s non-object test case entries while loading %s",
                len(test_cases) - len(normalized_cases),
                dataset_path,
            )

        return normalized_cases

    async def generate_rag_response(
        self,
        question: str,
        client: httpx.AsyncClient,
        image_paths: List[str] | None = None,
    ) -> Dict[str, Any]:
        """Generate a differential-diagnosis-style RAG response."""
        try:
            images = (
                await self._encode_image_paths(image_paths) if image_paths else []
            )
            payload = {
                "query": question,
                "mode": "hybrid",
                "include_references": True,
                "include_chunk_content": True,
                "response_type": "Multiple Paragraphs",
                "top_k": int(os.getenv("EVAL_QUERY_TOP_K", "10")),
                "user_prompt": (
                    "Given the clinical case, identify the differential diagnosis and provide relevant supporting information from the retrieved contexts. "
                ),
            }
            if images:
                payload["images"] = images

            api_key = os.getenv("LIGHTRAG_API_KEY")
            headers = {}
            if api_key:
                headers["X-API-Key"] = api_key

            response = await client.post(
                f"{self.rag_api_url}/query",
                json=payload,
                headers=headers if headers else None,
            )
            response.raise_for_status()
            result = response.json()

            answer = result.get("response", "No response generated")
            references = result.get("references", [])

            logger.debug("References Count: %s", len(references))

            contexts: List[str] = []
            for ref in references:
                content = ref.get("content", [])
                if isinstance(content, list):
                    contexts.extend(content)
                elif isinstance(content, str):
                    contexts.append(content)

            return {
                "answer": answer,
                "predicted_disease": "",
                "contexts": contexts,
            }

        except httpx.ConnectError as e:
            raise Exception(
                f"Cannot connect to LightRAG API at {self.rag_api_url}\n"
                f"   Make sure LightRAG server is running:\n"
                f"   python -m lightrag.api.lightrag_server\n"
                f"   Error: {str(e)}"
            ) from e
        except httpx.HTTPStatusError as e:
            raise Exception(
                f"LightRAG API error {e.response.status_code}: {e.response.text}"
            ) from e
        except httpx.ReadTimeout as e:
            raise Exception(
                f"Request timeout after waiting for response\n"
                f"   Question: {question[:100]}...\n"
                f"   Error: {str(e)}"
            ) from e
        except Exception as e:
            raise Exception(f"Error calling LightRAG API: {type(e).__name__}: {str(e)}") from e

    async def evaluate_single_case(
        self,
        idx: int,
        test_case: Dict[str, Any],
        rag_semaphore: asyncio.Semaphore,
        eval_semaphore: asyncio.Semaphore,
        client: httpx.AsyncClient,
        progress_counter: Dict[str, int],
        position_pool: asyncio.Queue,
        pbar_creation_lock: asyncio.Lock,
    ) -> Dict[str, Any]:
        """Evaluate a single case with context-recall-only scoring."""
        async with rag_semaphore:
            question = test_case["question"]
            ground_truth = test_case["ground_truth"]
            image_paths = test_case.get("image_path") or []
            if isinstance(image_paths, str):
                image_paths = [image_paths]
            elif not isinstance(image_paths, list):
                image_paths = []

            try:
                rag_response = await self.generate_rag_response(
                    question=question,
                    client=client,
                    image_paths=image_paths,
                )
            except Exception as e:
                logger.error("Error generating response for test %s: %s", idx, str(e))
                progress_counter["completed"] += 1
                return {
                    "test_number": idx,
                    "question": question,
                    "answer": "",
                    "ground_truth": ground_truth,
                    "predicted_disease": "",
                    "retrieved_contexts": [],
                    "missing_metrics": [],
                    "project": test_case.get("project", "unknown"),
                    "image_count": len(image_paths),
                    "image_paths": image_paths,
                    "error": str(e),
                    "metrics": {},
                    "timestamp": datetime.now().isoformat(),
                }

            retrieved_contexts = rag_response["contexts"]
            answer = rag_response["answer"]

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

                    df = eval_results.to_pandas()
                    scores_row = df.iloc[0]

                    context_recall = _sanitize_metric_value(
                        scores_row.get("context_recall", 0)
                    )
                    metrics = {"context_recall": context_recall}
                    missing_metrics = [
                        metric_name
                        for metric_name, metric_value in metrics.items()
                        if metric_value is None
                    ]

                    result = {
                        "test_number": idx,
                        "question": question,
                        "answer": answer,
                        "ground_truth": ground_truth,
                        "predicted_disease": "",
                        "retrieved_contexts": retrieved_contexts,
                        "missing_metrics": missing_metrics,
                        "project": test_case.get("project", "unknown"),
                        "image_count": len(image_paths),
                        "image_paths": image_paths,
                        "metrics": metrics,
                        "timestamp": datetime.now().isoformat(),
                    }

                    progress_counter["completed"] += 1
                    return result

                except Exception as e:
                    logger.error("Error evaluating test %s: %s", idx, str(e))
                    progress_counter["completed"] += 1
                    return {
                        "test_number": idx,
                        "question": question,
                        "answer": answer,
                        "ground_truth": ground_truth,
                        "predicted_disease": "",
                        "retrieved_contexts": retrieved_contexts,
                        "missing_metrics": [],
                        "project": test_case.get("project", "unknown"),
                        "image_count": len(image_paths),
                        "image_paths": image_paths,
                        "error": str(e),
                        "metrics": {},
                        "timestamp": datetime.now().isoformat(),
                    }
                finally:
                    if pbar is not None:
                        pbar.close()
                    if position is not None:
                        await position_pool.put(position)

    async def evaluate_responses(self) -> List[Dict[str, Any]]:
        """Evaluate all test cases in parallel."""
        max_async = int(os.getenv("EVAL_MAX_CONCURRENT", "2"))

        logger.info("%s", "=" * 70)
        logger.info("Starting differential-diagnosis context recall evaluation")
        logger.info("RAGAS Evaluation (Stage 2): %s concurrent", max_async)
        logger.info("%s", "=" * 70)

        rag_semaphore = asyncio.Semaphore(max_async * 2)
        eval_semaphore = asyncio.Semaphore(max_async)
        progress_counter = {"completed": 0}

        position_pool = asyncio.Queue()
        for i in range(max_async):
            await position_pool.put(i)

        pbar_creation_lock = asyncio.Lock()

        timeout = httpx.Timeout(
            TOTAL_TIMEOUT_SECONDS,
            connect=CONNECT_TIMEOUT_SECONDS,
            read=READ_TIMEOUT_SECONDS,
        )
        limits = httpx.Limits(
            max_connections=(max_async + 1) * 2,
            max_keepalive_connections=max_async + 1,
        )

        async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
            tasks = [
                self.evaluate_single_case(
                    idx,
                    test_case,
                    rag_semaphore,
                    eval_semaphore,
                    client,
                    progress_counter,
                    position_pool,
                    pbar_creation_lock,
                )
                for idx, test_case in enumerate(self.test_cases, 1)
            ]
            results = await asyncio.gather(*tasks)

        return list(results)

    def _csv_metric(self, value: Any) -> str:
        """Format a metric for CSV export, preserving missing values."""
        sanitized = _sanitize_metric_value(value)
        if sanitized is None:
            return "N/A"
        return f"{sanitized:.4f}"

    def _format_metric(self, value: Any, width: int = 6) -> str:
        """Format a metric value for display, handling missing values."""
        sanitized = _sanitize_metric_value(value)
        if sanitized is None:
            return "N/A".center(width)
        return f"{sanitized:.4f}".rjust(width)

    def _export_to_csv(self, results: List[Dict[str, Any]]) -> Path:
        """Export evaluation results to CSV, preserving the existing schema."""
        csv_path = (
            self.results_dir / f"results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        )

        with open(csv_path, "w", newline="", encoding="utf-8") as f:
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

            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            for idx, result in enumerate(results, 1):
                metrics = result.get("metrics", {})
                writer.writerow(
                    {
                        "test_number": idx,
                        "question": result.get("question", ""),
                        "answer": result.get("answer", ""),
                        "ground_truth": result.get("ground_truth", ""),
                        "retrieved_contexts": json.dumps(
                            result.get("retrieved_contexts", []), ensure_ascii=False
                        ),
                        "missing_metrics": json.dumps(
                            result.get("missing_metrics", []), ensure_ascii=False
                        ),
                        "project": result.get("project", "unknown"),
                        "image_count": result.get("image_count", 0),
                        "image_paths": json.dumps(
                            result.get("image_paths", []), ensure_ascii=False
                        ),
                        "context_recall": self._csv_metric(
                            metrics.get("context_recall")
                        ),
                        "status": (
                            "success"
                            if metrics and not result.get("missing_metrics")
                            else "partial"
                            if metrics
                            else "error"
                        ),
                        "timestamp": result.get("timestamp", ""),
                    }
                )

        return csv_path

    def _display_results_table(self, results: List[Dict[str, Any]]):
        """Display evaluation results in a compact table."""
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
            test_num = result.get("test_number", 0)
            question = result.get("question", "")
            question_display = (
                (question[:55] + "...") if len(question) > 58 else question
            )

            metrics = result.get("metrics", {})
            if metrics:
                ctx_rec = metrics.get("context_recall")
                status = "!" if result.get("missing_metrics") else "✓"

                logger.info(
                    "%-4d | %-58s | %s | %6s",
                    test_num,
                    question_display,
                    self._format_metric(ctx_rec, 6),
                    status,
                )
            else:
                error = result.get("error", "Unknown error")
                error_display = (error[:20] + "...") if len(error) > 23 else error
                logger.info(
                    "%-4d | %-58s | %6s | ✗ %s",
                    test_num,
                    question_display,
                    "N/A",
                    error_display,
                )

        logger.info("%s", "=" * 90)

    def _calculate_benchmark_stats(
        self, results: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Calculate benchmark statistics, preserving the existing summary shape."""
        valid_results = [r for r in results if r.get("metrics")]
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

        context_recall_sum = 0.0
        context_recall_count = 0

        for result in valid_results:
            metrics = result.get("metrics", {})

            context_recall = _sanitize_metric_value(metrics.get("context_recall"))
            if context_recall is not None:
                context_recall_sum += context_recall
                context_recall_count += 1

        avg_context_recall = (
            round(context_recall_sum / context_recall_count, 4)
            if context_recall_count
            else 0.0
        )

        return {
            "total_tests": total_tests,
            "successful_tests": successful_tests,
            "failed_tests": failed_tests,
            "success_rate": round(successful_tests / total_tests * 100, 2),
            "average_metrics": {
                "faithfulness": 0.0,
                "answer_relevance": 0.0,
                "context_recall": avg_context_recall,
                "context_precision": 0.0,
            },
        }

    async def run(self) -> Dict[str, Any]:
        """Run the complete evaluation pipeline."""
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

        json_path = (
            self.results_dir
            / f"results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, allow_nan=False)

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


async def main():
    """CLI entry point."""
    try:
        parser = argparse.ArgumentParser(
            description="RAGAS context recall evaluation for LightRAG differential diagnosis",
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
            help="Path to test dataset JSON file (default: sample_dataset.json in evaluation directory)",
        )
        parser.add_argument(
            "--ragendpoint",
            "-r",
            type=str,
            default=None,
            help="LightRAG API endpoint URL (default: http://localhost:9621 or $LIGHTRAG_API_URL)",
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
    except Exception as e:
        logger.exception("Error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
