"""The env as deployed: `python -m agentenv_openciv3.server` on a free port, driven over HTTP and MCP."""

import os
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
TOOLS = {"get_turn_brief", "list_units", "view_map", "find_city_sites", "unit_order", "city_info", "set_production",
         "research", "set_rates", "buy", "end_turn", "plan"}


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
