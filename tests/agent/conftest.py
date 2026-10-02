"""The player agents (agents/<cli>-player/agent.py, with agents/common on the path) and fakes of their CLIs on PATH."""

import asyncio
import importlib.util
import os
import shlex
import sys
from pathlib import Path

import pytest
from agentenv_protocol.a2a_agent import TaskRequest, TextPart

AGENTS = Path(__file__).resolve().parents[2] / "agents"
sys.path.insert(0, str(AGENTS / "common"))


def load_agent(cli: str):
    spec = importlib.util.spec_from_file_location(f"{cli}_player", AGENTS / f"{cli}-player" / "agent.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_cli(monkeypatch, tmp_path):
    """Puts fake_<cli>.py on PATH as `cli`, with sessions of 10 turns; returns the file the fake keeps its game in."""
    def install(cli: str) -> Path:
        script = tmp_path / "bin" / cli
        script.parent.mkdir(exist_ok=True)
        fake = Path(__file__).with_name(f"fake_{cli}.py")
        script.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(fake))} "$@"\n')
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{script.parent}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("FAKE_GAME", str(tmp_path / "game.json"))
        monkeypatch.setenv("OPENCIV3_SESSION_TURNS", "10")
        monkeypatch.delenv("OPENCIV3_SEAT", raising=False)
        return tmp_path / "game.json"
    return install


def run_task(player, config, prompt: str, servers: dict | None = None):
    """One prompt_agent task for the agent class `player`, with its config."""
    request = TaskRequest(task_id="t", context_id="c", parts=(TextPart(text=prompt),), config=config,
                          mcp_servers=servers or {"openciv3": {"url": "http://env/mcp"}})
    return asyncio.run(player().run(request))
