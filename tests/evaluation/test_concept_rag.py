"""Pure-logic coverage for next_step/concept_rag (no gliner/quickumls needed)."""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "next_step" / "concept_rag")
)

from common import (  # noqa: E402
    read_jsonl,
    repair_jsonl,
    chunk_text,
    is_negated,
    normalize_term,
    resolve_spans,
    stratified_sample,
)
from evaluate_concepts import (  # noqa: E402
    ConceptIndex,
    build_case_features,
    case_metrics,
    chance_hit,
    feature_kind,
    is_real_term,
    unseen_rate,
    label_vote,
    make_cui_lookup,
    paired_difference_ci,
)


@pytest.mark.offline
@pytest.mark.parametrize(
    "text, mention, expected",
    [
        ("The patient has no fever today.", "fever", True),
        ("Denies cough and chills.", "cough", True),
        ("Negative for malignancy on biopsy.", "malignancy", True),
        ("She presented with fever and rash.", "fever", False),
        ("No trauma, but fever persisted.", "fever", False),  # clause break at 'but'
        ("No cough. Fever for two days.", "Fever", False),  # sentence break
        ("fever", "fever", False),
    ],
)
def test_is_negated(text, mention, expected):
    assert is_negated(text, text.index(mention)) is expected


@pytest.mark.offline
def test_normalize_term_drops_noise_and_keeps_clinical_terms():
    assert normalize_term("Lyme Disease") == "lyme disease"
    assert normalize_term("Left-sided weakness!") == "left sided weakness"
    assert normalize_term("patient") == ""
    assert normalize_term("12") == ""
    assert normalize_term("an") == ""


@pytest.mark.offline
def test_chunk_text_offsets_reconstruct_the_text():
    text = (
        ". ".join(f"Sentence number {i} has several words in it" for i in range(40))
        + "."
    )
    chunks = chunk_text(text, max_words=30)

    assert len(chunks) > 1
    for offset, chunk in chunks:
        assert text[offset : offset + len(chunk)] == chunk
    assert max(len(c.split()) for _, c in chunks) <= 40
    assert chunk_text("short text", 30) == [(0, "short text")]


@pytest.mark.offline
def test_resolve_spans_prefers_score_then_length_and_never_overlaps():
    candidates = [
        {"start": 0, "end": 10, "similarity": 0.9, "id": "long-low"},
        {"start": 0, "end": 5, "similarity": 1.0, "id": "short-high"},
        {"start": 5, "end": 10, "similarity": 1.0, "id": "second-high"},
        {"start": 20, "end": 25, "similarity": 0.8, "id": "separate"},
    ]
    chosen = [c["id"] for c in resolve_spans(candidates)]

    assert chosen == ["short-high", "second-high", "separate"]


@pytest.mark.offline
def test_stratified_sample_is_round_robin_and_deterministic():
    cases = [
        {"case_id": f"{g}{i}", "split": "train", "group": g}
        for g, n in (("a", 10), ("b", 10), ("c", 1))
        for i in range(n)
    ]
    first = stratified_sample(cases, 6, seed=1)
    second = stratified_sample(cases, 6, seed=1)

    assert [c["case_id"] for c in first] == [c["case_id"] for c in second]
    assert {c["group"] for c in first} == {"a", "b", "c"}
    assert stratified_sample(cases, 1000) == cases


DOCS = [
    {"fever", "rash", "rare"},
    {"fever", "rash"},
    {"fever", "cough"},
    {"cough"},
]


@pytest.mark.offline
def test_tfidf_ranks_the_document_sharing_the_rare_feature_first():
    index = ConceptIndex(DOCS)
    ranked = index.score({"fever", "rare"}, "tfidf")

    assert ranked[0][0] == 0
    assert [d for d, _ in ranked][:3] == [0, 1, 2]


@pytest.mark.offline
def test_jaccard_values_and_exclude():
    index = ConceptIndex(DOCS)
    scores = dict(index.score({"fever", "rash"}, "jaccard"))

    assert scores[1] == pytest.approx(1.0)
    assert scores[0] == pytest.approx(2 / 3)
    assert scores[2] == pytest.approx(1 / 3)
    assert 3 not in scores  # no shared feature
    assert 1 not in dict(index.score({"fever", "rash"}, "jaccard", exclude=1))


@pytest.mark.offline
def test_bm25_scores_only_overlapping_documents_in_descending_order():
    index = ConceptIndex(DOCS)
    ranked = index.score({"cough"}, "bm25")

    assert {d for d, _ in ranked} == {2, 3}
    assert ranked[0][1] >= ranked[1][1]
    assert ranked[0][0] == 3  # shorter document, same single match


