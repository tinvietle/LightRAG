"""Pure helpers for the lane-fusion experiment (no faiss/langchain imports)."""

from __future__ import annotations

from typing import Iterable, Mapping


def rrf(
    rankings: Iterable[list[int]], k: int = 60, top: int | None = None
) -> list[int]:
    """Reciprocal-rank fusion of ranked lists of document ids.

    score(d) = sum over lists of 1 / (k + rank), rank starting at 1. Ties are broken
    by the smaller document id so the result is deterministic.
    """
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, doc in enumerate(ranking, start=1):
            scores[doc] = scores.get(doc, 0.0) + 1.0 / (k + rank)
    fused = [doc for doc, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))]
    return fused[:top] if top else fused


def concept_text(
    features: Iterable[str],
    labels: Mapping[str, str],
    idf: Mapping[str, float],
    max_terms: int = 60,
    max_absent: int = 15,
) -> str:
    """Readable concept list of one case for embedding.

    Present findings come rarest-first (the most discriminating concepts survive the
    cap); negated findings go to a separate "Absent" line.
    """
    present, absent = [], []
    for feature in features:
        negated = feature.startswith("NEG:")
        name = labels.get(feature) or feature
        (absent if negated else present).append((idf.get(feature, 0.0), name))

    def pick(items: list[tuple[float, str]], limit: int) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for _, name in sorted(items, key=lambda it: (-it[0], it[1])):
            key = name.casefold()
            if key not in seen:
                seen.add(key)
                out.append(name)
            if len(out) >= limit:
                break
        return out

    lines = []
    if present:
        lines.append("Findings: " + "; ".join(pick(present, max_terms)))
    if absent:
        lines.append("Absent: " + "; ".join(pick(absent, max_absent)))
    return "\n".join(lines)
