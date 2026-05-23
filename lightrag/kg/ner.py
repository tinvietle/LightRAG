"""
NER (Named Entity Recognition) module using GLiNER for improved entity extraction.

Handles GLiNER model loading, caching, and entity recognition with medical domain focus.
"""

import os
from pathlib import Path
import asyncio
from typing import Optional
import pipmaster as pm

# Install GLiNER if not present
if not pm.is_installed("gliner"):
    pm.install("gliner")

from gliner import GLiNER

from lightrag.utils import logger


# Model caching configuration
NER_MODEL_CACHE_DIR = Path("data/ner_model")
NER_MODEL_NAME = "Ihor/gliner-biomed-base-v1.0"

# Global model cache (lazy-loaded)
_ner_model_cache: Optional[GLiNER] = None


def _ensure_cache_dir():
    """Ensure the NER model cache directory exists."""
    NER_MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)


async def _load_ner_model(force_reload: bool = False) -> GLiNER:
    """
    Load or retrieve cached GLiNER model.
    
    Args:
        force_reload: If True, download model even if cached version exists
        
    Returns:
        GLiNER model instance
    """
    global _ner_model_cache
    
    if _ner_model_cache is not None and not force_reload:
        logger.debug("Using cached GLiNER model")
        return _ner_model_cache
    
    logger.info(f"Loading GLiNER model from {NER_MODEL_NAME}...")
    _ensure_cache_dir()
    
    # Run in executor to avoid blocking async loop during model download
    loop = asyncio.get_event_loop()
    model = await loop.run_in_executor(
        None,
        lambda: GLiNER.from_pretrained(
            NER_MODEL_NAME,
            cache_dir=str(NER_MODEL_CACHE_DIR)
        )
    )
    
    _ner_model_cache = model
    logger.info("GLiNER model loaded successfully")
    return model


def _format_ner_entities(entities: list[dict]) -> str:
    """
    Format GLiNER recognized entities into context string for LLM prompt.
    
    Args:
        entities: List of entity dicts with 'text', 'label', 'score' keys
        
    Returns:
        Formatted string for prompt injection
    """
    if not entities:
        return ""
    
    lines = []
    for ent in entities:
        entity_text = ent.get("text", "")
        lines.append(f"- {entity_text}")
    
    return "\n".join(lines)


async def recognize_entities(
    text: str,
    labels: list[str],
    threshold: float = 0.9,
) -> tuple[str, list[dict]]:
    """
    Recognize entities in text using GLiNER.
    
    Args:
        text: Input text to extract entities from
        labels: List of entity type labels to recognize
        threshold: Confidence threshold for entity recognition (0.0-1.0)
        
    Returns:
        Tuple of (formatted_context_string, raw_entities_list)
    """
    if not text or not labels:
        logger.warning("Empty text or labels provided to NER, skipping")
        return "", []
    
    try:
        model = await _load_ner_model()
        
        # Run entity prediction in executor to avoid blocking
        loop = asyncio.get_event_loop()
        entities = await loop.run_in_executor(
            None,
            lambda: model.predict_entities(
                text,
                labels,
                flat_ner=False,
                threshold=threshold
            )
        )
        
        # Format recognized entities for prompt
        formatted = _format_ner_entities(entities)
        logger.debug(f"NER recognized {len(entities)} entities")
        
        return formatted, entities
    
    except Exception as e:
        logger.error(f"Error during NER recognition: {str(e)}")
        return "", []


def clear_model_cache():
    """Clear the cached GLiNER model from memory."""
    global _ner_model_cache
    _ner_model_cache = None
    logger.debug("GLiNER model cache cleared")
