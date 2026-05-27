#!/usr/bin/env python3
"""
RAGAS Evaluation Script for LightRAG System

Evaluates RAG response quality using RAGAS metrics:
- Faithfulness: Is the answer factually accurate based on context?
- Answer Relevance: Is the answer relevant to the question?
- Context Recall: Is all relevant information retrieved?
- Context Precision: Is retrieved context clean without noise?

Usage:
    # Use defaults (sample_dataset.json, http://localhost:9621)
    python lightrag/evaluation/eval_rag_quality.py

    # Specify custom dataset
    python lightrag/evaluation/eval_rag_quality.py --dataset my_test.json
    python lightrag/evaluation/eval_rag_quality.py -d my_test.json

    # Specify custom RAG endpoint
    python lightrag/evaluation/eval_rag_quality.py --ragendpoint http://my-server.com:9621
    python lightrag/evaluation/eval_rag_quality.py -r http://my-server.com:9621

    # Specify both
    python lightrag/evaluation/eval_rag_quality.py -d my_test.json -r http://localhost:9621

    # Get help
    python lightrag/evaluation/eval_rag_quality.py --help

Results are saved to: lightrag/evaluation/results/
    - results_YYYYMMDD_HHMMSS.csv   (CSV export for analysis)
    - results_YYYYMMDD_HHMMSS.json  (Full results with details)

Technical Notes:
    - Uses native Ollama clients for evaluation LLM and embeddings
    - Reuses EVAL_* environment variables for host/model compatibility
    - Normalizes OpenAI-style /v1 hosts to Ollama native hosts automatically
    - Deprecation warnings are suppressed for cleaner output
"""

import argparse
import asyncio
import base64
import csv
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List

import httpx
from ollama import AsyncClient as OllamaAsyncClient
from ollama import Client as OllamaClient
from dotenv import load_dotenv
from lightrag.utils import logger
from langchain_core.outputs import Generation, LLMResult
from ragas.embeddings.base import BaseRagasEmbeddings
from ragas.llms.base import BaseRagasLLM
from ragas.run_config import RunConfig

if TYPE_CHECKING:
    from langchain_core.callbacks import Callbacks
    from langchain_core.prompt_values import PromptValue

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# use the .env that is inside the current folder
# allows to use different .env file for each lightrag instance
# the OS environment variables take precedence over the .env file
load_dotenv(dotenv_path=".env", override=False)

