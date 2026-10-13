#!/usr/bin/env python3
"""Stage 1 (GLiNER environment): case manifest + GLiNER mentions.

Writes, under ``--out``:
  cases.jsonl           selected cases with per-field text and labels
  mentions_gliner.jsonl one row per case: mentions found by GLiNER-biomed

Run it with the project's main environment (the one that has ``gliner``):

    .venv/bin/python next_step/concept_rag/extract_gliner.py \
        --dataset structured_rag_split_v2 --splits train --sample 50 \
        --out artifacts/concept_rag/demo50

A re-run resumes: cases already present in ``mentions_gliner.jsonl`` are skipped.
Use a fresh ``--out`` when the case selection changes.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    append_jsonl,
    chunk_text,
    load_cases,
    read_jsonl,
    repair_jsonl,
    stratified_sample,
    write_jsonl,
)

MODEL_NAME = "Ihor/gliner-biomed-base-v1.0"
# Entity types of the project's extraction prompt, minus the two that are not
# concepts (dates, bare lab values). Underscores read better to GLiNER as spaces.
DEFAULT_LABELS = (
    "Disease_disorder",
    "Pathogen",
    "Medication",
    "Anatomical_location",
    "Diagnostic_procedure",
    "Therapeutic_procedure",
    "Biological_structure",
    "Clinical_event",
    "Organism",
    "Sign_symptom",
    "Lab_test",
    "Transmission_vector",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--splits", nargs="+", default=["train"])
    parser.add_argument("--sample", type=int, default=None, help="Stratified N cases.")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--device", default="cpu", help="cpu or cuda")
    parser.add_argument("--max-words", type=int, default=200, help="Chunk size.")
    parser.add_argument("--model-dir", type=Path, default=Path("data/ner_model"))
    parser.add_argument("--labels", nargs="+", default=list(DEFAULT_LABELS))
    parser.add_argument(
        "--cases-only",
        action="store_true",
        help="Write cases.jsonl and stop (no GLiNER); enough for QuickUMLS-only runs.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cases = [c for split in args.splits for c in load_cases(args.dataset, split)]
    if args.sample:
        cases = stratified_sample(cases, args.sample, args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    write_jsonl(args.out / "cases.jsonl", cases)
    if args.cases_only:
        print(
            f"wrote {len(cases)} cases to {args.out / 'cases.jsonl'} (GLiNER skipped)"
        )
        return 0

    mentions_path = args.out / "mentions_gliner.jsonl"
    if removed := repair_jsonl(mentions_path):
        print(f"repaired {mentions_path}: dropped {removed} damaged bytes", flush=True)
    done = {row["case_id"] for row in read_jsonl(mentions_path)}
    todo = [c for c in cases if c["case_id"] not in done]
    print(f"{len(cases)} cases | {len(done)} done | {len(todo)} to run", flush=True)
    if not todo:
        return 0

    from gliner import GLiNER  # late: slow import

    model = GLiNER.from_pretrained(MODEL_NAME, cache_dir=str(args.model_dir))
    if args.device != "cpu":
        model = model.to(args.device)
    labels = [label.replace("_", " ") for label in args.labels]
    label_back = dict(zip(labels, args.labels))

    started = time.time()
    for number, case in enumerate(todo, start=1):
        mentions = []
        for field, text in case["fields"].items():
            for offset, chunk in chunk_text(text, args.max_words):
                for hit in model.predict_entities(
                    chunk, labels, flat_ner=True, threshold=args.threshold
                ):
                    mentions.append(
                        {
                            "field": field,
                            "text": hit["text"],
                            "start": offset + int(hit["start"]),
                            "end": offset + int(hit["end"]),
                            "label": label_back.get(hit["label"], hit["label"]),
                            "score": round(float(hit["score"]), 4),
                        }
                    )
        append_jsonl(mentions_path, {"case_id": case["case_id"], "mentions": mentions})
        if number % 10 == 0 or number == len(todo):
            rate = number / (time.time() - started)
            print(
                f"  [{number}/{len(todo)}] {rate:.2f} cases/s "
                f"| ~{(len(todo) - number) / rate / 60:.1f} min left",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
