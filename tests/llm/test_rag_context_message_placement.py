"""Regression tests for optional KG-RAG context message placement."""

import pytest

from lightrag import operate
from lightrag.base import QueryContextResult, QueryParam


class _Tokenizer:
    def encode(self, text: str) -> list[str]:
        return text.split()


async def _fake_keywords(*args: object, **kwargs: object) -> tuple[list[str], list[str]]:
    return [], ["clinical case"]


async def _fake_context(*args: object, **kwargs: object) -> QueryContextResult:
    return QueryContextResult(context="RETRIEVED CONTEXT", raw_data={})


def test_context_in_user_message_is_enabled_by_default() -> None:
    assert QueryParam().context_in_user_message is True


@pytest.mark.asyncio
async def test_prompt_preview_moves_context_to_user_message_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(operate, "get_keywords_from_query", _fake_keywords)
    monkeypatch.setattr(operate, "_build_query_context", _fake_context)

    async def unused_query_llm(*args: object, **kwargs: object) -> str:
        raise AssertionError("only_need_prompt must not call the query LLM")

    result = await operate.kg_query(
        "What is the diagnosis?",
        knowledge_graph_inst=None,
        entities_vdb=None,
        relationships_vdb=None,
        text_chunks_db=None,
        query_param=QueryParam(
            mode="mix", only_need_prompt=True, context_in_user_message=True
        ),
        global_config={
            "role_llm_funcs": {"query": unused_query_llm, "vlm": None},
            "llm_model_name": "test-model",
        },
    )

    system_prompt, user_prompt = result.content.split("---User Prompt---", maxsplit=1)
    assert "---System Prompt---" in system_prompt
    assert "RETRIEVED CONTEXT" not in system_prompt
    assert "---Retrieved Context---" in user_prompt
    assert "RETRIEVED CONTEXT" in user_prompt
    assert "---User Query---" in user_prompt
    assert "What is the diagnosis?" in user_prompt


@pytest.mark.asyncio
async def test_explicit_legacy_layout_keeps_context_in_system_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(operate, "get_keywords_from_query", _fake_keywords)
    monkeypatch.setattr(operate, "_build_query_context", _fake_context)

    async def unused_query_llm(*args: object, **kwargs: object) -> str:
        raise AssertionError("only_need_prompt must not call the query LLM")

    result = await operate.kg_query(
        "What is the diagnosis?",
        knowledge_graph_inst=None,
        entities_vdb=None,
        relationships_vdb=None,
        text_chunks_db=None,
        query_param=QueryParam(
            mode="mix", only_need_prompt=True, context_in_user_message=False
        ),
        global_config={
            "role_llm_funcs": {"query": unused_query_llm, "vlm": None},
            "llm_model_name": "test-model",
        },
    )

    system_prompt, user_prompt = result.content.split("---User Prompt---", maxsplit=1)
    assert "RETRIEVED CONTEXT" in system_prompt
    assert "RETRIEVED CONTEXT" not in user_prompt


@pytest.mark.asyncio
async def test_layout_selection_changes_query_cache_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_hash_inputs: list[tuple[object, ...]] = []
    captured_calls: list[tuple[str, str | None]] = []

    monkeypatch.setattr(operate, "get_keywords_from_query", _fake_keywords)
    monkeypatch.setattr(operate, "_build_query_context", _fake_context)

    async def no_cache(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(operate, "handle_cache", no_cache)

    def capture_hash(*args: object) -> str:
        cache_hash_inputs.append(args)
        return f"hash-{len(cache_hash_inputs)}"

    monkeypatch.setattr(operate, "compute_args_hash", capture_hash)

    async def fake_query_llm(prompt: str, **kwargs: object) -> str:
        captured_calls.append((prompt, kwargs.get("system_prompt")))
        return "answer"

    global_config = {
        "role_llm_funcs": {"query": fake_query_llm, "vlm": None},
        "llm_model_name": "test-model",
        "tokenizer": _Tokenizer(),
    }

    for context_in_user_message in (False, True):
        await operate.kg_query(
            "What is the diagnosis?",
            knowledge_graph_inst=None,
            entities_vdb=None,
            relationships_vdb=None,
            text_chunks_db=None,
            query_param=QueryParam(
                mode="mix", context_in_user_message=context_in_user_message
            ),
            global_config=global_config,
        )

    assert cache_hash_inputs[0] != cache_hash_inputs[1]
    assert "RETRIEVED CONTEXT" not in captured_calls[0][0]
    assert "RETRIEVED CONTEXT" in captured_calls[0][1]
    assert "RETRIEVED CONTEXT" in captured_calls[1][0]
    assert "RETRIEVED CONTEXT" not in captured_calls[1][1]
