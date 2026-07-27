"""Regression coverage for query image MIME detection."""

from lightrag.llm._vision_utils import normalize_image_inputs


def test_normalize_image_inputs_detects_raw_bmp_data() -> None:
    # BMP file header with a minimal placeholder body; MIME detection only
    # requires the signature and keeps raw base64 API inputs provider-safe.
    image = normalize_image_inputs(["Qk0AAAAAAAAAAAAAAAAAAAA="])[0]

    assert image.mime_type == "image/bmp"
