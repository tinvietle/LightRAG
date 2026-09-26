from __future__ import annotations

import asyncio
import multiprocessing
import os
import re
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from lightrag.utils import logger


DEFAULT_NER_MODEL_CACHE_DIR = Path("./ner_model")
NER_MODEL_DIR_ENV = "GLINER_MODEL_DIR"
NER_MODEL_NAME = "Ihor/gliner-biomed-base-v1.0"

# GLiNER inference runs CPU-bound PyTorch code inside a recycled worker
# process pool rather than in-process. GLiNER/PyTorch's native allocations
# (and glibc's per-thread malloc arenas under concurrent load) accumulate
# and are never returned to the OS for the life of a process; recycling the
# worker after a bounded number of tasks caps that growth instead of
# letting it ratchet upward for the lifetime of the server.
GLINER_WORKER_MAX_TASKS_ENV = "GLINER_WORKER_MAX_TASKS"
GLINER_WORKER_POOL_SIZE_ENV = "GLINER_WORKER_POOL_SIZE"
DEFAULT_WORKER_MAX_TASKS = 200
DEFAULT_WORKER_POOL_SIZE = 1

_worker_pool: ProcessPoolExecutor | None = None
_worker_pool_tasks_since_recycle = 0

# Only ever set inside a worker process (module is re-imported fresh under
# the "spawn" start method, so this starts as None in each new worker).
_worker_model_cache: Any | None = None

_ENTITY_TYPE_LINE_RE = re.compile(r"^\s*[-*]\s*`?([^:`]+?)`?\s*:")


class _Tokenizer(Protocol):
    def encode(self, content: str) -> list[int]: ...


def extract_entity_labels_from_guidance(entity_types_guidance: str) -> list[str]:
    """Extract bullet-list entity labels from prompt guidance markdown."""
    labels: list[str] = []
    for line in entity_types_guidance.splitlines():
        match = _ENTITY_TYPE_LINE_RE.match(line)
        if not match:
            continue
        label = match.group(1).strip()
        if label:
            labels.append(label)
    return labels


def _normalize_gliner_result(raw_entities: list[object]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in raw_entities:
        if isinstance(item, dict):
            normalized.append(
                {
                    "text": item.get("text", ""),
                    "label": item.get("label"),
                    "score": item.get("score"),
                    "start": item.get("start"),
                    "end": item.get("end"),
                }
            )
            continue

        normalized.append(
            {
                "text": getattr(item, "text", ""),
                "label": getattr(item, "label", None),
                "score": getattr(item, "score", None),
                "start": getattr(item, "start", None),
                "end": getattr(item, "end", None),
            }
        )
    return normalized


def _entity_text_key(entity: dict[str, Any]) -> str:
    """Return a case-insensitive key with insignificant whitespace collapsed."""
    return " ".join(str(entity.get("text", "")).split()).casefold()


def _select_ner_entities(
    entities: list[dict[str, Any]],
    *,
    max_entities: int,
    max_tokens: int,
    tokenizer: _Tokenizer | None,
) -> list[dict[str, Any]]:
    """Deduplicate and budget NER hints while preserving their source order."""
    best_by_text: dict[str, tuple[int, dict[str, Any]]] = {}
    for index, entity in enumerate(entities):
        key = _entity_text_key(entity)
        if not key:
            continue
        previous = best_by_text.get(key)
        score = float(entity.get("score") or 0.0)
        if previous is None or score > float(previous[1].get("score") or 0.0):
            best_by_text[key] = (index, entity)

    ranked = sorted(
        best_by_text.values(),
        key=lambda item: (-float(item[1].get("score") or 0.0), item[0]),
    )
    selected: list[tuple[int, dict[str, Any]]] = []
    used_tokens = 0
    for item in ranked:
        if len(selected) >= max_entities:
            break
        if tokenizer is not None:
            hint_tokens = len(tokenizer.encode(f"- {item[1]['text']}\n"))
            if used_tokens + hint_tokens > max_tokens:
                continue
            used_tokens += hint_tokens
        selected.append(item)

    selected.sort(key=lambda item: item[0])
    return [entity for _, entity in selected]


def _format_ner_entities(entities: list[dict[str, Any]]) -> str:
    if not entities:
        return ""

    lines: list[str] = []
    for ent in entities:
        entity_text = ent.get("text", "")
        lines.append(f"- {entity_text}")

    return "\n".join(lines)


def get_ner_model_cache_dir() -> Path:
    configured_dir = os.getenv(NER_MODEL_DIR_ENV, "").strip()
    if configured_dir:
        return Path(configured_dir).expanduser()
    return DEFAULT_NER_MODEL_CACHE_DIR


def _get_worker_model_sync() -> Any:
    """Lazily load + cache GLiNER once per worker process.

    Runs inside the recycled worker process under normal operation (or
    in-process under test doubles that bypass the real executor). Each
    fresh worker process re-imports this module, so the cache starts empty
    again after every recycle.
    """
    global _worker_model_cache

    if _worker_model_cache is not None:
        return _worker_model_cache

    from gliner import GLiNER

    logger.info(f"Loading GLiNER model from {NER_MODEL_NAME} in worker process...")
    cache_dir = get_ner_model_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)

    _worker_model_cache = GLiNER.from_pretrained(
        NER_MODEL_NAME,
        cache_dir=str(cache_dir),
    )
    logger.info("GLiNER model loaded successfully")
    return _worker_model_cache


