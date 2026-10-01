"""Toy stand-in for the OpenCiv3 env, to test the playtest harness without the engine.

It serves what the harness relies on, shaped like the real env (docs/tools.md) and the AgentEnv SDK:
the ten tools over streamable HTTP at /mcp, the card at /.well-known/agent-env.json, JSON-RPC
data/reset|add|get at /agentenv, the new-game and autoplay extensions, and the action log. The game
is tiny and deterministic: a settler that can found cities, a warrior that explores, growth,
production and research.

    MCP_PORT=18765 OPENCIV_TURN_LIMIT=6 python playtest/stub_env.py      # needs mcp>=1.25,<2
"""
import functools
import json
import os
import time
from collections import Counter
from typing import Literal

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

NAME = "openciv3"
SITES = [(14, 10, 41, "river"), (8, 14, 37, "coast"), (12, 4, 33, "hills"), (4, 8, 30, "grassland")]
BUILD_TURNS = {"Warrior": 2, "Settler": 4, "Worker": 3, "Granary": 6}
TECHS = {"Bronze Working": 3, "Pottery": 3, "Ceremonial Burial": 2, "Writing": 4}
ORDERS = {"Settler": ["settle", "found_city", "goto", "hold", "disband"],
          "Warrior": ["explore", "goto", "fortify", "wake", "hold", "disband"],
          "Worker": ["auto_work", "goto", "hold", "disband"]}
CITY_NAMES = ["Rome", "Veii", "Antium", "Cumae", "Neapolis", "Ravenna"]


class GameError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def dist(x1, y1, x2, y2) -> int:
    dx, dy = x2 - x1, y2 - y1
    return max(abs(dx + dy), abs(dx - dy)) // 2


def direction(x1, y1, x2, y2) -> str:
    dx, dy = x2 - x1, y2 - y1
    if dx == dy == 0:
        return "here"
    return ("N" if dy < 0 else "S" if dy > 0 else "") + ("E" if dx > 0 else "W" if dx < 0 else "")


