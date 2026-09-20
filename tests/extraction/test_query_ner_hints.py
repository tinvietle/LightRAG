from unittest.mock import AsyncMock, patch

import pytest

from lightrag.base import QueryParam
from lightrag.operate import _get_query_ner_suggestions, get_keywords_from_query


class _FakeTokenizer:
    def encode(self, content: str) -> list[int]:
        return [ord(ch) for ch in content]


def _base_global_config(**overrides) -> dict:
    config = {
        "addon_params": {"language": "en"},
        "tokenizer": _FakeTokenizer(),
        "entity_extraction_use_json": False,
        "enable_gliner_ner": True,
        "enable_quickumls_ner": False,
        "enable_query_ner_hints": False,
    }
    config.update(overrides)
    return config


async def _never_called(*_args, **_kwargs):
    raise AssertionError("detector should not be invoked when hints are disabled")


@pytest.mark.offline
@pytest.mark.asyncio
async def test_query_ner_hints_disabled_by_default_is_a_pure_noop():
    """enable_query_ner_hints defaults to False: no detector call at all."""
    global_config = _base_global_config(enable_query_ner_hints=False)

    with (
        patch("lightrag.operate.recognize_entities", _never_called),
        patch("lightrag.operate.recognize_quickumls_entities", _never_called),
    ):
        suggestions = await _get_query_ner_suggestions("chest pain", global_config)

    assert suggestions == []


@pytest.mark.offline
@pytest.mark.asyncio
async def test_query_ner_hints_disabled_when_no_detector_enabled():
    """Master switch on but both underlying detectors off: still a no-op."""
    global_config = _base_global_config(
        enable_query_ner_hints=True,
        enable_gliner_ner=False,
        enable_quickumls_ner=False,
    )

    with (
        patch("lightrag.operate.recognize_entities", _never_called),
        patch("lightrag.operate.recognize_quickumls_entities", _never_called),
    ):
        suggestions = await _get_query_ner_suggestions("chest pain", global_config)

    assert suggestions == []


@pytest.mark.offline
@pytest.mark.asyncio
async def test_query_ner_hints_merges_gliner_detections_when_enabled():
    global_config = _base_global_config(
        enable_query_ner_hints=True,
        enable_gliner_ner=True,
        enable_quickumls_ner=False,
    )

    gliner_mock = AsyncMock(
        return_value=(
            "- cutaneous abscess",
            [{"text": "cutaneous abscess", "score": 0.95}],
        )
    )

    with (
        patch("lightrag.operate.recognize_entities", gliner_mock),
        patch("lightrag.operate.recognize_quickumls_entities", _never_called),
    ):
        suggestions = await _get_query_ner_suggestions(
            "painful neck mass with pus", global_config
        )

    assert suggestions == ["cutaneous abscess"]
    gliner_mock.assert_awaited_once()


@pytest.mark.offline
@pytest.mark.asyncio
async def test_query_ner_hints_combines_and_dedupes_both_detectors():
    global_config = _base_global_config(
        enable_query_ner_hints=True,
        enable_gliner_ner=True,
        enable_quickumls_ner=True,
    )

    gliner_mock = AsyncMock(
        return_value=(
            "",
            [
                {"text": "Cutaneous abscess", "score": 0.9},
                {"text": "erythema", "score": 0.7},
            ],
        )
    )
    quickumls_mock = AsyncMock(
        return_value=(
            "",
            [
                # Case-insensitive duplicate of a GLiNER hit; lower score should
                # not create a second entry.
                {"text": "cutaneous abscess", "score": 0.5},
                {"text": "penicillin allergy", "score": 0.6},
            ],
        )
    )

    with (
        patch("lightrag.operate.recognize_entities", gliner_mock),
        patch("lightrag.operate.recognize_quickumls_entities", quickumls_mock),
    ):
        suggestions = await _get_query_ner_suggestions(
            "case narrative text", global_config
        )

    assert sorted(s.casefold() for s in suggestions) == sorted(
        ["cutaneous abscess", "erythema", "penicillin allergy"]
    )
    gliner_mock.assert_awaited_once()
    quickumls_mock.assert_awaited_once()


@pytest.mark.offline
@pytest.mark.asyncio
async def test_get_keywords_from_query_appends_deduplicated_ner_suggestions():
    param = QueryParam()
    global_config = _base_global_config(enable_query_ner_hints=True)

    async def fake_extract_keywords_only(*_args, **_kwargs):
        return ["Abscess management"], ["Cutaneous abscess", "Neck abscess"]

    async def fake_ner_suggestions(*_args, **_kwargs):
        # "Cutaneous abscess" duplicates an existing ll_keyword (case-insensitive)
        # and must not be appended twice; "erythema" is new.
        return ["cutaneous abscess", "erythema"]

    with (
        patch(
            "lightrag.operate.extract_keywords_only",
            fake_extract_keywords_only,
        ),
        patch(
            "lightrag.operate._get_query_ner_suggestions",
            fake_ner_suggestions,
        ),
    ):
        hl_keywords, ll_keywords = await get_keywords_from_query(
            "query text", param, global_config, hashing_kv=None
        )

    assert hl_keywords == ["Abscess management"]
    assert ll_keywords == ["Cutaneous abscess", "Neck abscess", "erythema"]


@pytest.mark.offline
@pytest.mark.asyncio
async def test_get_keywords_from_query_skips_augmentation_for_predefined_keywords():
    """Explicitly caller-supplied keywords bypass NER augmentation entirely."""
    param = QueryParam(hl_keywords=["AI"], ll_keywords=["RAG"])
    global_config = _base_global_config(enable_query_ner_hints=True)

    with patch(
        "lightrag.operate._get_query_ner_suggestions", _never_called
    ):
        hl_keywords, ll_keywords = await get_keywords_from_query(
            "query text", param, global_config, hashing_kv=None
        )

    assert hl_keywords == ["AI"]
    assert ll_keywords == ["RAG"]
