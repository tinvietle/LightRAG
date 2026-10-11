#!/usr/bin/env python3
"""Compare two retrieval artifacts on the same test cases, scored identically.

Typical use: LightRAG (graph RAG, rerank, NER hints) vs. the plain FAISS
baseline in simple_rag.py, both limited to the same number of chunks. Both
artifacts go through lightrag/evaluation/eval_saved_contexts.py, so a "hit"
means the same thing for each: a retrieved source case whose diagnosis label
equals the test case's ground truth after normalisation.

Reported: strict retrieval metrics, group-level hits, a rank-weighted vote of
the retrieved cases' labels (what a no-LLM classifier could answer), hit rate
by how many training cases the true disease has, and a paired bootstrap 95%
confidence interval for every difference (same cases, resampled together).

    python next_step/simple_rag/compare_retrieval.py \
        --lightrag lightrag/evaluation/results/rag_split_contexts.json \
        --simple   lightrag/evaluation/results/simple_rag_old_split.json \
        --corpus   /path/to/structured_rag_split/train \
        --output   lightrag/evaluation/results/compare_old_split.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
K_VALUES = (1, 3, 5, 10)
BOOTSTRAP_SAMPLES = 5000
SIZE_BUCKETS = ((0, 4), (5, 9), (10, 19), (20, 10**9))


def load_scorer() -> Any:
    path = REPO_ROOT / "lightrag/evaluation/eval_saved_contexts.py"
    spec = importlib.util.spec_from_file_location("eval_saved_contexts", path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["eval_saved_contexts"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def first_rank(items: list[str], target: str, normalize: Any) -> int | None:
    wanted = normalize(target)
    for position, item in enumerate(items, start=1):
        if normalize(item) == wanted:
            return position
    return None


def vote_ranking(labels: list[str], normalize: Any) -> list[str]:
    """Rank labels by the sum of 1/rank over the retrieved cases that carry them."""
    scores: Counter[str] = Counter()
    for position, label in enumerate(labels, start=1):
        if label.strip():
            scores[normalize(label)] += 1.0 / position
    return [label for label, _ in scores.most_common()]


def score_artifact(
    path: Path, corpus: Path, groups: dict[str, str], scorer: Any
) -> dict[str, dict]:
    artifact = json.loads(path.read_text(encoding="utf-8"))
    report = scorer.evaluate(artifact, scorer.load_corpus_label_counts(corpus))
    normalize = scorer.normalize_label
    rows: dict[str, dict] = {}
    for case in report["per_case"]:
        name = case["file_name"]
        retrieved = case["retrieved_source_labels"]
        specific = [r["extracted_disease_name"] for r in retrieved]
        grouped = [r["grouped_disease_name"] for r in retrieved]
        truth_group = groups[name]
        specific_votes = vote_ranking(specific, normalize)
        group_votes = vote_ranking(grouped, normalize)
        rows[name] = {
            "truth": case["ground_truth"],
            "corpus_docs": case["ground_truth_documents_in_corpus"],
            "chunks": len(retrieved),
            "retrieval_rank": case["retrieval_ground_truth_rank"],
            "group_rank": first_rank(grouped, truth_group, normalize),
            "vote_specific_rank": first_rank(
                specific_votes, case["ground_truth"], normalize
            ),
            "vote_group_rank": first_rank(group_votes, truth_group, normalize),
        }
    return rows


def hit(ranks: np.ndarray, k: int) -> np.ndarray:
    return np.array([r is not None and r <= k for r in ranks], dtype=float)


def reciprocal(ranks: np.ndarray, cap: int) -> np.ndarray:
    return np.array([1.0 / r if r is not None and r <= cap else 0.0 for r in ranks])


def paired_ci(
    a: np.ndarray, b: np.ndarray, rng: np.random.Generator
) -> tuple[float, float, float]:
    """Mean of (b - a) with a 95% paired-bootstrap interval."""
    diff = b - a
    draws = rng.integers(0, len(diff), size=(BOOTSTRAP_SAMPLES, len(diff)))
    means = diff[draws].mean(axis=1)
    return (
        float(diff.mean()),
        float(np.percentile(means, 2.5)),
        float(np.percentile(means, 97.5)),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--lightrag", type=Path, required=True)
    parser.add_argument("--simple", type=Path, required=True)
    parser.add_argument(
        "--corpus", type=Path, required=True, help="Train folder (indexed cases)."
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    scorer = load_scorer()
    simple_artifact = json.loads(args.simple.read_text(encoding="utf-8"))
    groups = {
        r["file_name"]: r["ground_truth_group"] for r in simple_artifact["results"]
    }
    light = score_artifact(args.lightrag, args.corpus, groups, scorer)
    simple = score_artifact(args.simple, args.corpus, groups, scorer)

    shared = sorted(set(light) & set(simple))
    mismatched = [n for n in shared if light[n]["truth"] != simple[n]["truth"]]
    if mismatched:
        raise SystemExit(
            f"Ground truth differs between artifacts for {len(mismatched)} cases, e.g. {mismatched[:3]}"
        )
    print(
        f"cases: lightrag {len(light)} | simple {len(simple)} | shared {len(shared)} | "
        f"chunks/query: lightrag {Counter(light[n]['chunks'] for n in shared).most_common(2)} "
        f"simple {Counter(simple[n]['chunks'] for n in shared).most_common(2)}"
    )

    def ranks(rows: dict[str, dict], key: str) -> np.ndarray:
        return np.array([rows[n][key] for n in shared], dtype=object)

    rng = np.random.default_rng(args.seed)
    report: dict[str, Any] = {"cases": len(shared), "metrics": {}}

    def line(title: str, key: str, ks: tuple[int, ...], mrr_cap: int | None) -> None:
        a, b = ranks(light, key), ranks(simple, key)
        print(
            f"\n{title}\n{'':26s}{'lightrag':>9} {'simple':>8}   simple - lightrag  [95% paired CI]"
        )
        section = report["metrics"].setdefault(key, {})
        for k in ks:
            ha, hb = hit(a, k), hit(b, k)
            d, lo, hi = paired_ci(ha, hb, rng)
            flag = "  *" if lo > 0 or hi < 0 else ""
            print(
                f"  hit@{k:<2d}                  {ha.mean():9.3f} {hb.mean():8.3f}   {d:+.3f}  [{lo:+.3f}, {hi:+.3f}]{flag}"
            )
            section[f"hit_at_{k}"] = {
                "lightrag": ha.mean(),
                "simple": hb.mean(),
                "diff": d,
                "ci": [lo, hi],
            }
        if mrr_cap:
            ra, rb = reciprocal(a, mrr_cap), reciprocal(b, mrr_cap)
            d, lo, hi = paired_ci(ra, rb, rng)
            flag = "  *" if lo > 0 or hi < 0 else ""
            print(
                f"  MRR@{mrr_cap:<2d}                 {ra.mean():9.3f} {rb.mean():8.3f}   {d:+.3f}  [{lo:+.3f}, {hi:+.3f}]{flag}"
            )
            section[f"mrr_at_{mrr_cap}"] = {
                "lightrag": ra.mean(),
                "simple": rb.mean(),
                "diff": d,
                "ci": [lo, hi],
            }

    line(
        "Retrieval: true SPECIFIC disease among the retrieved cases",
        "retrieval_rank",
        K_VALUES,
        10,
    )
    line("Retrieval: true GROUP among the retrieved cases", "group_rank", K_VALUES, 10)
    line(
        "No-LLM vote over retrieved labels: SPECIFIC disease in vote top-k",
        "vote_specific_rank",
        (1, 3, 5),
        5,
    )
    line(
        "No-LLM vote over retrieved labels: GROUP in vote top-k",
        "vote_group_rank",
        (1, 3, 5),
        5,
    )

    a, b = (
        hit(ranks(light, "retrieval_rank"), 10),
        hit(ranks(simple, "retrieval_rank"), 10),
    )
    overlap = {
        "both": int(((a == 1) & (b == 1)).sum()),
        "simple_only": int(((a == 0) & (b == 1)).sum()),
        "lightrag_only": int(((a == 1) & (b == 0)).sum()),
        "neither": int(((a == 0) & (b == 0)).sum()),
    }
    print(f"\nhit@10 overlap per case: {overlap}")
    report["hit_at_10_overlap"] = overlap

    print("\nhit@10 by how many training cases the true disease has")
    print(f"{'train docs':>11} {'cases':>6} {'lightrag':>9} {'simple':>8}")
    report["by_train_docs"] = []
    docs = np.array([light[n]["corpus_docs"] for n in shared])
    for lo, hi in SIZE_BUCKETS:
        mask = (docs >= lo) & (docs <= hi)
        if not mask.any():
            continue
        name = f"{lo}-{hi}" if hi < 10**9 else f"{lo}+"
        print(
            f"{name:>11} {int(mask.sum()):6d} {a[mask].mean():9.3f} {b[mask].mean():8.3f}"
        )
        report["by_train_docs"].append(
            {
                "bucket": name,
                "cases": int(mask.sum()),
                "lightrag": a[mask].mean(),
                "simple": b[mask].mean(),
            }
        )
    print("\n* = 95% interval excludes 0 (difference unlikely to be noise)")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, default=float), encoding="utf-8"
    )
    print(f"\nwritten {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
