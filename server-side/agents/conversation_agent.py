"""
Agent 1 — Conversation Agent
Converts raw Q&A answers into a structured GameParams dict.
Uses Claude Haiku to infer colors, theme, and title from natural-language descriptions.
Falls back to heuristic extraction if the API call fails.
"""

from __future__ import annotations

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Color heuristics — used as fallback when Gemini is unavailable
# ---------------------------------------------------------------------------

_THEME_COLORS = {
    "space":      {"sky": "#0A0A1A", "ground": "#1A1A2E", "platform": "#2E2E5E", "hero": "#00FFFF", "hero2": "#0088AA"},
    "forest":     {"sky": "#1A3A1A", "ground": "#2D5A1B", "platform": "#4A7A2A", "hero": "#228B22", "hero2": "#8B4513"},
    "city":       {"sky": "#1A1A2E", "ground": "#333355", "platform": "#4A4A6A", "hero": "#FF6B35", "hero2": "#FFB347"},
    "underwater": {"sky": "#003366", "ground": "#004499", "platform": "#0066CC", "hero": "#00FFAA", "hero2": "#00AAFF"},
    "desert":     {"sky": "#CC7722", "ground": "#C2963C", "platform": "#A0722A", "hero": "#FF6600", "hero2": "#FFCC00"},
    "castle":     {"sky": "#2C2C4E", "ground": "#555577", "platform": "#6B6B8B", "hero": "#C0C0C0", "hero2": "#888888"},
    "volcano":    {"sky": "#330000", "ground": "#661100", "platform": "#882200", "hero": "#FF4400", "hero2": "#FFAA00"},
    "snow":       {"sky": "#B0C4DE", "ground": "#E0E8F0", "platform": "#C8D8E8", "hero": "#4169E1", "hero2": "#87CEEB"},
    "default":    {"sky": "#1A1A2E", "ground": "#2D2D4E", "platform": "#3D3D6E", "hero": "#7C3AED", "hero2": "#5865F2"},
}


def _detect_theme(description: str) -> str:
    desc = description.lower()
    for theme in _THEME_COLORS:
        if theme in desc:
            return theme
    keyword_map = {
        "ocean": "underwater", "sea": "underwater", "water": "underwater",
        "jungle": "forest", "tree": "forest", "nature": "forest",
        "rooftop": "city", "building": "city", "urban": "city", "street": "city",
        "planet": "space", "galaxy": "space", "moon": "space", "star": "space",
        "sand": "desert", "dune": "desert",
        "ice": "snow", "tundra": "snow",
        "fire": "volcano", "lava": "volcano",
        "medieval": "castle", "dungeon": "castle", "tower": "castle",
    }
    for kw, theme in keyword_map.items():
        if kw in desc:
            return theme
    return "default"


# ---------------------------------------------------------------------------
# Color keyword extraction — reads explicit color words from user descriptions
# ---------------------------------------------------------------------------

_COLOR_WORDS = {
    "red": "#CC0000",    "crimson": "#DC143C",  "scarlet": "#FF2400",
    "blue": "#0044CC",   "navy": "#000080",     "cobalt": "#0047AB",   "cyan": "#00BBCC",
    "green": "#228B22",  "emerald": "#50C878",  "lime": "#32CD32",
    "yellow": "#FFD700", "gold": "#FFA500",
    "purple": "#7B2FBE", "violet": "#8B00FF",   "magenta": "#CC00CC",
    "orange": "#FF6600", "amber": "#FFBF00",
    "white": "#EEEEEE",  "silver": "#C0C0C0",   "gray": "#888888",  "grey": "#888888",
    "black": "#222222",  "dark": "#222233",
    "pink": "#FF69B4",   "brown": "#8B4513",
}


def _extract_color(desc: str, fallback: str) -> str:
    """Return the first colour word found in desc, or fallback."""
    for word, hex_val in _COLOR_WORDS.items():
        if word in desc.lower():
            return hex_val
    return fallback


def _heuristic_params(raw_answers: dict) -> dict:
    """Pure-Python fallback — no API calls.
    Extracts explicit colour words from descriptions so sprites match what the user typed.
    """
    name        = (raw_answers.get("name") or "Player").strip()
    hero_desc   = raw_answers.get("hero_description", "a brave hero")
    world_desc  = raw_answers.get("game_location", "a mysterious land")
    goal_raw    = (raw_answers.get("hero_goal") or "collecting goals").strip().lower()

    theme   = _detect_theme(world_desc)
    colors  = _THEME_COLORS[theme]

    # Pick a target description from whichever branch the user chose
    target_desc = (
        raw_answers.get("collecting_goals_object")
        or raw_answers.get("rescue_mission_character")
        or "the goal"
    )
    obstacle_desc = (
        raw_answers.get("collecting_goals_obstacles")
        or raw_answers.get("rescue_mission_obstacles")
        or raw_answers.get("time_trial_obstacles")
        or raw_answers.get("escape_enemy_description")
        or raw_answers.get("obstacle_run_obstacles")
        or "enemies"
    )

    # Extract explicit colour words — so "Superman with blue suit and red cape"
    # → primary=#0044CC, secondary=#CC0000 instead of generic theme orange
    hero_primary   = _extract_color(hero_desc,     colors["hero"])
    hero_secondary = _extract_color(
        hero_desc.split(" and ")[-1] if " and " in hero_desc else hero_desc,
        colors["hero2"]
    )
    obstacle_color = _extract_color(obstacle_desc, "#FF4444")
    target_color   = _extract_color(target_desc,   "#FFD700")

    return {
        "player_name":  name,
        "game_title":   f"{name}'s Adventure",
        "hero": {
            "description":     hero_desc,
            "primary_color":   hero_primary,
            "secondary_color": hero_secondary,
            "accent_color":    "#FFFFFF",
        },
        "world": {
            "description":        world_desc,
            "sky_color":          colors["sky"],
            "ground_color":       colors["ground"],
            "platform_color":     colors["platform"],
            "background_palette": list(colors.values()),
        },
        "goal_type": goal_raw.replace(" ", "_"),
        "target": {
            "description": target_desc,
            "color":       target_color,
        },
        "obstacles": {
            "description":     obstacle_desc,
            "primary_color":   obstacle_color,
            "secondary_color": "#AA2222",
            "motion_type":     _detect_motion(obstacle_desc),
        },
    }


