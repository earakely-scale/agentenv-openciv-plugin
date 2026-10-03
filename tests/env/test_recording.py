import copy
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from agentenv_openciv3 import recording
from agentenv_openciv3.matchdata import MatchData
from agentenv_openciv3.recording import H, HEADER_H, PANEL_X, W, Renderer, spread

FAKE = Path(__file__).with_name("fake_bridge.py")


def test_spread_leaves_distant_labels_alone():
    assert spread([10, 50, 90], 12, 0, 100) == [10, 50, 90]


def test_spread_separates_close_labels_in_order():
    ys = spread([40, 45, 41], 12, 0, 100)
    assert ys[0] == 40 and ys[2] == 52 and ys[1] == 64


def test_spread_keeps_labels_above_the_bottom():
    assert spread([98, 99], 12, 0, 100) == [88, 100]


@pytest.fixture(scope="module")
def game(tmp_path_factory) -> list[dict]:
    """Six turns of the fake bridge's game (T1 to T6), as recorded snapshots."""
    record = tmp_path_factory.mktemp("record")
    lines = [{"id": 1, "cmd": "new_game", "args": {"seed": 3, "turn_limit": 8}},
             {"id": 2, "cmd": "autoplay", "args": {"turns": 5, "policy": "settler_bot"}}]
    subprocess.run([sys.executable, str(FAKE), "--record", str(record)], check=True, capture_output=True, text=True,
                   timeout=60, input="".join(json.dumps(x) + "\n" for x in lines))
    snaps = recording.load_snapshots(record)
    assert [s["turn"] for s in snaps] == [1, 2, 3, 4, 5, 6]
    return snaps


def at_war(snaps: list[dict]) -> list[dict]:
    """The game in schema 2 with two seats, Greece at war with Rome from T3, and Athens taken by Rome on T5."""
    snaps = copy.deepcopy(snaps)
    for s in snaps:
        s["schema"] = 2
        s["seats"] = [{"index": 0, "civ": "Rome", "label": "opus"}, {"index": 1, "civ": "Greece", "label": "sol"}]
        for p in s["players"]:
            p["label"] = {"Rome": "opus", "Greece": "sol"}.get(p["civ"])
            p["at_war"] = [1 - p["index"]] if s["turn"] >= 3 and p["index"] < 2 else []
            p["government"] = "Despotism"
        for c in s["cities"]:
            if c["name"] == "Athens" and s["turn"] >= 5:
                c["owner"] = 0
    return snaps


def doc_of(snaps: list[dict], **kw) -> dict:
    return recording.document(snaps, **kw)


SEAT_ACTIONS = {t: {"Rome": [{"text": f"u1 goto → ({t},{t})", "ok": True}],
                    "Greece": [{"text": "c1 builds Warrior ✗ invalid", "ok": False}]} for t in range(6)}
CALLS = {t: {"Rome": {"ok": 5, "failed": 1}, "Greece": {"ok": 2, "failed": 0}} for t in range(6)}


def test_a_frame_per_turn_at_1080p(game):
    r = Renderer(doc_of(game))
    frames = list(r.frames())
    assert [t["turn"] for t, _ in frames] == [1, 2, 3, 4, 5, 6]
    assert all(img.size == (W, H) for _, img in frames)
    assert r.map_x >= 0 and r.map_x + r.map_w <= PANEL_X and r.chart_box[1] > r.map_y + r.map_h


def test_a_frame_shows_nothing_after_its_turn(game):
    """A frame drawn from the whole game is the frame drawn from the game up to its turn."""
    snaps = at_war(game)
    full = Renderer(doc_of(snaps, seat_actions=SEAT_ACTIONS, calls=CALLS))
    for k in (0, 2, 4):
        cut = Renderer(doc_of(snaps[: k + 1], seat_actions=SEAT_ACTIONS, calls=CALLS))
        assert full.frame(k).tobytes() == cut.frame(k).tobytes(), f"turn {snaps[k]['turn']}"
    assert full.frame(5).tobytes() != full.frame(4).tobytes()


def test_seat_actions_and_calls_are_keyed_by_player(game):
    snaps = at_war(game)
    doc = doc_of(snaps, seat_actions=SEAT_ACTIONS, calls=CALLS)
    t3 = next(t for t in doc["turns"] if t["turn"] == 3)
    assert t3["actions"] == {"0": [{"text": "u1 goto → (2,2)", "ok": True}],
                             "1": [{"text": "c1 builds Warrior ✗ invalid", "ok": False}]}
    assert t3["calls"] == {"0": {"ok": 5, "failed": 1}, "1": {"ok": 2, "failed": 0}}
    r = Renderer(doc)
    assert r.has_agents and r.has_war and r.latest[5][0] == (5, {"text": "u1 goto → (5,5)", "ok": True})
    bare = Renderer(doc_of(snaps))
    assert not bare.has_agents and r.frame(3).tobytes() != bare.frame(3).tobytes()


