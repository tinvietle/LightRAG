"""Pure helpers for the MedEmbed experiment (no torch / transformers imports)."""

from __future__ import annotations

import re

import numpy as np

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def split_units(text: str) -> list[str]:
    """Sentences and field lines of a case text, in order, without empty pieces."""
    return [piece.strip() for piece in _SENTENCE_SPLIT.split(text) if piece.strip()]


def pack_units(units: list[str], lengths: list[int], max_tokens: int) -> list[str]:
    """Greedily join consecutive units into chunks of at most ``max_tokens`` tokens.

    A single unit longer than the limit becomes its own chunk (the tokenizer will
    truncate it). ``lengths[i]`` is the token count of ``units[i]``.
    """
    chunks: list[str] = []
    current: list[str] = []
    used = 0
    for unit, length in zip(units, lengths):
        if current and used + length > max_tokens:
            chunks.append(" ".join(current))
            current, used = [], 0
        current.append(unit)
        used += length
    if current:
        chunks.append(" ".join(current))
    return chunks


def combine_vectors(
    vectors: np.ndarray, weights: list[float] | None = None
) -> np.ndarray:
    """Weighted mean of unit vectors, re-normalised to unit length."""
    if len(vectors) == 0:
        raise ValueError("no vectors to combine")
    w = np.ones(len(vectors)) if weights is None else np.asarray(weights, dtype=float)
    mean = (vectors * w[:, None]).sum(axis=0) / w.sum()
    return (mean / max(np.linalg.norm(mean), 1e-12)).astype(np.float32)


def rank_neighbours(
    query: np.ndarray, matrix: np.ndarray, k: int, exclude: int | None = None
) -> list[tuple[int, float]]:
    """Top-k rows of ``matrix`` by inner product with ``query`` (best first)."""
    scores = matrix @ query
    if exclude is not None:
        scores = scores.copy()
        scores[exclude] = -np.inf
    order = np.argsort(-scores, kind="stable")[:k]
    return [(int(i), float(scores[i])) for i in order]
