"""An A2A agent that plays OpenCiv3 with Claude Code (`claude -p`) over the env's MCP tools; openciv3_player has the
game loop. One Claude Code process plays a session in stream-json mode. The key may be a LiteLLM key, an Anthropic API
key or a Claude Code OAuth token (`claude setup-token`); behind a LiteLLM proxy, models of other providers work too.
"""

from __future__ import annotations

import asyncio
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
    STREAM_LIMIT,
    SYSTEM_PROMPT,
    TOOL_TIMEOUT_SECONDS,
    Harness,
    PlayerConfig,
    Session,
    mcp_headers,
    play,
)


class ClaudeConfig(PlayerConfig):
    model: str = "sonnet"
    max_turns: int = 200
    effort: str | None = None


def model_id(environ: dict[str, str], model: str) -> str:
    """The model as the endpoint names it: a LiteLLM proxy may route `anthropic/claude-opus-5-5`, Anthropic's own API
    takes `claude-opus-5-5`, so a task can name its models one way for both."""
    base = environ.get("LITELLM_BASE_URL", "")
    return model.removeprefix("anthropic/") if not base or "api.anthropic.com" in base else model


def model_env(environ: dict[str, str], model: str) -> dict[str, str]:
    """Claude Code's settings for the endpoint agent-env passes (LITELLM_BASE_URL, LITELLM_API_KEY). Behind a proxy,
    Claude Code's background calls use the configured model too, since the proxy may not serve its default names, and
    another provider's model gets no Anthropic betas (LiteLLM rejects `context_management` for xai and gemini)."""
    key, base = environ.get("LITELLM_API_KEY", ""), environ.get("LITELLM_BASE_URL", "").rstrip("/")
    env = {}
    if key.startswith("sk-ant-oat"):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = key
    elif key.startswith("sk-ant-"):
        env["ANTHROPIC_API_KEY"] = key
    elif key:
        env["ANTHROPIC_AUTH_TOKEN"] = key
    if base and "api.anthropic.com" not in base:
        env["ANTHROPIC_BASE_URL"] = base.removesuffix("/v1")
        env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = env["ANTHROPIC_SMALL_FAST_MODEL"] = model
    if "/" in model and not model.startswith("anthropic/"):
        env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"
    return env


def mcp_config(servers: dict, seat: str | None = None) -> dict:
    def server(s: dict) -> dict:
        headers = mcp_headers(s, seat)
        return {"type": "http", "url": s["url"], **({"headers": headers} if headers else {})}
    return {"mcpServers": {name: server(s) for name, s in servers.items()}}


def claude_cmd(config: ClaudeConfig, mcp_file: Path, names: list[str]) -> list[str]:
    cmd = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
           "--mcp-config", str(mcp_file), "--strict-mcp-config", "--tools", "", "--setting-sources", "",
           "--system-prompt", config.system_prompt or SYSTEM_PROMPT,
           "--allowedTools", ",".join(f"mcp__{n}__*" for n in names), "--permission-mode", "dontAsk",
           "--model", config.model, "--max-turns", str(config.max_turns), "--no-session-persistence"]
    return cmd + (["--effort", config.effort] if config.effort else [])


def tool_text(content: object) -> str:
    return " ".join(x.get("text", "") for x in content if isinstance(x, dict)) if isinstance(content, list) \
        else str(content or "")


def tokens(result: dict) -> tuple[int, int]:
    """A session's input and output tokens. In stream-json input mode a result's `usage` covers only its last message;
    `modelUsage` adds up the whole session over every model."""
    models = (result.get("modelUsage") or {}).values()
    return sum(m.get("inputTokens") or 0 for m in models), sum(m.get("outputTokens") or 0 for m in models)


class ClaudeCode(Harness):
    name, code, trajectory = "Claude Code", "claude", "claude-code-stream-json/v1"

    def __init__(self, config: ClaudeConfig, servers: dict, seat: str | None, environ: dict[str, str],
                 workdir: Path):
        config = config.model_copy(update={"model": model_id(environ, config.model)})
        super().__init__(config, servers, seat, environ, workdir)
        mcp_file = workdir / "mcp.json"
        mcp_file.write_text(json.dumps(mcp_config(servers, seat)))
        self.cmd = claude_cmd(config, mcp_file, list(servers))
        self.env = {**environ, **model_env(environ, config.model), "MCP_TOOL_TIMEOUT": str(TOOL_TIMEOUT_SECONDS * 1000)}
        self.proc: asyncio.subprocess.Process | None = None

    async def reply(self, text: str, session: Session) -> bool:
        if self.proc is None:
            self.proc = await asyncio.create_subprocess_exec(
                *self.cmd, cwd=self.workdir, env=self.env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=STREAM_LIMIT)
        message = {"type": "user", "message": {"role": "user", "content": text}}
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        await self.proc.stdin.drain()
        async for line in self.proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            self.see(event, session)
            if event.get("type") == "result":
                return event.get("subtype") != "error_max_budget_usd"
        return False

    @staticmethod
    def see(event: dict, session: Session) -> None:
        session.events.append(event)
        kind = event.get("type")
        content = (event.get("message") or {}).get("content") or []
        if kind == "assistant":
            session.calls += sum(c.get("type") == "tool_use" for c in content)
            session.said("\n".join(c.get("text", "") for c in content if c.get("type") == "text"))
        elif kind == "user":
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    session.saw(tool_text(c.get("content")))
        elif kind == "result":
            session.finished = True
            session.input_tokens, session.output_tokens = tokens(event)
            session.cost_usd = event.get("total_cost_usd")
            session.error = (event.get("result") or session.reply or "Claude Code failed.") \
                if event.get("is_error") else None

    async def close(self, session: Session) -> None:
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()
            await asyncio.wait_for(self.proc.wait(), 10)
        except TimeoutError:
            pass
        finally:
            if self.proc.returncode is None:
                self.proc.kill()
                await self.proc.wait()
        session.returncode = self.proc.returncode


@a2a_agent(
    identity=AgentIdentity(
        name="openciv3-claude-player",
        description="Plays OpenCiv3 with Claude Code over the env's MCP tools, in sessions of a set number of turns.",
        version="1.1.0",
    ),
    config=ClaudeConfig,
    extensions=(MCP_CONFIG_V1, TRAJECTORY_V1),
)
class ClaudePlayer(AgentEnvAgent):
    async def run(self, request: TaskRequest[ClaudeConfig]) -> TaskResult:
        return await play(ClaudeCode, request)


if __name__ == "__main__":
    ClaudePlayer().serve()
