"""Tests for the single-pass answer plus retrieval-data endpoint."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI


@pytest.mark.asyncio
async def test_query_full_returns_answer_and_exact_retrieval_data() -> None:
    original_argv = sys.argv.copy()
    sys.argv = ["lightrag-server"]
    try:
        from lightrag.api.routers.query_routes import create_query_routes
    finally:
        sys.argv = original_argv

    captured: dict[str, object] = {}
    raw_result = {
        "status": "success",
        "message": "Query executed successfully",
        "data": {
            "entities": [{"entity_name": "Liver abscess", "score": 0.99}],
            "relationships": [{"src_id": "Drainage", "tgt_id": "Liver abscess"}],
            "chunks": [{"chunk_id": "chunk-1", "content": "Evidence"}],
            "references": [{"reference_id": "1", "file_path": "case.json"}],
        },
        "metadata": {
            "keywords": {"high_level": ["abscess"], "low_level": ["liver"]},
            "processing_info": {"final_chunks_count": 1},
        },
        "llm_response": {
            "content": "The leading diagnosis is liver abscess.",
            "response_iterator": object(),
            "is_streaming": False,
        },
    }

    async def aquery_llm(query: str, param: object) -> dict[str, object]:
        captured["query"] = query
        captured["param"] = param
        return raw_result

    rag = SimpleNamespace(aquery_llm=aquery_llm)
    app = FastAPI()
    app.include_router(create_query_routes(rag))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/query/full",
            json={"query": "What is the likely diagnosis?", "mode": "hybrid"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["llm_response"]["content"] == (
        "The leading diagnosis is liver abscess."
    )
    assert body["llm_response"]["response_iterator"] is None
    assert body["data"] == raw_result["data"]
    assert body["metadata"] == raw_result["metadata"]
    assert captured["query"] == "What is the likely diagnosis?"
    assert captured["param"].stream is False