def _worker_predict(
    text: str, labels: list[str], threshold: float, flat_ner: bool
) -> list[dict[str, Any]]:
    """Entry point executed inside the worker process.

    Only picklable plain args in, only picklable plain dicts out — GLiNER's
    own result objects aren't guaranteed picklable across the process
    boundary, so normalization happens here rather than in the caller.
    """
    model = _get_worker_model_sync()
    raw_entities = model.predict_entities(
        text, labels, flat_ner=flat_ner, threshold=threshold
    )
    return _normalize_gliner_result(list(raw_entities))


def _get_worker_pool() -> ProcessPoolExecutor:
    """Return the persistent GLiNER worker pool, recycling it once it has
    handled ``GLINER_WORKER_MAX_TASKS`` tasks.

    A fresh worker process starts with a clean allocator (no accumulated
    malloc arenas, no cached PyTorch scratch buffers) so recycling caps the
    long-run memory growth instead of letting a single long-lived worker
    ratchet upward for the server's whole lifetime. Uses the "spawn" start
    method rather than "fork": this pool is created from inside a live
    asyncio/uvicorn server process with open DB connection pools, and
    forking that process risks inheriting half-open connections or a lock
    held by another thread at fork time.
    """
    global _worker_pool, _worker_pool_tasks_since_recycle

    max_tasks = int(
        os.getenv(GLINER_WORKER_MAX_TASKS_ENV, str(DEFAULT_WORKER_MAX_TASKS))
    )

    if _worker_pool is not None and _worker_pool_tasks_since_recycle >= max_tasks:
        logger.info(
            f"Recycling GLiNER worker pool after {_worker_pool_tasks_since_recycle} tasks"
        )
        _worker_pool.shutdown(wait=False, cancel_futures=False)
        _worker_pool = None

    if _worker_pool is None:
        pool_size = int(
            os.getenv(GLINER_WORKER_POOL_SIZE_ENV, str(DEFAULT_WORKER_POOL_SIZE))
        )
        _worker_pool = ProcessPoolExecutor(
            max_workers=pool_size,
            mp_context=multiprocessing.get_context("spawn"),
        )
        _worker_pool_tasks_since_recycle = 0

    _worker_pool_tasks_since_recycle += 1
    return _worker_pool


async def recognize_entities(
    text: str,
    labels: list[str],
    threshold: float = 0.9,
    *,
    flat_ner: bool = True,
    max_entities: int = 50,
    max_tokens: int = 400,
    tokenizer: _Tokenizer | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    if not text or not labels:
        logger.warning("Empty text or labels provided to GLiNER, skipping")
        return "", []

    try:
        loop = asyncio.get_running_loop()
        pool = _get_worker_pool()
        normalized_entities = await loop.run_in_executor(
            pool, _worker_predict, text, labels, threshold, flat_ner
        )

        entities = _select_ner_entities(
            normalized_entities,
            max_entities=max_entities,
            max_tokens=max_tokens,
            tokenizer=tokenizer,
        )
        formatted = _format_ner_entities(entities)
        logger.debug(f"GLiNER recognized {len(entities)} entities")
        return formatted, entities
    except Exception as error:
        logger.error(f"Error during GLiNER recognition: {error}")
        return "", []
