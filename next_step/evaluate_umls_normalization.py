#!/usr/bin/env python3
"""Evaluate QuickUMLS normalization coverage for stored LightRAG entities.

This script is intentionally read-only with respect to LightRAG storage. It writes
one JSON object per entity so long evaluations can be resumed, followed by a
separate aggregate summary generated from those rows.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import sys
import time
from typing import Any, Iterable, Sequence


TOKEN_RE = re.compile(r"\b\w+(?:[-']\w+)*\b", re.UNICODE)


@dataclass(frozen=True, slots=True)
class MatchMetrics:
    """Normalized metrics for one QuickUMLS candidate."""

    start: int
    end: int
    ngram: str
    term: str
    cui: str
    similarity: float
    semtypes: list[str]
    preferred: bool
    char_coverage: float
    token_coverage: float
    full_span: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate QuickUMLS coverage of LightRAG entity names."
    )
    parser.add_argument(
        "--full-entities",
        type=Path,
        default=Path("data_old/rag_storage/kv_store_full_entities.json"),
        help="LightRAG kv_store_full_entities.json file.",
    )
    parser.add_argument(
        "--quickumls-index",
        type=Path,
        default=Path("../umls/quickumls_data"),
        help="Built QuickUMLS index directory.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/umls/entity_normalization_evaluation.jsonl"),
        help="Per-entity JSONL output (append-only when --resume is used).",
    )
    parser.add_argument(
        "--summary",
        type=Path,
        default=Path("artifacts/umls/entity_normalization_summary.json"),
        help="Aggregate JSON summary output.",
    )
    parser.add_argument("--threshold", type=float, default=0.7)
    parser.add_argument(
        "--auto-threshold",
        type=float,
        default=0.9,
        help="Similarity required for an automatic lexical candidate.",
    )
    parser.add_argument("--window", type=int, default=10)
    parser.add_argument(
        "--similarity-name",
        choices=("dice", "jaccard", "cosine", "overlap"),
        default="jaccard",
    )
    parser.add_argument(
        "--ignore-syntax",
        action="store_true",
        help="Disable QuickUMLS syntax heuristics.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip entity names already present in the JSONL output.",
    )
    parser.add_argument(
        "--max-entities",
        type=int,
        help="Evaluate only the first N sorted entities (useful for validation).",
    )
    parser.add_argument("--progress-every", type=int, default=250)
    args = parser.parse_args()
    if not 0 <= args.threshold <= 1 or not 0 <= args.auto_threshold <= 1:
        parser.error("thresholds must be between 0 and 1")
    if args.auto_threshold < args.threshold:
        parser.error("--auto-threshold must be >= --threshold")
    if args.window < 1:
        parser.error("--window must be at least 1")
    if args.max_entities is not None and args.max_entities < 1:
        parser.error("--max-entities must be at least 1")
    return args


def load_entity_document_frequencies(path: Path) -> Counter[str]:
    """Load unique entity names and count documents containing each name."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"Full-entities storage not found: {path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON in full-entities storage: {path}") from error

    frequencies: Counter[str] = Counter()
    for document in payload.values():
        names = document.get("entity_names", [])
        if not isinstance(names, list):
            continue
        frequencies.update(
            {
                name.strip()
                for name in names
                if isinstance(name, str) and name.strip()
            }
        )
    return frequencies


def token_spans(text: str) -> list[tuple[int, int]]:
    return [match.span() for match in TOKEN_RE.finditer(text)]


def is_full_span(text: str, start: int, end: int) -> bool:
    """Return true when only whitespace/punctuation lies outside a match."""
    if start < 0 or end < start or end > len(text):
        return False
    outside = text[:start] + text[end:]
    return not any(character.isalnum() for character in outside)


def token_coverage(text: str, start: int, end: int) -> float:
    spans = token_spans(text)
    if not spans:
        return 0.0
    covered = sum(token_start >= start and token_end <= end for token_start, token_end in spans)
    return covered / len(spans)


def normalize_match(text: str, raw: dict[str, Any]) -> MatchMetrics:
    start = int(raw["start"])
    end = int(raw["end"])
    return MatchMetrics(
        start=start,
        end=end,
        ngram=str(raw["ngram"]),
        term=str(raw["term"]),
        cui=str(raw["cui"]),
        similarity=float(raw["similarity"]),
        semtypes=sorted(str(value) for value in raw.get("semtypes", [])),
        preferred=bool(raw.get("preferred", False)),
        char_coverage=(end - start) / len(text) if text else 0.0,
        token_coverage=token_coverage(text, start, end),
        full_span=is_full_span(text, start, end),
    )


def rank_key(match: MatchMetrics) -> tuple[float, float, float, bool, int]:
    return (
        match.token_coverage,
        match.char_coverage,
        match.similarity,
        match.preferred,
        match.end - match.start,
    )


def evaluate_entity(
    entity_name: str,
    document_frequency: int,
    raw_groups: Sequence[Sequence[dict[str, Any]]],
    auto_threshold: float,
) -> dict[str, Any]:
    """Calculate normalization metrics for a single entity name."""
    candidates = [
        normalize_match(entity_name, raw)
        for group in raw_groups
        for raw in group
    ]
    candidates.sort(key=rank_key, reverse=True)
    full_matches = [candidate for candidate in candidates if candidate.full_span]
    high_full_matches = [
        candidate
        for candidate in full_matches
        if candidate.similarity >= auto_threshold
    ]
    best = candidates[0] if candidates else None
    best_full = max(full_matches, key=rank_key) if full_matches else None
    best_score = max(
        (candidate.similarity for candidate in high_full_matches), default=None
    )
    best_cuis = sorted(
        {
            candidate.cui
            for candidate in high_full_matches
            if candidate.similarity == best_score
        }
    )
    unique_best_cui = len(best_cuis) == 1

    return {
        "entity_name": entity_name,
        "document_frequency": document_frequency,
        "character_count": len(entity_name),
        "token_count": len(token_spans(entity_name)),
        "candidate_count": len(candidates),
        "matched_any": bool(candidates),
        "has_full_span_match": bool(full_matches),
        "has_high_confidence_full_span_match": bool(high_full_matches),
        "unique_best_cui": unique_best_cui,
        "auto_lexical_candidate": bool(high_full_matches) and unique_best_cui,
        "best_cuis": best_cuis,
        "best_match": asdict(best) if best else None,
        "best_full_span_match": asdict(best_full) if best_full else None,
    }


def load_completed_names(path: Path) -> set[str]:
    if not path.exists():
        return set()
    names: set[str] = set()
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Invalid JSONL at {path}:{line_number}; repair or remove it"
                ) from error
            names.add(row["entity_name"])
    return names


