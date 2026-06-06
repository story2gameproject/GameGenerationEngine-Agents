"""
Agent 2 — Asset Agent (AI sprite version)

Generates URLs for three transparent-background sprite images:
  - hero       (the player character)
  - obstacle   (the danger)
  - target     (the rescue character or collectible item)

Three-tier strategy, fallback in order:
  1. AI sprites    — SDXL generates the sprite, RMBG-1.4 removes the background.
                     Best quality, looks like real pixel art.
  2. Claude SVG    — Claude Haiku draws SVG vector shapes. Recognizable but cartoonish.
  3. Library SVG   — Built-in fallback SVGs. Always available.

Every sprite is cached by description, so repeated games with "superman" as hero
reuse the same file across users and across sessions.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from io import BytesIO

from . import asset_cache

# Path to the standalone rembg worker script (sibling of the agents/ folder)
_REMBG_WORKER = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "rembg_worker.py"
))

# Serializes calls to the rembg subprocess. Without this, the orchestrator's
# 3-way parallel sprite generation would spawn 3 rembg workers simultaneously,
# each using ~250 MB → ~750 MB total → OOM on Render's 512 MB free tier.
# With the lock, only ONE worker runs at a time and total peak stays around
# ~500 MB (main process + one worker).
_REMBG_LOCK = threading.Lock()

logger = logging.getLogger(__name__)

SVG_RE = re.compile(r"<svg\b[^>]*>.*?</svg>", re.S | re.I)

# ---------------------------------------------------------------------------
# Built-in SVG library — always-valid fallbacks when both AI tiers fail
# ---------------------------------------------------------------------------

_DEFAULT_HERO_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 64"><circle cx="24" cy="14" r="9" fill="#FFD9B3"/><rect x="14" y="22" width="20" height="22" fill="#3366CC"/><rect x="10" y="22" width="6" height="14" fill="#3366CC"/><rect x="32" y="22" width="6" height="14" fill="#3366CC"/><rect x="16" y="44" width="6" height="18" fill="#222255"/><rect x="26" y="44" width="6" height="18" fill="#222255"/><circle cx="20" cy="13" r="1.5" fill="#222"/><circle cx="28" cy="13" r="1.5" fill="#222"/></svg>'
_DEFAULT_OBSTACLE_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 64"><ellipse cx="24" cy="34" rx="18" ry="22" fill="#CC2222"/><circle cx="18" cy="28" r="3" fill="#FFF"/><circle cx="30" cy="28" r="3" fill="#FFF"/><circle cx="18" cy="28" r="1.5" fill="#222"/><circle cx="30" cy="28" r="1.5" fill="#222"/><path d="M 14 42 Q 24 48 34 42" stroke="#222" stroke-width="2" fill="none"/></svg>'
_DEFAULT_TARGET_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 64"><polygon points="24,8 28,22 42,22 31,30 36,44 24,36 12,44 17,30 6,22 20,22" fill="#FFD700" stroke="#B8860B" stroke-width="1.5"/></svg>'
_DEFAULT_RESCUE_TARGET_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 64"><circle cx="24" cy="14" r="9" fill="#FFD9B3"/><path d="M 14 22 L 34 22 L 38 60 L 10 60 Z" fill="#FF69B4"/><circle cx="20" cy="13" r="1.5" fill="#222"/><circle cx="28" cy="13" r="1.5" fill="#222"/><path d="M 19 17 Q 24 20 29 17" stroke="#C03B7A" stroke-width="1.5" fill="none"/></svg>'

_THEME_BG = {
    "space":   {"sky": "#0A0A1A", "ground": "#1A1A2E", "platform": "#2E2E5E",
                "layers": [{"type":"rect","label":"star1","x_offset":100,"y":80,"width":4,"height":4,"color":"#FFFFFF","scroll_factor":0.1},{"type":"rect","label":"star2","x_offset":300,"y":200,"width":3,"height":3,"color":"#CCCCFF","scroll_factor":0.1},{"type":"rect","label":"star3","x_offset":600,"y":50,"width":4,"height":4,"color":"#FFFFFF","scroll_factor":0.15},{"type":"rect","label":"star4","x_offset":900,"y":150,"width":3,"height":3,"color":"#AAAAFF","scroll_factor":0.1},{"type":"rect","label":"planet","x_offset":800,"y":60,"width":60,"height":60,"color":"#4A4A8A","scroll_factor":0.2}]},
    "forest":  {"sky": "#1A3A1A", "ground": "#2D5A1B", "platform": "#4A7A2A",
                "layers": [{"type":"rect","label":"tree1","x_offset":100,"y":180,"width":40,"height":200,"color":"#3B2507","scroll_factor":0.3},{"type":"rect","label":"canopy1","x_offset":60,"y":140,"width":120,"height":80,"color":"#228B22","scroll_factor":0.3},{"type":"rect","label":"tree2","x_offset":350,"y":160,"width":40,"height":220,"color":"#3B2507","scroll_factor":0.4},{"type":"rect","label":"canopy2","x_offset":310,"y":120,"width":120,"height":80,"color":"#196619","scroll_factor":0.4},{"type":"rect","label":"tree3","x_offset":650,"y":170,"width":40,"height":210,"color":"#3B2507","scroll_factor":0.5}]},
    "city":    {"sky": "#1A1A2E", "ground": "#333355", "platform": "#4A4A6A",
                "layers": [{"type":"rect","label":"building1","x_offset":50,"y":150,"width":80,"height":350,"color":"#2A2A4A","scroll_factor":0.3},{"type":"rect","label":"building2","x_offset":200,"y":100,"width":100,"height":400,"color":"#1E1E3A","scroll_factor":0.4},{"type":"rect","label":"building3","x_offset":380,"y":180,"width":70,"height":320,"color":"#252540","scroll_factor":0.35},{"type":"rect","label":"building4","x_offset":530,"y":120,"width":90,"height":380,"color":"#2A2A4A","scroll_factor":0.45},{"type":"rect","label":"building5","x_offset":700,"y":160,"width":80,"height":340,"color":"#1E1E3A","scroll_factor":0.3}]},
    "default": {"sky": "#1A1A2E", "ground": "#2D2D4E", "platform": "#3D3D6E",
                "layers": [{"type":"rect","label":"bg1","x_offset":100,"y":100,"width":80,"height":200,"color":"#252542","scroll_factor":0.3},{"type":"rect","label":"bg2","x_offset":400,"y":150,"width":60,"height":180,"color":"#2A2A48","scroll_factor":0.4},{"type":"rect","label":"bg3","x_offset":700,"y":120,"width":90,"height":220,"color":"#1F1F3D","scroll_factor":0.35}]},
}

# ---------------------------------------------------------------------------
# Raw-answer helpers — pull the user's literal answer for cache stability
# ---------------------------------------------------------------------------

def _raw_hero_desc(raw: dict) -> str:
    return (raw.get("hero_description") or "a hero").strip()

def _raw_obstacle_desc(raw: dict) -> str:
    return (raw.get("collecting_goals_obstacles")
        or raw.get("rescue_mission_obstacles")
        or raw.get("time_trial_obstacles")
        or raw.get("escape_enemy_description")
        or raw.get("obstacle_run_obstacles")
        or "enemies").strip()

def _raw_target_desc(raw: dict, goal_type: str) -> str:
    g = (goal_type or "").lower().replace(" ", "_")
    if g == "collecting_goals":
        return (raw.get("collecting_goals_object") or "collectible").strip()
    if g == "rescue_mission":
        return (raw.get("rescue_mission_character") or "character to rescue").strip()
    if g == "escape":
        return "exit gate"
    if g == "time_trial":
        return "finish line flag"
    if g == "obstacle_run":
        return "victory flag"
    return (raw.get("collecting_goals_object") or "the goal").strip()

# ---------------------------------------------------------------------------
# World colors + background layers (always returned, used by template's
# fallback rendering when there's no AI background image)
# ---------------------------------------------------------------------------

def _get_world_bg(game_params: dict) -> dict:
    world_desc = game_params.get("world", {}).get("description", "")
    theme = "default"
    for t in _THEME_BG:
        if t in world_desc.lower():
            theme = t
            break
    bg = _THEME_BG.get(theme, _THEME_BG["default"])
    return {
        "background_layers": bg["layers"],
        "sky_color":         game_params.get("world", {}).get("sky_color", bg["sky"]),
        "ground_color":      game_params.get("world", {}).get("ground_color", bg["ground"]),
        "platform_color":    game_params.get("world", {}).get("platform_color", bg["platform"]),
    }

# ---------------------------------------------------------------------------
# Tier 1: AI sprites via SDXL + RMBG-1.4
# ---------------------------------------------------------------------------

def _singularize(text: str) -> str:
    """Naive English singularization — turns 'cars' → 'car', 'taxis' → 'taxi',
    'spiders' → 'spider'. Prevents SDXL from drawing multiples when the user
    typed a plural. Conservative: only touches the last word."""
    if not text:
        return text
    words = text.strip().split()
    last = words[-1].lower()
    # Common irregulars
    irregulars = {
        "men": "man", "women": "woman", "children": "child",
        "people": "person", "geese": "goose", "mice": "mouse",
    }
    if last in irregulars:
        words[-1] = irregulars[last]
    elif last.endswith("ies") and len(last) > 4:
        words[-1] = last[:-3] + "y"
    elif last.endswith("ses") or last.endswith("xes") or last.endswith("ches") or last.endswith("shes"):
        words[-1] = last[:-2]
    elif last.endswith("s") and not last.endswith("ss") and not last.endswith("us"):
        words[-1] = last[:-1]
    return " ".join(words)


# Style anchor used by EVERY prompt so the hero, obstacle and target look
# like they belong in the same game.
#
# CRITICAL choices:
# - We DON'T use the word "sprite" — the nscale provider we get routed to
#   interprets it as "sprite sheet" (a grid of multiple poses).
# - We DO ask for a bright magenta background — our chroma-key step
#   (_remove_bg_chroma_key below) relies on this exact color.
_STYLE_ANCHOR = (
    "single full-body character illustration, "
    "retro pixel art video game style, vibrant flat colors, "
    "ONE individual subject completely alone, "
    "centered, isolated, "
    "no other characters, no duplicates, no crowd, "
    "side view, no text, no watermark, no signature, no border, no frame, "
    "background must be plain solid bright magenta color #FF00FF, "
    "uniform magenta fill, no shadow, no gradient, no other background elements"
)


def _sprite_prompt(description: str, role: str) -> str:
    """Build a focused prompt asking SDXL for a SINGLE-SUBJECT image.

    Words like 'sprite' and 'sheet' are avoided because some image
    providers (e.g. nscale via HF router) interpret them as 'sprite sheet'
    and produce grids of multiple poses. For obstacles in particular
    (cars, vehicles, items) SDXL's training data is full of catalog/
    showcase compositions ("here are 5 taxis", "lineup of cars"), so we
    push HARD on close-up + single + centered + macro framing language to
    override that bias.
    """
    single = _singularize(description)
    if role == "hero":
        return (
            f"a single full-body character of {single}, alone, hero standing pose, "
            f"facing right, one individual person, {_STYLE_ANCHOR}"
        )
    if role == "obstacle":
        # Extra-aggressive anti-lineup language. The phrases 'close-up',
        # 'large centered', 'single subject filling frame' are the words
        # SDXL associates with portrait/macro compositions instead of
        # catalog rows.
        return (
            f"a single large close-up illustration of exactly ONE {single}, "
            f"one big {single} centered and filling the frame, alone, isolated, "
            f"video game obstacle, just one subject, single object portrait, "
            f"no other {single}s anywhere in the image, {_STYLE_ANCHOR}"
        )
    if role == "target_rescue":
        return (
            f"a single full-body character of {single}, alone, standing front view, "
            f"one individual person, {_STYLE_ANCHOR}"
        )
    # target_item — a collectible object
    return (
        f"a single large close-up of one {single}, alone, isolated game collectible, "
        f"one {single} centered and filling the frame, {_STYLE_ANCHOR}"
    )


def _sdxl_sprite_image(description: str, role: str):
    """Call SDXL to generate the sprite image. Returns a PIL Image."""
    from huggingface_hub import InferenceClient
    token = os.getenv("HF_TOKEN")
    if not token:
        raise RuntimeError("HF_TOKEN not set")

    prompt = _sprite_prompt(description, role)
    negative = (
        # The big ones — block sprite-sheet outputs explicitly
        "sprite sheet, sprite grid, character sheet, multiple poses, "
        "character variations, model sheet, pose chart, thumbnail grid, "
        "side-by-side, collage, comic panels, "
        # Vehicle/object catalog compositions — the specific failure mode
        # we hit with obstacle sprites ("yellow taxis" became 3 stacked
        # variants per sprite). These phrases bias SDXL toward portrait
        # framing instead of catalog/showcase rows.
        "vehicle lineup, car lineup, row of cars, row of vehicles, "
        "rows of objects, stacked vehicles, stacked cars, vehicle gallery, "
        "vehicle collection, car catalog, product catalog, "
        "color variants, vehicle variations, model variations, "
        "two cars, three cars, multiple vehicles, vehicle row, "
        # And the usual single-subject hygiene
        "text, watermark, signature, logo, multiple characters, group, crowd, "
        "duplicates, two people, three people, multiple instances, copies, "
        "blurry, photograph, 3d render"
    )
    logger.info("SDXL sprite [%s]: '%s'", role, prompt[:100])

    client = InferenceClient(token=token)
    last_exc = None
    for attempt in range(3):
        try:
            return client.text_to_image(
                prompt=prompt,
                model="stabilityai/stable-diffusion-xl-base-1.0",
                negative_prompt=negative,
                width=768, height=768,
            )
        except Exception as e:
            last_exc = e
            msg = str(e).lower()
            if "loading" in msg or "503" in msg or "service unavailable" in msg:
                logger.info("SDXL warming up, retrying in 15s (attempt %d/3)", attempt + 1)
                time.sleep(15)
            else:
                raise
    raise last_exc if last_exc else RuntimeError("SDXL failed after 3 attempts")


# Background removal — we try in order:
#   1. Hugging Face RMBG-1.4 via the router endpoint (best quality, costs an
#      HF Pro API call). We saw in the logs that router.huggingface.co is
#      reachable from Render, so this works in production.
#   2. Numpy chroma-key on bright magenta (cheap fallback, but only works
#      when SDXL actually painted a magenta background — which the new
#      nscale provider often doesn't).
# Both end with a bbox crop so the visible subject fills the frame (otherwise
# the character looks like it's floating above platforms).

def _remove_bg_local(pil_image):
    """Strip the background from a generated sprite, returning a PIL Image
    with alpha and cropped to the visible bounding box.

    Two-tier strategy:
      1. PRIMARY — spawn the rembg worker subprocess. High-quality
         AI-based segmentation, works for any background SDXL produces.
         Memory cost is paid only during the subprocess's lifetime.
      2. FALLBACK — corner-sampling chroma-key in-process. Used only if
         the subprocess crashes or times out.
    """
    out = None
    try:
        out = _remove_bg_via_subprocess(pil_image)
        logger.info("Sprite background removed via rembg subprocess")
    except Exception as exc:
        logger.warning("rembg subprocess failed (%s) — falling back to chroma-key", exc)
        out = _remove_bg_chroma_key(pil_image)

    # Crop to non-transparent bbox so the sprite fills its frame
    bbox = out.getbbox()
    if bbox is None:
        return out
    cropped = out.crop(bbox)
    logger.info("Sprite cropped %s → %s", out.size, cropped.size)
    return cropped


def _remove_bg_via_subprocess(pil_image):
    """Spawn a Python subprocess running rembg_worker.py. Communicate via
    temp PNG files. The subprocess's ~250 MB of memory is reclaimed when
    it exits, so successive sprite calls don't accumulate."""
    from PIL import Image as PILImage

    if not os.path.exists(_REMBG_WORKER):
        raise RuntimeError(f"rembg worker not found at {_REMBG_WORKER}")

    # Two named-temp files for input and output
    in_fd,  in_path  = tempfile.mkstemp(suffix=".png", prefix="rembg_in_")
    out_fd, out_path = tempfile.mkstemp(suffix=".png", prefix="rembg_out_")
    os.close(in_fd)
    os.close(out_fd)

    try:
        pil_image.save(in_path, "PNG")

        # Serialize subprocess execution — only one rembg worker at a time
        with _REMBG_LOCK:
            result = subprocess.run(
                [sys.executable, _REMBG_WORKER, in_path, out_path],
                capture_output=True,
                timeout=180,   # generous for Render's 0.1 CPU — locally ~3s,
                               # on Render the subprocess startup + onnxruntime
                               # import + model load + inference can easily
                               # take 30-60s combined.
                check=False,
            )

        if result.returncode != 0:
            err = result.stderr.decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"worker exit {result.returncode}: {err}")

        # Force a full read into memory before deleting the temp file
        img = PILImage.open(out_path).convert("RGBA")
        img.load()
        return img
    finally:
        for p in (in_path, out_path):
            try:
                os.unlink(p)
            except (FileNotFoundError, OSError):
                pass


