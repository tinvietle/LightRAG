"""Soft (embedding-assisted) concept matching, as a pure scoring module.

No faiss/numpy here: the runner (soft_match.py) computes feature-to-feature cosine
neighbours with embeddings and hands them in as a plain dict, so this logic is unit
testable.

Three matching modes for a query feature q against a pool case d:
  exact      only d containing q itself counts (the plain IDF lane)
  soft_only  only d containing a NEAR-NEIGHBOUR g of q counts (g != q), credit = cosine
  hybrid     exact credit (1.0) plus near-neighbour credit (cosine)

Rarity (IDF) of the soft modes uses the EXPANDED support of a feature: the number of
pool cases containing the feature or any of its neighbours, so a concept that merely
looks rare because other cases word it differently is not over-weighted.

A match q -> g in case d contributes  credit * idf(q) * idf(g)  (the exact case gives
idf(q)^2, i.e. the TF-IDF cosine numerator), the best match per (q, d) only, summed over
q and divided by the case's vector length.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Mapping, Sequence

MODES = ("exact", "soft_only", "hybrid")


def filter_neighbours(
    raw: Sequence[tuple[str, float]], tau: float, max_neighbours: int, self_id: str
) -> list[tuple[str, float]]:
    """Neighbours with cosine >= tau, best first, excluding the feature itself."""
    kept = [(g, s) for g, s in raw if g != self_id and s >= tau]
    kept.sort(key=lambda item: (-item[1], item[0]))
    return kept[:max_neighbours]


class SoftIndex:
    def __init__(
        self,
        docs: list[set[str]],
        neighbours: Mapping[str, Sequence[tuple[str, float]]],
        tau: float,
        max_neighbours: int = 5,
    ) -> None:
        self.docs = docs
        self.n = len(docs)
        self.tau = tau
        self.postings: dict[str, list[int]] = defaultdict(list)
        for position, features in enumerate(docs):
            for feature in features:
                self.postings[feature].append(position)
        self._raw = neighbours
        self.max_neighbours = max_neighbours
        self._nbr_cache: dict[str, list[tuple[str, float]]] = {}
        self._df_cache: dict[tuple[str, bool], int] = {}
        self._norm_cache: dict[bool, list[float]] = {}

    # ---------------------------------------------------------------- features
    def neighbours_of(self, feature: str) -> list[tuple[str, float]]:
        """Pool-present neighbours of ``feature`` at this index's tau / cap."""
        if feature not in self._nbr_cache:
            present = [
                (g, s) for g, s in self._raw.get(feature, ()) if g in self.postings
            ]
            self._nbr_cache[feature] = filter_neighbours(
                present, self.tau, self.max_neighbours, feature
            )
        return self._nbr_cache[feature]

    def df(self, feature: str, expanded: bool) -> int:
        key = (feature, expanded)
        if key not in self._df_cache:
            cases = set(self.postings.get(feature, ()))
            if expanded:
                for g, _ in self.neighbours_of(feature):
                    cases.update(self.postings[g])
            self._df_cache[key] = len(cases)
        return self._df_cache[key]

    def idf(self, feature: str, expanded: bool) -> float:
        df = self.df(feature, expanded)
        return math.log((self.n - df + 0.5) / (df + 0.5) + 1.0)

    def norms(self, expanded: bool) -> list[float]:
        if expanded not in self._norm_cache:
            self._norm_cache[expanded] = [
                math.sqrt(sum(self.idf(f, expanded) ** 2 for f in d)) or 1.0
                for d in self.docs
            ]
        return self._norm_cache[expanded]

    # ------------------------------------------------------------------ scoring
    def score(
        self, query: set[str], mode: str, exclude: int | None = None
    ) -> list[tuple[int, float]]:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        expanded = mode != "exact"
        total: dict[int, float] = defaultdict(float)
        for feature in query:
            weight_q = self.idf(feature, expanded)
            matches: list[tuple[str, float]] = []
            if mode in ("exact", "hybrid") and feature in self.postings:
                matches.append((feature, 1.0))
            if mode in ("soft_only", "hybrid"):
                matches.extend(self.neighbours_of(feature))
            best: dict[int, float] = {}
            for g, credit in matches:
                value = credit * weight_q * self.idf(g, expanded)
                for position in self.postings[g]:
                    if position != exclude and value > best.get(position, 0.0):
                        best[position] = value
            for position, value in best.items():
                total[position] += value
        norms = self.norms(expanded)
        ranked = [(p, v / norms[p]) for p, v in total.items()]
        return sorted(ranked, key=lambda item: (-item[1], item[0]))
