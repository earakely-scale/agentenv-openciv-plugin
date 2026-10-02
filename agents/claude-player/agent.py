"""An A2A agent that plays OpenCiv3 with Claude Code (`claude -p`) over the env's MCP tools.

agent-env deploys it with `deploy_agent` and hands it the env's MCP server (urn:agentenv:mcp-config/v1); each
`prompt_agent` step is one task here. A prompt that names the turn to stop at ("... until turn 180 ...") is one fresh
Claude Code session, nudged to keep playing until the game reaches that turn or GAME OVER, so a task can split a long
game into steps. Any other prompt plays the whole game in fresh sessions of OPENCIV3_SESSION_TURNS turns (75; 0 for a
single session), which bounds Claude's context: each session picks the game up from the brief and the plan tool. The
model endpoint comes from agent-env as LITELLM_BASE_URL and LITELLM_API_KEY; the key may be a LiteLLM key, an Anthropic
API key or a Claude Code OAuth token (`claude setup-token`). In a game with several agents, the X-OpenCiv3-Seat header
tells the env which civilization this agent plays: OPENCIV3_SEAT, or else the name deploy_agent gave the agent. end_turn
may wait minutes for the others, so tool calls get a longer timeout.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from functools import partial
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
SESSIONS = (
    "A game longer than {turns} turns is played in sessions of {turns} turns. Each session starts fresh and knows only "
    "the game and your plan (the plan tool), so keep the plan current. When this session has played its {turns} turns, "
    "update your plan and reply with one line on where the game stands."
)
RESUME = ("This game is under way: an earlier session played it up to turn {turn}. Start with the get_turn_brief tool "
          "and read your plan in it.")
SESSION_TURNS = 75
FOOTER = re.compile(r"\[(?:GAME OVER )?T(\d+)/(\d+)")
SEAT_HEADER = "X-OpenCiv3-Seat"
TOOL_TIMEOUT_MS = "1800000"
STOP_AT = re.compile(r"\buntil turn (\d+)", re.IGNORECASE)


class PlayerConfig(AgentConfig):
    model: str = "sonnet"
    system_prompt: str | None = None
    max_turns: int = 200
    max_nudges: int = 30
    effort: str | None = None


def model_id(environ: dict[str, str], model: str) -> str:
    """The model as the endpoint names it: a LiteLLM proxy may route `anthropic/claude-opus-5-5`, Anthropic's own API
    takes `claude-opus-5-5`, so a task can name its models one way for both."""
    base = environ.get("LITELLM_BASE_URL", "")
    return model.removeprefix("anthropic/") if not base or "api.anthropic.com" in base else model


def model_env(environ: dict[str, str], model: str) -> dict[str, str]:
    """Claude Code's settings for the endpoint agent-env passes (LITELLM_BASE_URL, LITELLM_API_KEY). Behind a proxy,
    Claude Code's background calls use the configured model too, since the proxy may not serve its default names."""
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
    return env


def seat_name(environ: dict[str, str], config: AgentConfig) -> str | None:
    """The seat this agent plays: OPENCIV3_SEAT, or else the name deploy_agent gave it, which the env maps to a seat."""
    return environ.get("OPENCIV3_SEAT") or config.name


def mcp_config(servers: dict, seat: str | None = None) -> dict:
    def server(s: dict) -> dict:
        headers = {**(s.get("headers") or {}), **({SEAT_HEADER: seat} if seat else {})}
        return {"type": "http", "url": s["url"], **({"headers": headers} if headers else {})}
    return {"mcpServers": {name: server(s) for name, s in servers.items()}}


def claude_cmd(config: PlayerConfig, mcp_file: Path, names: list[str]) -> list[str]:
    cmd = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
           "--mcp-config", str(mcp_file), "--strict-mcp-config", "--tools", "", "--setting-sources", "",
           "--system-prompt", config.system_prompt or SYSTEM_PROMPT,
           "--allowedTools", ",".join(f"mcp__{n}__*" for n in names), "--permission-mode", "dontAsk",
           "--model", config.model, "--max-turns", str(config.max_turns), "--no-session-persistence"]
    return cmd + (["--effort", config.effort] if config.effort else [])


