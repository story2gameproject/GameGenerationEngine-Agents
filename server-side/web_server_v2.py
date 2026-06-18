"""
Web-based Game Maker Chat Application Server - V3 (Cleaned up, Game Only)
"""

import logging
import os
import threading
import time
import uuid

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS
from werkzeug.utils import secure_filename

import sys
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from agents.orchestrator import generate_game
from image_vision import image_to_description

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'), override=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

base_dir = os.path.abspath(os.path.dirname(__file__))
client_side_dir = os.path.join(base_dir, "..", "client-side")

app = Flask(
    __name__,
    template_folder=os.path.join(client_side_dir, "templates"),
    static_folder=os.path.join(client_side_dir, "static"),
)
CORS(app)


# ──────────────────────────────────────────────────────────────────────────
# Cache-busting helper for static files
# ──────────────────────────────────────────────────────────────────────────
# Browsers aggressively cache /static/* responses, which means our JS/CSS
# changes don't reach users after a deploy until they hard-refresh. To fix
# this we expose a `static_version()` Jinja function that returns the
# file's mtime (integer seconds). The HTML appends it as `?v=1717603200`
# to the script/stylesheet URL — when the file changes, the version
# changes, the URL changes, and the browser fetches the new copy.
@app.context_processor
def _inject_static_version():
    def static_version(filename: str) -> str:
        path = os.path.join(app.static_folder, filename)
        try:
            return str(int(os.path.getmtime(path)))
        except OSError:
            # File missing — fall back to a constant; the browser will at
            # least try once and Flask will 404 if it really doesn't exist.
            return "0"
    return {"static_version": static_version}


