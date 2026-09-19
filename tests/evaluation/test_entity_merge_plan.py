from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys

import pytest


SCRIPT = Path(__file__).parents[2] / "next_step/build_entity_merge_plan.py"
SPEC = importlib.util.spec_from_file_location("build_entity_merge_plan", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


def write_graphml(path: Path, entity_types: dict[str, str]) -> None:
    nodes = "".join(
        f'<node id="{name}"><data key="type">{entity_type}</data>'
        f'<data key="description">Description for {name}</data></node>'
        for name, entity_type in entity_types.items()
    )
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">'
        '<key id="type" for="node" attr.name="entity_type" attr.type="string"/>'
        '<key id="description" for="node" attr.name="description" '
        'attr.type="string"/>'
        f'<graph edgedefault="undirected">{nodes}</graph></graphml>',
        encoding="utf-8",
    )


def analysis_args(
    storage: Path, umls: Path, sapbert: Path, output_dir: Path
) -> argparse.Namespace:
    return argparse.Namespace(
        storage=storage,
        umls_evaluation=umls,
        sapbert_candidates=sapbert,
        threshold=0.99,
        output_dir=output_dir,
        max_rejections_per_reason=2,
        skip_input_hashes=True,
    )


def test_stable_component_id_is_order_independent() -> None:
    assert MODULE.stable_component_id(["MRI", "magnetic resonance imaging"]) == (
        MODULE.stable_component_id(["magnetic resonance imaging", "MRI"])
    )


def test_choose_canonical_prefers_document_frequency_then_non_abbreviation() -> None:
    canonical, reason, ranking = MODULE.choose_canonical(
        ["MRI", "magnetic resonance imaging"],
        {
            "MRI": {"doc-1"},
            "magnetic resonance imaging": {"doc-1", "doc-2"},
        },
        {"MRI": "C1", "magnetic resonance imaging": "C1"},
    )

    assert canonical == "magnetic resonance imaging"
    assert reason == "highest_document_frequency"
    assert ranking[0]["entity_name"] == canonical

    canonical, reason, _ = MODULE.choose_canonical(
        ["MRI", "magnetic resonance imaging"],
        {"MRI": {"doc-1"}, "magnetic resonance imaging": {"doc-1"}},
        {"MRI": "C1", "magnetic resonance imaging": "C1"},
    )
    assert canonical == "magnetic resonance imaging"
    assert reason == "non_abbreviation_tiebreak"


def test_rejection_collector_counts_all_but_bounds_examples() -> None:
    collector = MODULE.RejectionCollector(limit_per_reason=2)
    for index in range(5):
        collector.add("reason", {"index": index})

    assert collector.counts["reason"] == 5
    assert list(collector.rows()) == [
        {"reason": "reason", "index": 0},
        {"reason": "reason", "index": 1},
    ]


def test_active_document_ids_blocks_unfinished_ingestion() -> None:
    assert MODULE.active_document_ids(
        {
            "done": {"status": "processed"},
            "failed": {"status": "failed"},
            "one": {"status": "pending"},
            "two": {"status": "PROCESSING"},
        }
    ) == ["one", "two"]


