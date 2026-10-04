"""The play API (docs/play.md): a person plays a human seat through the env's HTTP routes, on the same guarded path
as an agent's MCP tools, with the fake bridge (whose seats share one scripted game)."""

import asyncio
import json
import logging
import re
import shutil
import subprocess
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.context import RequestContext
from starlette.requests import Request

from agentenv_openciv3 import matchdata, server

pytestmark = pytest.mark.anyio

MATCH = {"seats": ["Greece"], "labels": {"Rome": "opus", "Greece": "you"}, "humans": ["Greece"]}


def request(path: str, token: str | None = None, body=None, *, header: bool = True, **params) -> Request:
    if token and not header:
        params["token"] = token
    headers = [(b"x-openciv3-token", token.encode())] if token and header else []
    data = json.dumps(body).encode() if body is not None else b""

    async def receive():
        return {"type": "http.request", "body": data, "more_body": False}
    return Request({"type": "http", "method": "POST" if body is not None else "GET", "path": path,
                    "query_string": urlencode(params).encode(), "headers": headers}, receive)


class Player:
    """A person in the browser: the play API with the seat's token."""

    def __init__(self, env, token: str):
        self.env, self.token = env, token

    async def raw(self, route: str, body=None, **params):
        handler = {"view": self.env._play_view, "status": self.env._play_status, "city": self.env._play_city,
                   "techs": self.env._play_techs, "diplomacy": self.env._play_diplomacy, "tile": self.env._play_tile,
                   "sites": self.env._play_sites, "act": self.env._play_act}[route]
        r = await handler(request(f"/play/api/{route}", self.token, body, **params))
        return r.status_code, json.loads(r.body)

    async def __call__(self, route: str, **params) -> dict:
        status, data = await self.raw(route, **params)
        assert status == 200, data
        return data

    async def act(self, tool: str, **args) -> dict:
        status, data = await self.raw("act", {"tool": tool, "args": args})
        assert status == 200, data
        return data


async def mcp(env, tool: str, seat: str | None = None, **args) -> str:
    """An agent's MCP tool call, with its X-OpenCiv3-Seat header when `seat` is given."""
    if seat is None:
        return (await env.mcp.call_tool(tool, args))[0].text
    req = SimpleNamespace(headers={server.SEAT_HEADER: seat})
    token = request_ctx.set(RequestContext(request_id=0, meta=None, session=None, lifespan_context=None, request=req))
    try:
        return (await env.mcp.call_tool(tool, args))[0].text
    finally:
        request_ctx.reset(token)


async def mcp_error(env, tool: str, seat: str | None = None, **args) -> str:
    with pytest.raises(ToolError) as info:
        await mcp(env, tool, seat, **args)
    return str(info.value)


def token_of(game: dict, civ: str) -> str:
    return game["play"][civ].split("#token=")[1]


@pytest.fixture
async def match(env):
    game = await env.new_game(**MATCH)
    return env, Player(env, token_of(game, "Greece"))


async def test_new_game_gives_each_human_seat_a_play_link(env, caplog):
    caplog.set_level(logging.INFO, logger=server.__name__)
    game = await env.new_game(**MATCH)
    assert list(game["play"]) == ["Greece"] and re.fullmatch(r"/play#token=[0-9a-f]{32}", game["play"]["Greece"])
    assert game["scenario"]["humans"] == ["Greece"] and game["scenario"]["human_turn_seconds"] == 900
    assert [(s.civ, s.human, bool(s.token)) for s in env.seats] == [("Rome", False, False), ("Greece", True, True)]
    lines = [r.getMessage() for r in caplog.records]
    assert f"NEW GAME {env.game_id}" in lines
    assert f"PLAY Greece (you) game {env.game_id} {game['play']['Greece']}" in lines

    again = await env.new_game(**MATCH)
    assert again["play"]["Greece"] != game["play"]["Greece"]
    assert (await env.new_game(seats=["Greece"]))["play"] == {}    # humans omitted: none
    with pytest.raises(ValueError, match="humans must be civs of the game"):
        await env.new_game(seats=["Greece"], humans=["Babylon"])
    with pytest.raises(ValueError, match="human_turn_seconds must be an integer"):
        await env.new_game(humans=["Rome"], human_turn_seconds=-1)


async def test_data_get_and_the_live_view_mark_human_seats(match):
    env, _ = match
    [part] = await env.data_get()
    assert [(s["civ"], s["human"]) for s in part.data["seats"]] == [("Rome", False), ("Greece", True)]
    assert part.data["human"] is False
    assert [(s["civ"], s["human"]) for s in env._live_now()["seats"]] == [("Rome", False), ("Greece", True)]