UPLOAD_FOLDER = os.path.join(app.static_folder, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

GAMES_FOLDER = os.path.join(app.static_folder, "games")
os.makedirs(GAMES_FOLDER, exist_ok=True)

# API keys are loaded by load_dotenv() above; agents read them via os.getenv()
# directly. We just verify Anthropic is present and warn (don't crash) if not —
# the agents have built-in fallbacks for the no-key case.
if not os.getenv("ANTHROPIC_API_KEY"):
    logger.warning("ANTHROPIC_API_KEY not set — agents will use built-in fallbacks")
if not os.getenv("HF_TOKEN"):
    logger.warning("HF_TOKEN not set — background + sprites will use SVG fallback")

conversation_questions = {
    "name": {"question": "What is your name?", "type": "text", "next_question": "hero_description"},
    "hero_description": {"question": "Describe your main hero:", "type": "text", "next_question": "game_location"},
    "game_location": {"question": "Where does the game take place?", "type": "text", "next_question": "hero_goal"},
    "hero_goal": {
        "question": "What is the hero's goal?",
        "type": "choice",
        "options": ["collecting goals", "rescue mission", "time trial", "escape", "obstacle run"],
        "next_question_map": {
            "collecting goals": "collecting_goals_object",
            "rescue mission": "rescue_mission_character",
            "time trial": "time_trial_type",
            "escape": "escape_enemy_description",
            "obstacle run": "obstacle_run_obstacles",
        },
    },
    "collecting_goals_object": {"question": "What object does the hero want to collect?", "type": "text", "next_question": "collecting_goals_obstacles"},
    "collecting_goals_obstacles": {"question": "What obstacles do you want to have?", "type": "text"},
    "rescue_mission_character": {"question": "What character does the hero want to rescue?", "type": "text", "next_question": "rescue_mission_obstacles"},
    "rescue_mission_obstacles": {"question": "What obstacles do you want to have?", "type": "text"},
    "time_trial_type": {"question": "Is the game a stopwatch or stay-in-frame kind of game?", "type": "choice", "options": ["stopwatch", "stay in frame"], "next_question": "time_trial_obstacles"},
    "time_trial_obstacles": {"question": "What obstacles do you want to have?", "type": "text"},
    "escape_enemy_description": {"question": "How do you describe the enemy?", "type": "text"},
    "obstacle_run_obstacles": {"question": "What are the obstacles you want to have?", "type": "text"},
}

user_states = {}

# ──────────────────────────────────────────────────────────────────────────
# Background job tracking
# ──────────────────────────────────────────────────────────────────────────
# Game generation takes 2–4 minutes on Render's free tier (rembg subprocess
# is the bottleneck — 3 sprites × ~30–60s serialized). That's well past
# Render's HTTP-proxy timeout (~100s for non-streaming responses), so we
# cannot block on /api/chat for the full duration — the proxy will cut the
# connection and the browser will get an empty body ("Unexpected end of JSON
# input").
#
# Fix: when the conversation ends, we start a background thread, return a
# job_id immediately, and let the client poll /api/job/<job_id> every few
# seconds until the job is done.
#
# `jobs` is an in-memory dict {job_id: {status, game_url, error, ...}}.
# Acceptable for a single-instance demo deployment. A multi-worker setup
# would need Redis or similar.
jobs: dict = {}
jobs_lock = threading.Lock()


def _normalize_obstacles(val: str) -> str:
    if val is None:
        return "No obstacles specified (default)"
    v = str(val).strip()
    if v == "" or v.lower() in ("none", "no", "n/a", "no obstacles"):
        return "No obstacles specified (default)"
    return v


def _map_answers_to_game_json(answers: dict) -> dict:
    goal_map = {
        "collecting goals": "Collecting goals",
        "rescue mission": "Rescue mission",
        "time trial": "Time trial",
        "escape": "Escape",
        "obstacle run": "Obstacle run",
    }

    hero_goal_raw = (answers.get("hero_goal") or "").strip().lower()
    goal_type = goal_map.get(hero_goal_raw, "Rescue mission")

    character = answers.get("hero_description", "main character")
    background = answers.get("game_location", "game level")

    if hero_goal_raw == "collecting goals":
        target = answers.get("collecting_goals_object", "collectible")
        obstacles = _normalize_obstacles(answers.get("collecting_goals_obstacles"))
    elif hero_goal_raw == "rescue mission":
        target = answers.get("rescue_mission_character", "rescued character")
        obstacles = _normalize_obstacles(answers.get("rescue_mission_obstacles"))
    elif hero_goal_raw == "time trial":
        target = "Finish line"
        obstacles = _normalize_obstacles(answers.get("time_trial_obstacles"))
    elif hero_goal_raw == "escape":
        target = "Exit gate"
        obstacles = answers.get("escape_enemy_description", "enemy")
    elif hero_goal_raw == "obstacle run":
        target = "Victory flag"
        obstacles = _normalize_obstacles(answers.get("obstacle_run_obstacles"))
    else:
        target = "Goal"
        obstacles = "Obstacles"

    return {
        "goal_type": goal_type,
        "character": character,
        "background": background,
        "obstacles": obstacles,
        "target": target,
    }


def _maybe_describe_image(user_message: str, session_id: str) -> str:
    """If `user_message` is an uploaded image URL, run Claude Vision on it and
    return the resulting text description. Otherwise return the message
    unchanged. This lets uploaded photos actually influence the generated
    game art instead of being treated as opaque URL strings."""
    if not user_message or not user_message.startswith("/static/uploads/"):
        return user_message

    # Resolve URL → on-disk file path
    relative = user_message[len("/static/"):]   # 'uploads/foo.png'
    file_path = os.path.join(app.static_folder, relative)
    if not os.path.exists(file_path):
        logger.warning("Image vision: file not found at %s", file_path)
        return user_message

    # What question is being answered? Determines the prompt context.
    current = user_states.get(session_id, {}).get("current_question", "")
    context = {
        "hero_description":         "main hero character",
        "rescue_mission_character": "character to rescue",
        "escape_enemy_description": "enemy or villain to escape from",
    }.get(current, "subject")

    description = image_to_description(file_path, context)
    if description:
        logger.info("Image %s → '%s' (context: %s)",
                    os.path.basename(file_path), description, context)
        return description
    return user_message


def handle_user_text(user_message: str, session_id: str) -> dict:
    # If the user uploaded an image, swap the URL for a Claude-Vision
    # description before processing — this is what gets stored as the
    # answer and what the downstream agents see.
    user_message = _maybe_describe_image(user_message, session_id)

    if session_id not in user_states or user_states[session_id].get("current_question") == "complete":
        user_states[session_id] = {"current_question": "name", "answers": {}}
        first_q = conversation_questions["name"]

        if user_message:
            user_states[session_id]["answers"]["name"] = user_message
            next_key = first_q.get("next_question")
            if next_key:
                user_states[session_id]["current_question"] = next_key
                next_q = conversation_questions[next_key]
                return {"message": next_q["question"], "type": next_q.get("type", "text"), "options": next_q.get("options", [])}

            user_states[session_id]["current_question"] = "complete"
            return finalize_structured_conversation(session_id)

        return {"message": first_q["question"], "type": first_q.get("type", "text"), "options": first_q.get("options", [])}

    current_key = user_states[session_id]["current_question"]
    current_data = conversation_questions[current_key]

    user_states[session_id]["answers"][current_key] = user_message

    next_key = None
    if "next_question_map" in current_data:
        chosen = user_message.lower()
        if chosen in current_data["next_question_map"]:
            next_key = current_data["next_question_map"][chosen]
        else:
            return {
                "message": f"Invalid choice. Please choose from {', '.join(current_data['options'])}",
                "type": "choice",
                "options": current_data["options"],
            }
    elif "next_question" in current_data:
        next_key = current_data["next_question"]

    if next_key:
        user_states[session_id]["current_question"] = next_key
        next_q = conversation_questions[next_key]
        return {"message": next_q["question"], "type": next_q.get("type", "text"), "options": next_q.get("options", [])}

    user_states[session_id]["current_question"] = "complete"
    return finalize_structured_conversation(session_id)


def _run_generation_job(job_id: str, answers: dict, user_name: str) -> None:
    """Background worker — runs the slow multi-agent pipeline and writes
    the result back into the shared `jobs` dict. Never raises (errors are
    caught and recorded so the polling client can show them)."""
    logger.info("Job %s: starting generation (user: %s)", job_id, user_name)
    try:
        game_html = generate_game(answers)

        game_filename = f"game_{uuid.uuid4().hex[:6]}.html"
        game_path     = os.path.join(GAMES_FOLDER, game_filename)
        with open(game_path, "w", encoding="utf-8") as f:
            f.write(game_html)
        game_url = f"/static/games/{game_filename}"

        with jobs_lock:
            jobs[job_id].update({
                "status":   "done",
                "game_url": game_url,
                "finished_at": time.time(),
            })
        logger.info("Job %s: done — %s", job_id, game_url)

    except Exception as err:
        logger.error("Job %s: generation failed: %s", job_id, err)
        with jobs_lock:
            jobs[job_id].update({
                "status": "error",
                "error":  str(err),
                "finished_at": time.time(),
            })


def finalize_structured_conversation(session_id: str) -> dict:
    """Kick off game generation as a background job and return immediately.

    The client gets a job_id and polls /api/job/<job_id> for status. We do
    this because the synchronous generate_game() takes 2–4 minutes on free
    Render — past the HTTP proxy timeout. See the `jobs` dict comment above.
    """
    answers   = user_states[session_id]["answers"]
    user_name = (answers.get("name") or "").strip()
    job_id    = uuid.uuid4().hex[:8]

    with jobs_lock:
        jobs[job_id] = {
            "status":      "working",
            "game_url":    None,
            "error":       None,
            "user_name":   user_name,
            "started_at":  time.time(),
            "finished_at": None,
        }

    # daemon=True so the thread doesn't block shutdown if the server is killed
    threading.Thread(
        target=_run_generation_job,
        args=(job_id, answers, user_name),
        daemon=True,
        name=f"gen-{job_id}",
    ).start()

    user_states[session_id]["current_question"] = "complete"
    logger.info("Job %s: queued for session %s (user: %s)", job_id, session_id, user_name)

    # Address the user directly by name (vocative) — reads like a
    # friendly chat ("Yael, generating your game…") rather than the
    # earlier awkward "Generating your game, Yael…" pattern. Skip the
    # name prefix when we don't have one yet.
    if user_name:
        msg = f"{user_name}, generating your game… this can take a few minutes."
    else:
        msg = "Generating your game… this can take a few minutes."
    return {
        "message": msg,
        "type":    "job_started",
        "job_id":  job_id,
        "options": [],
    }


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/api/chat", methods=["POST"])
def chat():
    try:
        data = request.get_json()
        if not data or "message" not in data:
            return jsonify({"error": "Missing 'message' field", "success": False}), 400

        user_message = (data.get("message") or "").strip()
        if not user_message:
            return jsonify({"error": "Message cannot be empty", "success": False}), 400

        session_id = request.headers.get("X-Session-ID", "default_session")
        logger.info("Received message from session %s: %s", session_id, user_message)

        try:
            message = handle_user_text(user_message, session_id)
        except Exception as err:
            logger.error("handle_user_text failed: %s", err)
            message = "Sorry, an internal error occurred while processing your message."

        if isinstance(message, dict):
            return jsonify(
                {
                    "response": message.get("message"),
                    "type": message.get("type"),
                    "options": message.get("options", []),
                    "game_url": message.get("game_url"),
                    # Present when type == "job_started" — client polls /api/job/<id>
                    "job_id":   message.get("job_id"),
                    "success": True,
                }
            ), 200

        return jsonify({"response": message, "success": True}), 200

    except Exception as e:
        logger.error("Error processing request: %s", e)
        return jsonify({"error": f"Server error: {str(e)}", "success": False}), 500


@app.route("/api/job/<job_id>", methods=["GET"])
def job_status(job_id: str):
    """Polled by the client every few seconds while a generation runs.

    Response shape:
      working: {success, status: "working"}
      done:    {success, status: "done",  game_url, message}
      error:   {success, status: "error", message}
      404 if job_id is unknown (e.g. server restarted and forgot the job).
    """
    with jobs_lock:
        job = jobs.get(job_id)
        # Snapshot under the lock so we don't read while _run_generation_job
        # is mid-update.
        snap = dict(job) if job else None

    if snap is None:
        return jsonify({"success": False, "error": "Job not found"}), 404

    resp = {"success": True, "status": snap["status"]}
    user_name = snap.get("user_name") or ""

    if snap["status"] == "done":
        resp["game_url"] = snap["game_url"]
        # Vocative form — "Yael, your game is ready!" sounds like a
        # friendly direct address. Falls back to no-name form if we
        # don't have it.
        if user_name:
            resp["message"] = f"{user_name}, your game is ready! 🎮"
        else:
            resp["message"] = "Your game is ready! 🎮"
    elif snap["status"] == "error":
        resp["error"]    = snap.get("error") or "unknown error"
        # "Sorry Yael, …" — informal apology that addresses the user.
        if user_name:
            resp["message"] = f"Sorry {user_name}, the game generation failed. Please try again."
        else:
            resp["message"] = "Sorry, the game generation failed. Please try again."
    # status == "working" → just {success, status}, client keeps polling
    return jsonify(resp), 200


@app.route("/api/health", methods=["GET"])
def health():
    # Report which AI services are available so the operator can spot
    # misconfigured deployments at a glance.
    services = {
        "anthropic": bool(os.getenv("ANTHROPIC_API_KEY")),
        "huggingface": bool(os.getenv("HF_TOKEN")),
    }
    return jsonify({
        "status":  "ok",
        "message": "Game Maker Chat Server is running",
        "services": services,
    }), 200


@app.route("/api/upload", methods=["POST"])
def upload_file():
    try:
        if "file" not in request.files:
            return jsonify({"error": "No file part in the request"}), 400

        file = request.files["file"]
        if file.filename == "":
            return jsonify({"error": "No selected file"}), 400

        filename = secure_filename(file.filename)
        base, ext = os.path.splitext(filename)
        unique = f"{base}_{int(time.time())}_{uuid.uuid4().hex[:6]}{ext}"
        save_path = os.path.join(UPLOAD_FOLDER, unique)
        file.save(save_path)

        url = f"/static/uploads/{unique}"
        return jsonify({"url": url}), 200

    except Exception as e:
        logger.error("Upload failed: %s", e)
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print("Starting Game Maker Chat Server V3 (Game Only)...")
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
