"""An A2A agent that plays OpenCiv3 with Claude Code (`claude -p`) over the env's MCP tools.

agent-env deploys it with `deploy_agent` and hands it the env's MCP server (urn:agentenv:mcp-config/v1); each
`prompt_agent` step is one task here, and one fresh Claude Code session. A prompt may name the turn its session
ends at ("... until turn 180 ..."): the agent nudges Claude to keep playing until the game reaches that turn, or
GAME OVER, so a long game can run as several bounded sessions. The model endpoint comes from agent-env as
LITELLM_BASE_URL and LITELLM_API_KEY; the key may be a LiteLLM key, an Anthropic API key or a Claude Code OAuth
token (`claude setup-token`).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path

from agentenv_protocol.a2a_agent import (
    MCP_CONFIG_V1,
    TRAJECTORY_V1,
    AgentConfig,
    AgentEnvAgent,
    AgentIdentity,
    TaskRequest,
    TaskResult,
    TextPart,
    Usage,
    a2a_agent,
)

SYSTEM_PROMPT = (
    "You play OpenCiv3, an open-source remake of Civilization III, through the tools of its MCP server. There are "
    "no other tools and no human to ask: never ask questions or wait for confirmation, and keep playing until the "
    "end_turn tool reports GAME OVER or the turn your session ends at. A failed call explains why, lists the valid "
    "choices and suggests a call that works."
)
NUDGE = "The game is at turn {turn}; this session ends at turn {stop}. Continue playing."
FOOTER = re.compile(r"\[T(\d+)/(\d+)")
STOP_AT = re.compile(r"\buntil turn (\d+)", re.IGNORECASE)


class PlayerConfig(AgentConfig):
    model: str = "sonnet"
    system_prompt: str | None = None
    max_turns: int = 200
    max_nudges: int = 30
    effort: str | None = None


def model_env(environ: dict[str, str]) -> dict[str, str]:
    """Claude Code's credential settings for the endpoint agent-env passes (LITELLM_BASE_URL, LITELLM_API_KEY)."""
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
    return env


def mcp_config(servers: dict) -> dict:
    return {"mcpServers": {name: {"type": "http", "url": s["url"], **({"headers": s["headers"]} if s.get("headers")
                                                                   else {})}
                           for name, s in servers.items()}}


def claude_cmd(config: PlayerConfig, mcp_file: Path, names: list[str]) -> list[str]:
    cmd = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
           "--mcp-config", str(mcp_file), "--strict-mcp-config", "--tools", "", "--setting-sources", "",
           "--system-prompt", config.system_prompt or SYSTEM_PROMPT,
           "--allowedTools", ",".join(f"mcp__{n}__*" for n in names), "--permission-mode", "dontAsk",
           "--model", config.model, "--max-turns", str(config.max_turns), "--no-session-persistence"]
    return cmd + (["--effort", config.effort] if config.effort else [])


def game_turn(event: dict) -> tuple[int, int, bool] | None:
    """(turn, turn_limit, game over) from the footer of the last tool result in a user event, if there is one."""
    found = None
    for c in (event.get("message") or {}).get("content") or []:
        if not isinstance(c, dict) or c.get("type") != "tool_result":
            continue
        content = c.get("content")
        text = " ".join(x.get("text", "") for x in content) if isinstance(content, list) else str(content or "")
        if m := FOOTER.findall(text):
            found = (int(m[-1][0]), int(m[-1][1]), "GAME OVER" in text)
        elif "GAME OVER" in text:
            found = (found or (0, 0, True))[:2] + (True,)
    return found


def next_message(turn: int | None, limit: int | None, over: bool, stop: int | None, nudges: int,
                 max_nudges: int) -> str | None:
    """The nudge to send after Claude ends a reply, or None when the session is done."""
    if over or nudges >= max_nudges or turn is None:
        return None
    target = min(x for x in (stop, limit) if x is not None) if (stop or limit) else None
    if target is None or turn >= target:
        return None
    return NUDGE.format(turn=turn, stop=target)


@a2a_agent(
    identity=AgentIdentity(
        name="openciv3-claude-player",
        description="Plays OpenCiv3 with Claude Code over the env's MCP tools, one session per prompt.",
        version="1.0.0",
    ),
    config=PlayerConfig,
    extensions=(MCP_CONFIG_V1, TRAJECTORY_V1),
)
class ClaudePlayer(AgentEnvAgent):
    async def run(self, request: TaskRequest[PlayerConfig]) -> TaskResult:
        config = request.config
        prompt = "\n".join(p.text for p in request.parts if isinstance(p, TextPart))
        if not request.mcp_servers:
            return TaskResult.failure("no_mcp_server", "No MCP server was configured for this agent.")
        stop = int(m.group(1)) if (m := STOP_AT.search(prompt)) else None
        with tempfile.TemporaryDirectory(prefix="claude-player-") as tmp:
            mcp_file = Path(tmp) / "mcp.json"
            mcp_file.write_text(json.dumps(mcp_config(dict(request.mcp_servers))))
            proc = await asyncio.create_subprocess_exec(
                *claude_cmd(config, mcp_file, list(request.mcp_servers)), cwd=tmp,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                env={**os.environ, **model_env(dict(os.environ))}, limit=64 * 1024 * 1024)
            try:
                return await asyncio.wait_for(self._play(proc, prompt, stop, config), config.timeout_seconds)
            except TimeoutError:
                return TaskResult.failure("timeout", f"The session ran past its {config.timeout_seconds} s limit.")
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()

    async def _play(self, proc, prompt: str, stop: int | None, config: PlayerConfig) -> TaskResult:
        async def send(text: str) -> None:
            message = {"type": "user", "message": {"role": "user", "content": text}}
            proc.stdin.write((json.dumps(message) + "\n").encode())
            await proc.stdin.drain()

        events, turn, limit, over, nudges, calls, reply, result = [], None, None, False, 0, 0, "", {}
        await send(prompt)
        async for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            events.append(event)
            kind = event.get("type")
            if kind == "assistant":
                content = (event.get("message") or {}).get("content") or []
                calls += sum(c.get("type") == "tool_use" for c in content)
                reply = "\n".join(c.get("text", "") for c in content if c.get("type") == "text") or reply
            elif kind == "user" and (seen := game_turn(event)):
                turn, limit = seen[0] or turn, seen[1] or limit
                over = over or seen[2]
            elif kind == "result":
                result = event
                if event.get("subtype") == "error_max_budget_usd":
                    break
                message = next_message(turn, limit, over, stop, nudges, config.max_nudges)
                if message is None:
                    break
                nudges += 1
                await send(message)
        proc.stdin.close()
        await proc.wait()
        if not result:
            return TaskResult.failure("claude_failed", f"Claude Code exited ({proc.returncode}) without a result.")
        usage = result.get("usage") or {}
        where = "GAME OVER" if over else f"turn {turn}/{limit}" if turn is not None else "no turn seen"
        cost = result.get("total_cost_usd") or 0
        summary = f"Session ended at {where} after {calls} tool calls and {nudges} nudges (${cost:.2f})."
        return (TaskResult.builder().succeeded().add_text(f"{reply.strip()}\n\n{summary}".strip())
                .usage(Usage(tool_call_count=calls, input_tokens=usage.get("input_tokens"),
                             output_tokens=usage.get("output_tokens"), cost_usd=result.get("total_cost_usd")))
                .native_trajectory(format="claude-code-stream-json/v1", payload=events)
                .build())


if __name__ == "__main__":
    ClaudePlayer().serve()
