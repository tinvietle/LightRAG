#!/usr/bin/env python3
"""Build a read-only, evidence-backed LightRAG entity merge plan.

The script analyzes a finalized LightRAG workspace. It never imports or calls
LightRAG's entity merge APIs and never writes to the storage directory.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Iterable, Iterator
import xml.etree.ElementTree as ET


DISEASE_FIELD_RE = re.compile(
    r'"(?P<field>grouped_disease_name|extracted_disease_name)"\s*:\s*'
    r'(?P<value>"(?:[^"\\]|\\.)*")'
)
ABBREVIATION_RE = re.compile(r"^[A-Z0-9][A-Z0-9./+_-]{1,14}$")
GENERIC_ENTITY_TYPES = {"", "other", "unknown"}
DEFAULT_MAX_REJECTIONS_PER_REASON = 100
ACTIVE_DOCUMENT_STATUSES = {
    "pending",
    "parsing",
    "analyzing",
    "processing",
    "preprocessed",
}


@dataclass(frozen=True, slots=True)
class DocumentLabel:
    """Disease labels and provenance associated with one source document."""

    file_path: str | None
    grouped_diseases: tuple[str, ...]
    extracted_diseases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EntityMetadata:
    """Graph metadata needed for analysis and canonical-name selection."""

    entity_type: str
    description: str


@dataclass(frozen=True, slots=True)
class AcceptedEdge:
    """One direct piece of evidence connecting two proposed aliases."""

    left: str
    right: str
    method: str
    similarity: float | None = None
    cui: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "left": self.left,
            "right": self.right,
            "method": self.method,
            "similarity": self.similarity,
            "cui": self.cui,
        }


@dataclass(slots=True)
class RejectionCollector:
    """Count every rejection while retaining bounded deterministic examples."""

    limit_per_reason: int
    counts: Counter[str] = field(default_factory=Counter)
    examples: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: defaultdict(list)
    )

    def add(self, reason: str, record: dict[str, Any]) -> None:
        self.counts[reason] += 1
        if len(self.examples[reason]) < self.limit_per_reason:
            self.examples[reason].append(record)

    def rows(self) -> Iterator[dict[str, Any]]:
        for reason in sorted(self.examples):
            for record in self.examples[reason]:
                yield {"reason": reason, **record}


class DisjointSet:
    """Union-find with component-level confident-CUI tracking."""

    def __init__(self, names: Iterable[str]) -> None:
        self.parent = {name: name for name in names}
        self.size = {name: 1 for name in names}
        self.cuis: dict[str, set[str]] = {name: set() for name in names}
        self.members: dict[str, set[str]] = {name: {name} for name in names}

    def find(self, name: str) -> str:
        parent = self.parent[name]
        if parent != name:
            self.parent[name] = self.find(parent)
        return self.parent[name]

    def add_cui(self, name: str, cui: str) -> None:
        self.cuis[self.find(name)].add(cui)

    def component_cuis(self, name: str) -> set[str]:
        return set(self.cuis[self.find(name)])

    def component_members(self, name: str) -> set[str]:
        return set(self.members[self.find(name)])

    def union(self, left: str, right: str) -> bool:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return False
        if self.size[left_root] < self.size[right_root]:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        self.size[left_root] += self.size[right_root]
        self.cuis[left_root].update(self.cuis.pop(right_root))
        self.members[left_root].update(self.members.pop(right_root))
        return True

    def components(self) -> list[list[str]]:
        groups: dict[str, list[str]] = defaultdict(list)
        for name in self.parent:
            groups[self.find(name)].append(name)
        return sorted(
            (
                sorted(group, key=lambda value: (value.casefold(), value))
                for group in groups.values()
                if len(group) > 1
            ),
            key=lambda group: (group[0].casefold(), group[0]),
        )


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
        "--output-dir",
        type=Path,
        default=Path("artifacts/entity_merge/latest"),
    )
    parser.add_argument(
        "--max-rejections-per-reason",
        type=int,
        default=DEFAULT_MAX_REJECTIONS_PER_REASON,
    )
    parser.add_argument(
        "--skip-input-hashes",
        action="store_true",
        help="Skip SHA-256 input hashing for a faster exploratory run.",
    )
    args = parser.parse_args()
    if not 0.0 <= args.threshold <= 1.0:
        parser.error("--threshold must be between 0 and 1")
    if args.max_rejections_per_reason < 0:
        parser.error("--max-rejections-per-reason must be non-negative")
    return args


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"Required input not found: {path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON in {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(
                        f"Invalid JSON at {path}:{line_number}"
                    ) from error
                if not isinstance(row, dict):
                    raise ValueError(f"Expected JSON object at {path}:{line_number}")
                yield row
    except FileNotFoundError as error:
        raise ValueError(f"Required input not found: {path}") from error


def extract_document_label(record: dict[str, Any]) -> DocumentLabel:
    """Extract labels even when LightRAG concatenates multiple JSON values."""
    labels: dict[str, set[str]] = defaultdict(set)
    for match in DISEASE_FIELD_RE.finditer(str(record.get("content", ""))):
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


def active_document_ids(doc_status: dict[str, Any]) -> list[str]:
    """Return documents whose status shows that ingestion is not finalized."""
    return sorted(
        doc_id
        for doc_id, record in doc_status.items()
        if str(record.get("status", "")).casefold() in ACTIVE_DOCUMENT_STATUSES
    )


def load_graph_metadata(path: Path) -> dict[str, EntityMetadata]:
    """Stream GraphML node metadata without loading graph edges into memory."""
    try:
        context = ET.iterparse(path, events=("start", "end"))
    except FileNotFoundError as error:
        raise ValueError(f"Required input not found: {path}") from error

    key_names: dict[str, str] = {}
    metadata: dict[str, EntityMetadata] = {}
    try:
        for event, element in context:
            local_name = element.tag.rsplit("}", 1)[-1]
            if event == "start" and local_name == "key":
                if element.attrib.get("for") == "node":
                    key_names[element.attrib["id"]] = element.attrib.get(
                        "attr.name", ""
                    )
            elif event == "end" and local_name == "node":
                values = {
                    key_names.get(child.attrib.get("key", ""), ""): child.text or ""
                    for child in element
                    if child.tag.rsplit("}", 1)[-1] == "data"
                }
                name = element.attrib.get("id", "")
                if name:
                    metadata[name] = EntityMetadata(
                        entity_type=values.get("entity_type", ""),
                        description=values.get("description", ""),
                    )
                element.clear()
            elif event == "end" and local_name == "edge":
                element.clear()
    except ET.ParseError as error:
        raise ValueError(f"Invalid GraphML in {path}: {error}") from error
    return metadata


def normalize_entity_type(value: str) -> str:
    return value.strip().casefold()


def incompatible_types(
    left_names: Iterable[str],
    right_names: Iterable[str],
    metadata: dict[str, EntityMetadata],
) -> bool:
    """Return true only for clear non-generic entity-type disagreement."""

    def concrete_types(names: Iterable[str]) -> set[str]:
        return {
            normalized
            for name in names
            if (normalized := normalize_entity_type(metadata.get(name, EntityMetadata("", "")).entity_type))
            not in GENERIC_ENTITY_TYPES
        }

    left_types = concrete_types(left_names)
    right_types = concrete_types(right_names)
    return bool(left_types and right_types and left_types.isdisjoint(right_types))


def stable_component_id(names: Iterable[str]) -> str:
    joined = "\0".join(sorted(names, key=lambda value: (value.casefold(), value)))
    return f"merge-{hashlib.sha256(joined.encode('utf-8')).hexdigest()[:16]}"


def choose_canonical(
    names: Iterable[str],
    document_ids: dict[str, set[str]],
    confident_cuis: dict[str, str],
) -> tuple[str, str, list[dict[str, Any]]]:
    """Choose an existing name with a stable, explainable ranking."""

    def rank(name: str) -> tuple[Any, ...]:
        is_abbreviation = bool(ABBREVIATION_RE.fullmatch(name))
        punctuation_count = sum(not char.isalnum() and not char.isspace() for char in name)
        return (
            -len(document_ids.get(name, set())),
            -(name in confident_cuis),
            is_abbreviation,
            punctuation_count,
            name.casefold(),
            name,
        )

    ranked = sorted(names, key=rank)
    canonical = ranked[0]
    scores = [
        {
            "entity_name": name,
            "document_count": len(document_ids.get(name, set())),
            "has_confident_unique_cui": name in confident_cuis,
            "looks_like_abbreviation": bool(ABBREVIATION_RE.fullmatch(name)),
        }
        for name in ranked
    ]
    if len(document_ids.get(canonical, set())) > len(
        document_ids.get(ranked[1], set())
    ):
        reason = "highest_document_frequency"
    elif canonical in confident_cuis and ranked[1] not in confident_cuis:
        reason = "confident_unique_cui_tiebreak"
    elif not ABBREVIATION_RE.fullmatch(canonical) and ABBREVIATION_RE.fullmatch(
        ranked[1]
    ):
        reason = "non_abbreviation_tiebreak"
    else:
        reason = "stable_lexical_tiebreak"
    return canonical, reason, scores


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    except FileNotFoundError as error:
        raise ValueError(f"Required input not found: {path}") from error
    return digest.hexdigest()


def snapshot_file(path: Path, *, include_hash: bool) -> dict[str, Any]:
    try:
        stat = path.stat()
    except FileNotFoundError as error:
        raise ValueError(f"Required input not found: {path}") from error
    return {
        "path": str(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": file_sha256(path) if include_hash else None,
    }


def verify_inputs_unchanged(snapshots: list[dict[str, Any]]) -> None:
    changed = []
    for snapshot in snapshots:
        path = Path(snapshot["path"])
        try:
            stat = path.stat()
        except FileNotFoundError:
            changed.append(str(path))
            continue
        if stat.st_size != snapshot["size"] or stat.st_mtime_ns != snapshot["mtime_ns"]:
            changed.append(str(path))
    if changed:
        raise ValueError(
            "Input files changed during analysis; wait for ingestion to finish: "
            + ", ".join(changed)
        )


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as destination:
        json.dump(value, destination, indent=2, ensure_ascii=False)
        destination.write("\n")
        temporary = Path(destination.name)
    temporary.replace(path)


def write_jsonl_atomic(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as destination:
        for row in rows:
            destination.write(json.dumps(row, ensure_ascii=False) + "\n")
        temporary = Path(destination.name)
    temporary.replace(path)


def count_distribution(values: Iterable[int]) -> dict[str, int]:
    return {str(key): value for key, value in sorted(Counter(values).items())}


def analyze(
    args: argparse.Namespace,
) -> tuple[
    list[dict[str, Any]],
    dict[str, Any],
    RejectionCollector,
    list[dict[str, Any]],
    dict[str, Any],
]:
    if args.output_dir.resolve().is_relative_to(args.storage.resolve()):
        raise ValueError("--output-dir must be outside the LightRAG storage directory")
    storage_files = {
        "full_docs": args.storage / "kv_store_full_docs.json",
        "entity_chunks": args.storage / "kv_store_entity_chunks.json",
        "text_chunks": args.storage / "kv_store_text_chunks.json",
        "doc_status": args.storage / "kv_store_doc_status.json",
        "graph": args.storage / "graph_chunk_entity_relation.graphml",
    }
    input_paths = [*storage_files.values(), args.umls_evaluation, args.sapbert_candidates]
    snapshots = [
        snapshot_file(path, include_hash=not args.skip_input_hashes)
        for path in input_paths
    ]

    full_docs = read_json_object(storage_files["full_docs"])
    entity_chunks = read_json_object(storage_files["entity_chunks"])
    text_chunks = read_json_object(storage_files["text_chunks"])
    doc_status = read_json_object(storage_files["doc_status"])
    active_documents = active_document_ids(doc_status)
    if active_documents:
        sample = ", ".join(active_documents[:5])
        raise ValueError(
            f"Storage has {len(active_documents)} active document(s); wait for "
            f"ingestion to finish. Examples: {sample}"
        )
    entity_names = set(entity_chunks)
    graph_metadata = load_graph_metadata(storage_files["graph"])
    document_labels = {
        doc_id: extract_document_label(record) for doc_id, record in full_docs.items()
    }
    chunk_document_ids = {
        chunk_id: str(record.get("full_doc_id") or document_id_from_chunk(chunk_id))
        for chunk_id, record in text_chunks.items()
    }
    entity_documents = {
        name: {
            chunk_document_ids.get(chunk_id, document_id_from_chunk(chunk_id))
            for chunk_id in record.get("chunk_ids", [])
            if chunk_id
        }
        for name, record in entity_chunks.items()
    }

    dsu = DisjointSet(entity_names)
    rejections = RejectionCollector(args.max_rejections_per_reason)
    modifier_conflict_counts: Counter[str] = Counter()
    confident_cuis: dict[str, str] = {}
    by_cui: dict[str, list[str]] = defaultdict(list)
    umls_status_counts: Counter[str] = Counter()

    for row in iter_jsonl(args.umls_evaluation):
        name = row.get("entity_name")
        if name not in entity_names:
            umls_status_counts["entity_not_in_snapshot"] += 1
            continue
        cuis = row.get("best_cuis", [])
        if row.get("auto_lexical_candidate") and len(cuis) == 1:
            cui = str(cuis[0])
            confident_cuis[name] = cui
            by_cui[cui].append(name)
            dsu.add_cui(name, cui)
            umls_status_counts["confident_unique_cui"] += 1
        elif len(cuis) > 1:
            umls_status_counts["ambiguous_cui"] += 1
        elif row.get("has_full_span_match"):
            umls_status_counts["full_span_below_auto_policy"] += 1
        elif row.get("matched_any"):
            umls_status_counts["partial_match_only"] += 1
        else:
            umls_status_counts["no_match"] += 1

    accepted_edges: list[AcceptedEdge] = []
    accepted_by_method: Counter[str] = Counter()
    for cui, names in sorted(by_cui.items()):
        unique_names = sorted(set(names), key=lambda value: (value.casefold(), value))
        if len(unique_names) < 2:
            continue
        anchor = unique_names[0]
        for name in unique_names[1:]:
            if incompatible_types(
                dsu.component_members(anchor),
                dsu.component_members(name),
                graph_metadata,
            ):
                rejections.add(
                    "umls_incompatible_entity_types",
                    {"left": anchor, "right": name, "cui": cui, "method": "quickumls"},
                )
                continue
            if dsu.union(anchor, name):
                accepted_edges.append(
                    AcceptedEdge(anchor, name, "quickumls_same_unique_cui", cui=cui)
                )
                accepted_by_method["quickumls"] += 1

    for pair in iter_jsonl(args.sapbert_candidates):
        left = pair.get("left")
        right = pair.get("right")
        similarity = float(pair.get("similarity", 0.0))
        base_record = {
            "left": left,
            "right": right,
            "similarity": similarity,
            "left_cui": pair.get("left_cui"),
            "right_cui": pair.get("right_cui"),
            "modifier_conflicts": pair.get("modifier_conflicts", []),
            "method": "sapbert",
        }
        if left not in entity_names or right not in entity_names:
            rejections.add("sapbert_entity_not_in_snapshot", base_record)
            continue
        if similarity < args.threshold:
            rejections.add("sapbert_below_threshold", base_record)
            continue
        if not pair.get("lexical_equivalent"):
            rejections.add("sapbert_not_lexical_equivalent", base_record)
            continue
        if pair.get("modifier_conflicts"):
            modifier_conflict_counts.update(pair["modifier_conflicts"])
            rejections.add("sapbert_modifier_conflict", base_record)
            continue
        if dsu.find(left) == dsu.find(right):
            rejections.add("sapbert_already_connected", base_record)
            continue
        left_members = dsu.component_members(left)
        right_members = dsu.component_members(right)
        if len(dsu.component_cuis(left) | dsu.component_cuis(right)) > 1:
            rejections.add("sapbert_conflicting_confident_cui", base_record)
            continue
        if incompatible_types(left_members, right_members, graph_metadata):
            rejections.add("sapbert_incompatible_entity_types", base_record)
            continue
        dsu.union(left, right)
        accepted_edges.append(
            AcceptedEdge(left, right, "sapbert_lexical_equivalent", similarity=similarity)
        )
        accepted_by_method["sapbert"] += 1

    components = dsu.components()
    edges_by_root: dict[str, list[AcceptedEdge]] = defaultdict(list)
    for edge in accepted_edges:
        edges_by_root[dsu.find(edge.left)].append(edge)

    plans: list[dict[str, Any]] = []
    grouped_disease_counts: Counter[str] = Counter()
    extracted_disease_counts: Counter[str] = Counter()
    risk_counts: Counter[str] = Counter()
    component_method_counts: Counter[str] = Counter()
    entity_type_counts: Counter[str] = Counter()

    empty_label = DocumentLabel(None, (), ())
    for names in components:
        all_document_ids = set().union(*(entity_documents[name] for name in names))
        grouped_diseases = sorted(
            {
                disease
                for doc_id in all_document_ids
                for disease in document_labels.get(doc_id, empty_label).grouped_diseases
            },
            key=str.casefold,
        )
        extracted_diseases = sorted(
            {
                disease
                for doc_id in all_document_ids
                for disease in document_labels.get(doc_id, empty_label).extracted_diseases
            },
            key=str.casefold,
        )
        types = sorted(
            {
                graph_metadata.get(name, EntityMetadata("", "")).entity_type
                for name in names
                if graph_metadata.get(name, EntityMetadata("", "")).entity_type
            },
            key=str.casefold,
        )
        canonical, canonical_reason, canonical_scores = choose_canonical(
            names, entity_documents, confident_cuis
        )
        variant_disease_sets = [
            {
                disease
                for doc_id in entity_documents[name]
                for disease in document_labels.get(doc_id, empty_label).grouped_diseases
            }
            for name in names
        ]
        disjoint_disease_variants = any(
            left_set.isdisjoint(right_set)
            for left_index, left_set in enumerate(variant_disease_sets)
            for right_set in variant_disease_sets[left_index + 1 :]
            if left_set and right_set
        )
        risks = []
        concrete_types = {
            normalize_entity_type(value)
            for value in types
            if normalize_entity_type(value) not in GENERIC_ENTITY_TYPES
        }
        if len(concrete_types) > 1:
            risks.append("entity_type_disagreement")
        if len(grouped_diseases) > 1:
            risks.append("spans_multiple_grouped_diseases")
        if disjoint_disease_variants:
            risks.append("disjoint_grouped_disease_provenance")
        if len(names) >= 5:
            risks.append("large_transitive_component")
        if any(not document_labels.get(doc_id, empty_label).grouped_diseases for doc_id in all_document_ids):
            risks.append("missing_grouped_disease_provenance")
        risk_counts.update(risks)
        grouped_disease_counts.update(grouped_diseases)
        extracted_disease_counts.update(extracted_diseases)

        component_edges = sorted(
            edges_by_root[dsu.find(names[0])],
            key=lambda edge: (edge.method, edge.left.casefold(), edge.right.casefold()),
        )
        component_methods = {edge.method for edge in component_edges}
        if component_methods == {"quickumls_same_unique_cui"}:
            component_method = "quickumls_only"
        elif component_methods == {"sapbert_lexical_equivalent"}:
            component_method = "sapbert_only"
        else:
            component_method = "combined"
        component_method_counts[component_method] += 1
        entity_type_counts.update(types or ["<missing>"])
        variant_document_sets = [entity_documents[name] for name in names]
        adds_document_provenance = len(all_document_ids) > 1 and not any(
            doc_ids == all_document_ids for doc_ids in variant_document_sets
        )
        adds_disease_provenance = len(grouped_diseases) > 1 and not any(
            diseases == set(grouped_diseases) for diseases in variant_disease_sets
        )
        variants = [
            {
                "entity_name": name,
                "entity_type": graph_metadata.get(name, EntityMetadata("", "")).entity_type,
                "document_count": len(entity_documents[name]),
                "document_ids": sorted(entity_documents[name]),
                "confident_unique_cui": confident_cuis.get(name),
                "grouped_diseases": sorted(variant_disease_sets[index], key=str.casefold),
                "extracted_diseases": sorted(
                    {
                        disease
                        for doc_id in entity_documents[name]
                        for disease in document_labels.get(
                            doc_id, empty_label
                        ).extracted_diseases
                    },
                    key=str.casefold,
                ),
            }
            for index, name in enumerate(names)
        ]
        documents = [
            {
                "document_id": doc_id,
                "file_path": document_labels.get(doc_id, empty_label).file_path,
                "grouped_diseases": list(
                    document_labels.get(doc_id, empty_label).grouped_diseases
                ),
                "extracted_diseases": list(
                    document_labels.get(doc_id, empty_label).extracted_diseases
                ),
            }
            for doc_id in sorted(all_document_ids)
        ]
        plans.append(
            {
                "component_id": stable_component_id(names),
                "status": "proposed",
                "canonical_entity": canonical,
                "canonical_reason": canonical_reason,
                "canonical_ranking": canonical_scores,
                "source_entities": [name for name in names if name != canonical],
                "entity_names": names,
                "entity_count": len(names),
                "proposed_reduction": len(names) - 1,
                "component_method": component_method,
                "entity_types": types,
                "quickumls_cuis": sorted(
                    {confident_cuis[name] for name in names if name in confident_cuis}
                ),
                "document_count": len(all_document_ids),
                "document_ids": sorted(all_document_ids),
                "grouped_diseases": grouped_diseases,
                "extracted_diseases": extracted_diseases,
                "risk_flags": risks,
                "adds_document_provenance": adds_document_provenance,
                "adds_grouped_disease_provenance": adds_disease_provenance,
                "evidence": [edge.as_dict() for edge in component_edges],
                "variants": variants,
                "documents": documents,
            }
        )

    plans.sort(key=lambda row: (row["component_id"], row["canonical_entity"].casefold()))
    total_reduction = sum(row["proposed_reduction"] for row in plans)
    entity_count = len(entity_names)
    summary = {
        "configuration": {
            "storage": str(args.storage),
            "umls_evaluation": str(args.umls_evaluation),
            "sapbert_candidates": str(args.sapbert_candidates),
            "sapbert_threshold": args.threshold,
            "read_only": True,
        },
        "totals": {
            "entity_count": entity_count,
            "proposed_component_count": len(plans),
            "entities_in_proposed_components": sum(row["entity_count"] for row in plans),
            "proposed_reduction": total_reduction,
            "proposed_reduction_percentage": round(
                100 * total_reduction / entity_count, 4
            )
            if entity_count
            else 0.0,
            "accepted_quickumls_edges": accepted_by_method["quickumls"],
            "accepted_sapbert_edges": accepted_by_method["sapbert"],
        },
        "umls_status_counts": dict(sorted(umls_status_counts.items())),
        "rejection_counts": dict(sorted(rejections.counts.items())),
        "component_method_counts": dict(sorted(component_method_counts.items())),
        "component_size_distribution": count_distribution(
            row["entity_count"] for row in plans
        ),
        "entity_type_counts": dict(sorted(entity_type_counts.items())),
        "modifier_conflict_counts": dict(sorted(modifier_conflict_counts.items())),
        "risk_flag_counts": dict(sorted(risk_counts.items())),
        "components_spanning_multiple_documents": sum(
            row["document_count"] > 1 for row in plans
        ),
        "components_spanning_multiple_grouped_diseases": sum(
            len(row["grouped_diseases"]) > 1 for row in plans
        ),
        "components_adding_document_provenance": sum(
            row["adds_document_provenance"] for row in plans
        ),
        "components_adding_grouped_disease_provenance": sum(
            row["adds_grouped_disease_provenance"] for row in plans
        ),
        "top_grouped_diseases": grouped_disease_counts.most_common(30),
        "top_extracted_diseases": extracted_disease_counts.most_common(30),
        "high_risk_components": [
            {
                "component_id": row["component_id"],
                "canonical_entity": row["canonical_entity"],
                "entity_names": row["entity_names"],
                "risk_flags": row["risk_flags"],
                "document_count": row["document_count"],
                "grouped_diseases": row["grouped_diseases"],
            }
            for row in sorted(
                (row for row in plans if row["risk_flags"]),
                key=lambda row: (
                    -len(row["risk_flags"]),
                    -row["document_count"],
                    row["component_id"],
                ),
            )[:100]
        ],
    }
    verify_inputs_unchanged(snapshots)
    manifest = {
        "analysis_only": True,
        "storage_modified": False,
        "input_files": snapshots,
        "outputs": {
            "merge_plan": "merge_plan.jsonl",
            "summary": "merge_plan_summary.json",
            "rejected_pairs": "rejected_pairs.jsonl",
            "manifest": "manifest.json",
        },
    }
    return plans, summary, rejections, snapshots, manifest


def main() -> int:
    args = parse_args()
    plans, summary, rejections, _snapshots, manifest = analyze(args)
    write_jsonl_atomic(args.output_dir / "merge_plan.jsonl", plans)
    write_json_atomic(args.output_dir / "merge_plan_summary.json", summary)
    write_jsonl_atomic(args.output_dir / "rejected_pairs.jsonl", rejections.rows())
    write_json_atomic(args.output_dir / "manifest.json", manifest)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
