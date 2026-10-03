"""The match viewer's server side (docs/viewer.md): the page, /live/data.json, the client's view per seat, and what
the recording hands the viewer: per-seat actions, call counts and client videos."""

import gzip
import json
import shutil
import threading
from pathlib import Path

import anyio
import pytest
from starlette.requests import Request

from agentenv_openciv3 import client, live, matchdata, recording, viewer
from agentenv_openciv3.actionlog import ActionLog
from agentenv_openciv3.server import Seat

pytestmark = pytest.mark.anyio


def get(path: str, **params) -> Request:
    query = "&".join(f"{k}={v}" for k, v in params.items()).encode()
    return Request({"type": "http", "method": "GET", "path": path, "query_string": query, "headers": []})


async def data(env, since=None) -> dict:
    r = await env._live_data(get("/live/data.json", **({} if since is None else {"since": since})))
    assert r.status_code == 200, r.body
    return json.loads(r.body)


def add_seat(env, civ: str, label: str | None = None) -> Seat:
    """A second seat on the fake bridge's one-seat game: enough for the env's per-seat bookkeeping."""
    seat = Seat(civ, label, ActionLog(None))
    env.seats.append(seat)
    return seat


def test_the_page_embeds_the_app_and_the_data():
    live_page = viewer.page()
    assert "window.OPENCIV_DATA = null;" in live_page and "window.OPENCIV_VIDEOS = null;" in live_page
    assert "/*__" not in live_page
    # Agents' text ends up in the data: neither `</script>` nor `<!--<script>` may end the script or keep it open.
    doc = {"game": "g-1", "turns": [{"events": [{"text": "</script><script>alert(1)</script>"}],
                                      "messages": [{"from": 0, "to": "all", "text": "Hold <!--<script> the river"}]}]}
    videos = {"Rome": {"file": "x.client-A.mp4", "fps": 4, "turns": [0, 1]}}
    page = viewer.page(doc, videos)
    assert page.count("<script") == live_page.count("<script")
    embedded = page.split("window.OPENCIV_DATA = ", 1)[1].split("; window.OPENCIV_VIDEOS = ", 1)
    assert "<" not in embedded[0] and json.loads(embedded[0]) == doc
    assert json.loads(embedded[1].split(";</script>", 1)[0]) == videos


def test_the_action_log_counts_every_call_per_turn():
    log = ActionLog(None)
    for turn, tool, ok in [(1, "get_turn_brief", True), (1, "unit_order", False), (2, "end_turn", True),
                           (None, "plan", True)]:
        log.record(turn=turn, tool=tool, args={"unit": "u1", "order": "settle"}, ok=ok, error_code=None, ms=1)
    assert log.calls == {1: {"ok": 1, "failed": 1}, 2: {"ok": 1, "failed": 0}}
    assert log.timeline == {1: [{"text": "u1 settle ✗ None", "ok": False}]}


async def test_data_json_sends_the_turns_after_since_and_the_turn_being_played(env, tools):
    first = await data(env)
    assert first["game"] is None and first["turns"] == [] and first["live"] == {
        "turn": None, "game_over": False, "victory": None, "client": False, "recording": True, "min_turn_seconds": 0,
        "messages": [], "seats": []}

    await tools("unit_order", unit="u1", order="found_city")
    await tools.error("unit_order", unit="u2", order="explore")
    await tools("get_turn_brief")
    doc = await data(env)
    assert doc["schema"] == 1 and doc["game"].startswith("g-") and doc["static"]["tiles"]
    assert [t["turn"] for t in doc["turns"]] == [1] and doc["meta"]["turn_limit"] == 8
    assert [(p["civ"], p["seat"]) for p in doc["players"]][:2] == [("Rome", 0), ("Greece", None)]
    [seat] = doc["live"]["seats"]
    assert doc["live"]["turn"] == 1 and seat["calls"] == {"ok": 2, "failed": 1} and not seat["ended"]
    assert seat["actions"] == [{"text": "u1 found_city", "ok": True},
                               {"text": "u2 explore ✗ invalid_order", "ok": False}]
    assert 0 <= seat["seconds"] < 30

    await tools("end_turn", skip_idle=True)
    later = await data(env, since=1)
    assert "static" not in later and [t["turn"] for t in later["turns"]] == [2]
    assert later["turns"][0]["actions"] == {"0": seat["actions"]}
    assert later["turns"][0]["calls"] == {"0": {"ok": 3, "failed": 1}}
    assert later["live"]["turn"] == 2 and later["live"]["seats"][0]["calls"] == {"ok": 0, "failed": 0}
    assert (await data(env, since=2))["turns"] == []
    assert [t["turn"] for t in (await data(env, since=-1))["turns"]] == [1, 2]

    bad = await env._live_data(get("/live/data.json", since="two"))
    assert bad.status_code == 400

    await env.new_game(seed=5)
    fresh = await data(env)
    assert fresh["game"] != doc["game"] and [t["turn"] for t in fresh["turns"]] == [1]
    assert fresh["meta"]["seed"] == 5


