"""OpenCiv3 as an AgentEnv environment: ten MCP tools over one CivBridge game (docs/tools.md)."""

from __future__ import annotations

import asyncio
import difflib
import json
import re
import logging
import os
import time
from collections.abc import Awaitable, Callable
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

from . import render
from .actionlog import ActionLog
from .baselines import POLICIES, Baselines
from .bridge import Bridge, BridgeError

log = logging.getLogger(__name__)

SCENARIO_KEYS = ("seed", "civ", "opponents", "size", "difficulty", "barbarians", "landform", "ocean", "turn_limit")
ENV_SCENARIO = (("size", "OPENCIV_SIZE", str), ("opponents", "OPENCIV_OPPONENTS", int),
                ("difficulty", "OPENCIV_DIFFICULTY", str), ("barbarians", "OPENCIV_BARBARIANS", str))
CALLS_BEFORE_NUDGE = 25
REPEATS_BEFORE_HINT = 3
PLAN_LIMIT = 1000
SITE_LOOKUPS = 2

UnitId = Annotated[str, Field(description='Unit id, e.g. "u7".')]
CityId = Annotated[str, Field(description='City id, e.g. "c1".')]
Coord = Annotated[int | None, Field(description="Map coordinate; x+y is always even.")]


def scenario_from_env() -> dict:
    scenario = {"seed": int(os.environ.get("OPENCIV_SEED", "1")),
                "turn_limit": int(os.environ.get("OPENCIV_TURN_LIMIT", "60"))}
    scenario.update({key: conv(os.environ[var]) for key, var, conv in ENV_SCENARIO if os.environ.get(var)})
    return scenario


def merge_scenario(base: dict, update: dict) -> dict:
    unknown = set(update) - set(SCENARIO_KEYS)
    if unknown:
        raise ValueError(f"unknown scenario keys {sorted(unknown)}; valid: {', '.join(SCENARIO_KEYS)}")
    return {**base, **{k: v for k, v in update.items() if v is not None}}


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


