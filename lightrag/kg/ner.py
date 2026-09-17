from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any, Protocol

from lightrag.utils import logger


DEFAULT_NER_MODEL_CACHE_DIR = Path("./ner_model")
NER_MODEL_DIR_ENV = "GLINER_MODEL_DIR"
NER_MODEL_NAME = "Ihor/gliner-biomed-base-v1.0"
_ner_model_cache: Any | None = None

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
        model = await _load_ner_model()

        loop = asyncio.get_running_loop()
        raw_entities = await loop.run_in_executor(
            None,
            lambda: model.predict_entities(
                text,
                labels,
                flat_ner=flat_ner,
                threshold=threshold,
            ),
        )

        entities = _select_ner_entities(
            _normalize_gliner_result(list(raw_entities)),
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
