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
import json
import logging
import os
from io import BytesIO

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


# ──────────────────────────────────────────────────────────────────────────
# Sprite quality verification
# ──────────────────────────────────────────────────────────────────────────

def verify_sprite(pil_image, expected_subject: str, role: str) -> dict:
    """
    Inspect a generated sprite and return a structured verdict.

    SDXL is stochastic — even with strong prompts, a percentage of outputs
    have semantic problems (multiple subjects in one sprite, wrong viewing
    angle, character facing the wrong way, cropped/incomplete subjects).
    Rather than tune prompts endlessly to suppress every failure mode, we
    add a Claude Vision verification step AFTER background removal and
    BEFORE caching, so we can catch and respond to those failures
    deterministically: regenerate the bad ones, flip the inverted ones.

    Returns a dict:
      {
        "is_acceptable": bool,        # overall verdict — false means regenerate
        "facing": "right" | "left" | "forward" | "backward" | "unclear",
        "issues": [list of strings],  # human-readable diagnostic
      }

    Fail-open: any exception (API down, malformed JSON, etc.) accepts the
    sprite. We don't want a transient Claude issue to halt game generation
    for the user; a slightly-imperfect sprite beats no game at all.
    """
    try:
        return _claude_verify_sprite(pil_image, expected_subject, role)
    except Exception as exc:
        logger.warning("Sprite verification failed (%s) — accepting sprite", exc)
        return {"is_acceptable": True, "facing": "unclear", "issues": []}


def _claude_verify_sprite(pil_image, expected_subject: str, role: str) -> dict:
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    # PNG-encode the PIL image to base64 (preserves alpha channel — Claude
    # needs to see what the GAME will see, not the raw SDXL output).
    buf = BytesIO()
    pil_image.save(buf, format="PNG")
    image_b64 = base64.standard_b64encode(buf.getvalue()).decode("utf-8")

    role_blurb = {
        "hero":          "the player's hero character",
        "obstacle":      "an enemy or obstacle the player jumps over",
        "target_rescue": "a character the player is rescuing",
        "target_item":   "a collectible item",
    }.get(role, "a game element")

    prompt = (
        f"You are reviewing a sprite for a 2D side-scrolling platformer.\n"
        f"This image should depict ONE \"{expected_subject}\" as {role_blurb}.\n"
        f"The background has been removed — the subject sits on a transparent canvas.\n\n"
        f"Respond with ONLY a JSON object (no preamble, no markdown fences):\n"
        f"{{\n"
        f'  "is_subject":     true|false,   // recognizable as a "{expected_subject}"\n'
        f'  "is_single":      true|false,   // exactly ONE subject, no duplicates/lineups\n'
        f'  "is_complete":    true|false,   // full subject visible, not cropped at edges\n'
        f'  "semantic_match": true|false,   // image visually contains the key attributes in "{expected_subject}"\n'
        f'  "facing":         "right"|"left"|"forward"|"backward"|"unclear",\n'
        f'  "notes":          "one short sentence on any quality issue, or empty"\n'
        f"}}\n\n"
        f"For \"semantic_match\": the user wrote \"{expected_subject}\". Check that the "
        f"image contains the SPECIFIC attributes they named, especially COLORS and "
        f"CLOTHING/ACCESSORIES. Be strict about colors — a 'blue shirt' must be visibly "
        f"blue (not orange, not red, not green), a 'red dragon' must be visibly red, a "
        f"'pink dress' must be visibly pink. Be strict about distinctive items — "
        f"'with a sword', 'wearing a crown', 'holding a wand' must actually be visible. "
        f"Generic attributes are flexible (the SHADE of blue doesn't need to be perfect; "
        f"a child can be older/younger than expected) but the SPECIFIC named features "
        f"must match what's shown. If they said only the subject with no extra attributes "
        f"(e.g. just 'princess'), any princess passes.\n\n"
        f"For \"facing\" — this is the most error-prone field, so follow these rules STRICTLY:\n"
        f" - Look at the HEAD/FACE specifically, not the overall body angle.\n"
        f" - 'right' = strict side profile, ONE ear visible on the left side of the head, nose pointing to the right edge of the image.\n"
        f" - 'left'  = strict side profile, ONE ear visible on the right side of the head, nose pointing to the left edge of the image.\n"
        f" - 'forward' = facing the camera, BOTH ears (or both eyes) visible, nose pointing toward the viewer. If you see two symmetric eyes, it's forward, not profile.\n"
        f" - 'backward' = turned away from camera, back of head visible.\n"
        f" - 'unclear' = if you're not highly confident, prefer this over guessing. 3/4 views (slightly turned) are 'unclear', NOT 'right'/'left'.\n"
        f"For vehicles, 'facing' is where the FRONT of the vehicle points (where the headlights are).\n"
        f"For non-directional objects (coin, mushroom, gem, flag) use 'unclear'."
    )

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": image_b64,
                    },
                },
                {"type": "text", "text": prompt},
            ],
        }],
    )

    raw = response.content[0].text.strip()
    # Be forgiving of ```json fences Claude sometimes adds
    if raw.startswith("```"):
        raw = raw.strip("`").lstrip("json").strip()

    parsed = json.loads(raw)

    # semantic_match defaults to True for backward compat / when Claude omits
    # the field entirely (older response shape).
    semantic_match = parsed.get("semantic_match", True)

    is_acceptable = bool(
        parsed.get("is_subject")
        and parsed.get("is_single")
        and parsed.get("is_complete")
        and semantic_match
    )

    issues = []
    if not parsed.get("is_subject"):
        issues.append(f"not recognizable as {expected_subject}")
    if not parsed.get("is_single"):
        issues.append("multiple subjects in one sprite")
    if not parsed.get("is_complete"):
        issues.append("subject cropped or incomplete")
    if not semantic_match:
        issues.append(f"visual content does not match description \"{expected_subject}\"")
    notes = (parsed.get("notes") or "").strip()
    if notes:
        issues.append(notes)

    facing = parsed.get("facing", "unclear")
    if facing not in ("right", "left", "forward", "backward", "unclear"):
        facing = "unclear"

    logger.info(
        "Sprite verify [%s, %s]: %s, facing=%s%s",
        role, expected_subject[:30],
        "OK" if is_acceptable else "REJECT",
        facing,
        f", issues={issues}" if issues else "",
    )

    return {
        "is_acceptable": is_acceptable,
        "facing":        facing,
        "issues":        issues,
    }
