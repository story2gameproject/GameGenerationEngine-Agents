"""
Agent 4 — Background Agent
Generates a realistic pixel-art background image via Hugging Face SDXL.
Caches results in /static/cache/ by description hash so repeat requests
(e.g. multiple games set in "NYC") reuse the same image and skip the API call.

Falls back to None on any failure — the template then uses its built-in
sky gradient + parallax rectangles instead.
"""

from __future__ import annotations

import logging
import os
import time
import uuid

from . import asset_cache

logger = logging.getLogger(__name__)

# Image dimensions — wide aspect for side-scrolling
IMG_W = 1024
IMG_H = 576

# Style scaffolding so every background looks like it belongs in the same game
_STYLE = (
    "16-bit pixel art video game background, side view, "
    "vibrant cinematic atmosphere, detailed environment, "
    "no characters, no people, no text, no watermark"
)
_NEGATIVE = (
    "people, person, characters, faces, hands, text, watermark, signature, "
    "logo, blurry, low quality, 3d render, photograph, ugly"
)


def generate_background(game_params: dict, output_dir: str = None) -> str | None:
    """
    Return a URL to a background image matching this game's world.
    First checks the cache; if no match, generates via HF SDXL and caches the result.
    Returns None on total failure (in which case template uses gradient fallback).

    The `output_dir` argument is kept for backwards-compat but unused — assets
    are now stored centrally in /static/cache/ via asset_cache.
    """
    try:
        return _cached_or_generate(game_params)
    except Exception as exc:
        logger.warning("Background agent failed (%s) — game will use gradient fallback", exc)
        return None


def _cache_description(game_params: dict) -> str:
    """The description we use as the cache key.
    Prefer the RAW user input (so typing "NYC" twice always hits the same
    cache entry, regardless of how Claude expanded it in this run).
    Fall back to Claude's expansion if raw isn't available."""
    raw = game_params.get("_raw_answers", {})
    location = (raw.get("game_location") or
                game_params.get("world", {}).get("description", "outdoor scene"))
    goal_type = game_params.get("goal_type", "")
    # Goal type changes the mood (rescue→dusk, escape→dark, else→bright),
    # so include it in the key to avoid mood mismatches.
    return f"{location} | mood:{goal_type}"


def _cached_or_generate(game_params: dict) -> str | None:
    cache_desc = _cache_description(game_params)

    # 1. Try the cache
    hit = asset_cache.lookup(cache_desc, "background")
    if hit:
        return hit

    # 2. Generate via HF SDXL on miss
    image = _hf_generate(game_params)
    if image is None:
        return None

    # 3. Save into cache, return its URL
    return asset_cache.save(cache_desc, "background", image)


def _hf_generate(game_params: dict):
    """Run the SDXL call and return a PIL Image (or raise)."""
    from huggingface_hub import InferenceClient

    token = os.getenv("HF_TOKEN")
    if not token:
        raise RuntimeError("HF_TOKEN not set")

    world_desc = game_params.get("world", {}).get("description", "outdoor scene")
    goal_type  = game_params.get("goal_type", "")

    prompt = f"{_STYLE}, location: {world_desc}, empty street view with no characters"
    if "rescue" in goal_type:
        prompt += ", dramatic mood, dusk lighting"
    elif "escape" in goal_type:
        prompt += ", tense atmosphere, dark mood"
    else:
        prompt += ", bright cheerful lighting"

    logger.info("Background agent calling SDXL: '%s'", prompt[:120])

    client = InferenceClient(token=token)

    # HF Inference can return 503 while the model warms up — retry a few times.
    last_exc = None
    for attempt in range(3):
        try:
            return client.text_to_image(
                prompt=prompt,
                model="stabilityai/stable-diffusion-xl-base-1.0",
                negative_prompt=_NEGATIVE,
                width=IMG_W,
                height=IMG_H,
            )
        except Exception as e:
            last_exc = e
            msg = str(e).lower()
            if "loading" in msg or "503" in msg or "service unavailable" in msg:
                logger.info("HF SDXL warming up, retrying in 15s (attempt %d/3)", attempt + 1)
                time.sleep(15)
            else:
                raise
    raise last_exc if last_exc else RuntimeError("HF image generation failed")