# Conditional imports - will raise ImportError if dependencies not installed
try:
    from datasets import Dataset
    from ragas import evaluate
    from ragas.metrics import (
        AnswerRelevancy,
        ContextPrecision,
        ContextRecall,
        Faithfulness,
    )
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
DISEASE_TAG_PATTERN = re.compile(
    r"<disease_name>\s*(.*?)\s*</disease_name>", re.IGNORECASE | re.DOTALL
)
DISEASE_PREFIX_PATTERN = re.compile(
    r"^(?:predicted\s+)?(?:disease(?:_name)?|diagnosis)\s*[:\-]\s*(.+)$",
    re.IGNORECASE,
)


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
    normalized_host = (host or os.getenv("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")
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


def _extract_ollama_embeddings(response: Any) -> List[List[float]]:
    """Read embedding vectors from an Ollama embed response."""
    embeddings = getattr(response, "embeddings", None)
    if embeddings is None and isinstance(response, dict):
        embeddings = response.get("embeddings")
    if embeddings is None:
        raise ValueError("Ollama embed response did not include embeddings")
    return [list(vector) for vector in embeddings]


def _sanitize_metric_value(value: Any) -> float | None:
    """Convert a metric to a finite float or None."""
    if _is_nan(value):
        return None
    return float(value)


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


class OllamaNativeEmbeddings(BaseRagasEmbeddings):
    """Minimal native Ollama embedding wrapper for RAGAS."""

    def __init__(self, model: str, host: str, timeout: int):
        super().__init__()
        self.model = model
        self.host = host
        self.timeout = timeout
        self.sync_client = OllamaClient(host=self.host, timeout=self.timeout)
        self.async_client = OllamaAsyncClient(host=self.host, timeout=self.timeout)

    def embed_query(self, text: str) -> List[float]:
        response = self.sync_client.embed(model=self.model, input=[text])
        return _extract_ollama_embeddings(response)[0]

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        response = self.sync_client.embed(model=self.model, input=texts)
        return _extract_ollama_embeddings(response)

    async def aembed_query(self, text: str) -> List[float]:
        response = await self.async_client.embed(model=self.model, input=[text])
        return _extract_ollama_embeddings(response)[0]

    async def aembed_documents(self, texts: List[str]) -> List[List[float]]:
        response = await self.async_client.embed(model=self.model, input=texts)
        return _extract_ollama_embeddings(response)


class RAGEvaluator:
    """Evaluate RAG system quality using RAGAS metrics"""

    def __init__(self, test_dataset_path: str = None, rag_api_url: str = None):
        """
        Initialize evaluator with test dataset

        Args:
            test_dataset_path: Path to test dataset JSON file
            rag_api_url: Base URL of LightRAG API (e.g., http://localhost:9621)
                        If None, will try to read from environment or use default

        Environment Variables:
            EVAL_LLM_MODEL: Ollama chat model for evaluation
            EVAL_EMBEDDING_MODEL: Ollama embedding model for evaluation
            EVAL_LLM_BINDING_HOST: Ollama host (OpenAI-style /v1 hosts are normalized)
            EVAL_EMBEDDING_BINDING_HOST: Ollama embedding host (falls back to EVAL_LLM_BINDING_HOST)
            EVAL_OLLAMA_THINK: Native Ollama think mode (false, true, low, medium, high)

        Raises:
            ImportError: If ragas or datasets packages are not installed
        """
        # Validate RAGAS dependencies are installed
        if not RAGAS_AVAILABLE:
            raise ImportError(
                "RAGAS dependencies not installed. "
                "Install with: pip install ragas datasets"
            )

        eval_model = os.getenv("EVAL_LLM_MODEL", "gpt-oss:120b-cloud")
        eval_embedding_model = os.getenv(
            "EVAL_EMBEDDING_MODEL", "qwen3-embedding:0.6b"
        )
        eval_llm_base_url = _normalize_ollama_host(os.getenv("EVAL_LLM_BINDING_HOST"))
        eval_embedding_base_url = _normalize_ollama_host(
            os.getenv("EVAL_EMBEDDING_BINDING_HOST") or eval_llm_base_url
        )
        eval_timeout = int(os.getenv("EVAL_LLM_TIMEOUT", "180"))
        eval_think = _parse_ollama_think(os.getenv("EVAL_OLLAMA_THINK"))

        self.eval_llm = OllamaRagasLLM(
            model=eval_model,
            host=eval_llm_base_url,
            timeout=eval_timeout,
            think=eval_think,
            run_config=RunConfig(timeout=eval_timeout),
        )
        self.eval_embeddings = OllamaNativeEmbeddings(
            model=eval_embedding_model,
            host=eval_embedding_base_url,
            timeout=eval_timeout,
        )
        self.eval_embeddings.set_run_config(RunConfig(timeout=eval_timeout))

        if test_dataset_path is None:
            test_dataset_path = Path(__file__).parent / "sample_dataset.json"

        if rag_api_url is None:
            rag_api_url = os.getenv("LIGHTRAG_API_URL", "http://localhost:9621")

        self.test_dataset_path = Path(test_dataset_path)
        self.rag_api_url = rag_api_url.rstrip("/")
        self.results_dir = Path(__file__).parent / "results"
        self.results_dir.mkdir(exist_ok=True)
        self.repo_root = Path(__file__).resolve().parents[2]

        # Load test dataset
        self.test_cases = self._load_test_dataset()

        # Store configuration values for display
        self.eval_model = eval_model
        self.eval_embedding_model = eval_embedding_model
        self.eval_llm_base_url = eval_llm_base_url
        self.eval_embedding_base_url = eval_embedding_base_url
        self.eval_max_retries = int(os.getenv("EVAL_LLM_MAX_RETRIES", "5"))
        self.eval_timeout = eval_timeout
        self.eval_think = eval_think

        # Display configuration
        self._display_configuration()

    def _display_configuration(self):
        """Display all evaluation configuration settings"""
        logger.info("Evaluation Models:")
        logger.info("  • LLM Model:            %s", self.eval_model)
        logger.info("  • Embedding Model:      %s", self.eval_embedding_model)

        # Display LLM endpoint
        logger.info("  • LLM Endpoint:         %s", self.eval_llm_base_url)
        logger.info("  • Ollama Think Mode:    %s", self.eval_think)

        # Display Embedding endpoint (only if different from LLM)
        if self.eval_embedding_base_url != self.eval_llm_base_url:
            logger.info(
                "  • Embedding Endpoint:   %s", self.eval_embedding_base_url
            )

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
        """
        Generate RAG response by calling LightRAG API.

        Args:
            question: The user query.
            client: Shared httpx AsyncClient for connection pooling.

        Returns:
            Dictionary with 'answer' and 'contexts' keys.
            'contexts' is a list of strings (one per retrieved document).

        Raises:
            Exception: If LightRAG API is unavailable.
        """
        try:
            images = (
                await self._encode_image_paths(image_paths) if image_paths else []
            )
            payload = {
                "query": question,
                "mode": "hybrid",
                "include_references": True,
                "include_chunk_content": True,  # NEW: Request chunk content in references
                "response_type": "Multiple Paragraphs",
                "top_k": int(os.getenv("EVAL_QUERY_TOP_K", "10")),
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

            # Get API key from environment for authentication
            api_key = os.getenv("LIGHTRAG_API_KEY")

            # Prepare headers with optional authentication
            headers = {}
            if api_key:
                headers["X-API-Key"] = api_key

            # Single optimized API call - gets both answer AND chunk content
            response = await client.post(
                f"{self.rag_api_url}/query",
                json=payload,
                headers=headers if headers else None,
            )
            response.raise_for_status()
            result = response.json()

            answer = result.get("response", "No response generated")
            references = result.get("references", [])

            # DEBUG: Inspect the API response
            logger.debug("🔍 References Count: %s", len(references))
            if references:
                first_ref = references[0]
                logger.debug("🔍 First Reference Keys: %s", list(first_ref.keys()))
                if "content" in first_ref:
                    content_preview = first_ref["content"]
                    if isinstance(content_preview, list) and content_preview:
                        logger.debug(
                            "🔍 Content Preview (first chunk): %s...",
                            content_preview[0][:100],
                        )
                    elif isinstance(content_preview, str):
                        logger.debug("🔍 Content Preview: %s...", content_preview[:100])

            # Extract chunk content from enriched references
            # Note: content is now a list of chunks per reference (one file may have multiple chunks)
            contexts = []
            for ref in references:
                content = ref.get("content", [])
                if isinstance(content, list):
                    # Flatten the list: each chunk becomes a separate context
                    contexts.extend(content)
                elif isinstance(content, str):
                    # Backward compatibility: if content is still a string (shouldn't happen)
                    contexts.append(content)

            return {
                "answer": answer,
                "predicted_disease": _extract_predicted_disease(answer),
                "contexts": contexts,  # List of strings from actual retrieved chunks
                "retrieved_context": _format_retrieved_context(contexts),
            }

        except httpx.ConnectError as e:
            raise Exception(
                f"❌ Cannot connect to LightRAG API at {self.rag_api_url}\n"
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
        rag_semaphore: asyncio.Semaphore,
        eval_semaphore: asyncio.Semaphore,
        client: httpx.AsyncClient,
        progress_counter: Dict[str, int],
        position_pool: asyncio.Queue,
        pbar_creation_lock: asyncio.Lock,
    ) -> Dict[str, Any]:
        """
        Evaluate a single test case with two-stage pipeline concurrency control

        Args:
            idx: Test case index (1-based)
            test_case: Test case dictionary with question and ground_truth
            rag_semaphore: Semaphore to control overall concurrency (covers entire function)
            eval_semaphore: Semaphore to control RAGAS evaluation concurrency (Stage 2)
            client: Shared httpx AsyncClient for connection pooling
            progress_counter: Shared dictionary for progress tracking
            position_pool: Queue of available tqdm position indices
            pbar_creation_lock: Lock to serialize tqdm creation and prevent race conditions

        Returns:
            Evaluation result dictionary
        """
        # rag_semaphore controls the entire evaluation process to prevent
        # all RAG responses from being generated at once when eval is slow
        async with rag_semaphore:
            question = test_case["question"]
            ground_truth = test_case["ground_truth"]
            image_paths = test_case.get("image_path") or []
            if isinstance(image_paths, str):
                image_paths = [image_paths]
            elif not isinstance(image_paths, list):
                image_paths = []

            # Stage 1: Generate RAG response
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
                    "retrieved_context": "",
                    "retrieved_contexts": [],
                    "missing_metrics": [],
                    "project": test_case.get("project", "unknown"),
                    "image_count": len(image_paths),
                    "image_paths": image_paths,
                    "error": str(e),
                    "metrics": {},
                    "ragas_score": 0,
                    "timestamp": datetime.now().isoformat(),
                }

            # *** CRITICAL FIX: Use actual retrieved contexts, NOT ground_truth ***
            retrieved_contexts = rag_response["contexts"]
            retrieved_context = rag_response["retrieved_context"]
            predicted_disease = rag_response["predicted_disease"]
            answer = rag_response["answer"]

            # Prepare dataset for RAGAS evaluation with CORRECT contexts
            eval_dataset = Dataset.from_dict(
                {
                    "question": [question],
                    "answer": [answer],
                    "contexts": [retrieved_contexts],
                    "ground_truth": [ground_truth],
                }
            )

            # Stage 2: Run RAGAS evaluation (controlled by eval_semaphore)
            # IMPORTANT: Create fresh metric instances for each evaluation to avoid
            # concurrent state conflicts when multiple tasks run in parallel
            async with eval_semaphore:
                pbar = None
                position = None
                try:
                    # Acquire a position from the pool for this tqdm progress bar
                    position = await position_pool.get()

                    # Serialize tqdm creation to prevent race conditions
                    # Multiple tasks creating tqdm simultaneously can cause display conflicts
                    async with pbar_creation_lock:
                        # Create tqdm progress bar with assigned position to avoid overlapping
                        # leave=False ensures the progress bar is cleared after completion,
                        # preventing accumulation of completed bars and allowing position reuse
                        pbar = tqdm(
                            total=4,
                            desc=f"Eval-{idx:02d}",
                            position=position,
                            leave=False,
                        )
                        # Give tqdm time to initialize and claim its screen position
                        await asyncio.sleep(0.05)

                    eval_results = evaluate(
                        dataset=eval_dataset,
                        metrics=[
                            Faithfulness(),
                            AnswerRelevancy(),
                            ContextRecall(),
                            ContextPrecision(),
                        ],
                        llm=self.eval_llm,
                        embeddings=self.eval_embeddings,
                        _pbar=pbar,
                    )

                    # Convert to DataFrame (RAGAS v0.3+ API)
                    df = eval_results.to_pandas()

                    # Extract scores from first row
                    scores_row = df.iloc[0]

                    # Extract scores (RAGAS v0.3+ uses .to_pandas())
                    metrics = {
                        "faithfulness": _sanitize_metric_value(
                            scores_row.get("faithfulness", 0)
                        ),
                        "answer_relevance": _sanitize_metric_value(
                            scores_row.get("answer_relevancy", 0)
                        ),
                        "context_recall": _sanitize_metric_value(
                            scores_row.get("context_recall", 0)
                        ),
                        "context_precision": _sanitize_metric_value(
                            scores_row.get("context_precision", 0)
                        ),
                    }
                    missing_metrics = [
                        metric_name
                        for metric_name, metric_value in metrics.items()
                        if metric_value is None
                    ]
                    valid_metrics = [
                        value for value in metrics.values() if value is not None
                    ]
                    ragas_score = (
                        sum(valid_metrics) / len(valid_metrics)
                        if valid_metrics
                        else None
                    )

                    result = {
                        "test_number": idx,
                        "question": question,
                        "answer": answer,
                        "ground_truth": ground_truth,
                        "predicted_disease": predicted_disease,
                        # "retrieved_context": retrieved_context,
                        "retrieved_contexts": retrieved_contexts,
                        "missing_metrics": missing_metrics,
                        "project": test_case.get("project", "unknown"),
                        "image_count": len(image_paths),
                        "image_paths": image_paths,
                        "metrics": metrics,
                        "timestamp": datetime.now().isoformat(),
                    }

                    # Calculate RAGAS score from only the valid metrics.
                    result["ragas_score"] = (
                        round(ragas_score, 4) if ragas_score is not None else None
                    )

                    # Update progress counter
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
                        "predicted_disease": predicted_disease,
                        "retrieved_context": retrieved_context,
                        "retrieved_contexts": retrieved_contexts,
                        "missing_metrics": [],
                        "project": test_case.get("project", "unknown"),
                        "image_count": len(image_paths),
                        "image_paths": image_paths,
                        "error": str(e),
                        "metrics": {},
                        "ragas_score": 0,
                        "timestamp": datetime.now().isoformat(),
                    }
                finally:
                    # Force close progress bar to ensure completion
                    if pbar is not None:
                        pbar.close()
                    # Release the position back to the pool for reuse
                    if position is not None:
                        await position_pool.put(position)

    async def evaluate_responses(self) -> List[Dict[str, Any]]:
        """
        Evaluate all test cases in parallel with two-stage pipeline and return metrics

        Returns:
            List of evaluation results with metrics
        """
        # Get evaluation concurrency from environment (default to 2 for parallel evaluation)
        max_async = int(os.getenv("EVAL_MAX_CONCURRENT", "2"))

        logger.info("%s", "=" * 70)
        logger.info("🚀 Starting RAGAS Evaluation of LightRAG System")
        logger.info("🔧 RAGAS Evaluation (Stage 2): %s concurrent", max_async)
        logger.info("%s", "=" * 70)

        # Create two-stage pipeline semaphores
        # Stage 1: RAG generation - allow x2 concurrency to keep evaluation fed
        rag_semaphore = asyncio.Semaphore(max_async * 2)
        # Stage 2: RAGAS evaluation - primary bottleneck
        eval_semaphore = asyncio.Semaphore(max_async)

        # Create progress counter (shared across all tasks)
        progress_counter = {"completed": 0}

        # Create position pool for tqdm progress bars
        # Positions range from 0 to max_async-1, ensuring no overlapping displays
        position_pool = asyncio.Queue()
        for i in range(max_async):
            await position_pool.put(i)

        # Create lock to serialize tqdm creation and prevent race conditions
        # This ensures progress bars are created one at a time, avoiding display conflicts
        pbar_creation_lock = asyncio.Lock()

        # Create shared HTTP client with connection pooling and proper timeouts
        # Timeout: 3 minutes for connect, 5 minutes for read (LLM can be slow)
        timeout = httpx.Timeout(
            TOTAL_TIMEOUT_SECONDS,
            connect=CONNECT_TIMEOUT_SECONDS,
            read=READ_TIMEOUT_SECONDS,
        )
        limits = httpx.Limits(
            max_connections=(max_async + 1) * 2,  # Allow buffer for RAG stage
            max_keepalive_connections=max_async + 1,
        )

        async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
            # Create tasks for all test cases
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

            # Run all evaluations in parallel (limited by two-stage semaphores)
            results = await asyncio.gather(*tasks)

        return list(results)

    def _export_to_csv(self, results: List[Dict[str, Any]]) -> Path:
        """
        Export evaluation results to CSV file

        Args:
            results: List of evaluation results

        Returns:
            Path to the CSV file

        CSV Format:
            - question: The test question
            - project: Project context
            - faithfulness: Faithfulness score (0-1)
            - answer_relevance: Answer relevance score (0-1)
            - context_recall: Context recall score (0-1)
            - context_precision: Context precision score (0-1)
            - ragas_score: Overall RAGAS score (0-1)
            - timestamp: When evaluation was run
        """
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
                "missing_metrics",
                "project",
                "image_count",
                "image_paths",
                "faithfulness",
                "answer_relevance",
                "context_recall",
                "context_precision",
                "ragas_score",
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
                        "predicted_disease": result.get("predicted_disease", ""),
                        "retrieved_context": result.get("retrieved_context", ""),
                        "missing_metrics": json.dumps(
                            result.get("missing_metrics", []), ensure_ascii=False
                        ),
                        "project": result.get("project", "unknown"),
                        "image_count": result.get("image_count", 0),
                        "image_paths": json.dumps(
                            result.get("image_paths", []), ensure_ascii=False
                        ),
                        "faithfulness": self._csv_metric(metrics.get("faithfulness")),
                        "answer_relevance": self._csv_metric(
                            metrics.get("answer_relevance")
                        ),
                        "context_recall": self._csv_metric(
                            metrics.get("context_recall")
                        ),
                        "context_precision": self._csv_metric(
                            metrics.get("context_precision")
                        ),
                        "ragas_score": self._csv_metric(result.get("ragas_score")),
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

    def _csv_metric(self, value: Any) -> str:
        """Format a metric for CSV export, preserving missing values."""
        sanitized = _sanitize_metric_value(value)
        if sanitized is None:
            return "N/A"
        return f"{sanitized:.4f}"

    def _format_metric(self, value: Any, width: int = 6) -> str:
        """
        Format a metric value for display, handling NaN gracefully

        Args:
            value: The metric value to format
            width: The width of the formatted string

        Returns:
            Formatted string (e.g., "0.8523" or "  N/A ")
        """
        sanitized = _sanitize_metric_value(value)
        if sanitized is None:
            return "N/A".center(width)
        return f"{sanitized:.4f}".rjust(width)

    def _display_results_table(self, results: List[Dict[str, Any]]):
        """
        Display evaluation results in a formatted table

        Args:
            results: List of evaluation results
        """
        logger.info("")
        logger.info("%s", "=" * 115)
        logger.info("📊 EVALUATION RESULTS SUMMARY")
        logger.info("%s", "=" * 115)

        # Table header
        logger.info(
            "%-4s | %-50s | %6s | %7s | %6s | %7s | %6s | %6s",
            "#",
            "Question",
            "Faith",
            "AnswRel",
            "CtxRec",
            "CtxPrec",
            "RAGAS",
            "Status",
        )
        logger.info("%s", "-" * 115)

        # Table rows
        for result in results:
            test_num = result.get("test_number", 0)
            question = result.get("question", "")
            # Truncate question to 50 chars
            question_display = (
                (question[:47] + "...") if len(question) > 50 else question
            )

            metrics = result.get("metrics", {})
            if metrics:
                # Success/partial case - format each metric, handling missing values.
                faith = metrics.get("faithfulness")
                ans_rel = metrics.get("answer_relevance")
                ctx_rec = metrics.get("context_recall")
                ctx_prec = metrics.get("context_precision")
                ragas = result.get("ragas_score")
                status = "!" if result.get("missing_metrics") else "✓"

                logger.info(
                    "%-4d | %-50s | %s | %s | %s | %s | %s | %6s",
                    test_num,
                    question_display,
                    self._format_metric(faith, 6),
                    self._format_metric(ans_rel, 7),
                    self._format_metric(ctx_rec, 6),
                    self._format_metric(ctx_prec, 7),
                    self._format_metric(ragas, 6),
                    status,
                )
            else:
                # Error case
                error = result.get("error", "Unknown error")
                error_display = (error[:20] + "...") if len(error) > 23 else error
                logger.info(
                    "%-4d | %-50s | %6s | %7s | %6s | %7s | %6s | ✗ %s",
                    test_num,
                    question_display,
                    "N/A",
                    "N/A",
                    "N/A",
                    "N/A",
                    "N/A",
                    error_display,
                )

        logger.info("%s", "=" * 115)

    def _calculate_benchmark_stats(
        self, results: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Calculate benchmark statistics from evaluation results

        Args:
            results: List of evaluation results

        Returns:
            Dictionary with benchmark statistics
        """
        # Filter out results with errors
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
            }

        # Calculate averages for each metric (handling NaN values correctly)
        # Track both sum and count for each metric to handle NaN values properly
        metrics_data = {
            "faithfulness": {"sum": 0.0, "count": 0},
            "answer_relevance": {"sum": 0.0, "count": 0},
            "context_recall": {"sum": 0.0, "count": 0},
            "context_precision": {"sum": 0.0, "count": 0},
            "ragas_score": {"sum": 0.0, "count": 0},
        }

        for result in valid_results:
            metrics = result.get("metrics", {})

            # For each metric, sum only finite values and count them.
            faithfulness = _sanitize_metric_value(metrics.get("faithfulness"))
            if faithfulness is not None:
                metrics_data["faithfulness"]["sum"] += faithfulness
                metrics_data["faithfulness"]["count"] += 1

            answer_relevance = _sanitize_metric_value(metrics.get("answer_relevance"))
            if answer_relevance is not None:
                metrics_data["answer_relevance"]["sum"] += answer_relevance
                metrics_data["answer_relevance"]["count"] += 1

            context_recall = _sanitize_metric_value(metrics.get("context_recall"))
            if context_recall is not None:
                metrics_data["context_recall"]["sum"] += context_recall
                metrics_data["context_recall"]["count"] += 1

            context_precision = _sanitize_metric_value(metrics.get("context_precision"))
            if context_precision is not None:
                metrics_data["context_precision"]["sum"] += context_precision
                metrics_data["context_precision"]["count"] += 1

            ragas_score = _sanitize_metric_value(result.get("ragas_score"))
            if ragas_score is not None:
                metrics_data["ragas_score"]["sum"] += ragas_score
                metrics_data["ragas_score"]["count"] += 1

        # Calculate averages using actual counts for each metric
        avg_metrics = {}
        for metric_name, data in metrics_data.items():
            if data["count"] > 0:
                avg_val = data["sum"] / data["count"]
                avg_metrics[metric_name] = (
                    round(avg_val, 4) if not _is_nan(avg_val) else 0.0
                )
            else:
                avg_metrics[metric_name] = 0.0

        # Find min and max RAGAS scores (filter out NaN)
        ragas_scores = []
        for r in valid_results:
            score = _sanitize_metric_value(r.get("ragas_score"))
            if score is None:
                continue  # Skip missing metric values
            ragas_scores.append(score)

        min_score = min(ragas_scores) if ragas_scores else 0
        max_score = max(ragas_scores) if ragas_scores else 0

        return {
            "total_tests": total_tests,
            "successful_tests": successful_tests,
            "failed_tests": failed_tests,
            "success_rate": round(successful_tests / total_tests * 100, 2),
            "average_metrics": avg_metrics,
            "min_ragas_score": round(min_score, 4),
            "max_ragas_score": round(max_score, 4),
        }

    async def run(self) -> Dict[str, Any]:
        """Run complete evaluation pipeline"""

        start_time = time.time()

        # Evaluate responses
        results = await self.evaluate_responses()

        elapsed_time = time.time() - start_time

        # Calculate benchmark statistics
        benchmark_stats = self._calculate_benchmark_stats(results)

        # Save results
        summary = {
            "timestamp": datetime.now().isoformat(),
            "total_tests": len(results),
            "elapsed_time_seconds": round(elapsed_time, 2),
            "benchmark_stats": benchmark_stats,
            "results": results,
        }

        # Display results table
        self._display_results_table(results)

        # Save JSON results
        json_path = (
            self.results_dir
            / f"results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        with open(json_path, "w") as f:
            json.dump(summary, f, indent=2, allow_nan=False)

        # Export to CSV
        csv_path = self._export_to_csv(results)

        # Print summary
        logger.info("")
        logger.info("%s", "=" * 70)
        logger.info("📊 EVALUATION COMPLETE")
        logger.info("%s", "=" * 70)
        logger.info("Total Tests:    %s", len(results))
        logger.info("Successful:     %s", benchmark_stats["successful_tests"])
        logger.info("Failed:         %s", benchmark_stats["failed_tests"])
        logger.info("Success Rate:   %.2f%%", benchmark_stats["success_rate"])
        logger.info("Elapsed Time:   %.2f seconds", elapsed_time)
        logger.info("Avg Time/Test:  %.2f seconds", elapsed_time / len(results))

        # Print benchmark metrics
        logger.info("")
        logger.info("%s", "=" * 70)
        logger.info("📈 BENCHMARK RESULTS (Average)")
        logger.info("%s", "=" * 70)
        avg = benchmark_stats["average_metrics"]
        logger.info("Average Faithfulness:      %.4f", avg["faithfulness"])
        logger.info("Average Answer Relevance:  %.4f", avg["answer_relevance"])
        logger.info("Average Context Recall:    %.4f", avg["context_recall"])
        logger.info("Average Context Precision: %.4f", avg["context_precision"])
        logger.info("Average RAGAS Score:       %.4f", avg["ragas_score"])
        logger.info("%s", "-" * 70)
        logger.info(
            "Min RAGAS Score:           %.4f",
            benchmark_stats["min_ragas_score"],
        )
        logger.info(
            "Max RAGAS Score:           %.4f",
            benchmark_stats["max_ragas_score"],
        )

        logger.info("")
        logger.info("%s", "=" * 70)
        logger.info("📁 GENERATED FILES")
        logger.info("%s", "=" * 70)
        logger.info("Results Dir:    %s", self.results_dir.absolute())
        logger.info("   • CSV:  %s", csv_path.name)
        logger.info("   • JSON: %s", json_path.name)
        logger.info("%s", "=" * 70)

        return summary


async def main():
    """
    Main entry point for RAGAS evaluation

    Command-line arguments:
        --dataset, -d: Path to test dataset JSON file (default: sample_dataset.json)
        --ragendpoint, -r: LightRAG API endpoint URL (default: http://localhost:9621 or $LIGHTRAG_API_URL)

    Usage:
        python lightrag/evaluation/eval_rag_quality.py
        python lightrag/evaluation/eval_rag_quality.py --dataset my_test.json
        python lightrag/evaluation/eval_rag_quality.py -d my_test.json -r http://localhost:9621
    """
    try:
        # Parse command-line arguments
        parser = argparse.ArgumentParser(
            description="RAGAS Evaluation Script for LightRAG System",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
Examples:
  # Use defaults
  python lightrag/evaluation/eval_rag_quality.py

  # Specify custom dataset
  python lightrag/evaluation/eval_rag_quality.py --dataset my_test.json

  # Specify custom RAG endpoint
  python lightrag/evaluation/eval_rag_quality.py --ragendpoint http://my-server.com:9621

  # Specify both
  python lightrag/evaluation/eval_rag_quality.py -d my_test.json -r http://localhost:9621
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
            help="LightRAG API endpoint URL (default: http://localhost:9621 or $LIGHTRAG_API_URL environment variable)",
        )

        args = parser.parse_args()

        logger.info("%s", "=" * 70)
        logger.info("🔍 RAGAS Evaluation - Using Real LightRAG API")
        logger.info("%s", "=" * 70)

        evaluator = RAGEvaluator(
            test_dataset_path=args.dataset, rag_api_url=args.ragendpoint
        )
        await evaluator.run()
    except Exception as e:
        logger.exception("❌ Error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
