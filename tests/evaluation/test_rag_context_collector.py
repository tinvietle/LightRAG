"""Tests for raw LightRAG retrieval-context collection."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from lightrag.evaluation.eval_rag_context_differential_ollama import (
    RAGContextCollector,
)


def _dataset(path: Path) -> Path:
    path.write_text(
        json.dumps(
            [
                {
                    "question": "What is the most likely diagnosis?",
                    "ground_truth": "Liver abscess",
                    "file_name": "case.json",
                    "image_path": [],
                }
            ]
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def test_retrieve_context_preserves_answer_and_detailed_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVAL_QUERY_MODE", "hybrid")
    monkeypatch.setenv("EVAL_QUERY_TOP_K", "12")
    monkeypatch.setenv("EVAL_CHUNK_TOP_K", "7")
    monkeypatch.delenv("EVAL_DIFFERENTIAL_USER_PROMPT", raising=False)
    collector = RAGContextCollector(
        test_dataset_path=str(_dataset(tmp_path / "dataset.json")),
        rag_api_url="http://testserver",
        include_images=False,
    )
    api_response = {
        "status": "success",
        "message": "Query executed successfully",
        "llm_response": {
            "content": "The leading diagnosis is liver abscess.",
            "response_iterator": None,
            "is_streaming": False,
        },
        "data": {
            "entities": [{"entity_name": "Liver abscess", "score": 0.99}],
            "relationships": [{"src_id": "Drainage", "tgt_id": "Liver abscess"}],
            "chunks": [
                {
                    "chunk_id": "chunk-1",
                    "file_path": "train_case.json",
                    "reference_id": "1",
                    "content": "Evidence",
                    "custom_metadata": "kept",
                }
            ],
            "references": [
                {"reference_id": "1", "file_path": "train_case.json"}
            ],
        },
        "metadata": {
            "keywords": {"high_level": ["abscess"], "low_level": ["liver"]},
            "processing_info": {"total_entities_found": 4},
        },
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/query/full"
        payload = json.loads(request.content)
        assert payload["query"] == "What is the most likely diagnosis?"
        assert payload["mode"] == "hybrid"
        assert payload["top_k"] == 12
        assert payload["chunk_top_k"] == 7
        assert "user_prompt" not in payload
        return httpx.Response(200, json=api_response)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await collector.retrieve_context(
            question="What is the most likely diagnosis?",
            client=client,
        )

    assert result["answer"] == "The leading diagnosis is liver abscess."
    assert result["contexts"] == ["Evidence"]
    assert result["retrieved_chunks"][0]["custom_metadata"] == "kept"
    assert result["retrieval"]["entities"][0]["rank"] == 1
    assert result["retrieval"]["relationships"][0]["rank"] == 1
    assert result["references"][0]["rank"] == 1
    assert result["retrieval_metadata"] == api_response["metadata"]


@pytest.mark.asyncio
async def test_collect_single_case_keeps_source_case_and_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = RAGContextCollector(
        test_dataset_path=str(_dataset(tmp_path / "dataset.json")),
        rag_api_url="http://testserver",
        include_images=False,
    )

    async def fake_retrieve_context(**_: object) -> dict[str, object]:
        return {
            "answer": "Liver abscess is the leading diagnosis.",
            "contexts": ["Supporting context"],
            "retrieved_chunks": [{"chunk_id": "chunk-1"}],
            "references": [{"reference_id": "1"}],
            "retrieval": {
                "entities": [],
                "relationships": [],
                "chunks": [{"rank": 1, "chunk_id": "chunk-1"}],
                "references": [{"rank": 1, "reference_id": "1"}],
            },
            "retrieval_metadata": {"processing_info": {"final_chunks_count": 1}},
        }

    monkeypatch.setattr(collector, "retrieve_context", fake_retrieve_context)
    async with httpx.AsyncClient() as client:
        result = await collector.collect_single_case(
            1,
            collector.test_cases[0],
            asyncio.Semaphore(1),
            client,
        )

    assert result["ground_truth"] == "Liver abscess"
    assert result["file_name"] == "case.json"
    assert result["answer"] == "Liver abscess is the leading diagnosis."
    assert result["retrieved_contexts"] == ["Supporting context"]
    assert result["retrieval"]["chunks"][0]["rank"] == 1
    assert "query_configuration" in result
    assert "experiment" in result
    assert "request_latency_seconds" in result
    assert "metrics" not in result
    assert "missing_metrics" not in result
    assert result["error"] is None
