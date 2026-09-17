from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


SCRIPT = Path(__file__).parents[2] / "next_step/evaluate_umls_normalization.py"
SPEC = importlib.util.spec_from_file_location("evaluate_umls_normalization", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def candidate(
    *,
    start: int,
    end: int,
    ngram: str,
    cui: str = "C1",
    similarity: float = 1.0,
) -> dict:
    return {
        "start": start,
        "end": end,
        "ngram": ngram,
        "term": ngram,
        "cui": cui,
        "similarity": similarity,
        "semtypes": {"T047"},
        "preferred": True,
    }


def test_full_span_allows_only_surrounding_punctuation() -> None:
    assert MODULE.is_full_span("(dyspnoea)", 1, 9)
    assert not MODULE.is_full_span("mild dyspnoea", 5, 13)


def test_partial_match_is_not_an_auto_candidate() -> None:
    row = MODULE.evaluate_entity(
        "mild dyspnoea",
        2,
        [[candidate(start=5, end=13, ngram="dyspnoea")]],
        0.9,
    )

    assert row["matched_any"]
    assert row["best_match"]["token_coverage"] == 0.5
    assert not row["has_full_span_match"]
    assert not row["auto_lexical_candidate"]


def test_unique_high_confidence_full_cui_is_an_auto_candidate() -> None:
    row = MODULE.evaluate_entity(
        "dyspnoea",
        1,
        [[candidate(start=0, end=8, ngram="dyspnoea", similarity=0.95)]],
        0.9,
    )

    assert row["has_high_confidence_full_span_match"]
    assert row["unique_best_cui"]
    assert row["auto_lexical_candidate"]


def test_equal_best_full_matches_with_different_cuis_are_ambiguous() -> None:
    row = MODULE.evaluate_entity(
        "ejection fraction",
        1,
        [[
            candidate(start=0, end=17, ngram="ejection fraction", cui="C1"),
            candidate(start=0, end=17, ngram="ejection fraction", cui="C2"),
        ]],
        0.9,
    )

    assert row["best_cuis"] == ["C1", "C2"]
    assert not row["unique_best_cui"]
    assert not row["auto_lexical_candidate"]


def test_summary_reports_entity_and_document_weighted_rates() -> None:
    rows = [
        {
            **MODULE.evaluate_entity(
                "dyspnoea",
                3,
                [[candidate(start=0, end=8, ngram="dyspnoea")]],
                0.9,
            ),
        },
        MODULE.evaluate_entity("unknown phrase", 1, [], 0.9),
    ]

    summary = MODULE.build_summary(rows, {"threshold": 0.7})

    assert summary["totals"]["entities"] == 2
    assert summary["totals"]["auto_lexical_candidate_percentage"] == 50.0
    assert (
        summary["totals"]["document_weighted_auto_lexical_candidate_percentage"]
        == 75.0
    )
    assert summary["totals"]["token_weighted_auto_lexical_candidate_count"] == 1
    assert summary["totals"]["token_weighted_auto_lexical_candidate_percentage"] == 33.333