@environment_card(name="openciv3")
class OpenCiv3Env(AgentEnvEnvironment):
    """One OpenCiv3 game per env. The bridge starts on first use so the HTTP port binds at once."""

    def __init__(self) -> None:
        self.cmd = os.environ.get("CIVBRIDGE_CMD", "/opt/civbridge/CivBridge").split()
        self.scenario = scenario_from_env()
        self.bridge = Bridge(self.cmd)
        on = os.environ.get("OPENCIV_BASELINES", "1").lower() not in ("0", "false", "no", "")
        self.baselines = Baselines(self.cmd) if on else None
        self.actions = ActionLog(os.environ.get("OPENCIV_ACTION_LOG"))
        self.lock = asyncio.Lock()
        self.game: dict | None = None
        self.cache: dict | None = None
        self.turn: int | None = None
        self.over = False
        self.start_techs = 0
        self.plan_text, self.plan_turn = "", None

    # ---- game lifecycle ----

    async def _new_game(self, scenario: dict | None = None) -> None:
        """Start a game; a scenario is kept only once a game has started with it."""
        scenario = scenario or self.scenario
        self.game, self.cache = None, None
        self.game = await self.bridge.new_game(**scenario)
        self.scenario = scenario
        self.turn, self.over = self.game["turn"], False
        self.plan_text, self.plan_turn = "", None
        self.actions.reset()
        self.start_techs = (await self._state())["score"]["techs"]
        if self.baselines:
            self.baselines.start(dict(self.scenario))

    async def _ensure_game(self) -> None:
        if self.game is None:
            await self._new_game()

    async def _state(self) -> dict:
        if self.cache is None:
            self.cache = await self.bridge.call("state")
            self.turn = self.cache["turn"]
            self.over = self.cache["game_over"] or self.cache["defeated"]
        return self.cache

    def _vs(self, turn: int) -> dict | None:
        return self.baselines.scores_at(turn) if self.baselines else None

    async def close(self) -> None:
        if self.baselines:
            await self.baselines.stop()
        await self.bridge.close()

    # ---- the tool wrapper: anti-stuck rules, footer, action log ----

    async def _run(self, tool_name: str, args: dict, body: Callable[[], Awaitable[str]], *,
                   mutating: bool = False, extra: dict | None = None) -> str:
        """Run one tool call; `extra` holds additional action-log fields, which `body` may fill in."""
        args = {k: v for k, v in args.items() if v is not None}
        extra = {} if extra is None else extra
        started, turn, calls = time.monotonic(), self.turn, 0
        async with self.lock:
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
                err = e if isinstance(e, BridgeError) else BridgeError("internal_error", f"internal error: {e!r}")
                if not isinstance(e, BridgeError):
                    log.exception("%s failed", tool_name)
                msg = render.error(err)
                repeats = self.actions.failed(f"{tool_name} {json.dumps(args, sort_keys=True)}")
                if repeats >= REPEATS_BEFORE_HINT:
                    options = [o for o in (err.suggest, "get_turn_brief()", "end_turn(skip_idle=true)") if o]
                    msg += f"\nsame error {repeats}x — try one of: " + " | ".join(dict.fromkeys(options))
                msg += self._nudge(calls)
                if mutating and self.game is not None:
                    try:
                        msg += "\n" + render.footer(await self._state())
                    except BridgeError:
                        pass
                self._record(turn, tool_name, args, False, err.code, started, extra)
                raise ToolError(msg) from None
            self._record(turn, tool_name, args, True, None, started, extra)
            return text + self._nudge(calls)

    def _nudge(self, calls: int) -> str:
        return f"\n({calls} calls this turn — consider end_turn(skip_idle=true))" if calls > CALLS_BEFORE_NUDGE else ""

    def _record(self, turn, tool_name, args, ok, code, started, extra) -> None:
        self.actions.record(turn=turn, tool=tool_name, args=args, ok=ok, error_code=code,
                            ms=(time.monotonic() - started) * 1000, **extra)

    async def _brief(self, events: bool = True) -> str:
        s = await self._state()
        if self.over:
            return render.game_over(s, self._vs(s["turn"]))
        units = {u["id"]: u for u in s.get("units", [])}
        settlers = [b["id"] for b in s.get("blockers", [])
                    if b.get("kind") == "idle_unit" and "settle" in units.get(b["id"], {}).get("orders", [])]
        sites = {}
        for uid in settlers[:SITE_LOOKUPS]:
            try:
                found = (await self.bridge.call("city_sites", unit=uid, top=1)).get("sites")
            except BridgeError:
                continue
            if found:
                sites[uid] = found[0]
        return render.brief(s, start_techs=self.start_techs, plan=self.plan_text, plan_turn=self.plan_turn,
                            baselines=self._vs(s["turn"]), sites=sites, events=events)

    async def _footer(self) -> str:
        return render.footer(await self._state())

    # ---- tools ----
    # Tools have no return annotation on purpose: `-> str` makes FastMCP send every result twice
    # (as text and as structuredContent).

    @tool()
    async def get_turn_brief(self):
        """Your whole situation in one page: turn, gold, research, score (10·cities + 3·pop + tiles + 4·techs), pace
        against targets and against the built-in AI and a do-nothing player on the same seed, what needs orders
        (with the call that resolves it), standing orders, cities, last turn's events, and your plan.
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
        tiles. Below the map: foreign units, cities, your units, good city sites, resources and rivers, each with
        distance and direction from the center. Unexplored tiles are blank."""
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
            except BridgeError:
                sites = []
            return render.map_view(m, label=label, width=self.game["map"]["width"],
                                   wrap_x=self.game["map"].get("wrap_x", False), sites=sites)
        return await self._run("view_map", {"x": x, "y": y, "radius": radius, "around": around}, body)

    @tool()
    async def find_city_sites(
            self, unit: Annotated[str | None, Field(
                description="Settler id to rank sites for (travel turns from it); default: the first settler.")] = None,
            top: Annotated[int, Field(ge=1, le=10, description="How many sites, 1-10.")] = 5):
        """Best places to found a city, ranked: (x,y), score, distance and direction, travel turns, area yields,
        river or coast. Then: unit_order(unit=..., order="settle", x=..., y=...)."""
        async def body():
            res = await self.bridge.call("city_sites", **({"unit": unit} if unit else {}), top=top)
            s = await self._state()
            origin = res.get("origin") or {}
            at_origin = (origin.get("x"), origin.get("y"))
            settlers = [u for u in s.get("units", []) if u["id"] == unit or (
                not unit and "settle" in u.get("orders", []) and (u["x"], u["y"]) == at_origin)]
            return render.sites_list(res, settlers[0] if settlers else None)
        return await self._run("find_city_sites", {"unit": unit, "top": top}, body)

    @tool()
    async def unit_order(
            self, unit: UnitId,
            order: Annotated[str, Field(description=(
                "Standing orders (run every turn until done): settle (walk to x,y and found a city there), goto (x,y), "
                "explore, auto_work. Now: found_city (on this tile), fortify, wake, hold (skip this turn), disband, "
                "build_road, build_mine, irrigate, clear_forest."))],
            x: Coord = None, y: Coord = None):
        """Order one of your units. Standing orders keep working on later turns without further calls, so prefer
        them: settle for settlers, explore for scouts and warriors, auto_work for workers. A standing order that
        cannot progress is reported as an event and the unit becomes idle again."""
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
        """City details: size, food and growth ETA, production and ETA, buildings, and everything it can build now
        with shield cost and turns."""
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
                raise
            return await self.bridge.call(cmd, **{key: match}, **args), f"(read {value!r} as {match!r}) "

    @tool()
    async def set_production(self, city: CityId, item: Annotated[str, Field(
            description='What to build, as named by city_info, e.g. "Settler".')]):
        """Set what a city builds. A Settler costs 2 population and completes only at city size 3 or more; a Worker
        needs size 2."""
        async def body():
            res, read_as = await self._call_resolving("set_production", "unknown_item", "item", item, city=city)
            return "\n".join([read_as + res.get("message", "done"), render.city_line(res["city"]), await self._footer()])
        return await self._run("set_production", {"city": city, "item": item}, body, mutating=True)

    @tool()
    async def research(self, tech: Annotated[str | None, Field(
            description='Tech to research, e.g. "Bronze Working"; omit to list the options.')] = None):
        """With no tech: the techs you can research now, with turns and what each unlocks. With a tech: research it;
        any tech is accepted and missing prerequisites are queued first."""
        async def body():
            if tech is None:
                return render.techs_list(await self.bridge.call("techs"))
            res, read_as = await self._call_resolving("set_research", "unknown_tech", "tech", tech)
            queue = [t for t in res.get("queue", []) if t != res.get("current")]
            return "\n".join([read_as + res.get("message", f"researching {res.get('current')}")
                              + (f" (queue: {', '.join(queue)})" if queue else ""), await self._footer()])
        return await self._run("research", {"tech": tech}, body, mutating=tech is not None)

    @tool()
    async def end_turn(
            self,
            skip_idle: Annotated[bool, Field(
                description="Hold idle units this turn instead of being blocked.")] = False,
            until_attention: Annotated[bool, Field(
                description="Keep ending turns until something needs you (or max_turns).")] = False,
            max_turns: Annotated[int, Field(ge=1, le=20, description="Cap for until_attention, 1-20.")] = 5):
        """End your turn. If decisions are pending, returns END TURN BLOCKED with the call that resolves each one;
        skip_idle=true holds idle units and auto-picks research instead. Otherwise returns the events and the next
        turn's brief. At the turn limit: GAME OVER with the final score."""
        extra = {"idle_units": 0, "turns_advanced": 0}

        async def body():
            before = await self._state()
            extra["idle_units"] = sum(b.get("kind") == "idle_unit" for b in before.get("blockers", []))
            self.cache = None
            res = await self.bridge.call("end_turn", skip_idle=skip_idle, until_attention=until_attention,
                                         max_turns=max_turns)
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
            if len(text) > PLAN_LIMIT:
                raise BridgeError("plan_too_long", f"the plan is {len(text):,} characters; the limit is "
                                  f"{PLAN_LIMIT:,}. Shorten it and call plan(text=...) again.")
            self.plan_text, self.plan_turn = text.strip(), self.turn
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
            await self._ensure_game()
            s = await self._state()
        score = s["score"]
        return [DataPart(data={
            "turn": s["turn"], "turn_limit": s["turn_limit"], "game_over": s["game_over"], "defeated": s["defeated"],
            "seed": self.game["seed"], "civ": s["civ"], "score": score,
            "metrics": {"cities": score["cities"], "pop": score["pop"], "techs": score["techs"],
                        "tiles": score["tiles"], "units": len(s.get("units", [])), "gold": s.get("gold"),
                        "explored_pct": s.get("explored_pct")},
            "baselines": self.baselines.summary(s["turn"]) if self.baselines
            else {p: {"status": "disabled"} for p in POLICIES},
            "actions": self.actions.summary(),
        })]

    # ---- extensions (harness only) ----

    @extension("urn:openciv3:new-game/v1", description="Start a new game; omitted args keep the current scenario.")
    async def new_game(self, seed: int | None = None, civ: str | None = None, opponents: int | None = None,
                       size: str | None = None, difficulty: str | None = None, barbarians: str | None = None,
                       landform: str | None = None, ocean: int | None = None, turn_limit: int | None = None) -> dict:
        args = {"seed": seed, "civ": civ, "opponents": opponents, "size": size, "difficulty": difficulty,
                "barbarians": barbarians, "landform": landform, "ocean": ocean, "turn_limit": turn_limit}
        async with self.lock:
            await self._new_game(merge_scenario(self.scenario, args))
            return {**self.game, "scenario": self.scenario}

    @extension("urn:openciv3:autoplay/v1", description="Advance the game a number of turns with a scripted policy.")
    async def autoplay(self, turns: int, policy: Literal["null", "found_capital", "engine_ai"] = "null") -> dict:
        async with self.lock:
            await self._ensure_game()
            self.cache = None
            res = await self.bridge.call("autoplay", timeout=60 + 10 * turns, turns=turns, policy=policy, record=True)
            await self._state()
            return res


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    OpenCiv3Env().serve()


if __name__ == "__main__":
    main()
