"""Coverage for next_step/simple_rag/relevant_count.py (relevant cases in the top k)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "next_step" / "simple_rag")
)

from relevant_count import count_relevant  # noqa: E402


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


@pytest.mark.offline
def test_counts_matches_after_normalisation_within_the_top_k():
    labels = ["Lyme  Disease", "Dengue", "lyme disease", "Brucellosis"] + [
        "lyme disease"
    ] * 8

    assert (
        count_relevant(labels, "Lyme disease", _normalize, 10) == 8
    )  # only the first 10 count
    assert count_relevant(labels, "Lyme disease", _normalize, 3) == 2
    assert count_relevant(labels, "Lyme disease", _normalize, 1) == 1


@pytest.mark.offline
def test_missing_truth_or_labels_count_zero():
    assert count_relevant(["a", "b"], "", _normalize, 10) == 0
    assert count_relevant([], "a", _normalize, 10) == 0
    assert count_relevant(["x"] * 5, "y", _normalize, 10) == 0
