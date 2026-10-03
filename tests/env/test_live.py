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


def test_the_page_carries_the_client_art_painter_in_its_own_scope():
    """The live page brings map.js, art.js and viewart.js inside one function (their helpers share names with
    app.js's), before app.js, which loads the art from play/art/ when the env has it."""
    page = viewer.page()
    kit = page.index("const ArtKit = (() => {")
    assert kit < page.index("class ViewArt") < page.index("return {Art, ViewArt};") < page.index("class Match {")
    assert page.count("class ArtPainter") == 1 and 'ArtKit.Art.load("play/art/")' in page


def snapshot(turn: int, *, tiles, cities=(), units=(), moves=None, battles=None) -> dict:
    snap = {"schema": 2, "turn": turn, "seed": 1, "turn_limit": 10, "map": {"width": 8, "height": 4, "wrap_x": True},
            "seats": [{"index": 1, "civ": "Rome", "label": "opus"}],
            "players": [{"index": 0, "civ": "Barbarians", "score": {k: 0 for k in matchdata.SCORE_KEYS}},
                        {"index": 1, "civ": "Rome", "is_human": True, "label": "opus",
                         "score": {k: 0 for k in matchdata.SCORE_KEYS}}],
            "tiles": tiles, "cities": list(cities), "units": list(units), "events": []}
    if moves is not None:
        snap["moves"], snap["battles"] = moves, battles or []
    return snap


def test_match_data_carries_what_the_client_art_draws():
    """How tiles look (overlay, resource, improvements, bonus grassland) as per-turn changes, river edges, a city's era
    and walls, a unit's hit points when they aren't a healthy 3, and the turn's moves and battles (docs/viewer.md)."""
    tiles0 = [[0, 0, "grassland", "forest", -1, 3, 1, None, [], 0],
              [2, 0, "plains", None, 1, 0, 1, "Wheat", ["road"], 0],
              [1, 1, "grassland", None, 1, 0, 0, None, [], 1]]
    tiles1 = [[0, 0, "grassland", None, -1, 3, 1, None, [], 1],
              [2, 0, "plains", None, 1, 0, 1, "Wheat", ["road", "irrigation"], 0],
              [1, 1, "grassland", None, 1, 0, 1, None, [], 1]]
    city = {"id": "city-1", "x": 2, "y": 0, "name": "Rome", "owner": 1, "size": 1, "capital": True, "production": None,
            "era": 1, "walls": True}
    healthy = {"id": "Warrior-1", "x": 1, "y": 1, "owner": 1, "type": "Warrior", "hp": 3, "hp_max": 3,
               "fortified": False}
    hurt = {**healthy, "id": "Warrior-2", "hp": 2, "fortified": True}
    side = {"owner": 1, "type": "Warrior", "x": 1, "y": 1, "hp_before": 3, "hp_after": 2, "hp_max": 3}
    battle = {"id": 1, "seq": 4, "turn": 0, "kind": "attack", "attacker": side,
              "defender": {**side, "owner": 0, "type": "Horseman", "x": 2, "y": 0, "hp_after": 0},
              "rounds": ["a", "d", "a", "a", "a"], "winner": "attacker", "city": None, "captured": False,
              "razed": False, "seen": 1}
    walk = {"seq": 3, "unit": "Warrior-1", "owner": 1, "type": "Warrior", "path": [[0, 0], [1, 1]], "seen": 1}
    snaps = [snapshot(0, tiles=tiles0, cities=[city], units=[healthy], moves=[]),
             snapshot(1, tiles=tiles1, cities=[city], units=[healthy, hurt], moves=[walk], battles=[battle])]
    d = matchdata.MatchData.from_snapshots(snaps).document()
    assert d["meta"]["resources"] == ["Wheat"] and d["meta"]["improvements"] == ["road", "irrigation"]
    assert [t[4] for t in d["static"]["tiles"]] == [3, 0, 0], "the river's edges"
    t0, t1 = d["turns"]
    forest, grass = matchdata.TERRAIN.index("forest"), matchdata.TERRAIN.index("grassland")
    assert t0["looks"] == [[1, -1, 0, 1, 0], [2, -1, -1, 0, 1]] and forest == d["static"]["tiles"][0][3]
    assert t1["looks"] == [[0, -1, -1, 0, 1], [1, -1, 0, 3, 0]] and grass != forest, "the forest cleared, irrigation"
    assert t0["cities"][0][8:] == [1, 1]
    assert t1["units"] == [[1, 1, 1, 1, 0], [2, 1, 1, 1, 0, 2, 3, 1]]   # id 0 is the city's
    assert "moves" not in t0 and "battles" not in t0
    assert t1["moves"] == [[3, 1, 1, 0, 1, 0, 0, 1, 1]]
    assert t1["battles"] == [[4, 0, "a", "adaaa", 0, 1, 1, 0, 1, 1, 3, 2, 3, 0, 1, 2, 0, 3, 0, 3]]
    assert d["meta"]["unit_types"] == ["Warrior", "Horseman"]
    # Snapshots from before the art's fields: no looks, moves or battles, and the rest as it was.
    old = [snapshot(t, tiles=[r[:7] for r in tiles0], units=[{k: v for k, v in healthy.items() if k in
                                                               ("id", "x", "y", "owner", "type")}]) for t in (0, 1)]
    od = matchdata.MatchData.from_snapshots(old).document()
    assert not {"looks", "moves", "battles"} & {k for t in od["turns"] for k in t}
    assert od["turns"][1]["units"] == [[0, 1, 1, 1, 0]] and od["meta"]["resources"] == []


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
        "broadcast": None, "messages": [], "seats": []}

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