_FLYING_WORDS = (
    "fly", "flying", "drone", "bird", "bat", "ghost", "spirit", "dragon",
    "ufo", "air", "winged", "phantom", "wraith",
)
_STATIONARY_WORDS = (
    "spike", "trap", "thorn", "saw", "fire pit", "lava", "mine", "spear",
)

def _detect_motion(description: str) -> str:
    d = description.lower()
    if any(w in d for w in _FLYING_WORDS):
        return "flying"
    if any(w in d for w in _STATIONARY_WORDS):
        return "stationary"
    return "ground"


def extract_game_params(raw_answers: dict) -> dict:
    """
    Main entry point.
    Tries Claude first; falls back to heuristics on any failure.
    """
    try:
        return _claude_extract(raw_answers)
    except Exception as exc:
        logger.warning("Conversation agent Claude call failed (%s) — using heuristics", exc)
        return _heuristic_params(raw_answers)


def _claude_extract(raw_answers: dict) -> dict:
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)

    prompt = f"""You are a game design assistant. Given these player answers, produce a structured JSON object.

PLAYER ANSWERS:
{json.dumps(raw_answers, indent=2)}

Return ONLY valid JSON (no markdown) with this exact structure:
{{
  "player_name": "string",
  "game_title": "string (creative title based on the answers)",
  "hero": {{
    "description": "string",
    "primary_color": "#RRGGBB",
    "secondary_color": "#RRGGBB",
    "accent_color": "#RRGGBB",
    "size_scale": 1.0
  }},
  "world": {{
    "description": "string",
    "sky_color": "#RRGGBB",
    "ground_color": "#RRGGBB",
    "platform_color": "#RRGGBB",
    "background_palette": ["#RRGGBB", "#RRGGBB", "#RRGGBB", "#RRGGBB", "#RRGGBB"]
  }},
  "goal_type": "collecting_goals|rescue_mission|time_trial|escape|obstacle_run",
  "target": {{
    "description": "string",
    "color": "#RRGGBB",
    "size_scale": 1.0
  }},
  "obstacles": {{
    "description": "string",
    "primary_color": "#RRGGBB",
    "secondary_color": "#RRGGBB",
    "motion_type": "ground|flying|stationary",
    "size_scale": 1.0
  }}
}}

The "size_scale" field is the subject's size relative to an average human adult, who is 1.0. It controls how big each character renders on screen and how big their collision hitbox is. Examples:

- adult human, knight, princess, person ........ 1.0
- child, teenager ................................ 0.85
- small dog, cat, fox, rabbit, raccoon ........... 0.6
- mouse, frog, bird, butterfly, coin, gem ........ 0.4
- large dog, wolf, panda, sheep .................. 0.9
- big bear, lion, gorilla, elephant ............... 1.6
- dragon, giant, troll, ogre, T-rex ............... 2.2
- huge boss creature, mecha, godzilla ............ 3.0
- car, taxi, motorbike, scooter .................. 1.4
- truck, bus ..................................... 2.0
- tank, large machine ............................. 2.5

Pick the closest match. If hero is "a brown dog with a red collar", size_scale=0.6. If obstacle is "a red fire-breathing dragon", size_scale=2.2. If hero is "a brave knight", size_scale=1.0. If hero AND obstacle are both animals of similar real-world size (e.g. dog vs cat), keep them in proportion — a dog is larger than a cat so dog=0.6 and cat=0.5. Get the relative scale right so the game world feels plausible.

Choose colors that visually match the descriptions. If the hero is a known character (e.g. Superman, Mario, Batman), use that character's canonical color palette. For locations, infer the atmosphere (NYC → urban dark blue/grey, Mars → orange/red, etc.). All colors must be valid hex codes.

For obstacles.motion_type, classify so the obstacle is FUN to dodge in a side-scrolling platformer.
DEFAULT to "ground" for almost everything — moving enemies are way more interesting than static ones.
- "ground"     — moves on the floor (will be made to chase the player in-game): cars, taxis, zombies, knights, slimes, dogs, wolves, soldiers, robots, MUSHROOMS, monsters, animals, creatures, basically any living thing or vehicle
- "flying"     — moves through the air (hovers + chases): drones, ghosts, bats, dragons, birds, UFOs, spirits, anything with wings or that flies
- "stationary" — ONLY use for truly fixed hazards: spike traps, thorns, fire pits, saws, lava pools, mines, spear traps. If in doubt, pick "ground" — it makes the game playable.

Return ONLY the JSON object, no markdown.""".strip()

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()

    # Strip markdown fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    params = json.loads(text)
    logger.info("Conversation agent extracted params: %s", params.get("game_title"))
    return params
