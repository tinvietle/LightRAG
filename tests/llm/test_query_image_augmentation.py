"""Regression coverage for retrieval-side query image descriptions."""

import pytest

from lightrag import operate
from lightrag.base import QueryContextResult, QueryParam
from lightrag.multimodal_case import augment_query_with_image_descriptions


VALID_PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL0mQAAAABJRU5ErkJggg=="
)


@pytest.mark.asyncio
async def test_query_images_augment_retrieval_query_and_preserve_image_payload() -> None:
    received_image_inputs: list[dict[str, str]] = []

    async def fake_vlm(prompt: str, **kwargs: object) -> str:
        assert "expert image analyzer" in prompt
        received_image_inputs.extend(kwargs["image_inputs"])
        return '{"name":"red_square","type":"Illustration","description":"A solid red square."}'

    augmented_query = await augment_query_with_image_descriptions(
        "What does this image show?",
        [VALID_PNG_DATA_URL],
        fake_vlm,
        "English",
        max_images=10,
    )

    assert augmented_query == (
        "What does this image show?\n\n"
        "Image descriptions:\n"
        "- Image 1: A solid red square."
    )
    assert received_image_inputs[0]["mime_type"] == "image/png"


@pytest.mark.asyncio
async def test_query_uses_original_text_when_image_description_fails() -> None:
    async def failing_vlm(*args: object, **kwargs: object) -> str:
        raise RuntimeError("VLM unavailable")

    augmented_query = await augment_query_with_image_descriptions(
        "What does this image show?",
        [VALID_PNG_DATA_URL],
        failing_vlm,
        "English",
        max_images=10,
    )

    assert augmented_query == "What does this image show?"


@pytest.mark.asyncio
async def test_kg_query_uses_augmented_query_for_keywords_and_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_queries: list[str] = []

    async def fake_augment_query(*args: object, **kwargs: object) -> str:
        return "What does this image show?\n\nImage descriptions:\n- Image 1: A red square."

    async def fake_get_keywords(
        query: str, *args: object, **kwargs: object
    ) -> tuple[list[str], list[str]]:
        observed_queries.append(query)
        return [], ["red square"]

    async def fake_build_context(query: str, *args: object, **kwargs: object) -> QueryContextResult:
        observed_queries.append(query)
        return QueryContextResult(context="retrieved context", raw_data={})

    async def unused_query_llm(*args: object, **kwargs: object) -> str:
        return "unused"

    monkeypatch.setattr(operate, "augment_query_with_image_descriptions", fake_augment_query)
    monkeypatch.setattr(operate, "get_keywords_from_query", fake_get_keywords)
    monkeypatch.setattr(operate, "_build_query_context", fake_build_context)

    result = await operate.kg_query(
        "What does this image show?",
        knowledge_graph_inst=None,
        entities_vdb=None,
        relationships_vdb=None,
        text_chunks_db=None,
        query_param=QueryParam(
            mode="mix",
            only_need_context=True,
            image_inputs=[VALID_PNG_DATA_URL],
        ),
        global_config={
            "role_llm_funcs": {"query": unused_query_llm, "vlm": None},
            "llm_model_name": "test-model",
        },
    )

    assert result.content == "retrieved context"
    assert observed_queries == [
        "What does this image show?\n\nImage descriptions:\n- Image 1: A red square.",
        "What does this image show?\n\nImage descriptions:\n- Image 1: A red square.",
    ]
