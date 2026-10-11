"""Tests for the pure helpers in next_step/eval_answers_snomed.py."""

from pathlib import Path
import sys

import pytest

NEXT_STEP_DIR = Path(__file__).resolve().parents[2] / "next_step"
if str(NEXT_STEP_DIR) not in sys.path:
    sys.path.insert(0, str(NEXT_STEP_DIR))

from eval_answers_snomed import best_relation, parenthetical_candidates  # noqa: E402


@pytest.mark.offline
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Post-surgical biliary infection (cholangitis)", [("cholangitis", "name")]),
        ("Diffuse large B-cell lymphoma (DLBCL) in the oral cavity", [("DLBCL", "abbreviation")]),
        ("Aspergillus sinusitis (invasive fungal sinusitis)", [("invasive fungal sinusitis", "name")]),
        ("Disseminated herpes zoster (shingles)", [("shingles", "name")]),
        ("Systemic polyomavirus infection (non-PML).", []),
        ("Malaria encephalitis (non-cerebral malaria encephalopathy)", []),
        ("Vegetation (possibly related to *Brucellosis*)", []),
        ("Rickettsioses (e.g., Rocky Mountain spotted fever)", []),
        ("Pneumonia (Reference id 5, \"Infective endocarditis\")", []),
        ("ESRD (uremic encephalopathy or metabolic derangement)", []),
        ("No parentheses here", []),
    ],
)
def test_parenthetical_candidates_keeps_synonyms_and_drops_hedges(raw, expected):
    assert parenthetical_candidates(raw) == expected


@pytest.mark.offline
def test_best_relation_prefers_equal_then_more_specific_then_generic():
    assert best_relation(None, "more_generic") == "more_generic"
    assert best_relation("more_generic", "more_specific") == "more_specific"
    assert best_relation("more_specific", "equal", None) == "equal"
    assert best_relation(None, None) is None