def _remove_bg_chroma_key(pil_image):
    """Detect-the-background chroma-key.

    Strategy: SDXL doesn't reliably honor "use a magenta background" —
    sometimes it draws taxis on a street, knights on a battlefield, etc.
    So instead of hardcoding "remove magenta", we SAMPLE the four corners
    of the image, check whether they're all similar (which means the
    image has a uniform background), and if so, remove all pixels
    matching that color within a wide tolerance.

    If the corners disagree (subject extends to corners, or background
    is gradient/multicolor) we fall back to magenta + near-white masks
    so we at least catch the standard cases.
    """
    import numpy as np
    from PIL import Image as PILImage

    img = pil_image.convert("RGBA")
    arr = np.array(img).astype(np.int32)
    h, w = arr.shape[:2]

    # Sample 12×12 patches in each corner — averages out noise/grain
    cs = 12
    corner_patches = [
        arr[ :cs,  :cs,  :3],     # top-left
        arr[ :cs, -cs:,  :3],     # top-right
        arr[-cs:,  :cs,  :3],     # bottom-left
        arr[-cs:, -cs:,  :3],     # bottom-right
    ]
    corner_means = [p.reshape(-1, 3).mean(axis=0) for p in corner_patches]

    # If all four corners are within a tight color distance, that's a
    # uniform background → use the mean of all corners as the bg color.
    def _dist(a, b):
        return float(np.linalg.norm(a - b))
    pair_dists = [
        _dist(corner_means[i], corner_means[j])
        for i in range(4) for j in range(i + 1, 4)
    ]
    corners_agree = max(pair_dists) < 70  # generous threshold

    if corners_agree:
        bg = np.mean(corner_means, axis=0)
        # Euclidean distance from each pixel to the background color
        diff = arr[:, :, :3].astype(np.float32) - bg.astype(np.float32)
        dist = np.sqrt((diff * diff).sum(axis=2))
        is_bg = dist < 75  # wide tolerance to catch the AA halo around the subject
        arr[:, :, 3][is_bg] = 0
        logger.info("chroma-key (corner-sampled): bg≈RGB(%d,%d,%d), removed %d%%",
                    int(bg[0]), int(bg[1]), int(bg[2]),
                    int(is_bg.sum() * 100 / (h * w)))
    else:
        # Fallback: hardcoded magenta + near-white masks
        r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
        is_magenta = (r > 100) & (g < 100) & (r > g + 60) & (b > 50)
        is_lightbg = (r > 220) & (g > 220) & (b > 220)
        arr[:, :, 3][is_magenta | is_lightbg] = 0
        logger.info("chroma-key (fallback, corners disagreed): removed %d%%",
                    int((is_magenta | is_lightbg).sum() * 100 / (h * w)))

    return PILImage.fromarray(arr.astype(np.uint8), mode="RGBA")