def length_bucket(token_count: int) -> str:
    if token_count <= 1:
        return "1"
    if token_count == 2:
        return "2"
    if token_count <= 5:
        return "3-5"
    if token_count <= 10:
        return "6-10"
    return "11+"


def percentage(numerator: int, denominator: int) -> float:
    return round(100 * numerator / denominator, 3) if denominator else 0.0


def build_summary(
    rows: Iterable[dict[str, Any]], configuration: dict[str, Any]
) -> dict[str, Any]:
    metrics = (
        "matched_any",
        "has_full_span_match",
        "has_high_confidence_full_span_match",
        "unique_best_cui",
        "auto_lexical_candidate",
    )
    total = Counter[str]()
    by_length: dict[str, Counter[str]] = defaultdict(Counter)
    semantic_types: Counter[str] = Counter()
    token_coverage_bands: Counter[str] = Counter()
    document_weighted = Counter[str]()
    token_weighted = Counter[str]()

    for row in rows:
        total["entities"] += 1
        total["tokens"] += row["token_count"]
        document_frequency = row["document_frequency"]
        total["document_mentions"] += document_frequency
        bucket = by_length[length_bucket(row["token_count"])]
        bucket["entities"] += 1
        bucket["tokens"] += row["token_count"]
        for metric in metrics:
            if row[metric]:
                total[metric] += 1
                bucket[metric] += 1
                document_weighted[metric] += document_frequency
                token_weighted[metric] += row["token_count"]

        best = row["best_match"]
        if best:
            coverage = best["token_coverage"]
            band = (
                "100%"
                if coverage == 1
                else "75-99%"
                if coverage >= 0.75
                else "50-74%"
                if coverage >= 0.5
                else "1-49%"
            )
            token_coverage_bands[band] += 1
        else:
            token_coverage_bands["unmatched"] += 1

        if row["auto_lexical_candidate"]:
            for semtype in row["best_full_span_match"]["semtypes"]:
                semantic_types[semtype] += 1

    entity_count = total["entities"]
    mention_count = total["document_mentions"]
    normalized_by_length = {}
    for bucket_name in ("1", "2", "3-5", "6-10", "11+"):
        counts = by_length[bucket_name]
        normalized_by_length[bucket_name] = {
            **dict(counts),
            **{
                f"{metric}_percentage": percentage(counts[metric], counts["entities"])
                for metric in metrics
            },
        }

    return {
        "configuration": configuration,
        "totals": {
            **dict(total),
            **{
                f"{metric}_percentage": percentage(total[metric], entity_count)
                for metric in metrics
            },
            **{
                f"document_weighted_{metric}_percentage": percentage(
                    document_weighted[metric], mention_count
                )
                for metric in metrics
            },
            **{
                f"token_weighted_{metric}_count": token_weighted[metric]
                for metric in metrics
            },
            **{
                f"token_weighted_{metric}_percentage": percentage(
                    token_weighted[metric], total["tokens"]
                )
                for metric in metrics
            },
        },
        "by_token_length": normalized_by_length,
        "best_match_token_coverage": dict(token_coverage_bands),
        "auto_candidate_semantic_types": dict(semantic_types.most_common()),
    }


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                yield json.loads(line)


