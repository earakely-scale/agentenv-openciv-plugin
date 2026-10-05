"""A game with several seats, one per agent, on the real bridge: each request plays the seat its X-OpenCiv3-Seat
header names, end_turn waits for every seat, and a silent seat's turn is ended for it. A game with one seat ignores
the header, which every agent deploy_agent names sends."""

import asyncio
import os
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.context import RequestContext

from agentenv_openciv3 import server
from agentenv_openciv3.server import OpenCiv3Env

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parents[2]
REAL_CMD = os.environ.get("CIVBRIDGE_CMD", str(ROOT / "build" / "bridge" / "CivBridge"))
SEATS = ("Rome", "Greece", "Egypt")
GAME = {"seed": 1, "opponents": 2, "seats": ["Greece", "Egypt"], "labels": {"Rome": "A", "Greece": "B"},
        "turn_limit": 10}


class SeatTools:
    """Calls tools as the agent playing `civ` would: through FastMCP, with the seat header on the request."""

    def __init__(self, env: OpenCiv3Env, civ: str):
        self.env, self.civ = env, civ

    async def _call(self, name: str, args: dict):
        request = SimpleNamespace(headers={server.SEAT_HEADER: self.civ})
        token = request_ctx.set(RequestContext(request_id=0, meta=None, session=None, lifespan_context=None,
                                               request=request))
        try:
            return await self.env.mcp.call_tool(name, args)
        finally:
            request_ctx.reset(token)

    async def __call__(self, name: str, **args) -> str:
        return (await self._call(name, args))[0].text

    async def error(self, name: str, **args) -> str:
        with pytest.raises(ToolError) as info:
            await self._call(name, args)
        return str(info.value)


@pytest.fixture
async def seats(env_vars, monkeypatch):
    if not Path(shlex.split(REAL_CMD)[0]).exists():
        pytest.fail(f"no CivBridge at {REAL_CMD}: run scripts/build-bridge.sh")
    monkeypatch.setenv("CIVBRIDGE_CMD", REAL_CMD)
    env = OpenCiv3Env()
    env.create_app()
    await env.new_game(**GAME)
    yield env, {civ: SeatTools(env, civ) for civ in SEATS}
    await env.close()


async def settle(tools: dict) -> None:
    for t in tools.values():
        await t("unit_order", unit="u1", order="found_city")


async def test_each_request_plays_its_seat(seats):
    env, tools = seats
    for civ in SEATS:
        assert (await tools[civ]("get_turn_brief")).startswith(f"T0/10 (4000 BC) · {civ} · Despotism")
    assert "u1 Settler founded Athens (c1)" in await tools["Greece"]("unit_order", unit="u1", order="found_city")
    assert "found here: yes" in await tools["Rome"]("list_units", filter="all")
    assert "'Babylon' is not a seat in this game; the seats are Rome, Greece, Egypt." in await SeatTools(
        env, "Babylon").error("get_turn_brief")
    with pytest.raises(ValueError, match="every seat ends its own turns"):
        await env.autoplay(turns=1)


async def test_an_agent_names_its_seat_by_its_label(seats):
    env, _ = seats
    assert (await SeatTools(env, "b")("get_turn_brief")).startswith("T0/10 (4000 BC) · Greece")
    brief = await SeatTools(env, "Egypt")("get_turn_brief")
    assert ("MATCH vs agents Rome, Greece · every agent plays each turn at once; end_turn waits for the others · it "
            "ends at T10, or once one agent's civilization is the last an agent plays (conquest)") in brief


async def test_the_last_agent_standing_wins_and_the_game_ends(env_vars, monkeypatch):
    monkeypatch.setenv("CIVBRIDGE_CMD", REAL_CMD)
    env = OpenCiv3Env()
    env.create_app()
    try:
        await env.new_game(seed=1, opponents=1, seats=["Greece"], labels={"Rome": "A", "Greece": "B"}, turn_limit=10)
        greece, rome = SeatTools(env, "Greece"), SeatTools(env, "A")
        await greece("unit_order", unit="u2", order="disband")
        await greece("unit_order", unit="u1", order="disband")
        text = await rome("end_turn", skip_idle=True)
        assert "Rome (A) won by conquest: it is the last civilization left." in text
        assert "GAME OVER — you won by conquest on T1." in text
        [part] = await env.data_get()
        assert part.data["victory"] == {"kind": "conquest", "civ": "Rome", "label": "A", "turn": 1}
        assert part.data["game_over"]
    finally:
        await env.close()


async def test_a_seat_knocked_out_is_not_the_end_of_the_game(seats):
    """A defeated seat's own state says game over, since the game is over for it, while the others play on: the
    live view, the play page's status and data/get keep the game going until a victory or the turn limit."""
    env, tools = seats
    await tools["Egypt"]("unit_order", unit="u2", order="disband")
    await tools["Egypt"]("unit_order", unit="u1", order="disband")
    await settle({civ: tools[civ] for civ in ("Rome", "Greece")})
    texts = await asyncio.gather(*(tools[civ]("end_turn", skip_idle=True) for civ in ("Rome", "Greece")))
    assert all(text.startswith("TURN T0 → T1") for text in texts)
    [part] = await env.data_get()
    assert [s["defeated"] for s in part.data["seats"]] == [False, False, True]
    assert env.seats[2].last_state["game_over"]   # over for Egypt
    assert not part.data["game_over"] and part.data["victory"] is None
    live = env._live_now()
    assert (live["game_over"], live["victory"], live["turn"]) == (False, None, 1)
    assert env._game_over() == (False, None)
    assert (await tools["Rome"]("get_turn_brief")).startswith("T1/10")


