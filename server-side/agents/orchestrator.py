"""
Orchestrator
Coordinates the four agents and injects their outputs into the game template.

Flow:
  1. conversation_agent  →  game_params                     (~2s, sequential)
  2. asset_agent + level_agent + background_agent in parallel  (~30s, concurrent)
  3. inject_into_template                                    (~0.1s)
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import string

logger = logging.getLogger(__name__)

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "..", "templates", "platform_game.html.template")
# Where backgrounds get saved — same folder games live in, so the game HTML
# can reference them via a relative /static/games/bg_*.png URL.
GAMES_FOLDER  = os.path.join(os.path.dirname(__file__), "..", "..", "client-side", "static", "games")


class GameGenerationError(Exception):
    pass


def generate_game(raw_answers: dict) -> str:
    """
    Main entry point called by web_server_v2.py.
    Returns a complete HTML string.
    Raises GameGenerationError on unrecoverable failure.
    """
    from agents import conversation_agent, asset_agent, level_agent, background_agent

    # ── Step 1: extract structured params from Q&A answers ────────────────
    logger.info("Orchestrator: Step 1 — Conversation Agent")
    game_params = conversation_agent.extract_game_params(raw_answers)
    # Attach the raw user answers so downstream agents can use them for
    # CACHE STABILITY — e.g. the user typing "NYC" twice should hit the
    # same cache key, even if Claude expanded it differently each time.
    game_params["_raw_answers"] = raw_answers

    # ── Step 2: assets + level + background IN PARALLEL ───────────────────
    # The background image (SDXL) is the slow one — running it concurrently with
    # the Claude calls means total wall time = the longest single agent.
    logger.info("Orchestrator: Step 2 — Asset + Level + Background agents (parallel)")
    os.makedirs(GAMES_FOLDER, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        asset_future = pool.submit(asset_agent.generate_assets,       game_params)
        level_future = pool.submit(level_agent.generate_level,        game_params)
        bg_future    = pool.submit(background_agent.generate_background, game_params, GAMES_FOLDER)

        # Asset agent takes the longest on memory-constrained hosts because
        # the rembg subprocess is serialized (3 × ~30s on Render free tier).
        # Background and level are quicker (Claude / single API call).
        assets     = asset_future.result(timeout=300)
        level_data = level_future.result(timeout=60)
        bg_url     = bg_future.result(timeout=180)

    # ── Step 3: inject into template ──────────────────────────────────────
    logger.info("Orchestrator: Step 3 — Template injection (bg_url=%s)", bg_url)
    html = inject_into_template(game_params, assets, level_data, bg_url)

    logger.info("Orchestrator: game generated successfully — %d bytes", len(html))
    return html


def _js(value) -> str:
    """Serialize a Python value to a safe JS literal (JSON is valid JS)."""
    return json.dumps(value, ensure_ascii=False)


_GOAL_META = {
    # goal_type          : (label,        win_message,                   collectibles_needed)
    "collecting_goals"   : ("COLLECT",    "You collected everything",    True),
    "rescue_mission"     : ("RESCUE",     "Mission accomplished",        False),
    "time_trial"         : ("FINISH",     "You made it in time",         False),
    "escape"             : ("ESCAPE",     "You escaped",                 False),
    "obstacle_run"       : ("FINISH",     "You completed the run",       False),
}


def inject_into_template(game_params: dict, assets: dict, level_data: dict, bg_url: str | None = None) -> str:
    """
    Reads the template file and substitutes all $VARNAME placeholders.
    Uses string.Template so the JS engine code (which contains {}) is untouched.
    """
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        raw = f.read()

    # Resolve goal-type metadata
    goal_type = game_params.get("goal_type", "collecting_goals").lower().replace(" ", "_")
    label, win_msg, needs_collectibles = _GOAL_META.get(
        goal_type, ("GOAL", "Well done", True)
    )

    # Human-readable name of what the player chases/collects.
    # Keep it readable in the HUD but preserve the user's descriptive phrase
    # (e.g. "princess with a pink dress", not just "princess with a").
    raw_target = (game_params.get("target", {}).get("description") or "goal").strip()
    # Clean: strip trailing punctuation, drop leading "A "/"An "/"The " articles
    # so the HUD reads "Find the princess" not "Find the The princess,"
    raw_target = raw_target.rstrip(" ,.;:!?")
    for article in ("the ", "The ", "a ", "A ", "an ", "An "):
        if raw_target.startswith(article):
            raw_target = raw_target[len(article):]
            break
    if len(raw_target) <= 40:
        target_name = raw_target
    else:
        # Soft cap at ~40 chars, snap to a word boundary so we never cut a word in half
        target_name = raw_target[:40].rsplit(" ", 1)[0]

    # For non-collecting goals, set required to 0 so goal is immediately reachable,
    # AND remove the collectibles from the level entirely so they don't litter the world
    # (a rescue mission shouldn't have decorative coins scattered around).
    if needs_collectibles:
        collectibles_required = level_data["collectibles_required"]
        collectibles_list     = level_data["collectibles"]
    else:
        collectibles_required = 0
        collectibles_list     = []

    tmpl = string.Template(raw)

    # Per-character size scales. Conversation Agent estimated each
    # subject's size relative to a human adult (1.0). Dragon ~ 2.2,
    # small dog ~ 0.6, taxi ~ 1.4, etc. The template uses these to
    # render visuals (and scale hitboxes for non-player characters) so
    # a dragon is visually imposing and a kitten is appropriately tiny.
    hero_scale     = float(game_params.get("hero", {}).get("size_scale", 1.0))
    obstacle_scale = float(game_params.get("obstacles", {}).get("size_scale", 1.0))
    target_scale   = float(game_params.get("target", {}).get("size_scale", 1.0))

    return tmpl.substitute(
        GAME_TITLE           = _js(game_params.get("game_title", "My Game")),
        PLAYER_NAME          = _js(game_params.get("player_name", "Player")),
        GOAL_TYPE            = _js(goal_type),
        GOAL_LABEL           = _js(label),
        WIN_MESSAGE          = _js(win_msg),
        TARGET_NAME          = _js(target_name),
        HERO_IMG_URL         = _js(assets["hero_image_url"]),
        OBSTACLE_IMG_URL     = _js(assets["obstacle_image_url"]),
        TARGET_IMG_URL       = _js(assets["target_image_url"]),
        # Directional flags — true means "flip the sprite on left-walk"
        # (i.e., it's in side profile). False for forward/back/unclear
        # sprites where flipping would look wrong; the game renders those
        # without flipping. Default True for backward compatibility.
        HERO_DIRECTIONAL     = _js(bool(assets.get("hero_directional", True))),
        OBSTACLE_DIRECTIONAL = _js(bool(assets.get("obstacle_directional", True))),
        # Size scales — applied per-character in the template.
        HERO_SIZE_SCALE      = hero_scale,
        OBSTACLE_SIZE_SCALE  = obstacle_scale,
        TARGET_SIZE_SCALE    = target_scale,
        SKY_COLOR            = _js(assets["sky_color"]),
        GROUND_COLOR         = _js(assets["ground_color"]),
        PLATFORM_COLOR       = _js(assets["platform_color"]),
        BACKGROUND_LAYERS_JS = _js(assets["background_layers"]),
        BACKGROUND_IMAGE_URL = _js(bg_url or ""),
        PLATFORMS_JS         = _js(level_data["platforms"]),
        ENEMIES_JS           = _js(level_data["enemies"]),
        COLLECTIBLES_JS      = _js(collectibles_list),
        COLLECTIBLES_REQUIRED= collectibles_required,
        GOAL_X               = level_data["goal_x"],
        GOAL_Y               = level_data["goal_y"],
        LEVEL_WIDTH          = level_data["level_width"],
    )
