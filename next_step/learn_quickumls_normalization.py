#!/usr/bin/env python3
"""Small, commented lesson for the QuickUMLS normalization algorithm.

Run the built-in example (QuickUMLS is not required):

    python next_step/learn_quickumls_normalization.py

Run the same decision logic with the real QuickUMLS index:

    python next_step/learn_quickumls_normalization.py \
        --quickumls-index ../umls/quickumls_data \
        "heart attack" "myocardial infarction" "acute myocardial infarction"

This file teaches the conservative policy used in our evaluation. It does not
modify LightRAG storage.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import string
from typing import Any, Iterable, Sequence


@dataclass(frozen=True, slots=True)
class UMLSDecision:
    """The small amount of information needed for a merge decision."""

    entity: str
    accepted_cui: str | None
    reason: str


def is_full_span(entity: str, start: int, end: int) -> bool:
    """True if a match covers the whole entity except punctuation/whitespace.

    QuickUMLS offsets use Python's usual convention: ``start`` is inclusive and
    ``end`` is exclusive. A partial match is unsafe for automatic replacement.

    Example: in "acute myocardial infarction", matching only "myocardial
    infarction" leaves the meaningful word "acute", so this returns False.
    """

    outside_match = entity[:start] + entity[end:]
    allowed_outside = set(string.punctuation + string.whitespace)
    return all(character in allowed_outside for character in outside_match)


def flatten(raw_groups: Sequence[Sequence[dict[str, Any]]]) -> Iterable[dict[str, Any]]:
    """QuickUMLS returns groups of overlapping candidates; flatten them."""

    for group in raw_groups:
        yield from group


def decide_cui(
    entity: str,
    raw_groups: Sequence[Sequence[dict[str, Any]]],
    threshold: float = 0.90,
) -> UMLSDecision:
    """Accept exactly one high-confidence CUI for the complete entity.

    The three safety gates are:

    1. The match must cover the complete entity, not just some words.
    2. QuickUMLS lexical similarity must be at least ``threshold``.
    3. All candidates tied at the best score must agree on one CUI.

    Gate 3 matters because the same surface form can represent different medical
    concepts. When the best interpretation is ambiguous, we do not auto-merge.
    """

    full_matches = [
        candidate
        for candidate in flatten(raw_groups)
        if is_full_span(entity, int(candidate["start"]), int(candidate["end"]))
        and float(candidate["similarity"]) >= threshold
    ]
    if not full_matches:
        return UMLSDecision(entity, None, "no high-confidence full-span match")

    best_score = max(float(candidate["similarity"]) for candidate in full_matches)
    best_cuis = {
        str(candidate["cui"])
        for candidate in full_matches
        if float(candidate["similarity"]) == best_score
    }
    if len(best_cuis) != 1:
        return UMLSDecision(entity, None, f"ambiguous best CUIs: {sorted(best_cuis)}")

    return UMLSDecision(entity, best_cuis.pop(), "safe unique full-span CUI")


def group_synonyms(decisions: Iterable[UMLSDecision]) -> dict[str, list[str]]:
    """Names with the same accepted CUI are normalization candidates."""

    groups: dict[str, list[str]] = defaultdict(list)
    for decision in decisions:
        if decision.accepted_cui is not None:
            groups[decision.accepted_cui].append(decision.entity)
    return {
        cui: names
        for cui, names in groups.items()
        if len(names) > 1  # A one-name CUI cannot reduce the entity count.
    }


def toy_matches() -> dict[str, list[list[dict[str, Any]]]]:
    """Fake QuickUMLS output that makes each safety gate easy to inspect."""

    return {
        "heart attack": [[{"start": 0, "end": 12, "similarity": 1.0, "cui": "C1"}]],
        "myocardial infarction": [
            [{"start": 0, "end": 21, "similarity": 1.0, "cui": "C1"}]
        ],
        # Partial match: the important modifier "acute" is left outside.
        "acute myocardial infarction": [
            [{"start": 6, "end": 27, "similarity": 1.0, "cui": "C1"}]
        ],
        # Ambiguous match: two CUIs have the same best score.
        "cold": [
            [
                {"start": 0, "end": 4, "similarity": 1.0, "cui": "C2"},
                {"start": 0, "end": 4, "similarity": 1.0, "cui": "C3"},
            ]
        ],
    }


def real_matches(index: Path, entities: Sequence[str]) -> dict[str, Any]:
    """Call QuickUMLS only when the learner explicitly requests real mode."""

    try:
        from quickumls import QuickUMLS
    except ImportError as error:
        raise RuntimeError("QuickUMLS is not installed in this Python environment") from error

    matcher = QuickUMLS(str(index), threshold=0.7, similarity_name="jaccard")
    return {
        entity: matcher.match(entity, best_match=False, ignore_syntax=False)
        for entity in entities
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("entities", nargs="*")
    parser.add_argument("--quickumls-index", type=Path)
    args = parser.parse_args()

    if args.quickumls_index:
        if not args.entities:
            parser.error("provide one or more entities with --quickumls-index")
        matches = real_matches(args.quickumls_index, args.entities)
    else:
        matches = toy_matches()

    decisions = [decide_cui(entity, groups) for entity, groups in matches.items()]
    print("\nDecision for every entity:")
    for decision in decisions:
        print(f"  {decision.entity!r:32} -> {decision.accepted_cui}: {decision.reason}")

    groups = group_synonyms(decisions)
    print("\nSafe synonym groups:")
    for cui, names in groups.items():
        print(f"  {cui}: {names}  (reduction = {len(names) - 1})")


if __name__ == "__main__":
    main()
