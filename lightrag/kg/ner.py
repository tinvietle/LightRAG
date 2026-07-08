from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from lightrag.utils import logger


DEFAULT_NER_MODEL_CACHE_DIR = Path("./ner_model")
NER_MODEL_DIR_ENV = "GLINER_MODEL_DIR"
NER_MODEL_NAME = "Ihor/gliner-biomed-base-v1.0"
_ner_model_cache: Any | None = None

_ENTITY_TYPE_LINE_RE = re.compile(r"^\s*[-*]\s*`?([^:`]+?)`?\s*:")


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
                }
            )
            continue

        normalized.append(
            {
                "text": getattr(item, "text", ""),
                "label": getattr(item, "label", None),
                "score": getattr(item, "score", None),
            }
        )
    return normalized


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


def _load_ner_model_sync() -> Any:
    from gliner import GLiNER

    return GLiNER.from_pretrained(
        NER_MODEL_NAME,
        cache_dir=str(get_ner_model_cache_dir()),
    )


async def _load_ner_model(force_reload: bool = False) -> Any:
    global _ner_model_cache

    if _ner_model_cache is not None and not force_reload:
        logger.debug("Using cached GLiNER model")
        return _ner_model_cache

    logger.info(f"Loading GLiNER model from {NER_MODEL_NAME}...")
    cache_dir = get_ner_model_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)

    loop = asyncio.get_running_loop()
    model = await loop.run_in_executor(None, _load_ner_model_sync)

    _ner_model_cache = model
    logger.info("GLiNER model loaded successfully")
    return model


async def recognize_entities(
    text: str,
    labels: list[str],
    threshold: float = 0.9,
) -> tuple[str, list[dict[str, Any]]]:
    if not text or not labels:
        logger.warning("Empty text or labels provided to GLiNER, skipping")
        return "", []

    try:
        model = await _load_ner_model()

        loop = asyncio.get_running_loop()
        raw_entities = await loop.run_in_executor(
            None,
            lambda: model.predict_entities(
                text,
                labels,
                flat_ner=False,
                threshold=threshold,
            ),
        )

        entities = _normalize_gliner_result(list(raw_entities))
        formatted = _format_ner_entities(entities)
        logger.debug(f"GLiNER recognized {len(entities)} entities")
        return formatted, entities
    except Exception as error:
        logger.error(f"Error during GLiNER recognition: {error}")
        return "", []
