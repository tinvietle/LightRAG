#!/usr/bin/env python3
"""Score one retrieval artifact (no baseline needed).

Uses the same scorer and definitions as compare_retrieval.py: a hit means a
retrieved case's diagnosis label equals the test case's, after normalisation.
Reports specific-disease and group-level hit rates, a rank-weighted vote over
the retrieved cases' labels (a no-LLM classifier), and hit@k by how many
training cases the true disease has. Intervals are bootstrap 95% over cases.

    python next_step/simple_rag/evaluate_retrieval.py \
        --artifact lightrag/evaluation/results/simple_rag_v2.json \
        --corpus   structured_rag_split_v2/train \
        --output   lightrag/evaluation/results/eval_simple_rag_v2.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compare_retrieval import (  # noqa: E402
    BOOTSTRAP_SAMPLES,
    SIZE_BUCKETS,
    hit,
    load_scorer,
    reciprocal,
    score_artifact,
)


def interval(values: np.ndarray, rng: np.random.Generator) -> tuple[float, float]:
    draws = rng.integers(0, len(values), size=(BOOTSTRAP_SAMPLES, len(values)))
    means = values[draws].mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True, help="Train folder.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    scorer = load_scorer()
    artifact = json.loads(args.artifact.read_text(encoding="utf-8"))
    groups = {r["file_name"]: r["ground_truth_group"] for r in artifact["results"]}
    rows = score_artifact(args.artifact, args.corpus, groups, scorer)
    names = sorted(rows)
    rng = np.random.default_rng(args.seed)
    report: dict = {
        "artifact": str(args.artifact),
        "cases": len(names),
        "chunks_per_query": sorted({rows[n]["chunks"] for n in names}),
        "metrics": {},
    }

    def ranks(key: str) -> np.ndarray:
        return np.array([rows[n][key] for n in names], dtype=object)

    sections = (
        (
            "retrieval_rank",
            "Retrieval: true SPECIFIC disease among retrieved cases",
            (1, 3, 5, 10),
            10,
        ),
        (
            "group_rank",
            "Retrieval: true GROUP among retrieved cases",
            (1, 3, 5, 10),
            10,
        ),
        ("vote_specific_rank", "Label vote: SPECIFIC disease in top-k", (1, 3, 5), 5),
        ("vote_group_rank", "Label vote: GROUP in top-k", (1, 3, 5), 5),
    )
    for key, title, ks, cap in sections:
        print(f"\n{title}  ({len(names)} cases)")
        section = report["metrics"][key] = {}
        for k in ks:
            values = hit(ranks(key), k)
            low, high = interval(values, rng)
            print(f"  hit@{k:<2d} {values.mean():.3f}  [{low:.3f}, {high:.3f}]")
            section[f"hit_at_{k}"] = {"value": values.mean(), "ci": [low, high]}
        mrr = reciprocal(ranks(key), cap)
        low, high = interval(mrr, rng)
        print(f"  MRR@{cap:<2d} {mrr.mean():.3f}  [{low:.3f}, {high:.3f}]")
        section[f"mrr_at_{cap}"] = {"value": mrr.mean(), "ci": [low, high]}

    docs = np.array([rows[n]["corpus_docs"] for n in names])
    found = hit(ranks("retrieval_rank"), 10)
    print("\nSpecific-disease hit@10 by training cases of the true disease")
    report["by_train_docs"] = []
    for low, high in SIZE_BUCKETS:
        mask = (docs >= low) & (docs <= high)
        if mask.any():
            name = f"{low}-{high}" if high < 10**9 else f"{low}+"
            print(f"  {name:>6s}: {int(mask.sum()):4d} cases  {found[mask].mean():.3f}")
            report["by_train_docs"].append(
                {
                    "bucket": name,
                    "cases": int(mask.sum()),
                    "hit_at_10": found[mask].mean(),
                }
            )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, default=float), encoding="utf-8"
    )
    print(f"\nwritten {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
