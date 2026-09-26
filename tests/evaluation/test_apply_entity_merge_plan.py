import json
from pathlib import Path

import pytest

import sys

NEXT_STEP_DIR = Path(__file__).resolve().parents[2] / "next_step"
if str(NEXT_STEP_DIR) not in sys.path:
    sys.path.insert(0, str(NEXT_STEP_DIR))

from apply_entity_merge_plan import load_journal  # noqa: E402


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


@pytest.mark.offline
def test_load_journal_ignores_batch_flush_events(tmp_path):
    """batch_flush rows have no component_id; load_journal must skip them,
    not crash, when resuming a run that already flushed at least once."""
    journal_path = tmp_path / "apply_journal.jsonl"
    _write_jsonl(
        journal_path,
        [
            {"component_id": "merge-a", "status": "pending"},
            {"component_id": "merge-a", "status": "applied"},
            {
                "event": "batch_flush",
                "status": "applied",
                "reason": "periodic",
                "covered_component_ids": ["merge-a"],
            },
            {"component_id": "merge-b", "status": "rejected_precondition"},
        ],
    )

    latest = load_journal(journal_path)

    assert latest == {"merge-a": "applied", "merge-b": "rejected_precondition"}


@pytest.mark.offline
def test_load_journal_keeps_latest_status_per_component(tmp_path):
    journal_path = tmp_path / "apply_journal.jsonl"
    _write_jsonl(
        journal_path,
        [
            {"component_id": "merge-a", "status": "pending"},
            {"component_id": "merge-a", "status": "failed"},
        ],
    )

    latest = load_journal(journal_path)

    assert latest == {"merge-a": "failed"}


@pytest.mark.offline
def test_load_journal_missing_file_returns_empty(tmp_path):
    assert load_journal(tmp_path / "does_not_exist.jsonl") == {}