def game_turn(event: dict) -> tuple[int | None, int | None, bool] | None:
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
            found = (found or (None, None, True))[:2] + (True,)
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


@dataclass
class Session:
    """One Claude Code process's play. It ends at `stop` when the prompt names that turn, else `turns` after the turn
    it started at, else at the game's end."""

    stop: int | None = None
    turns: int = 0
    start: int | None = None
    turn: int | None = None
    limit: int | None = None
    over: bool = False
    calls: int = 0
    nudges: int = 0
    reply: str = ""
    result: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    returncode: int | None = None

    @property
    def end(self) -> int | None:
        return self.start + self.turns if self.turns and self.start is not None else self.stop

    def see(self, event: dict) -> None:
        self.events.append(event)
        kind = event.get("type")
        if kind == "assistant":
            content = (event.get("message") or {}).get("content") or []
            self.calls += sum(c.get("type") == "tool_use" for c in content)
            self.reply = "\n".join(c.get("text", "") for c in content if c.get("type") == "text") or self.reply
        elif kind == "user" and (seen := game_turn(event)):
            turn, limit, over = seen
            if turn is not None:
                self.turn, self.limit = turn, limit
                self.start = turn if self.start is None else self.start
            self.over = self.over or over
        elif kind == "result":
            self.result = event

    def following(self) -> Session:
        """The fresh session that takes the game on from where this one left it."""
        return Session(turns=self.turns, start=self.turn, turn=self.turn, limit=self.limit)


def session_plan(prompt: str, environ: dict[str, str]) -> Session:
    """The first session: to the turn the prompt names, else the first of the sessions that play the whole game."""
    if m := STOP_AT.search(prompt):
        return Session(stop=int(m.group(1)))
    return Session(turns=int(environ.get("OPENCIV3_SESSION_TURNS") or SESSION_TURNS))


def session_message(prompt: str, session: Session) -> str:
    """A session's first message: the prompt and, in a game played in sessions, how sessions work, after where the
    game stands when an earlier session has played it."""
    if not session.turns:
        return prompt
    resume = [] if session.start is None else [RESUME.format(turn=session.start)]
    return "\n\n".join([*resume, prompt, SESSIONS.format(turns=session.turns)])


def rotate(sessions: list[Session]) -> bool:
    """Whether a fresh session plays on: the game is played in sessions and not over, the last session made calls, and
    the turn moved in one of the last two sessions."""
    last = sessions[-1]
    if not last.turns or last.over or not last.calls or last.turn is None:
        return False
    return len(sessions) < 2 or last.turn != sessions[-2].start


def played(sessions: list[Session]) -> TaskResult:
    """The task's result: the last reply and what the sessions played, their usage summed and their events in order."""
    first, last = sessions[0], sessions[-1]
    if not first.result and len(sessions) == 1:
        return TaskResult.failure("claude_failed", f"Claude Code exited ({first.returncode}) without a result.")
    if first.result.get("is_error") and first.calls == 0:
        message = first.result.get("result") or first.reply or "Claude Code failed."
        return TaskResult.failure("claude_error", message.strip())
    usage = [s.result.get("usage") or {} for s in sessions]
    calls, nudges = sum(s.calls for s in sessions), sum(s.nudges for s in sessions)
    cost = sum(s.result.get("total_cost_usd") or 0 for s in sessions)
    reply = next((s.reply for s in reversed(sessions) if s.reply), "")
    if not first.turns:
        where = "GAME OVER" if last.over else "no turn seen" if last.turn is None else f"turn {last.turn}/{last.limit}"
        summary = f"Session ended at {where} after {calls} tool calls and {nudges} nudges (${cost:.2f})."
        events = last.events
    else:
        span = "no turn" if last.turn is None else f"T{first.start} to T{last.turn}" + (
            " (GAME OVER)" if last.over else f" of {last.limit}")
        count = f"{len(sessions)} session" + "s" * (len(sessions) > 1)
        summary = f"Played {span} in {count} after {calls:,} tool calls and {nudges:,} nudges (${cost:,.2f})."
        events = [e for k, s in enumerate(sessions, 1)
                  for e in ({"type": "session", "index": k, "start_turn": s.start}, *s.events)]
    return (TaskResult.builder().succeeded().add_text(f"{reply.strip()}\n\n{summary}".strip())
            .usage(Usage(tool_call_count=calls, input_tokens=sum(u.get("input_tokens") or 0 for u in usage),
                         output_tokens=sum(u.get("output_tokens") or 0 for u in usage), cost_usd=cost))
            .native_trajectory(format="claude-code-stream-json/v1", payload=events)
            .build())