def create_matcher(args: argparse.Namespace) -> Any:
    try:
        from quickumls import QuickUMLS
    except ImportError as error:
        raise RuntimeError(
            "QuickUMLS is not installed; run this script with the UMLS virtual "
            "environment, for example ../umls/.venv/bin/python"
        ) from error
    if not args.quickumls_index.exists():
        raise ValueError(f"QuickUMLS index not found: {args.quickumls_index}")
    return QuickUMLS(
        str(args.quickumls_index.resolve()),
        overlapping_criteria="score",
        threshold=args.threshold,
        similarity_name=args.similarity_name,
        window=args.window,
    )


def main() -> int:
    args = parse_args()
    frequencies = load_entity_document_frequencies(args.full_entities)
    names = sorted(frequencies)
    if args.max_entities is not None:
        names = names[: args.max_entities]

    completed = load_completed_names(args.output) if args.resume else set()
    pending = [name for name in names if name not in completed]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    matcher = create_matcher(args) if pending else None
    mode = "a" if args.resume else "w"
    started_at = time.monotonic()

    with args.output.open(mode, encoding="utf-8") as destination:
        for index, entity_name in enumerate(pending, start=1):
            groups = matcher.match(
                entity_name,
                best_match=False,
                ignore_syntax=args.ignore_syntax,
            )
            row = evaluate_entity(
                entity_name,
                frequencies[entity_name],
                groups,
                args.auto_threshold,
            )
            destination.write(json.dumps(row, ensure_ascii=False) + "\n")
            destination.flush()
            if index % args.progress_every == 0 or index == len(pending):
                elapsed = time.monotonic() - started_at
                rate = index / elapsed if elapsed else 0.0
                print(
                    f"Evaluated {index}/{len(pending)} pending entities "
                    f"({rate:.1f} entities/s)",
                    file=sys.stderr,
                    flush=True,
                )

    configuration = {
        "full_entities": str(args.full_entities),
        "quickumls_index": str(args.quickumls_index),
        "threshold": args.threshold,
        "auto_threshold": args.auto_threshold,
        "window": args.window,
        "similarity_name": args.similarity_name,
        "ignore_syntax": args.ignore_syntax,
        "selected_entity_count": len(names),
    }
    selected_names = set(names)
    summary = build_summary(
        (row for row in iter_jsonl(args.output) if row["entity_name"] in selected_names),
        configuration,
    )
    args.summary.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
