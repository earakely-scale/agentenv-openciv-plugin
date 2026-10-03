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


def _capture(saves: Path, out: Path, count: int, deadline: int, settle_frames: int,
             seats: list[str] | None = None) -> dict[str | None, list[Path]]:
    """Run capture.sh over ``saves`` (a directory, or one save) into ``out``.

    Without ``seats``, returns ``{None: frames}``, the controller's view of each save (``turn-NNNN.png``). With
    ``seats`` (civ names), the client loads each save once and draws it for every seat: ``{seat: frames}``, from
    ``turn-NNNN.<seat>.png``. Each list holds ``count`` frames in turn order.
    """
    for seat in seats or ():
        if not seat or any(c in seat for c in ",/*?[") or seat != seat.strip():
            raise ValueError(f"not a civ name: {seat!r}")
    args = [f"--capture-settle-frames={settle_frames}", *([f"--capture-players={','.join(seats)}"] if seats else [])]
    run = subprocess.run(
        [str(home() / "capture.sh"), str(home() / "OpenCiv3/C7"), str(saves), str(out), *args],
        capture_output=True, text=True, timeout=deadline + 60, env={**os.environ, "DEADLINE": str(deadline)})
    frames = {seat: sorted(out.glob(f"turn-*.{seat}.png")) for seat in seats} if seats else \
        {None: sorted(out.glob("turn-*.png"))}
    short = {seat: len(f) for seat, f in frames.items() if len(f) != count}
    if run.returncode or short:
        log = out / "capture.log"
        tail = (log.read_text(errors="replace") if log.exists() else run.stdout + run.stderr)[-1500:]
        got = ", ".join(f"{n} for {seat}" if seat else str(n) for seat, n in short.items()) or str(count)
        raise RuntimeError(f"the client rendered {got} of {count} turns (exit {run.returncode}): {tail}")
    return frames


def _turn_saves(saves: Path) -> int:
    count = len(list(saves.glob("turn-*.json.gz")))
    if not count:
        raise RuntimeError(f"no per-turn saves in {saves}")
    return count


def _join(frames: list[Path], fps: int) -> bytes:
    mp4 = Mp4(fps)
    for png in frames:
        mp4.add_png(png)
    video = mp4.finish()
    if video is None:
        raise RuntimeError("ffmpeg could not encode the client's frames")
    return video


def render(saves: Path, *, fps: int, settle_frames: int = 5) -> bytes:
    """Load every turn-NNNN.json.gz in ``saves`` into the client, capture the map, and join the frames."""
    count = _turn_saves(saves)
    with tempfile.TemporaryDirectory(prefix="openciv3-client-") as tmp:
        [frames] = _capture(saves, Path(tmp), count, 60 + 5 * count, settle_frames).values()
        return _join(frames, fps)


def render_seats(saves: Path, seats: list[str], *, fps: int, settle_frames: int = 5) -> dict[str, bytes]:
    """One mp4 per seat (a civ name), each turn drawn as that seat sees it; one client run loads each save once."""
    if not seats:
        raise ValueError("no seats to render")
    seats = list(dict.fromkeys(seats))
    count = _turn_saves(saves)
    with tempfile.TemporaryDirectory(prefix="openciv3-client-") as tmp:
        frames = _capture(saves, Path(tmp), count, 60 + (5 + 2 * len(seats)) * count, settle_frames, seats)
        return {seat: _join(frames[seat], fps) for seat in seats}


def frame(save: Path, seat: str | None = None, *, settle_frames: int = 5) -> bytes:
    """Load one turn-NNNN.json.gz into the client and capture the map as PNG, for the live view.

    ``seat`` (a civ name) draws the map as that seat sees it; None draws the controller's (the first civ's) view.
    """
    with tempfile.TemporaryDirectory(prefix="openciv3-client-") as tmp:
        [[png]] = _capture(save, Path(tmp), 1, FRAME_DEADLINE, settle_frames, [seat] if seat else None).values()
        return png.read_bytes()