@pytest.mark.offline
def test_label_vote_and_case_metrics():
    neighbours = ["x", "y", "y", "z"]

    # x = 1.0, y = 1/2 + 1/3 = 0.83, z = 0.25
    assert label_vote(neighbours) == ["x", "y", "z"]
    # three lower-ranked neighbours overtake a single top hit: y = 1.08 > x = 1.0
    assert label_vote(["x", "y", "y", "y"]) == ["y", "x"]
    metrics = case_metrics(neighbours, "y")
    assert metrics["vote@1"] == 0.0
    assert metrics["vote@5"] == 1.0
    assert metrics["hit@1"] == 0.0
    assert metrics["hit@5"] == 1.0
    assert metrics["mrr"] == pytest.approx(0.5)
    assert case_metrics(neighbours, "x")["vote@1"] == 1.0
    assert case_metrics(neighbours, "missing")["hit@10"] == 0.0


@pytest.mark.offline
def test_paired_difference_sign_follows_the_better_arm():
    worse = [0.0] * 30 + [1.0] * 10
    better = [1.0] * 30 + [1.0] * 10

    mean, low, high = paired_difference_ci(worse, better, resamples=500)

    assert mean == pytest.approx(0.75)
    assert low > 0
    assert paired_difference_ci(better, worse, resamples=500)[2] < 0


def _lookup():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE snomed (cui TEXT, sctid TEXT, tty TEXT, term TEXT, semtypes TEXT)"
    )
    conn.executemany(
        "INSERT INTO snomed VALUES (?,?,?,?,?)",
        [
            ("C1", "999", "SY", "synonym name", "T047"),
            ("C1", "111", "PT", "Preferred name", "T047"),
        ],
    )
    return make_cui_lookup(conn)


@pytest.mark.offline
def test_cui_lookup_prefers_preferred_term_and_falls_back_to_cui():
    lookup = _lookup()

    assert lookup("C1") == ("S111", "Preferred name")
    assert lookup("C404") == ("CC404", "C404")


def _case():
    return {
        "case_id": "c",
        "fields": {"history_of_present_illness": "Fever. No cough. Lyme disease."},
    }


def _mentions():
    return [
        {
            "field": "history_of_present_illness",
            "text": "Fever",
            "start": 0,
            "end": 5,
            "label": "Sign_symptom",
            "score": 0.9,
        },
        {
            "field": "history_of_present_illness",
            "text": "cough",
            "start": 10,
            "end": 15,
            "label": "Sign_symptom",
            "score": 0.9,
        },
        {
            "field": "history_of_present_illness",
            "text": "Lyme disease",
            "start": 17,
            "end": 29,
            "label": "Disease_disorder",
            "score": 0.95,
        },
        {
            "field": "history_of_present_illness",
            "text": "Fever",
            "start": 0,
            "end": 5,
            "label": "Sign_symptom",
            "score": 0.2,
        },
    ]


def _raw():
    field = "history_of_present_illness"
    return {
        "text_matches": [
            {"field": field, "text": "Fever", "start": 0, "end": 5, "cui": "C1"},
            {"field": field, "text": "cough", "start": 10, "end": 15, "cui": "C2"},
        ],
        "mention_links": [
            {"field": field, "text": "Fever", "start": 0, "end": 5, "cui": "C1"},
        ],
    }


@pytest.mark.offline
def test_arms_build_distinct_feature_sets():
    lookup = _lookup()
    args = (_case(), _mentions(), _raw())

    gliner = build_case_features(*args, "gliner", lookup)
    quickumls = build_case_features(*args, "quickumls", lookup)
    both = build_case_features(*args, "both", lookup)

    assert gliner == {
        "t:fever",
        "NEG:t:cough",
        "t:lyme disease",
    }  # 0.2-score mention ignored
    assert quickumls == {"S111", "NEG:CC2"}
    # 'Fever' is linked, so it appears as a concept only; the unlinked mentions stay as terms
    assert both == {"S111", "NEG:CC2", "NEG:t:cough", "t:lyme disease"}


@pytest.mark.offline
def test_negation_policies():
    lookup = _lookup()
    args = (_case(), _mentions(), _raw(), "quickumls", lookup)

    assert build_case_features(*args, negation="drop") == {"S111"}
    assert build_case_features(*args, negation="ignore") == {"S111", "CC2"}
    assert build_case_features(*args, negation="mark") == {"S111", "NEG:CC2"}


@pytest.mark.offline
def test_chance_hit_matches_the_hypergeometric_value():
    # 10 items, 2 share the label, draw 1: P = 2/10
    assert chance_hit(10, 2, 1) == pytest.approx(0.2)
    # draw 2: 1 - (8/10 * 7/9)
    assert chance_hit(10, 2, 2) == pytest.approx(1 - 8 / 10 * 7 / 9)
    assert chance_hit(10, 0, 5) == 0.0
    assert chance_hit(10, 9, 5) == 1.0  # fewer non-matching items than draws


@pytest.mark.offline
def test_is_real_term_rejects_numeric_and_symbol_matches():
    assert is_real_term("fever")
    assert is_real_term("T2 hyperintensity")
    assert not is_real_term("<50")
    assert not is_real_term("10/10")


