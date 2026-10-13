#!/usr/bin/env python3
"""Compare retrieval lanes and their fusions on the extracted concepts.

Lanes (each returns a ranked list of pool cases):
  A  case-text FAISS     embedding of the case text, the same text simple_rag embeds
                         (its embedding cache is reused, so this costs nothing new)
  B  concept-ID overlap  IDF-weighted overlap of SNOMED/CUI/term features
  C  concept-text FAISS  embedding of the case's concept names ("Findings: ...;
                         Absent: ...") searched with FAISS, so near-synonyms match

Fusions are reciprocal-rank fusion of any subset: A+B, A+C, B+C, A+B+C.

Run it with the simple_rag environment (it has faiss + langchain-ollama):

    next_step/simple_rag/.venv/bin/python next_step/concept_rag/fuse_lanes.py \
        --out artifacts/concept_rag/demo50 --arm quickumls

Needs Ollama with the embedding model (default qwen3-embedding:0.6b). Mode is chosen
as in evaluate_concepts.py: train->test when both splits exist, else leave-one-out.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sqlite3
import statistics
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "simple_rag"))

import faiss  # noqa: E402
import numpy as np  # noqa: E402
from common import CAPTION_FIELD, FIELD_ORDER, read_jsonl  # noqa: E402
from evaluate_concepts import (  # noqa: E402
    KS,
    ConceptIndex,
    bootstrap_ci,
    build_case_features,
    case_metrics,
    chance_hit,
    make_cui_lookup,
    paired_difference_ci,
)
from fusion import concept_text, rrf  # noqa: E402
from simple_rag import CachedEmbedder  # noqa: E402

LANES = ("A", "B", "C")
LANE_NAMES = {
    "A": "case-text FAISS",
    "B": "concept-ID overlap",
    "C": "concept-text FAISS",
}


def case_text(fields: dict[str, str]) -> str:
    """Identical to simple_rag.case_text, so its embedding cache is hit."""
    lines = [
        f"{name.replace('_', ' ').capitalize()}: {fields[name]}"
        for name in FIELD_ORDER
        if fields.get(name)
    ]
    if fields.get(CAPTION_FIELD):
        lines.append(f"Image findings: {fields[CAPTION_FIELD]}")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--snomed-db", type=Path, default=Path("/home/tin/umls/snomed_index.sqlite")
    )
    parser.add_argument(
        "--arm", choices=("gliner", "quickumls", "both"), default="quickumls"
    )
    parser.add_argument(
        "--scorer", choices=("tfidf", "bm25", "jaccard"), default="tfidf"
    )
    parser.add_argument(
        "--negation", choices=("mark", "drop", "ignore"), default="mark"
    )
    parser.add_argument("--min-gliner-score", type=float, default=0.5)
    parser.add_argument("--mode", choices=("auto", "traintest", "loo"), default="auto")
    parser.add_argument("--model", default="qwen3-embedding:0.6b")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument(
        "--cache", type=Path, default=Path("artifacts/simple_rag/embeddings.sqlite")
    )
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument(
        "--max-terms", type=int, default=60, help="Concepts per case text."
    )
    parser.add_argument(
        "--lane-depth", type=int, default=50, help="Candidates per lane before fusion."
    )
    parser.add_argument("--rrf-k", type=int, default=60)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument(
        "--show-text", type=int, default=1, help="Concept texts to print."
    )
    parser.add_argument("--report", type=Path, default=None)
    return parser.parse_args()


def faiss_lane(
    pool_vecs: np.ndarray,
    query_vecs: np.ndarray,
    depth: int,
    excludes: list[int | None],
) -> list[list[int]]:
    index = faiss.IndexFlatIP(pool_vecs.shape[1])
    index.add(pool_vecs)
    _, ids = index.search(query_vecs, min(depth + 1, len(pool_vecs)))
    out = []
    for row, exclude in zip(ids, excludes):
        ranked = [int(i) for i in row if i >= 0 and int(i) != exclude]
        out.append(ranked[:depth])
    return out


def main() -> int:
    args = parse_args()
    cases = list(read_jsonl(args.out / "cases.jsonl"))
    mentions = {
        r["case_id"]: r["mentions"]
        for r in read_jsonl(args.out / "mentions_gliner.jsonl")
    }
    raw = {r["case_id"]: r for r in read_jsonl(args.out / "quickumls_raw.jsonl")}
    need_g = args.arm in ("gliner", "both")
    need_q = args.arm in ("quickumls", "both")
    usable = [
        c
        for c in cases
        if (not need_g or c["case_id"] in mentions)
        and (not need_q or c["case_id"] in raw)
    ]
    splits = {c["split"] for c in usable}
    mode = (
        args.mode
        if args.mode != "auto"
        else ("traintest" if {"train", "test"} <= splits else "loo")
    )
    print(f"{len(usable)} cases | mode={mode} | concept arm={args.arm}")

    lookup = make_cui_lookup(sqlite3.connect(str(args.snomed_db)))
    clean: dict[str, str] = {}
    feats = {
        c["case_id"]: build_case_features(
            c,
            mentions.get(c["case_id"], []),
            raw.get(c["case_id"]),
            args.arm,
            lookup,
            args.negation,
            args.min_gliner_score,
            None,
            frozenset(),
            clean,
        )
        for c in usable
    }

    if mode == "traintest":
        pool = [c for c in usable if c["split"] == "train"]
        queries = [c for c in usable if c["split"] == "test"]
    else:
        pool, queries = list(usable), list(usable)
    position = {c["case_id"]: i for i, c in enumerate(pool)}
    excludes = [position.get(q["case_id"]) if mode == "loo" else None for q in queries]
    index = ConceptIndex([feats[c["case_id"]] for c in pool])

    # --- texts to embed
    texts_a = {c["case_id"]: case_text(c["fields"]) for c in usable}
    texts_c = {
        c["case_id"]: concept_text(
            feats[c["case_id"]], clean, index.idf, args.max_terms
        )
        for c in usable
    }
    for case in usable[: args.show_text]:
        print(
            f"\nconcept text of {case['case_id']} ({case['specific']}):\n{texts_c[case['case_id']][:900]}\n"
        )

    embedder = CachedEmbedder(args.model, args.ollama_url, args.cache, args.batch_size)
    pool_ids = [c["case_id"] for c in pool]
    query_ids = [q["case_id"] for q in queries]
    vec_a = embedder.embed([texts_a[i] for i in pool_ids + query_ids], "case-text")
    vec_c = embedder.embed([texts_c[i] for i in pool_ids + query_ids], "concept-text")
    pa, qa = vec_a[: len(pool)], vec_a[len(pool) :]
    pc, qc = vec_c[: len(pool)], vec_c[len(pool) :]

    depth = args.lane_depth
    lane_rankings: dict[str, list[list[int]]] = {
        "A": faiss_lane(pa, qa, depth, excludes),
        "C": faiss_lane(pc, qc, depth, excludes),
        "B": [
            [
                d
                for d, _ in index.score(feats[q["case_id"]], args.scorer, exclude=ex)[
                    :depth
                ]
            ]
            for q, ex in zip(queries, excludes)
        ],
    }
    combos: dict[str, list[list[int]]] = {name: lane_rankings[name] for name in LANES}
    for size in (2, 3):
        for subset in itertools.combinations(LANES, size):
            combos["+".join(subset)] = [
                rrf([lane_rankings[lane][i] for lane in subset], args.rrf_k, args.k)
                for i in range(len(queries))
            ]

    report: dict = {
        "mode": mode,
        "arm": args.arm,
        "scorer": args.scorer,
        "cases": len(usable),
        "results": {},
    }
    per_combo: dict[str, dict[str, dict[str, list[float]]]] = {}
    metrics = [f"hit@{k}" for k in KS] + ["mrr", "vote@1", "vote@5", "rel@10", "np@10"]
    for label_key in ("specific", "group"):
        pool_labels = Counter(c[label_key] for c in pool)
        eligible = []
        availability: dict[int, int] = {}
        chance = []
        for i, query in enumerate(queries):
            truth = query[label_key]
            available = pool_labels[truth] - (1 if excludes[i] is not None else 0)
            if truth and available > 0:
                eligible.append(i)
                availability[i] = available
                chance.append(
                    {
                        f"hit@{k}": chance_hit(
                            len(pool) - (excludes[i] is not None), available, k
                        )
                        for k in KS
                    }
                )
        print(
            f"\n[{label_key}] eligible queries: {len(eligible)}/{len(queries)}  "
            f"chance hit@1/5/10 = "
            + "/".join(
                f"{statistics.mean(c[f'hit@{k}'] for c in chance):.3f}" for k in KS
            )
            if chance
            else ""
        )
        for name, rankings in combos.items():
            rows = [
                case_metrics(
                    [pool[d][label_key] for d in rankings[i][: args.k]],
                    queries[i][label_key],
                    availability[i],
                )
                for i in eligible
            ]
            columns = {m: [r[m] for r in rows] for m in metrics}
            per_combo.setdefault(name, {})[label_key] = columns
            summary = {
                m: [round(v, 3) for v in bootstrap_ci(columns[m])] for m in metrics
            }
            report["results"].setdefault(name, {})[label_key] = {
                "n": len(rows),
                **summary,
            }
            line = "  ".join(f"{m}={summary[m][0]:.3f}" for m in metrics)
            print(f"  {name:6s} {line}")

        print(f"  paired differences vs lane A ({label_key}), mean [95% CI]:")
        for name in combos:
            if name == "A":
                continue
            parts = []
            for metric in ("hit@10", "vote@5", "vote@1", "rel@10"):
                a, b = (
                    per_combo["A"][label_key][metric],
                    per_combo[name][label_key][metric],
                )
                if a:
                    mean, low, high = paired_difference_ci(a, b)
                    parts.append(f"{metric} {mean:+.3f} [{low:+.3f},{high:+.3f}]")
            print(f"    {name:6s} " + "   ".join(parts))

    path = args.report or (args.out / f"fusion_{args.arm}.json")
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nreport -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
