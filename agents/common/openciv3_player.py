"""The game loop the OpenCiv3 player agents share, whatever coding-agent CLI plays: Claude Code (agents/claude-player),
Codex (agents/codex-player) or Gemini CLI (agents/gemini-player).

agent-env deploys a player with `deploy_agent` and hands it the env's MCP server (urn:agentenv:mcp-config/v1); each
`prompt_agent` step is one task. A prompt that names the turn to stop at ("... until turn 180 ...") is one session,
nudged to keep playing until the game reaches that turn or GAME OVER, so a task can split a long game into steps. Any
other prompt plays the whole game in fresh sessions of OPENCIV3_SESSION_TURNS turns (75; 0 for a single session), which
bounds the model's context: each session picks the game up from the brief and the plan tool. The model endpoint comes
from agent-env as LITELLM_BASE_URL and LITELLM_API_KEY. In a game with several agents, the X-OpenCiv3-Seat header tells
the env which civilization this agent plays: OPENCIV3_SEAT, or else the name deploy_agent gave the agent. end_turn may
wait minutes for the others, so tool calls get a longer timeout.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from agentenv_protocol.a2a_agent import AgentConfig, TaskRequest, TaskResult, TextPart, Usage

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
TOOL_TIMEOUT_SECONDS = 1800
STOP_AT = re.compile(r"\buntil turn (\d+)", re.IGNORECASE)
STREAM_LIMIT = 64 * 1024 * 1024


class PlayerConfig(AgentConfig):
    model: str
    system_prompt: str | None = None
    max_nudges: int = 30


def seat_name(environ: dict[str, str], config: AgentConfig) -> str | None:
    """The seat this agent plays: OPENCIV3_SEAT, or else the name deploy_agent gave it, which the env maps to a seat."""
    return environ.get("OPENCIV3_SEAT") or config.name


def mcp_headers(server: dict, seat: str | None) -> dict[str, str]:
    return {**(server.get("headers") or {}), **({SEAT_HEADER: seat} if seat else {})}


def endpoint(environ: dict[str, str]) -> str:
    """LITELLM_BASE_URL without a trailing /v1: the root a CLI's own provider path goes under."""
    return environ.get("LITELLM_BASE_URL", "").rstrip("/").removesuffix("/v1")


def game_turn(text: str) -> tuple[int | None, int | None, bool] | None:
    """(turn, turn_limit, game over) from the footer of a tool result, if it has one."""
    if m := FOOTER.findall(text):
        return int(m[-1][0]), int(m[-1][1]), "GAME OVER" in text
    return (None, None, True) if "GAME OVER" in text else None


def next_message(turn: int | None, limit: int | None, over: bool, stop: int | None, nudges: int,
                 max_nudges: int) -> str | None:
    """The nudge to send after the model ends a reply, or None when the session is done."""
    if over or nudges >= max_nudges or turn is None:
        return None
    target = min(x for x in (stop, limit) if x is not None) if (stop or limit) else None
    if target is None or turn >= target:
        return None
    return NUDGE.format(turn=turn, stop=target)


@dataclass
class Session:
    """One conversation's play. It ends at `stop` when the prompt names that turn, else `turns` after the turn it
    started at, else at the game's end. `finished` is whether a reply ran to its end; `error` the CLI's last error."""

    stop: int | None = None
    turns: int = 0
    start: int | None = None
    turn: int | None = None
    limit: int | None = None
    over: bool = False
    calls: int = 0
    nudges: int = 0
    reply: str = ""
    finished: bool = False
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    events: list[dict] = field(default_factory=list)
    returncode: int | None = None

    @property
    def end(self) -> int | None:
        return self.start + self.turns if self.turns and self.start is not None else self.stop

    def said(self, text: str) -> None:
        self.reply = text or self.reply

    def saw(self, text: str) -> None:
        """A tool result: its footer moves the session's turn."""
        if seen := game_turn(text):
            turn, limit, over = seen
            if turn is not None:
                self.turn, self.limit = turn, limit
                self.start = turn if self.start is None else self.start
            self.over = self.over or over

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


class Harness:
    """One conversation with a coding-agent CLI that plays over the env's MCP servers."""

    name: ClassVar[str]
    code: ClassVar[str]
    trajectory: ClassVar[str]

    def __init__(self, config: PlayerConfig, servers: dict, seat: str | None, environ: dict[str, str], workdir: Path):
        self.config, self.servers, self.seat, self.environ, self.workdir = config, servers, seat, environ, workdir

    async def reply(self, text: str, session: Session) -> bool:
        """Send `text` and feed `session` the model's reply; False when the conversation cannot go on."""
        raise NotImplementedError

    async def close(self, session: Session) -> None:
        """End the conversation; a CLI that runs once per message has nothing left to end."""