def test_the_old_merged_timeline_still_reaches_the_seats(game):
    snaps = at_war(game)
    timeline = {2: [{"text": "opus: u1 settle", "ok": True}, {"text": "sol: c1 builds Warrior", "ok": False}]}
    t3 = next(t for t in doc_of(snaps, actions=timeline)["turns"] if t["turn"] == 3)
    assert t3["actions"] == {"0": [{"text": "u1 settle", "ok": True}], "1": [{"text": "c1 builds Warrior", "ok": False}]}
    single = {2: [{"text": "u1 settle", "ok": True}]}
    t3 = next(t for t in doc_of(game, actions=single)["turns"] if t["turn"] == 3)
    assert t3["actions"] == {"0": [{"text": "u1 settle", "ok": True}]}
    # a single-seat action log has no seat: key -1
    t3 = next(t for t in doc_of(game, seat_actions={2: {-1: single[2]}}, calls={2: {-1: {"ok": 3, "failed": 0}}})
              ["turns"] if t["turn"] == 3)
    assert t3["actions"] == {"0": single[2]} and t3["calls"] == {"0": {"ok": 3, "failed": 0}}


def test_wars_and_captures_are_key_moments(game):
    r = Renderer(doc_of(at_war(game)))
    kinds = [e["kind"] for _, e in r.moments(len(r.turns) - 1, 11)]
    assert "war_declared" in kinds and "city_captured" in kinds
    assert kinds.index("city_captured") < kinds.index("war_declared")
    assert {"war_declared", "city_captured"} <= recording.STRONG
    assert not {"war_declared", "city_captured"} & {e["kind"] for _, e in r.moments(1, 11)}


def test_the_agent_view_hides_unexplored_tiles(game):
    doc = doc_of(game)
    spectator, agent = Renderer(doc), Renderer(doc, view="agent")
    last = len(spectator.turns) - 1
    a, s = agent.frame(last), spectator.frame(last)
    known = [0] * len(doc["static"]["tiles"])
    for t in doc["turns"]:
        for k, mask in t["known"]:
            known[k] = mask
    hidden = [k for k, m in enumerate(known) if not m and agent.centre[k] is not None]
    seen = [k for k, m in enumerate(known) if m and agent.centre[k] is not None]
    assert hidden and seen

    def pixel(img, k):
        cx, cy = agent.centre[k]
        return img.getpixel((round(agent.map_x + cx / 2), round(agent.map_y + cy / 2)))

    assert all(pixel(a, k) == recording.FOG for k in hidden)
    assert sum(pixel(a, k) == pixel(s, k) for k in seen) > 0.8 * len(seen)


def test_render_formats(game):
    formats = ["png", "gif"] + (["mp4"] if shutil.which("ffmpeg") else [])
    files, notes = recording.render(game, formats=formats, seat_actions=SEAT_ACTIONS, calls=CALLS)
    assert notes == [] and [f.name.split(".")[-1] for f in files] == formats
    png = Image.open(io.BytesIO(files[0].data))
    assert png.size == (W, H)
    gif = Image.open(io.BytesIO(files[1].data))
    assert gif.n_frames == len(game) and gif.size == recording.GIF_SIZE
    if "mp4" in formats:
        assert files[2].data[4:8] == b"ftyp"


def test_render_falls_back_to_a_gif_without_ffmpeg(game, monkeypatch):
    monkeypatch.setenv("PATH", "")
    files, notes = recording.render(game[:2], formats=["mp4"])
    assert [f.name for f in files] == ["openciv3.gif"] and "replaced by an animated gif" in notes[0]


def test_html_is_the_viewer_with_the_match_embedded(game, monkeypatch):
    seen = {}

    def page(doc, videos):
        seen.update(doc=doc, videos=videos)
        return "<html>viewer</html>"

    monkeypatch.setattr("agentenv_openciv3.viewer.page", page)
    videos = {"Rome": {"file": "openciv3.client-Rome.mp4", "fps": 4, "turns": [1, 2]}}
    files, _ = recording.render(game, formats=["html"], seat_actions=SEAT_ACTIONS, calls=CALLS, client_videos=videos)
    assert [(f.name, f.data) for f in files] == [("openciv3.html", b"<html>viewer</html>")]
    expected = MatchData.from_snapshots(game).document(actions=recording.by_player(SEAT_ACTIONS, seen["doc"]["players"]),
                                                       calls=recording.by_player(CALLS, seen["doc"]["players"]))
    assert seen["doc"] == expected and seen["videos"] is videos


def test_the_live_frame_is_the_map_and_chart(game):
    png = Image.open(io.BytesIO(recording.map_png(iter(game), view="agent")))
    assert png.size == (PANEL_X, H - HEADER_H)
