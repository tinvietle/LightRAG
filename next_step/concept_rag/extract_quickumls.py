#!/usr/bin/env python3
"""Stage 2 (QuickUMLS environment): CUIs from text and for GLiNER mentions.

Reads ``cases.jsonl`` (and ``mentions_gliner.jsonl`` when present) from ``--out`` and
writes ``quickumls_raw.jsonl`` with, per case:
  text_matches   QuickUMLS run directly on each field (the "QuickUMLS only" arm)
  mention_links  each GLiNER mention linked to its best CUI (used by the "both" arm)

"Wider accepted terms" is controlled by ``--semtypes`` (disease | wide | all or an
explicit T-code list) and ``--threshold`` (lower accepts looser string matches).

Run it with the UMLS environment (the one that has ``quickumls``):

    .venv-quickumls/bin/python next_step/concept_rag/extract_quickumls.py \
        --out artifacts/concept_rag/demo50 --semtypes wide --threshold 0.8
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    SEMTYPE_PRESETS,
    append_jsonl,
    read_jsonl,
    repair_jsonl,
    resolve_spans,
)


def parse_semtypes(value: str) -> frozenset[str]:
    if value in SEMTYPE_PRESETS:
        return SEMTYPE_PRESETS[value]
    return frozenset(code.strip() for code in value.split(",") if code.strip())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--index", type=Path, default=Path("data/quickumls_data"))
    parser.add_argument("--nltk-data", type=Path, default=Path("data/nltk_data"))
    parser.add_argument(
        "--semtypes", default="wide", help="disease | wide | all | T047,T184,..."
    )
    parser.add_argument("--threshold", type=float, default=0.8, help="Text matching.")
    parser.add_argument(
        "--link-threshold", type=float, default=0.7, help="Mention linking."
    )
    parser.add_argument("--link-semtypes", default="wide")
    parser.add_argument("--window", type=int, default=10)
    return parser.parse_args()


def candidates_of(matcher, text: str, semtypes: frozenset[str]) -> list[dict]:
    """Flat candidate list for ``text`` filtered by semantic type."""
    flat = []
    for group in matcher.match(text, best_match=False, ignore_syntax=False):
        for candidate in group:
            if semtypes and not semtypes.intersection(candidate.get("semtypes", ())):
                continue
            flat.append(candidate)
    return flat


def compact(candidate: dict, field: str, offset: int = 0) -> dict:
    return {
        "field": field,
        "text": " ".join(str(candidate["ngram"]).split()),
        "start": offset + int(candidate["start"]),
        "end": offset + int(candidate["end"]),
        "cui": str(candidate["cui"]),
        "term": str(candidate.get("term", "")),
        "semtypes": sorted(str(s) for s in candidate.get("semtypes", ())),
        "score": round(float(candidate["similarity"]), 4),
    }


def main() -> int:
    args = parse_args()
    os.environ.setdefault("NLTK_DATA", str(args.nltk_data.resolve()))
    cases = list(read_jsonl(args.out / "cases.jsonl"))
    if not cases:
        raise SystemExit(f"No cases.jsonl in {args.out}; run extract_gliner.py first.")
    mentions = {
        row["case_id"]: row["mentions"]
        for row in read_jsonl(args.out / "mentions_gliner.jsonl")
    }
    raw_path = args.out / "quickumls_raw.jsonl"
    if removed := repair_jsonl(raw_path):
        print(f"repaired {raw_path}: dropped {removed} damaged bytes", flush=True)
    done = {row["case_id"] for row in read_jsonl(raw_path)}
    todo = [c for c in cases if c["case_id"] not in done]
    print(f"{len(cases)} cases | {len(done)} done | {len(todo)} to run", flush=True)
    if not todo:
        return 0

    from quickumls import QuickUMLS  # late: slow import, needs the UMLS venv

    text_semtypes = parse_semtypes(args.semtypes)
    link_semtypes = parse_semtypes(args.link_semtypes)

    def build(threshold: float) -> QuickUMLS:
        return QuickUMLS(
            str(args.index),
            overlapping_criteria="score",
            threshold=threshold,
            similarity_name="jaccard",
            window=args.window,
        )

    text_matcher = build(args.threshold)
    link_matcher = (
        text_matcher
        if args.link_threshold == args.threshold
        else build(args.link_threshold)
    )

    started = time.time()
    for number, case in enumerate(todo, start=1):
        text_matches = []
        for field, text in case["fields"].items():
            spans = resolve_spans(candidates_of(text_matcher, text, text_semtypes))
            text_matches.extend(compact(span, field) for span in spans)

        mention_links = []
        for mention in mentions.get(case["case_id"], []):
            options = candidates_of(link_matcher, mention["text"], link_semtypes)
            # Accept a candidate only if it covers most of the mention's characters.
            width = max(1, len(mention["text"]))
            options = [
                o for o in options if (int(o["end"]) - int(o["start"])) >= 0.6 * width
            ]
            if not options:
                continue
            best = max(
                options,
                key=lambda o: (float(o["similarity"]), int(o["end"]) - int(o["start"])),
            )
            link = compact(best, mention["field"])
            link.update(
                {
                    "text": mention["text"],
                    "start": mention["start"],
                    "end": mention["end"],
                    "gliner_label": mention["label"],
                }
            )
            mention_links.append(link)

        append_jsonl(
            raw_path,
            {
                "case_id": case["case_id"],
                "text_matches": text_matches,
                "mention_links": mention_links,
            },
        )
        if number % 5 == 0 or number == len(todo):
            rate = number / (time.time() - started)
            print(
                f"  [{number}/{len(todo)}] {rate:.2f} cases/s "
                f"| ~{(len(todo) - number) / rate / 60:.1f} min left",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
