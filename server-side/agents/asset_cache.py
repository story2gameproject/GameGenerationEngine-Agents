"""
Asset Cache
A tiny content-addressable cache for AI-generated assets (backgrounds, sprites).
Keyed by a normalized description string + asset type so that similar requests
across different games share the same generated image.

Cache hits are instant (file existence check) and skip the API call entirely.

Files live under client-side/static/cache/ so they can be served as
/static/cache/<key>.png by Flask alongside game HTML.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re

logger = logging.getLogger(__name__)

# Cache directory — sibling of /static/games/
CACHE_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..",
    "client-side", "static", "cache"
))
CACHE_URL_PREFIX = "/static/cache"


def _normalize(description: str) -> str:
    """Lowercase, strip, collapse whitespace, remove non-alphanumeric chars.
    'NYC at  sunset!' and 'nyc at sunset' both normalize to 'nyc at sunset'."""
    d = (description or "").lower().strip()
    d = re.sub(r"[^a-z0-9 ]+", " ", d)
    d = re.sub(r"\s+", " ", d).strip()
    return d


# Bump this whenever we make a change that should INVALIDATE previously
# cached sprites — e.g. a new rembg model, a new SDXL prompt style, a
# negative-prompt tweak. Without this, cached pre-fix sprites get served
# forever and users still see old artifacts.
CACHE_VERSION = "v5-tight-bbox-strict-facing-single-subject"


def cache_key(description: str, asset_type: str) -> str:
    """Stable 16-char hash for an (asset_type, description) pair.
    Different asset types (background vs hero vs obstacle) live in separate
    keyspaces so 'a robot' as a hero never collides with 'a robot' as an obstacle.

    CACHE_VERSION participates in the hash, so bumping it invalidates every
    sprite cached before the bump — necessary when the underlying model or
    prompts change in a way that would re-segment / re-render the same input.
    """
    norm = _normalize(description)
    raw  = f"{CACHE_VERSION}|{asset_type}|{norm}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def cache_path(key: str) -> str:
    """Absolute filesystem path for a given cache key (PNG)."""
    return os.path.join(CACHE_DIR, f"{key}.png")


def cache_url(key: str) -> str:
    """Public URL Flask serves this cache entry at."""
    return f"{CACHE_URL_PREFIX}/{key}.png"


def lookup(description: str, asset_type: str) -> str | None:
    """Return the cache URL if a matching file exists, else None."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    key  = cache_key(description, asset_type)
    path = cache_path(key)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        logger.info("Cache HIT  [%s] %r → %s", asset_type, description[:60], key)
        return cache_url(key)
    logger.info("Cache MISS [%s] %r", asset_type, description[:60])
    return None


def save(description: str, asset_type: str, pil_image) -> str:
    """Save a PIL image into the cache; return its public URL."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    key  = cache_key(description, asset_type)
    path = cache_path(key)
    pil_image.save(path, "PNG")
    logger.info("Cache SAVE [%s] %r → %s (%d bytes)",
                asset_type, description[:60], key, os.path.getsize(path))
    return cache_url(key)