async def test_end_turn_waits_for_every_seat(seats):
    env, tools = seats
    await settle(tools)
    rome = asyncio.create_task(tools["Rome"]("end_turn", skip_idle=True))
    greece = asyncio.create_task(tools["Greece"]("end_turn", skip_idle=True))
    await asyncio.sleep(0.3)
    assert not rome.done() and not greece.done()
    # The waiting seats leave the env free: Egypt still plays its turn.
    assert (await tools["Egypt"]("get_turn_brief")).startswith("T0/10 (4000 BC) · Egypt")
    egypt = await tools["Egypt"]("end_turn", skip_idle=True)
    for text in (await rome, await greece, egypt):
        assert text.startswith("TURN T0 → T1 (1 turn)")
    assert (await tools["Greece"]("get_turn_brief")).startswith("T1/10 (3950 BC) · Greece")

    [part] = await env.data_get()
    data = part.data
    assert [(s["civ"], s["label"], s["auto_ended_turns"]) for s in data["seats"]] == [
        ("Rome", "A", 0), ("Greece", "B", 0), ("Egypt", None, 0)]
    assert all(s["score"]["cities"] == 1 and s["actions"]["ok"] >= 2 and s["rank"] for s in data["seats"])
    assert {p["seat"] for p in data["standings"]} == {"A", "B", "Egypt"}
    assert data["baselines"]["engine_ai"] == {"status": "disabled"}


async def test_a_silent_seat_has_its_turn_ended(seats, monkeypatch):
    env, tools = seats
    monkeypatch.setattr(server, "SEAT_STALL_SECONDS", 0.3)
    monkeypatch.setattr(server, "STALL_CHECK_SECONDS", 0.05)
    await settle(tools)
    ended = await asyncio.gather(tools["Rome"]("end_turn", skip_idle=True), tools["Greece"]("end_turn", skip_idle=True))
    assert all(text.startswith("TURN T0 → T1") for text in ended)
    brief = await tools["Egypt"]("get_turn_brief")
    assert brief.startswith("!! the env ended your turn 0 after 0.3 s without a call from you")
    [part] = await env.data_get()
    assert [s["auto_ended_turns"] for s in part.data["seats"]] == [0, 0, 1]


async def test_seats_that_waited_are_not_taken_for_silent_on_the_next_turn(seats, monkeypatch):
    env, tools = seats
    monkeypatch.setattr(server, "SEAT_STALL_SECONDS", 0.5)
    monkeypatch.setattr(server, "STALL_CHECK_SECONDS", 0.05)
    await settle(tools)
    waiting = asyncio.gather(tools["Greece"]("end_turn", skip_idle=True), tools["Egypt"]("end_turn", skip_idle=True))
    for _ in range(5):  # Rome plays on past the stall time of the seats that wait, then falls silent
        await tools["Rome"]("get_turn_brief")
        await asyncio.sleep(0.2)
    assert all(text.startswith("TURN T0 → T1") for text in await waiting)
    [part] = await env.data_get()
    assert [s["auto_ended_turns"] for s in part.data["seats"]] == [1, 0, 0] and part.data["turn"] == 1


async def test_the_turn_ends_when_the_seat_still_playing_it_is_defeated(env_vars, monkeypatch):
    monkeypatch.setenv("CIVBRIDGE_CMD", REAL_CMD)
    monkeypatch.setattr(server, "STALL_CHECK_SECONDS", 60)
    env = OpenCiv3Env()
    env.create_app()
    try:
        await env.new_game(seed=1, opponents=1, seats=["Greece"], labels={"Rome": "greece", "Greece": "rome"},
                           turn_limit=10)
        rome, greece = SeatTools(env, "greece"), SeatTools(env, "rome")
        assert (await rome("get_turn_brief")).startswith("T0/10 (4000 BC) · Rome")
        waiting = asyncio.create_task(rome("end_turn", skip_idle=True))
        await asyncio.sleep(0.3)
        await greece("unit_order", unit="u2", order="disband")
        await greece("unit_order", unit="u1", order="disband")
        assert "GAME OVER — you won by conquest on T1." in await asyncio.wait_for(waiting, 10)
    finally:
        await env.close()


async def test_a_header_that_names_no_seat_touches_no_seat(seats):
    env, tools = seats
    env.seats[0].notices.append({"turn": 0, "kind": "turn_ended", "text": "a notice for Rome"})
    assert "'Babylon' is not a seat" in await SeatTools(env, "Babylon").error("unit_order", unit="u1", order="hold")
    assert env.seats[0].notices_shown == 0 and env.seats[0].actions.summary()["invalid"] == 0


async def test_a_long_wait_returns_and_the_turn_is_not_lost(seats, monkeypatch):
    env, tools = seats
    monkeypatch.setattr(server, "SEAT_WAIT_SECONDS", 0.2)
    await settle(tools)
    waiting = await tools["Rome"]("end_turn", skip_idle=True)
    assert waiting.startswith("WAITING — you ended turn T0; Greece, Egypt are still playing it")
    await tools["Greece"]("end_turn", skip_idle=True)
    assert (await tools["Egypt"]("end_turn", skip_idle=True)).startswith("TURN T0 → T1")
    # Rome's next end_turn reports the turn it waited for instead of ending T1 unplayed.
    assert (await tools["Rome"]("end_turn")).startswith("TURN T0 → T1")
    assert (await tools["Rome"]("get_turn_brief")).startswith("T1/10 (3950 BC) · Rome")


async def test_a_game_with_one_seat_ignores_the_seat_header(env):
    assert (await SeatTools(env, "default-agent")("get_turn_brief")).startswith("T1/8 (3950 BC) · Rome · Despotism")
