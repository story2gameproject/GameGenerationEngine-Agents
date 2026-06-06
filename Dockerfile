# Dockerfile for Hugging Face Spaces deployment.
#
# Why HF Spaces instead of Render: free CPU Basic tier gives 2 vCPUs and
# 16 GB RAM, vs Render free tier's 0.1 vCPU / 512 MB. The rembg sprite
# pipeline that was OOM'ing on Render has no trouble here.
#
# HF Spaces convention:
#   - Image runs as user UID 1000 ('user'). Running as root throws warnings
#     and certain file operations fail.
#   - App must listen on the port declared as `app_port:` in README.md's
#     YAML frontmatter (we use 7860, the HF default).
#   - Environment variables (ANTHROPIC_API_KEY, HF_TOKEN) are configured
#     via the Space's "Variables and secrets" UI, not baked into the image.

# Pin to Debian Bookworm explicitly. The unsuffixed `python:3.11-slim` tag
# recently switched to Debian Trixie, where the package `libgl1-mesa-glx`
# was removed (replaced by plain `libgl1`). Pinning to bookworm keeps the
# build deterministic across rebuilds.
FROM python:3.11-slim-bookworm

# ── System deps ──────────────────────────────────────────────────────────
# `libgl1` provides the OpenGL runtime that onnxruntime / rembg pull in
# transitively. `libglib2.0-0` is needed by some Pillow + onnxruntime
# combinations. Both packages exist with the same names on bookworm and
# trixie, so this works regardless of future base-image shifts.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# ── HF user (UID 1000) ───────────────────────────────────────────────────
# Create the user up front so all subsequent COPYs land with the right
# ownership and pip's --user install dir is on $PATH.
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH
WORKDIR /home/user/app

# ── Python deps ──────────────────────────────────────────────────────────
# Install requirements first (separate layer) so code changes don't
# invalidate the pip cache.
COPY --chown=user:user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Pre-download the rembg model. We use `isnet-general-use` (~170 MB)
# rather than the lightweight `u2netp` (~5 MB) because isnet was trained
# on general objects (vehicles, items, accessories) where u2netp is biased
# toward humans/portraits. On Render's 512 MB tier we had to use u2netp;
# on HF Spaces' 16 GB tier we can afford the better model, which makes
# obstacle/target sprites segment as cleanly as the hero does.
ENV REMBG_MODEL=isnet-general-use
RUN python -c "from rembg import new_session; new_session('isnet-general-use'); print('isnet-general-use pre-cached')"

# ── App code ─────────────────────────────────────────────────────────────
COPY --chown=user:user . .

# Some directories are gitignored (cache/uploads/games are runtime-only)
# but the app expects them to exist. Create them with the right owner.
RUN mkdir -p client-side/static/uploads \
             client-side/static/games \
             client-side/static/cache

# HF Spaces routes external traffic to port 7860 by default. The
# web_server_v2.py code already reads PORT from the environment.
ENV PORT=7860
EXPOSE 7860

CMD ["python", "server-side/web_server_v2.py"]
