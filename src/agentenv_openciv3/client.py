"""The real OpenCiv3 client as a renderer: one frame per kept turn save, joined into an mp4 (docs/recording.md).

Only an image built with the Dockerfile's ``client`` target carries the client; elsewhere ``missing()`` says why.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .recording import Mp4

FORMAT = "client_mp4"
FRAME_DEADLINE = 30


def home() -> Path:
    return Path(os.environ.get("OPENCIV_CLIENT", "/opt/openciv3-client"))


def missing() -> str | None:
    """Why the client cannot render here, or None when it can."""
    root = home()
    if not (root / "capture.sh").is_file() or not (root / "OpenCiv3/C7/project.godot").is_file():
        return f"the OpenCiv3 client is not installed at {root} (build the image with --target client)"
    godot = os.environ.get("GODOT", "")
    if not os.access(godot, os.X_OK):
        return "GODOT does not name the Godot 4.4.1 .NET binary"
    if not shutil.which("ffmpeg"):
        return "ffmpeg is not on PATH"
    return None


def _capture(saves: Path, out: Path, count: int, deadline: int, settle_frames: int) -> list[Path]:
    """Run capture.sh over ``saves`` (a directory, or one save) into ``out``; returns the ``count`` frames."""
    run = subprocess.run(
        [str(home() / "capture.sh"), str(home() / "OpenCiv3/C7"), str(saves), str(out),
         f"--capture-settle-frames={settle_frames}"],
        capture_output=True, text=True, timeout=deadline + 60, env={**os.environ, "DEADLINE": str(deadline)})
    frames = sorted(out.glob("turn-*.png"))
    if run.returncode or len(frames) != count:
        log = out / "capture.log"
        tail = (log.read_text(errors="replace") if log.exists() else run.stdout + run.stderr)[-1500:]
        raise RuntimeError(f"the client rendered {len(frames)} of {count} turns (exit {run.returncode}): {tail}")
    return frames


def render(saves: Path, *, fps: int, settle_frames: int = 5) -> bytes:
    """Load every turn-NNNN.json.gz in ``saves`` into the client, capture the map, and join the frames."""
    count = len(list(saves.glob("turn-*.json.gz")))
    if not count:
        raise RuntimeError(f"no per-turn saves in {saves}")
    with tempfile.TemporaryDirectory(prefix="openciv3-client-") as tmp:
        mp4 = Mp4(fps)
        for png in _capture(saves, Path(tmp), count, 60 + 5 * count, settle_frames):
            mp4.add_png(png)
        video = mp4.finish()
    if video is None:
        raise RuntimeError("ffmpeg could not encode the client's frames")
    return video


def frame(save: Path, *, settle_frames: int = 5) -> bytes:
    """Load one turn-NNNN.json.gz into the client and capture the map as PNG, for the live view."""
    with tempfile.TemporaryDirectory(prefix="openciv3-client-") as tmp:
        [png] = _capture(save, Path(tmp), 1, FRAME_DEADLINE, settle_frames)
        return png.read_bytes()
