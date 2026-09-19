#!/usr/bin/env python3
"""Measure whether noisy entity types are useful as a hard merge gate."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from itertools import combinations
import json
from pathlib import Path
import re
from typing import Any

from build_entity_merge_plan import (
    EntityMetadata,
    GENERIC_ENTITY_TYPES,
    iter_jsonl,
    load_graph_metadata,
    normalize_entity_type,
    read_json_object,
    write_json_atomic,
)


LEXICAL_KEY_RE = re.compile(r"[^a-z0-9]+")


def lexical_key(value: str) -> str:
    return LEXICAL_KEY_RE.sub("", value.casefold())


def concrete_type(name: str, metadata: dict[str, EntityMetadata]) -> str | None:
    value = normalize_entity_type(metadata.get(name, EntityMetadata("", "")).entity_type)
    return None if value in GENERIC_ENTITY_TYPES else value


def types_disagree(
    left: str, right: str, metadata: dict[str, EntityMetadata]
) -> bool:
    left_type = concrete_type(left, metadata)
    right_type = concrete_type(right, metadata)
    return bool(left_type and right_type and left_type != right_type)


def pair_record(
    left: str,
    right: str,
    metadata: dict[str, EntityMetadata],
    **extra: Any,
) -> dict[str, Any]:
    return {
        "left": left,
        "right": right,
        "left_type": concrete_type(left, metadata),
        "right_type": concrete_type(right, metadata),
        **extra,
    }


def rate(numerator: int, denominator: int) -> float:
    return round(100 * numerator / denominator, 4) if denominator else 0.0


def analyze(
    storage: Path,
    umls_path: Path,
    sapbert_path: Path,
    threshold: float,
    example_limit: int,
) -> dict[str, Any]:
    entity_names = set(
        read_json_object(storage / "kv_store_entity_chunks.json")
    )
    metadata = load_graph_metadata(storage / "graph_chunk_entity_relation.graphml")

    exact_groups: dict[str, list[str]] = defaultdict(list)
    for name in entity_names:
        exact_groups[lexical_key(name)].append(name)
    exact_pairs = [
        pair
        for names in exact_groups.values()
        if len(names) > 1
        for pair in combinations(sorted(names, key=str.casefold), 2)
    ]
    exact_mismatches = [
        pair for pair in exact_pairs if types_disagree(*pair, metadata)
    ]

    by_cui: dict[str, list[str]] = defaultdict(list)
    for row in iter_jsonl(umls_path):
        name = row.get("entity_name")
        cuis = row.get("best_cuis", [])
        if (
            name in entity_names
            and row.get("auto_lexical_candidate")
            and len(cuis) == 1
        ):
            by_cui[str(cuis[0])].append(name)
    umls_edges: list[tuple[str, str, str]] = []
    for cui, names in by_cui.items():
        unique = sorted(set(names), key=str.casefold)
        if len(unique) > 1:
            umls_edges.extend((unique[0], name, cui) for name in unique[1:])
    umls_mismatches = [
        edge for edge in umls_edges if types_disagree(edge[0], edge[1], metadata)
    ]

    sapbert_counts: Counter[str] = Counter()
    sapbert_confusions: Counter[tuple[str | None, str | None]] = Counter()
    sapbert_examples: list[dict[str, Any]] = []
    for row in iter_jsonl(sapbert_path):
        left = row.get("left")
        right = row.get("right")
        if left not in entity_names or right not in entity_names:
            continue
        if float(row.get("similarity", 0.0)) < threshold:
            continue
        if not row.get("lexical_equivalent") or row.get("modifier_conflicts"):
            continue
        sapbert_counts["eligible_pairs"] += 1
        same_lexical_key = lexical_key(left) == lexical_key(right)
        same_cui = bool(
            row.get("left_cui")
            and row.get("left_cui") == row.get("right_cui")
        )
        conflicting_cui = bool(
            row.get("left_cui")
            and row.get("right_cui")
            and row.get("left_cui") != row.get("right_cui")
        )
        disagreement = types_disagree(left, right, metadata)
        if same_lexical_key:
            sapbert_counts["lexically_identical_pairs"] += 1
        if same_cui:
            sapbert_counts["same_cui_pairs"] += 1
        if conflicting_cui:
            sapbert_counts["conflicting_cui_pairs"] += 1
        if disagreement:
            sapbert_counts["type_disagreement_pairs"] += 1
            if same_lexical_key:
                sapbert_counts["type_disagreement_lexically_identical"] += 1
            if same_cui:
                sapbert_counts["type_disagreement_same_cui"] += 1
            if conflicting_cui:
                sapbert_counts["type_disagreement_conflicting_cui"] += 1
            left_type = concrete_type(left, metadata)
            right_type = concrete_type(right, metadata)
            sapbert_confusions[tuple(sorted((left_type, right_type)))] += 1
            if len(sapbert_examples) < example_limit:
                sapbert_examples.append(
                    pair_record(
                        left,
                        right,
                        metadata,
                        similarity=row.get("similarity"),
                        left_cui=row.get("left_cui"),
                        right_cui=row.get("right_cui"),
                        lexically_identical=same_lexical_key,
                    )
                )

    eligible = sapbert_counts["eligible_pairs"]
    mismatches = sapbert_counts["type_disagreement_pairs"]
    conflicting = sapbert_counts["conflicting_cui_pairs"]
    caught_conflicting = sapbert_counts["type_disagreement_conflicting_cui"]
    return {
        "configuration": {
            "storage": str(storage),
            "umls_evaluation": str(umls_path),
            "sapbert_candidates": str(sapbert_path),
            "threshold": threshold,
            "read_only": True,
        },
        "lexically_identical_surface_forms": {
            "pair_count": len(exact_pairs),
            "type_disagreement_count": len(exact_mismatches),
            "type_disagreement_percentage": rate(len(exact_mismatches), len(exact_pairs)),
            "examples": [
                pair_record(left, right, metadata)
                for left, right in exact_mismatches[:example_limit]
            ],
        },
        "quickumls_same_unique_cui": {
            "proposed_edge_count": len(umls_edges),
            "type_disagreement_count": len(umls_mismatches),
            "type_disagreement_percentage": rate(len(umls_mismatches), len(umls_edges)),
            "examples": [
                pair_record(left, right, metadata, cui=cui)
                for left, right, cui in umls_mismatches[:example_limit]
            ],
        },
        "sapbert_high_confidence_lexical": {
            **dict(sorted(sapbert_counts.items())),
            "type_disagreement_percentage": rate(mismatches, eligible),
            "conflicting_cui_detection_recall_by_type_gate": rate(
                caught_conflicting, conflicting
            ),
            "type_confusion_counts": [
                {"types": list(types), "count": count}
                for types, count in sapbert_confusions.most_common()
            ],
            "examples": sapbert_examples,
        },
        "interpretation": {
            "hard_gate_cost": (
                "Type disagreements inside lexically identical or same-CUI pairs are "
                "strong evidence of label noise and false merge rejections."
            ),
            "hard_gate_benefit_proxy": (
                "The fraction of different-CUI pairs caught by type disagreement is a "
                "limited proxy for the gate's ability to prevent false merges."
            ),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage", type=Path, default=Path("data/rag_storage"))
    parser.add_argument(
        "--umls-evaluation",
        type=Path,
        default=Path("artifacts/umls/data_entity_normalization_evaluation.jsonl"),
    )
    parser.add_argument(
        "--sapbert-candidates",
        type=Path,
        default=Path("artifacts/sapbert/data_full_top50_candidates.jsonl"),
    )
    parser.add_argument("--threshold", type=float, default=0.99)
    parser.add_argument("--example-limit", type=int, default=50)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/entity_merge/data/entity_type_gate_analysis.json"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = analyze(
        args.storage,
        args.umls_evaluation,
        args.sapbert_candidates,
        args.threshold,
        args.example_limit,
    )
    write_json_atomic(args.output, report)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
