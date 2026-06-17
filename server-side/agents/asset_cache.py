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
import json
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
CACHE_VERSION = "v10-reject-backward-rescue-target"


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


def meta_path(key: str) -> str:
    """Absolute filesystem path for a cache entry's metadata sidecar JSON.

    The sidecar holds per-sprite facts the game engine needs but that
    aren't visible in the PNG — most importantly the verifier's facing
    verdict, which decides whether the sprite should be horizontally
    flipped on left-walk."""
    return os.path.join(CACHE_DIR, f"{key}.meta.json")


def cache_url(key: str) -> str:
    """Public URL Flask serves this cache entry at."""
    return f"{CACHE_URL_PREFIX}/{key}.png"


def lookup(description: str, asset_type: str) -> str | None:
    """Return the cache URL if a matching file exists, else None.

    Kept for backward compatibility. New callers should prefer
    lookup_with_meta() so they also get the cached facing verdict."""
    url, _ = lookup_with_meta(description, asset_type)
    return url


def lookup_with_meta(description: str, asset_type: str) -> tuple[str | None, dict]:
    """Return (cache_url, metadata_dict). Returns (None, {}) on miss."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    key  = cache_key(description, asset_type)
    path = cache_path(key)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        meta = {}
        mpath = meta_path(key)
        if os.path.exists(mpath):
            try:
                with open(mpath, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Cache HIT [%s] but meta file unreadable: %s", asset_type, exc)
                meta = {}
        logger.info("Cache HIT  [%s] %r → %s (meta=%s)",
                    asset_type, description[:60], key, meta or "—")
        return cache_url(key), meta
    logger.info("Cache MISS [%s] %r", asset_type, description[:60])
    return None, {}


def save(description: str, asset_type: str, pil_image, metadata: dict | None = None) -> str:
    """Save a PIL image (and optional metadata sidecar) into the cache;
    return the public URL of the PNG.

    metadata is a small dict of facts about the sprite the engine reads
    later (e.g., {"facing": "right", "directional": True}). It's written
    to <key>.meta.json next to the PNG. Pass None or {} to skip the sidecar."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    key  = cache_key(description, asset_type)
    path = cache_path(key)
    pil_image.save(path, "PNG")
    if metadata:
        with open(meta_path(key), "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
    logger.info("Cache SAVE [%s] %r → %s (%d bytes, meta=%s)",
                asset_type, description[:60], key, os.path.getsize(path),
                metadata or "—")
    return cache_url(key)
