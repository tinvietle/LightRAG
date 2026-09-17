from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


SCRIPT = Path(__file__).parents[2] / "next_step/analyze_entity_merge_provenance.py"
SPEC = importlib.util.spec_from_file_location("analyze_entity_merge_provenance", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_extract_document_label_handles_concatenated_json() -> None:
    record = {
        "file_path": "case.json",
        "content": (
            '[{"grouped_disease_name":"Abscess",'
            '"extracted_disease_name":"Brain abscess"}]\n'
            '[{"grouped_disease_name":"Abscess",'
            '"extracted_disease_name":"Lung abscess"}]'
        ),
    }

    label = MODULE.extract_document_label(record)

    assert label.file_path == "case.json"
    assert label.grouped_diseases == ("Abscess",)
    assert label.extracted_diseases == ("Brain abscess", "Lung abscess")


def test_build_components_combines_umls_then_lexical_pairs(tmp_path: Path) -> None:
    umls = tmp_path / "umls.jsonl"
    umls.write_text(
        '\n'.join(
            [
                '{"entity_name":"MRI","auto_lexical_candidate":true,"best_cuis":["C1"]}',
                '{"entity_name":"magnetic resonance imaging","auto_lexical_candidate":true,"best_cuis":["C1"]}',
                '{"entity_name":"CT","auto_lexical_candidate":false,"best_cuis":[]}',
            ]
        ),
        encoding="utf-8",
    )
    sapbert = tmp_path / "sapbert.jsonl"
    sapbert.write_text(
        '{"left":"CT","right":"ct","similarity":0.999,'
        '"lexical_equivalent":true,"modifier_conflicts":[]}\n',
        encoding="utf-8",
    )

    components, umls_reductions, lexical_reductions, blocked = MODULE.build_components(
        {"MRI", "magnetic resonance imaging", "CT", "ct"}, umls, sapbert, 0.99
    )

    assert umls_reductions == 1
    assert lexical_reductions == 1
    assert blocked == 0
    assert {frozenset(group) for group in components} == {
        frozenset({"MRI", "magnetic resonance imaging"}),
        frozenset({"CT", "ct"}),
    }


def test_document_id_from_chunk_preserves_doc_prefix() -> None:
    assert MODULE.document_id_from_chunk("doc-abc-chunk-003") == "doc-abc"


def test_disjoint_set_rejects_conflicting_cuis() -> None:
    groups = MODULE.DisjointSet(["A", "a"])
    groups.add_cui("A", "C1")
    groups.add_cui("a", "C2")

    assert not groups.union("A", "a", reject_cui_conflict=True)
    assert groups.components() == []
