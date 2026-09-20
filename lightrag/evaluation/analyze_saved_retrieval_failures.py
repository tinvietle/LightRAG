#!/usr/bin/env python3
"""Explain strict disease-retrieval failures in a saved evaluation artifact."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
from statistics import mean
from typing import Any

from lightrag.evaluation.eval_saved_contexts import (
    answer_labels_match,
    extract_source_labels,
    labels_match,
    normalize_label,
)


def load_corpus_documents(path: Path) -> dict[str, set[str]]:
    if path.is_dir():
        labels: dict[str, set[str]] = defaultdict(set)
        for case_path in path.rglob("custom_case_*.json"):
            try:
                record = json.loads(case_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict):
                continue
            extracted = str(record.get("extracted_disease_name", ""))
            if extracted:
                labels[normalize_label(extracted)].add(case_path.name)
        return labels

    stored = json.loads(path.read_text(encoding="utf-8"))
    labels: dict[str, set[str]] = defaultdict(set)
    for document in stored.values():
        if not isinstance(document, dict):
            continue
        extracted, _ = extract_source_labels(str(document.get("content", "")))
        if extracted:
            labels[normalize_label(extracted)].add(
                str(document.get("file_path", ""))
            )
    return labels


def load_graph_entity_names(path: Path) -> set[str]:
    stored = json.loads(path.read_text(encoding="utf-8"))
    return {normalize_label(str(name)) for name in stored}


def _keywords(metadata: dict[str, Any]) -> list[str]:
    keyword_data = metadata.get("keywords", {})
    return [
        str(item)
        for key in ("high_level", "low_level")
        for item in keyword_data.get(key, [])
    ]


def _contains_concept(left: str, right: str) -> bool:
    return answer_labels_match(left, right) or answer_labels_match(right, left)


def _average(values: list[float]) -> float:
    return round(mean(values), 4) if values else 0.0


def analyze(
    artifact: dict[str, Any],
    corpus_documents: dict[str, set[str]],
    graph_entities: set[str],
) -> dict[str, Any]:
    category_counts: Counter[str] = Counter()
    category_examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    signals: Counter[str] = Counter()
    processing: dict[str, list[float]] = defaultdict(list)
    cases: list[dict[str, Any]] = []

    for result in artifact.get("results", []):
        truth = str(result.get("ground_truth", ""))
        truth_key = normalize_label(truth)
        retrieval = result.get("retrieval", {})
        chunks = retrieval.get("chunks", [])
        entities = retrieval.get("entities", [])
        metadata = result.get("retrieval_metadata", {})
        info = metadata.get("processing_info", {})

        labels = [
            extract_source_labels(str(chunk.get("content", ""))) for chunk in chunks
        ]
        exact_chunk_hit = any(labels_match(truth, extracted) for extracted, _ in labels)
        grouped_chunk_hit = any(labels_match(truth, grouped) for _, grouped in labels)
        related_chunk_hit = any(
            _contains_concept(truth, extracted) for extracted, _ in labels
        )
        entity_names = [str(entity.get("entity_name", "")) for entity in entities]
        exact_entity_retrieved = any(labels_match(truth, name) for name in entity_names)
        related_entity_retrieved = any(
            _contains_concept(truth, name) for name in entity_names
        )
        corpus_present = bool(corpus_documents.get(truth_key))
        graph_entity_present = truth_key in graph_entities
        related_graph_entity_present = any(
            _contains_concept(truth_key, entity_name)
            for entity_name in graph_entities
        )
        keyword_concept_present = any(
            _contains_concept(truth, keyword) for keyword in _keywords(metadata)
        )

        if exact_chunk_hit:
            category = "exact_retrieval_success"
        elif not corpus_present:
            category = "exact_label_absent_from_indexed_corpus"
        elif not graph_entity_present:
            category = "exact_disease_entity_absent_from_graph"
        elif not exact_entity_retrieved:
            category = "disease_entity_not_retrieved"
        else:
            category = "entity_retrieved_but_relevant_chunk_not_selected"
        category_counts[category] += 1

        if grouped_chunk_hit:
            signals["strict_failures_with_grouped_label_hit"] += int(
                not exact_chunk_hit
            )
        if related_chunk_hit:
            signals["strict_failures_with_related_label_hit"] += int(
                not exact_chunk_hit
            )
        if related_entity_retrieved:
            signals["strict_failures_with_related_entity_retrieved"] += int(
                not exact_chunk_hit
            )
        if related_graph_entity_present:
            signals["strict_failures_with_related_entity_in_graph"] += int(
                not exact_chunk_hit
            )
        if keyword_concept_present:
            signals["strict_failures_with_disease_in_keywords"] += int(
                not exact_chunk_hit
            )

        merged_chunks = int(info.get("merged_chunks_count", 0) or 0)
        final_chunks = int(info.get("final_chunks_count", 0) or 0)
        total_entities = int(info.get("total_entities_found", 0) or 0)
        final_entities = int(info.get("entities_after_truncation", 0) or 0)
        if not exact_chunk_hit:
            processing["failed_merged_chunks"].append(float(merged_chunks))
            processing["failed_final_chunks"].append(float(final_chunks))
            processing["failed_entity_retention"].append(
                final_entities / total_entities if total_entities else 0.0
            )

        record = {
            "test_number": result.get("test_number"),
            "file_name": result.get("file_name"),
            "ground_truth": truth,
            "category": category,
            "exact_documents_in_corpus": len(corpus_documents.get(truth_key, set())),
            "exact_graph_entity_present": graph_entity_present,
            "related_graph_entity_present": related_graph_entity_present,
            "exact_entity_retrieved": exact_entity_retrieved,
            "related_entity_retrieved": related_entity_retrieved,
            "disease_or_related_term_in_keywords": keyword_concept_present,
            "grouped_label_hit": grouped_chunk_hit,
            "related_source_label_hit": related_chunk_hit,
            "retrieved_source_labels": [extracted for extracted, _ in labels],
            "keywords": _keywords(metadata),
            "processing_info": info,
        }
        cases.append(record)
        if len(category_examples[category]) < 10:
            category_examples[category].append(record)

    failures = len(cases) - category_counts["exact_retrieval_success"]
    return {
        "methodology": {
            "strict_success": (
                "Ground truth exactly matches retrieved source extracted_disease_name "
                "after case and punctuation normalization."
            ),
            "failure_taxonomy_order": [
                "exact label absent from indexed corpus",
                "exact disease entity absent from graph",
                "disease entity not retrieved",
                "entity retrieved but relevant chunk not selected",
            ],
        },
        "totals": {
            "case_count": len(cases),
            "strict_successes": category_counts["exact_retrieval_success"],
            "strict_failures": failures,
        },
        "category_counts": dict(category_counts),
        "category_percent_of_all_cases": {
            key: round(value / len(cases) * 100, 2)
            for key, value in category_counts.items()
        },
        "failure_signals": {
            **dict(signals),
            "strict_failure_count": failures,
        },
        "processing_summary_for_failures": {
            "mean_candidate_chunks_before_final_selection": _average(
                processing["failed_merged_chunks"]
            ),
            "mean_final_chunks": _average(processing["failed_final_chunks"]),
            "mean_entity_retention_ratio": _average(
                processing["failed_entity_retention"]
            ),
        },
        "category_examples": category_examples,
        "cases": cases,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument(
        "--corpus",
        type=Path,
        default=Path("dataset/train"),
    )
    parser.add_argument(
        "--entity-chunks",
        type=Path,
        default=Path("data/rag_storage/kv_store_entity_chunks.json"),
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    artifact = json.loads(args.artifact.read_text(encoding="utf-8"))
    report = analyze(
        artifact,
        load_corpus_documents(args.corpus),
        load_graph_entity_names(args.entity_chunks),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    temporary.replace(args.output)
    print(
        json.dumps(
            {
                key: value
                for key, value in report.items()
                if key not in {"cases", "category_examples"}
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