class Game:
    def __init__(self, seed: int = 1, turn_limit: int = 60, **_):
        self.seed, self.limit, self.turn = int(seed), int(turn_limit), 1
        self.units = {"u1": {"type": "Settler", "x": 10, "y": 10, "order": None, "target": None, "eta": 0},
                      "u2": {"type": "Warrior", "x": 10, "y": 10, "order": None, "target": None, "eta": 0}}
        self.cities: dict[str, dict] = {}
        self.next_unit, self.research, self.beakers, self.techs = 3, None, 0, ["Alphabet"]
        self.plan, self.events, self.grow_every = "", [], 3 + self.seed % 2

    # --- rules -------------------------------------------------------------------------------
    @property
    def over(self) -> bool:
        return self.turn >= self.limit

    def score(self) -> dict:
        pop = sum(c["size"] for c in self.cities.values())
        tiles = sum(5 + 2 * c["size"] for c in self.cities.values())
        c, t = len(self.cities), len(self.techs)
        return {"total": 10 * c + 3 * pop + tiles + 4 * t, "cities": c, "pop": pop, "tiles": tiles, "techs": t}

    def why_not_found(self, x, y) -> str | None:
        for c in self.cities.values():
            if dist(x, y, c["x"], c["y"]) <= 1:
                return f"adjacent to {c['name']} ({c['x']},{c['y']}); cities need one empty tile between them"
        return None

    def sites(self, x, y, top=5) -> list[tuple]:
        ok = [s for s in SITES if not self.why_not_found(s[0], s[1])]
        return sorted(ok, key=lambda s: (-s[2], dist(x, y, s[0], s[1])))[:top]

    def found(self, uid: str) -> str:
        u = self.units.pop(uid)
        cid = f"c{len(self.cities) + 1}"
        self.cities[cid] = {"name": CITY_NAMES[len(self.cities) % len(CITY_NAMES)], "x": u["x"], "y": u["y"],
                            "size": 1, "food": 0, "producing": None, "progress": 0}
        self.events.append(f"{self.cities[cid]['name']} ({cid}) founded at ({u['x']},{u['y']})")
        return cid

    def needs_orders(self) -> list[str]:
        return [uid for uid, u in self.units.items() if u["order"] is None]

    def blockers(self) -> list[str]:
        out = [f"{uid} {self.units[uid]['type']} has no orders → unit_order(unit=\"{uid}\", ...)" for uid in self.needs_orders()]
        out += [f"{cid} {c['name']} produces nothing → set_production(city=\"{cid}\", item=\"Settler\")"
                for cid, c in self.cities.items() if not c["producing"]]
        if self.cities and not self.research and (tech := next((t for t in TECHS if t not in self.techs), None)):
            out.append(f"no research → research(tech=\"{tech}\")")
        return out

    def advance(self) -> None:
        self.events = []
        if self.cities and not self.research and (pick := next((t for t in TECHS if t not in self.techs), None)):
            self.research = pick
            self.events.append(f"research auto-picked: {pick}")
        for uid, u in list(self.units.items()):
            if u["order"] in ("settle", "goto"):
                u["eta"] -= 1
                if u["eta"] <= 0:
                    u["x"], u["y"] = u["target"]
                    if u["order"] == "settle":
                        if (why := self.why_not_found(u["x"], u["y"])) is None:
                            self.found(uid)
                            continue
                        self.events.append(f"settle_failed: {uid} at ({u['x']},{u['y']}): {why}")
                    else:
                        self.events.append(f"{uid} arrived at ({u['x']},{u['y']})")
                    u["order"] = u["target"] = None
            elif u["order"] == "explore":
                u["eta"] += 1
                if u["eta"] >= 3:
                    self.events.append(f"explore_done: {uid} found nothing more to explore")
                    u["order"], u["eta"] = None, 0
            elif u["order"] == "hold":
                u["order"] = None
        for cid, c in self.cities.items():
            c["food"] += 1
            if c["food"] >= self.grow_every:
                c["size"], c["food"] = c["size"] + 1, 0
                self.events.append(f"{c['name']} grew to size {c['size']}")
            if c["producing"]:
                c["progress"] += 1
                item = c["producing"]
                if c["progress"] >= BUILD_TURNS[item] and not (item == "Settler" and c["size"] < 2):
                    c["progress"] = 0
                    if item == "Granary":
                        c["producing"] = None
                    else:
                        c["size"] -= item == "Settler"
                        uid = f"u{self.next_unit}"
                        self.next_unit += 1
                        self.units[uid] = {"type": item, "x": c["x"], "y": c["y"], "order": None, "target": None, "eta": 0}
                    self.events.append(f"{c['name']} built {item}")
        if self.research:
            self.beakers += 1 + len(self.cities) // 2
            if self.beakers >= TECHS[self.research]:
                self.techs.append(self.research)
                self.events.append(f"learned {self.research}")
                self.research, self.beakers = None, 0
        self.turn += 1

    # --- text ---------------------------------------------------------------------------------
    def footer(self) -> str:
        need = self.needs_orders() + [cid for cid, c in self.cities.items() if not c["producing"]]
        return f"[T{self.turn}/{self.limit} · needs orders: {', '.join(need) or 'none'}]"

    def unit_line(self, uid: str) -> str:
        u = self.units[uid]
        status = u["order"] or "idle"
        if u["target"]:
            status += f" → ({u['target'][0]},{u['target'][1]}) {u['eta']}t"
        line = f"{uid} {u['type']} ({u['x']},{u['y']}) {status} · orders: {', '.join(ORDERS[u['type']])}"
        if u["type"] == "Settler":
            why = self.why_not_found(u["x"], u["y"])
            line += " · found_city here: " + ("yes" if why is None else f"no, {why}")
        return line

    def city_line(self, cid: str) -> str:
        c = self.cities[cid]
        prod = f"{c['producing']} {c['progress']}/{BUILD_TURNS[c['producing']]}" if c["producing"] else "nothing"
        return f"{cid} {c['name']} ({c['x']},{c['y']}) size {c['size']}, grows in {self.grow_every - c['food']}t, building {prod}"


