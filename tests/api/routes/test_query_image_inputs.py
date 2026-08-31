"""Validation and query-parameter coverage for attached query images."""

import base64
import sys

import pytest
from pydantic import ValidationError

_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
)
VALID_PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(_PNG_BYTES).decode(
    "ascii"
)


def _query_request_model():
    """Import the router with server-style arguments for lazy API config init."""
    original_argv = sys.argv.copy()
    try:
        sys.argv = ["lightrag-server"]
        from lightrag.api.routers.query_routes import MAX_QUERY_IMAGES, QueryRequest

        return MAX_QUERY_IMAGES, QueryRequest
    finally:
        sys.argv = original_argv


def test_query_request_forwards_valid_images_to_query_param() -> None:
    _, QueryRequest = _query_request_model()
    request = QueryRequest(query="Describe this image", images=[VALID_PNG_DATA_URL])

    query_param = request.to_query_params(is_stream=False)

    assert query_param.image_inputs == [VALID_PNG_DATA_URL]


def test_query_request_forwards_context_message_layout_setting() -> None:
    _, QueryRequest = _query_request_model()
    request = QueryRequest(query="Describe the case", context_in_user_message=True)

    query_param = request.to_query_params(is_stream=False)

    assert query_param.context_in_user_message is True


def test_query_request_rejects_invalid_image_base64() -> None:
    _, QueryRequest = _query_request_model()
    with pytest.raises(ValidationError, match="Invalid query image"):
        QueryRequest(query="Describe this image", images=["not-valid-base64"])


def test_query_request_limits_attached_images() -> None:
    MAX_QUERY_IMAGES, QueryRequest = _query_request_model()
    with pytest.raises(ValidationError):
        QueryRequest(
            query="Describe these images",
            images=[VALID_PNG_DATA_URL] * (MAX_QUERY_IMAGES + 1),
        )
