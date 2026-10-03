"""The env as deployed: `python -m agentenv_openciv3.server` on a free port, driven over HTTP and MCP."""

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import anyio
import httpx
import pytest
from agentenv_protocol import DataPart, client
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

pytestmark = pytest.mark.anyio

SRC = Path(__file__).resolve().parents[2] / "src"
REAL_CMD = os.environ.get("CIVBRIDGE_CMD", str(SRC.parent / "build" / "bridge" / "CivBridge"))
TOOLS = {"get_turn_brief", "list_units", "view_map", "find_city_sites", "unit_order", "city_info", "set_production",
         "research", "set_rates", "buy", "revolution", "diplomacy", "end_turn", "plan"}
END_TURN = ("end_turn", {"skip_idle": True})


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def serve(env_vars: dict, **overrides: str):
    port = free_port()
    env = {**os.environ, **env_vars, **overrides, "MCP_HOST": "127.0.0.1", "MCP_PORT": str(port),
           "PYTHONPATH": os.pathsep.join([str(SRC), os.environ.get("PYTHONPATH", "")])}
    proc = subprocess.Popen([sys.executable, "-m", "agentenv_openciv3.server"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    with anyio.fail_after(20):
        while True:
            try:
                await client.get_card(base)
                return proc, base
            except httpx.HTTPError:
                assert proc.poll() is None, "server exited"
                await anyio.sleep(0.1)


@pytest.fixture
async def server(env_vars):
    proc, base = await serve(env_vars)
    yield base
    proc.terminate()
    proc.wait(10)


async def test_card(server):
    card = await client.get_card(server)
    assert card["name"] == "openciv3"
    assert {t["name"] for t in card["capabilities"]["tools"]} == TOOLS
    assert card["capabilities"]["operations"] == ["data/reset", "data/add", "data/get"]
    ext = {e["uri"]: e for e in card["capabilities"]["extensions"]}
    assert set(ext) == {"urn:openciv3:new-game/v1", "urn:openciv3:autoplay/v1", "urn:openciv3:recording/v1"}
    assert ext["urn:openciv3:autoplay/v1"]["params"]["methods"]["autoplay"]["request"]["properties"]["policy"]["enum"] \
        == ["null", "found_capital", "engine_ai", "settler_bot"]
    recording = ext["urn:openciv3:recording/v1"]["params"]["methods"]["recording"]["request"]["properties"]
    assert recording["view"]["enum"] == ["spectator", "agent"] and recording["fps"]["default"] == 4
    assert client.mcp_path(card) == "/mcp"
    unit_order = client.find_tool(card, "unit_order")
    assert unit_order["inputSchema"]["required"] == ["unit", "order"]


async def test_mcp_tools(server):
    async with streamable_http_client(f"{server}/mcp") as (read, write, _), ClientSession(read, write) as session:
        await session.initialize()
        listed = await session.list_tools()
        assert {t.name for t in listed.tools} == TOOLS
        brief = await session.call_tool("get_turn_brief", {})
        assert not brief.isError and brief.content[0].text.startswith("T1/8 · Rome")
        assert brief.structuredContent is None
        ok = await session.call_tool("unit_order", {"unit": "u1", "order": "found_city"})
        assert not ok.isError and ok.content[0].text.startswith("Founded Rome")
        bad = await session.call_tool("unit_order", {"unit": "u3", "order": "found_city"})
        assert bad.isError
        assert 'Do this: unit_order(unit="u3", order="settle", x=16, y=12)' in bad.content[0].text
        blocked = await session.call_tool("end_turn", {})
        assert blocked.isError and "END TURN BLOCKED" in blocked.content[0].text
        invalid = await session.call_tool("end_turn", {"max_turns": 50})
        assert invalid.isError


async def test_data_plane_and_extensions(server):
    await client.reset_data(server)
    [part] = (await client.get_data(server)).parts
    assert (part.data["seed"], part.data["turn"], part.data["turn_limit"]) == (3, 1, 8)
    await client.add_data(server, [DataPart(data={"scenario": {"seed": 11, "turn_limit": 15}})])
    [part] = (await client.get_data(server)).parts
    assert (part.data["seed"], part.data["turn_limit"]) == (11, 15)
    with pytest.raises(RuntimeError, match="unknown scenario keys"):
        await client.add_data(server, [DataPart(data={"scenario": {"map": "big"}})])

    card = await client.get_card(server)
    game = await client.invoke_extension(server, card, "urn:openciv3:new-game/v1", {"seed": 21, "turn_limit": 10})
    assert game["seed"] == 21 and game["scenario"]["seed"] == 21
    res = await client.invoke_extension(server, card, "urn:openciv3:autoplay/v1", {"turns": 4, "policy": "null"})
    assert res["turn"] == 5 and len(res["trajectory"]) == 4
    [part] = (await client.get_data(server)).parts
    assert (part.data["seed"], part.data["turn"]) == (21, 5)
    assert part.data["harness"] == {"autoplay_turns": 4, "new_games": 3, "extension_calls": 2, "engine_restarts": 0}
    args = {"formats": ["png"], "view": "agent"}
    rec = await client.invoke_extension(server, card, "urn:openciv3:recording/v1", args)
    assert rec["turns"] == 5 and [f["name"] for f in rec["files"]] == ["openciv3-seed21-agent.png"]
    async with httpx.AsyncClient() as http:
        r = await http.post(f"{server}/agentenv/ext/autoplay", json={"turns": 1, "policy": "random"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_params"


async def test_binds_before_the_bridge_starts(env_vars):
    proc, base = await serve(env_vars, CIVBRIDGE_CMD="/nonexistent/CivBridge")
    try:
        assert (await client.get_card(base))["name"] == "openciv3"
        async with streamable_http_client(f"{base}/mcp") as (read, write, _), ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool("get_turn_brief", {})
            assert res.isError and "could not start the game engine" in res.content[0].text
    finally:
        proc.terminate()
        proc.wait(10)


async def test_the_seat_header_over_http(env_vars):
    proc, base = await serve(env_vars, CIVBRIDGE_CMD=REAL_CMD)
    try:
        card = await client.get_card(base)
        game = await client.invoke_extension(base, card, "urn:openciv3:new-game/v1",
                                             {"seed": 1, "opponents": 2, "seats": ["Greece", "Egypt"]})
        assert [s["civ"] for s in game["seats"]] == ["Rome", "Greece", "Egypt"]
        headers = {"X-OpenCiv3-Seat": "Egypt"}
        async with httpx.AsyncClient(headers=headers) as http, \
                streamable_http_client(f"{base}/mcp", http_client=http) as (read, write, _), \
                ClientSession(read, write) as session:
            await session.initialize()
            brief = await session.call_tool("get_turn_brief", {})
            assert not brief.isError and brief.content[0].text.startswith("T0/8 · Egypt")
    finally:
        proc.terminate()
        proc.wait(10)


async def play(base: str, civ: str, *calls: tuple[str, dict]) -> None:
    """The agent playing `civ` makes these tool calls."""
    async with httpx.AsyncClient(headers={"X-OpenCiv3-Seat": civ}, timeout=60) as http, \
            streamable_http_client(f"{base}/mcp", http_client=http) as (read, write, _), \
            ClientSession(read, write) as session:
        await session.initialize()
        for name, args in calls:
            assert not (await session.call_tool(name, args)).isError


async def test_the_live_view_follows_the_game(env_vars):
    proc, base = await serve(env_vars, CIVBRIDGE_CMD=REAL_CMD)
    try:
        async with httpx.AsyncClient(base_url=base) as http:
            page = await http.get("/live")
            assert page.headers["content-type"].startswith("text/html") and "window.OPENCIV_DATA = null;" in page.text
            empty = (await http.get("/live/data.json")).json()
            assert (empty["game"], empty["turns"], empty["live"]["turn"]) == (None, [], None)
            assert (await http.get("/live/state.json")).json() == {
                "game": None, "turn": None, "turn_limit": None, "game_over": False, "victory": None, "players": [],
                "events": [], "actions": [], "client": False, "recording": True}
            assert (await http.get("/live/frame.png")).status_code == 404

            await client.invoke_extension(base, await client.get_card(base), "urn:openciv3:new-game/v1", {
                "seed": 1, "opponents": 2, "seats": ["Greece", "Egypt"],
                "labels": {"Rome": "A", "Greece": "B", "Egypt": "C"}})
            state = (await http.get("/live/state.json")).json()
            assert (state["turn"], state["turn_limit"], state["game_over"], state["victory"]) == (0, 8, False, None)
            assert sorted((p["civ"], p["label"]) for p in state["players"] if p["is_agent"]) == [
                ("Egypt", "C"), ("Greece", "B"), ("Rome", "A")]
            for view in ("spectator", "agent"):
                frame = await http.get("/live/frame.png", params={"turn": 0, "view": view})
                assert frame.headers["content-type"] == "image/png" and frame.content.startswith(b"\x89PNG")
            assert (await http.get("/live/frame.png", params={"turn": 1})).status_code == 404
            assert (await http.get("/live/frame.png", params={"view": "god"})).status_code == 400
            assert (await http.get("/live/client.png")).status_code == 404
            doc = (await http.get("/live/data.json")).json()
            assert [t["turn"] for t in doc["turns"]] == [0] and doc["static"]["tiles"]
            assert [(p["civ"], p["label"], p["seat"]) for p in doc["players"] if p["seat"] is not None] == [
                ("Rome", "A", 0), ("Greece", "B", 1), ("Egypt", "C", 2)]

            async with anyio.create_task_group() as tg:
                tg.start_soon(play, base, "Greece", ("unit_order", {"unit": "u1", "order": "found_city"}), END_TURN)
                with anyio.fail_after(20):
                    while True:
                        now = (await http.get("/live/data.json", params={"since": 0})).json()
                        if any(s["ended"] for s in now["live"]["seats"]):
                            break
                        await anyio.sleep(0.1)
                assert "static" not in now and now["turns"] == [] and now["live"]["turn"] == 0
                seats = {s["civ"]: s for s in now["live"]["seats"]}
                assert seats["Greece"]["ended"] and not seats["Rome"]["ended"]
                assert seats["Greece"]["calls"] == {"ok": 1, "failed": 0}    # end_turn counts once it returns
                assert seats["Rome"]["calls"] == {"ok": 0, "failed": 0}
                assert seats["Greece"]["actions"] == [{"text": "u1 found_city", "ok": True}]
                assert (await http.get("/live/data.json", params={"since": "x"})).status_code == 400
                for civ in ("Rome", "Egypt"):
                    tg.start_soon(play, base, civ, ("unit_order", {"unit": "u1", "order": "found_city"}), END_TURN)
            after = (await http.get("/live/data.json", params={"since": 0})).json()
            [turn1] = after["turns"]
            assert turn1["turn"] == 1 and after["live"]["turn"] == 1
            index = {p["civ"]: str(p["index"]) for p in doc["players"]}
            assert {civ: turn1["actions"][index[civ]] for civ in ("Rome", "Greece", "Egypt")} == {
                civ: [{"text": "u1 found_city", "ok": True}] for civ in ("Rome", "Greece", "Egypt")}
            assert turn1["calls"][index["Greece"]] == {"ok": 2, "failed": 0}
            assert any(e["kind"] == "city_founded" for e in turn1["events"])
            assert not any(s["ended"] for s in after["live"]["seats"])
            state = (await http.get("/live/state.json")).json()
            assert state["turn"] == 1
            assert sorted(a["text"] for a in state["actions"]) == ["A: u1 found_city", "B: u1 found_city",
                                                                   "C: u1 found_city"]
            assert {p["civ"]: p["score"]["cities"] for p in state["players"] if p["is_agent"]} == {
                "Rome": 1, "Greece": 1, "Egypt": 1}
            latest = await http.get("/live/frame.png")
            assert latest.content == (await http.get("/live/frame.png", params={"turn": 1})).content

            await client.invoke_extension(base, await client.get_card(base), "urn:openciv3:new-game/v1", {
                "seed": 1, "opponents": 1, "seats": ["Greece"], "labels": {"Rome": "A", "Greece": "B"}})
            await play(base, "Greece", *(("unit_order", {"unit": u, "order": "disband"}) for u in ("u2", "u1")))
            await play(base, "Rome", END_TURN)
            won = (await http.get("/live/state.json")).json()
            assert won["victory"] == {"kind": "conquest", "civ": "Rome", "label": "A", "turn": 1} and won["game_over"]
            data = (await http.get("/live/data.json")).json()
            assert data["game"] != doc["game"] and data["live"]["game_over"]
            assert data["live"]["victory"] == won["victory"] and data["meta"]["victory"] == won["victory"]
            assert won["game"] != state["game"] and [(p["label"], p["defeated"]) for p in won["players"]] == [
                ("A", False), ("B", True)]
            assert (await http.get("/live/frame.png")).content.startswith(b"\x89PNG")
    finally:
        proc.terminate()
        proc.wait(10)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
async def test_the_live_client_view_draws_the_newest_save(fake_client, env_vars):
    proc, base = await serve(env_vars, OPENCIV_CLIENT=str(fake_client))
    try:
        await client.reset_data(base)
        async with httpx.AsyncClient(base_url=base) as http:
            assert (await http.get("/live/state.json")).json()["client"] is True
            first = await http.get("/live/client.png", params={"turn": 1})
            assert first.status_code == 503 and first.headers["retry-after"] == "2"
            with anyio.fail_after(10):
                while (shown := await http.get("/live/client.png", params={"turn": 1})).status_code == 503:
                    await anyio.sleep(0.1)
            assert shown.headers["x-openciv3-turn"] == "1" and shown.content.startswith(b"\x89PNG")

            await client.invoke_extension(base, await client.get_card(base), "urn:openciv3:autoplay/v1", {"turns": 3})
            turns = []
            with anyio.fail_after(10):
                while not turns or turns[-1] != "4":
                    turns.append((await http.get("/live/client.png", params={"turn": 4})).headers["x-openciv3-turn"])
                    await anyio.sleep(0.1)
        assert turns[0] == "1" and set(turns) == {"1", "4"}
    finally:
        proc.terminate()
        proc.wait(10)
