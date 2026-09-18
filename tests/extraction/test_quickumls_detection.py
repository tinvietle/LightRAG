from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.offline
@pytest.mark.asyncio
async def test_quickumls_recognizer_is_lazy_and_formats_source_spans():
    from lightrag.kg import quickumls

    quickumls._workers.clear()
    worker = AsyncMock()
    worker.match.return_value = [
        {
            "text": "liver abscess",
            "score": 1.0,
            "start": 18,
            "end": 31,
            "source": "quickumls",
            "cuis": ["C0023885"],
            "semtypes": ["T047"],
        }
    ]
    with patch("lightrag.kg.quickumls.QuickUMLSWorker", return_value=worker):
        formatted, entities = await quickumls.recognize_quickumls_entities(
            "The patient had a liver abscess.",
            python="python3.11",
            index_dir="quickumls_data",
            nltk_data="nltk_data",
        )

    assert formatted == "- liver abscess"
    assert entities[0]["text"] == "liver abscess"
    assert entities[0]["cuis"] == ["C0023885"]


@pytest.mark.offline
@pytest.mark.asyncio
async def test_quickumls_failure_is_optional_and_returns_no_hints():
    from lightrag.kg import quickumls

    quickumls._workers.clear()
    worker = AsyncMock()
    worker.match.side_effect = FileNotFoundError("worker unavailable")
    with (
        patch("lightrag.kg.quickumls.QuickUMLSWorker", return_value=worker),
        patch("lightrag.kg.quickumls.logger"),
    ):
        formatted, entities = await quickumls.recognize_quickumls_entities(
            "liver abscess",
            python="missing-python",
            index_dir="missing-index",
            nltk_data="missing-nltk",
        )

    assert formatted == ""
    assert entities == []
