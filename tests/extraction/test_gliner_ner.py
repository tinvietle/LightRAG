from unittest.mock import AsyncMock, Mock, patch

import pytest

from lightrag.kg.ner import recognize_entities


class CharacterTokenizer:
    def encode(self, content: str) -> list[int]:
        return [ord(character) for character in content]


class ImmediateExecutorLoop:
    async def run_in_executor(self, executor, function):
        return function()


@pytest.mark.offline
@pytest.mark.asyncio
async def test_recognize_entities_uses_flat_ner_and_conservative_threshold():
    model = Mock()
    model.predict_entities.return_value = []

    with (
        patch("lightrag.kg.ner._load_ner_model", new=AsyncMock(return_value=model)),
        patch(
            "lightrag.kg.ner.asyncio.get_running_loop",
            return_value=ImmediateExecutorLoop(),
        ),
    ):
        await recognize_entities("Acute renal failure", ["Disease"])

    model.predict_entities.assert_called_once_with(
        "Acute renal failure",
        ["Disease"],
        flat_ner=True,
        threshold=0.9,
    )


@pytest.mark.offline
@pytest.mark.asyncio
async def test_recognize_entities_deduplicates_text_and_preserves_offsets():
    model = Mock()
    model.predict_entities.return_value = [
        {
            "text": "Renal   failure",
            "label": "Disease",
            "score": 0.91,
            "start": 0,
            "end": 13,
        },
        {
            "text": "renal failure",
            "label": "Condition",
            "score": 0.98,
            "start": 30,
            "end": 43,
        },
        {
            "text": "dialysis",
            "label": "Treatment",
            "score": 0.95,
            "start": 50,
            "end": 58,
        },
    ]

    with (
        patch("lightrag.kg.ner._load_ner_model", new=AsyncMock(return_value=model)),
        patch(
            "lightrag.kg.ner.asyncio.get_running_loop",
            return_value=ImmediateExecutorLoop(),
        ),
    ):
        formatted, entities = await recognize_entities("clinical text", ["Disease"])

    assert formatted == "- renal failure\n- dialysis"
    assert [entity["text"] for entity in entities] == ["renal failure", "dialysis"]
    assert entities[0]["start"] == 30
    assert entities[0]["end"] == 43


@pytest.mark.offline
@pytest.mark.asyncio
async def test_recognize_entities_applies_confidence_ranked_prompt_budgets():
    model = Mock()
    model.predict_entities.return_value = [
        {"text": "lower confidence", "score": 0.91, "start": 0, "end": 16},
        {"text": "top", "score": 0.99, "start": 20, "end": 23},
        {"text": "second", "score": 0.95, "start": 30, "end": 36},
    ]

    with (
        patch("lightrag.kg.ner._load_ner_model", new=AsyncMock(return_value=model)),
        patch(
            "lightrag.kg.ner.asyncio.get_running_loop",
            return_value=ImmediateExecutorLoop(),
        ),
    ):
        formatted, entities = await recognize_entities(
            "clinical text",
            ["Disease"],
            max_entities=2,
            max_tokens=len("- top\n- second\n"),
            tokenizer=CharacterTokenizer(),
        )

    assert formatted == "- top\n- second"
    assert [entity["text"] for entity in entities] == ["top", "second"]
