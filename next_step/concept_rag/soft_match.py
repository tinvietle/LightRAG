#!/usr/bin/env python3
"""Per-keyword soft matching of concepts, with and without the exact match.

Each distinct concept name is embedded once. A concept's near-neighbours are the other
pool concepts whose name vector has cosine >= tau (present findings only match present
findings, absent only absent). Three arms are compared:

  exact      only the identical concept counts        (the plain concept-ID lane)
  soft_only  only near-neighbours count, never the identical concept
  hybrid     the exact match plus near-neighbours

The soft arms also use the EXPANDED support of a concept (cases holding it or a
neighbour) for its rarity weight, so a concept that only looks rare because other cases
word it differently is not over-weighted.

tau is chosen on held-out TRAIN cases (leave-one-out inside the train pool) and then
fixed; the test set is scored once, at the chosen tau.

    next_step/simple_rag/.venv/bin/python next_step/concept_rag/soft_match.py \
        --out artifacts/concept_rag/v2
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "simple_rag"))

import faiss  # noqa: E402
import numpy as np  # noqa: E402
from common import stratified_sample  # noqa: E402
from evaluate_concepts import (  # noqa: E402
    KS,
    bootstrap_ci,
    case_metrics,
    load_arm,
    paired_difference_ci,
)
from simple_rag import CachedEmbedder  # noqa: E402
from soft_scoring import MODES, SoftIndex  # noqa: E402

METRICS = [f"hit@{k}" for k in KS] + ["mrr", "vote@1", "vote@5", "rel@10", "np@10"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--snomed-db", type=Path, default=Path("/home/tin/umls/snomed_index.sqlite")
    )
    parser.add_argument("--model", default="qwen3-embedding:0.6b")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument(
        "--cache", type=Path, default=Path("artifacts/simple_rag/embeddings.sqlite")
    )
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument(
        "--taus", type=float, nargs="+", default=[0.75, 0.80, 0.85, 0.90, 0.95]
    )
    parser.add_argument("--max-neighbours", type=int, default=5)
    parser.add_argument(
        "--val-size", type=int, default=500, help="Train cases used to pick tau."
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--select-by",
        default="mrr",
        choices=("mrr", "hit@10", "vote@5"),
        help="Specific-disease metric on the validation cases that picks tau.",
    )
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument(
        "--negation", choices=("mark", "drop", "ignore"), default="mark"
    )
    parser.add_argument("--report", type=Path, default=None)
    return parser.parse_args()


def build_neighbours(
    vocab: list[str],
    labels: dict[str, str],
    pool_vocab: set[str],
    vectors: dict[str, np.ndarray],
    floor: float,
    depth: int,
) -> dict[str, list[tuple[str, float]]]:
    """For every vocab feature, its most similar POOL features of the same polarity."""
    neighbours: dict[str, list[tuple[str, float]]] = {f: [] for f in vocab}
    for negated in (False, True):
        targets = sorted(f for f in pool_vocab if f.startswith("NEG:") == negated)
        sources = [f for f in vocab if f.startswith("NEG:") == negated]
        if not targets or not sources:
            continue
        matrix = np.stack([vectors[labels[f]] for f in targets]).astype(np.float32)
        index = faiss.IndexFlatIP(matrix.shape[1])
        index.add(matrix)
        queries = np.stack([vectors[labels[f]] for f in sources]).astype(np.float32)
        sims, ids = index.search(queries, min(depth + 1, len(targets)))
        for feature, row_sims, row_ids in zip(sources, sims, ids):
            neighbours[feature] = [
                (targets[i], float(s))
                for s, i in zip(row_sims, row_ids)
                if i >= 0 and targets[i] != feature and s >= floor
            ]
    return neighbours


def evaluate_arm(
    index: SoftIndex,
    mode: str,
    queries: list[dict[str, Any]],
    feats: dict[str, set[str]],
    pool: list[dict[str, Any]],
    position: dict[str, int] | None,
    k: int,
) -> dict[str, dict[str, list[float]]]:
    """Per-case metric columns by label level (only queries with a same-label pool case)."""
    columns: dict[str, dict[str, list[float]]] = {}
    for label_key in ("specific", "group"):
        counts = Counter(c[label_key] for c in pool)
        rows: list[dict[str, float]] = []
        for query in queries:
            truth = query[label_key]
            own = position.get(query["case_id"]) if position is not None else None
            available = counts[truth] - (1 if own is not None else 0)
            if not truth or available <= 0:
                continue
            ranked = index.score(feats[query["case_id"]], mode, exclude=own)[:k]
            rows.append(
                case_metrics([pool[d][label_key] for d, _ in ranked], truth, available)
            )
        columns[label_key] = {m: [r[m] for r in rows] for m in METRICS}
    return columns


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def main() -> int:
    args = parse_args()
    usable, feats, clean, mode = load_arm(
        args.out, "quickumls", args.snomed_db, args.negation
    )
    if mode != "traintest":
        raise SystemExit("soft_match needs both train and test cases in --out")
    pool = [c for c in usable if c["split"] == "train"]
    tests = [c for c in usable if c["split"] == "test"]
    position = {c["case_id"]: i for i, c in enumerate(pool)}
    pool_sets = [feats[c["case_id"]] for c in pool]
    vocab = sorted({f for s in feats.values() for f in s})
    pool_vocab = {f for s in pool_sets for f in s}
    labels = {f: clean.get(f) or f.removeprefix("NEG:") for f in vocab}
    print(
        f"{len(pool)} train | {len(tests)} test | {len(vocab)} distinct concepts "
        f"({len(pool_vocab)} in train) | {len(set(labels.values()))} distinct names",
        flush=True,
    )

    embedder = CachedEmbedder(args.model, args.ollama_url, args.cache, args.batch_size)
    unique = sorted(set(labels.values()))
    started = time.time()
    matrix = embedder.embed(unique, "concept-names")
    vectors = dict(zip(unique, matrix))
    print(f"embedded {len(unique)} names in {time.time() - started:.0f}s", flush=True)

    floor = min(args.taus)
    neighbours = build_neighbours(vocab, labels, pool_vocab, vectors, floor, depth=9)
    with_nbr = sum(1 for f in vocab if neighbours[f])
    print(
        f"{with_nbr}/{len(vocab)} concepts have a neighbour at cosine >= {floor}",
        flush=True,
    )

    # ------------------------------------------------------------ pick tau (train only)
    val = stratified_sample(pool, args.val_size, args.seed)
    print(
        f"\nValidation: leave-one-out on {len(val)} train cases, metric = specific {args.select_by}"
    )
    indexes = {
        t: SoftIndex(pool_sets, neighbours, t, args.max_neighbours) for t in args.taus
    }
    base = evaluate_arm(
        indexes[args.taus[0]], "exact", val, feats, pool, position, args.k
    )
    base_value = mean(base["specific"][args.select_by])
    print(
        f"  exact (no tau): {base_value:.3f}   n={len(base['specific'][args.select_by])}"
    )
    sweep: dict[str, dict[str, float]] = {}
    for tau in args.taus:
        row = {}
        for soft_mode in ("soft_only", "hybrid"):
            columns = evaluate_arm(
                indexes[tau], soft_mode, val, feats, pool, position, args.k
            )
            row[soft_mode] = round(mean(columns["specific"][args.select_by]), 4)
        sweep[str(tau)] = row
        print(
            f"  tau={tau:.2f}  soft_only={row['soft_only']:.3f}  hybrid={row['hybrid']:.3f}",
            flush=True,
        )
    chosen = {
        m: max(args.taus, key=lambda t: (sweep[str(t)][m], -t))
        for m in ("soft_only", "hybrid")
    }
    print(f"chosen tau (fixed from here): {chosen}")

    # ------------------------------------------------------- test, scored once
    results: dict[str, dict[str, dict[str, list[float]]]] = {}
    test_index = {t: indexes[t] for t in set(chosen.values())}
    arms = {
        "exact": (indexes[args.taus[0]], "exact"),
        "soft_only": (test_index[chosen["soft_only"]], "soft_only"),
        "hybrid": (test_index[chosen["hybrid"]], "hybrid"),
    }
    for name, (index, soft_mode) in arms.items():
        results[name] = evaluate_arm(index, soft_mode, tests, feats, pool, None, args.k)

    report: dict[str, Any] = {
        "tau_validation": sweep,
        "chosen_tau": chosen,
        "select_by": args.select_by,
        "test": {},
        "paired_vs_exact": {},
    }
    print(f"\nTEST ({len(tests)} cases, pool = {len(pool)} train)")
    for label_key in ("specific", "group"):
        print(f"[{label_key}]")
        for name in MODES:
            cols = results[name][label_key]
            summary = {m: [round(v, 3) for v in bootstrap_ci(cols[m])] for m in METRICS}
            report["test"].setdefault(name, {})[label_key] = {
                "n": len(cols["mrr"]),
                **summary,
            }
            print(
                "  "
                + f"{name:10s} "
                + "  ".join(f"{m}={summary[m][0]:.3f}" for m in METRICS)
            )
        for name in ("soft_only", "hybrid"):
            parts = []
            for metric in ("hit@10", "vote@5", "vote@1", "mrr", "rel@10"):
                a, b = (
                    results["exact"][label_key][metric],
                    results[name][label_key][metric],
                )
                diff, low, high = paired_difference_ci(a, b)
                report["paired_vs_exact"].setdefault(f"{name}-exact", {}).setdefault(
                    label_key, {}
                )[metric] = [round(diff, 3), round(low, 3), round(high, 3)]
                parts.append(f"{metric} {diff:+.3f} [{low:+.3f},{high:+.3f}]")
            print(f"  {name} - exact:  " + "   ".join(parts))

    reference = args.out / "fusion_quickumls.json"
    if reference.exists():
        lane_a = json.loads(reference.read_text())["results"]["A"]
        print(
            "\nreference, plain case-text FAISS (lane A, same test cases): "
            + "  ".join(
                f"{label_key}: hit@10={lane_a[label_key]['hit@10'][0]:.3f} vote@5={lane_a[label_key]['vote@5'][0]:.3f}"
                for label_key in ("specific", "group")
            )
        )

    # ------------- are rare concepts really rare? (support before/after expansion)
    index = test_index[chosen["hybrid"]]
    rare = [f for f in pool_vocab if 1 <= index.df(f, False) <= 2]
    soft_df = [index.df(f, True) for f in rare]
    exact_df = [index.df(f, False) for f in rare]
    grew = sum(1 for e, s in zip(exact_df, soft_df) if s > e)
    report["rare_support"] = {
        "rare_concepts_df<=2": len(rare),
        "share_with_more_support_after_expansion": round(grew / max(1, len(rare)), 3),
        "median_df_before": statistics.median(exact_df) if rare else 0,
        "median_df_after": statistics.median(soft_df) if rare else 0,
        "share_with_df_after>=5": round(
            sum(1 for s in soft_df if s >= 5) / max(1, len(rare)), 3
        ),
    }
    print(
        f"\nRare concepts (in 1-2 train cases) at tau={chosen['hybrid']}: {report['rare_support']}"
    )
    print("examples (concept -> neighbours with cosine, support before -> after):")
    shown = 0
    for f in sorted(rare):
        near = index.neighbours_of(f)
        if near and shown < 12:
            shown += 1
            text = "; ".join(f"{labels[g]} ({s:.2f})" for g, s in near[:3])
            print(
                f"  {labels[f]!r}: {index.df(f, False)} -> {index.df(f, True)}   ~ {text}"
            )

    path = args.report or (args.out / "soft_match_quickumls.json")
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nreport -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
