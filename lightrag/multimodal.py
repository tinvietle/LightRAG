from __future__ import annotations

import asyncio
import os
from typing import Any

import requests

from lightrag.prompt import PROMPTS
from lightrag.utils import logger

DEFAULT_IMAGE_DESCRIPTION_MODEL = os.getenv(
    "LIGHTRAG_IMAGE_DESCRIPTION_MODEL", "gemma4:31b-cloud"
)
DEFAULT_IMAGE_DESCRIPTION_URL = os.getenv(
    "LIGHTRAG_IMAGE_DESCRIPTION_URL", "http://localhost:11434/api/generate"
)
DEFAULT_IMAGE_DESCRIPTION_TIMEOUT = int(
    os.getenv("LIGHTRAG_IMAGE_DESCRIPTION_TIMEOUT", "600")
)

DEFAULT_IMAGES_UPLOAD_LIMIT = int(os.getenv("MAX_IMAGES_UPLOAD", 10))


def normalize_image_data(image_data: str) -> str:
    """Return raw base64 payload from either a data URL or plain base64 string."""
    if image_data.startswith("data:") and "," in image_data:
        return image_data.split(",", 1)[1].strip()
    return image_data.strip()


def build_image_augmented_query(query: str, image_descriptions: list[str]) -> str:
    """Combine the user query with image descriptions for retrieval and prompting."""
    if not image_descriptions:
        return query.strip()

    descriptions = "\n".join(
        f"- Image {index + 1}: {description}"
        for index, description in enumerate(image_descriptions)
        if description.strip()
    )
    if not descriptions:
        return query.strip()

    return f"{query.strip()}\n\nImage descriptions:\n{descriptions}".strip()


def _call_ollama_generate(
    system_prompt: str,
    user_prompt: str,
    image_b64: str,
    *,
    model: str = DEFAULT_IMAGE_DESCRIPTION_MODEL,
    url: str = DEFAULT_IMAGE_DESCRIPTION_URL,
    timeout: int = DEFAULT_IMAGE_DESCRIPTION_TIMEOUT,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "system": system_prompt,
        "prompt": user_prompt,
        "images": [image_b64],
        "stream": False,
        "options": {
            "temperature": 0.1,
            "num_ctx": 32768,
            "num_predict": 8192,
        },
    }
    response = requests.post(url, json=payload, timeout=timeout)
    response.raise_for_status()
    return response.json()


async def describe_image_with_refinement(
    image_data: str,
    *,
    model: str = DEFAULT_IMAGE_DESCRIPTION_MODEL,
    url: str = DEFAULT_IMAGE_DESCRIPTION_URL,
    timeout: int = DEFAULT_IMAGE_DESCRIPTION_TIMEOUT,
) -> str:
    """Run the two-stage image description flow used by the multimodal query pipeline."""
    image_b64 = normalize_image_data(image_data)

    try:
        initial_response = await asyncio.to_thread(
            _call_ollama_generate,
            PROMPTS["generate_image_description"],
            PROMPTS["generate_image_description"],
            image_b64,
            model=model,
            url=url,
            timeout=timeout,
        )
        initial_output = initial_response.get("response", "").strip()

        refinement_prompt = PROMPTS["refine_image_description"].format(
            initial_output=initial_output
        )
        refined_response = await asyncio.to_thread(
            _call_ollama_generate,
            PROMPTS["refine_image_description"],
            refinement_prompt,
            image_b64,
            model=model,
            url=url,
            timeout=timeout,
        )
        refined_output = refined_response.get("response", "").strip()

        return refined_output or initial_output
    except Exception as exc:
        logger.warning("Image description pipeline failed: %s", exc)
        return ""


async def describe_images_with_refinement(image_data_list: list[str]) -> list[str]:
    """Describe up to ten images sequentially, preserving query order."""
    descriptions: list[str] = []
    for image_data in image_data_list[:DEFAULT_IMAGES_UPLOAD_LIMIT]:
        description = await describe_image_with_refinement(image_data)
        if description:
            descriptions.append(description)
    return descriptions


def build_openai_multimodal_user_content(prompt: str, images: list[str] | None) -> Any:
    """Build an OpenAI-style multimodal user content payload."""
    if not images:
        return prompt

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for image_data in images:
        normalized_image_data = normalize_image_data(image_data)
        image_url = (
            image_data
            if image_data.startswith("data:")
            else f"data:image/jpeg;base64,{normalized_image_data}"
        )
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": image_url},
            }
        )
    return content


def build_ollama_multimodal_user_message(prompt: str, images: list[str] | None) -> dict[str, Any]:
    """Build an Ollama-style multimodal user message payload."""
    message: dict[str, Any] = {"role": "user", "content": prompt}
    if images:
        message["images"] = [normalize_image_data(image_data) for image_data in images]
    return message