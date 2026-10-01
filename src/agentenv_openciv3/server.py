"""OpenCiv3 as an AgentEnv environment: twelve MCP tools over one CivBridge game (docs/tools.md)."""

from __future__ import annotations

import asyncio
import base64
import difflib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import weakref
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Literal

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
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from . import client, recording, render
from .actionlog import ActionLog
from .baselines import POLICIES, Baselines
from .bridge import DEAD, Bridge, BridgeError

log = logging.getLogger(__name__)

SCENARIO_KEYS = ("seed", "civ", "opponents", "size", "difficulty", "barbarians", "landform", "ocean", "turn_limit")
SCENARIO_INTS = {"seed": (0, None), "opponents": (1, 11), "ocean": (0, 100), "turn_limit": (1, 1000)}
ENV_SCENARIO = (("size", "OPENCIV_SIZE", str), ("opponents", "OPENCIV_OPPONENTS", int),
                ("difficulty", "OPENCIV_DIFFICULTY", str), ("barbarians", "OPENCIV_BARBARIANS", str))
AUTOPLAY_POLICIES = Literal["null", "found_capital", "engine_ai", "settler_bot"]
CALLS_BEFORE_NUDGE = 25
REPEATS_BEFORE_HINT = 3
PLAN_LIMIT = 1000
SITE_LOOKUPS = 2
AUTOPLAY_CHUNK = 10
RESTARTS_PER_TURN = 3

