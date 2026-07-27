from __future__ import annotations

import base64
import mimetypes
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

import json_repair

from lightrag.prompt_multimodal import MULTIMODAL_PROMPTS
from lightrag.llm._vision_utils import normalize_image_inputs
from lightrag.utils import logger


MULTIMODAL_CASE_IMAGE_HEADER = "Attached image descriptions:"
QUERY_IMAGE_DESCRIPTION_HEADER = "Image descriptions:"
_FENCED_JSON_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```$", re.DOTALL)

_TEXT_FILE_ENCODINGS: tuple[str, ...] = (
    "utf-8-sig",
    "utf-16",
    "utf-16-le",
    "utf-16-be",
    "latin-1",
)


def _extract_json_object(text: str) -> dict[str, Any]:
    if not text:
        return {}

    candidate = text.strip()
    fence_match = _FENCED_JSON_RE.match(candidate)
    if fence_match:
        candidate = fence_match.group(1).strip()

    try:
        parsed = json_repair.loads(candidate)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass

    brace_match = re.search(r"\{.*\}", candidate, re.DOTALL)
    if not brace_match:
        return {}

    try:
        parsed = json_repair.loads(brace_match.group(0))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _extract_image_description(payload: dict[str, Any]) -> str:
    description = payload.get("description")
    if not isinstance(description, str):
        return ""
    return description.strip()


async def read_text_case_content(file_path: Path) -> str:
    raw_bytes = file_path.read_bytes()
    if not raw_bytes:
        return ""

    for encoding in _TEXT_FILE_ENCODINGS:
        try:
            return raw_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue

    raise ValueError(
        f"Unable to decode text case file {file_path.name}; "
        "expected a UTF-8, UTF-16, or Latin-1 compatible text file."
    )


async def describe_image_paths_with_vlm(
    image_paths: list[Path],
    use_vlm_func: Callable[..., Awaitable[str]],
    language: str,
    *,
    max_images: int,
) -> list[str]:
    descriptions: list[str] = []

    for image_path in image_paths[:max_images]:
        try:
            image_bytes = image_path.read_bytes()
            if not image_bytes:
                logger.warning(
                    "[multimodal_case] skipping empty image attachment: %s",
                    image_path,
                )
                continue

            mime_type, _ = mimetypes.guess_type(str(image_path))
            prompt = MULTIMODAL_PROMPTS["image_analysis"].format(
                language=language,
                content="",
                captions="n/a",
                footnotes="n/a",
                leading="n/a",
                trailing="n/a",
                item_id=image_path.name,
                file_path=str(image_path),
            )
            payload = {
                "base64": base64.b64encode(image_bytes).decode("ascii"),
                "mime_type": mime_type or "image/png",
                "source_id": image_path.name,
                "source_file": str(image_path),
                "modality": "image",
            }
            response_text = await use_vlm_func(
                prompt,
                stream=False,
                image_inputs=[payload],
                response_format={"type": "json_object"},
            )
            parsed = _extract_json_object(response_text)
            description = _extract_image_description(parsed)
            if description:
                descriptions.append(description)
        except Exception as error:
            logger.warning(
                "[multimodal_case] failed to describe image %s: %s",
                image_path,
                error,
            )

    return descriptions


async def describe_query_images_with_vlm(
    image_inputs: list[str],
    use_vlm_func: Callable[..., Awaitable[str]] | None,
    language: str,
    *,
    max_images: int,
) -> list[str]:
    """Describe attached query images for retrieval-side query augmentation."""
    if not image_inputs or use_vlm_func is None:
        return []

    descriptions: list[str] = []
    normalized_images = normalize_image_inputs(image_inputs)

    for image in normalized_images[:max_images]:
        try:
            source_id = image.source_id or f"query_image_{image.index + 1}"
            prompt = MULTIMODAL_PROMPTS["image_analysis"].format(
                language=language,
                content="",
                captions="n/a",
                footnotes="n/a",
                leading="n/a",
                trailing="n/a",
                item_id=source_id,
                file_path=image.source_file or source_id,
            )
            response_text = await use_vlm_func(
                prompt,
                stream=False,
                image_inputs=[
                    {
                        "base64": image.base64_str,
                        "mime_type": image.mime_type,
                        "source_id": source_id,
                        "source_file": image.source_file,
                        "modality": "image",
                    }
                ],
                response_format={"type": "json_object"},
            )
            description = _extract_image_description(_extract_json_object(response_text))
            if description:
                descriptions.append(description)
        except Exception as error:
            logger.warning(
                "[query_images] failed to describe image %s: %s",
                image.index + 1,
                error,
            )

    return descriptions


async def augment_query_with_image_descriptions(
    query: str,
    image_inputs: list[str],
    use_vlm_func: Callable[..., Awaitable[str]] | None,
    language: str,
    *,
    max_images: int,
) -> str:
    """Append VLM descriptions to a query while leaving raw images available."""
    descriptions = await describe_query_images_with_vlm(
        image_inputs,
        use_vlm_func,
        language,
        max_images=max_images,
    )
    if not descriptions:
        return query.strip()

    description_block = "\n".join(
        f"- Image {index}: {description}"
        for index, description in enumerate(descriptions, start=1)
        if description.strip()
    )
    if not description_block:
        return query.strip()

    return f"{query.strip()}\n\n{QUERY_IMAGE_DESCRIPTION_HEADER}\n{description_block}"


async def augment_text_with_image_descriptions(
    content: str,
    image_paths: list[Path] | None,
    use_vlm_func: Callable[..., Awaitable[str]] | None,
    language: str,
    *,
    max_images: int,
) -> str:
    if not image_paths or use_vlm_func is None:
        return content

    try:
        image_descriptions = await describe_image_paths_with_vlm(
            image_paths,
            use_vlm_func,
            language,
            max_images=max_images,
        )
        if not image_descriptions:
            return content

        image_description_block = "\n".join(
            f"- {description}"
            for description in image_descriptions
            if description.strip()
        )
        if not image_description_block:
            return content

        return (
            f"{content.rstrip()}\n\n"
            f"{MULTIMODAL_CASE_IMAGE_HEADER}\n"
            f"{image_description_block}"
        )
    except Exception as error:
        logger.warning(
            "[multimodal_case] failed to enrich content with image descriptions: %s",
            error,
        )
        return content
