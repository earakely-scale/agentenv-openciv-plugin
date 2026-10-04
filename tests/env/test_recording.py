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
from agentenv_openciv3.recording import HEADER_H, PANEL_X, H, Renderer, W, spread

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
    assert t3["actions"] == {"0": [{"text": "u1 settle", "ok": True}],
                             "1": [{"text": "c1 builds Warrior", "ok": False}]}
    single = {2: [{"text": "u1 settle", "ok": True}]}
    t3 = next(t for t in doc_of(game, actions=single)["turns"] if t["turn"] == 3)
    assert t3["actions"] == {"0": [{"text": "u1 settle", "ok": True}]}
    # a single-seat action log has no seat: key -1
    t3 = next(t for t in doc_of(game, seat_actions={2: {-1: single[2]}}, calls={2: {-1: {"ok": 3, "failed": 0}}})
              ["turns"] if t["turn"] == 3)
    assert t3["actions"] == {"0": single[2]} and t3["calls"] == {"0": {"ok": 3, "failed": 0}}


def test_notes_plans_and_messages_of_a_turn_go_on_the_next_entry(game):
    snaps = at_war(game)
    doc = doc_of(snaps, seat_notes={2: {"Rome": "Settling the river."}}, plans={2: {"sol": "Hold the hills."}},
                 messages={2: [{"from": 1, "to": [0], "text": "Leave."}, {"from": 0, "to": "all", "text": "No."}]})
    t2, t3 = (next(t for t in doc["turns"] if t["turn"] == n) for n in (2, 3))
    assert not {"notes", "plans", "messages"} & set(t2)
    assert t3["notes"] == {"0": "Settling the river."} and t3["plans"] == {"1": "Hold the hills."}
    assert t3["messages"] == [{"from": 1, "to": [0], "text": "Leave."}, {"from": 0, "to": "all", "text": "No."}]


def test_wars_and_captures_are_key_moments(game):
    r = Renderer(doc_of(at_war(game)))
    kinds = [e["kind"] for _, e in r.moments(len(r.turns) - 1, 11)]
    assert "war_declared" in kinds and "city_captured" in kinds
    assert kinds.index("city_captured") < kinds.index("war_declared")
    assert {"war_declared", "city_captured"} <= recording.STRONG
    assert not {"war_declared", "city_captured"} & {e["kind"] for _, e in r.moments(1, 11)}


def later_game(snaps: list[dict]) -> list[dict]:
    """The game with what the later game brings (the snapshot's newer fields): the year, culture, eras and shares;
    Rome meets Greece on T3, enters the Middle Ages and builds the Pyramids in Veii on T4, trades with Greece on T5,
    upgrades its Warrior on T4, and lands its Worker from a ship in Greece's land on T6."""
    snaps = at_war(snaps)
    greek = next(r for r in snaps[-1]["tiles"] if r[2] not in ("ocean", "sea", "coast") and r[4] == 1)
    for s in snaps:
        t = s["turn"]
        s["date"] = f"{4000 - 50 * t} BC"
        for p in s["players"]:
            if p["index"] < 2:
                p["contacts"] = [1 - p["index"]] if t >= 3 else []
            p.update(culture=10 * t, era=int(p["index"] == 0 and t >= 4), land=0.1, pop=0.2)
        for c in s["cities"]:
            c["wonders"] = ["The Pyramids"] if c["name"] == "Veii" and t >= 4 else []
        for u in s["units"]:
            if u["id"] == 4 and t >= 4:
                u["type"] = "Spearman"
            if u["id"] == 2:
                u["aboard"] = "ship-9" if t == 5 else None
                if t == 6:
                    u["x"], u["y"] = greek[0], greek[1]
        trade = {"seq": 40, "turn": 5, "a": 0, "b": 1, "a_gave": "Bronze Working", "b_gave": "60 gold"}
        s["trades"] = [trade] if t == 5 else []
    return snaps


def test_the_later_games_stories_are_events_for_every_civ(game):
    m = MatchData.from_snapshots(later_game(game), names={"opus": "Opus 5.5"})
    events = {(t["turn"], e["kind"]): e for t in m.turns for e in t["events"]}
    assert events[(3, "contact")]["text"] == "First contact: Opus 5.5 meets sol"
    assert events[(4, "era_entered")]["text"] == "Opus 5.5 enters the Middle Ages"
    assert events[(4, "wonder_built")] | {"source": None} == {
        "kind": "wonder_built", "owner": 0, "text": "Opus 5.5 completed The Pyramids in Veii", "source": None,
        "wonder": "The Pyramids", "city": "Veii", "x": 16, "y": 12}
    assert events[(4, "units_upgraded")]["text"] == "Opus 5.5 upgraded 1 Warrior to Spearman"
    assert events[(5, "trade")]["text"] == "Opus 5.5 traded Bronze Working to sol for 60 gold"
    assert events[(5, "trade")]["from"] == 1 and events[(5, "trade")]["got"] == "60 gold"
    landing = events[(6, "landing")]
    assert landing["text"] == "Opus 5.5 landed 1 unit from the sea in sol's land" and landing["from"] == 1
    # each once: the wonder stays built, the era entered and the contact made
    kinds = [(t["turn"], e["kind"]) for t in m.turns for e in t["events"]]
    for kind in ("wonder_built", "era_entered", "contact", "trade", "landing", "units_upgraded"):
        assert sum(k == kind for _, k in kinds) == 1, kind
    last = m.turns[-1]
    assert last["date"] == "3700 BC"
    rome = last["stats"]["0"]
    military = sum(1 for u in game[-1]["units"] if u["owner"] == 0 and u["type"] not in ("Worker", "Settler"))
    assert {k: rome[k] for k in ("culture", "era", "land", "pop", "military", "wonders")} == {
        "culture": 60, "era": 1, "land": 0.1, "pop": 0.2, "military": military, "wonders": 1}
    assert m.players[0]["name"] == "Opus 5.5" and "name" not in m.players[1]


def test_older_snapshots_keep_the_bridges_contact_events(game):
    snaps = copy.deepcopy(game)
    for s in snaps:
        for p in s["players"]:
            p.pop("contacts", None)
        s["events"] = [{"kind": "contact", "text": "met Greece"}] if s["turn"] == 2 else []
    m = MatchData.from_snapshots(snaps)
    assert [(t["turn"], e["kind"]) for t in m.turns for e in t["events"] if e["kind"] == "contact"] == [(2, "contact")]


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

    assert sum(pixel(a, k) == recording.FOG for k in hidden) > 0.9 * len(hidden)    # labels and the edge aside
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
    players = seen["doc"]["players"]
    expected = MatchData.from_snapshots(game).document(actions=recording.by_player(SEAT_ACTIONS, players),
                                                       calls=recording.by_player(CALLS, players))
    assert seen["doc"] == expected and seen["videos"] is videos


def test_the_viewer_marks_the_seats_people_play(game, monkeypatch):
    seen = {}
    monkeypatch.setattr("agentenv_openciv3.viewer.page", lambda doc, videos: seen.update(doc=doc) or "<html></html>")
    recording.render(game, formats=["html"], humans=["rome"])
    marked = {p["civ"]: p.get("human") for p in seen["doc"]["players"]}
    assert marked["Rome"] is True and not any(v for civ, v in marked.items() if civ != "Rome")
    assert "human" not in MatchData.from_snapshots(game).document()["players"][0]


def test_the_live_frame_is_the_map_and_chart(game):
    png = Image.open(io.BytesIO(recording.map_png(iter(game), view="agent")))
    assert png.size == (PANEL_X, H - HEADER_H)
