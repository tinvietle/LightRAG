"""Regression coverage for the ignore_hl_keywords / ignore_ll_keywords switches."""

from unittest.mock import AsyncMock, patch

import pytest

from lightrag import operate
from lightrag.base import QueryContextResult, QueryParam
from lightrag.operate import get_keywords_from_query

LLM_KEYWORDS = (["hl theme"], ["ll finding", "ll diagnosis"])


def _config(**overrides) -> dict:
    config = {"enable_query_ner_hints": False}
    config.update(overrides)
    return config


@pytest.mark.offline
@pytest.mark.asyncio
async def test_keywords_are_returned_unchanged_by_default():
    extract = AsyncMock(return_value=LLM_KEYWORDS)
    with patch("lightrag.operate.extract_keywords_only", extract):
        hl, ll = await get_keywords_from_query("q", QueryParam(), _config())

    assert (hl, ll) == LLM_KEYWORDS
    extract.assert_awaited_once()


@pytest.mark.offline
@pytest.mark.asyncio
async def test_ignore_hl_keeps_only_low_level_keywords():
    extract = AsyncMock(return_value=LLM_KEYWORDS)
    with patch("lightrag.operate.extract_keywords_only", extract):
        hl, ll = await get_keywords_from_query(
            "q", QueryParam(ignore_hl_keywords=True), _config()
        )

    assert hl == []
    assert ll == LLM_KEYWORDS[1]


@pytest.mark.offline
@pytest.mark.asyncio
async def test_ignore_ll_keeps_only_high_level_keywords():
    extract = AsyncMock(return_value=LLM_KEYWORDS)
    with patch("lightrag.operate.extract_keywords_only", extract):
        hl, ll = await get_keywords_from_query(
            "q", QueryParam(ignore_ll_keywords=True), _config()
        )

    assert hl == LLM_KEYWORDS[0]
    assert ll == []


@pytest.mark.offline
@pytest.mark.asyncio
async def test_ignoring_both_skips_the_keyword_llm_call():
    extract = AsyncMock(return_value=LLM_KEYWORDS)
    with patch("lightrag.operate.extract_keywords_only", extract):
        hl, ll = await get_keywords_from_query(
            "q",
            QueryParam(ignore_hl_keywords=True, ignore_ll_keywords=True),
            _config(),
        )

    assert (hl, ll) == ([], [])
    extract.assert_not_awaited()


@pytest.mark.offline
@pytest.mark.asyncio
async def test_ignore_ll_does_not_run_query_ner_hints():
    extract = AsyncMock(return_value=LLM_KEYWORDS)
    ner = AsyncMock(return_value=["hint"])
    with (
        patch("lightrag.operate.extract_keywords_only", extract),
        patch("lightrag.operate._get_query_ner_suggestions", ner),
    ):
        _, ll = await get_keywords_from_query(
            "q",
            QueryParam(ignore_ll_keywords=True),
            _config(enable_query_ner_hints=True),
        )

    assert ll == []
    ner.assert_not_awaited()


@pytest.mark.offline
@pytest.mark.asyncio
async def test_ner_hints_still_added_when_only_hl_is_ignored():
    extract = AsyncMock(return_value=LLM_KEYWORDS)
    ner = AsyncMock(return_value=["hint"])
    with (
        patch("lightrag.operate.extract_keywords_only", extract),
        patch("lightrag.operate._get_query_ner_suggestions", ner),
    ):
        hl, ll = await get_keywords_from_query(
            "q",
            QueryParam(ignore_hl_keywords=True),
            _config(enable_query_ner_hints=True),
        )

    assert hl == []
    assert ll == [*LLM_KEYWORDS[1], "hint"]


@pytest.mark.offline
@pytest.mark.asyncio
async def test_caller_supplied_keywords_are_also_filtered():
    extract = AsyncMock(return_value=LLM_KEYWORDS)
    param = QueryParam(
        hl_keywords=["given hl"], ll_keywords=["given ll"], ignore_hl_keywords=True
    )
    with patch("lightrag.operate.extract_keywords_only", extract):
        hl, ll = await get_keywords_from_query("q", param, _config())

    assert (hl, ll) == ([], ["given ll"])
    extract.assert_not_awaited()


@pytest.mark.offline
@pytest.mark.asyncio
async def test_kg_query_with_all_keywords_ignored_still_retrieves_on_raw_query(
    monkeypatch: pytest.MonkeyPatch,
):
    """Empty-by-request keywords must not trigger the fail response."""
    long_query = ("patient with fever and rash " * 5).strip()  # >= 50 chars
    built: list[tuple[str, str, str]] = []

    async def fake_build_context(query, ll_keywords, hl_keywords, *args, **kwargs):
        built.append((query, ll_keywords, hl_keywords))
        return QueryContextResult(context="retrieved context", raw_data={})

    async def unused_llm(*args, **kwargs):
        return "unused"

    monkeypatch.setattr(operate, "_build_query_context", fake_build_context)
    extract = AsyncMock(return_value=LLM_KEYWORDS)
    monkeypatch.setattr(operate, "extract_keywords_only", extract)

    result = await operate.kg_query(
        long_query,
        knowledge_graph_inst=None,
        entities_vdb=None,
        relationships_vdb=None,
        text_chunks_db=None,
        query_param=QueryParam(
            mode="mix",
            only_need_context=True,
            ignore_hl_keywords=True,
            ignore_ll_keywords=True,
        ),
        global_config={
            "role_llm_funcs": {"query": unused_llm, "vlm": None},
            "tokenizer": None,
        },
    )

    assert result is not None
    assert result.content == "retrieved context"
    assert built == [(long_query, "", "")]
    extract.assert_not_awaited()
