"""QueryRequest must forward the ignore_hl/ll_keywords switches to QueryParam."""

import sys

import pytest

from lightrag.base import QueryParam


def _query_request_cls(monkeypatch: pytest.MonkeyPatch):
    # lightrag.api.config parses sys.argv on first use; keep pytest's args out of it.
    monkeypatch.setattr(sys, "argv", ["lightrag-server"])
    from lightrag.api.routers.query_routes import QueryRequest

    return QueryRequest


@pytest.mark.offline
def test_api_request_forwards_ignore_flags(monkeypatch: pytest.MonkeyPatch):
    QueryRequest = _query_request_cls(monkeypatch)

    param = QueryRequest(
        query="some question", ignore_hl_keywords=True, ignore_ll_keywords=False
    ).to_query_params(is_stream=False)

    assert param.ignore_hl_keywords is True
    assert param.ignore_ll_keywords is False


@pytest.mark.offline
def test_api_request_leaves_environment_default_when_unset(
    monkeypatch: pytest.MonkeyPatch,
):
    QueryRequest = _query_request_cls(monkeypatch)

    param = QueryRequest(query="some question").to_query_params(is_stream=False)

    assert param.ignore_hl_keywords == QueryParam().ignore_hl_keywords
    assert param.ignore_ll_keywords == QueryParam().ignore_ll_keywords
