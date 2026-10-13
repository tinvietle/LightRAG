"""Shared helpers for the concept-overlap retrieval experiments.

No heavy imports here (no gliner, quickumls, langchain, scipy): this module is
imported from both the GLiNER environment and the QuickUMLS environment, and from
the unit tests.
"""

from __future__ import annotations

import json
import random
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Iterator

# Same field order as next_step/simple_rag so both experiments see the same text.
FIELD_ORDER = (
    "demographics",
    "chief_complaint",
    "history_of_present_illness",
    "past_medical_history",
    "exposures_risk_factors",
    "physical_exam_findings",
    "lab_findings",
    "imaging_findings",
)
CAPTION_FIELD = "image_findings"
ALL_FIELDS = FIELD_ORDER + (CAPTION_FIELD,)

# UMLS semantic types QuickUMLS may accept. "disease" is the high-precision set the
# server uses for NER hints; "wide" adds everything a clinician would call a
# finding, test, organism, anatomical site or intervention.
SEMTYPE_PRESETS: dict[str, frozenset[str]] = {
    "disease": frozenset({"T019", "T020", "T047", "T048", "T050", "T190", "T191"}),
    "wide": frozenset(
        {
            # diseases, injuries, abnormal states
            "T019",
            "T020",
            "T037",
            "T046",
            "T047",
            "T048",
            "T049",
            "T050",
            "T190",
            "T191",
            # signs, symptoms, findings, test results
            "T033",
            "T034",
            "T184",
            # anatomy
            "T017",
            "T018",
            "T021",
            "T022",
            "T023",
            "T024",
            "T029",
            "T030",
            "T031",
            # organisms (pathogens and hosts)
            "T001",
            "T002",
            "T004",
            "T005",
            "T007",
            "T008",
            "T204",
            # procedures and substances
            "T059",
            "T060",
            "T061",
            "T121",
            "T109",
            "T116",
            "T125",
            "T129",
            "T195",
        }
    ),
    "all": frozenset(),  # no filtering
}


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return "; ".join(filter(None, (as_text(item) for item in value)))
    if isinstance(value, dict):
        return "; ".join(f"{k}: {as_text(v)}" for k, v in value.items() if as_text(v))
    return str(value)