async def stream(cmd: list[str], cwd: Path, env: dict[str, str], stdin: str | None,
                 see: Callable[[dict], None]) -> tuple[int, str]:
    """Run a CLI that prints JSON lines, `see` each event; its exit code and the end of its stderr."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL if stdin is None else asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=STREAM_LIMIT)
    errors = asyncio.create_task(proc.stderr.read())
    try:
        if stdin is not None:
            proc.stdin.write(stdin.encode())
            await proc.stdin.drain()
            proc.stdin.close()
        async for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                see(event)
        await proc.wait()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    return proc.returncode, (await errors).decode(errors="replace").strip()[-2000:]


def played(sessions: list[Session], cli: type[Harness], failure: tuple[str, str] | None = None) -> TaskResult:
    """The task's result: the last reply and what the sessions played, their usage summed and their events in order.
    `failure` (a code and a message, e.g. a timeout) fails the task but keeps what was played."""
    if not sessions:
        return TaskResult.failure(*failure)
    first, last = sessions[0], sessions[-1]
    if first.error and not first.calls:
        return TaskResult.failure(f"{cli.code}_error", first.error.strip())
    if not first.finished and len(sessions) == 1 and failure is None:
        return TaskResult.failure(f"{cli.code}_failed", f"{cli.name} exited ({first.returncode}) without a result.")
    if failure is None and len(sessions) > 1 and not last.over and not last.calls:
        detail = last.error or last.reply or f"{cli.name} exited ({last.returncode}) without a result"
        failure = (f"{cli.code}_error", f"Session {len(sessions)} failed at turn {last.start} before playing: "
                                        f"{detail.strip()}")
    calls, nudges = sum(s.calls for s in sessions), sum(s.nudges for s in sessions)
    costs = [s.cost_usd for s in sessions if s.cost_usd is not None]
    cost = sum(costs) if costs else None
    priced = "" if cost is None else f" (${cost:,.2f})"
    reply = next((s.reply for s in reversed(sessions) if s.reply), "")
    if not first.turns:
        where = "GAME OVER" if last.over else "no turn seen" if last.turn is None else f"turn {last.turn}/{last.limit}"
        summary = f"Session ended at {where} after {calls} tool calls and {nudges} nudges{priced}."
        events = last.events
    else:
        span = "no turn" if last.turn is None else f"T{first.start} to T{last.turn}" + (
            " (GAME OVER)" if last.over else f" of {last.limit}")
        count = f"{len(sessions)} session" + "s" * (len(sessions) > 1)
        summary = f"Played {span} in {count} after {calls:,} tool calls and {nudges:,} nudges{priced}."
        events = [e for k, s in enumerate(sessions, 1)
                  for e in ({"type": "session", "index": k, "start_turn": s.start}, *s.events)]
    builder = TaskResult.builder()
    builder = builder.failed(*failure, error_type="infra_error") if failure else builder.succeeded()
    return (builder.add_text(f"{reply.strip()}\n\n{summary}".strip())
            .usage(Usage(tool_call_count=calls, input_tokens=sum(s.input_tokens for s in sessions),
                         output_tokens=sum(s.output_tokens for s in sessions), cost_usd=cost))
            .native_trajectory(format=cli.trajectory, payload=events)
            .build())


async def play(cli: type[Harness], request: TaskRequest) -> TaskResult:
    """Play the task's prompt with `cli`: one session, or sessions that follow one another, within the task's time."""
    environ, config = dict(os.environ), request.config
    prompt = "\n".join(p.text for p in request.parts if isinstance(p, TextPart))
    if not request.mcp_servers:
        return TaskResult.failure("no_mcp_server", "No MCP server was configured for this agent.")
    with tempfile.TemporaryDirectory(prefix=f"openciv3-{cli.code}-") as tmp:
        def start() -> Harness:
            return cli(config, dict(request.mcp_servers), seat_name(environ, config), environ, Path(tmp))

        sessions: list[Session] = []
        try:
            await asyncio.wait_for(_play(start, prompt, session_plan(prompt, environ), config.max_nudges, sessions),
                                   config.timeout_seconds)
        except TimeoutError:
            return played(sessions, cli, ("timeout", f"The game ran past its {config.timeout_seconds} s limit."))
    return played(sessions, cli)


async def _play(start: Callable[[], Harness], prompt: str, session: Session, max_nudges: int,
                sessions: list[Session]) -> None:
    """Play sessions into `sessions` as they start, so a timeout still has what was played."""
    while True:
        sessions.append(session)
        await _session(start(), prompt, session, max_nudges)
        if not rotate(sessions):
            return
        session = session.following()


async def _session(cli: Harness, prompt: str, session: Session, max_nudges: int) -> None:
    message = session_message(prompt, session)
    try:
        while await cli.reply(message, session):
            message = next_message(session.turn, session.limit, session.over, session.end, session.nudges,
                                   max_nudges)
            if message is None:
                return
            session.nudges += 1
    finally:
        await cli.close(session)
