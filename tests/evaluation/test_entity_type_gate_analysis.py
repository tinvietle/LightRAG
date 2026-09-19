"""Tests for the read-only entity-type gate analysis helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
NEXT_STEP = ROOT / "next_step"
sys.path.insert(0, str(NEXT_STEP))

SPEC = importlib.util.spec_from_file_location(
    "analyze_entity_type_gate", NEXT_STEP / "analyze_entity_type_gate.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

EntityMetadata = MODULE.EntityMetadata
lexical_key = MODULE.lexical_key
rate = MODULE.rate
types_disagree = MODULE.types_disagree


def test_lexical_key_ignores_case_punctuation_and_spacing() -> None:
    assert lexical_key("Hematoxylin & Eosin (H&E)") == lexical_key(
        "hematoxylin-eosin h e"
    )


def test_types_disagree_only_for_two_concrete_types() -> None:
    metadata = {
        "Abscess": EntityMetadata("Disease_disorder", ""),
        "abscess": EntityMetadata("Clinical_event", ""),
        "ABSCESS": EntityMetadata("Other", ""),
    }

    assert types_disagree("Abscess", "abscess", metadata)
    assert not types_disagree("Abscess", "ABSCESS", metadata)
    assert not types_disagree("Abscess", "missing", metadata)


def test_rate_handles_empty_denominator() -> None:
    assert rate(1, 4) == 25.0
    assert rate(0, 0) == 0.0