def _ai_one_sprite(description: str, asset_type: str, role: str) -> str:
    """Generate one transparent-PNG sprite. Returns its public URL.
    Cache-aware: identical (description, role) pairs reuse the same file across games.
    """
    cache_desc = f"{description} | role:{role}"
    hit = asset_cache.lookup(cache_desc, asset_type)
    if hit:
        return hit

    image = _sdxl_sprite_image(description, role)
    transparent = _remove_bg_local(image)
    return asset_cache.save(cache_desc, asset_type, transparent)


def _ai_sprite_urls(game_params: dict) -> dict:
    """Generate hero/obstacle/target sprites in parallel.
    Each sprite has its own try/except — one failure doesn't kill the others.
    Failed sprites get the library SVG fallback for that slot.
    Returns a dict with all three URLs guaranteed populated.
    """
    raw       = game_params.get("_raw_answers", {})
    goal_type = (game_params.get("goal_type") or "").lower().replace(" ", "_")
    is_rescue = goal_type in ("rescue_mission", "escape")

    hero_desc     = _raw_hero_desc(raw)
    obstacle_desc = _raw_obstacle_desc(raw)
    target_desc   = _raw_target_desc(raw, goal_type)
    target_role   = "target_rescue" if is_rescue else "target_item"

    # Each task runs independently; failures are isolated per sprite.
    def safe_one(desc, asset_type, role, svg_fallback, fallback_label):
        try:
            return _ai_one_sprite(desc, asset_type, role)
        except Exception as e:
            logger.warning("AI sprite for %s failed (%s) — using SVG fallback", asset_type, e)
            return _svg_string_to_url(svg_fallback, fallback_label, asset_type)

    target_fallback_svg   = _DEFAULT_RESCUE_TARGET_SVG if is_rescue else _DEFAULT_TARGET_SVG
    target_fallback_label = f"library-target-{'rescue' if is_rescue else 'item'}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        hf = pool.submit(safe_one, hero_desc,     "hero",     "hero",        _DEFAULT_HERO_SVG,     "library-hero")
        of = pool.submit(safe_one, obstacle_desc, "obstacle", "obstacle",    _DEFAULT_OBSTACLE_SVG, "library-obstacle")
        tf = pool.submit(safe_one, target_desc,   "target",   target_role,   target_fallback_svg,   target_fallback_label)
        return {
            "hero_image_url":     hf.result(timeout=240),
            "obstacle_image_url": of.result(timeout=240),
            "target_image_url":   tf.result(timeout=240),
        }