async def test_data_json_reads_each_snapshot_once_and_waits_for_a_whole_one(env, tools, monkeypatch):
    await tools("get_turn_brief")
    record = env.game_dir / "record"
    whole = gzip.decompress((record / "turn-0001.json.gz").read_bytes())
    (record / "turn-0002.json.gz").write_bytes(gzip.compress(whole)[:200])
    reads = []
    read = matchdata.read_snapshot
    monkeypatch.setattr(matchdata, "read_snapshot", lambda p: reads.append(p.name) or read(p))
    assert [t["turn"] for t in (await data(env))["turns"]] == [1]
    snap = json.loads(whole)
    (record / "turn-0002.json.gz").write_bytes(gzip.compress(json.dumps({**snap, "turn": 2}).encode()))
    assert [t["turn"] for t in (await data(env))["turns"]] == [1, 2]
    assert [t["turn"] for t in (await data(env, since=1))["turns"]] == [2]
    assert reads == ["turn-0001.json.gz", "turn-0002.json.gz", "turn-0002.json.gz"]


async def test_the_live_seats_say_who_has_ended_the_turn(env, tools):
    await tools("get_turn_brief")
    greece = add_seat(env, "Greece", "B")
    greece.actions.record(turn=1, tool="unit_order", args={"unit": "u1", "order": "found_city"}, ok=True,
                          error_code=None, ms=1)
    greece.ready, greece.ended_at = True, env.turn_started + 2.5
    doc = await data(env)
    rome, greek = doc["live"]["seats"]
    assert (rome["civ"], rome["label"], rome["ended"]) == ("Rome", None, False)
    assert greek == {"civ": "Greece", "label": "B", "human": False, "ended": True, "seconds": 2.5,
                     "calls": {"ok": 1, "failed": 0}, "actions": [{"text": "u1 found_city", "ok": True}],
                     "note": None, "plan": None, "plan_turn": None}


async def test_the_client_view_draws_the_seat_asked_for(env, tools, monkeypatch):
    await tools("get_turn_brief")
    env.client = True
    add_seat(env, "Greece", "B")
    saves = env.game_dir / "saves"
    saves.mkdir()
    (saves / "turn-0001.json.gz").write_bytes(b"")
    drawn, gate = [], threading.Event()

    def frame(save: Path, *, seat: str | None = None) -> bytes:
        drawn.append((save.name, seat))
        gate.wait(10)
        return f"png of {seat}".encode()
    monkeypatch.setattr(client, "frame", frame)

    async def shown(**params):
        return await env._live_client(get("/live/client.png", **params))

    assert (await shown(seat="greece")).status_code == 503
    assert (await shown(seat="Rome")).status_code == 503    # one render at a time: Rome waits its turn
    while not drawn:
        await anyio.sleep(0.01)
    assert drawn == [("turn-0001.json.gz", "Greece")]
    gate.set()
    await env.live.client_task
    greece = await shown(seat="Greece")
    assert greece.status_code == 200 and greece.body == b"png of Greece"
    assert greece.headers["x-openciv3-turn"] == "1" and greece.headers["x-openciv3-seat"] == "Greece"
    assert (await shown()).status_code == 503              # the default seat is the first, Rome
    await env.live.client_task
    assert (await shown()).body == b"png of Rome"
    assert drawn == [("turn-0001.json.gz", "Greece"), ("turn-0001.json.gz", "Rome")]
    unknown = await shown(seat="Babylon")
    assert unknown.status_code == 400 and b"not a seat" in unknown.body

    (saves / "turn-0002.json.gz").write_bytes(b"")
    assert (await shown(seat="Greece")).body == b"png of Greece"   # the old frame while the new one is drawn
    await env.live.client_task
    assert (await shown(seat="Greece")).headers["x-openciv3-turn"] == "2"


async def test_one_seat_draws_without_naming_it(env, tools, monkeypatch):
    await tools("get_turn_brief")
    env.client = True
    (env.game_dir / "saves").mkdir()
    (env.game_dir / "saves" / "turn-0001.json.gz").write_bytes(b"")
    monkeypatch.setattr(client, "frame", lambda save, **kw: json.dumps(kw).encode())
    assert (await env._live_client(get("/live/client.png", seat="Rome"))).status_code == 503
    await env.live.client_task
    assert (await env._live_client(get("/live/client.png"))).body == b"{}"


