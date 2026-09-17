from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import numpy as np


SCRIPT = Path(__file__).parents[2] / "next_step/demo_sapbert_entity_similarity.py"
SPEC = importlib.util.spec_from_file_location("demo_sapbert_entity_similarity", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def row(name: str, cui: str | None = None) -> dict:
    return {
        "entity_name": name,
        "auto_lexical_candidate": cui is not None,
        "best_cuis": [cui] if cui else [],
    }


def test_select_demo_rows_includes_every_known_duplicate() -> None:
    rows = [row("HTN", "C1"), row("hypertension", "C1"), row("other")]

    selected = MODULE.select_demo_rows(rows, max_entities=2, seed=1)

    assert {item["entity_name"] for item in selected} == {"HTN", "hypertension"}


def test_modifier_conflicts_detect_clinical_differences() -> None:
    conflicts = MODULE.modifier_conflicts(
        "acute left renal failure", "chronic right renal failure"
    )

    assert conflicts == ["laterality", "temporality"]
    assert MODULE.modifier_conflicts("heart attack", "myocardial infarction") == []


def test_l2_normalize_produces_unit_vectors() -> None:
    result = MODULE.l2_normalize(np.asarray([[3.0, 4.0]], dtype=np.float32))

    assert np.allclose(result, [[0.6, 0.8]])


def test_pair_classification_uses_only_unique_cuis() -> None:
    assert MODULE.classify_pair("C1", "C1") == "same_cui"
    assert MODULE.classify_pair("C1", "C2") == "different_cui"
    assert MODULE.classify_pair("C1", None) == "unknown"
    assert MODULE.lexical_key("Chest X-ray") == MODULE.lexical_key("chest x ray")


def test_threshold_metrics_exclude_modifier_conflicts() -> None:
    pairs = [
        {
            "similarity": 0.96,
            "label": "same_cui",
            "modifier_conflicts": [],
            "lexical_equivalent": False,
        },
        {
            "similarity": 0.97,
            "label": "different_cui",
            "modifier_conflicts": ["laterality"],
            "lexical_equivalent": False,
        },
        {
            "similarity": 0.95,
            "label": "unknown",
            "modifier_conflicts": [],
            "lexical_equivalent": True,
        },
    ]

    metrics = MODULE.evaluate_thresholds(pairs, known_positive_total=2)["0.95"]

    assert metrics["accepted_pairs"] == 2
    assert metrics["same_cui_pairs"] == 1
    assert metrics["different_cui_pairs"] == 0
    assert metrics["known_pair_precision_proxy"] == 1.0
    assert metrics["known_positive_neighbor_recall"] == 0.5
    assert metrics["lexical_equivalent_pairs"] == 1