# ---------------------------------------------------------------------------
# Tier 3: library SVG fallback — write SVG strings into cache, return URLs
# ---------------------------------------------------------------------------

def _svg_string_to_url(svg_string: str, cache_desc: str, asset_type: str) -> str:
    """Save an SVG string to /static/cache/<key>.svg and return the URL.
    We bypass asset_cache.save (which is PIL-image-only) and write the SVG
    bytes directly under a stable cache key, so repeated calls reuse the file.
    """
    import hashlib
    os.makedirs(asset_cache.CACHE_DIR, exist_ok=True)
    norm = asset_cache._normalize(cache_desc)
    raw  = f"{asset_type}|{norm}"
    key  = hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]
    path = os.path.join(asset_cache.CACHE_DIR, f"{key}.svg")
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, "w", encoding="utf-8") as f:
            f.write(svg_string)
        logger.info("SVG cache SAVE [%s] %r → %s", asset_type, cache_desc[:60], key)
    return f"{asset_cache.CACHE_URL_PREFIX}/{key}.svg"


def _library_sprite_urls(game_params: dict) -> dict:
    """Final fallback: pick a built-in SVG per role and save as .svg in cache."""
    goal_type = (game_params.get("goal_type") or "").lower().replace(" ", "_")
    is_rescue = goal_type in ("rescue_mission", "escape")
    target_svg = _DEFAULT_RESCUE_TARGET_SVG if is_rescue else _DEFAULT_TARGET_SVG
    return {
        "hero_image_url":     _svg_string_to_url(_DEFAULT_HERO_SVG,     "library-hero",     "hero"),
        "obstacle_image_url": _svg_string_to_url(_DEFAULT_OBSTACLE_SVG, "library-obstacle", "obstacle"),
        "target_image_url":   _svg_string_to_url(target_svg,            f"library-target-{('rescue' if is_rescue else 'item')}", "target"),
    }

# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate_assets(game_params: dict) -> dict:
    """
    Returns a dict with sprite URLs + world colors/layers.

    Sprite generation tries:
      1. AI sprites (SDXL + RMBG)  ← primary
      2. Library SVG fallback       ← when HF is unavailable

    Returned keys:
      hero_image_url, obstacle_image_url, target_image_url   (URLs to PNG or SVG)
      sky_color, ground_color, platform_color                (hex strings)
      background_layers                                       (list of layer dicts)
    """
    world_data = _get_world_bg(game_params)
    try:
        sprite_urls = _ai_sprite_urls(game_params)
        logger.info("Asset agent: AI sprites generated successfully")
    except Exception as exc:
        logger.warning("AI sprite generation failed (%s) — using library SVG fallback", exc)
        sprite_urls = _library_sprite_urls(game_params)
    return {**sprite_urls, **world_data}