class State:
    def __init__(self):
        self.scenario = {"seed": int(os.environ.get("OPENCIV_SEED", 1)),
                         "turn_limit": int(os.environ.get("OPENCIV_TURN_LIMIT", 60))}
        self.log_path = os.environ.get("OPENCIV_ACTION_LOG")
        self.new_game()

    def new_game(self):
        self.game = Game(**self.scenario)
        self.ok = self.invalid = self.streak = self.max_streak = 0
        self.calls, self.failures = Counter(), Counter()
        self._baselines = {p: simulate(self.scenario, p) for p in ("null", "engine_ai")}

    def baselines(self) -> dict:
        g = self.game
        return {p: {"turn": g.turn, "score": s[min(g.turn, len(s)) - 1], "final": s[-1]} for p, s in self._baselines.items()}

    def summary(self) -> dict:
        g, sc = self.game, self.game.score()
        return {"turn": g.turn, "turn_limit": g.limit, "game_over": g.over, "defeated": False, "seed": g.seed,
                "civ": "Rome", "score": sc,
                "metrics": {"cities": sc["cities"], "pop": sc["pop"], "techs": sc["techs"], "tiles": sc["tiles"],
                            "units": len(g.units), "gold": 10 * g.turn, "explored_pct": min(100, 10 + 5 * g.turn)},
                "baselines": self.baselines(),
                "actions": {"ok": self.ok, "invalid": self.invalid, "max_consecutive_errors": self.max_streak}}


def autoplay(g: Game, turns: int, policy: str) -> None:
    for _ in range(turns):
        if g.over:
            return
        if policy in ("found_capital", "engine_ai") and "u1" in g.units and not g.cities:
            g.found("u1")
        for cid, c in g.cities.items():
            c["producing"] = c["producing"] or ("Settler" if policy == "engine_ai" else "Warrior")
        for uid, u in g.units.items():
            if policy == "engine_ai" and u["type"] == "Settler" and u["order"] is None and (s := g.sites(u["x"], u["y"], 1)):
                u.update(order="settle", target=(s[0][0], s[0][1]), eta=max(1, dist(u["x"], u["y"], s[0][0], s[0][1])))
            elif u["order"] is None:
                u["order"] = "explore" if policy == "engine_ai" and u["type"] == "Warrior" else "hold"
        g.advance()


def simulate(scenario: dict, policy: str) -> list[int]:
    """Score per turn (index turn-1) for a policy on a fresh game of the same scenario."""
    g = Game(**scenario)
    scores = [g.score()["total"]]
    while not g.over:
        autoplay(g, 1, policy)
        scores.append(g.score()["total"])
    return scores


STATE = State()
mcp = FastMCP(NAME, host=os.environ.get("MCP_HOST", "0.0.0.0"), port=int(os.environ.get("MCP_PORT", 18765)))


def tool(fn):
    """Register a tool that logs each call, raises GameError as an MCP error and applies the anti-stuck hints."""
    @functools.wraps(fn)
    def wrapper(**kwargs):
        g, t0 = STATE.game, time.perf_counter()
        turn, args = g.turn, {k: v for k, v in kwargs.items() if v is not None}
        row = {"ts": round(time.time(), 3), "turn": turn, "tool": fn.__name__, "args": args, "ok": True, "error_code": None}
        if fn.__name__ == "end_turn":
            row["idle_units"] = len(g.needs_orders())
        STATE.calls[turn] += 1
        try:
            text = fn(**kwargs)
            STATE.ok, STATE.streak = STATE.ok + 1, 0
            if STATE.calls[turn] >= 25:
                text += "\nconsider end_turn(skip_idle=true)"
            return text
        except GameError as e:
            row.update(ok=False, error_code=e.code)
            STATE.invalid, STATE.streak = STATE.invalid + 1, STATE.streak + 1
            STATE.max_streak = max(STATE.max_streak, STATE.streak)
            key = (turn, fn.__name__, json.dumps(args, sort_keys=True))
            STATE.failures[key] += 1
            hint = "\nsame error 3x — try one of: get_turn_brief(), list_units(), end_turn(skip_idle=true)" \
                if STATE.failures[key] >= 3 else ""
            raise ValueError(f"{e}{hint}\n{g.footer()}") from None
        except Exception:
            row.update(ok=False, error_code="internal")
            raise
        finally:
            if fn.__name__ == "end_turn":
                row["turns_advanced"] = STATE.game.turn - turn
            row["ms"] = round((time.perf_counter() - t0) * 1000, 2)
            if STATE.log_path:
                with open(STATE.log_path, "a") as f:
                    f.write(json.dumps(row) + "\n")
    return mcp.tool()(wrapper)


def _live() -> Game:
    if STATE.game.over:
        raise GameError("game_over", f"the game is over at T{STATE.game.turn}; final score {STATE.game.score()['total']}.")
    return STATE.game