UnitId = Annotated[str, Field(description='Unit id, e.g. "u7".')]
CityId = Annotated[str, Field(description='City id, e.g. "c1".')]
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
        if key not in SCENARIO_INTS:
            if not isinstance(value, str):
                raise ValueError(f"scenario {key} must be a name, got {value!r}")
            continue
        low, high = SCENARIO_INTS[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < low or (high is not None and value > high):
            upper = high if high is not None else "…"
            raise ValueError(f"scenario {key} must be an integer {low}-{upper}, got {value!r}")
    return {**base, **update}


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


def clean(text: str) -> str:
    """Text the MCP transport can always encode: lone surrogates (from agent input echoed back) become '?'."""
    return text.encode("utf-8", "replace").decode("utf-8")


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
        self.actions = ActionLog(os.environ.get("OPENCIV_ACTION_LOG"))
        self.lock = asyncio.Lock()
        self.game: dict | None = None
        self.cache: dict | None = None
        self.last_state: dict | None = None
        self.turn: int | None = None
        self.over = False
        self.start_techs = 0
        self.plan_text, self.plan_turn = "", None
        self.harness = {"autoplay_turns": 0, "new_games": 0, "extension_calls": 0, "engine_restarts": 0}
        self.notices: list[dict] = []
        self.restarts: dict[int | None, int] = {}
        self.failed: str | None = None

    # ---- game lifecycle ----

    def _bridge_cmd(self, game_dir: Path) -> list[str]:
        return [*self.cmd, "--autosave", str(game_dir / "autosave")] + (
            ["--record", str(game_dir / "record")] if self.record else []) + (
            ["--saves", str(game_dir / "saves")] if self.client else [])

    async def _new_game(self, scenario: dict | None = None) -> None:
        """Start a game in a new bridge; the running game is replaced only once the new one has started."""
        scenario = dict(scenario or self.scenario)
        self.games += 1
        game_dir = self.root / f"game-{self.games}"
        bridge = Bridge(self._bridge_cmd(game_dir))
        try:
            game = await bridge.new_game(**scenario)
            state = await bridge.call("state")
        except BaseException:
            await bridge.close()
            shutil.rmtree(game_dir, ignore_errors=True)
            raise
        old_bridge, old_dir = self.bridge, self.game_dir
        self.bridge, self.game_dir, self.game, self.scenario = bridge, game_dir, game, scenario
        self._keep(state)
        self.start_techs = state["score"]["techs"]
        self.plan_text, self.plan_turn = "", None
        self.actions.reset()
        self.notices, self.restarts, self.failed = [], {}, None
        self.harness["new_games"] += 1
        self.harness["autoplay_turns"] = 0
        await old_bridge.close()
        if old_dir is not None:
            shutil.rmtree(old_dir, ignore_errors=True)
        if self.baselines:
            self.baselines.start(dict(scenario))

    async def _restart(self) -> None:
        """Restore the game in a new bridge from the autosave of the current turn's start."""
        self.cache = None
        await self.bridge.close()
        self.restarts[self.turn] = self.restarts.get(self.turn, 0) + 1
        if self.restarts[self.turn] > RESTARTS_PER_TURN:
            self.failed = (f"the game engine failed {RESTARTS_PER_TURN} times on turn {self.turn}; "
                           "the game cannot continue.")
            raise BridgeError("engine_failed", self.failed)
        save = self.game_dir / "autosave" / "autosave.json"
        bridge = Bridge(self._bridge_cmd(self.game_dir))
        try:
            game = await (bridge.load(str(save)) if save.exists() else bridge.new_game(**self.scenario))
            state = await bridge.call("state")
        except BridgeError as e:
            await bridge.close()
            self.failed = f"the game engine stopped and could not be restarted ({e.message}); the game cannot continue."
            raise BridgeError("engine_failed", self.failed) from None
        self.bridge, self.game = bridge, {**self.game, **game}
        self._keep(state)
        self.harness["engine_restarts"] += 1
        notice = {"turn": state["turn"], "kind": "engine_restarted",
                  "text": f"the engine restarted from the start of turn {state['turn']}; orders given since then "
                          "are lost"}
        self.notices.append(notice)
        self.actions.note(state["turn"], notice["text"], ok=False)

    async def _ensure_game(self) -> None:
        if self.failed:
            raise BridgeError("engine_failed", self.failed)
        if self.game is None:
            await self._new_game()
        elif not self.bridge.running:
            await self._restart()

    def _keep(self, state: dict) -> None:
        self.cache = self.last_state = state
        self.turn = state["turn"]
        self.over = state["game_over"] or state["defeated"]

    async def _state(self) -> dict:
        if self.cache is None:
            self._keep(await self.bridge.call("state"))
        return self.cache

    def _vs(self, turn: int) -> dict | None:
        return self.baselines.scores_at(turn) if self.baselines else None

    async def close(self) -> None:
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
        return BridgeError("engine_restarted", f"{e.message}\n!! {self.notices[-1]['text']}.",
                           suggest="get_turn_brief()")

    async def _run(self, tool_name: str, args: dict, body: Callable[[], Awaitable[str]], *,
                   mutating: bool = False, extra: dict | None = None) -> str:
        """Run one tool call; `extra` holds additional action-log fields, which `body` may fill in."""
        args = {k: v for k, v in args.items() if v is not None}
        extra = {} if extra is None else extra
        started, turn, calls = time.monotonic(), self.turn, 0
        async with self.lock:
            seen = len(self.notices)
            try:
                await self._ensure_game()
                turn = self.turn
                calls = self.actions.begin(turn)
                if mutating:
                    if self.over:
                        s = await self._state()
                        raise BridgeError("game_over", f"the game is over (T{s['turn']}/{s['turn_limit']}); "
                                          "no further actions are possible.", suggest="get_turn_brief()")
                    self.cache = None
                text = await body()
            except Exception as e:
                err = await self._failure(e, tool_name)
                msg = render.error(err)
                repeats = self.actions.failed(f"{tool_name} {json.dumps(args, sort_keys=True)}")
                if repeats >= REPEATS_BEFORE_HINT:
                    options = [o for o in (err.suggest, "get_turn_brief()", "end_turn(skip_idle=true)") if o]
                    msg += f"\nsame error {repeats}x — try one of: " + " | ".join(dict.fromkeys(options))
                msg += self._nudge(calls)
                if mutating and self.game is not None and not self.failed:
                    try:
                        msg += "\n" + render.footer(await self._state())
                    except BridgeError:
                        pass
                self._record(turn, tool_name, args, False, err.code, started, extra)
                raise ToolError(clean(msg)) from None
            self._record(turn, tool_name, args, True, None, started, extra)
            restarted = [f"!! {n['text']}." for n in self.notices[seen:] if n["text"] not in text]
            return clean("\n".join(restarted + [text]) + self._nudge(calls))

    def _nudge(self, calls: int) -> str:
        return f"\n({calls} calls this turn — consider end_turn(skip_idle=true))" if calls > CALLS_BEFORE_NUDGE else ""

    def _record(self, turn, tool_name, args, ok, code, started, extra) -> None:
        self.actions.record(turn=turn, tool=tool_name, args=args, ok=ok, error_code=code,
                            ms=(time.monotonic() - started) * 1000, **extra)

    async def _site_for(self, uid: str, s: dict) -> dict | None:
        """The best site a settler can still reach before the turn limit."""
        try:
            found = (await self.bridge.call("city_sites", unit=uid, top=3)).get("sites") or []
        except BridgeError as e:
            if e.code in DEAD:
                raise
            return None
        return next((x for x in found if not render.arrives_late(x, s["turn"], s["turn_limit"])), None)

    async def _brief(self, events: bool = True) -> str:
        s = await self._state()
        if self.over:
            return render.game_over(s, self._vs(s["turn"]))
        units = {u["id"]: u for u in s.get("units", [])}
        settlers = [b["id"] for b in s.get("blockers", [])
                    if b.get("kind") == "idle_unit" and "settle" in units.get(b["id"], {}).get("orders", [])]
        sites = {uid: site for uid in settlers[:SITE_LOOKUPS] if (site := await self._site_for(uid, s))}
        notices = [n for n in self.notices if n["turn"] == s["turn"]]
        return render.brief(s, start_techs=self.start_techs, plan=self.plan_text, plan_turn=self.plan_turn,
                            baselines=self._vs(s["turn"]), sites=sites, events=events, notices=notices)

    async def _footer(self) -> str:
        return render.footer(await self._state())

    # ---- tools ----
    # Tools have no return annotation on purpose: `-> str` makes FastMCP send every result twice
    # (as text and as structuredContent).

    @tool()
    async def get_turn_brief(self):
        """Your whole situation in one page: turn, gold and tax/science/luxury rates, research, score (10·cities +
        3·pop + tiles + 4·techs), pace against targets and against reference players on the same seed, what needs
        orders (with the call that resolves it), what needs attention (disorder and riot risk, cities without a
        defender, full production, unspent gold), standing orders, cities, last turn's events, and your plan.
        Lost context? Call get_turn_brief."""
        return await self._run("get_turn_brief", {}, self._brief)

    @tool()
    async def list_units(self, filter: Annotated[Literal["needs_orders", "all"], Field(
            description="needs_orders (default): only units waiting for orders; all: every unit.")] = "needs_orders"):
        """One line per unit: id, type, (x,y), moves, status or standing order, the orders it accepts now, and
        whether it can found a city on its tile (and why not)."""
        async def body():
            return render.units_list(await self._state(), everything=filter == "all")
        return await self._run("list_units", {"filter": filter}, body)

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
            m = await self.bridge.call("map", x=cx, y=cy, radius=radius)
            try:
                found = await self.bridge.call("city_sites", **({"unit": settler} if settler else {}), top=5)
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
            res = await self.bridge.call("city_sites", **({"unit": unit} if unit else {}), top=top)
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
                "with) and bombard (x,y in range, for units that can)."))],
            x: Coord = None, y: Coord = None):
        """Order one of your units. Standing orders keep working on later turns without further calls, so prefer
        them: settle for settlers, explore for one scout, auto_work for workers; keep a military unit in every city.
        A standing order that cannot progress is reported as an event and the unit becomes idle again. A unit next to
        an enemy lists its attack targets with an estimated chance to win; a city that falls is razed."""
        args = {"unit": unit, "order": order, "x": x, "y": y}

        async def body():
            res = await self.bridge.call("unit_order", **{k: v for k, v in args.items() if v is not None})
            path = res.get("path")
            lines = [res.get("message", "done") + (f" (path {path['length']} tiles, {path['turns']}t)" if path else "")]
            if res.get("unit"):
                lines.append(render.unit_line(res["unit"]))
            if res.get("city"):
                lines.append(render.city_line(res["city"]))
            return "\n".join(lines + [await self._footer()])
        return await self._run("unit_order", args, body, mutating=True)

    @tool()
    async def city_info(self, city: Annotated[str | None, Field(
            description='City id, e.g. "c1"; omit for all your cities.')] = None):
        """City details: size, food and growth ETA, mood (happy, content, unhappy, defenders), production and ETA,
        buildings, and everything it can build now with shield cost and turns."""
        async def body():
            ids = [city] if city else [c["id"] for c in (await self._state()).get("cities", [])]
            if not ids:
                return "No cities yet — find_city_sites(), then unit_order(unit=..., order=\"settle\", x=..., y=...)."
            details = [render.city_detail(await self.bridge.call("city", city=cid)) for cid in ids]
            return "\n".join(details) + '\nChange production: set_production(city="...", item="...")'
        return await self._run("city_info", {"city": city}, body)

    async def _call_resolving(self, cmd: str, code: str, key: str, value: str, **args) -> tuple[dict, str]:
        """Call `cmd`; if the name is unknown but plainly means one of the alternatives, retry with that name."""
        try:
            return await self.bridge.call(cmd, **{key: value}, **args), ""
        except BridgeError as e:
            match = resolve_name(value, [str(a) for a in e.alternatives or []]) if e.code == code else None
            if match is None:
                if e.code == code:
                    e.suggest = None    # the bridge's guess at an unknown name is no better than the list
                raise
            return await self.bridge.call(cmd, **{key: match}, **args), f"(read {value!r} as {match!r}) "

    @tool()
    async def set_production(self, city: CityId, item: Annotated[str, Field(
            description='What to build, as named by city_info, e.g. "Settler".')]):
        """Set what a city builds; it keeps the shields already stored. A Settler costs 2 population and is delivered
        when the city reaches size 3 (a Worker: size 2): shields build up while the city grows, so start it early,
        but shields beyond the cost are lost while it waits. After each completion the engine picks the next item;
        the brief asks you to keep or change it."""
        async def body():
            res, read_as = await self._call_resolving("set_production", "unknown_item", "item", item, city=city)
            lines = [read_as + res.get("message", "done"), render.city_line(res["city"]), await self._footer()]
            return "\n".join(lines)
        return await self._run("set_production", {"city": city, "item": item}, body, mutating=True)

    @tool()
    async def research(self, tech: Annotated[str | None, Field(
            description='Tech to research, e.g. "Bronze Working"; omit to list the options.')] = None):
        """With no tech: the techs you can research now, with turns and what each unlocks. With a tech: research it;
        a later tech is accepted too, with its missing prerequisites queued first. After each tech the engine picks
        the next one; the brief asks you to keep or change it."""
        async def body():
            if tech is None:
                return render.techs_list(await self.bridge.call("techs"))
            res, read_as = await self._call_resolving("set_research", "unknown_tech", "tech", tech)
            queue = [t for t in res.get("queue", []) if t != res.get("current")]
            return "\n".join([read_as + res.get("message", f"researching {res.get('current')}")
                              + (f" (queue: {', '.join(queue)})" if queue else ""), await self._footer()])
        return await self._run("research", {"tech": tech}, body, mutating=tech is not None)

    @tool()
    async def set_rates(self, science: Rate = None, luxury: Rate = None):
        """Split your cities' commerce, in tenths: science (research), luxury (makes citizens content: the main
        fix for disorder) and tax (gold), which gets the rest (10 - science - luxury). Your government caps each
        rate; a refused split lists the allowed range."""
        async def body():
            if science is None and luxury is None:
                raise BridgeError("bad_rates", "give science, luxury or both, in tenths (0-10).",
                                  suggest="set_rates(science=6, luxury=0)")
            rates = (await self._state()).get("rates") or {}
            self.cache = None
            res = await self.bridge.call("set_rates", science=rates.get("science", 0) if science is None else science,
                                         luxury=rates.get("luxury", 0) if luxury is None else luxury)
            return "\n".join([res.get("message", "rates set"), render.rates_result(res), await self._footer()])
        return await self._run("set_rates", {"science": science, "luxury": luxury}, body, mutating=True)

    @tool()
    async def buy(self, city: CityId):
        """Complete the city's current production next turn. Monarchy and later governments pay gold; Despotism
        pays with citizens (forced labour, at most half the city). Not possible in disorder or for Wealth."""
        async def body():
            res = await self.bridge.call("hurry", city=city)
            cost = [f"{res[k]} {what}" for k, what in (("gold_cost", "gold"), ("pop_cost", "population")) if res.get(k)]
            lines = [res.get("message", "bought") + (f" (cost: {', '.join(cost)})" if cost else "")]
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
            res, read_as = await self._call_resolving("revolution", "unknown_government", "government", government)
            return "\n".join([read_as + res.get("message", "revolution"),
                              render.governments(res.get("government") or {}), await self._footer()])
        return await self._run("revolution", {"government": government}, body, mutating=True)

    @tool()
    async def diplomacy(
            self,
            action: Annotated[Literal["status", "declare_war", "propose_peace"], Field(description=(
                "status (default): the civilizations you know; declare_war or propose_peace with civ."))] = "status",
            civ: Annotated[str | None, Field(description='A civilization you know, e.g. "Arabia".')] = None,
            gold: Annotated[int, Field(ge=0, description="Gold you pay with a peace proposal.")] = 0):
        """The civilizations you know: war or peace, their score, government and military against yours, and their
        wars. Declare war to attack a civ; a civ you attack refuses to talk for some turns. At war, the status shows the
        gold a civ asks for peace (it asks more when it is winning); propose_peace pays it."""
        args = {"action": action, "civ": civ, "gold": gold}

        async def body():
            if action == "status":
                return "\n".join([render.diplomacy(await self.bridge.call("diplomacy")), await self._footer()])
            if not civ:
                raise BridgeError("bad_args", f"{action} needs civ; diplomacy() lists the civilizations you know.",
                                  suggest='diplomacy(action="status")')
            res, read_as = await self._call_resolving(action, "unknown_civ", "civ", civ,
                                                      **({"gold": gold} if action == "propose_peace" else {}))
            return "\n".join([read_as + res.get("message", "done"), render.civ_line(res["civ"]), await self._footer()])
        return await self._run("diplomacy", args, body, mutating=action != "status")

    @tool()
    async def end_turn(
            self,
            skip_idle: Annotated[bool, Field(
                description="Hold idle units and accept the engine's picks instead of being blocked.")] = False,
            until_attention: Annotated[bool, Field(
                description="Keep ending turns until something needs you (or max_turns).")] = False,
            max_turns: Annotated[int, Field(ge=1, le=20, description="Cap for until_attention, 1-20.")] = 5):
        """End your turn. If decisions are pending, returns END TURN BLOCKED with the call that resolves each one;
        skip_idle=true holds idle units, auto-picks research and accepts the engine's picks instead. Otherwise
        returns the events and the next turn's brief. until_attention stops at anything that needs you: a decision,
        disorder or riot risk, a lost unit or city, stolen gold, war, or a hostile unit near your cities or
        settlers. At the turn limit: GAME OVER with the final score."""
        extra = {"idle_units": 0, "turns_advanced": 0}

        async def body():
            before = await self._state()
            extra["idle_units"] = sum(b.get("kind") == "idle_unit" for b in before.get("blockers", []))
            self.cache = None
            try:
                res = await self.bridge.call("end_turn", skip_idle=skip_idle, until_attention=until_attention,
                                             max_turns=max_turns)
            except BridgeError as e:
                if e.code not in DEAD:
                    extra["turns_advanced"] = (await self._state())["turn"] - before["turn"]
                raise
            if res.get("blocked"):
                self.cache = before
                raise BridgeError("blocked", render.blocked(res, before))
            extra["turns_advanced"] = res.get("turns_advanced", 0)
            s = await self._state()
            report = render.turn_report(res, before["turn"])
            if self.over:
                return report + "\n" + render.game_over(s, self._vs(s["turn"]))
            return f"{report}\n---\n{await self._brief(events=False)}\n{render.footer(s)}"
        return await self._run("end_turn", {"skip_idle": skip_idle, "until_attention": until_attention,
                                            "max_turns": max_turns}, body, mutating=True, extra=extra)

    @tool()
    async def plan(self, text: Annotated[str | None, Field(
            description="Your new plan, at most 1,000 characters; omit to read the current one.")] = None):
        """Read your plan, or replace it. Every brief shows it back, so write down what you intend to do over the
        next turns (where to settle, what to build and research) and keep it current."""
        async def body():
            if text is None:
                return f"PLAN (T{self.plan_turn}) {self.plan_text}" if self.plan_text else \
                    "No plan yet — plan(text=...) records one; every brief shows it."
            plain = re.sub(r"[\ud800-\udfff]", "", text).strip()
            if len(plain) > PLAN_LIMIT:
                raise BridgeError("plan_too_long", f"the plan is {len(plain):,} characters; the limit is "
                                  f"{PLAN_LIMIT:,}. Shorten it and call plan(text=...) again.")
            self.plan_text, self.plan_turn = plain, self.turn
            return f"Plan saved at T{self.turn} ({len(self.plan_text)}/{PLAN_LIMIT} chars); every brief shows it."
        return await self._run("plan", {"text": text}, body)

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
            try:
                await self._ensure_game()
                s = await self._state()
            except BridgeError as e:
                if self.last_state is None:
                    raise
                if not self.failed and (await self._failure(e, "data/get")).code == "engine_restarted":
                    s = await self._state()
                else:
                    s = self.last_state
        score = s["score"]
        return [DataPart(data={
            "turn": s["turn"], "turn_limit": s["turn_limit"], "game_over": s["game_over"], "defeated": s["defeated"],
            "seed": self.game["seed"], "civ": s["civ"], "score": score,
            "metrics": {"cities": score["cities"], "pop": score["pop"], "techs": score["techs"],
                        "tiles": score["tiles"], "units": len(s.get("units", [])), "gold": s.get("gold"),
                        "explored_pct": s.get("explored_pct")},
            "decisions": s.get("decisions"),
            "baselines": self.baselines.summary(s["turn"]) if self.baselines
            else {p: {"status": "disabled"} for p in POLICIES},
            "actions": self.actions.summary(),
            "harness": dict(self.harness),
            "engine_failed": self.failed is not None,
        })]

    # ---- extensions (harness only; every call is counted in data/get's harness) ----

    @extension("urn:openciv3:new-game/v1", description="Start a new game; omitted args keep the current scenario.")
    async def new_game(self, seed: int | None = None, civ: str | None = None, opponents: int | None = None,
                       size: str | None = None, difficulty: str | None = None, barbarians: str | None = None,
                       landform: str | None = None, ocean: int | None = None, turn_limit: int | None = None) -> dict:
        self.harness["extension_calls"] += 1
        args = {"seed": seed, "civ": civ, "opponents": opponents, "size": size, "difficulty": difficulty,
                "barbarians": barbarians, "landform": landform, "ocean": ocean, "turn_limit": turn_limit}
        async with self.lock:
            await self._new_game(merge_scenario(self.scenario, args))
            return {**self.game, "scenario": self.scenario}

    @extension("urn:openciv3:autoplay/v1", description="Advance the game a number of turns with a scripted policy.")
    async def autoplay(self, turns: int, policy: AUTOPLAY_POLICIES = "null") -> dict:
        """Played in chunks, releasing the env between them, so tools and data/get are never held up for long."""
        self.harness["extension_calls"] += 1
        if turns < 1:
            raise ValueError("turns must be at least 1")
        trajectory: dict[int, dict] = {}
        left, noted = turns, False
        while left > 0:
            async with self.lock:
                await self._ensure_game()
                if self.over:
                    break
                if not noted:
                    self.actions.note(self.turn, f"autoplay {turns} turns ({policy})")
                    noted = True
                start, chunk = self.turn, min(AUTOPLAY_CHUNK, left)
                self.cache = None
                res = await self.bridge.call("autoplay", timeout=60 + 10 * chunk, turns=chunk, policy=policy,
                                             record=True)
                trajectory.update((p["turn"], p["score"]) for p in res.get("trajectory", []))
                s = await self._state()
                self.harness["autoplay_turns"] += s["turn"] - start
                left -= chunk
        async with self.lock:
            s = await self._state()
        return {"turn": s["turn"], "game_over": s["game_over"], "defeated": s["defeated"], "score": s["score"],
                "trajectory": [{"turn": t, "score": score} for t, score in sorted(trajectory.items())]}

    @extension("urn:openciv3:recording/v1",
               description="Render the game so far: mp4 (gif without ffmpeg), html replay, png of the last turn, or "
                           "client_mp4 (the real OpenCiv3 client's view, in images built with --target client).")
    async def recording(self, formats: list[str] | None = None, view: Literal["spectator", "agent"] = "spectator",
                        fps: int = 4) -> dict:
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
            snapshots = recording.load_snapshots(self.game_dir / "record") or [await self.bridge.call("world")]
            actions = {t: list(lines) for t, lines in self.actions.timeline.items()}
            baselines = self.baselines.trajectories() if self.baselines else {}
            name = f"openciv3-seed{self.game['seed']}" + ("-agent" if view == "agent" else "")
            saves = self.game_dir / "saves"
        own = [f for f in formats if f in recording.FORMATS]
        files, notes = await asyncio.to_thread(recording.render, snapshots, formats=own, view=view, fps=fps, name=name,
                                               actions=actions, baselines=baselines) if own else ([], [])
        if client.FORMAT in formats:
            if not self.client:
                notes.append(f"{client.FORMAT} skipped: {self.client_missing}")
            else:
                try:
                    video = await asyncio.to_thread(client.render, saves, fps=fps)
                    files.append(recording.File(f"{name}.client.mp4", "video/mp4", video))
                except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
                    notes.append(f"{client.FORMAT} failed: {e}")
        return {"turns": len(snapshots), "notes": notes,
                "files": [{"name": f.name, "content_type": f.content_type, "bytes": len(f.data),
                           "base64": base64.b64encode(f.data).decode()} for f in files]}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    OpenCiv3Env().serve()


if __name__ == "__main__":
    main()
