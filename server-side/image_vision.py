"""
Image Vision helper.
Uses Claude Haiku 4.5 with vision capability to convert an uploaded image
into a brief text description suitable for the game-generation pipeline.

The Q&A flow lets users upload an image instead of typing for "hero",
"rescue character", or "enemy" questions. Previously the URL got passed
through to the asset agent as a literal string, which SDXL couldn't make
sense of. Now we transcribe the image to a short natural-language
description first, so downstream prompts get meaningful input.
"""

from __future__ import annotations

import base64
import logging
import os

logger = logging.getLogger(__name__)

# File extension → IANA media type. Anthropic's vision API needs the right one.
_MEDIA_TYPES = {
    "png":  "image/png",
    "jpg":  "image/jpeg",
    "jpeg": "image/jpeg",
    "gif":  "image/gif",
    "webp": "image/webp",
}

# Max file size we'll send to Claude — keep API costs and latency reasonable
_MAX_BYTES = 5 * 1024 * 1024   # 5 MB


def image_to_description(image_path: str, context: str = "subject") -> str | None:
    """
    Return a short text description of the image at `image_path`, or None
    on any failure (caller should fall back to using the original URL/text).

    `context` is plugged into the prompt — pass something like
    "main hero character" or "character to rescue" so Claude picks
    relevant visual features.
    """
    try:
        return _claude_describe(image_path, context)
    except Exception as exc:
        logger.warning("Image vision failed (%s) — caller will fall back", exc)
        return None


def _claude_describe(image_path: str, context: str) -> str:
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"Image not found: {image_path}")

    size = os.path.getsize(image_path)
    if size > _MAX_BYTES:
        raise RuntimeError(f"Image too large for vision API: {size} bytes")

    ext = image_path.lower().rsplit(".", 1)[-1] if "." in image_path else ""
    media_type = _MEDIA_TYPES.get(ext, "image/png")

    with open(image_path, "rb") as f:
        image_b64 = base64.standard_b64encode(f.read()).decode("utf-8")

    prompt = (
        f"Describe this image briefly to be used as the {context} in a "
        f"2D pixel-art platformer game. Focus on visual features: appearance, "
        f"colors, clothing or markings, key recognizable elements. "
        f"Keep it under 60 characters. NO preamble like 'I see' or 'The image shows' — "
        f"just the description as a noun phrase. "
        f"Example output: 'young girl with red scarf and dark hair tied up'"
    )

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=120,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": image_b64,
                    },
                },
                {"type": "text", "text": prompt},
            ],
        }],
    )

    description = response.content[0].text.strip()
    # Strip trailing punctuation, drop leading articles to keep it natural
    description = description.rstrip(" ,.;:!?")
    for article in ("The ", "the ", "A ", "a ", "An ", "an "):
        if description.startswith(article):
            description = description[len(article):]
            break
    logger.info("Image vision: %s → %r", os.path.basename(image_path), description)
    return description