def _unit(g: Game, uid: str) -> dict:
    if uid not in g.units:
        raise GameError("unknown_unit", f"no unit {uid}. Your units: {', '.join(g.units) or 'none'}.")
    return g.units[uid]


def _city(g: Game, cid: str) -> dict:
    if cid not in g.cities:
        raise GameError("unknown_city", f"no city {cid}. Your cities: {', '.join(g.cities) or 'none'}.")
    return g.cities[cid]


def brief(g: Game) -> str:
    sc, base = g.score(), STATE.baselines()
    research = f"{g.research} {g.beakers}/{TECHS[g.research]}" if g.research else "none"
    standing = [g.unit_line(uid) for uid, u in g.units.items() if u["order"]]
    lines = [f"T{g.turn}/{g.limit} · Rome · Research: {research}",
             f"SCORE {sc['total']} (cities {sc['cities']}, pop {sc['pop']}, tiles {sc['tiles']}, techs {sc['techs']})"
             f" · baselines now: null {base['null']['score']}, built-in AI {base['engine_ai']['score']}",
             "NEEDS ORDERS: " + ("; ".join(g.blockers()) or "nothing")]
    lines += ["STANDING: " + "; ".join(standing)] if standing else []
    lines += ["CITIES: " + "; ".join(g.city_line(c) for c in g.cities)] if g.cities else ["CITIES: none yet"]
    lines += ["EVENTS: " + "; ".join(g.events)] if g.events else []
    lines += [f"PLAN: {g.plan}"] if g.plan else []
    return "\n".join(lines)


@tool
def get_turn_brief() -> str:
    """Turn, score, research, baselines, what needs orders, cities, last events and your plan. Lost context? Call this."""
    return brief(STATE.game)


@tool
def list_units(filter: Literal["needs_orders", "all"] = "needs_orders") -> str:
    """One line per unit: id, type, (x,y), status, valid orders, and whether found_city works where it stands."""
    g = STATE.game
    ids = g.needs_orders() if filter == "needs_orders" else list(g.units)
    return "\n".join([g.unit_line(u) for u in ids] or ["no units need orders"]) + "\n" + g.footer()


@tool
def view_map(x: int | None = None, y: int | None = None, radius: int = 3, around: str | None = None) -> str:
    """ASCII map around (x,y) or around a unit or city id ("u1", "c1"). C city, S settler, W warrior, * good site."""
    g = STATE.game
    if around:
        obj = g.units.get(around) or g.cities.get(around)
        if not obj:
            raise GameError("bad_target", f"no unit or city {around}.")
        x, y = obj["x"], obj["y"]
    if x is None or y is None:
        x, y = 10, 10
    radius = max(1, min(radius, 6))
    marks = {(s[0], s[1]): "*" for s in SITES}
    marks |= {(u["x"], u["y"]): u["type"][0] for u in g.units.values()}
    marks |= {(c["x"], c["y"]): "C" for c in g.cities.values()}
    rows = ["".join(marks.get((cx, cy), ".") if (cx + cy) % 2 == 0 else " " for cx in range(x - 2 * radius, x + 2 * radius + 1))
            for cy in range(y - 2 * radius, y + 2 * radius + 1)]
    notable = [f"site ({s[0]},{s[1]}) {s[3]}, {dist(x, y, s[0], s[1])} tiles {direction(x, y, s[0], s[1])}" for s in g.sites(x, y)]
    return "\n".join(rows + ["legend: C city, S settler, W warrior, * good site, . grassland", "notable: " + "; ".join(notable)])


@tool
def find_city_sites(unit: str | None = None, top: int = 5) -> str:
    """Ranked city sites with score, distance, direction and travel turns from the unit (default: first settler)."""
    g = STATE.game
    u = _unit(g, unit) if unit else next((u for u in g.units.values() if u["type"] == "Settler"), {"x": 10, "y": 10})
    sites = g.sites(u["x"], u["y"], top)
    return "\n".join(f"({sx},{sy}) score {score}, {dist(u['x'], u['y'], sx, sy)} tiles {direction(u['x'], u['y'], sx, sy)}, "
                     f"{max(1, dist(u['x'], u['y'], sx, sy))} turns, {kind}" for sx, sy, score, kind in sites) or "no free sites"


