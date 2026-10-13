#!/usr/bin/env python3
"""Stage 3: compare GLiNER-only, QuickUMLS-only and combined concept extraction.

Reads ``cases.jsonl``, ``mentions_gliner.jsonl`` and ``quickumls_raw.jsonl`` from
``--out`` and builds one feature set per case for each arm:

  gliner     surface terms of GLiNER mentions (no concept linking)
  quickumls  QuickUMLS matches run directly on the text (SNOMED id, else CUI)
  both       quickumls matches + GLiNER mentions linked to concepts by QuickUMLS
             + the GLiNER mentions that could not be linked, kept as surface terms

Negated findings ("no fever", "denies cough") become separate ``NEG:`` features, are
dropped, or are ignored (``--negation``). Cases are then ranked against each other by
shared features (IDF-weighted cosine, BM25 or Jaccard) and scored by whether the
neighbours carry the query case's disease label.

Two evaluation modes (chosen automatically, or with ``--mode``):
  traintest  index = train cases, queries = test cases (the real experiment)
  loo        leave-one-out inside one pool of cases (a demo on a small sample; the
             numbers are noisy and are NOT an evaluation of the method)

Plain Python, no scipy/numpy: it runs in either environment.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import is_negated, normalize_term, read_jsonl  # noqa: E402

ARMS = ("gliner", "quickumls", "both")
SCORERS = ("tfidf", "bm25", "jaccard")
KS = (1, 5, 10)


# ------------------------------------------------------------------ features


def make_cui_lookup(conn: sqlite3.Connection) -> Callable[[str], tuple[str, str]]:
    """cui -> (feature id, display name): 'S<sctid>' when SNOMED has it, else 'C<cui>'."""
    cache: dict[str, tuple[str, str]] = {}

    def lookup(cui: str) -> tuple[str, str]:
        if cui not in cache:
            rows = conn.execute(
                "SELECT sctid, tty, term FROM snomed WHERE cui = ?", (cui,)
            ).fetchall()
            if rows:
                rows.sort(key=lambda r: (r[1] != "PT", r[0]))
                cache[cui] = (f"S{rows[0][0]}", rows[0][2])
            else:
                cache[cui] = (f"C{cui}", cui)
        return cache[cui]

    return lookup


def _tag(feature: str, negated: bool, negation: str) -> str | None:
    if not negated or negation == "ignore":
        return feature
    if negation == "drop":
        return None
    return f"NEG:{feature}"


_HAS_WORD = re.compile(r"[a-z]{3}")


def is_real_term(text: str) -> bool:
    """Reject matches without a word (QuickUMLS links strings like '<50' to CUIs)."""
    return bool(_HAS_WORD.search(text.lower()))


def _readable(feature: str, display: str, match: dict[str, Any]) -> str:
    """Concept name for embedding: the SNOMED term, else the matched term (never a bare CUI)."""
    if feature.startswith("S"):
        return display
    return str(match.get("term") or match["text"])


def build_case_features(
    case: dict[str, Any],
    mentions: list[dict[str, Any]],
    raw: dict[str, Any] | None,
    arm: str,
    cui_lookup: Callable[[str], tuple[str, str]],
    negation: str = "mark",
    min_gliner_score: float = 0.5,
    names: dict[str, str] | None = None,
    exclude_gliner_labels: frozenset[str] = frozenset(),
    clean_labels: dict[str, str] | None = None,
) -> set[str]:
    """Feature set of one case for one arm.

    ``names`` collects display strings (with the source text, for eyeballing) and
    ``clean_labels`` the bare concept name or surface term of each feature.
    """
    fields = case["fields"]
    features: set[str] = set()

    def add(feature: str, display: str, field: str, start: int, clean: str) -> None:
        tagged = _tag(feature, is_negated(fields.get(field, ""), start), negation)
        if tagged is None:
            return
        features.add(tagged)
        if names is not None:
            names.setdefault(tagged, display)
        if clean_labels is not None:
            clean_labels.setdefault(tagged, clean)

    linked_spans: set[tuple[str, int, int]] = set()
    if arm in ("quickumls", "both") and raw:
        for match in raw.get("text_matches", []):
            if not is_real_term(match["text"]):
                continue
            feature, display = cui_lookup(match["cui"])
            add(
                feature,
                f"{display} <- '{match['text']}'",
                match["field"],
                match["start"],
                _readable(feature, display, match),
            )
    if arm == "both" and raw:
        for link in raw.get("mention_links", []):
            if not is_real_term(link["text"]):
                continue
            feature, display = cui_lookup(link["cui"])
            add(
                feature,
                f"{display} <- '{link['text']}'",
                link["field"],
                link["start"],
                _readable(feature, display, link),
            )
            linked_spans.add((link["field"], link["start"], link["end"]))
    if arm in ("gliner", "both"):
        for mention in mentions:
            if mention["score"] < min_gliner_score:
                continue
            if mention["label"] in exclude_gliner_labels:
                continue
            if (
                arm == "both"
                and (mention["field"], mention["start"], mention["end"]) in linked_spans
            ):
                continue
            term = normalize_term(mention["text"])
            if term:
                add(
                    f"t:{term}",
                    f"'{mention['text']}' ({mention['label']})",
                    mention["field"],
                    mention["start"],
                    term,
                )
    return features


# --------------------------------------------------------------------- index


class ConceptIndex:
    """Inverted index over feature sets with three scorers."""

    def __init__(self, docs: list[set[str]]) -> None:
        self.docs = docs
        self.n = len(docs)
        self.postings: dict[str, list[int]] = defaultdict(list)
        for position, features in enumerate(docs):
            for feature in features:
                self.postings[feature].append(position)
        self.idf = {
            f: math.log((self.n - len(p) + 0.5) / (len(p) + 0.5) + 1.0)
            for f, p in self.postings.items()
        }
        self.sizes = [len(d) for d in docs]
        self.avgdl = (sum(self.sizes) / self.n) if self.n else 0.0
        self.norms = [math.sqrt(sum(self.idf[f] ** 2 for f in d)) or 1.0 for d in docs]

    def score(
        self, query: set[str], scorer: str, exclude: int | None = None
    ) -> list[tuple[int, float]]:
        scores: dict[int, float] = defaultdict(float)
        overlap: dict[int, int] = defaultdict(int)
        k1, b = 1.2, 0.75
        for feature in query:
            for position in self.postings.get(feature, ()):
                if position == exclude:
                    continue
                overlap[position] += 1
                weight = self.idf[feature]
                if scorer == "tfidf":
                    scores[position] += weight * weight  # query and doc weight idf
                elif scorer == "bm25":
                    norm = 1 - b + b * self.sizes[position] / (self.avgdl or 1.0)
                    scores[position] += weight * (k1 + 1) / (1 + k1 * norm)
        if scorer == "tfidf":
            for position in scores:
                scores[position] /= self.norms[position]  # query norm is constant
        elif scorer == "jaccard":
            for position, shared in overlap.items():
                scores[position] = shared / (len(query) + self.sizes[position] - shared)
        return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


# ------------------------------------------------------------------- metrics


def label_vote(neighbour_labels: list[str]) -> list[str]:
    """Labels ordered by rank-weighted vote (weight 1/rank)."""
    weights: dict[str, float] = defaultdict(float)
    for rank, label in enumerate(neighbour_labels, start=1):
        weights[label] += 1.0 / rank
    return [
        label for label, _ in sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def case_metrics(
    neighbour_labels: list[str], truth: str, available: int | None = None
) -> dict[str, float]:
    """Per-case hit@k, reciprocal rank, vote accuracy and relevant-count for one label.

    ``rel@10`` is how many of the top 10 carry the true label. ``np@10`` divides it by
    the best possible count, min(10, ``available``), where ``available`` is the number
    of pool cases with that label, so diseases with few training cases are not
    penalised for a ceiling they cannot reach (only set when ``available`` is given).
    """
    out: dict[str, float] = {}
    relevant = sum(1 for name in neighbour_labels[:10] if name == truth)
    out["rel@10"] = float(relevant)
    if available is not None:
        out["np@10"] = relevant / max(1, min(10, available))
    for k in KS:
        out[f"hit@{k}"] = float(truth in neighbour_labels[:k])
    first = next(
        (r for r, name in enumerate(neighbour_labels, 1) if name == truth), None
    )
    out["mrr"] = 1.0 / first if first else 0.0
    ranked = label_vote(neighbour_labels)
    out["vote@1"] = float(truth in ranked[:1])
    out["vote@5"] = float(truth in ranked[:5])
    return out


def chance_hit(pool: int, same_label: int, k: int) -> float:
    """P(at least one of k random distinct pool items shares the label)."""
    k = min(k, pool)
    if same_label <= 0 or pool <= 0:
        return 0.0
    if pool - same_label < k:
        return 1.0
    miss = 1.0
    for i in range(k):
        miss *= (pool - same_label - i) / (pool - i)
    return 1.0 - miss


def bootstrap_ci(
    values: list[float], resamples: int = 2000, seed: int = 0
) -> tuple[float, float, float]:
    """(mean, low, high) 95% percentile interval."""
    if not values:
        return 0.0, 0.0, 0.0
    rng = random.Random(seed)
    n = len(values)
    means = sorted(
        sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples)
    )
    return (
        sum(values) / n,
        means[int(0.025 * resamples)],
        means[int(0.975 * resamples) - 1],
    )


def paired_difference_ci(
    a: list[float], b: list[float], resamples: int = 2000, seed: int = 0
) -> tuple[float, float, float]:
    """(mean(b - a), low, high): positive means ``b`` is better."""
    return bootstrap_ci([y - x for x, y in zip(a, b)], resamples, seed)


def feature_kind(feature: str) -> str:
    """snomed | cui | surface (negation prefix ignored)."""
    bare = feature[4:] if feature.startswith("NEG:") else feature
    return {"S": "snomed", "C": "cui", "t": "surface"}.get(bare[:1], "other")


def unseen_rate(
    query_sets: list[set[str]], pool_sets: list[set[str]], leave_one_out: bool
) -> dict[str, dict[str, float]]:
    """Share of query features that no pool case has, by feature kind.

    A concept written differently in two cases only matches when both wordings map to
    the same ID, so a high unseen rate (especially for unlinked 'surface' terms) means
    the overlap lane cannot see those features. In leave-one-out mode the query is
    itself in the pool, so a feature is unseen when it occurs in exactly one case.
    """
    df: Counter = Counter()
    for features in pool_sets:
        df.update(features)
    floor = 1 if leave_one_out else 0
    total: Counter = Counter()
    unseen: Counter = Counter()
    for features in query_sets:
        for feature in features:
            kind = feature_kind(feature)
            total[kind] += 1
            unseen[kind] += df[feature] <= floor
    out = {
        kind: {
            "features": total[kind],
            "unseen": unseen[kind],
            "rate": round(unseen[kind] / total[kind], 3),
        }
        for kind in total
    }
    all_total, all_unseen = sum(total.values()), sum(unseen.values())
    out["all"] = {
        "features": all_total,
        "unseen": all_unseen,
        "rate": round(all_unseen / max(1, all_total), 3),
    }
    return out


def overlap_stats(
    case_raw: dict[str, Any], mentions: list[dict[str, Any]]
) -> dict[str, float]:
    """How much the two extractors agree on where concepts are (character spans)."""
    gl = [(m["field"], m["start"], m["end"]) for m in mentions]
    qu = [(m["field"], m["start"], m["end"]) for m in case_raw.get("text_matches", [])]

    def covered(
        span: tuple[str, int, int], others: Iterable[tuple[str, int, int]]
    ) -> bool:
        return any(
            span[0] == o[0] and span[1] < o[2] and o[1] < span[2] for o in others
        )

    return {
        "gliner_mentions": len(gl),
        "quickumls_matches": len(qu),
        "gliner_covered_by_quickumls": sum(covered(s, qu) for s in gl),
        "quickumls_covered_by_gliner": sum(covered(s, gl) for s in qu),
        "gliner_linked": len(case_raw.get("mention_links", [])),
    }


# ----------------------------------------------------------------------- main


def load_arm(
    out: Path,
    arm: str,
    snomed_db: Path,
    negation: str = "mark",
    min_gliner_score: float = 0.5,
) -> tuple[list[dict[str, Any]], dict[str, set[str]], dict[str, str], str]:
    """(usable cases, features per case id, clean label per feature, mode) for one arm."""
    cases = list(read_jsonl(out / "cases.jsonl"))
    mentions = {
        r["case_id"]: r["mentions"] for r in read_jsonl(out / "mentions_gliner.jsonl")
    }
    raw = {r["case_id"]: r for r in read_jsonl(out / "quickumls_raw.jsonl")}
    need_g, need_q = arm in ("gliner", "both"), arm in ("quickumls", "both")
    usable = [
        c
        for c in cases
        if (not need_g or c["case_id"] in mentions)
        and (not need_q or c["case_id"] in raw)
    ]
    lookup = make_cui_lookup(sqlite3.connect(str(snomed_db)))
    clean: dict[str, str] = {}
    feats = {
        c["case_id"]: build_case_features(
            c,
            mentions.get(c["case_id"], []),
            raw.get(c["case_id"]),
            arm,
            lookup,
            negation,
            min_gliner_score,
            None,
            frozenset(),
            clean,
        )
        for c in usable
    }
    splits = {c["split"] for c in usable}
    mode = "traintest" if {"train", "test"} <= splits else "loo"
    return usable, feats, clean, mode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--snomed-db", type=Path, default=Path("/home/tin/umls/snomed_index.sqlite")
    )
    parser.add_argument("--mode", choices=("auto", "traintest", "loo"), default="auto")
    parser.add_argument("--arms", nargs="+", choices=ARMS, default=list(ARMS))
    parser.add_argument("--scorers", nargs="+", choices=SCORERS, default=list(SCORERS))
    parser.add_argument(
        "--negation", choices=("mark", "drop", "ignore"), default="mark"
    )
    parser.add_argument("--min-gliner-score", type=float, default=0.5)
    parser.add_argument(
        "--exclude-gliner-labels",
        nargs="*",
        default=[],
        help="GLiNER labels to ignore, e.g. Clinical_event Organism.",
    )
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument(
        "--examples", type=int, default=2, help="Cases to print per arm."
    )
    parser.add_argument("--report", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cases = list(read_jsonl(args.out / "cases.jsonl"))
    mentions = {
        r["case_id"]: r["mentions"]
        for r in read_jsonl(args.out / "mentions_gliner.jsonl")
    }
    raw = {r["case_id"]: r for r in read_jsonl(args.out / "quickumls_raw.jsonl")}
    needs_gliner = any(a in ("gliner", "both") for a in args.arms)
    needs_qu = any(a in ("quickumls", "both") for a in args.arms)
    if needs_gliner and not mentions:
        raise SystemExit("mentions_gliner.jsonl missing/empty (run extract_gliner.py)")
    if needs_qu and not raw:
        raise SystemExit("quickumls_raw.jsonl missing/empty (run extract_quickumls.py)")
    usable = [
        c
        for c in cases
        if (not needs_gliner or c["case_id"] in mentions)
        and (not needs_qu or c["case_id"] in raw)
    ]
    print(f"{len(usable)}/{len(cases)} cases have all required extractions")

    splits = {c["split"] for c in usable}
    mode = (
        args.mode
        if args.mode != "auto"
        else ("traintest" if {"train", "test"} <= splits else "loo")
    )
    conn = sqlite3.connect(str(args.snomed_db))
    lookup = make_cui_lookup(conn)

    report: dict[str, Any] = {"mode": mode, "cases": len(usable), "arms": {}}
    per_arm_metrics: dict[str, dict[str, dict[str, list[float]]]] = {}
    names: dict[str, dict[str, str]] = {}

    for arm in args.arms:
        names[arm] = {}
        feats = {
            c["case_id"]: build_case_features(
                c,
                mentions.get(c["case_id"], []),
                raw.get(c["case_id"]),
                arm,
                lookup,
                args.negation,
                args.min_gliner_score,
                names[arm],
                frozenset(args.exclude_gliner_labels),
            )
            for c in usable
        }
        sizes = [len(f) for f in feats.values()]
        neg = sum(1 for f in feats.values() for x in f if x.startswith("NEG:"))
        total = sum(sizes) or 1
        arm_report: dict[str, Any] = {
            "features_per_case": {
                "mean": round(statistics.mean(sizes), 1),
                "median": statistics.median(sizes),
                "min": min(sizes),
                "max": max(sizes),
                "cases_under_5": sum(1 for s in sizes if s < 5),
            },
            "distinct_features": len({x for f in feats.values() for x in f}),
            "negated_share": round(neg / total, 3),
            "retrieval": {},
        }

        if mode == "traintest":
            pool = [c for c in usable if c["split"] == "train"]
            queries = [c for c in usable if c["split"] == "test"]
        else:
            pool = list(usable)
            queries = list(usable)
        index = ConceptIndex([feats[c["case_id"]] for c in pool])
        arm_report["unseen_feature_rate"] = unseen_rate(
            [feats[c["case_id"]] for c in queries],
            [feats[c["case_id"]] for c in pool],
            leave_one_out=(mode == "loo"),
        )
        position = {c["case_id"]: i for i, c in enumerate(pool)}

        for label_key in ("specific", "group"):
            pool_labels = Counter(c[label_key] for c in pool)
            for scorer in args.scorers:
                rows: list[dict[str, float]] = []
                eligible_ids: list[str] = []
                chance: list[dict[str, float]] = []
                for query in queries:
                    truth = query[label_key]
                    own = position.get(query["case_id"]) if mode == "loo" else None
                    available = pool_labels[truth] - (1 if own is not None else 0)
                    if not truth or available <= 0:
                        continue
                    ranked = index.score(feats[query["case_id"]], scorer, exclude=own)[
                        : args.k
                    ]
                    labels = [pool[i][label_key] for i, _ in ranked]
                    rows.append(case_metrics(labels, truth, available))
                    eligible_ids.append(query["case_id"])
                    chance.append(
                        {
                            f"hit@{k}": chance_hit(
                                len(pool) - (own is not None), available, k
                            )
                            for k in KS
                        }
                    )
                summary = {"n_eligible": len(rows)}
                columns: dict[str, list[float]] = {}
                for metric in [f"hit@{k}" for k in KS] + [
                    "mrr",
                    "vote@1",
                    "vote@5",
                    "rel@10",
                    "np@10",
                ]:
                    values = [r[metric] for r in rows]
                    columns[metric] = values
                    mean, low, high = bootstrap_ci(values)
                    summary[metric] = [round(mean, 3), round(low, 3), round(high, 3)]
                summary["chance"] = {
                    f"hit@{k}": round(statistics.mean(c[f"hit@{k}"] for c in chance), 3)
                    if chance
                    else 0.0
                    for k in KS
                }
                arm_report["retrieval"][f"{label_key}/{scorer}"] = summary
                per_arm_metrics.setdefault(arm, {})[f"{label_key}/{scorer}"] = columns
        report["arms"][arm] = arm_report

        spans = [
            overlap_stats(raw[c["case_id"]], mentions.get(c["case_id"], []))
            for c in usable
            if c["case_id"] in raw
        ]
        if spans and needs_gliner and needs_qu:
            totals = Counter()
            for s in spans:
                totals.update(s)
            report["extractor_agreement"] = {
                "gliner_mentions": totals["gliner_mentions"],
                "quickumls_matches": totals["quickumls_matches"],
                "gliner_share_also_found_by_quickumls": round(
                    totals["gliner_covered_by_quickumls"]
                    / max(1, totals["gliner_mentions"]),
                    3,
                ),
                "quickumls_share_also_found_by_gliner": round(
                    totals["quickumls_covered_by_gliner"]
                    / max(1, totals["quickumls_matches"]),
                    3,
                ),
                "gliner_mentions_linked_to_concept": round(
                    totals["gliner_linked"] / max(1, totals["gliner_mentions"]), 3
                ),
            }

        # Most discriminating features of the first few cases, for eyeballing.
        shown = []
        for case in usable[: args.examples]:
            top = sorted(
                feats[case["case_id"]],
                key=lambda f: -index.idf.get(f, math.log(index.n + 1)),
            )[:12]
            shown.append(
                {
                    "case": case["case_id"],
                    "label": case["specific"],
                    "rarest_features": [names[arm].get(f, f) for f in top],
                }
            )
        arm_report["examples"] = shown

    # Paired comparisons between arms for the primary scorer.
    comparisons = {}
    primary = f"specific/{args.scorers[0]}"
    for metric in ("hit@10", "vote@1", "vote@5", "rel@10"):
        for i, a in enumerate(args.arms):
            for b in args.arms[i + 1 :]:
                x = per_arm_metrics.get(a, {}).get(primary, {}).get(metric)
                y = per_arm_metrics.get(b, {}).get(primary, {}).get(metric)
                if x and y and len(x) == len(y):
                    mean, low, high = paired_difference_ci(x, y)
                    comparisons[f"{b} - {a} [{metric}, {primary}]"] = [
                        round(mean, 3),
                        round(low, 3),
                        round(high, 3),
                    ]
    report["paired_differences"] = comparisons

    print(f"\nmode={mode}  negation={args.negation}  k={args.k}")
    if mode == "loo":
        print(
            "NOTE: leave-one-out on a small pool; intervals are wide, this is a demo.\n"
        )
    if "extractor_agreement" in report:
        print(f"extractor agreement: {report['extractor_agreement']}\n")
    for arm, info in report["arms"].items():
        print(
            f"== {arm}: {info['features_per_case']}  distinct={info['distinct_features']}  negated={info['negated_share']}"
        )
        print(f"   features seen in no other case: {info['unseen_feature_rate']}")
        for key, summary in info["retrieval"].items():
            line = "  ".join(
                f"{m}={summary[m][0]:.3f}"
                for m in (
                    "hit@1",
                    "hit@5",
                    "hit@10",
                    "mrr",
                    "vote@1",
                    "vote@5",
                    "rel@10",
                    "np@10",
                )
            )
            chance = summary["chance"]
            print(
                f"   {key:18s} n={summary['n_eligible']:<4d} {line}"
                f"   | chance hit@1/5/10 = {chance['hit@1']:.3f}/{chance['hit@5']:.3f}/{chance['hit@10']:.3f}"
            )
    if comparisons:
        print("\npaired differences (mean, 95% CI):")
        for name, value in comparisons.items():
            print(f"   {name}: {value}")
    for arm, info in report["arms"].items():
        for example in info["examples"]:
            print(f"\n[{arm}] {example['case']} ({example['label']}) rarest features:")
            for feature in example["rarest_features"]:
                print(f"     - {feature}")

    path = args.report or (args.out / "report.json")
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nreport -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
