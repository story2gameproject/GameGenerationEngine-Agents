"""
Background-removal worker — spawned as a subprocess by asset_agent.

The whole point of running rembg in a subprocess (instead of importing it
into the main Flask process) is memory: rembg + onnxruntime occupies
~250 MB while it's loaded. By using a short-lived subprocess, we:

  - Keep the main Flask process light (~250 MB always-loaded)
  - Pay ~250 MB only DURING a sprite call
  - Release the 250 MB back to the OS as soon as the subprocess exits

This is what makes the app fit Render's 512 MB free tier without losing
rembg's segmentation quality. (The asset_agent uses a threading lock to
ensure only ONE worker runs at a time, otherwise three parallel sprite
generations would each spawn a worker and combined exceed 512 MB.)

Usage:
    python rembg_worker.py <input.png> <output.png>

Exit codes:
    0 = success, output file written
    1 = bad arguments
    2 = rembg processing error
"""

import sys


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: rembg_worker.py <input.png> <output.png>", file=sys.stderr)
        return 1

    in_path, out_path = sys.argv[1], sys.argv[2]

    try:
        from rembg import remove, new_session
        from PIL import Image

        # u2netp = the lightweight U2Net variant (~5 MB model file).
        # Keeps total subprocess memory under control. Override via
        # REMBG_MODEL env var on a beefier host if you want full u2net.
        import os
        model_name = os.getenv("REMBG_MODEL", "u2netp")
        session = new_session(model_name)

        img = Image.open(in_path)
        out = remove(img, session=session)
        out.save(out_path)
        return 0
    except Exception as exc:
        print(f"rembg worker error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