@tool
def unit_order(unit: str, order: str, x: int | None = None, y: int | None = None) -> str:
    """Order a unit. settle(x,y) walks there and founds a city on arrival; found_city founds here; goto(x,y);
    explore; auto_work; fortify; wake; hold (skip this turn); disband."""
    g = _live()
    u = _unit(g, unit)
    valid = ORDERS[u["type"]]
    if order not in valid:
        raise GameError("invalid_order", f"{unit} {u['type']} cannot {order}. Valid orders: {', '.join(valid)}.")
    if order in ("settle", "goto"):
        if x is None or y is None:
            raise GameError("bad_target", f"{order} needs x and y, e.g. unit_order(unit=\"{unit}\", order=\"{order}\", x=14, y=10).")
        if (x + y) % 2:
            raise GameError("bad_target", f"({x},{y}) is not a tile: x+y must be even. Copy coordinates from find_city_sites.")
        if order == "settle" and (why := g.why_not_found(x, y)):
            best = g.sites(u["x"], u["y"], 1)
            raise GameError("cannot_found", f"cannot settle at ({x},{y}): {why}."
                            + (f" Do this: unit_order(unit=\"{unit}\", order=\"settle\", x={best[0][0]}, y={best[0][1]})" if best else ""))
        if (x, y) == (u["x"], u["y"]) and order == "settle":
            return f"{g.cities[g.found(unit)]['name']} founded.\n{g.footer()}"
        d = dist(u["x"], u["y"], x, y)
        u.update(order=order, target=(x, y), eta=max(1, d))
        return f"{unit} {u['type']} will {order} at ({x},{y}), {d} tiles {direction(u['x'], u['y'], x, y)}, arriving in {max(1, d)}t.\n{g.footer()}"
    if order == "found_city":
        if why := g.why_not_found(u["x"], u["y"]):
            best = g.sites(u["x"], u["y"], 3)
            raise GameError("cannot_found", f"cannot found a city at ({u['x']},{u['y']}): {why}. Sites: "
                            + " | ".join(f"({s[0]},{s[1]}) score {s[2]}" for s in best)
                            + (f". Do this: unit_order(unit=\"{unit}\", order=\"settle\", x={best[0][0]}, y={best[0][1]})" if best else ""))
        return f"{g.cities[g.found(unit)]['name']} founded at ({u['x']},{u['y']}).\n{g.footer()}"
    if order == "disband":
        del g.units[unit]
        return f"{unit} disbanded.\n{g.footer()}"
    u.update(order=None if order == "wake" else {"fortify": "fortified", "explore": "explore"}.get(order, order), target=None, eta=0)
    return f"{unit} {u['type']}: {order}.\n{g.footer()}"


@tool
def city_info(city: str | None = None) -> str:
    """Size, growth, production and ETA, and what each city can build with turns."""
    g = STATE.game
    ids = [city] if city else list(g.cities)
    for cid in ids:
        _city(g, cid)
    options = ", ".join(f"{k} ({v}t)" for k, v in BUILD_TURNS.items())
    return "\n".join([f"{g.city_line(cid)} · can build: {options}" for cid in ids] or ["no cities yet"]) + "\n" + g.footer()


@tool
def set_production(city: str, item: str) -> str:
    """Set what a city builds."""
    g = _live()
    c = _city(g, city)
    if item not in BUILD_TURNS:
        raise GameError("unknown_item", f"{c['name']} cannot build {item!r}. Options: {', '.join(BUILD_TURNS)}.")
    c["producing"], c["progress"] = item, 0
    return f"{c['name']} now builds {item} ({BUILD_TURNS[item]}t).\n{g.footer()}"


@tool
def research(tech: str | None = None) -> str:
    """Without a tech: what can be researched, with turns. With a tech: research it."""
    g = STATE.game
    available = [t for t in TECHS if t not in g.techs]
    if tech is None:
        return "\n".join(f"{t}: {TECHS[t]} turns" for t in available) or "everything is known"
    g = _live()
    if tech in g.techs:
        raise GameError("already_known", f"{tech} is already known. Available: {', '.join(available)}.")
    if tech not in TECHS:
        raise GameError("unknown_tech", f"unknown tech {tech!r}. Available: {', '.join(available)}.")
    g.research, g.beakers = tech, 0
    return f"Researching {tech} ({TECHS[tech]}t).\n{g.footer()}"