def test_analyze_refuses_output_inside_storage(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    args = analysis_args(
        storage,
        tmp_path / "umls.jsonl",
        tmp_path / "sapbert.jsonl",
        storage / "analysis-output",
    )

    with pytest.raises(ValueError, match="outside the LightRAG storage"):
        MODULE.analyze(args)


def test_analyze_builds_read_only_plan_and_reports_rejections(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    entity_chunks = {
        "MRI": {"chunk_ids": ["doc-1-chunk-000"]},
        "magnetic resonance imaging": {
            "chunk_ids": ["doc-1-chunk-000", "doc-2-chunk-000"]
        },
        "CT": {"chunk_ids": ["chunk-old-a"]},
        "ct": {"chunk_ids": ["chunk-old-b"]},
        "Disease A": {"chunk_ids": ["doc-1-chunk-000"]},
        "Drug A": {"chunk_ids": ["doc-2-chunk-000"]},
        "left lesion": {"chunk_ids": ["doc-1-chunk-000"]},
        "right lesion": {"chunk_ids": ["doc-2-chunk-000"]},
        "Concept A": {"chunk_ids": ["doc-1-chunk-000"]},
        "Concept B": {"chunk_ids": ["doc-2-chunk-000"]},
    }
    full_docs = {
        "doc-1": {
            "file_path": "one.json",
            "content": '{"grouped_disease_name":"Group 1",'
            '"extracted_disease_name":"Disease 1"}',
        },
        "doc-2": {
            "file_path": "two.json",
            "content": '{"grouped_disease_name":"Group 2",'
            '"extracted_disease_name":"Disease 2"}',
        },
    }
    write_json(storage / "kv_store_entity_chunks.json", entity_chunks)
    write_json(storage / "kv_store_full_docs.json", full_docs)
    write_json(
        storage / "kv_store_text_chunks.json",
        {
            "doc-1-chunk-000": {"full_doc_id": "doc-1"},
            "doc-2-chunk-000": {"full_doc_id": "doc-2"},
            "chunk-old-a": {"full_doc_id": "doc-1"},
            "chunk-old-b": {"full_doc_id": "doc-2"},
        },
    )
    write_json(
        storage / "kv_store_doc_status.json",
        {
            "doc-1": {"status": "processed"},
            "doc-2": {"status": "processed"},
        },
    )
    write_graphml(
        storage / "graph_chunk_entity_relation.graphml",
        {
            "MRI": "Diagnostic_procedure",
            "magnetic resonance imaging": "Diagnostic_procedure",
            "CT": "Diagnostic_procedure",
            "ct": "Diagnostic_procedure",
            "Disease A": "Disease_disorder",
            "Drug A": "Medication",
            "left lesion": "Disease_disorder",
            "right lesion": "Disease_disorder",
            "Concept A": "Disease_disorder",
            "Concept B": "Disease_disorder",
        },
    )

    umls = tmp_path / "umls.jsonl"
    write_jsonl(
        umls,
        [
            {
                "entity_name": "MRI",
                "auto_lexical_candidate": True,
                "best_cuis": ["C1"],
            },
            {
                "entity_name": "magnetic resonance imaging",
                "auto_lexical_candidate": True,
                "best_cuis": ["C1"],
            },
            {
                "entity_name": "Concept A",
                "auto_lexical_candidate": True,
                "best_cuis": ["C2"],
            },
            {
                "entity_name": "Concept B",
                "auto_lexical_candidate": True,
                "best_cuis": ["C3"],
            },
        ],
    )
    sapbert = tmp_path / "sapbert.jsonl"
    write_jsonl(
        sapbert,
        [
            {
                "left": "CT",
                "right": "ct",
                "similarity": 1.0,
                "lexical_equivalent": True,
                "modifier_conflicts": [],
            },
            {
                "left": "Disease A",
                "right": "Drug A",
                "similarity": 1.0,
                "lexical_equivalent": True,
                "modifier_conflicts": [],
            },
            {
                "left": "left lesion",
                "right": "right lesion",
                "similarity": 1.0,
                "lexical_equivalent": True,
                "modifier_conflicts": ["laterality"],
            },
            {
                "left": "Concept A",
                "right": "Concept B",
                "similarity": 1.0,
                "lexical_equivalent": True,
                "modifier_conflicts": [],
                "left_cui": "C2",
                "right_cui": "C3",
            },
        ],
    )

    plans, summary, rejections, snapshots, manifest = MODULE.analyze(
        analysis_args(storage, umls, sapbert, tmp_path / "output")
    )

    assert len(plans) == 2
    assert {frozenset(row["entity_names"]) for row in plans} == {
        frozenset({"MRI", "magnetic resonance imaging"}),
        frozenset({"CT", "ct"}),
    }
    mri_plan = next(row for row in plans if "MRI" in row["entity_names"])
    assert mri_plan["canonical_entity"] == "magnetic resonance imaging"
    assert mri_plan["proposed_reduction"] == 1
    assert mri_plan["status"] == "proposed"
    assert "spans_multiple_grouped_diseases" in mri_plan["risk_flags"]
    assert {document["file_path"] for document in mri_plan["documents"]} == {
        "one.json",
        "two.json",
    }
    assert mri_plan["variants"][0]["extracted_diseases"]
    ct_plan = next(row for row in plans if "CT" in row["entity_names"])
    assert ct_plan["grouped_diseases"] == ["Group 1", "Group 2"]
    assert summary["totals"]["proposed_reduction"] == 2
    assert summary["totals"]["accepted_quickumls_edges"] == 1
    assert summary["totals"]["accepted_sapbert_edges"] == 1
    assert summary["component_method_counts"] == {
        "quickumls_only": 1,
        "sapbert_only": 1,
    }
    assert summary["components_adding_document_provenance"] == 1
    assert summary["modifier_conflict_counts"] == {"laterality": 1}
    assert rejections.counts["sapbert_incompatible_entity_types"] == 1
    assert rejections.counts["sapbert_modifier_conflict"] == 1
    assert rejections.counts["sapbert_conflicting_confident_cui"] == 1
    assert len(snapshots) == 7
    assert manifest["analysis_only"] is True
    assert manifest["storage_modified"] is False


def test_analysis_source_never_calls_merge_api() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert ".amerge_entities(" not in source
    assert ".merge_entities(" not in source
