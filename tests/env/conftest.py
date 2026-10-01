import json
import shlex
import sys
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agentenv_openciv3.server import OpenCiv3Env  # noqa: E402

FAKE = Path(__file__).with_name("fake_bridge.py")
FAKE_CMD = f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE))}"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def fake_cmd():
    return shlex.split(FAKE_CMD)


@pytest.fixture
def env_vars(monkeypatch, tmp_path):
    """The env's configuration: the fake bridge, no baselines, an action log in tmp_path."""
    values = {"CIVBRIDGE_CMD": FAKE_CMD, "OPENCIV_BASELINES": "0", "OPENCIV_SEED": "3", "OPENCIV_TURN_LIMIT": "8",
              "OPENCIV_ACTION_LOG": str(tmp_path / "actions.jsonl")}
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    for k in ("ENVIRONMENT_NAME", "OPENCIV_SIZE", "OPENCIV_OPPONENTS", "OPENCIV_DIFFICULTY", "OPENCIV_BARBARIANS"):
        monkeypatch.delenv(k, raising=False)
    return values


@pytest.fixture
async def env(env_vars):
    e = OpenCiv3Env()
    e.create_app()
    yield e
    await e.close()


class Tools:
    """Calls tools through FastMCP, as an MCP client would: argument validation and error wrapping included."""

    def __init__(self, env):
        self.env = env

    async def __call__(self, name: str, **args) -> str:
        return (await self.env.mcp.call_tool(name, args))[0].text

    async def error(self, name: str, **args) -> str:
        with pytest.raises(ToolError) as info:
            await self.env.mcp.call_tool(name, args)
        return str(info.value)


@pytest.fixture
def tools(env):
    return Tools(env)


@pytest.fixture
def action_log(env_vars):
    """Reads the action log back as a list of rows."""
    path = Path(env_vars["OPENCIV_ACTION_LOG"])
    return lambda: [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