@tool
def end_turn(skip_idle: bool = False, until_attention: bool = False, max_turns: int = 5) -> str:
    """End the turn. Blocked while something needs orders unless skip_idle (idle units hold). until_attention
    keeps ending turns (up to max_turns) until something needs you. Returns the turn report and the next brief."""
    g = _live()
    if (blockers := g.blockers()) and not skip_idle:
        return f"END TURN BLOCKED (T{g.turn}) — {len(blockers)} pending:\n" + "\n".join(f"  {b}" for b in blockers) \
            + "\nor end_turn(skip_idle=true) to hold idle units.\n" + g.footer()
    start, events = g.turn, []
    while True:
        for uid in g.needs_orders():
            g.units[uid]["order"] = "hold"
        g.advance()
        events += g.events
        if g.over or not until_attention or g.turn - start >= max(1, min(max_turns, 20)) or g.blockers():
            break
    g.events = events
    if g.over:
        sc, base = g.score(), STATE.baselines()
        return (f"GAME OVER at T{g.turn}/{g.limit}. Final score {sc['total']} (cities {sc['cities']}, pop {sc['pop']}, "
                f"tiles {sc['tiles']}, techs {sc['techs']}); null baseline {base['null']['final']}, "
                f"built-in AI {base['engine_ai']['final']}.")
    return f"Ended T{start}" + (f"–T{g.turn - 1}" if g.turn - start > 1 else "") + ".\n" + brief(g) + "\n" + g.footer()


@tool
def plan(text: str | None = None) -> str:
    """Read your plan, or replace it (at most 1,000 characters). Every brief shows it."""
    g = STATE.game
    if text is not None:
        g.plan = text[:1000]
    return f"PLAN: {g.plan or '(none)'}"


# --- AgentEnv card, data plane and extensions ---------------------------------------------------

def _ext(uri: str, op: str, props: dict) -> dict:
    return {"uri": uri, "description": op, "required": None,
            "params": {"endpoint": f"/agentenv/ext/{op}",
                       "methods": {op: {"method": "POST", "request": {"type": "object", "properties": props}}}}}


@mcp.custom_route("/.well-known/agent-env.json", methods=["GET"])
async def card(request: Request) -> JSONResponse:
    tools = [{"name": t.name, "description": t.description, "inputSchema": t.inputSchema} for t in await mcp.list_tools()]
    return JSONResponse({
        "name": os.environ.get("ENVIRONMENT_NAME", NAME), "protocolVersion": "1.0", "url": "/agentenv",
        "preferredTransport": "JSONRPC", "additionalInterfaces": [{"url": "/mcp", "transport": "mcp"}],
        "capabilities": {"operations": ["data/reset", "data/add", "data/get"], "tools": tools, "extensions": [
            _ext("urn:openciv3:new-game/v1", "new_game", {"seed": {"type": "integer"}, "turn_limit": {"type": "integer"}}),
            _ext("urn:openciv3:autoplay/v1", "autoplay", {"turns": {"type": "integer"},
                                                         "policy": {"enum": ["null", "found_capital", "engine_ai"]}})]}})


@mcp.custom_route("/agentenv", methods=["POST"])
async def data_plane(request: Request) -> JSONResponse:
    req = await request.json()
    rid, method, params = req.get("id"), req.get("method"), req.get("params") or {}
    if method == "data/reset":
        STATE.new_game()
        result = {}
    elif method == "data/add":
        for part in params.get("parts") or []:
            STATE.scenario |= (part.get("data") or {}).get("scenario") or {}
        STATE.new_game()
        result = {}
    elif method == "data/get":
        result = {"parts": [{"kind": "data", "data": STATE.summary()}]}
    else:
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}})
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result})


@mcp.custom_route("/agentenv/ext/new_game", methods=["POST"])
async def new_game(request: Request) -> JSONResponse:
    STATE.scenario |= await request.json()
    STATE.new_game()
    return JSONResponse({"turn": STATE.game.turn, "turn_limit": STATE.game.limit, "seed": STATE.game.seed})


@mcp.custom_route("/agentenv/ext/autoplay", methods=["POST"])
async def autoplay_ext(request: Request) -> JSONResponse:
    body = await request.json()
    autoplay(STATE.game, int(body.get("turns", 1)), body.get("policy", "null"))
    g = STATE.game
    return JSONResponse({"turn": g.turn, "game_over": g.over, "defeated": False, "score": g.score()})


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
