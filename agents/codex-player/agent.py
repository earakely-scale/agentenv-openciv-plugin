"""An A2A agent that plays OpenCiv3 with OpenAI's Codex CLI (`codex exec`) over the env's MCP tools; openciv3_player
has the game loop. Each message of a session is one `codex exec` run, resuming the session's thread. Codex reaches the
models through LiteLLM's OpenAI route (LITELLM_BASE_URL/openai/v1, LITELLM_API_KEY), or OpenAI's API when agent-env
names no endpoint. Its shell, image and browser tools are off, so the model plays through the game's tools only.
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

OFF = ("shell_tool", "unified_exec", "view_image", "image_generation", "browser_use", "computer_use", "apps",
       "plugins")
TOOL_ITEMS = {"mcp_tool_call", "command_execution", "file_change", "web_search"}


class CodexConfig(PlayerConfig):
    model: str = "gpt-5.6-sol"
    effort: str | None = None


def toml(value: str) -> str:
    return json.dumps(value)


def codex_config(environ: dict[str, str], servers: dict, seat: str | None) -> str:
    """CODEX_HOME/config.toml: the model endpoint and the env's MCP servers, whose calls may wait for other agents."""
    base = endpoint(environ)
    url = f"{base}/openai/v1" if base and "api.openai.com" not in base else "https://api.openai.com/v1"
    lines = ['model_provider = "agentenv"', 'web_search = "disabled"', "", "[model_providers.agentenv]",
             'name = "agent-env"', f"base_url = {toml(url)}", 'env_key = "LITELLM_API_KEY"', 'wire_api = "responses"']
    for name, s in servers.items():
        lines += ["", f"[mcp_servers.{toml(name)}]", f"url = {toml(s['url'])}", "required = true",
                  "startup_timeout_sec = 60", f"tool_timeout_sec = {TOOL_TIMEOUT_SECONDS}"]
        if headers := mcp_headers(s, seat):
            lines.append("http_headers = { " + ", ".join(f"{toml(k)} = {toml(v)}" for k, v in headers.items()) + " }")
    return "\n".join(lines) + "\n"


def codex_cmd(config: CodexConfig, thread: str | None) -> list[str]:
    cmd = ["codex", "exec", "--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
           "-c", f"model={toml(config.model.removeprefix('openai/'))}",
           "-c", f"developer_instructions={toml(config.system_prompt or SYSTEM_PROMPT)}",
           *(x for feature in OFF for x in ("--disable", feature))]
    if config.effort:
        cmd += ["-c", f"model_reasoning_effort={toml(config.effort)}"]
    return cmd + (["resume", thread, "-"] if thread else ["-"])


def tool_text(item: dict) -> str:
    content = (item.get("result") or {}).get("content") or []
    error = (item.get("error") or {}).get("message")
    return "\n".join([c.get("text", "") for c in content if isinstance(c, dict)] + ([error] if error else []))


class Codex(Harness):
    name, code, trajectory = "Codex", "codex", "codex-exec-json/v1"

    def __init__(self, config: CodexConfig, servers: dict, seat: str | None, environ: dict[str, str], workdir: Path):
        super().__init__(config, servers, seat, environ, workdir)
        home = workdir / "codex"
        home.mkdir(exist_ok=True)
        (home / "config.toml").write_text(codex_config(environ, servers, seat))
        self.env = {**environ, "CODEX_HOME": str(home)}
        self.thread: str | None = None

    async def reply(self, text: str, session: Session) -> bool:
        done = False

        def see(event: dict) -> None:
            nonlocal done
            session.events.append(event)
            kind = event.get("type")
            if kind == "thread.started":
                self.thread = event.get("thread_id") or self.thread
            elif kind == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message":
                    session.said(item.get("text", ""))
                elif item.get("type") in TOOL_ITEMS:
                    session.calls += 1
                if item.get("type") == "mcp_tool_call":
                    session.saw(tool_text(item))
            elif kind == "turn.completed":
                usage = event.get("usage") or {}
                session.input_tokens = usage.get("input_tokens", 0)
                session.output_tokens = usage.get("output_tokens", 0)
                done = session.finished = True
                session.error = None
            elif kind == "turn.failed":
                session.error = (event.get("error") or {}).get("message") or "Codex's turn failed."

        cmd = codex_cmd(self.config, self.thread)
        session.returncode, stderr = await stream(cmd, self.workdir, self.env, text, see)
        if not done and session.error is None:
            session.error = stderr or f"Codex exited ({session.returncode}) without finishing its turn."
        return done


@a2a_agent(
    identity=AgentIdentity(
        name="openciv3-codex-player",
        description="Plays OpenCiv3 with the Codex CLI over the env's MCP tools, in sessions of a set number of turns.",
        version="1.0.0",
    ),
    config=CodexConfig,
    extensions=(MCP_CONFIG_V1, TRAJECTORY_V1),
)
class CodexPlayer(AgentEnvAgent):
    async def run(self, request: TaskRequest[CodexConfig]) -> TaskResult:
        return await play(Codex, request)


if __name__ == "__main__":
    CodexPlayer().serve()
