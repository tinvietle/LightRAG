#!/usr/bin/env python3
"""
Bypass evaluation script for LightRAG.

This runner sends questions and attached images directly to the LightRAG server
with mode="bypass", so no retrieval context is generated and no RAGAS scoring is
performed.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dotenv import load_dotenv

from lightrag.utils import logger


# use the .env that is inside the current folder
# allows to use different .env file for each lightrag instance
# the OS environment variables take precedence over the .env file
load_dotenv(dotenv_path=".env", override=False)


MAX_QUERY_IMAGES = 10

DISEASE_TAG_PATTERN = re.compile(
    r"<disease_name>\s*(.*?)\s*</disease_name>", re.IGNORECASE | re.DOTALL
)
DISEASE_PREFIX_PATTERN = re.compile(
    r"^(?:predicted\s+)?(?:disease(?:_name)?|diagnosis)\s*[:\-]\s*(.+)$",
    re.IGNORECASE,
)


def _normalize_disease_name(value: str) -> str:
    """Normalize the predicted disease label extracted from model output."""
    cleaned = value.strip()
    cleaned = cleaned.strip(" \t\r\n:;-")
    cleaned = cleaned.strip("\"'“”‘’")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _extract_predicted_disease(answer: str) -> str:
    """Extract the disease label from a tagged or prefixed model answer."""
    if not answer:
        return ""

    tagged_match = DISEASE_TAG_PATTERN.search(answer)
    if tagged_match:
        return _normalize_disease_name(tagged_match.group(1))

    stripped_answer = answer.strip()
    if not stripped_answer:
        return ""

    first_line = stripped_answer.splitlines()[0].strip()
    first_line_tagged = re.match(
        r"^<\s*disease_name\s*>(.*?)(?:<\s*/\s*disease_name\s*>|$)",
        first_line,
        flags=re.IGNORECASE,
    )
    if first_line_tagged:
        return _normalize_disease_name(first_line_tagged.group(1))

    prefix_match = DISEASE_PREFIX_PATTERN.match(first_line)
    if prefix_match:
        return _normalize_disease_name(prefix_match.group(1))

    return ""


def _format_retrieved_context(contexts: List[str]) -> str:
    """Render retrieved chunks in a human-readable form."""
    if not contexts:
        return ""
    return "\n\n".join(
        f"[Context {index}] {context}" for index, context in enumerate(contexts, 1)
    )


class BypassEvaluator:
    """Evaluate LightRAG bypass responses for a JSON benchmark dataset."""

    def __init__(self, test_dataset_path: str | None = None, rag_api_url: str | None = None):
        """
        Initialize evaluator with test dataset.

        Args:
            test_dataset_path: Path to test dataset JSON file.
            rag_api_url: Base URL of LightRAG API (e.g., http://localhost:9621).
        """
        if test_dataset_path is None:
            test_dataset_path = Path(__file__).parent / "fold1_test_subset_10.json"

        if rag_api_url is None:
            rag_api_url = os.getenv("LIGHTRAG_API_URL", "http://localhost:9621")

        timeout_value = os.getenv("EVAL_QUERY_TIMEOUT") or os.getenv(
            "EVAL_LLM_TIMEOUT", "360"
        )

        self.test_dataset_path = Path(test_dataset_path)
        self.rag_api_url = rag_api_url.rstrip("/")
        self.results_dir = Path(__file__).parent / "results"
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.repo_root = Path(__file__).resolve().parents[2]

        self.request_timeout = int(timeout_value)
        self.max_async = int(os.getenv("EVAL_MAX_CONCURRENT", "2"))

        self.test_cases = self._load_test_dataset()

        self._display_configuration()

    def _display_configuration(self):
        """Display evaluation configuration settings."""
        logger.info("Bypass Evaluation Configuration:")
        logger.info("  - Query Mode:          bypass")
        logger.info("  - LightRAG API:        %s", self.rag_api_url)
        logger.info("  - Test Dataset:        %s", self.test_dataset_path.name)
        logger.info("  - Total Test Cases:    %s", len(self.test_cases))
        logger.info("  - Max Concurrent:      %s", self.max_async)
        logger.info("  - Request Timeout:     %s seconds", self.request_timeout)
        logger.info("  - Results Directory:   %s", self.results_dir.name)

    def _resolve_existing_path(self, raw_path: Path | str) -> Path:
        """Resolve paths relative to the current working directory or repo root."""
        candidate = Path(raw_path).expanduser()
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

    def _extract_image_paths(self, test_case: Dict[str, Any]) -> List[str]:
        """Normalize image path fields from supported dataset layouts."""
        for key in ("image_path", "image_paths", "images"):
            if key not in test_case:
                continue

            value = test_case.get(key)
            if value is None:
                return []
            if isinstance(value, str):
                cleaned = value.strip()
                return [cleaned] if cleaned else []
            if isinstance(value, list):
                return [
                    str(item).strip()
                    for item in value
                    if isinstance(item, str) and item.strip()
                ]
            return []

        return []

    async def _encode_image_paths(self, image_paths: List[str]) -> List[str]:
        """Read image files from disk and return base64 payloads."""
        encoded_images: List[str] = []
        for image_path in image_paths[:MAX_QUERY_IMAGES]:
            resolved_path = self._resolve_image_path(image_path)
            # Local benchmark images are small; read them directly to avoid
            # threadpool dependency issues in restricted runtime environments.
            image_bytes = resolved_path.read_bytes()
            encoded_images.append(base64.b64encode(image_bytes).decode("utf-8"))
        return encoded_images

    def _load_test_dataset(self) -> List[Dict[str, Any]]:
        """Load test cases from a JSON file."""
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

    async def generate_bypass_response(
        self,
        question: str,
        client: httpx.AsyncClient,
        image_paths: List[str] | None = None,
    ) -> Dict[str, Any]:
        """
        Generate a bypass response by calling LightRAG API.

        The request keeps the same medical-answer prompt shape used by the
        existing Ollama evaluator, but switches the query mode to bypass so the
        server skips retrieval and answers directly from the provided question
        and images.
        """
        try:
            images = (
                await self._encode_image_paths(image_paths) if image_paths else []
            )
            payload: Dict[str, Any] = {
                "query": question,
                "mode": "bypass",
                "include_references": True,
                "include_chunk_content": True,
                "response_type": "Multiple Paragraphs",
                "user_prompt": (
                    "Answer in this exact format when the question is about a disease:\n"
                    "<disease_name>DISEASE NAME</disease_name>\n"
                    "<explanation>FULL EXPLANATION</explanation>\n"
                    "Put the disease name on the first line, inside the <disease_name> tag, "
                    "with no text before it. The disease name must be concise and contain only "
                    "the diagnosis name. Then provide the full explanation below."
                ),
            }
            if images:
                payload["images"] = images

            api_key = os.getenv("LIGHTRAG_API_KEY")
            headers: Dict[str, str] = {}
            if api_key:
                headers["X-API-Key"] = api_key

            response = await client.post(
                f"{self.rag_api_url}/query",
                json=payload,
                headers=headers if headers else None,
            )
            response.raise_for_status()
            result = response.json()

            answer = result.get("response") or "No response generated"
            references = result.get("references") or []

            contexts: List[str] = []
            for ref in references:
                if not isinstance(ref, dict):
                    continue
                content = ref.get("content", [])
                if isinstance(content, list):
                    contexts.extend(str(item) for item in content if item)
                elif isinstance(content, str) and content:
                    contexts.append(content)

            return {
                "answer": answer,
                "predicted_disease": _extract_predicted_disease(answer),
                "contexts": contexts,
                "retrieved_context": _format_retrieved_context(contexts),
            }

        except httpx.ConnectError as e:
            raise Exception(
                f"Cannot connect to LightRAG API at {self.rag_api_url}\n"
                f"   Make sure LightRAG server is running:\n"
                f"   python -m lightrag.api.lightrag_server\n"
                f"   Error: {str(e)}"
            )
        except httpx.HTTPStatusError as e:
            raise Exception(
                f"LightRAG API error {e.response.status_code}: {e.response.text}"
            )
        except httpx.ReadTimeout as e:
            raise Exception(
                f"Request timeout after waiting for response\n"
                f"   Question: {question[:100]}...\n"
                f"   Error: {str(e)}"
            )
        except Exception as e:
            raise Exception(f"Error calling LightRAG API: {type(e).__name__}: {str(e)}")

    async def evaluate_single_case(
        self,
        idx: int,
        test_case: Dict[str, Any],
        semaphore: asyncio.Semaphore,
        client: httpx.AsyncClient,
    ) -> Dict[str, Any]:
        """Evaluate a single test case against the bypass endpoint."""
        async with semaphore:
            question = ""
            ground_truth = ""
            image_paths: List[str] = []
            project = "unknown"

            try:
                question = str(test_case.get("question", "")).strip()
                if not question:
                    raise ValueError(f"Test case {idx} is missing a question")

                ground_truth = test_case.get("ground_truth", "")
                image_paths = self._extract_image_paths(test_case)
                project = (
                    test_case.get("project") or test_case.get("file_name") or "unknown"
                )

                bypass_response = await self.generate_bypass_response(
                    question=question,
                    client=client,
                    image_paths=image_paths,
                )
            except Exception as e:
                logger.error("Error generating response for test %s: %s", idx, str(e))
                return {
                    "test_number": idx,
                    "question": question,
                    "answer": "",
                    "ground_truth": ground_truth,
                    "predicted_disease": "",
                    "retrieved_context": "",
                    "retrieved_contexts": [],
                    "project": project,
                    "image_count": len(image_paths),
                    "image_paths": image_paths,
                    "status": "error",
                    "error": str(e),
                    "timestamp": datetime.now().isoformat(),
                }

            retrieved_contexts = bypass_response["contexts"]
            retrieved_context = bypass_response["retrieved_context"]
            predicted_disease = bypass_response["predicted_disease"]
            answer = bypass_response["answer"]

            return {
                "test_number": idx,
                "question": question,
                "answer": answer,
                "ground_truth": ground_truth,
                "predicted_disease": predicted_disease,
                "retrieved_context": retrieved_context,
                "retrieved_contexts": retrieved_contexts,
                "project": project,
                "image_count": len(image_paths),
                "image_paths": image_paths,
                "status": "success",
                "timestamp": datetime.now().isoformat(),
            }

    async def evaluate_responses(self) -> List[Dict[str, Any]]:
        """Evaluate all test cases in parallel and return their results."""
        logger.info("%s", "=" * 70)
        logger.info("Starting bypass evaluation of LightRAG")
        logger.info("Query mode: bypass")
        logger.info("Concurrent requests: %s", self.max_async)
        logger.info("Request timeout: %s seconds", self.request_timeout)
        logger.info("%s", "=" * 70)

        semaphore = asyncio.Semaphore(self.max_async)

        timeout = httpx.Timeout(
            self.request_timeout,
            connect=self.request_timeout,
            read=self.request_timeout,
        )
        limits = httpx.Limits(
            max_connections=(self.max_async + 1) * 2,
            max_keepalive_connections=self.max_async + 1,
        )

        async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
            tasks = [
                self.evaluate_single_case(idx, test_case, semaphore, client)
                for idx, test_case in enumerate(self.test_cases, 1)
            ]
            results = await asyncio.gather(*tasks)

        return list(results)

    def _export_to_csv(self, results: List[Dict[str, Any]]) -> Path:
        """Export evaluation results to a CSV file."""
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
                "retrieved_context",
                "project",
                "image_count",
                "image_paths",
                "status",
                "error",
                "timestamp",
            ]

            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

            for idx, result in enumerate(results, 1):
                writer.writerow(
                    {
                        "test_number": idx,
                        "question": result.get("question", ""),
                        "answer": result.get("answer", ""),
                        "ground_truth": result.get("ground_truth", ""),
                        "predicted_disease": result.get("predicted_disease", ""),
                        "retrieved_context": result.get("retrieved_context", ""),
                        "project": result.get("project", "unknown"),
                        "image_count": result.get("image_count", 0),
                        "image_paths": json.dumps(
                            result.get("image_paths", []), ensure_ascii=False
                        ),
                        "status": result.get("status", "error"),
                        "error": result.get("error", ""),
                        "timestamp": result.get("timestamp", ""),
                    }
                )

        return csv_path

    def _display_results_table(self, results: List[Dict[str, Any]]):
        """Display evaluation results in a formatted table."""
        logger.info("")
        logger.info("%s", "=" * 115)
        logger.info("EVALUATION RESULTS SUMMARY")
        logger.info("%s", "=" * 115)

        logger.info(
            "%-4s | %-50s | %-24s | %-24s | %-6s",
            "#",
            "Question",
            "Predicted",
            "Ground Truth",
            "Status",
        )
        logger.info("%s", "-" * 115)

        for result in results:
            test_num = result.get("test_number", 0)
            question = result.get("question", "")
            question_display = (
                (question[:47] + "...") if len(question) > 50 else question
            )

            predicted = result.get("predicted_disease", "") or "N/A"
            predicted_display = (
                (predicted[:21] + "...") if len(predicted) > 24 else predicted
            )

            ground_truth = result.get("ground_truth", "") or "N/A"
            ground_truth_display = (
                (ground_truth[:21] + "...") if len(ground_truth) > 24 else ground_truth
            )

            if result.get("status") == "success":
                status = "OK"
                logger.info(
                    "%-4d | %-50s | %-24s | %-24s | %-6s",
                    test_num,
                    question_display,
                    predicted_display,
                    ground_truth_display,
                    status,
                )
            else:
                error = result.get("error", "Unknown error")
                error_display = (error[:20] + "...") if len(error) > 23 else error
                logger.info(
                    "%-4d | %-50s | %-24s | %-24s | ERR   ",
                    test_num,
                    question_display,
                    predicted_display,
                    ground_truth_display,
                )
                logger.info("      Error: %s", error_display)

        logger.info("%s", "=" * 115)

    def _calculate_benchmark_stats(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Calculate summary statistics from the evaluation results."""
        total_tests = len(results)
        successful_tests = len([r for r in results if r.get("status") == "success"])
        failed_tests = total_tests - successful_tests

        if total_tests == 0:
            return {
                "total_tests": 0,
                "successful_tests": 0,
                "failed_tests": 0,
                "success_rate": 0.0,
            }

        return {
            "total_tests": total_tests,
            "successful_tests": successful_tests,
            "failed_tests": failed_tests,
            "success_rate": round(successful_tests / total_tests * 100, 2),
        }

    async def run(self) -> Dict[str, Any]:
        """Run the complete bypass evaluation pipeline."""
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
            self.results_dir / f"results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, allow_nan=False)

        csv_path = self._export_to_csv(results)

        average_time = elapsed_time / len(results) if results else 0.0

        logger.info("")
        logger.info("%s", "=" * 70)
        logger.info("EVALUATION COMPLETE")
        logger.info("%s", "=" * 70)
        logger.info("Total Tests:    %s", len(results))
        logger.info("Successful:     %s", benchmark_stats["successful_tests"])
        logger.info("Failed:         %s", benchmark_stats["failed_tests"])
        logger.info("Success Rate:   %.2f%%", benchmark_stats["success_rate"])
        logger.info("Elapsed Time:   %.2f seconds", elapsed_time)
        logger.info("Avg Time/Test:  %.2f seconds", average_time)

        logger.info("")
        logger.info("%s", "=" * 70)
        logger.info("GENERATED FILES")
        logger.info("%s", "=" * 70)
        logger.info("Results Dir:    %s", self.results_dir.absolute())
        logger.info("   - CSV:  %s", csv_path.name)
        logger.info("   - JSON: %s", json_path.name)
        logger.info("%s", "=" * 70)

        return summary


async def main():
    """
    Main entry point for bypass evaluation.

    Command-line arguments:
        --dataset, -d: Path to test dataset JSON file
        --ragendpoint, -r: LightRAG API endpoint URL
    """
    try:
        parser = argparse.ArgumentParser(
            description="Bypass Evaluation Script for LightRAG System",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
Examples:
  # Use defaults
  python lightrag/evaluation/eval_bypass_ollama.py

  # Specify custom dataset
  python lightrag/evaluation/eval_bypass_ollama.py --dataset fold1_test_subset_10.json

  # Specify custom LightRAG endpoint
  python lightrag/evaluation/eval_bypass_ollama.py --ragendpoint http://my-server.com:9621

  # Specify both
  python lightrag/evaluation/eval_bypass_ollama.py -d fold1_test_subset_10.json -r http://localhost:9621
            """,
        )

        parser.add_argument(
            "--dataset",
            "-d",
            type=str,
            default=None,
            help="Path to test dataset JSON file (default: fold1_test_subset_10.json in evaluation directory)",
        )

        parser.add_argument(
            "--ragendpoint",
            "-r",
            type=str,
            default=None,
            help="LightRAG API endpoint URL (default: http://localhost:9621 or $LIGHTRAG_API_URL environment variable)",
        )

        args = parser.parse_args()

        logger.info("%s", "=" * 70)
        logger.info("Starting LightRAG bypass evaluation")
        logger.info("%s", "=" * 70)

        evaluator = BypassEvaluator(
            test_dataset_path=args.dataset,
            rag_api_url=args.ragendpoint,
        )
        await evaluator.run()
    except Exception as e:
        logger.exception("Error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
