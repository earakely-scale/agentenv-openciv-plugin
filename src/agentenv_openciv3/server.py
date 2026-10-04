"""OpenCiv3 as an AgentEnv environment: sixteen MCP tools over one CivBridge game (docs/tools.md).

A game may have several seats, one per agent (new-game `seats`): each MCP request plays the seat its
X-OpenCiv3-Seat header names, and the turn advances once every seat has ended it. A person may play a seat
(new-game `humans`) through the play API next to /mcp (docs/play.md)."""

from __future__ import annotations

import asyncio
import base64
import difflib
import inspect
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
import weakref
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal, get_type_hints

from agentenv_protocol import (
    AgentEnvEnvironment,
    DataPart,
    add_data,
    environment_card,
    extension,
    get_data,
    reset_data,
    tool,
)
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.lowlevel.server import request_ctx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response

from . import broadcast, client, live, matchdata, moderation, recording, render, viewer, webart
from .actionlog import ActionLog
from .baselines import POLICIES, Baselines
from .bridge import DEAD, Bridge, BridgeError

log = logging.getLogger(__name__)

SCENARIO_KEYS = ("seed", "civ", "opponents", "size", "difficulty", "barbarians", "landform", "ocean", "turn_limit",
                 "seats", "labels", "humans", "human_turn_seconds", "min_turn_seconds", "broadcast")
SCENARIO_INTS = {"seed": (0, None), "opponents": (1, 11), "ocean": (0, 100), "turn_limit": (1, 1000),
                 "human_turn_seconds": (0, None), "min_turn_seconds": (0, 600)}
ENV_ONLY = ("humans", "human_turn_seconds", "min_turn_seconds", "broadcast")   # the env's own keys, not new_game args
ENV_SCENARIO = (("size", "OPENCIV_SIZE", str), ("opponents", "OPENCIV_OPPONENTS", int),
                ("difficulty", "OPENCIV_DIFFICULTY", str), ("barbarians", "OPENCIV_BARBARIANS", str))
AUTOPLAY_POLICIES = Literal["null", "found_capital", "engine_ai", "settler_bot"]
CALLS_BEFORE_NUDGE = 25
REPEATS_BEFORE_HINT = 3
PLAN_LIMIT = 1000
PUBLIC_PLAN_LIMIT = 300       # what spectators see of a plan
NOTE_LIMIT = 140
MESSAGE_LIMIT = 280
MESSAGES_PER_TURN = 3
SITE_LOOKUPS = 2
AUTOPLAY_CHUNK = 10
RESTARTS_PER_TURN = 3
SEAT_HEADER = "x-openciv3-seat"
SEAT_WAIT_SECONDS = 600
SEAT_STALL_SECONDS = 300
HUMAN_TURN_SECONDS = 900
STALL_CHECK_SECONDS = 5
TOKEN_HEADER = "x-openciv3-token"

UnitId = Annotated[str, Field(description='Unit id, e.g. "u7".')]
CityId = Annotated[str, Field(description='City id, e.g. "c1".')]


class UnitOrderSpec(BaseModel):
    """One order of unit_orders."""
    unit: str = Field(description='A unit id ("u7"), or a group: "idle" (every unit waiting for orders), "idle:Worker" '
                                  '(those of a type) or "all:Warrior" (every unit of a type).')
    order: str = Field(description="As unit_order takes it: fortify, auto_work, explore, goto, settle, hold, upgrade, "
                                   "board, ...")
    x: int | None = Field(default=None, description="Target x, for goto, settle, attack, bombard and board.")
    y: int | None = Field(default=None, description="Target y.")
Coord = Annotated[int | None, Field(description="Map coordinate; x+y is always even.")]
Rate = Annotated[int | None, Field(ge=0, le=10, description="Tenths of commerce, 0-10; omit to keep the current one.")]


def flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).lower() not in ("0", "false", "no", "")


def scenario_from_env() -> dict:
    scenario = {"seed": int(os.environ.get("OPENCIV_SEED", "1")),
                "turn_limit": int(os.environ.get("OPENCIV_TURN_LIMIT", "60"))}
    scenario.update({key: conv(os.environ[var]) for key, var, conv in ENV_SCENARIO if os.environ.get(var)})
    return scenario


