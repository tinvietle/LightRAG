#!/usr/bin/env python3
"""How many of the retrieved cases carry the right disease? (relevant count in the top k)

hit@k only says whether AT LEAST ONE retrieved case has the true disease. This reports
the average NUMBER of them, plus the share of queries with 0, 1, 2 or 3+ right cases,
for any number of retrieval artifacts scored the same way (the scorer of
lightrag/evaluation/eval_saved_contexts.py, so a case is "relevant" when its diagnosis
label equals the ground truth after normalisation).

The first artifact is the baseline for paired bootstrap differences.

    python next_step/simple_rag/relevant_count.py \
        --artifact lightrag=lightrag/evaluation/results/rag_split_contexts.json \
        --artifact faiss=lightrag/evaluation/results/simple_rag_old_split.json \
        --label-counts artifacts/old_split_label_counts.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compare_retrieval import BOOTSTRAP_SAMPLES, load_scorer  # noqa: E402


def count_relevant(labels: list[str], truth: str, normalize: Any, k: int) -> int:
    """How many of the first ``k`` labels equal the truth after normalisation."""
    wanted = normalize(truth)
    if not wanted:
        return 0
    return sum(1 for label in labels[:k] if normalize(label) == wanted)


def score(
    path: Path, counts: Counter[str], groups: dict[str, str], scorer: Any, k: int
) -> dict[str, dict[str, Any]]:
    artifact = json.loads(path.read_text(encoding="utf-8"))
    report = scorer.evaluate(artifact, counts)
    normalize = scorer.normalize_label
    rows: dict[str, dict[str, Any]] = {}
    for case in report["per_case"]:
        retrieved = case["retrieved_source_labels"]
        name = case["file_name"]
        in_corpus = case["ground_truth_documents_in_corpus"]
        rows[name] = {
            "chunks": len(retrieved),
            "in_corpus": in_corpus,
            "rel_specific": count_relevant(
                [r["extracted_disease_name"] for r in retrieved],
                case["ground_truth"],
                normalize,
                k,
            ),
            "rel_group": count_relevant(
                [r["grouped_disease_name"] for r in retrieved],
                groups.get(name, ""),
                normalize,
                k,
            ),
        }
    return rows


def paired(a: np.ndarray, b: np.ndarray, seed: int) -> tuple[float, float, float]:
    rng = np.random.default_rng(seed)
    diff = b - a
    means = diff[rng.integers(0, len(diff), size=(BOOTSTRAP_SAMPLES, len(diff)))].mean(
        axis=1
    )
    return (
        float(diff.mean()),
        float(np.percentile(means, 2.5)),
        float(np.percentile(means, 97.5)),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--artifact", action="append", required=True, help="name=path")
    parser.add_argument(
        "--label-counts", type=Path, required=True, help="JSON {label: n_train_cases}"
    )
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    scorer = load_scorer()
    counts: Counter[str] = Counter(
        json.loads(args.label_counts.read_text(encoding="utf-8"))
    )
    named = [item.split("=", 1) for item in args.artifact]
    # Not every artifact carries the group label (LightRAG's does not), so merge them all.
    groups: dict[str, str] = {}
    for _, path in named:
        for row in json.loads(Path(path).read_text(encoding="utf-8"))["results"]:
            if row.get("ground_truth_group"):
                groups.setdefault(row["file_name"], row["ground_truth_group"])
    if not groups:
        raise SystemExit(
            "None of the artifacts has ground_truth_group; cannot score groups"
        )

    scored = {
        name: score(Path(path), counts, groups, scorer, args.k) for name, path in named
    }
    shared = sorted(set.intersection(*(set(rows) for rows in scored.values())))
    shared = [n for n in shared if scored[named[0][0]][n]["in_corpus"] > 0]
    print(
        f"{len(shared)} test cases shared by all artifacts whose disease is in the training corpus\n"
    )

    report: dict[str, Any] = {
        "cases": len(shared),
        "k": args.k,
        "artifacts": {},
        "paired_vs_first": {},
    }
    columns: dict[str, dict[str, np.ndarray]] = {}
    for name, _ in named:
        rows = scored[name]
        spec = np.array([rows[n]["rel_specific"] for n in shared], dtype=float)
        grp = np.array([rows[n]["rel_group"] for n in shared], dtype=float)
        ceiling = np.array(
            [min(args.k, rows[n]["in_corpus"]) for n in shared], dtype=float
        )
        chunks = np.array([rows[n]["chunks"] for n in shared], dtype=float)
        columns[name] = {"specific": spec, "group": grp}
        dist = Counter(min(int(v), 3) for v in spec)
        entry = {
            "chunks_per_query": round(float(chunks.mean()), 2),
            "relevant_specific_mean": round(float(spec.mean()), 3),
            "relevant_group_mean": round(float(grp.mean()), 3),
            "share_of_ceiling_specific": round(float((spec / ceiling).mean()), 3),
            "specific_share_with_0_1_2_3plus": [
                round(dist[i] / len(shared), 3) for i in range(4)
            ],
        }
        report["artifacts"][name] = entry
        print(f"{name}: {entry}")
    base = named[0][0]
    for name, _ in named[1:]:
        for label in ("specific", "group"):
            mean, low, high = paired(
                columns[base][label], columns[name][label], args.seed
            )
            report["paired_vs_first"][f"{name} - {base} [{label}]"] = [
                round(mean, 3),
                round(low, 3),
                round(high, 3),
            ]
            print(
                f"  {name} - {base} relevant count ({label}): {mean:+.3f} [{low:+.3f}, {high:+.3f}]"
            )
    if args.output:
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nreport -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