async def test_every_route_but_the_page_needs_the_seats_token(match):
    env, player = match
    for route in ("view", "status", "city", "techs", "diplomacy", "tile", "sites", "act"):
        for token in (None, "0" * 32):
            status, data = await Player(env, token).raw(route, {"tool": "end_turn"} if route == "act" else None)
            assert status == 401 and data["ok"] is False and data["error"]["code"] == "bad_token", route
    r = await env._play_status(request("/play/api/status", player.token, header=False))
    assert r.status_code == 200 and json.loads(r.body)["game"] == env.game_id
    await env.new_game(**MATCH)    # a new game: the old link plays nothing
    assert (await player.raw("status"))[0] == 401


async def test_the_play_page_is_served():
    from agentenv_openciv3 import viewer
    page = viewer.play_page()
    assert "/*__" not in page and "<script>" in page
    assert "class World" in page and "class ArtPainter" in page and "function boot" in page   # map.js, art.js, play.js


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
@pytest.mark.parametrize("which", ["play_page", "page"])
def test_the_pages_scripts_parse_as_one(which, tmp_path):
    """Each page's script files run as one script, so a name declared twice (a `const` in two files) is a
    SyntaxError that keeps the whole page from starting."""
    from agentenv_openciv3 import viewer
    scripts = re.findall(r"<script>(.*?)</script>", getattr(viewer, which)(), re.S)
    (tmp_path / "page.js").write_text("\n".join(scripts))
    check = subprocess.run(["node", "--check", str(tmp_path / "page.js")], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


async def test_play_page_route(env):
    r = await env._play_page(request("/play"))
    assert r.status_code == 200 and r.media_type == "text/html" and b"/*__" not in r.body


async def test_view_has_everything_the_ui_draws(match):
    env, player = match
    last_call = env.seats[1].last_call
    view = await player("view")
    assert set(view) == {"game", "colors", "state", "map", "notices"}
    game = view["game"]
    world = await env.bridge.call("world", seat="Rome")
    colors = matchdata.player_colors(world["players"])
    assert view["colors"] == {str(i): c for i, c in colors.items()}
    assert game["me"] == {"civ": "Greece", "label": "you", "index": 1, "color": colors[1]}
    assert (game["id"], game["turn"], game["turn_limit"], game["game_over"], game["victory"]) == (
        env.game_id, 1, 8, False, None)
    assert game["seats"] == [
        {"civ": "Rome", "label": "opus", "human": False, "ended": False, "defeated": False, "color": colors[0]},
        {"civ": "Greece", "label": "you", "human": True, "ended": False, "defeated": False, "color": colors[1]}]
    assert (game["ended"], game["waiting_for"], game["human_turn_seconds"], game["seconds_left"]) == (
        False, [], 900, None)
    assert view["state"]["turn"] == 1 and view["state"]["units"]
    assert view["map"]["players"][1]["me"] and view["map"]["tiles"] and view["map"]["units"][0]["id"] == "u1"
    assert view["notices"] == []
    # Polling is no sign of the person: no call logged, the stall clock runs on.
    await player("status")
    assert env.seats[1].last_call == last_call and env.seats[1].actions.calls == {}


async def test_actions_are_logged_like_the_agents_tool_calls(match, action_log):
    env, player = match
    res = await player.act("unit_order", unit="u1", order="found_city")
    assert res["ok"] and res["message"] == "Founded Rome at (12,10)." and res["result"]["city"]["id"] == "c1"
    res = await player.act("set_production", city="c1", item="warriors")
    assert res["message"].startswith("(read 'warriors' as 'Warrior') ") and res["result"]["city"]["producing"] == \
        "Warrior"
    assert (await player.act("set_rates", science=5))["result"]
    assert (await player.act("research"))["result"]["available"]    # no tech: the options, nothing changes
    await mcp(env, "unit_order", "Rome", unit="u2", order="auto_work")
    rows = action_log()
    assert [(r["tool"], r["args"], r["ok"], r["seat"]) for r in rows] == [
        ("unit_order", {"unit": "u1", "order": "found_city"}, True, "Greece"),
        ("set_production", {"city": "c1", "item": "warriors"}, True, "Greece"),
        ("set_rates", {"science": 5}, True, "Greece"),
        ("research", {}, True, "Greece"),
        ("unit_order", {"unit": "u2", "order": "auto_work"}, True, "Rome")]
    assert env.seats[1].actions.timeline[1] == [
        {"text": "u1 found_city", "ok": True}, {"text": "c1 builds warriors", "ok": True},
        {"text": "rates science 5", "ok": True}]
    assert env._live_now()["seats"][1]["calls"] == {"ok": 4, "failed": 0}
    # Explicit requests count as the person being there; they are logged as the agents' matching observations.
    before = env.seats[1].last_call
    assert (await player("city", city="c1"))["options"]
    assert "available" in await player("techs") and "civs" in await player("diplomacy")
    assert (await player("tile", x=12, y=10))["tiles"][0]["city_site"] is not None
    assert (await player("sites", unit="u3"))["sites"]
    assert env.seats[1].last_call > before
    assert [r["tool"] for r in action_log()[5:]] == ["city_info", "research", "diplomacy", "view_map",
                                                     "find_city_sites"]


async def test_a_person_queues_production_like_an_agent(match, action_log):
    env, player = match
    await player.act("unit_order", unit="u1", order="found_city")
    res = await player.act("set_production", city="c1", item="Warrior", then=["Settler", "Warrior"])
    assert res["ok"] and res["result"]["city"]["queue"] == ["Settler", "Warrior"]
    assert (await player("city", city="c1"))["queue"] == ["Settler", "Warrior"]
    res = await player.act("set_production", city="c1", item="Warrior", then=[])
    assert res["ok"] and res["result"]["city"]["queue"] == []
    assert action_log()[-1]["args"] == {"city": "c1", "item": "Warrior", "then": []}


async def test_a_refused_action_answers_the_error(match, action_log):
    env, player = match
    status, data = await player.raw("act", {"tool": "unit_order", "args": {"unit": "u2", "order": "explore"}})
    assert status == 200 and data["ok"] is False
    assert data["error"]["code"] == "invalid_order" and "auto_work" in data["error"]["alternatives"]
    assert set(data["error"]) == {"code", "message", "alternatives", "suggest"}
    assert action_log()[-1] | {"ts": 0, "ms": 0} == {
        "ts": 0, "turn": 1, "tool": "unit_order", "args": {"unit": "u2", "order": "explore"}, "ok": False,
        "error_code": "invalid_order", "ms": 0, "seat": "Greece"}
    assert env.seats[1].actions.timeline[1] == [{"text": "u2 explore ✗ invalid_order", "ok": False}]
    for body, code in [({"tool": "set_rates", "args": {"science": 11}}, "bad_args"),
                       ({"tool": "unit_order", "args": {"unit": "u1"}}, "bad_args"),
                       ({"tool": "unit_order", "args": {"unit": "u1", "order": "hold", "bogus": 1}}, "bad_args"),
                       ({"tool": "plan", "args": {"text": "x"}}, "unknown_tool"),
                       ({"tool": "end_turn", "args": {"skip_idle": False}}, "bad_args"),
                       (["end_turn"], "bad_request")]:
        status, data = await player.raw("act", body)
        assert (status, data["error"]["code"]) == (400, code), body
    status, data = await player.raw("city")
    assert (status, data["error"]["code"]) == (400, "bad_args")
    data = await player.act("diplomacy", action="declare_war")
    assert data["ok"] is False and data["error"]["code"] == "bad_args"


async def test_end_turn_answers_at_once_and_the_turn_advances_when_every_seat_has_ended_it(match, action_log):
    env, player = match
    res = await player.act("end_turn")
    assert res == {"ok": True, "advanced": False, "turn": 1, "waiting_for": ["Rome"]}
    assert action_log()[-1]["args"] == {"skip_idle": True, "until_attention": False, "max_turns": 5}
    status = await player("status")
    assert status["ended"] and status["waiting_for"] == ["Rome"] and status["turn"] == 1
    assert [s["ended"] for s in status["seats"]] == [False, True]
    assert (await player.act("end_turn"))["advanced"] is False    # again: still waiting, the bridge isn't asked
    text = await mcp(env, "end_turn", "opus", skip_idle=True)
    assert text.startswith("TURN T1 → T2")
    status = await player("status")
    assert (status["turn"], status["ended"], status["waiting_for"]) == (2, False, [])

    # The agent ends first and waits; the person's end_turn advances the game and the agent gets its report.
    waiting = asyncio.create_task(mcp(env, "end_turn", "Rome", skip_idle=True))
    await asyncio.sleep(0.3)
    assert not waiting.done()
    view = await player("view")
    assert view["game"]["seconds_left"] is not None and 0 < view["game"]["seconds_left"] <= 900
    assert await player.act("end_turn") == {"ok": True, "advanced": True, "turn": 3, "waiting_for": []}
    assert (await asyncio.wait_for(waiting, 5)).startswith("TURN T2 → T3")


async def test_a_persons_end_turn_keeps_the_broadcast_pace_too(env):
    game = await env.new_game(**MATCH, min_turn_seconds=1)
    player = Player(env, token_of(game, "Greece"))
    started = env.turn_started
    waiting = asyncio.create_task(mcp(env, "end_turn", "Rome", skip_idle=True))
    await asyncio.sleep(0.1)
    assert await player.act("end_turn") == {"ok": True, "advanced": True, "turn": 2, "waiting_for": []}
    assert env.turn_started - started >= 1
    assert (await asyncio.wait_for(waiting, 5)).startswith("TURN T1 → T2")


async def test_a_person_gets_the_agents_messages_as_notices(match):
    env, player = match
    await mcp(env, "message", "Rome", to="you", text="Stay out of my way.")
    await mcp(env, "message", "Rome", to="all", text="Hello, world.")
    view = await player("view")
    assert view["notices"] == [
        {"turn": 1, "kind": "message", "from": "Rome", "label": "opus", "to_all": False, "text": "Stay out of my way."},
        {"turn": 1, "kind": "message", "from": "Rome", "label": "opus", "to_all": True, "text": "Hello, world."}]
    assert (await mcp(env, "message", "Rome", to="Greece", text="a")).startswith("Sent to Greece (you)")


async def test_an_idle_human_seat_has_its_turn_ended_after_human_turn_seconds(env, monkeypatch):
    monkeypatch.setattr(server, "STALL_CHECK_SECONDS", 0.05)
    monkeypatch.setattr(server, "SEAT_STALL_SECONDS", 0.05)    # agents' limit: not the human's
    game = await env.new_game(**MATCH, human_turn_seconds=1)
    player = Player(env, token_of(game, "Greece"))
    waiting = asyncio.create_task(mcp(env, "end_turn", "Rome", skip_idle=True))
    await asyncio.sleep(0.4)
    assert not waiting.done()
    assert (await asyncio.wait_for(waiting, 5)).startswith("TURN T1 → T2")
    view = await player("view")
    assert view["notices"] == [{"turn": 1, "kind": "turn_ended", "text": "the env ended your turn 1 after 1 s "
                                "without a move from you, so the other civilizations could play on"}]
    [part] = await env.data_get()
    assert [s["auto_ended_turns"] for s in part.data["seats"]] == [0, 1]
    assert server.duration(900) == "15 min" and server.duration(90) == "90 s"


async def test_a_human_seat_with_human_turn_seconds_0_is_never_ended_for(env, monkeypatch):
    monkeypatch.setattr(server, "STALL_CHECK_SECONDS", 0.05)
    monkeypatch.setattr(server, "SEAT_WAIT_SECONDS", 0.5)
    game = await env.new_game(**MATCH, human_turn_seconds=0)
    env.seats[1].last_call -= 3600
    text = await mcp(env, "end_turn", "Rome", skip_idle=True)
    assert text.startswith("WAITING — you ended turn T1; Greece")
    view = await Player(env, token_of(game, "Greece"))("view")
    assert view["game"]["seconds_left"] is None and view["game"]["human_turn_seconds"] == 0
    assert env.seats[1].auto_ended_turns == 0


async def test_agents_cannot_play_a_human_seat(env):
    await env.new_game(**MATCH)
    assert "Greece is played by a person." in await mcp_error(env, "get_turn_brief", "Greece")
    assert "Greece is played by a person." in await mcp_error(env, "unit_order", "you", unit="u1", order="hold")
    assert (await mcp(env, "get_turn_brief", "Rome")).startswith("T1/8 (3950 BC) · Rome")
    # no header: the first seat, an agent's
    assert (await mcp(env, "get_turn_brief")).startswith("T1/8 (3950 BC) · Rome")
    assert env.seats[1].actions.calls == {}

    await env.new_game(seats=["Greece"], humans=["Rome"])
    assert "Rome is played by a person; name your own seat in the X-OpenCiv3-Seat header." in await mcp_error(
        env, "get_turn_brief")
    assert (await mcp(env, "get_turn_brief", "Greece")).startswith("T1/8")


async def test_a_game_with_one_human_seat(env, action_log):
    game = await env.new_game(humans=["rome"])
    assert list(game["play"]) == ["Rome"] and not env.multi
    player = Player(env, token_of(game, "Rome"))
    assert "Rome is played by a person." in await mcp_error(env, "get_turn_brief")
    assert "Rome is played by a person." in await mcp_error(env, "get_turn_brief", "anyone")
    view = await player("view")
    assert view["game"]["me"]["civ"] == "Rome" and view["game"]["seats"][0]["human"]
    await player.act("unit_order", unit="u1", order="found_city")
    assert await player.act("end_turn") == {"ok": True, "advanced": True, "turn": 2, "waiting_for": []}
    assert (await player("status"))["turn"] == 2
    assert [r["tool"] for r in action_log()] == ["unit_order", "end_turn"]
    assert "seat" not in action_log()[0]
    [part] = await env.data_get()
    assert part.data["human"] is True and [(s["civ"], s["human"]) for s in part.data["seats"]] == [("Rome", True)]
    assert env._live_now()["seats"][0]["human"] is True
    # A one-seat game without humans plays as ever.
    await env.new_game(humans=None)
    assert (await mcp(env, "get_turn_brief", "anyone")).startswith("T1/8 (3950 BC) · Rome")
