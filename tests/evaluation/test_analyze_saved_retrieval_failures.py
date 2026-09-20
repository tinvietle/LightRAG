"""Tests for saved retrieval-failure attribution."""

from __future__ import annotations

import json

from lightrag.evaluation.analyze_saved_retrieval_failures import analyze


def _chunk(label: str) -> dict[str, str]:
    return {
        "content": json.dumps(
            {
                "extracted_disease_name": label,
                "grouped_disease_name": label,
            }
        )
    }


def _case(number: int, truth: str, entities: list[str], labels: list[str]) -> dict:
    return {
        "test_number": number,
        "file_name": f"case-{number}.json",
        "ground_truth": truth,
        "retrieval": {
            "entities": [{"entity_name": name} for name in entities],
            "chunks": [_chunk(label) for label in labels],
        },
        "retrieval_metadata": {
            "keywords": {"high_level": [], "low_level": []},
            "processing_info": {
                "total_entities_found": 10,
                "entities_after_truncation": 5,
                "merged_chunks_count": 20,
                "final_chunks_count": len(labels),
            },
        },
    }


def test_analyze_assigns_pipeline_failure_stages() -> None:
    artifact = {
        "results": [
            _case(1, "Disease A", ["Disease A"], ["Disease A"]),
            _case(2, "Disease B", [], ["Other"]),
            _case(3, "Disease C", [], ["Other"]),
            _case(4, "Disease D", ["Disease D"], ["Other"]),
        ]
    }
    corpus = {
        "disease a": {"a.json"},
        "disease c": {"c.json"},
        "disease d": {"d.json"},
    }
    graph = {"disease a", "disease c", "disease d"}

    report = analyze(artifact, corpus, graph)

    assert report["category_counts"] == {
        "exact_retrieval_success": 1,
        "exact_label_absent_from_indexed_corpus": 1,
        "disease_entity_not_retrieved": 1,
        "entity_retrieved_but_relevant_chunk_not_selected": 1,
    }