@a2a_agent(
    identity=AgentIdentity(
        name="openciv3-claude-player",
        description="Plays OpenCiv3 with Claude Code over the env's MCP tools, in sessions of a set number of turns.",
        version="1.0.0",
    ),
    config=PlayerConfig,
    extensions=(MCP_CONFIG_V1, TRAJECTORY_V1),
)
class ClaudePlayer(AgentEnvAgent):
    async def run(self, request: TaskRequest[PlayerConfig]) -> TaskResult:
        environ = dict(os.environ)
        config = request.config.model_copy(update={"model": model_id(environ, request.config.model)})
        prompt = "\n".join(p.text for p in request.parts if isinstance(p, TextPart))
        if not request.mcp_servers:
            return TaskResult.failure("no_mcp_server", "No MCP server was configured for this agent.")
        with tempfile.TemporaryDirectory(prefix="claude-player-") as tmp:
            mcp_file = Path(tmp) / "mcp.json"
            mcp_file.write_text(json.dumps(mcp_config(dict(request.mcp_servers), seat_name(environ, config))))
            spawn = partial(
                asyncio.create_subprocess_exec, *claude_cmd(config, mcp_file, list(request.mcp_servers)), cwd=tmp,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                env={**environ, **model_env(environ, config.model), "MCP_TOOL_TIMEOUT": TOOL_TIMEOUT_MS},
                limit=64 * 1024 * 1024)
            try:
                sessions = await asyncio.wait_for(
                    self._play(spawn, prompt, session_plan(prompt, environ), config.max_nudges),
                    config.timeout_seconds)
            except TimeoutError:
                return TaskResult.failure("timeout", f"The session ran past its {config.timeout_seconds} s limit.")
        return played(sessions)

    async def _play(self, spawn, prompt: str, session: Session, max_nudges: int) -> list[Session]:
        sessions = [await self._session(spawn, prompt, session, max_nudges)]
        while rotate(sessions):
            sessions.append(await self._session(spawn, prompt, sessions[-1].following(), max_nudges))
        return sessions

    async def _session(self, spawn, prompt: str, session: Session, max_nudges: int) -> Session:
        proc = await spawn()

        async def send(text: str) -> None:
            message = {"type": "user", "message": {"role": "user", "content": text}}
            proc.stdin.write((json.dumps(message) + "\n").encode())
            await proc.stdin.drain()

        try:
            await send(session_message(prompt, session))
            async for line in proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                session.see(event)
                if event.get("type") != "result":
                    continue
                if event.get("subtype") == "error_max_budget_usd":
                    break
                message = next_message(session.turn, session.limit, session.over, session.end, session.nudges,
                                       max_nudges)
                if message is None:
                    break
                session.nudges += 1
                await send(message)
            proc.stdin.close()
            await proc.wait()
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        session.returncode = proc.returncode
        return session


if __name__ == "__main__":
    ClaudePlayer().serve()
