"""The client renderer's plumbing (client.py) against a fake capture.sh: arguments, frame names, per-seat videos."""

import json
import sys
from pathlib import Path

import pytest

from agentenv_openciv3 import client

# Writes "<stem>.png" per save, or "<stem>.<civ>.png" per save and civ with --capture-players, holding
# "<stem>:<civ>" (no civ: "<stem>:"). Appends its argv to calls.jsonl. SKIP (a civ) is left out, to fail a run.
FAKE_CAPTURE = f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
saves, out, opts = Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4:]
with open(Path(__file__).with_name("calls.jsonl"), "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
players = [a.split("=", 1)[1].split(",") for a in opts if a.startswith("--capture-players=")]
for save in [saves] if saves.is_file() else sorted(saves.glob("turn-*.json.gz")):
    stem = save.name.split(".")[0]
    for civ in players[0] if players else [None]:
        if civ is None or civ != os.environ.get("SKIP"):
            (out / (f"{{stem}}.{{civ}}.png" if civ else f"{{stem}}.png")).write_text(f"{{stem}}:{{civ or ''}}")
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "client"
    (root / "OpenCiv3/C7").mkdir(parents=True)
    (root / "capture.sh").write_text(FAKE_CAPTURE)
    (root / "capture.sh").chmod(0o755)
    monkeypatch.setenv("OPENCIV_CLIENT", str(root))
    monkeypatch.delenv("SKIP", raising=False)
    return root


@pytest.fixture
def saves(tmp_path):
    d = tmp_path / "saves"
    d.mkdir()
    for turn in range(3):
        (d / f"turn-{turn:04d}.json.gz").write_bytes(b"")
    return d


class FakeMp4:
    """recording.Mp4 without ffmpeg: the video is the frames' contents, joined."""

    def __init__(self, fps):
        self.fps, self.frames = fps, []

    def add_png(self, path):
        self.frames.append(Path(path).read_text())

    def finish(self):
        return json.dumps({"fps": self.fps, "frames": self.frames}).encode()


@pytest.fixture(autouse=True)
def fake_mp4(monkeypatch):
    monkeypatch.setattr(client, "Mp4", FakeMp4)


def calls(home):
    return [json.loads(line) for line in (home / "calls.jsonl").read_text().splitlines()]


def test_render_seats_makes_one_video_per_seat_from_one_run(home, saves):
    videos = client.render_seats(saves, ["Rome", "Greece", "Egypt"], fps=4)
    assert list(videos) == ["Rome", "Greece", "Egypt"]
    for civ, video in videos.items():
        assert json.loads(video) == {"fps": 4, "frames": [f"turn-{t:04d}:{civ}" for t in range(3)]}
    [argv] = calls(home)
    assert argv[1] == str(saves) and "--capture-players=Rome,Greece,Egypt" in argv
    assert "--capture-settle-frames=5" in argv


def test_render_seats_drops_repeated_seats(home, saves):
    assert list(client.render_seats(saves, ["Rome", "Greece", "Rome"], fps=2)) == ["Rome", "Greece"]
    assert "--capture-players=Rome,Greece" in calls(home)[0]


def test_render_keeps_the_controllers_view(home, saves):
    assert json.loads(client.render(saves, fps=3))["frames"] == [f"turn-{t:04d}:" for t in range(3)]
    assert not any(a.startswith("--capture-players") for a in calls(home)[0])


def test_frame_is_the_controllers_view_without_a_seat(home, saves):
    save = saves / "turn-0002.json.gz"
    assert client.frame(save) == b"turn-0002:"
    assert not any(a.startswith("--capture-players") for a in calls(home)[0])


def test_frame_for_a_seat(home, saves):
    assert client.frame(saves / "turn-0001.json.gz", "Greece") == b"turn-0001:Greece"
    assert "--capture-players=Greece" in calls(home)[0]


def test_a_seat_without_frames_fails(home, saves, monkeypatch):
    monkeypatch.setenv("SKIP", "Egypt")
    with pytest.raises(RuntimeError, match="rendered 0 for Egypt of 3 turns"):
        client.render_seats(saves, ["Rome", "Egypt"], fps=4)


@pytest.mark.parametrize("seat", ["", "Rome,Greece", " Rome", "../x", "Ro*"])
def test_seats_must_be_civ_names(home, saves, seat):
    with pytest.raises(ValueError, match="not a civ name"):
        client.render_seats(saves, [seat], fps=4)


def test_no_seats_or_no_saves(home, saves, tmp_path):
    with pytest.raises(ValueError, match="no seats"):
        client.render_seats(saves, [], fps=4)
    with pytest.raises(RuntimeError, match="no per-turn saves"):
        client.render_seats(tmp_path, ["Rome"], fps=4)