@pytest.fixture
def rendered(monkeypatch):
    """recording.render, replaced: the keyword arguments of each call."""
    calls = []

    def render(snapshots, *, formats, name, **kw):
        calls.append({"snapshots": snapshots, "formats": formats, "name": name, **kw})
        return [recording.File(f"{name}.{f}", recording.FORMATS[f], b"x") for f in formats], []
    monkeypatch.setattr(recording, "render", render)
    return calls


async def test_the_recording_gets_each_players_actions_and_calls(env, tools, rendered):
    await tools("unit_order", unit="u1", order="found_city")
    await tools.error("unit_order", unit="u2", order="explore")
    await tools("end_turn", skip_idle=True)
    res = await env.recording(formats=["html"])
    assert [f["name"] for f in res["files"]] == ["openciv3-seed3.html"]
    [call] = rendered
    assert call["seat_actions"] == {1: {0: [{"text": "u1 found_city", "ok": True},
                                            {"text": "u2 explore ✗ invalid_order", "ok": False}]}}
    assert call["calls"] == {1: {0: {"ok": 2, "failed": 1}}}
    assert call["client_videos"] is None and 1 in call["actions"]


async def test_the_client_records_every_seat_in_a_file_of_its_own(env, tools, rendered, monkeypatch):
    await tools("end_turn", skip_idle=True)
    env.client = True
    greece = add_seat(env, "Greece", "Claude Opus")
    greece.actions.note(1, "c1 builds Warrior")
    saves = env.game_dir / "saves"
    saves.mkdir()
    for t in (1, 2):
        (saves / f"turn-{t:04d}.json.gz").write_bytes(b"")
    asked = []

    def render_seats(saves_dir, seats, *, fps):
        asked.append((saves_dir, seats, fps))
        return {civ: f"mp4 of {civ}".encode() for civ in seats}
    monkeypatch.setattr(client, "render_seats", render_seats)

    res = await env.recording(formats=["html", "client_mp4"], fps=3)
    assert res["notes"] == [] and asked == [(saves, ["Rome", "Greece"], 3)]
    assert [f["name"] for f in res["files"]] == ["openciv3-seed3-seats.html", "openciv3-seed3-seats.client-Rome.mp4",
                                                 "openciv3-seed3-seats.client-Claude-Opus.mp4"]
    assert rendered[0]["client_videos"] == {
        "Rome": {"file": "openciv3-seed3-seats.client-Rome.mp4", "fps": 3, "turns": [1, 2]},
        "Greece": {"file": "openciv3-seed3-seats.client-Claude-Opus.mp4", "fps": 3, "turns": [1, 2]}}
    assert rendered[0]["seat_actions"][1][1] == [{"text": "c1 builds Warrior", "ok": True}]

    res = await env.recording(formats=["client_mp4"], client_seats=["claude opus"])
    assert asked[-1][1] == ["Greece"] and [f["name"] for f in res["files"]] == [
        "openciv3-seed3-seats.client-Claude-Opus.mp4"]
    with pytest.raises(ValueError, match="client_seats: 'Babylon' is not a seat"):
        await env.recording(formats=["client_mp4"], client_seats=["Babylon"])


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
async def test_one_seat_keeps_its_client_mp4_name(fake_client, tool_env, rendered):
    env, tools = await tool_env()
    await tools("end_turn", skip_idle=True)
    res = await env.recording(formats=["html", "client_mp4"])
    assert [f["name"] for f in res["files"]] == ["openciv3-seed3.html", "openciv3-seed3.client.mp4"]
    assert rendered[0]["client_videos"] == {"Rome": {"file": "openciv3-seed3.client.mp4", "fps": 4, "turns": [1, 2]}}


def test_by_player_maps_civs_to_player_indices():
    players = [{"index": 0, "civ": "Rome"}, {"index": 3, "civ": "Greece"}, {"index": 4, "civ": "Barbarians"}]
    assert live.by_player({"Greece": {5: ["a"]}, "Rome": {5: ["b"]}, "Egypt": {5: ["c"]}}, players) == {
        5: {3: ["a"], 0: ["b"]}}


async def test_live_match_starts_over_with_a_new_game(tmp_path):
    lv = live.Live()
    first = lv.match_of(tmp_path, "g-1")
    assert lv.match_of(tmp_path, "g-1") is first and lv.match_of(tmp_path, "g-2") is not first
    body = json.loads(await lv.match.document(-1, {}, {}, {"turn": None}))
    assert body["game"] == "g-2" and body["turns"] == [] and body["live"] == {"turn": None}