def load_cases(dataset: Path, split: str) -> list[dict[str, Any]]:
    """Cases of one split with their per-field text. Labels are kept apart."""
    root = dataset / split
    if not root.is_dir():
        raise SystemExit(f"Missing {split} split: {root}")
    cases = []
    for path in sorted(root.rglob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        record = record[0] if isinstance(record, list) and record else record
        structured = record.get("structured_fields") or {}
        fields = {
            name: text
            for name in FIELD_ORDER
            if (text := as_text(structured.get(name)))
        }
        if caption := as_text(record.get("image_captioning")):
            fields[CAPTION_FIELD] = caption
        cases.append(
            {
                "case_id": path.stem,
                "split": split,
                "file_name": path.name,
                "specific": str(record.get("snomed_extracted_disease_name") or ""),
                "group": str(record.get("snomed_grouped_disease_name") or ""),
                "fields": fields,
            }
        )
    return cases


def stratified_sample(
    cases: list[dict[str, Any]], n: int, seed: int = 0, key: str = "group"
) -> list[dict[str, Any]]:
    """Up to ``n`` cases taken round-robin across labels (deterministic)."""
    if n >= len(cases):
        return list(cases)
    rng = random.Random(seed)
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        buckets[case[key]].append(case)
    for bucket in buckets.values():
        rng.shuffle(bucket)
    order = sorted(buckets)
    rng.shuffle(order)
    picked: list[dict[str, Any]] = []
    while len(picked) < n and any(buckets.values()):
        for label in order:
            if buckets[label] and len(picked) < n:
                picked.append(buckets[label].pop())
    return sorted(picked, key=lambda c: (c["split"], c["case_id"]))


# ---------------------------------------------------------------- text helpers

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def chunk_text(text: str, max_words: int = 200) -> list[tuple[int, str]]:
    """Split ``text`` on sentence boundaries into (char_offset, chunk) pieces.

    GLiNER truncates long inputs, so long fields are fed in windows of about
    ``max_words`` words. Offsets let mentions be mapped back to the full field.
    """
    chunks: list[tuple[int, str]] = []
    start = 0
    current_start = 0
    current_words = 0
    for match in _SENTENCE_SPLIT.finditer(text):
        piece = text[start : match.start()]
        words = len(piece.split())
        if current_words and current_words + words > max_words:
            chunks.append((current_start, text[current_start:start].rstrip()))
            current_start, current_words = start, 0
        current_words += words
        start = match.end()
    tail_words = len(text[start:].split())
    if current_words and current_words + tail_words > max_words:
        chunks.append((current_start, text[current_start:start].rstrip()))
        current_start = start
    if text[current_start:].strip():
        chunks.append((current_start, text[current_start:]))
    return chunks


_NON_WORD = re.compile(r"[^0-9a-z]+")

# Terms GLiNER tags as entities but that say nothing about the disease. IDF already
# down-weights them; dropping them keeps feature sets and examples readable.
GENERIC_TERMS = frozenset(
    {
        "patient",
        "patients",
        "man",
        "woman",
        "male",
        "female",
        "boy",
        "girl",
        "child",
        "adult",
        "infant",
        "year",
        "years",
        "month",
        "months",
        "week",
        "weeks",
        "day",
        "days",
        "symptom",
        "symptoms",
        "sign",
        "signs",
        "history",
        "onset",
        "symptom onset",
        "presentation",
        "admission",
        "hospital",
        "follow up",
        "treatment",
        "examination",
        "evaluation",
    }
)


def normalize_term(text: str) -> str:
    """Lower-case surface form with punctuation collapsed; '' if not informative."""
    term = _NON_WORD.sub(" ", text.lower()).strip()
    if len(term) < 3 or term.isdigit() or term in GENERIC_TERMS:
        return ""
    return term


# Lightweight NegEx: a negation cue shortly before the mention, in the same
# clause. It is deliberately simple and conservative.
_NEG_CUES = (
    "no",
    "not",
    "without",
    "denies",
    "denied",
    "negative for",
    "absence of",
    "absent",
    "free of",
    "no evidence of",
    "ruled out",
    "never",
    "nor",
    "no history of",
    "no signs of",
    "no sign of",
    "unremarkable for",
)
_NEG_CUE_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(c) for c in sorted(_NEG_CUES, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)
_CLAUSE_BREAK = re.compile(r"[.;:\n]|\b(?:but|however|although|except|though)\b", re.I)
NEG_WINDOW_WORDS = 6


def is_negated(text: str, start: int) -> bool:
    """True when a negation cue precedes ``text[start:]`` within the same clause."""
    before = text[:start]
    breaks = list(_CLAUSE_BREAK.finditer(before))
    clause = before[breaks[-1].end() :] if breaks else before
    words = clause.split()
    if not words:
        return False
    window = " ".join(words[-NEG_WINDOW_WORDS:])
    return bool(_NEG_CUE_RE.search(window))


# ----------------------------------------------------------------------- jsonl


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as sink:
        for row in rows:
            sink.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as sink:
        sink.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Rows of a jsonl file. A damaged FINAL line (a write cut off by a shutdown) is
    skipped with a warning; damage anywhere else still raises."""
    if not path.exists():
        return
    with path.open(encoding="utf-8") as source:
        lines = [line for line in source if line.strip()]
    for number, line in enumerate(lines):
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            if number == len(lines) - 1:
                print(f"warning: skipping damaged last line of {path}", file=sys.stderr)
                return
            raise


def repair_jsonl(path: Path) -> int:
    """Make a jsonl file safe to append to after an interrupted run.

    Removes a damaged final line, and adds the newline a valid final line lacks, so the
    next appended record cannot be glued onto the end of a cut-off one. Returns the
    number of bytes removed.
    """
    if not path.exists():
        return 0
    data = path.read_bytes()
    if not data:
        return 0
    stripped = data.rstrip(b"\n")
    cut = stripped.rfind(b"\n") + 1  # start of the last line
    try:
        json.loads(stripped[cut:])
    except json.JSONDecodeError:
        path.write_bytes(data[:cut])
        return len(data) - cut
    if not data.endswith(b"\n"):
        path.write_bytes(data + b"\n")
    return 0


# ------------------------------------------------------------- span resolution


def resolve_spans(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Greedy non-overlapping selection: best similarity first, then longest.

    Each candidate needs ``start``, ``end`` and ``similarity``. This is applied
    AFTER the semantic-type filter, so a valid short span is not discarded in
    favour of a longer one whose type was filtered out.
    """
    chosen: list[dict[str, Any]] = []
    for candidate in sorted(
        candidates,
        key=lambda c: (
            -float(c["similarity"]),
            -(int(c["end"]) - int(c["start"])),
            int(c["start"]),
        ),
    ):
        start, end = int(candidate["start"]), int(candidate["end"])
        if all(end <= int(o["start"]) or start >= int(o["end"]) for o in chosen):
            chosen.append(candidate)
    return sorted(chosen, key=lambda c: int(c["start"]))
