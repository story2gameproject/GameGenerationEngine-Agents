# Storybased Game Maker

A web app that turns a chat conversation into a fully playable 2D platform game. The user answers a handful of questions about their hero, world, and goal — and the system generates a standalone HTML platformer with AI-drawn pixel art, an AI-generated background scene, and a level laid out to match the story.

![Status: Demo](https://img.shields.io/badge/status-demo-blue)

## What it does

1. **Chat** — bot asks: your name, hero description, location, goal type (collect / rescue / time-trial / escape / obstacle run), then 1-2 follow-ups depending on the goal.
2. **Generate** — four agents run in parallel:
   - **Conversation agent** parses your answers into a structured `game_params` dict (hero colors, theme, goal type).
   - **Asset agent** generates 3 transparent-PNG sprites (hero, obstacle, target) via Stable Diffusion XL + local background-removal.
   - **Level agent** picks difficulty parameters and lays out 10–20 platforms, enemies, and collectibles deterministically.
   - **Background agent** generates a wide cinematic scene image via Stable Diffusion XL.
3. **Play** — a `Play Game` button opens a self-contained HTML game in a new tab. Move with arrow keys / WASD, jump with Space.

Each generated asset is cached by description hash, so repeat scenarios reuse the same art with zero API calls.

## Architecture

```
client-side/
  templates/index.html    — chat UI (cream/arcade theme)
  static/script_v2.js     — chat logic, message rendering, status rotation
  static/style.css        — pixel-art styling

server-side/
  web_server_v2.py        — Flask app, /api/chat endpoint, Q&A state machine
  image_vision.py         — Claude-Vision helper (currently disabled in UI)
  templates/
    platform_game.html.template  — the generated-game engine (canvas + physics)
  agents/
    orchestrator.py       — coordinates the four agents in parallel
    conversation_agent.py — Claude → game_params dict
    asset_agent.py        — SDXL + rembg → 3 transparent sprite PNGs
    level_agent.py        — Claude difficulty + deterministic placement
    background_agent.py   — SDXL → wide background image
    asset_cache.py        — content-addressable asset cache (/static/cache/)
```

## Local setup

### 1. Create a virtual environment

```bash
cd GameGenerationEngine
python3 -m venv venv
source venv/bin/activate           # macOS / Linux
# venv\Scripts\activate.bat        # Windows
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

First install will download ~180 MB (mostly the rembg U2Net model for background removal — local, no network calls during inference).

### 3. Configure API keys

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Edit `.env` and fill in your keys:

| Key | Where to get it | What for |
|---|---|---|
| `ANTHROPIC_API_KEY` | https://console.anthropic.com/ | Conversation agent, level difficulty, asset prompt design |
| `HF_TOKEN` | https://huggingface.co/settings/tokens | Stable Diffusion XL image generation |

The app degrades gracefully if either key is missing — agents fall back to heuristics + a built-in SVG sprite library.

### 4. Run the server

```bash
python server-side/web_server_v2.py
```

Open <http://localhost:10000> in your browser.

## How a typical game gets generated

1. User completes the chat — answers are stored in `user_states[session_id]['answers']`.
2. Final answer triggers `finalize_structured_conversation()`.
3. Orchestrator runs the four agents (~10–25 seconds wall time when uncached, ~3 s when fully cached).
4. Final HTML is written to `client-side/static/games/game_<uuid>.html` and the URL is returned to the chat.

## Deployment

Designed for **Render** (free tier supported). Add the two API keys as environment variables on your service dashboard. The repo's `requirements.txt` and `render.yaml` (if present) handle the rest.

Build command: `pip install -r requirements.txt`
Start command: `python server-side/web_server_v2.py`
The server listens on `$PORT` (defaults to 10000) on `0.0.0.0`.

## Credits / models used

- **Anthropic Claude Haiku 4.5** — conversation parsing, level difficulty, asset prompt design, image-vision (when enabled).
- **Stable Diffusion XL** (`stabilityai/stable-diffusion-xl-base-1.0` via Hugging Face Inference API) — sprite + background image generation.
- **rembg + U2Net** — local background removal so sprites render with transparent backgrounds.

## License

MIT — feel free to learn from / fork / remix.