def merge_scenario(base: dict, update: dict) -> dict:
    unknown = set(update) - set(SCENARIO_KEYS)
    if unknown:
        raise ValueError(f"unknown scenario keys {sorted(unknown)}; valid: {', '.join(SCENARIO_KEYS)}")
    update = {k: v for k, v in update.items() if v is not None}
    for key, value in update.items():
        if key in ("seats", "humans"):
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"scenario {key} must be a list of civ names, got {value!r}")
            continue
        if key == "labels":
            if not isinstance(value, dict) or not all(isinstance(v, str) for v in (*value, *value.values())):
                raise ValueError(f"scenario labels must map civ names to labels, got {value!r}")
            continue
        if key == "broadcast":
            update[key] = broadcast.settings(value)
            continue
        if key not in SCENARIO_INTS:
            if not isinstance(value, str):
                raise ValueError(f"scenario {key} must be a name, got {value!r}")
            continue
        low, high = SCENARIO_INTS[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < low or (high is not None and value > high):
            upper = high if high is not None else "…"
            raise ValueError(f"scenario {key} must be an integer {low}-{upper}, got {value!r}")
    merged = {**base, **update}
    civs = [merged.get("civ") or "Rome", *(merged.get("seats") or [])]
    if strangers := [h for h in merged.get("humans") or [] if h.lower() not in {c.lower() for c in civs}]:
        raise ValueError(f"scenario humans must be civs of the game ({', '.join(civs)}), got {strangers!r}")
    return merged


def bridge_args(scenario: dict) -> dict:
    """The scenario as new_game args for the bridge."""
    return {k: v for k, v in scenario.items() if k not in ENV_ONLY}


def duration(seconds: float) -> str:
    return f"{seconds // 60:g} min" if seconds >= 60 and seconds % 60 == 0 else f"{seconds:g} s"


def _norm(name: str) -> str:
    n = re.sub(r"[^a-z0-9]", "", name.lower())
    return n[:-2] if n.endswith("es") and len(n) > 4 else n[:-1] if n.endswith("s") and len(n) > 3 else n


def resolve_name(given: str, options: list[str]) -> str | None:
    """The one option `given` plainly means (case, plural or a close spelling), or None if unclear."""
    exact = [o for o in options if _norm(o) == _norm(given)]
    if len(exact) == 1:
        return exact[0]
    close = difflib.get_close_matches(_norm(given), [_norm(o) for o in options], n=2, cutoff=0.85)
    return next(o for o in options if _norm(o) == close[0]) if len(close) == 1 else None


def leader_named(name: str, seats: list[Seat]) -> Seat | None:
    """The seat `name` plainly means, by civ or by label (as resolve_name forgives), or None."""
    wanted = name.strip().lower()
    if not wanted:
        return None
    names = (lambda s: s.civ, lambda s: s.label or "")
    for key in names:
        if exact := next((s for s in seats if key(s).lower() == wanted), None):
            return exact
    for key in names:
        if match := resolve_name(name, [key(s) for s in seats if key(s)]):
            return next(s for s in seats if key(s) == match)
    return None


def clean(text: str) -> str:
    """Text the MCP transport can always encode: lone surrogates (from agent input echoed back) become '?'."""
    return text.encode("utf-8", "replace").decode("utf-8")


@dataclass(eq=False)
class Seat:
    """A civilization an agent (or a person, `human`) plays: its view of the game, plan, notices and action log."""
    civ: str
    label: str | None
    actions: ActionLog
    human: bool = False
    token: str | None = None         # a human seat's play token: whoever has it plays the seat (docs/play.md)
    cache: dict | None = None
    last_state: dict | None = None
    over: bool = False
    start_techs: int = 0
    plan_text: str = ""
    plan_turn: int | None = None
    notices: list[dict] = field(default_factory=list)
    notices_shown: int = 0
    last_call: float = field(default_factory=time.monotonic)
    ready: bool = False
    turn_result: asyncio.Future | None = None
    auto_ended_turns: int = 0
    ended_at: float | None = None    # when the seat ended the turn (time.monotonic), while `ready` or `pacing`
    ended_itself: bool = False       # a human seat ended the turn (rather than the env, for it), while `ready`
    pacing: bool = False             # its end_turn waits for the broadcast pace (min_turn_seconds); ended_at is set
    notes: dict[int, str] = field(default_factory=dict)    # turn -> its end_turn note, as spectators see it
    plans: dict[int, str] = field(default_factory=dict)    # turn -> the last plan it set then, as spectators see it

    @property
    def name(self) -> str:
        return self.label or self.civ


SEAT: ContextVar[Seat | None] = ContextVar("openciv3_seat", default=None)


def requested_seat() -> str | None:
    """The civ the current MCP request's X-OpenCiv3-Seat header names, if any."""
    try:
        request = request_ctx.get().request
    except LookupError:
        return None
    return (getattr(request, "headers", None) or {}).get(SEAT_HEADER)


@environment_card(name="openciv3")
class OpenCiv3Env(AgentEnvEnvironment):
    """One OpenCiv3 game per env. The bridge starts on first use so the HTTP port binds at once."""

    def __init__(self) -> None:
        self.cmd = shlex.split(os.environ.get("CIVBRIDGE_CMD", "/opt/civbridge/CivBridge"))
        self.scenario = scenario_from_env()
        self.record = flag("OPENCIV_RECORD")
        self.client_missing = client.missing() if self.record else "recording is off"
        self.client = self.client_missing is None
        self.root = Path(tempfile.mkdtemp(prefix="openciv3-"))
        weakref.finalize(self, shutil.rmtree, self.root, True)
        self.games = 0
        self.game_dir: Path | None = None
        self.bridge = Bridge(self.cmd)
        self.baselines = Baselines(self.cmd, self.root / "baselines") if flag("OPENCIV_BASELINES") else None
        self.action_log = os.environ.get("OPENCIV_ACTION_LOG")
        self.lock = asyncio.Lock()
        self.game: dict | None = None
        self.seats: list[Seat] = [Seat(self.scenario.get("civ", "Rome"), None, ActionLog(self.action_log))]
        self.messages: list[dict] = []    # {"turn", "from": civ, "to": [civ] | "all", "text", "seconds"}
        self.turn: int | None = None
        self.turn_started = time.monotonic()
        self.game_id: str | None = None
        self.harness = {"autoplay_turns": 0, "new_games": 0, "extension_calls": 0, "engine_restarts": 0}
        self.restarts: dict[int | None, int] = {}
        self.failed: str | None = None
        self.stall_watch: asyncio.Task | None = None
        self.colors: tuple[str | None, dict[int, str], dict[str, int]] = (None, {}, {})  # game, index->hex, civ->index

    # ---- seats ----

    @property
    def seat(self) -> Seat:
        """The seat the current request plays (the first seat outside a tool call)."""
        return SEAT.get() or self.seats[0]

    @property
    def multi(self) -> bool:
        return len(self.seats) > 1

    def _seat_named(self, name: str | None) -> Seat:
        """The seat a header names: by its label (the agent's name in a match), else by its civ."""
        if not name:
            return self.seats[0]
        wanted = name.strip().lower()
        seat = next((s for s in self.seats if (s.label or "").lower() == wanted), None) or next(
            (s for s in self.seats if s.civ.lower() == wanted), None)
        if seat is None:
            raise BridgeError("unknown_seat", f"{name!r} is not a seat in this game; the seats are "
                              f"{', '.join(s.civ for s in self.seats)}.", [s.civ for s in self.seats])
        return seat

    def _mcp_seat(self) -> Seat:
        """The seat an MCP request plays: the one its header names (the first with none; a game with one seat ignores
        the header). Agents can't play a person's seat; with a person in the first seat, agents must name theirs."""
        name = requested_seat() if self.multi else None
        seat = self._seat_named(name)
        if seat.human:
            hint = "" if name or not self.multi else "; name your own seat in the X-OpenCiv3-Seat header"
            raise BridgeError("human_seat", f"{seat.civ} is played by a person{hint}.")
        return seat

    def _seat_of_token(self, token: str | None) -> Seat | None:
        """The human seat a play token belongs to."""
        if not token:
            return None
        return next((s for s in self.seats if s.human and s.token and secrets.compare_digest(s.token, token)), None)

    def _stall_seconds(self, seat: Seat) -> float:
        """How long a seat may make no call while others wait before the env ends its turn; 0: never (humans only)."""
        return self.scenario.get("human_turn_seconds", HUMAN_TURN_SECONDS) if seat.human else SEAT_STALL_SECONDS

    def _seconds_left(self, seat: Seat) -> int | None:
        """Seconds until the env would end this human seat's turn for it; None while it would not (nobody waits, the
        seat has ended the turn, or its turn is never ended for it)."""
        limit = self._stall_seconds(seat)
        if not seat.human or not limit or seat.ready or seat.over or self.failed or not any(
                s.ready for s in self.seats if s is not seat):
            return None
        return max(0, round(limit - (time.monotonic() - seat.last_call)))

    async def _call(self, cmd: str, *, seat: Seat | None = None, **args):
        """A bridge command played by `seat` (default: the request's)."""
        seat = seat or self.seat
        return await self.bridge.call(cmd, **args, **({"seat": seat.civ} if self.multi else {}))

    # ---- game lifecycle ----

    def _bridge_cmd(self, game_dir: Path) -> list[str]:
        return [*self.cmd, "--autosave", str(game_dir / "autosave")] + (
            ["--record", str(game_dir / "record")] if self.record else []) + (
            ["--saves", str(game_dir / "saves")] if self.client else [])

    async def _states(self, bridge: Bridge, seats: list[Seat]) -> list[dict]:
        multi = len(seats) > 1
        return [await bridge.call("state", **({"seat": s.civ} if multi else {})) for s in seats]

    async def _new_game(self, scenario: dict | None = None) -> None:
        """Start a game in a new bridge; the running game is replaced only once the new one has started."""
        scenario = dict(scenario or self.scenario)
        self.games += 1
        game_dir = self.root / f"game-{self.games}"
        bridge = Bridge(self._bridge_cmd(game_dir))
        try:
            game = await bridge.new_game(**bridge_args(scenario))
            humans = {h.lower() for h in scenario.get("humans") or []}
            seats = [Seat(s["civ"], s.get("label"), ActionLog(self.action_log), human=s["civ"].lower() in humans)
                     for s in game.get("seats") or [{"civ": game["civ"]}]]
            for seat in seats:
                seat.token = secrets.token_hex(16) if seat.human else None
            states = await self._states(bridge, seats)
        except BaseException:
            await bridge.close()
            shutil.rmtree(game_dir, ignore_errors=True)
            raise
        old_bridge, old_dir = self.bridge, self.game_dir
        for old in self.seats:
            if old.turn_result and not old.turn_result.done():
                old.turn_result.set_exception(BridgeError("new_game", "a new game started; this one is over."))
        self.bridge, self.game_dir, self.game, self.scenario, self.seats = bridge, game_dir, game, scenario, seats
        self.messages = []
        self.game_id, self.turn = f"g-{uuid.uuid4().hex[:8]}", None
        for seat, state in zip(seats, states, strict=True):
            self._keep(state, seat)
            seat.start_techs = state["score"]["techs"]
        self.restarts, self.failed = {}, None
        self.harness["new_games"] += 1
        self.harness["autoplay_turns"] = 0
        log.info("NEW GAME %s", self.game_id)
        for seat in seats:
            if seat.human:
                log.info("PLAY %s (%s) game %s %s", seat.civ, seat.name, self.game_id, self._play_link(seat))
        await old_bridge.close()
        if old_dir is not None:
            shutil.rmtree(old_dir, ignore_errors=True)
        if self.baselines and not self.multi:
            self.baselines.start(bridge_args(scenario))

    @staticmethod
    def _play_link(seat: Seat) -> str:
        return f"/play#token={seat.token}"

    async def _restart(self) -> None:
        """Restore the game in a new bridge from the autosave of the current turn's start."""
        for seat in self.seats:
            seat.cache, seat.ready, seat.ended_itself = None, False, False
        await self.bridge.close()
        self.restarts[self.turn] = self.restarts.get(self.turn, 0) + 1
        if self.restarts[self.turn] > RESTARTS_PER_TURN:
            self.failed = (f"the game engine failed {RESTARTS_PER_TURN} times on turn {self.turn}; "
                           "the game cannot continue.")
            raise BridgeError("engine_failed", self.failed)
        save = self.game_dir / "autosave" / "autosave.json"
        bridge = Bridge(self._bridge_cmd(self.game_dir))
        try:
            game = await (bridge.load(str(save)) if save.exists() else bridge.new_game(**bridge_args(self.scenario)))
            states = await self._states(bridge, self.seats)
        except BridgeError as e:
            await bridge.close()
            self.failed = f"the game engine stopped and could not be restarted ({e.message}); the game cannot continue."
            raise BridgeError("engine_failed", self.failed) from None
        self.bridge, self.game = bridge, {**self.game, **game}
        for seat, state in zip(self.seats, states, strict=True):
            self._keep(state, seat)
        self.harness["engine_restarts"] += 1
        turn = states[0]["turn"]
        notice = {"turn": turn, "kind": "engine_restarted",
                  "text": f"the engine restarted from the start of turn {turn}; orders given since then are lost"
                          + ("; every civilization ends this turn again" if self.multi else "")}
        for seat in self.seats:
            seat.notices.append(notice)
            seat.actions.note(turn, notice["text"], ok=False)
            if seat.turn_result and not seat.turn_result.done():
                seat.turn_result.set_exception(BridgeError(
                    "engine_restarted", f"{notice['text']}; end your turn again.", suggest="get_turn_brief()"))

    async def _ensure_game(self) -> None:
        if self.failed:
            raise BridgeError("engine_failed", self.failed)
        if self.game is None:
            await self._new_game()
        elif not self.bridge.running:
            await self._restart()

    def _keep(self, state: dict, seat: Seat | None = None) -> None:
        seat = seat or self.seat
        seat.cache = seat.last_state = state
        self._set_turn(state["turn"])
        seat.over = state["game_over"] or state["defeated"]

    async def _state(self, seat: Seat | None = None) -> dict:
        seat = seat or self.seat
        if seat.cache is None:
            self._keep(await self._call("state", seat=seat), seat)
        return seat.cache

    def _set_turn(self, turn: int) -> None:
        """The game is at `turn`; a new turn starts its clock (the live view's seconds)."""
        if turn != self.turn:
            self.turn, self.turn_started = turn, time.monotonic()

    def _vs(self, turn: int) -> dict | None:
        return self.baselines.scores_at(turn) if self.baselines and not self.multi else None

    # ---- the turn of a game with several seats ----

    def _advanced(self, results: dict) -> None:
        """The turn advanced: every seat sees the new turn, and seats waiting on it get their result. An agent that was
        waiting has made no call meanwhile, so its stall clock starts with the new turn; so does a person's who ended
        the turn."""
        now = time.monotonic()
        for seat in self.seats:
            if seat.ended_itself:
                seat.last_call = now
            seat.cache, seat.ready, seat.ended_itself = None, False, False
            if seat.turn_result and not seat.turn_result.done():
                seat.last_call = now
                seat.turn_result.set_result(results[seat.civ])
        self._set_turn(next(iter(results.values()))["turn"])

    async def _seat_turn(self, seat: Seat, res: dict | None) -> dict | None:
        """This seat's result of the turn: at once when its end_turn advanced the game, else when the other seats
        have ended the turn too. The env serves them meanwhile; None when they take longer than SEAT_WAIT_SECONDS."""
        if res is not None and "seats" in res:
            self._advanced(res["seats"])
            return res["seats"][seat.civ]
        if seat.turn_result is None:
            seat.ready, seat.ended_at = True, time.monotonic()
            seat.turn_result = asyncio.get_running_loop().create_future()
            self._watch_stalls()
        result = seat.turn_result
        self.lock.release()
        try:
            return await asyncio.wait_for(asyncio.shield(result), SEAT_WAIT_SECONDS)
        except TimeoutError:
            return None
        finally:
            await self.lock.acquire()
            if result.done():
                seat.turn_result = None

    def _watch_stalls(self) -> None:
        if self.stall_watch is None or self.stall_watch.done():
            self.stall_watch = asyncio.create_task(self._end_stalled_turns())

    async def _end_stalled_turns(self) -> None:
        """While seats wait for the turn to end, end it for any seat that has made no call for SEAT_STALL_SECONDS (an
        agent between sessions, or one that stopped; a person: human_turn_seconds, 0 for never), so one seat cannot
        hold up the others. A seat waiting for the broadcast pace is not silent."""
        while any(s.ready for s in self.seats):
            await asyncio.sleep(STALL_CHECK_SECONDS)
            async with self.lock:
                for seat in self.seats:
                    limit = self._stall_seconds(seat)
                    if (seat.human and not limit) or seat.pacing:
                        continue
                    if not (seat.ready or seat.over or time.monotonic() - seat.last_call < limit or self.failed):
                        await self._end_turn_for(seat)
                if not self.failed:
                    await self._advance_if_ended()

    async def _advance_if_ended(self) -> None:
        """Advance a turn every live seat has ended: the last seat still playing it may have been defeated meanwhile
        (its last city lost, its last settler disbanded), and nothing else would end the turn."""
        if not any(s.ready for s in self.seats):
            return
        for seat in self.seats:
            if not seat.ready and not seat.over:
                await self._state(seat)
                if not seat.over:
                    return
        try:
            res = await self._call("end_turn", seat=next(s for s in self.seats if s.ready), skip_idle=True)
        except BridgeError as e:
            log.warning("advancing the turn every seat ended failed: %s", e.message)
            return
        if "seats" in res:
            self._advanced(res["seats"])

    async def _end_turn_for(self, seat: Seat) -> None:
        turn = self.turn
        try:
            res = await self._call("end_turn", seat=seat, skip_idle=True)
        except BridgeError as e:
            if e.code == "game_over":
                seat.over = True
            elif e.code in DEAD:
                await self._failure(e, "end_turn")
            else:
                log.warning("ending %s's turn failed: %s", seat.civ, e.message)
            return
        seat.auto_ended_turns += 1
        if seat.human:
            text = (f"the env ended your turn {turn} after {duration(self._stall_seconds(seat))} without a move from "
                    "you, so the other civilizations could play on")
        else:
            text = (f"the env ended your turn {turn} after {SEAT_STALL_SECONDS} s without a call from you, so the "
                    "other civilizations could play on")
        seat.notices.append({"turn": turn, "kind": "turn_ended", "text": text})
        seat.actions.note(turn, text, ok=False)
        if "seats" in res:
            self._advanced(res["seats"])
        else:
            seat.ready, seat.ended_at = True, time.monotonic()

    async def _pace(self, seat: Seat) -> None:
        """The broadcast pace (new-game min_turn_seconds): in a game with several seats, the last seat still playing a
        turn ends it no sooner than that long after it began, so spectators can follow every turn. It waits with the
        env free, as _seat_turn does. A new game or an engine restart meanwhile fails the call: a restart takes the
        turn back to its start, so the seat plays it again, as the seats waiting on the turn do."""
        pace = self.scenario.get("min_turn_seconds") or 0
        while self.multi and pace and all(s.ready or s.over for s in self.seats if s is not seat):
            left = self.turn_started + pace - time.monotonic()
            if left <= 0:
                return
            game, restarts = self.game_id, self.harness["engine_restarts"]
            seat.pacing, seat.ended_at = True, time.monotonic()
            self.lock.release()
            try:
                await asyncio.sleep(left)
            finally:
                await self.lock.acquire()
                seat.pacing, seat.last_call = False, time.monotonic()
            if self.game_id != game:
                raise BridgeError("new_game", "a new game started; this one is over.")
            if self.failed:
                raise BridgeError("engine_failed", self.failed)
            if self.harness["engine_restarts"] != restarts:
                restarted = next(n for n in reversed(seat.notices) if n["kind"] == "engine_restarted")
                raise BridgeError("engine_restarted", f"{restarted['text']}; end your turn again.",
                                  suggest="get_turn_brief()")

    async def close(self) -> None:
        if self.stall_watch:
            self.stall_watch.cancel()
        if self.baselines:
            await self.baselines.stop()
        await self.bridge.close()
        shutil.rmtree(self.root, ignore_errors=True)

    # ---- the tool wrapper: anti-stuck rules, footer, action log, crash recovery ----

    async def _failure(self, e: Exception, tool_name: str) -> BridgeError:
        """The error to show for `e`; a dead engine is restored first, and the error says so."""
        if not isinstance(e, BridgeError):
            log.exception("%s failed", tool_name)
            return BridgeError("internal_error", f"internal error: {e!r}")
        if e.code not in DEAD or self.game is None or self.failed:
            return e
        try:
            await self._restart()
        except BridgeError as fatal:
            return fatal
        return BridgeError("engine_restarted", f"{e.message}\n!! {self.seat.notices[-1]['text']}.",
                           suggest="get_turn_brief()")

    async def _guarded(self, tool_name: str, args: dict, body: Callable[[], Awaitable[Any]], *,
                       seat_of: Callable[[], Seat], mutating: bool, extra: dict,
                       done: Callable[[Seat, Any, int], Awaitable[Any]],
                       failed: Callable[[Seat | None, BridgeError, int], Awaitable[Any]]) -> Any:
        """The one path every call that plays a seat takes, from an agent's MCP tool or a person's play API: the
        game (restored if the engine died), the seat `seat_of` binds, the stall clock, the action log, the game-over
        check and turn advance of game actions. Answers `done(seat, body's value, calls this turn)`, or
        `failed(seat, error, calls)` for an error; an error before a seat is bound (seat None) is not logged."""
        started, turn, calls = time.monotonic(), self.turn, 0
        async with self.lock:
            seat = None
            try:
                await self._ensure_game()
                seat = seat_of()
                SEAT.set(seat)
                seat.last_call = started
                turn = self.turn
                calls = seat.actions.begin(turn)
                if mutating:
                    if seat.over:
                        s = await self._state()
                        raise BridgeError("game_over", f"the game is over (T{s['turn']}/{s['turn_limit']}); "
                                          "no further actions are possible.", suggest="get_turn_brief()")
                    for s in self.seats:
                        s.cache = None
                value = await body()
                if mutating and self.multi:
                    await self._advance_if_ended()
            except Exception as e:
                err = await self._failure(e, tool_name)
                answer = await failed(seat, err, calls)
                if seat is not None:
                    self._record(seat, turn, tool_name, args, False, err.code, started, extra)
                return answer
            self._record(seat, turn, tool_name, args, True, None, started, extra)
            return await done(seat, value, calls)

    async def _run(self, tool_name: str, args: dict, body: Callable[[], Awaitable[str]], *,
                   mutating: bool = False, extra: dict | None = None) -> str:
        """Run one MCP tool call; `extra` holds additional action-log fields, which `body` may fill in."""
        args = {k: v for k, v in args.items() if v is not None}

        async def failed(seat: Seat | None, err: BridgeError, calls: int) -> ToolError:
            msg = render.error(err)
            if seat is None:
                return ToolError(clean(msg))
            repeats = seat.actions.failed(f"{tool_name} {json.dumps(args, sort_keys=True)}")
            if repeats >= REPEATS_BEFORE_HINT:
                options = [o for o in (err.suggest, "get_turn_brief()", "end_turn(skip_idle=true)") if o]
                msg += f"\nsame error {repeats}x — try one of: " + " | ".join(dict.fromkeys(options))
            msg = "\n".join(self._notices(seat, msg) + [msg]) + self._nudge(calls)
            if mutating and self.game is not None and not self.failed:
                try:
                    msg += "\n" + render.footer(await self._state())
                except BridgeError:
                    pass
            return ToolError(clean(msg))

        async def done(seat: Seat, text: str, calls: int) -> str:
            return clean("\n".join(self._notices(seat, text) + [text]) + self._nudge(calls))

        answer = await self._guarded(tool_name, args, body, seat_of=self._mcp_seat, mutating=mutating,
                                     extra={} if extra is None else extra, done=done, failed=failed)
        if isinstance(answer, ToolError):
            raise answer
        return answer

    @staticmethod
    def _notices(seat: Seat, text: str) -> list[str]:
        """What happened to the seat since its last call (an engine restart, a turn ended for it, a message from
        another leader) and `text` omits."""
        fresh, seat.notices_shown = seat.notices[seat.notices_shown:], len(seat.notices)
        lines = []
        for n in fresh:
            if n["kind"] == "message":
                said = render.message_line(render.leader(n["from"], n["label"]), "all" if n["to_all"] else "you",
                                           n["text"])
                if said not in text:
                    lines.append(f"✉ {said}")
            elif n["text"] not in text:
                lines.append(f"!! {n['text']}.")
        return lines

    def _nudge(self, calls: int) -> str:
        return f"\n({calls} calls this turn — consider end_turn(skip_idle=true))" if calls > CALLS_BEFORE_NUDGE else ""

    def _record(self, seat, turn, tool_name, args, ok, code, started, extra) -> None:
        seat.actions.record(turn=turn, tool=tool_name, args=args, ok=ok, error_code=code,
                            ms=(time.monotonic() - started) * 1000, **({"seat": seat.civ} if self.multi else {}),
                            **extra)

    async def _site_for(self, uid: str, s: dict) -> dict | None:
        """The best site a settler can still reach before the turn limit."""
        try:
            found = (await self._call("city_sites", unit=uid, top=3)).get("sites") or []
        except BridgeError as e:
            if e.code in DEAD:
                raise
            return None
        return next((x for x in found if not render.arrives_late(x, s["turn"], s["turn_limit"])), None)

    async def _brief(self, events: bool = True) -> str:
        s = await self._state()
        if self.seat.over:
            return render.game_over(s, self._vs(s["turn"]))
        units = {u["id"]: u for u in s.get("units", [])}
        settlers = [b["id"] for b in s.get("blockers", [])
                    if b.get("kind") == "idle_unit" and "settle" in units.get(b["id"], {}).get("orders", [])]
        sites = {uid: site for uid in settlers[:SITE_LOOKUPS] if (site := await self._site_for(uid, s))}
        seat = self.seat
        notices = [n for n in seat.notices if n["turn"] == s["turn"] and n["kind"] != "message"]
        return render.brief(s, start_techs=seat.start_techs, plan=seat.plan_text, plan_turn=seat.plan_turn,
                            baselines=self._vs(s["turn"]), sites=sites, events=events, notices=notices,
                            messages=self._message_lines(seat, s["turn"]))

    def _leader(self, civ: str) -> str:
        """A seat's civ as messages name it: `Greece (sonnet)`."""
        return render.leader(civ, next((s.label for s in self.seats if s.civ == civ), None))

    def _message_lines(self, seat: Seat, turn: int) -> list[str]:
        """The messages `seat` sent or was sent in `turn` and the turn before, oldest first, as its brief lists them."""
        lines = []
        for m in self.messages:
            mine, to_all = m["from"] == seat.civ, m["to"] == "all"
            if not (turn - 1 <= m["turn"] <= turn and (mine or to_all or seat.civ in m["to"])):
                continue
            if mine:
                sender, recipient = "you", "all" if to_all else ", ".join(m["to"])
            else:
                sender, recipient = self._leader(m["from"]), "all" if to_all else "you"
            lines.append(f"T{m['turn']} {render.message_line(sender, recipient, m['text'])}")
        return lines

    async def _footer(self) -> str:
        return render.footer(await self._state())

    # ---- game actions: the bridge call behind each acting tool, shared by the MCP tools and the play API ----
    # Each returns the bridge's result and a one-line message.

    async def _unit_order(self, args: dict) -> tuple[dict, str]:
        res = await self._call("unit_order", **{k: v for k, v in args.items() if v is not None})
        path = res.get("path")
        return res, res.get("message", "done") + (f" (path {path['length']} tiles, {path['turns']}t)" if path else "")

    async def _set_production(self, city: str, item: str, then: list[str] | None = None) -> tuple[dict, str]:
        res, read_as = await self._call_resolving("set_production", "unknown_item", "item", item, city=city,
                                                  **({"then": then} if then is not None else {}))
        return res, read_as + res.get("message", "done")

    async def _research(self, tech: str) -> tuple[dict, str]:
        res, read_as = await self._call_resolving("set_research", "unknown_tech", "tech", tech)
        queue = [t for t in res.get("queue", []) if t != res.get("current")]
        return res, (read_as + res.get("message", f"researching {res.get('current')}")
                     + (f" (queue: {', '.join(queue)})" if queue else ""))

    async def _set_rates(self, science: int | None, luxury: int | None) -> tuple[dict, str]:
        if science is None and luxury is None:
            raise BridgeError("bad_rates", "give science, luxury or both, in tenths (0-10).",
                              suggest="set_rates(science=6, luxury=0)")
        rates = (await self._state()).get("rates") or {}
        self.seat.cache = None
        res = await self._call("set_rates", science=rates.get("science", 0) if science is None else science,
                               luxury=rates.get("luxury", 0) if luxury is None else luxury)
        return res, res.get("message", "rates set")

    async def _buy(self, city: str) -> tuple[dict, str]:
        res = await self._call("hurry", city=city)
        cost = [f"{res[k]} {what}" for k, what in (("gold_cost", "gold"), ("pop_cost", "population")) if res.get(k)]
        return res, res.get("message", "bought") + (f" (cost: {', '.join(cost)})" if cost else "")

    async def _revolution(self, government: str) -> tuple[dict, str]:
        res, read_as = await self._call_resolving("revolution", "unknown_government", "government", government)
        return res, read_as + res.get("message", "revolution")

    async def _diplomacy(self, action: str, civ: str | None, gold: int, give_techs: list[str] | None = None,
                         give_gold: int = 0, get_techs: list[str] | None = None,
                         get_gold: int = 0) -> tuple[dict, str]:
        if not civ:
            raise BridgeError("bad_args", f"{action} needs civ; diplomacy() lists the civilizations you know.",
                              suggest='diplomacy(action="status")')
        args = {"gold": gold} if action == "propose_peace" else {}
        if action in ("quote_trade", "propose_trade"):
            args = {k: v for k, v in (("give_techs", give_techs), ("give_gold", give_gold), ("get_techs", get_techs),
                                      ("get_gold", get_gold)) if v}
        try:
            res, read_as = await self._call_resolving(action, "unknown_civ", "civ", civ, **args)
        except BridgeError as e:
            fixed = await self._trade_techs(civ, args) if e.code == "unknown_tech" else None
            if fixed is None:
                raise
            res, read_as = await self._call_resolving(action, "unknown_civ", "civ", civ, **{**args, **fixed[0]})
            read_as = fixed[1] + read_as
        if action == "quote_trade":
            return res, read_as + render.trade_quote(res)
        return res, read_as + res.get("message", "done")

    async def _trade_techs(self, civ: str, args: dict) -> tuple[dict, str] | None:
        """The trade's tech names, each read as the tradeable tech it plainly means; None if one is unclear or none
        changes. Your techs are what the civ lacks (techs_for_them), its techs what you lack (techs_for_you)."""
        civs = (await self._call("diplomacy")).get("civs", [])
        them = next((c for c in civs if c["civ"] == resolve_name(civ, [c["civ"] for c in civs])), None)
        if them is None:
            return None
        fixed, notes = {}, []
        for key, side in (("give_techs", "techs_for_them"), ("get_techs", "techs_for_you")):
            options = [t["name"] for t in them.get(side) or []]
            names = []
            for name in args.get(key) or []:
                match = resolve_name(name, options)
                if match is None:
                    return None
                if match != name:
                    notes.append(f"(read {name!r} as {match!r}) ")
                names.append(match)
            if names:
                fixed[key] = names
        return (fixed, "".join(notes)) if notes else None

    # ---- tools ----
    # Tools have no return annotation on purpose: `-> str` makes FastMCP send every result twice
    # (as text and as structuredContent).

    @tool()
    async def get_turn_brief(self):
        """Your whole situation in one page: turn and year, gold and tax/science/luxury rates, research, score
        (10·cities + 3·pop + tiles + 4·techs) and your rank, pace against targets and against reference players on
        the same seed, your share of the world's land and population, the culture race once it matters (CULTURE),
        AI trade offers with their worth to you and the accept_trade call (TRADE), what needs orders (with the call
        that resolves it), what needs attention (disorder and riot risk, cities without a defender, full production,
        unspent gold, units that can upgrade and the call), standing orders, cities, last turn's events, and your
        plan. A civilization wins, and the game ends, by
        conquest (the last one left), domination (2/3 of the land and 2/3 of the population), culture (a city with
        20,000 culture points, or 100,000 in all and twice the next civ's) or, at the turn limit, the top score.
        Lost context? Call get_turn_brief."""
        return await self._run("get_turn_brief", {}, self._brief)

    @tool()
    async def list_units(self, filter: Annotated[Literal["needs_orders", "all"], Field(
            description="needs_orders (default): only units waiting for orders; all: every unit.")] = "needs_orders",
                         type: Annotated[str | None, Field(
                             description='Only units of this type, e.g. "Worker".')] = None):
        """One line per unit: id, type, (x,y), moves, status or standing order ("aboard u3" on a ship), a ship's
        cargo ("cargo 1/2: u5"), the orders it accepts now, whether it can found a city on its tile (and why not), and
        in a city its upgrade and the gold (or why not now). Many units at once: unit_orders."""
        async def body():
            return render.units_list(await self._state(), everything=filter == "all", kind=type)
        return await self._run("list_units", {"filter": filter, "type": type}, body)

    @tool()
    async def view_map(self, x: Coord = None, y: Coord = None,
                       radius: Annotated[int, Field(ge=1, le=6, description="Tiles from the center, 1-6.")] = 3,
                       around: Annotated[str | None, Field(
                           description='Center on a unit or city instead of x,y, e.g. "u7" or "c1".')] = None):
        """ASCII map of the explored tiles around (x,y), or around a unit or city; default: your capital.
        Coordinates: x+y is always even; north is (x,y-2), east (x+2,y), northeast (x+1,y-1); distances are in
        tiles. Below the map: hostile and foreign units, cities, your units, good city sites, resources and rivers,
        each with distance and direction from the center. Unexplored tiles are blank."""
        async def body():
            s = await self._state()
            settler = None
            if around:
                things = {o["id"]: o for o in s.get("units", []) + s.get("cities", [])}
                if around not in things:
                    raise BridgeError("unknown_id", f"there is no unit or city {around!r}.", list(things))
                o = things[around]
                cx, cy, label = o["x"], o["y"], f"around {around} {o.get('type') or o.get('name')} {render.pos(o)}"
                settler = around if "settle" in o.get("orders", []) else None
            elif x is not None and y is not None:
                cx, cy, label = x, y, f"at ({x},{y})"
            elif x is not None or y is not None:
                raise BridgeError("bad_target", "give both x and y, or around=<unit or city id>.")
            else:
                home = next((c for c in s.get("cities", []) if c.get("capital")), None) or \
                    next(iter(s.get("cities", []) + s.get("units", [])), None)
                if home is None:
                    raise BridgeError("bad_target", "you have no units or cities; give x and y.")
                cx, cy, label = home["x"], home["y"], f"around {home['id']} {render.pos(home)}"
            m = await self._call("map", x=cx, y=cy, radius=radius)
            try:
                found = await self._call("city_sites", **({"unit": settler} if settler else {}), top=5)
                sites = found["sites"]
            except BridgeError as e:
                if e.code in DEAD:
                    raise
                sites = []
            hostile = ["Barbarians", *(r["civ"] for r in s.get("rivals", []) if r.get("at_war"))]
            return render.map_view(m, label=label, width=self.game["map"]["width"],
                                   wrap_x=self.game["map"].get("wrap_x", False), sites=sites, hostile=hostile)
        return await self._run("view_map", {"x": x, "y": y, "radius": radius, "around": around}, body)

    @tool()
    async def find_city_sites(
            self, unit: Annotated[str | None, Field(
                description="Settler id to rank sites for (travel turns from it); default: the first settler.")] = None,
            top: Annotated[int, Field(ge=1, le=10, description="How many sites, 1-10.")] = 5):
        """Best places to found a city, ranked: (x,y), score, distance and direction, travel turns, area yields,
        river or coast; then every legal site within 4 tiles. Cities need one empty tile between them.
        Then: unit_order(unit=..., order="settle", x=..., y=...)."""
        async def body():
            res = await self._call("city_sites", **({"unit": unit} if unit else {}), top=top)
            s = await self._state()
            origin = res.get("origin") or {}
            at_origin = (origin.get("x"), origin.get("y"))
            who = [u for u in s.get("units", []) if u["id"] == unit or (
                not unit and "settle" in u.get("orders", []) and (u["x"], u["y"]) == at_origin)]
            return render.sites_list(res, who[0] if who else None, s["turn"], s["turn_limit"])
        return await self._run("find_city_sites", {"unit": unit, "top": top}, body)

    @tool()
    async def unit_order(
            self, unit: UnitId,
            order: Annotated[str, Field(description=(
                "Standing orders (run every turn until done): settle (walk to x,y and found a city there), goto (x,y), "
                "explore, auto_work. Now: found_city (on this tile), fortify (a military unit fortified in a city "
                "also keeps an unhappy citizen content), wake, hold (skip this turn), disband, build_road, build_mine, "
                "irrigate, clear_forest, attack (an adjacent enemy unit or city, x,y; only civs you are at war "
                "with), bombard (x,y in range, for units that can), upgrade (in one of your cities, for gold, to "
                "the best unit of its line the city can build; uses its moves), board (a land unit boards your ship "
                "on its tile, or on the adjacent water tile x,y; the ship then carries it) and unload (in a city, a "
                "ship's passengers go ashore). At sea a passenger lands with goto or settle to a land tile next to its "
                "ship."))],
            x: Coord = None, y: Coord = None):
        """Order one of your units. Standing orders keep working on later turns without further calls, so prefer
        them: settle for settlers, explore for one scout, auto_work for workers; keep a military unit in every city.
        A standing order that cannot progress is reported as an event and the unit becomes idle again. A unit next to
        an enemy lists its attack targets with an estimated chance to win; a city that falls is captured (one of size 1
        is destroyed)."""
        args = {"unit": unit, "order": order, "x": x, "y": y}

        async def body():
            res, message = await self._unit_order(args)
            lines = [message]
            if res.get("unit"):
                lines.append(render.unit_line(res["unit"]))
            if res.get("city"):
                lines.append(render.city_line(res["city"]))
            return "\n".join(lines + [await self._footer()])
        return await self._run("unit_order", args, body, mutating=True)

    @tool()
    async def unit_orders(self, orders: Annotated[list[UnitOrderSpec], Field(
            min_length=1, max_length=100, description="The orders, in order; each names a unit or a group.")]):
        """Order many units in one call: each order is what unit_order takes, for one unit ("u7") or a group:
        "idle" (every unit waiting for orders), "idle:Worker" or "all:Warrior". E.g. [{"unit": "idle:Worker",
        "order": "auto_work"}, {"unit": "idle:Warrior", "order": "fortify"}]. One that fails doesn't stop the rest;
        the reply lists each result."""
        spec = [o.model_dump(exclude_none=True) for o in orders]

        async def body():
            res = await self._call("unit_orders", orders=spec)
            return "\n".join([res.get("message", "done"), *render.batch_lines(res["results"], "unit"),
                              await self._footer()])
        return await self._run("unit_orders", {"orders": spec}, body, mutating=True)

    @tool()
    async def city_info(self, city: Annotated[str | None, Field(
            description='City id, e.g. "c1"; omit for all your cities.')] = None):
        """City details: size, food and growth ETA, mood (happy, content, unhappy, defenders), production and ETA,
        buildings and the % they add, and everything it can build now with shield cost, turns and, for a building,
        what it does (e.g. "+50% science (+2 here)"). With more than 4 cities and none named: one line per city,
        those waiting on you first."""
        async def body():
            cities = (await self._state()).get("cities", [])
            ids = [city] if city else [c["id"] for c in cities]
            if not ids:
                return "No cities yet — find_city_sites(), then unit_order(unit=..., order=\"settle\", x=..., y=...)."
            if not city and len(cities) > render.MAX_DETAILED_CITIES:
                return render.cities_table(await self._state())
            details = [render.city_detail(await self._call("city", city=cid)) for cid in ids]
            return "\n".join(details) + '\nChange production: set_production(city="...", item="...", then=[...])'
        return await self._run("city_info", {"city": city}, body)

    async def _call_resolving(self, cmd: str, code: str, key: str, value: str, **args) -> tuple[dict, str]:
        """Call `cmd`; if the name is unknown but plainly means one of the alternatives, retry with that name."""
        try:
            return await self._call(cmd, **{key: value}, **args), ""
        except BridgeError as e:
            match = resolve_name(value, [str(a) for a in e.alternatives or []]) if e.code == code else None
            if match is None:
                if e.code == code:
                    e.suggest = None    # the bridge's guess at an unknown name is no better than the list
                raise
            return await self._call(cmd, **{key: match}, **args), f"(read {value!r} as {match!r}) "

    @tool()
    async def set_production(
            self, city: Annotated[str, Field(description=(
                'City id, e.g. "c1"; several, "c1,c3"; "all"; or "pending" (every city whose next item waits on '
                'you).'))],
            item: Annotated[str, Field(description='What to build, as named by city_info, e.g. "Settler".')],
            then: Annotated[list[str] | None, Field(
                max_length=10, description='What to build after it, in order (a queue); [] clears the queue; omit '
                                           'to keep it.')] = None):
        """Set what a city builds; it keeps the shields already stored. A Settler costs 2 population and is delivered
        when the city reaches size 3 (a Worker: size 2): shields build up while the city grows, so start it early,
        but shields beyond the cost are lost while it waits. With `then`, each completion starts the next queued item
        the city can build; once the queue is empty the engine picks the next item and the brief asks you to keep or
        change it."""
        args = {"city": city, "item": item, **({"then": then} if then is not None else {})}

        async def body():
            res, message = await self._set_production(city, item, then)
            if "results" not in res:
                return "\n".join([message, render.city_line(res["city"]), await self._footer()])
            failed = [r for r in res["results"] if not r["ok"]]
            return "\n".join([message, *render.batch_lines(failed, "city"),
                              *(render.city_line(c) for c in res["cities"][:render.MAX_CITY_LINES]),
                              await self._footer()])
        return await self._run("set_production", args, body, mutating=True)

    @tool()
    async def research(self, tech: Annotated[str | None, Field(
            description='Tech to research, e.g. "Bronze Working"; omit to list the options.')] = None):
        """With no tech: the techs you can research now, with turns and what each unlocks. With a tech: research it;
        a later tech is accepted too, with its missing prerequisites queued first. After each tech the engine picks
        the next one; the brief asks you to keep or change it."""
        async def body():
            if tech is None:
                return render.techs_list(await self._call("techs"))
            _, message = await self._research(tech)
            return "\n".join([message, await self._footer()])
        return await self._run("research", {"tech": tech}, body, mutating=tech is not None)

    @tool()
    async def set_rates(self, science: Rate = None, luxury: Rate = None):
        """Split your cities' commerce, in tenths: science (research), luxury (makes citizens content: the main
        fix for disorder) and tax (gold), which gets the rest (10 - science - luxury). Your government caps each
        rate; a refused split lists the allowed range."""
        async def body():
            res, message = await self._set_rates(science, luxury)
            return "\n".join([message, render.rates_result(res), await self._footer()])
        return await self._run("set_rates", {"science": science, "luxury": luxury}, body, mutating=True)

    @tool()
    async def buy(self, city: CityId):
        """Complete the city's current production next turn. Monarchy and later governments pay gold; Despotism
        pays with citizens (forced labour, at most half the city). Not possible in disorder or for Wealth."""
        async def body():
            res, message = await self._buy(city)
            lines = [message]
            if res.get("city"):
                lines.append(render.city_line(res["city"]))
            return "\n".join(lines + [await self._footer()])
        return await self._run("buy", {"city": city}, body, mutating=True)

    @tool()
    async def revolution(self, government: Annotated[str, Field(
            description='The government to change to, e.g. "Monarchy"; the brief lists the ones you can choose.')]):
        """Change government. Anarchy follows for a few turns (no taxes and no science), then the new government
        starts. Governments differ in corruption, how production is hurried (population or gold), unit support and
        tile yields; Despotism loses a point on rich tiles."""
        async def body():
            res, message = await self._revolution(government)
            return "\n".join([message, render.governments(res.get("government") or {}), await self._footer()])
        return await self._run("revolution", {"government": government}, body, mutating=True)

    @tool()
    async def diplomacy(
            self,
            action: Annotated[Literal["status", "declare_war", "propose_peace", "quote_trade", "propose_trade",
                                      "accept_trade", "decline_trade"], Field(description=(
                "status (default): the civilizations you know; the others take civ. quote_trade asks what a trade is "
                "worth to each side, propose_trade makes it, accept_trade or decline_trade answers an offer."))
            ] = "status",
            civ: Annotated[str | None, Field(description='A civilization you know, e.g. "Arabia".')] = None,
            gold: Annotated[int, Field(ge=0, description="Gold you pay with a peace proposal.")] = 0,
            give_techs: Annotated[list[str] | None, Field(
                max_length=20, description="Trade: techs you give (ones the civ lacks).")] = None,
            give_gold: Annotated[int, Field(ge=0, description="Trade: gold you give.")] = 0,
            get_techs: Annotated[list[str] | None, Field(
                max_length=20, description="Trade: techs you get (ones you lack).")] = None,
            get_gold: Annotated[int, Field(ge=0, description="Trade: gold you get.")] = 0):
        """The civilizations you know: war or peace, their score, government and military against yours, their wars,
        treasury and the techs each side could trade. Declare war to attack a civ; a civ you attack refuses to talk for
        some turns. At war, the status shows the gold a civ asks for peace (it asks more when it is winning);
        propose_peace pays it. At peace, trade techs and gold: an AI takes a trade it values at least even (its
        values are its own research costs), another agent's civ when it accepts. An AI's offer stands for one turn."""
        args = {"action": action, "civ": civ, "gold": gold, "give_techs": give_techs, "give_gold": give_gold,
                "get_techs": get_techs, "get_gold": get_gold}

        async def body():
            if action == "status":
                return "\n".join([render.diplomacy(await self._call("diplomacy")), await self._footer()])
            res, message = await self._diplomacy(action, civ, gold, give_techs, give_gold, get_techs, get_gold)
            if action == "quote_trade":
                return "\n".join([message, await self._footer()])
            return "\n".join([message, render.civ_line(res["civ"]), await self._footer()])
        return await self._run("diplomacy", args, body, mutating=action not in ("status", "quote_trade"))

    @tool()
    async def end_turn(
            self,
            skip_idle: Annotated[bool, Field(
                description="Hold idle units and accept the engine's picks instead of being blocked.")] = False,
            until_attention: Annotated[bool, Field(
                description="Keep ending turns until something needs you (or max_turns).")] = False,
            max_turns: Annotated[int, Field(ge=1, le=20, description="Cap for until_attention, 1-20.")] = 5,
            note: Annotated[str | None, Field(description=(
                "One line for the people watching: what you did this turn and why. At most 140 characters; shown on "
                "the live broadcast and in recordings."))] = None):
        """End your turn. If decisions are pending, returns END TURN BLOCKED with the call that resolves each one;
        skip_idle=true holds idle units, auto-picks research and accepts the engine's picks instead. Otherwise
        returns the events and the next turn's brief. until_attention stops at anything that needs you: a decision,
        disorder or riot risk, a lost unit or city, stolen gold, war, or a hostile unit near your cities or
        settlers. At the turn limit: GAME OVER with the final score."""
        extra = {"idle_units": 0, "turns_advanced": 0}

        async def body():
            seat = self.seat
            before = await self._state()
            extra["idle_units"] = sum(b.get("kind") == "idle_unit" for b in before.get("blockers", []))
            if note is not None and (said := moderation.public(note, NOTE_LIMIT)):
                seat.notes[before["turn"]] = said
            if seat.turn_result is None:
                if skip_idle or not before.get("blockers"):
                    await self._pace(seat)
                seat.cache = None
                try:
                    res = await self._call("end_turn", skip_idle=skip_idle, until_attention=until_attention,
                                           max_turns=max_turns)
                except BridgeError as e:
                    if e.code not in DEAD:
                        extra["turns_advanced"] = (await self._state())["turn"] - before["turn"]
                    raise
                if res.get("blocked"):
                    seat.cache = before
                    raise BridgeError("blocked", render.blocked(res, before))
            else:
                res = None
            if self.multi:
                res = await self._seat_turn(seat, res)
                if res is None:
                    return render.waiting(before["turn"], [s.civ for s in self.seats if not s.ready and not s.over],
                                          SEAT_WAIT_SECONDS)
            extra["turns_advanced"] = res.get("turns_advanced", 0)
            s = await self._state()
            report = render.turn_report(res, res["turn"] - extra["turns_advanced"])
            if seat.over:
                return f"{report}\n{render.game_over(s, self._vs(s['turn']))}\n{render.footer(s)}"
            return f"{report}\n---\n{await self._brief(events=False)}\n{render.footer(s)}"
        return await self._run("end_turn", {"skip_idle": skip_idle, "until_attention": until_attention,
                                            "max_turns": max_turns, "note": note}, body, mutating=True, extra=extra)

    @tool()
    async def plan(self, text: Annotated[str | None, Field(
            description="Your new plan, at most 1,000 characters; omit to read the current one.")] = None):
        """Read your plan, or replace it. Every brief shows it back, so write down what you intend to do over the
        next turns (where to settle, what to build and research) and keep it current."""
        async def body():
            seat = self.seat
            if text is None:
                return f"PLAN (T{seat.plan_turn}) {seat.plan_text}" if seat.plan_text else \
                    "No plan yet — plan(text=...) records one; every brief shows it."
            plain = re.sub(r"[\ud800-\udfff]", "", text).strip()
            if len(plain) > PLAN_LIMIT:
                raise BridgeError("plan_too_long", f"the plan is {len(plain):,} characters; the limit is "
                                  f"{PLAN_LIMIT:,}. Shorten it and call plan(text=...) again.")
            seat.plan_text, seat.plan_turn = plain, self.turn
            seat.plans[self.turn] = moderation.public(plain, PUBLIC_PLAN_LIMIT)
            return f"Plan saved at T{self.turn} ({len(seat.plan_text)}/{PLAN_LIMIT} chars); every brief shows it."
        return await self._run("plan", {"text": text}, body)

    @tool()
    async def message(
            self, to: Annotated[str, Field(description='"all", or another leader\'s civ (or name), e.g. "Greece".')],
            text: Annotated[str, Field(description="What you say, at most 280 characters.")]):
        """Send a message to another leader in this match, or to all of them: they read it in their next tool reply
        and their brief, and everyone watching the match sees it. Alliances, threats, deals: diplomacy is talk. At
        most 280 characters, 3 messages a turn. The game's AI civilizations don't read messages; diplomacy() deals
        with them."""
        async def body():
            seat = self.seat
            if not self.multi:
                raise BridgeError("no_one_to_message", "you are the only leader in this game, so no one reads "
                                  "messages; diplomacy() deals with the AI civilizations.",
                                  suggest='diplomacy(action="status")')
            sent = sum(m["from"] == seat.civ and m["turn"] == self.turn for m in self.messages)
            if sent >= MESSAGES_PER_TURN:
                raise BridgeError("message_limit", f"you have sent {MESSAGES_PER_TURN} messages this turn, the "
                                  "limit; you can send more next turn.", suggest="end_turn(skip_idle=true)")
            recipients, to_all = self._recipients(seat, to)
            if len(text) > MESSAGE_LIMIT:
                raise BridgeError("message_too_long", f"the message is {len(text):,} characters; the limit is "
                                  f"{MESSAGE_LIMIT}. Shorten it and send it again.")
            said = moderation.public(text, MESSAGE_LIMIT)
            if not said:
                raise BridgeError("empty_message", "the message is empty: put what you want to say in text.")
            self.messages.append({"turn": self.turn, "from": seat.civ,
                                  "to": "all" if to_all else [r.civ for r in recipients], "text": said,
                                  "seconds": round(time.monotonic() - self.turn_started, 1)})
            for r in recipients:
                r.notices.append({"turn": self.turn, "kind": "message", "from": seat.civ, "label": seat.label,
                                  "to_all": to_all, "text": said})
            if to_all:
                whom = "all: " + (", ".join(self._leader(r.civ) for r in recipients) or "no other leader is left")
            else:
                whom = self._leader(recipients[0].civ)
            return f'Sent to {whom} · message {sent + 1} of {MESSAGES_PER_TURN} this turn: "{said}"'
        return await self._run("message", {"to": to, "text": text}, body)

    def _recipients(self, seat: Seat, to: str) -> tuple[list[Seat], bool]:
        """The seats a message from `seat` reaches, and whether it went to all: "all" is every other seat still in the
        game, else `to` names one by civ or label (case, plural or a close spelling forgiven)."""
        others = [s for s in self.seats if s is not seat and not s.over]
        if to.strip().lower() == "all":
            return others, True
        if (named := leader_named(to, others)) is not None:
            return [named], False
        seats = {s.civ for s in self.seats}
        ai = resolve_name(to, [r["civ"] for r in (seat.last_state or {}).get("rivals", []) if r["civ"] not in seats])
        if leader_named(to, [seat]):
            why = f"{seat.civ} is you"
        elif out := leader_named(to, [s for s in self.seats if s.over]):
            why = f"{out.civ} is out of the game"
        elif ai:
            why = f"{ai} is an AI civilization: it doesn't read messages, diplomacy() deals with it"
        else:
            why = f"{to!r} is no leader in this match"
        options = ", ".join(self._leader(s.civ) for s in others)
        raise BridgeError("not_a_leader", f"{why}. You can message {options + ' or ' if options else ''}\"all\".",
                          [s.civ for s in others] + ["all"])

    # ---- data plane ----

    @reset_data
    async def data_reset(self) -> None:
        async with self.lock:
            await self._new_game()

    @add_data
    async def data_add(self, parts: list) -> None:
        updates = [p.data["scenario"] for p in parts if isinstance(p, DataPart) and "scenario" in p.data]
        if not updates:
            raise ValueError('data/add expects a DataPart {"scenario": {...}}')
        async with self.lock:
            scenario = self.scenario
            for update in updates:
                scenario = merge_scenario(scenario, update)
            await self._new_game(scenario)

    @get_data
    async def data_get(self) -> list[DataPart]:
        async with self.lock:
            states = {}
            try:
                await self._ensure_game()
                states = {seat.civ: await self._state(seat) for seat in self.seats}
            except BridgeError as e:
                if self.seats[0].last_state is None:
                    raise
                if not self.failed and (await self._failure(e, "data/get")).code == "engine_restarted":
                    states = {seat.civ: await self._state(seat) for seat in self.seats}
            states = {seat.civ: states.get(seat.civ) or seat.last_state for seat in self.seats}
            try:
                world = await self._call("score", seat=self.seats[0]) if not self.failed else {}
            except BridgeError:
                world = {}
        standings = [{"civ": p["civ"], "you": p["is_human"], "defeated": p["defeated"], "score": p["score"]["total"],
                      "culture": p.get("culture", 0), **({"seat": p.get("seat")} if self.multi else {})}
                     for p in sorted(world.get("players", []), key=lambda p: -p["score"]["total"])]
        s = states[self.seats[0].civ]
        summary = {
            "turn": s["turn"], "turn_limit": s["turn_limit"], "game_over": s["game_over"], "defeated": s["defeated"],
            "seed": self.game["seed"], "civ": s["civ"], "human": self.seats[0].human,
            **self._seat_summary(self.seats[0], s),
            "baselines": self.baselines.summary(s["turn"]) if self.baselines and not self.multi
            else {p: {"status": "disabled"} for p in POLICIES},
            "harness": dict(self.harness),
            "engine_failed": self.failed is not None,
            "standings": standings,
            "share": world.get("human_share"),
            "victory": s.get("victory"),
        }
        if self.multi or self.seats[0].human:
            players = {p["civ"]: p for p in world.get("players", [])}
            ranks = {p["civ"]: i for i, p in enumerate(standings, 1)}
            summary["seats"] = [{"civ": seat.civ, "label": seat.label, "human": seat.human,
                                 "defeated": states[seat.civ]["defeated"],
                                 **self._seat_summary(seat, states[seat.civ]), "rank": ranks.get(seat.civ),
                                 "share": (players.get(seat.civ) or {}).get("share"),
                                 "auto_ended_turns": seat.auto_ended_turns} for seat in self.seats]
        return [DataPart(data=summary)]

    @staticmethod
    def _seat_summary(seat: Seat, s: dict) -> dict:
        score = s["score"]
        return {"score": score,
                "metrics": {"cities": score["cities"], "pop": score["pop"], "techs": score["techs"],
                            "tiles": score["tiles"], "units": len(s.get("units", [])), "gold": s.get("gold"),
                            "explored_pct": s.get("explored_pct"), "government": s.get("government")},
                "decisions": s.get("decisions"),
                "actions": seat.actions.summary()}

    # ---- extensions (harness only; every call is counted in data/get's harness) ----

    @extension("urn:openciv3:new-game/v1",
               description="Start a new game; omitted args keep the current scenario. seats: more civs played by "
                           "agents (each in an opponent slot), each agent naming its civ in the X-OpenCiv3-Seat "
                           "header; labels: a name per civ for recordings and reports, e.g. the agent's model; "
                           "humans: civs (among civ and seats) that people play in the browser, each through the "
                           "link in the result's `play` (none when omitted); human_turn_seconds: how long a human "
                           "seat may idle while others wait before the env ends its turn (0: never); "
                           "min_turn_seconds: with several seats, the shortest a turn lasts (0-600, default 0), so "
                           "people can follow a broadcast: the last seat to end a turn waits until then; broadcast: "
                           "the stream's title and voiced casters ({title, casters: false | true | {model, tts_model, "
                           "play_by_play, analyst}}, off when omitted), which `agent-env openciv3 stream` follows.")
    async def new_game(self, seed: int | None = None, civ: str | None = None, opponents: int | None = None,
                       size: str | None = None, difficulty: str | None = None, barbarians: str | None = None,
                       landform: str | None = None, ocean: int | None = None, turn_limit: int | None = None,
                       seats: list[str] | None = None, labels: dict[str, str] | None = None,
                       humans: list[str] | None = None, human_turn_seconds: int = HUMAN_TURN_SECONDS,
                       min_turn_seconds: int = 0, broadcast: dict | None = None) -> dict:
        self.harness["extension_calls"] += 1
        args = {"seed": seed, "civ": civ, "opponents": opponents, "size": size, "difficulty": difficulty,
                "barbarians": barbarians, "landform": landform, "ocean": ocean, "turn_limit": turn_limit,
                "seats": seats, "labels": labels, "humans": humans or [], "human_turn_seconds": human_turn_seconds,
                "min_turn_seconds": min_turn_seconds, "broadcast": {} if broadcast is None else broadcast}
        async with self.lock:
            await self._new_game(merge_scenario(self.scenario, args))
            return {**self.game, "scenario": self.scenario,
                    "play": {seat.civ: self._play_link(seat) for seat in self.seats if seat.human}}

    @extension("urn:openciv3:autoplay/v1", description="Advance the game a number of turns with a scripted policy.")
    async def autoplay(self, turns: int, policy: AUTOPLAY_POLICIES = "null") -> dict:
        """Played in chunks, releasing the env between them, so tools and data/get are never held up for long."""
        self.harness["extension_calls"] += 1
        if turns < 1:
            raise ValueError("turns must be at least 1")
        if self.multi:
            raise ValueError("autoplay plays a game with one seat; in this game every seat ends its own turns")
        trajectory: dict[int, dict] = {}
        left, noted = turns, False
        while left > 0:
            async with self.lock:
                await self._ensure_game()
                if self.seat.over:
                    break
                if not noted:
                    self.seat.actions.note(self.turn, f"autoplay {turns} turns ({policy})")
                    noted = True
                start, chunk = self.turn, min(AUTOPLAY_CHUNK, left)
                self.seat.cache = None
                res = await self._call("autoplay", timeout=60 + 10 * chunk, turns=chunk, policy=policy, record=True)
                trajectory.update((p["turn"], p["score"]) for p in res.get("trajectory", []))
                s = await self._state()
                self.harness["autoplay_turns"] += s["turn"] - start
                left -= chunk
        async with self.lock:
            s = await self._state()
        return {"turn": s["turn"], "game_over": s["game_over"], "defeated": s["defeated"], "score": s["score"],
                "trajectory": [{"turn": t, "score": score} for t, score in sorted(trajectory.items())]}

    def _timeline(self) -> dict[int, list[dict]]:
        """Each turn's game actions for the recording; with several seats each line names its seat."""
        if not self.multi:
            return {t: list(lines) for t, lines in self.seats[0].actions.timeline.items()}
        out: dict[int, list[dict]] = {}
        for seat in self.seats:
            for t, lines in seat.actions.timeline.items():
                out.setdefault(t, []).extend({**a, "text": f"{seat.name}: {a['text']}"} for a in lines)
        return out

    def _per_seat(self, since: int | None = None) -> tuple[dict[str, matchdata.Actions], dict[str, matchdata.Calls]]:
        """Each seat's actions and call counts by turn (from `since` on), keyed by civ; copies, safe to hand to a
        thread."""
        actions = {s.civ: {t: list(a) for t, a in s.actions.timeline.items() if since is None or t >= since}
                   for s in self.seats}
        calls = {s.civ: {t: dict(c) for t, c in s.actions.calls.items() if since is None or t >= since}
                 for s in self.seats}
        return actions, calls

    def _talk(self, since: int | None = None) -> tuple[dict[str, dict[int, str]], dict[str, dict[int, str]],
                                                       list[dict]]:
        """What spectators read of the seats (docs/viewer.md): each seat's end_turn notes and plans by turn (from
        `since` on), keyed by civ, and the game's messages; copies, safe to hand to a thread."""
        def kept(t: int) -> bool:
            return since is None or t >= since
        notes = {s.civ: {t: n for t, n in s.notes.items() if kept(t)} for s in self.seats}
        plans = {s.civ: {t: p for t, p in s.plans.items() if kept(t) and p} for s in self.seats}
        return notes, plans, [dict(m) for m in self.messages if kept(m["turn"])]

    def _client_seats(self, names: list[str] | None) -> list[Seat]:
        if not names or not self.multi:
            return list(self.seats)
        try:
            return list(dict.fromkeys(self._seat_named(n) for n in names))
        except BridgeError as e:
            raise ValueError(f"client_seats: {e.message}") from None

    @extension("urn:openciv3:recording/v1",
               description="Render the game so far: mp4 (gif without ffmpeg), html (the match viewer, one file), png "
                           "of the last turn, or client_mp4 (the real OpenCiv3 client's view, one video per seat, in "
                           "images built with --target client; client_seats limits it to those seats).")
    async def recording(self, formats: list[str] | None = None, view: Literal["spectator", "agent"] = "spectator",
                        fps: int = 4, client_seats: list[str] | None = None) -> dict:
        self.harness["extension_calls"] += 1
        formats = formats or ["mp4", "html", *([client.FORMAT] if self.client else [])]
        valid = [*recording.FORMATS, client.FORMAT]
        if unknown := set(formats) - set(valid):
            raise ValueError(f"unknown formats {sorted(unknown)}; valid: {', '.join(valid)}")
        if not 1 <= fps <= 30:
            raise ValueError("fps must be 1-30")
        if not self.record:
            raise ValueError("recording is off (OPENCIV_RECORD=0)")
        async with self.lock:
            await self._ensure_game()
            seats, multi = self._client_seats(client_seats), self.multi
            snapshots = recording.load_snapshots(self.game_dir / "record") or [await self._call("world")]
            actions = self._timeline()
            seat_actions, calls = self._per_seat()
            seat_notes, plans, messages = self._talk()
            baselines = self.baselines.trajectories() if self.baselines else {}
            name = f"openciv3-seed{self.game['seed']}" + ("-seats" if multi else "") + (
                "-agent" if view == "agent" else "")
            saves = self.game_dir / "saves"
        players = snapshots[-1]["players"]
        seat_actions, calls = live.by_player(seat_actions, players), live.by_player(calls, players)
        seat_notes, plans = live.by_player(seat_notes, players), live.by_player(plans, players)
        messages = live.messages_by_turn(messages, players)
        notes: list[str] = []
        videos: list[recording.File] = []
        client_videos: dict[str, dict] = {}
        if client.FORMAT in formats:
            if not self.client:
                notes.append(f"{client.FORMAT} skipped: {self.client_missing}")
            else:
                turns = sorted(live.turn_of(p) for p in saves.glob("turn-*.json.gz"))
                try:
                    if multi:
                        named = {s.civ: f"{name}.client-{file_part(s.name)}.mp4" for s in seats}
                        rendered = await asyncio.to_thread(client.render_seats, saves, list(named), fps=fps)
                    else:
                        named = {seats[0].civ: f"{name}.client.mp4"}
                        rendered = {seats[0].civ: await asyncio.to_thread(client.render, saves, fps=fps)}
                    for civ, video in rendered.items():
                        videos.append(recording.File(named[civ], "video/mp4", video))
                        client_videos[civ] = {"file": named[civ], "fps": fps, "turns": turns}
                except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
                    notes.append(f"{client.FORMAT} failed: {e}")
        own = [f for f in formats if f in recording.FORMATS]
        files: list[recording.File] = []
        if own:
            files, rendered_notes = await asyncio.to_thread(
                recording.render, snapshots, formats=own, view=view, fps=fps, name=name, actions=actions,
                baselines=baselines, seat_actions=seat_actions, calls=calls, client_videos=client_videos or None,
                humans=[s.civ for s in self.seats if s.human], seat_notes=seat_notes, plans=plans, messages=messages)
            notes = rendered_notes + notes
        return {"turns": len(snapshots), "notes": notes,
                "files": [{"name": f.name, "content_type": f.content_type, "bytes": len(f.data),
                           "base64": base64.b64encode(f.data).decode()} for f in [*files, *videos]]}

    # ---- live view: GET /live follows the game while it plays ----

    def create_app(self) -> FastMCP:
        app = super().create_app()
        self.live = live.Live()
        for path, handler in (("/live", self._live_page), ("/live/data.json", self._live_data),
                              ("/live/state.json", self._live_state), ("/live/frame.png", self._live_frame),
                              ("/live/client.png", self._live_client), ("/play", self._play_page),
                              ("/play/api/view", self._play_view), ("/play/api/status", self._play_status),
                              ("/play/api/city", self._play_city), ("/play/api/techs", self._play_techs),
                              ("/play/api/diplomacy", self._play_diplomacy), ("/play/api/tile", self._play_tile),
                              ("/play/api/sites", self._play_sites)):
            app.custom_route(path, methods=["GET"])(handler)
        app.custom_route("/play/api/act", methods=["POST"])(self._play_act)
        app.custom_route("/play/art/{path:path}", methods=["GET"])(self._play_art)
        return app

    async def _live_page(self, request: Request) -> Response:
        return HTMLResponse(viewer.page(), headers={"Cache-Control": "no-cache"})

    def _live_now(self) -> dict:
        """The turn being played (docs/viewer.md, `live`): who has ended it, for how long each has played it, each
        seat's calls, actions, note and plan so far, and the messages of the turn."""
        pace, show = self.scenario.get("min_turn_seconds", 0), self.scenario.get("broadcast")
        if self.game is None:
            return {"turn": None, "game_over": False, "victory": None, "client": self.client,
                    "recording": self.record, "min_turn_seconds": pace, "broadcast": show, "messages": [], "seats": []}
        now, turn = time.monotonic(), self.turn
        states = [s.last_state for s in self.seats if s.last_state]
        victory = next((st["victory"] for st in states if st.get("victory")), None)
        return {
            "turn": turn, "game_over": victory is not None or any(st.get("game_over") for st in states),
            "victory": victory, "client": self.client, "recording": self.record, "min_turn_seconds": pace,
            "broadcast": show, "messages": [{k: m[k] for k in ("from", "to", "text", "seconds")} for m in self.messages
                         if m["turn"] == turn],
            "seats": [{"civ": s.civ, "label": s.label, "human": s.human, "ended": s.ready or s.pacing or s.over,
                       "seconds": round(max(0.0, (s.ended_at if (s.ready or s.pacing) and s.ended_at else now)
                                            - self.turn_started), 1),
                       "calls": dict(s.actions.calls.get(turn) or {"ok": 0, "failed": 0}),
                       "actions": list(s.actions.timeline.get(turn) or []),
                       "note": s.notes.get(turn), **self._public_plan(s)} for s in self.seats]}

    @staticmethod
    def _public_plan(seat: Seat) -> dict:
        """The seat's plan as spectators see it, and the turn it was set."""
        plan = seat.plans.get(seat.plan_turn)
        return {"plan": plan or None, "plan_turn": seat.plan_turn if plan else None}

    async def _live_data(self, request: Request) -> Response:
        """GET /live/data.json?since=N: the viewer's data with the turns after N, and the turn being played. Reads
        the env's state without its lock (no awaits in between), so a long end_turn never holds the page up."""
        try:
            since = int(request.query_params.get("since", "-1"))
        except ValueError:
            return PlainTextResponse("since is a turn number; -1 (the default) for the whole game", 400)
        now = self._live_now()
        headers = {"Cache-Control": "no-store"}
        if self.game_dir is None or self.game_id is None:
            doc = matchdata.MatchData(game=None).document(since)
            return JSONResponse({**doc, "live": now}, headers=headers)
        actions, calls = self._per_seat(since)
        notes, plans, messages = self._talk(since)
        match = self.live.match_of(self.game_dir / "record", self.game_id,
                                   {s.civ: s.label for s in self.seats if s.label},
                                   [s.civ for s in self.seats if s.human])
        body = await match.document(since, actions, calls, now, notes=notes, plans=plans, messages=messages)
        return Response(body, media_type="application/json", headers=headers)

    async def _live_state(self, request: Request) -> Response:
        async with self.lock:
            game_dir, timeline = self.game_dir, self._timeline()
        snap = await asyncio.to_thread(live.latest, game_dir / "record") if game_dir else None
        return JSONResponse({**live.state(snap, timeline, game=game_dir and game_dir.name, has_client=self.client),
                             "recording": self.record})

    async def _live_frame(self, request: Request) -> Response:
        turn, view = request.query_params.get("turn", ""), request.query_params.get("view", "spectator")
        if not (turn == "" or turn.isdecimal()) or view not in live.VIEWS:
            return PlainTextResponse(f"turn is a number and view one of {', '.join(live.VIEWS)}", 400)
        async with self.lock:
            game_dir = self.game_dir
        png = game_dir and await self.live.frame(game_dir / "record", int(turn) if turn else None, view)
        if png is None:
            return PlainTextResponse("the game has not reached that turn", 404)
        return Response(png, media_type="image/png")

    async def _live_client(self, request: Request) -> Response:
        """GET /live/client.png?seat=CIV: the client's newest frame from that seat (default: the first)."""
        if not self.client:
            return PlainTextResponse(f"no client view here: {self.client_missing}", 404)
        game_dir, seat = self.game_dir, self.seats[0]
        if wanted := request.query_params.get("seat"):
            try:
                seat = self._seat_named(wanted)
            except BridgeError as e:
                return PlainTextResponse(e.message, 400)
        shown = self.live.client_view(game_dir / "saves", seat.civ if self.multi else None) if game_dir else None
        if shown is None:
            return PlainTextResponse("The client is drawing its first frame.", 503, headers={"Retry-After": "2"})
        turn, png = shown
        return Response(png, media_type="image/png", headers={"X-OpenCiv3-Turn": str(turn), "X-OpenCiv3-Seat": seat.civ,
                                                              "Cache-Control": "no-store"})

    # ---- the play API: a person plays a human seat in the browser (docs/play.md, section 4) ----

    async def _play_page(self, request: Request) -> Response:
        return HTMLResponse(viewer.play_page(), headers={"Cache-Control": "no-cache"})

    async def _play_art(self, request: Request) -> Response:
        """The client's art for the play page, when the image has it (webart.py); 404 otherwise. The manifest is
        revalidated, the sheets are cached: their URLs carry the art's id."""
        root, rel = webart.find(), request.path_params["path"]
        if root is None:
            return PlainTextResponse("this env has no art: build the image with the client (--target client)", 404)
        path = (root / rel).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            return PlainTextResponse("no such art", 404)
        cache = "no-cache" if rel == "manifest.json" else "public, max-age=31536000, immutable"
        return FileResponse(path, headers={"Cache-Control": cache})

    @staticmethod
    def _play_token(request: Request) -> str | None:
        return request.headers.get(TOKEN_HEADER) or request.query_params.get("token")

    def _play_seat(self, request: Request) -> Seat | None:
        return self._seat_of_token(self._play_token(request))

    @staticmethod
    def _play_error(err: BridgeError, status: int = 200) -> JSONResponse:
        return JSONResponse({"ok": False, "error": {"code": err.code, "message": err.message,
                                                     "alternatives": err.alternatives, "suggest": err.suggest}},
                            status, headers={"Cache-Control": "no-store"})

    def _play_unauthorized(self) -> JSONResponse:
        return self._play_error(BridgeError("bad_token", "this link plays no seat in the running game: the token is "
                                            "missing or wrong, or a new game has started since"), 401)

    def _bound_seat(self, token: str | None) -> Callable[[], Seat]:
        def seat_of() -> Seat:
            seat = self._seat_of_token(token)
            if seat is None:
                raise BridgeError("bad_token", "this link plays no seat in the running game; a new game has started")
            return seat
        return seat_of

    async def _play_call(self, request: Request, tool_name: str, args: dict, body: Callable[[], Awaitable[Any]], *,
                         mutating: bool = False, extra: dict | None = None,
                         answer: Callable[[Seat, Any], Awaitable[dict]] | None = None) -> JSONResponse:
        """A request of the play API that plays the seat: the same guarded path as an agent's MCP tool (`tool_name`
        and `args` as that tool's, for the action log), answered with `answer(seat, body's value)`, by default the
        value itself."""
        token = self._play_token(request)
        if self._seat_of_token(token) is None:
            return self._play_unauthorized()

        async def done(seat: Seat, value: Any, calls: int) -> JSONResponse:
            return JSONResponse(await answer(seat, value) if answer else value, headers={"Cache-Control": "no-store"})

        async def failed(seat: Seat | None, err: BridgeError, calls: int) -> JSONResponse:
            return self._play_error(err, 401 if err.code == "bad_token" else 200)

        return await self._guarded(tool_name, {k: v for k, v in args.items() if v is not None}, body,
                                   seat_of=self._bound_seat(token), mutating=mutating,
                                   extra={} if extra is None else extra, done=done, failed=failed)

    def _game_over(self) -> tuple[bool, dict | None]:
        states = [s.last_state for s in self.seats if s.last_state]
        victory = next((st["victory"] for st in states if st.get("victory")), None)
        return victory is not None or any(st.get("game_over") for st in states), victory

    def _waiting_for(self, seat: Seat) -> list[str]:
        """The seats still playing the turn, once `seat` has ended it."""
        return [s.civ for s in self.seats if not s.ready and not s.over] if seat.ready else []

    def _play_seats(self) -> list[dict]:
        _, colors, index = self.colors if self.colors[0] == self.game_id else (None, {}, {})
        return [{"civ": s.civ, "label": s.label, "human": s.human, "ended": s.ready or s.over,
                 "defeated": bool((s.last_state or {}).get("defeated")), "color": colors.get(index.get(s.civ, -1))}
                for s in self.seats]

    async def _player_colors(self, seat: Seat) -> tuple[dict[int, str], dict[str, int]]:
        """Every player's colour as the viewer gives it (matchdata.player_colors), and each civ's player index; read
        from the bridge's world once per game."""
        if self.colors[0] != self.game_id:
            players = (await self._call("world", seat=seat)).get("players") or []
            self.colors = (self.game_id, matchdata.player_colors(players), {p["civ"]: p["index"] for p in players})
        return self.colors[1], self.colors[2]

    async def _play_status(self, request: Request) -> Response:
        """Cheap, for polling: reads the env's state without its lock or the bridge, and is no sign of the person."""
        seat = self._play_seat(request)
        if seat is None:
            return self._play_unauthorized()
        game_over, _ = self._game_over()
        return JSONResponse({"game": self.game_id, "turn": self.turn, "game_over": game_over,
                             "ended": seat.ready or seat.over, "waiting_for": self._waiting_for(seat),
                             "seats": self._play_seats(), "seconds_left": self._seconds_left(seat)},
                            headers={"Cache-Control": "no-store"})

    async def _play_view(self, request: Request) -> Response:
        """Everything the play UI draws. Not a call of the seat: no action log, and the stall clock runs on."""
        token = self._play_token(request)
        if self._seat_of_token(token) is None:
            return self._play_unauthorized()
        async with self.lock:
            try:
                await self._ensure_game()
                seat = self._bound_seat(token)()
                state = await self._state(seat)
                known = await self._call("known_map", seat=seat)
                colors, index = await self._player_colors(seat)
            except Exception as e:
                err = await self._failure(e, "play view")
                return self._play_error(err, 401 if err.code == "bad_token" else 200)
            game_over, victory = self._game_over()
            me = next((p["index"] for p in known.get("players") or [] if p.get("me")), index.get(seat.civ))
            game = {"id": self.game_id, "turn": self.turn, "turn_limit": state["turn_limit"],
                    "game_over": game_over or state["game_over"], "victory": victory or state.get("victory"),
                    "me": {"civ": seat.civ, "label": seat.label, "index": me, "color": colors.get(me)},
                    "ended": seat.ready or seat.over, "waiting_for": self._waiting_for(seat),
                    "seats": self._play_seats(), "human_turn_seconds": self._stall_seconds(seat),
                    "seconds_left": self._seconds_left(seat), "art": webart.find() is not None}
            # The notices of this turn and the last: a turn the env ended for the seat is noted with that turn.
            notices = [n for n in seat.notices if self.turn is not None and n["turn"] >= self.turn - 1]
            return JSONResponse({"game": game, "colors": {str(i): c for i, c in colors.items()}, "state": state,
                                 "map": known, "notices": notices}, headers={"Cache-Control": "no-store"})

    async def _play_city(self, request: Request) -> Response:
        if self._play_seat(request) is None:
            return self._play_unauthorized()
        city = request.query_params.get("city")
        if not city:
            return self._play_error(BridgeError("bad_args", "give city, e.g. ?city=c1"), 400)

        async def body():
            return await self._call("city", city=city)
        return await self._play_call(request, "city_info", {"city": city}, body)

    async def _play_techs(self, request: Request) -> Response:
        async def body():
            return await self._call("techs")
        return await self._play_call(request, "research", {}, body)

    async def _play_diplomacy(self, request: Request) -> Response:
        async def body():
            return await self._call("diplomacy")
        args = {"action": "status", "civ": None, "gold": 0, "give_techs": None, "give_gold": 0, "get_techs": None,
                "get_gold": 0}
        return await self._play_call(request, "diplomacy", args, body)

    async def _play_tile(self, request: Request) -> Response:
        if self._play_seat(request) is None:
            return self._play_unauthorized()
        try:
            x, y = int(request.query_params["x"]), int(request.query_params["y"])
        except (KeyError, ValueError):
            return self._play_error(BridgeError("bad_args", "give the tile as ?x=..&y=.., two integers"), 400)

        async def body():
            return await self._call("map", x=x, y=y, radius=0)
        return await self._play_call(request, "view_map", {"x": x, "y": y, "radius": 0}, body)

    async def _play_sites(self, request: Request) -> Response:
        if self._play_seat(request) is None:
            return self._play_unauthorized()
        unit = request.query_params.get("unit") or None
        try:
            top = int(request.query_params.get("top", "5"))
        except ValueError:
            return self._play_error(BridgeError("bad_args", "top is a number of sites, 1-10"), 400)

        async def body():
            return await self._call("city_sites", **({"unit": unit} if unit else {}), top=top)
        return await self._play_call(request, "find_city_sites", {"unit": unit, "top": top}, body)

    async def _play_act(self, request: Request) -> Response:
        """POST {"tool", "args"}: one action, run as the agents' tool of that name."""
        if self._play_seat(request) is None:
            return self._play_unauthorized()
        try:
            req = await request.json()
        except ValueError:
            req = None
        if not isinstance(req, dict) or not isinstance(req.get("args", {}), dict):
            return self._play_error(BridgeError("bad_request", 'send JSON {"tool": "...", "args": {...}}'), 400)
        tool_name, given = req.get("tool"), req.get("args") or {}
        if tool_name not in PLAY_TOOLS:
            return self._play_error(BridgeError("unknown_tool", f"{tool_name!r} is not an action here.",
                                                sorted(PLAY_TOOLS)), 400)
        if tool_name == "end_turn":
            if given:
                return self._play_error(BridgeError("bad_args", "end_turn takes no args."), 400)
            return await self._play_end_turn(request)
        try:
            args = play_args(tool_name, given)
        except ValidationError as e:
            problems = "; ".join(f"{'.'.join(map(str, d['loc'])) or 'args'}: {d['msg']}" for d in e.errors())
            return self._play_error(BridgeError("bad_args", f"{tool_name}: {problems}"), 400)
        body, mutating = self._play_action(tool_name, args)

        async def answer(seat: Seat, value: dict) -> dict:
            return {"ok": True, **value}
        return await self._play_call(request, tool_name, args, body, mutating=mutating, answer=answer)

    def _play_action(self, tool_name: str, a: dict) -> tuple[Callable[[], Awaitable[dict]], bool]:
        """The body of a play API action and whether it acts (as the MCP tool's: research and diplomacy only read
        with no tech or action)."""
        async def body() -> dict:
            if tool_name == "unit_order":
                res, message = await self._unit_order({k: a[k] for k in ("unit", "order", "x", "y")})
            elif tool_name == "set_production":
                res, message = await self._set_production(a["city"], a["item"])
            elif tool_name == "research":
                if a["tech"] is None:
                    return {"message": "the techs you can research", "result": await self._call("techs")}
                res, message = await self._research(a["tech"])
            elif tool_name == "set_rates":
                res, message = await self._set_rates(a["science"], a["luxury"])
            elif tool_name == "buy":
                res, message = await self._buy(a["city"])
            elif tool_name == "revolution":
                res, message = await self._revolution(a["government"])
            elif a["action"] == "status":
                return {"message": "the civilizations you know", "result": await self._call("diplomacy")}
            else:
                res, message = await self._diplomacy(a["action"], a["civ"], a["gold"], a["give_techs"], a["give_gold"],
                                                     a["get_techs"], a["get_gold"])
            return {"message": message, "result": res}
        mutating = not ((tool_name == "research" and a["tech"] is None)
                        or (tool_name == "diplomacy" and a["action"] in ("status", "quote_trade")))
        return body, mutating

    async def _play_end_turn(self, request: Request) -> Response:
        """End the seat's turn with skip_idle, at once: the answer says whether the turn advanced and, if not, who is
        still playing it; the UI polls status meanwhile. Logged as the MCP end_turn(skip_idle=true)."""
        extra = {"idle_units": 0, "turns_advanced": 0}

        async def body() -> dict | None:
            seat = self.seat
            before = await self._state()
            extra["idle_units"] = sum(b.get("kind") == "idle_unit" for b in before.get("blockers", []))
            if seat.ready:
                return None
            await self._pace(seat)
            seat.cache = None
            try:
                res = await self._call("end_turn", skip_idle=True)
            except BridgeError as e:
                if e.code not in DEAD:
                    extra["turns_advanced"] = (await self._state())["turn"] - before["turn"]
                raise
            if res.get("blocked"):
                seat.cache = before
                raise BridgeError("blocked", render.blocked(res, before))
            if "seats" in res:
                self._advanced(res["seats"])
                res = res["seats"][seat.civ]
            elif self.multi:
                seat.ready, seat.ended_at, seat.ended_itself = True, time.monotonic(), True
                self._watch_stalls()
            extra["turns_advanced"] = res.get("turns_advanced", 0)
            return res

        async def answer(seat: Seat, res: dict | None) -> dict:
            await self._state(seat)
            return {"ok": True, "advanced": not seat.ready and res is not None, "turn": self.turn,
                    "waiting_for": self._waiting_for(seat)}

        return await self._play_call(request, "end_turn", {"skip_idle": True, "until_attention": False,
                                                           "max_turns": 5}, body, mutating=True, extra=extra,
                                     answer=answer)


PLAY_TOOLS = ("unit_order", "set_production", "research", "set_rates", "buy", "revolution", "diplomacy", "end_turn")
_PLAY_MODELS: dict[str, type[BaseModel]] = {}


def play_args(tool_name: str, given: dict) -> dict:
    """The play API's args for an action, validated as the MCP tool's (same types, ranges and defaults)."""
    if tool_name not in _PLAY_MODELS:
        fn = getattr(OpenCiv3Env, tool_name)
        hints = get_type_hints(fn, include_extras=True)
        params = list(inspect.signature(fn).parameters.values())[1:]
        _PLAY_MODELS[tool_name] = create_model(
            f"{tool_name}_args", __config__=ConfigDict(extra="forbid"),
            **{p.name: (hints[p.name], ... if p.default is p.empty else p.default) for p in params})
    return _PLAY_MODELS[tool_name].model_validate(given).model_dump()


def file_part(name: str) -> str:
    """A seat's name as part of a file name: `Claude Opus 4` -> `Claude-Opus-4`."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "seat"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    OpenCiv3Env().serve()


if __name__ == "__main__":
    main()
