"""Coverage for next_step/concept_rag/soft_scoring.py (embedding-assisted matching)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "next_step" / "concept_rag")
)

from evaluate_concepts import ConceptIndex  # noqa: E402
from soft_scoring import SoftIndex, filter_neighbours  # noqa: E402

DOCS = [
    {"liver abscess", "fever", "rash"},
    {"hepatic abscess", "fever"},
    {"hepatic cyst", "cough"},
    {"fever", "cough"},
    {"rash"},
]
# 'liver abscess' ~ 'hepatic abscess' (0.92), ~ 'hepatic cyst' (0.78)
NEIGHBOURS = {
    "liver abscess": [("hepatic abscess", 0.92), ("hepatic cyst", 0.78)],
    "hepatic abscess": [("liver abscess", 0.92)],
}


@pytest.mark.offline
def test_filter_neighbours_applies_threshold_cap_order_and_drops_self():
    raw = [("a", 0.9), ("b", 0.95), ("self", 1.0), ("c", 0.7), ("d", 0.85)]

    assert filter_neighbours(raw, 0.8, 2, "self") == [("b", 0.95), ("a", 0.9)]
    assert filter_neighbours(raw, 0.8, 9, "self") == [
        ("b", 0.95),
        ("a", 0.9),
        ("d", 0.85),
    ]
    assert filter_neighbours([], 0.5, 3, "x") == []


@pytest.mark.offline
def test_exact_mode_reproduces_the_plain_tfidf_lane():
    soft = SoftIndex(DOCS, NEIGHBOURS, tau=0.8)
    plain = ConceptIndex(DOCS)
    query = {"liver abscess", "fever"}

    ours = dict(soft.score(query, "exact"))
    theirs = dict(plain.score(query, "tfidf"))

    assert ours.keys() == theirs.keys()
    for doc, value in theirs.items():
        assert ours[doc] == pytest.approx(value)


@pytest.mark.offline
def test_soft_only_credits_neighbours_and_never_the_identical_feature():
    soft = SoftIndex(DOCS, NEIGHBOURS, tau=0.8)
    scores = dict(soft.score({"liver abscess"}, "soft_only"))

    assert 1 in scores  # has 'hepatic abscess', the 0.92 neighbour
    assert 0 not in scores  # has only the identical 'liver abscess'
    assert 2 not in scores  # 'hepatic cyst' is below tau
    assert dict(soft.score({"liver abscess"}, "exact")).keys() == {0}


@pytest.mark.offline
def test_hybrid_adds_neighbour_credit_on_top_of_the_exact_match():
    soft = SoftIndex(DOCS, NEIGHBOURS, tau=0.8)
    hybrid = dict(soft.score({"liver abscess"}, "hybrid"))
    assert set(hybrid) == {0, 1}  # the neighbour case is retrieved too

    # Two cases of equal length: the exact match (credit 1.0) beats the 0.92 neighbour.
    equal = SoftIndex(
        [{"liver abscess", "x"}, {"hepatic abscess", "y"}], NEIGHBOURS, tau=0.8
    )
    scores = dict(equal.score({"liver abscess"}, "hybrid"))
    assert scores[0] > scores[1] > 0
    assert scores[1] / scores[0] == pytest.approx(0.92)


@pytest.mark.offline
def test_expanded_support_lowers_the_weight_of_a_feature_with_hidden_variants():
    soft = SoftIndex(DOCS, NEIGHBOURS, tau=0.8)

    assert soft.df("liver abscess", expanded=False) == 1
    assert soft.df("liver abscess", expanded=True) == 2  # + the 'hepatic abscess' case
    assert soft.idf("liver abscess", True) < soft.idf("liver abscess", False)
    # a feature with no neighbours is unchanged
    assert soft.df("fever", True) == soft.df("fever", False) == 3


@pytest.mark.offline
def test_tau_changes_which_neighbours_count_and_exclude_removes_a_case():
    loose = SoftIndex(DOCS, NEIGHBOURS, tau=0.7)
    strict = SoftIndex(DOCS, NEIGHBOURS, tau=0.95)

    assert 2 in dict(loose.score({"liver abscess"}, "soft_only"))
    assert strict.score({"liver abscess"}, "soft_only") == []
    assert 0 not in dict(loose.score({"liver abscess"}, "hybrid", exclude=0))


@pytest.mark.offline
def test_neighbours_missing_from_the_pool_are_ignored_and_bad_mode_rejected():
    soft = SoftIndex(DOCS, {"x": [("not in pool", 0.99)]}, tau=0.5)

    assert soft.neighbours_of("x") == []
    with pytest.raises(ValueError):
        soft.score({"x"}, "fuzzy")
