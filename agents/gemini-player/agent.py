"""An A2A agent that plays OpenCiv3 with Google's Gemini CLI (`gemini -p`) over the env's MCP tools; openciv3_player
has the game loop. Each message of a session is one `gemini -p` run, resuming the session. Gemini CLI reaches the models
through LiteLLM's Gemini route (LITELLM_BASE_URL/gemini, LITELLM_API_KEY as the Gemini API key), or Google's API when
agent-env names no endpoint. Its built-in tools and subagents are off, so the model plays through the game's tools only;
the system prompt is the run's GEMINI.md, which Gemini CLI adds to its own.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentenv_protocol.a2a_agent import (
    MCP_CONFIG_V1,
    TRAJECTORY_V1,
    AgentEnvAgent,
    AgentIdentity,
    TaskRequest,
    TaskResult,
    a2a_agent,
)
from openciv3_player import (
    SYSTEM_PROMPT,
    TOOL_TIMEOUT_SECONDS,
    Harness,
    PlayerConfig,
    Session,
    endpoint,
    mcp_headers,
    play,
    stream,
)

SUBAGENTS = ["activate_skill", "cli_help", "codebase_investigator", "generalist"]


class GeminiConfig(PlayerConfig):
    model: str = "gemini-3.1-pro-preview"


def gemini_settings(servers: dict, seat: str | None) -> dict:
    """~/.gemini/settings.json: the env's MCP servers, whose calls may wait for other agents, and no other tools.
    Loop detection is off: a game repeats the same calls turn after turn."""
    def server(s: dict) -> dict:
        headers = mcp_headers(s, seat)
        return {"httpUrl": s["url"], "timeout": TOOL_TIMEOUT_SECONDS * 1000, "trust": True,
                **({"headers": headers} if headers else {})}
    return {"mcpServers": {name: server(s) for name, s in servers.items()},
            "tools": {"core": [], "exclude": SUBAGENTS, "useRipgrep": False},
            "model": {"disableLoopDetection": True},
            "security": {"auth": {"selectedType": "gemini-api-key"}},
            "privacy": {"usageStatisticsEnabled": False},
            "general": {"enableAutoUpdate": False, "enableAutoUpdateNotification": False}}


def gemini_env(environ: dict[str, str]) -> dict[str, str]:
    base = endpoint(environ)
    env = {"GEMINI_API_KEY": environ.get("LITELLM_API_KEY", "")}
    if base and "googleapis.com" not in base:
        env["GOOGLE_GEMINI_BASE_URL"] = f"{base}/gemini"
    return env


def gemini_cmd(model: str, text: str, session_id: str | None) -> list[str]:
    cmd = ["gemini", "-p", text, "--output-format", "stream-json", "--approval-mode", "yolo",
           "--model", model.removeprefix("gemini/")]
    return cmd + (["--resume", session_id] if session_id else [])


def tool_text(event: dict) -> str:
    return "\n".join(filter(None, [event.get("output"), (event.get("error") or {}).get("message")]))


class GeminiCli(Harness):
    name, code, trajectory = "Gemini CLI", "gemini", "gemini-cli-stream-json/v1"

    def __init__(self, config: GeminiConfig, servers: dict, seat: str | None, environ: dict[str, str],
                 workdir: Path):
        super().__init__(config, servers, seat, environ, workdir)
        home, self.game = workdir / "home", workdir / "game"
        (home / ".gemini").mkdir(parents=True, exist_ok=True)
        self.game.mkdir(exist_ok=True)
        (home / ".gemini" / "settings.json").write_text(json.dumps(gemini_settings(servers, seat), indent=1))
        (self.game / "GEMINI.md").write_text(config.system_prompt or SYSTEM_PROMPT)
        self.env = {**environ, **gemini_env(environ), "HOME": str(home)}
        self.session_id: str | None = None

    async def reply(self, text: str, session: Session) -> bool:
        done, said, failed = False, "", None

        def see(event: dict) -> None:
            nonlocal done, said, failed
            session.events.append(event)
            kind = event.get("type")
            if kind == "init":
                self.session_id = event.get("session_id") or self.session_id
            elif kind == "message" and event.get("role") == "assistant":
                said = said + event.get("content", "") if event.get("delta") else event.get("content", "")
                session.said(said)
            elif kind == "tool_use":
                session.calls += 1
                said = ""
            elif kind == "tool_result":
                session.saw(tool_text(event))
            elif kind == "error":
                failed = event.get("message") or failed
            elif kind == "result":
                stats = event.get("stats") or {}
                session.input_tokens += stats.get("input_tokens", 0)
                session.output_tokens += stats.get("output_tokens", 0)
                if event.get("status") == "success":
                    done = session.finished = True
                    session.error = None
                else:
                    failed = (event.get("error") or {}).get("message") or failed

        cmd = gemini_cmd(self.config.model, text, self.session_id)
        session.returncode, stderr = await stream(cmd, self.game, self.env, None, see)
        if not done:
            session.error = failed or stderr or f"Gemini CLI exited ({session.returncode}) without a result."
        return done


@a2a_agent(
    identity=AgentIdentity(
        name="openciv3-gemini-player",
        description="Plays OpenCiv3 with Gemini CLI over the env's MCP tools, in sessions of a set number of turns.",
        version="1.0.0",
    ),
    config=GeminiConfig,
    extensions=(MCP_CONFIG_V1, TRAJECTORY_V1),
)
class GeminiPlayer(AgentEnvAgent):
    async def run(self, request: TaskRequest[GeminiConfig]) -> TaskResult:
        return await play(GeminiCli, request)


if __name__ == "__main__":
    GeminiPlayer().serve()
