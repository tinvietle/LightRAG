"""Coverage for the opt-in case-oriented keyword prompt (keywords_extraction_case)."""

import json
import re
from unittest.mock import AsyncMock, patch

import pytest

from lightrag.base import QueryParam
from lightrag.operate import _parse_keywords_payload, extract_keywords_only
from lightrag.prompt import PROMPTS
from lightrag.utils import compute_args_hash

RELATION_VERBS = (
    "causes",
    "complicates",
    "treats",
    "indicates",
    "characterized_by",
    "risk_factor_for",
    "complication_of",
    "contraindicated_with",
    "associated_with",
    "monitored_by",
    "influences",
    "identified_by",
    "confirms",
    "equivalent_to",
    "grouped_under",
)
TASK_WORDS = ("diagnosis", "differential", "workup", "evaluation", "management")


def _config(prompt_name: str | None, captured: list[str]) -> dict:
    async def fake_keyword_llm(prompt: str, **_kwargs) -> str:
        captured.append(prompt)
        return '{"high_level_keywords": [], "low_level_keywords": ["x"]}'

    config = {
        "addon_params": {"language": "English"},
        "_resolved_summary_language": "English",
        "role_llm_funcs": {"keyword": fake_keyword_llm},
        "tokenizer": type("T", (), {"encode": lambda self, s: list(s)})(),
    }
    if prompt_name is not None:
        config["keywords_extraction_prompt"] = prompt_name
    return config


@pytest.mark.offline
def test_case_prompt_formats_with_literal_json_braces():
    rendered = PROMPTS["keywords_extraction_case"].format(
        query="a patient case", examples="EXAMPLES", language="English"
    )

    assert "User Query: a patient case" in rendered
    assert "EXAMPLES" in rendered
    assert '{"high_level_keywords": [], "low_level_keywords": []}' in rendered
    assert (
        "Its first character must be `{` and its last character must be `}`" in rendered
    )


@pytest.mark.offline
def test_case_examples_are_valid_and_follow_the_prompt_rules():
    examples = PROMPTS["keywords_extraction_case_examples"]
    assert len(examples) >= 2

    for example in examples:
        output_json = example.split("Output:", 1)[1]
        is_valid, hl, ll = _parse_keywords_payload(output_json)

        assert is_valid
        assert 1 <= len(ll) <= 10
        assert len(hl) <= 3
        assert len(set(ll)) == len(ll)
        for keyword in hl + ll:
            lowered = keyword.lower()
            assert not any(
                re.search(rf"\b{verb}\b", lowered) for verb in RELATION_VERBS
            )
            assert not any(word in lowered for word in TASK_WORDS)
        # json.loads must also accept it unmodified (providers use JSON mode)
        assert set(json.loads(output_json)) == {
            "high_level_keywords",
            "low_level_keywords",
        }


@pytest.mark.offline
def test_default_keyword_prompt_and_examples_are_left_untouched():
    assert "expert clinical keyword extractor" in PROMPTS["keywords_extraction"]
    assert len(PROMPTS["keywords_extraction_examples"]) == 3


@pytest.mark.offline
@pytest.mark.asyncio
async def test_extract_keywords_uses_the_selected_prompt_variant():
    default_prompts: list[str] = []
    case_prompts: list[str] = []

    await extract_keywords_only(
        "some case", QueryParam(), _config(None, default_prompts)
    )
    await extract_keywords_only(
        "some case", QueryParam(), _config("keywords_extraction_case", case_prompts)
    )

    assert "expert clinical keyword extractor" in default_prompts[0]
    assert "case-based diagnostic RAG system" not in default_prompts[0]
    assert "case-based diagnostic RAG system" in case_prompts[0]
    assert "Acute intermittent porphyria" in case_prompts[0]  # variant's own examples
    assert "Community-acquired pneumonia" not in case_prompts[0]


@pytest.mark.offline
@pytest.mark.asyncio
async def test_unknown_prompt_variant_is_rejected_with_available_names():
    with pytest.raises(ValueError) as error:
        await extract_keywords_only("q", QueryParam(), _config("no_such_prompt", []))

    message = str(error.value)
    assert "no_such_prompt" in message
    assert "keywords_extraction_case" in message


@pytest.mark.offline
@pytest.mark.asyncio
async def test_cache_key_is_variant_specific_and_default_key_is_unchanged():
    seen: dict[str, str] = {}

    async def run(name: str | None) -> str:
        handle_cache = AsyncMock(return_value=None)
        with patch("lightrag.operate.handle_cache", handle_cache):
            await extract_keywords_only("some case", QueryParam(), _config(name, []))
        return handle_cache.await_args.args[1]

    seen["default"] = await run(None)
    seen["explicit_default"] = await run("keywords_extraction")
    seen["case"] = await run("keywords_extraction_case")

    assert seen["default"] == seen["explicit_default"]
    assert seen["case"] != seen["default"]

    # The default key must stay byte-compatible with caches written before the
    # selector existed (same inputs, no prompt-name component).
    from lightrag.utils import get_llm_cache_identity, serialize_llm_cache_identity

    config = _config(None, [])
    legacy = compute_args_hash(
        QueryParam().mode,
        "some case",
        "English",
        "\n<llm_identity>\n",
        serialize_llm_cache_identity(get_llm_cache_identity(config, "keyword")),
    )
    assert seen["default"] == legacy
