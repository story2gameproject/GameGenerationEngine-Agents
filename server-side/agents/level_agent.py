"""
Agent 3 — Level Agent
Generates platform/enemy/collectible positions.
Uses Claude Haiku to get difficulty parameters, then places objects deterministically
so geometry is always valid (no overlaps, reachable collectibles).
Falls back to preset configs if API is unavailable.
"""

from __future__ import annotations

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

LEVEL_WIDTH  = 5000
CANVAS_H     = 550
GROUND_Y     = 490   # top of the ground rect
PLATFORM_H   = 18

# ---------------------------------------------------------------------------
# Preset difficulty configs (fallback)
# ---------------------------------------------------------------------------

_PRESETS = {
    "easy":   {"platform_count": 18, "platform_gap": 180, "enemy_count": 5,  "enemy_speed": 1.5, "collectible_count": 6,  "height_variance": 60},
    "medium": {"platform_count": 15, "platform_gap": 220, "enemy_count": 8,  "enemy_speed": 2.0, "collectible_count": 8,  "height_variance": 90},
    "hard":   {"platform_count": 12, "platform_gap": 270, "enemy_count": 12, "enemy_speed": 2.8, "collectible_count": 10, "height_variance": 120},
}


def _build_level(cfg: dict) -> dict:
    """
    Deterministic placement algorithm.
    Guarantees: no enemy spawns inside platforms, collectibles sit 40px above a surface.
    Seed is derived from game content so every game looks different.
    """
    import random
    import hashlib
    seed_str = cfg.get("seed_key", "default")
    seed = int(hashlib.md5(seed_str.encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)

    pc      = cfg["platform_count"]
    gap     = cfg["platform_gap"]
    ec      = cfg["enemy_count"]
    espeed  = cfg["enemy_speed"]
    cc      = cfg["collectible_count"]
    hvar    = cfg["height_variance"]

    # ── Platforms ──────────────────────────────────────────────────────────
    platforms = []
    x = 350
    for i in range(pc):
        w = rng.randint(90, 160)
        y = GROUND_Y - 80 - rng.randint(0, hvar)
        y = max(180, min(y, GROUND_Y - 80))   # clamp
        platforms.append({"x": x, "y": y, "width": w, "height": PLATFORM_H})
        x += w + gap + rng.randint(-30, 30)
        x = min(x, LEVEL_WIDTH - 500)

    # ── Enemies — placement varies by motion type AND goal type ───────────
    motion_type = cfg.get("motion_type", "ground")
    goal_type   = cfg.get("goal_type", "")
    is_escape   = goal_type == "escape"

    enemies = []

    if is_escape:
        # ── Escape mode: a SINGLE pursuer placed just behind the hero.
        # The activation_delay_ms field tells the template to hold the
        # enemy in place for the first couple seconds — giving the player
        # time to start running. After that, it chases rightward forever.
        pursuer_speed = round(espeed * 1.5 + rng.uniform(-0.2, 0.2), 1)
        pursuer = {
            "x": 20,             # player spawns at x=80; pursuer is just behind
            "patrol_range": 0,
            "speed": pursuer_speed,
            "activation_delay_ms": 2200,   # ~2.2 second head start
        }
        if motion_type == "flying":
            pursuer.update({
                "y": 280,
                "motion": "flying",
                "bob_amplitude": 35,
                "bob_speed": 0.03,
            })
        else:
            pursuer.update({
                "y": GROUND_Y,
                "motion": "ground",
            })
        enemies.append(pursuer)
    else:
        # ── Normal modes: spread enemies across the level ─────────────────
        enemy_xs = set()
        spawn_max = LEVEL_WIDTH - 400
        segment   = spawn_max // (ec + 1)
        for i in range(ec):
            ex = segment * (i + 1) + rng.randint(-80, 80)
            ex = max(200, min(ex, spawn_max))
            while any(abs(ex - e) < 150 for e in enemy_xs):
                ex += 160
            enemy_xs.add(ex)

            if motion_type == "flying":
                ey = rng.randint(200, 360)
                enemy = {
                    "x": ex, "y": ey,
                    "patrol_range": rng.randint(120, 220),
                    "speed": round(espeed + rng.uniform(-0.3, 0.3), 1),
                    "motion": "flying",
                    "bob_amplitude": rng.randint(20, 50),
                    "bob_speed":     round(rng.uniform(0.02, 0.05), 3),
                }
            elif motion_type == "stationary":
                enemy = {
                    "x": ex, "y": GROUND_Y,
                    "patrol_range": 0,
                    "speed": 0,
                    "motion": "stationary",
                }
            else:
                enemy = {
                    "x": ex, "y": GROUND_Y,
                    "patrol_range": rng.randint(80, 160),
                    "speed": round(espeed + rng.uniform(-0.3, 0.3), 1),
                    "motion": "ground",
                }
            enemies.append(enemy)

    # ── Collectibles (above platforms or floating safely above ground) ─────
    collectibles = []
    available_platforms = platforms[:]
    rng.shuffle(available_platforms)

    for i in range(cc):
        if i < len(available_platforms):
            p = available_platforms[i]
            cx = p["x"] + p["width"] // 2
            cy = p["y"] - 40
        else:
            # Float above ground
            cx = rng.randint(300, LEVEL_WIDTH - 400)
            cy = GROUND_Y - 80 - rng.randint(20, 60)
        collectibles.append({"x": cx, "y": cy})

    return {
        "level_width":           LEVEL_WIDTH,
        "platforms":             platforms,
        "enemies":               enemies,
        "collectibles":          collectibles,
        "collectibles_required": max(3, cc // 2),
        "goal_x":                LEVEL_WIDTH - 200,
        "goal_y":                GROUND_Y - 80,
    }


def generate_level(game_params: dict) -> dict:
    """
    Main entry point.
    Tries Claude for difficulty params, then places objects deterministically.
    Falls back to medium preset on any failure.
    """
    # Build a seed key so each unique game gets a unique level layout
    seed_key = (
        game_params.get("player_name", "")
        + game_params.get("game_title", "")
        + game_params.get("goal_type", "")
        + game_params.get("hero", {}).get("description", "")
    )
    # Pull the motion classification from conversation_agent so we can place
    # enemies at the right height ("ground" / "flying" / "stationary"). Also
    # pass the goal type — escape games need enemies placed BEHIND the hero
    # and chasing rightward.
    motion_type = game_params.get("obstacles", {}).get("motion_type", "ground")
    goal_type   = (game_params.get("goal_type") or "").lower().replace(" ", "_")

    try:
        cfg = _claude_difficulty(game_params)
        cfg["seed_key"]    = seed_key
        cfg["motion_type"] = motion_type
        cfg["goal_type"]   = goal_type
        return _build_level(cfg)
    except Exception as exc:
        logger.warning("Level agent Claude call failed (%s) — using medium preset", exc)
        preset = dict(_PRESETS["medium"])
        preset["seed_key"]    = seed_key
        preset["motion_type"] = motion_type
        preset["goal_type"]   = goal_type
        return _build_level(preset)


def _claude_difficulty(game_params: dict) -> dict:
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)

    goal_type = game_params.get("goal_type", "collecting_goals")
    world     = game_params.get("world", {}).get("description", "a world")
    hero      = game_params.get("hero", {}).get("description", "a hero")
    obstacle  = game_params.get("obstacles", {}).get("description", "enemies")

    prompt = f"""You are a game level designer.

Game info:
- Hero: {hero}
- World: {world}
- Goal type: {goal_type}
- Obstacles: {obstacle}

Choose difficulty parameters for the level layout.
Return ONLY valid JSON:
{{
  "platform_count": integer 10-20,
  "platform_gap": integer 150-300,
  "enemy_count": integer 4-14,
  "enemy_speed": float 1.0-3.5,
  "collectible_count": integer 5-12,
  "height_variance": integer 40-130
}}

Match difficulty to the tone: dangerous/action themes = harder, peaceful themes = easier.
Return ONLY the JSON object, no markdown.""".strip()

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    cfg = json.loads(text)

    # Clamp all values to safe ranges
    cfg["platform_count"]    = max(10, min(int(cfg.get("platform_count", 15)),    20))
    cfg["platform_gap"]      = max(150, min(int(cfg.get("platform_gap", 220)),    300))
    cfg["enemy_count"]       = max(4,  min(int(cfg.get("enemy_count", 8)),        14))
    cfg["enemy_speed"]       = max(1.0, min(float(cfg.get("enemy_speed", 2.0)),   3.5))
    cfg["collectible_count"] = max(5,  min(int(cfg.get("collectible_count", 8)),  12))
    cfg["height_variance"]   = max(40, min(int(cfg.get("height_variance", 90)),  130))

    logger.info("Level agent config: %s", cfg)
    return cfg