@pytest.mark.offline
def test_excluded_gliner_labels_and_unreal_quickumls_text_are_skipped():
    lookup = _lookup()
    raw = {
        "text_matches": [
            {
                "field": "history_of_present_illness",
                "text": "<50",
                "start": 0,
                "end": 3,
                "cui": "C1",
            },
        ],
        "mention_links": [],
    }
    features = build_case_features(
        _case(),
        _mentions(),
        raw,
        "both",
        lookup,
        exclude_gliner_labels=frozenset({"Disease_disorder"}),
    )

    assert "S111" not in features  # '<50' match rejected
    assert "t:lyme disease" not in features  # label excluded
    assert "t:fever" in features


@pytest.mark.offline
def test_clean_labels_never_expose_a_bare_cui():
    lookup = _lookup()
    raw = {
        "text_matches": [
            {
                "field": "history_of_present_illness",
                "text": "Fever",
                "start": 0,
                "end": 5,
                "cui": "C1",
                "term": "Fever",
            },
            {
                "field": "history_of_present_illness",
                "text": "chain",
                "start": 6,
                "end": 11,
                "cui": "C777",
                "term": "Lymphatic chain",
            },
        ],
        "mention_links": [],
    }
    clean: dict[str, str] = {}
    build_case_features(
        _case(), [], raw, "quickumls", lookup, "ignore", clean_labels=clean
    )

    assert clean == {"S111": "Preferred name", "CC777": "Lymphatic chain"}


@pytest.mark.offline
def test_feature_kind_ignores_negation_prefix():
    assert feature_kind("S123") == "snomed"
    assert feature_kind("NEG:S123") == "snomed"
    assert feature_kind("CC9") == "cui"
    assert feature_kind("t:lyme disease") == "surface"
    assert feature_kind("NEG:t:cough") == "surface"


@pytest.mark.offline
def test_unseen_rate_train_test_and_leave_one_out():
    pool = [{"S1", "t:a"}, {"S1", "t:b"}]
    queries = [{"S1", "t:zzz", "CC5"}]

    rates = unseen_rate(queries, pool, leave_one_out=False)
    assert rates["snomed"] == {"features": 1, "unseen": 0, "rate": 0.0}
    assert rates["surface"]["rate"] == 1.0
    assert rates["cui"]["rate"] == 1.0
    assert rates["all"]["unseen"] == 2

    # leave-one-out: the query is in the pool, so a feature seen once is unseen
    loo = unseen_rate(pool, pool, leave_one_out=True)
    assert loo["snomed"]["rate"] == 0.0  # S1 is shared by both
    assert loo["surface"]["rate"] == 1.0  # t:a and t:b each occur once


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.offline
def test_read_jsonl_skips_only_a_damaged_last_line(tmp_path, capsys):
    good = _write(tmp_path / "a.jsonl", '{"n": 1}\n{"n": 2}\n{"n": 3, "cut"')
    assert [r["n"] for r in read_jsonl(good)] == [1, 2]
    assert "damaged last line" in capsys.readouterr().err

    bad_middle = _write(tmp_path / "b.jsonl", '{"n": 1}\n{broken\n{"n": 3}\n')
    with pytest.raises(Exception):
        list(read_jsonl(bad_middle))
    assert list(read_jsonl(tmp_path / "missing.jsonl")) == []


@pytest.mark.offline
def test_repair_jsonl_makes_the_file_safe_to_append_to(tmp_path):
    damaged = _write(tmp_path / "a.jsonl", '{"n": 1}\n{"n": 2, "cut"')
    assert repair_jsonl(damaged) > 0
    assert damaged.read_text(encoding="utf-8") == '{"n": 1}\n'

    no_newline = _write(tmp_path / "b.jsonl", '{"n": 1}\n{"n": 2}')
    assert repair_jsonl(no_newline) == 0
    assert no_newline.read_text(encoding="utf-8") == '{"n": 1}\n{"n": 2}\n'

    clean = _write(tmp_path / "c.jsonl", '{"n": 1}\n')
    assert repair_jsonl(clean) == 0
    assert clean.read_text(encoding="utf-8") == '{"n": 1}\n'
    assert repair_jsonl(tmp_path / "missing.jsonl") == 0
    assert repair_jsonl(_write(tmp_path / "empty.jsonl", "")) == 0


@pytest.mark.offline
def test_relevant_count_and_normalized_precision():
    labels = ["x", "y", "y", "z", "y", "w", "w", "w", "w", "w"]

    metrics = case_metrics(labels, "y", available=3)
    assert metrics["rel@10"] == 3.0
    assert metrics["np@10"] == pytest.approx(1.0)  # all 3 existing cases were found

    assert case_metrics(labels, "y", available=20)["np@10"] == pytest.approx(0.3)
    assert case_metrics(labels, "missing", available=5)["rel@10"] == 0.0
    assert "np@10" not in case_metrics(labels, "y")  # only when `available` is known
    # only the top 10 count
    assert case_metrics(["a"] * 10 + ["y"] * 5, "y", available=5)["rel@10"] == 0.0
