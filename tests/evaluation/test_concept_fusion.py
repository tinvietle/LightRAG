"""Coverage for next_step/concept_rag/fusion.py (reciprocal-rank fusion, concept text)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "next_step" / "concept_rag")
)

from fusion import concept_text, rrf  # noqa: E402


@pytest.mark.offline
def test_rrf_rewards_agreement_between_lists():
    fused = rrf([[1, 2, 3], [3, 1, 4]], k=60)

    assert fused[:2] == [1, 3]  # both lists rank them high
    assert set(fused) == {1, 2, 3, 4}


@pytest.mark.offline
def test_rrf_is_deterministic_on_ties_and_truncates():
    assert rrf([[5, 9], [9, 5]], k=60) == [5, 9]  # equal scores -> smaller id first
    assert rrf([[1, 2, 3, 4]], top=2) == [1, 2]
    assert rrf([]) == []


@pytest.mark.offline
def test_concept_text_orders_rarest_first_caps_and_separates_absent():
    labels = {
        "a": "Common finding",
        "b": "Rare finding",
        "c": "Mid finding",
        "NEG:d": "Cough",
        "e": "Common finding",
    }
    idf = {"a": 0.1, "b": 3.0, "c": 1.0, "e": 0.2, "NEG:d": 1.5}

    text = concept_text(["a", "b", "c", "e", "NEG:d"], labels, idf, max_terms=3)

    lines = text.split("\n")
    assert (
        lines[0] == "Findings: Rare finding; Mid finding; Common finding"
    )  # duplicates by name collapse
    assert lines[1] == "Absent: Cough"


@pytest.mark.offline
def test_concept_text_empty_when_no_features():
    assert concept_text([], {}, {}) == ""
