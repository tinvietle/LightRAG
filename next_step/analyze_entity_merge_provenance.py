"""Trace conservative entity-normalization groups to documents and diseases."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Iterable


DISEASE_FIELD_RE = re.compile(
    r'"(?P<field>grouped_disease_name|extracted_disease_name)"\s*:\s*'
    r'(?P<value>"(?:[^"\\]|\\.)*")'
)


class DisjointSet:
    """Small union-find implementation for normalization components."""

    def __init__(self, names: Iterable[str]) -> None:
        self.parent = {name: name for name in names}
        self.size = {name: 1 for name in names}
        self.cuis: dict[str, set[str]] = {name: set() for name in names}

    def find(self, name: str) -> str:
        parent = self.parent[name]
        if parent != name:
            self.parent[name] = self.find(parent)
        return self.parent[name]

    def add_cui(self, name: str, cui: str) -> None:
        self.cuis[self.find(name)].add(cui)

    def union(self, left: str, right: str, *, reject_cui_conflict: bool = False) -> bool:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return False
        if reject_cui_conflict and len(self.cuis[left_root] | self.cuis[right_root]) > 1:
            return False
        if self.size[left_root] < self.size[right_root]:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        self.size[left_root] += self.size[right_root]
        self.cuis[left_root].update(self.cuis.pop(right_root))
        return True

    def components(self) -> list[list[str]]:
        groups: dict[str, list[str]] = defaultdict(list)
        for name in self.parent:
            groups[self.find(name)].append(name)
        return [sorted(group, key=str.casefold) for group in groups.values() if len(group) > 1]


@dataclass(frozen=True)
class DocumentLabel:
    file_path: str | None
    grouped_diseases: tuple[str, ...]
    extracted_diseases: tuple[str, ...]


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
    parser.add_argument(
        "--details-output",
        type=Path,
        default=Path("artifacts/merge_provenance/data_conservative_components.jsonl"),
    )
    parser.add_argument(
        "--summary-output",
        type=Path,
        default=Path("artifacts/merge_provenance/data_conservative_summary.json"),
    )
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"Required input not found: {path}") from error


def jsonl_rows(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(f"Invalid JSON at {path}:{line_number}") from error
    except FileNotFoundError as error:
        raise ValueError(f"Required input not found: {path}") from error


def extract_document_label(record: dict[str, Any]) -> DocumentLabel:
    """Extract labels even when LightRAG content concatenates multiple JSON values."""
    labels: dict[str, set[str]] = defaultdict(set)
    content = str(record.get("content", ""))
    for match in DISEASE_FIELD_RE.finditer(content):
        value = json.loads(match.group("value")).strip()
        if value:
            labels[match.group("field")].add(value)
    return DocumentLabel(
        file_path=record.get("file_path"),
        grouped_diseases=tuple(sorted(labels["grouped_disease_name"], key=str.casefold)),
        extracted_diseases=tuple(
            sorted(labels["extracted_disease_name"], key=str.casefold)
        ),
    )


def document_id_from_chunk(chunk_id: str) -> str:
    return chunk_id.rsplit("-chunk-", 1)[0]


def build_components(
    entity_names: set[str],
    umls_path: Path,
    sapbert_path: Path,
    threshold: float,
) -> tuple[list[list[str]], int, int, int]:
    dsu = DisjointSet(entity_names)
    by_cui: dict[str, list[str]] = defaultdict(list)
    for row in jsonl_rows(umls_path):
        cuis = row.get("best_cuis", [])
        name = row.get("entity_name")
        if row.get("auto_lexical_candidate") and len(cuis) == 1 and name in entity_names:
            by_cui[cuis[0]].append(name)
            dsu.add_cui(name, cuis[0])

    umls_reductions = 0
    for names in by_cui.values():
        for name in names[1:]:
            umls_reductions += dsu.union(names[0], name)

    lexical_reductions = 0
    conflicting_cui_pairs_blocked = 0
    for pair in jsonl_rows(sapbert_path):
        if (
            pair.get("similarity", 0.0) >= threshold
            and pair.get("lexical_equivalent")
            and not pair.get("modifier_conflicts")
            and pair.get("left") in entity_names
            and pair.get("right") in entity_names
        ):
            left_root = dsu.find(pair["left"])
            right_root = dsu.find(pair["right"])
            if left_root == right_root:
                continue
            if len(dsu.cuis[left_root] | dsu.cuis[right_root]) > 1:
                conflicting_cui_pairs_blocked += 1
                continue
            lexical_reductions += dsu.union(
                pair["left"], pair["right"], reject_cui_conflict=True
            )
    return (
        dsu.components(),
        umls_reductions,
        lexical_reductions,
        conflicting_cui_pairs_blocked,
    )


def count_distribution(values: Iterable[int]) -> dict[str, int]:
    return {str(key): value for key, value in sorted(Counter(values).items())}


def main() -> int:
    args = parse_args()
    full_docs = read_json(args.storage / "kv_store_full_docs.json")
    entity_chunks = read_json(args.storage / "kv_store_entity_chunks.json")
    document_labels = {
        doc_id: extract_document_label(record) for doc_id, record in full_docs.items()
    }
    components, umls_reductions, lexical_reductions, conflicting_cui_pairs_blocked = (
        build_components(
        set(entity_chunks), args.umls_evaluation, args.sapbert_candidates, args.threshold
        )
    )

    details: list[dict[str, Any]] = []
    grouped_disease_counts: Counter[str] = Counter()
    extracted_disease_counts: Counter[str] = Counter()
    document_counts: list[int] = []
    grouped_disease_cardinalities: list[int] = []
    components_with_missing_labels = 0
    components_adding_document_provenance = 0
    components_adding_disease_provenance = 0
    components_with_disjoint_disease_variants = 0

    for component_id, names in enumerate(components, 1):
        variants = []
        all_doc_ids: set[str] = set()
        for name in names:
            doc_ids = sorted(
                {
                    document_id_from_chunk(chunk_id)
                    for chunk_id in entity_chunks[name].get("chunk_ids", [])
                }
            )
            all_doc_ids.update(doc_ids)
            variant_grouped_diseases = sorted(
                {
                    disease
                    for doc_id in doc_ids
                    for disease in document_labels.get(
                        doc_id, DocumentLabel(None, (), ())
                    ).grouped_diseases
                },
                key=str.casefold,
            )
            variants.append(
                {
                    "entity_name": name,
                    "document_ids": doc_ids,
                    "grouped_diseases": variant_grouped_diseases,
                }
            )

        grouped = sorted(
            {
                disease
                for doc_id in all_doc_ids
                for disease in document_labels.get(
                    doc_id, DocumentLabel(None, (), ())
                ).grouped_diseases
            },
            key=str.casefold,
        )
        extracted = sorted(
            {
                disease
                for doc_id in all_doc_ids
                for disease in document_labels.get(
                    doc_id, DocumentLabel(None, (), ())
                ).extracted_diseases
            },
            key=str.casefold,
        )
        documents = [
            {
                "document_id": doc_id,
                "file_path": document_labels.get(
                    doc_id, DocumentLabel(None, (), ())
                ).file_path,
                "grouped_diseases": list(
                    document_labels.get(
                        doc_id, DocumentLabel(None, (), ())
                    ).grouped_diseases
                ),
                "extracted_diseases": list(
                    document_labels.get(
                        doc_id, DocumentLabel(None, (), ())
                    ).extracted_diseases
                ),
            }
            for doc_id in sorted(all_doc_ids)
        ]
        if any(not document["grouped_diseases"] for document in documents):
            components_with_missing_labels += 1
        variant_doc_sets = [set(variant["document_ids"]) for variant in variants]
        variant_disease_sets = [
            set(variant["grouped_diseases"]) for variant in variants
        ]
        adds_document_provenance = len(all_doc_ids) > 1 and not any(
            doc_ids == all_doc_ids for doc_ids in variant_doc_sets
        )
        adds_disease_provenance = len(grouped) > 1 and not any(
            diseases == set(grouped) for diseases in variant_disease_sets
        )
        has_disjoint_disease_variants = any(
            variant_disease_sets[left].isdisjoint(variant_disease_sets[right])
            for left in range(len(variant_disease_sets))
            for right in range(left + 1, len(variant_disease_sets))
        )
        components_adding_document_provenance += adds_document_provenance
        components_adding_disease_provenance += adds_disease_provenance
        components_with_disjoint_disease_variants += has_disjoint_disease_variants
        grouped_disease_counts.update(grouped)
        extracted_disease_counts.update(extracted)
        document_counts.append(len(all_doc_ids))
        grouped_disease_cardinalities.append(len(grouped))
        details.append(
            {
                "component_id": component_id,
                "entity_names": names,
                "entity_count": len(names),
                "document_count": len(all_doc_ids),
                "grouped_disease_count": len(grouped),
                "extracted_disease_count": len(extracted),
                "spans_multiple_documents": len(all_doc_ids) > 1,
                "spans_multiple_grouped_diseases": len(grouped) > 1,
                "merge_adds_document_provenance": adds_document_provenance,
                "merge_adds_grouped_disease_provenance": adds_disease_provenance,
                "has_disjoint_grouped_disease_variants": (
                    has_disjoint_disease_variants
                ),
                "grouped_diseases": grouped,
                "extracted_diseases": extracted,
                "variants": variants,
                "documents": documents,
            }
        )

    details.sort(
        key=lambda row: (
            row["grouped_disease_count"],
            row["document_count"],
            row["entity_count"],
        ),
        reverse=True,
    )
    args.details_output.parent.mkdir(parents=True, exist_ok=True)
    with args.details_output.open("w", encoding="utf-8") as destination:
        for row in details:
            destination.write(json.dumps(row, ensure_ascii=False) + "\n")

    component_count = len(details)
    summary = {
        "configuration": {
            "storage": str(args.storage),
            "sapbert_threshold": args.threshold,
            "policy": "QuickUMLS safe unique-CUI groups plus SapBERT lexical-equivalent pairs without modifier conflicts",
        },
        "totals": {
            "entity_count": len(entity_chunks),
            "merged_component_count": component_count,
            "entities_in_merged_components": sum(len(group) for group in components),
            "umls_reductions": umls_reductions,
            "sapbert_lexical_reductions_after_umls": lexical_reductions,
            "total_reductions": umls_reductions + lexical_reductions,
            "lexical_pairs_blocked_by_conflicting_cui": conflicting_cui_pairs_blocked,
        },
        "provenance": {
            "components_spanning_multiple_documents": sum(
                count > 1 for count in document_counts
            ),
            "components_spanning_multiple_grouped_diseases": sum(
                count > 1 for count in grouped_disease_cardinalities
            ),
            "components_with_any_missing_grouped_disease_label": components_with_missing_labels,
            "components_where_merge_adds_document_provenance": (
                components_adding_document_provenance
            ),
            "components_where_merge_adds_grouped_disease_provenance": (
                components_adding_disease_provenance
            ),
            "components_with_disjoint_grouped_disease_variants": (
                components_with_disjoint_disease_variants
            ),
            "document_count_distribution": count_distribution(document_counts),
            "grouped_disease_count_distribution": count_distribution(
                grouped_disease_cardinalities
            ),
        },
        "top_grouped_diseases_by_merge_components": grouped_disease_counts.most_common(30),
        "top_extracted_diseases_by_merge_components": extracted_disease_counts.most_common(
            30
        ),
        "largest_cross_disease_components": [
            {
                key: row[key]
                for key in (
                    "entity_names",
                    "document_count",
                    "grouped_disease_count",
                    "grouped_diseases",
                    "extracted_diseases",
                )
            }
            for row in details
            if row["spans_multiple_grouped_diseases"]
        ][:25],
    }
    args.summary_output.parent.mkdir(parents=True, exist_ok=True)
    args.summary_output.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as error:
        raise SystemExit(f"error: {error}") from error
