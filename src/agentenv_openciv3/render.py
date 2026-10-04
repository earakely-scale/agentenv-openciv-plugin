"""Compact text for the agent, rendered from CivBridge results (docs/protocol.md)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from .bridge import BridgeError

URGENT = {"threat", "unit_lost", "war_declared", "city_destroyed", "disorder", "disorder_started", "city_starved",
          "settle_failed", "goto_blocked", "gold_stolen", "defenseless", "riot_risk", "engine_restarted",
          "peace_offered", "city_lost", "city_captured"}
# Kept first when a turn has more events than fit; threats go last, they repeat the most.
FIRST = {"city_founded", "unit_lost", "city_destroyed", "civ_destroyed", "war_declared", "disorder", "disorder_started",
         "gold_stolen", "defenseless", "tech_learned", "city_starved", "settle_failed", "goto_blocked",
         "engine_restarted", "peace_offered", "peace_signed", "government_picked", "city_lost", "city_captured"}
CITY_TARGETS = ((1, 1), (15, 2), (30, 3), (45, 4), (60, 5), (80, 6), (100, 7))
TECHS_LEARNED_TARGETS = ((20, 2), (40, 4), (60, 6), (80, 8), (100, 10))
TERRAIN = {"grassland": "g", "plains": "p", "desert": "d", "tundra": "t", "floodplain": "f", "hills": "h",
           "mountains": "m", "forest": "F", "jungle": "j", "marsh": "s", "volcano": "v", "coast": "~",
           "sea": "~", "ocean": "~"}
TERRAIN_NAMES = {"g": "grassland", "p": "plains", "d": "desert", "t": "tundra", "f": "flood plain", "h": "hills",
                 "m": "mountains", "F": "forest", "j": "jungle", "s": "marsh", "v": "volcano", "~": "water"}
BASELINE_LABELS = {"engine_ai": "built-in AI", "settler_bot": "settler bot", "null": "do-nothing"}
MAX_UNIT_LINES, MAX_STANDING, MAX_CITY_LINES, MAX_EVENTS, MAX_AUTO, MAX_MESSAGES = 5, 6, 6, 5, 3, 12
MAX_DETAILED_CITIES, MAX_PICKS, MAX_BATCH_LINES = 4, 8, 30
# The characters of messages a brief lists at most (about 300 tokens; the oldest are left out first): every brief
# repeats them, and the seat has read each one in full once already.
MESSAGE_CHARS = 1200
# An item is delivered only when the city is bigger than its population cost (Settler 2, Worker 1 in the ruleset).
MIN_SIZE = {"Settler": 3, "Worker": 2}
IDLE_GOLD = 100
# Governments that rush production with population, not gold (Civ III rules).
FORCED_LABOUR = {"Despotism", "Communism", "Anarchy"}


def pos(o: dict) -> str:
    return f"({o['x']},{o['y']})"


def rel(o: dict) -> str:
    if o.get("dir") in (None, "here") or not o.get("dist"):
        return "here"
    return f"{o['dist']} {o['dir']}"


def num(v: float) -> str:
    return f"{round(v, 2):g}"


def call(tool: str, **args: Any) -> str:
    return f"{tool}(" + ", ".join(f"{k}={json.dumps(v)}" for k, v in args.items()) + ")"


def score_text(s: dict, short: bool = False) -> str:
    if short:
        cities = f"{s['cities']} cit{'y' if s['cities'] == 1 else 'ies'}"
        return f"{s['total']} ({cities}, pop {s['pop']}, techs {s['techs']})"
    return f"{s['total']} (cities {s['cities']}, pop {s['pop']}, tiles {s['tiles']}, techs {s['techs']})"


def footer(state: dict) -> str:
    turn = f"T{state['turn']}/{state['turn_limit']}"
    if state.get("game_over") or state.get("defeated"):
        return f"[GAME OVER {turn}]"
    pending = dict.fromkeys(b.get("id") or "research" for b in state.get("blockers", []))
    return f"[{turn} · " + (f"needs orders: {', '.join(pending)}]" if pending else "nothing needs orders]")


# ---- units ----

def status_text(u: dict) -> str:
    status = u.get("status") or "idle"
    if status in ("goto", "settle") and u.get("target"):
        return f"{status}→{pos(u['target'])} {rel(u['target'])}"
    if status.startswith("working:"):
        return "working " + status.split(":", 1)[1]
    return {"exploring": "exploring (auto)", "auto_work": "auto-working", "done": "no moves left"}.get(status, status)


def found_text(u: dict) -> str | None:
    if not {"settle", "found_city"} & set(u.get("orders", [])) or not u.get("can_found_city"):
        return None
    cf = u["can_found_city"]
    return "found here: yes" if cf.get("ok") else f"found here: no — {cf.get('reason', 'not allowed')}"


def unit_line(u: dict, detail: bool = False) -> str:
    parts = [f"{u['id']} {u['type']} {pos(u)} {num(u.get('moves_left', 0))}/{num(u.get('moves_max', 0))}mv",
             status_text(u)]
    if u.get("hp") is not None and u.get("hp_max") and u["hp"] < u["hp_max"]:
        parts.append(f"hp {u['hp']}/{u['hp_max']}")
    if detail and u.get("orders"):
        parts.append("orders: " + " ".join(u["orders"]))
    if found := found_text(u):
        parts.append(found)
    if targets := u.get("attack_targets"):
        parts.append("attack: " + ", ".join(target_text(t) for t in targets))
    if up := u.get("upgrade"):
        parts.append(upgrade_text(up, detail))
    return " · ".join(parts)


def upgrade_text(up: dict, detail: bool = False) -> str:
    text = f"upgrade → {up['to']} {up['gold']}g"
    if up.get("ok"):
        return text
    return text + (f" (not now: {up['reason']})" if detail and up.get("reason") else " (not now)")


def target_text(t: dict) -> str:
    city = f"in {t['city']}" if t.get("city") else ""
    what = " ".join(x for x in (t.get("owner"), t.get("defender") or "undefended", city) if x)
    return f"({t['x']},{t['y']}) {t.get('dir', '')} {what} {round(100 * t.get('win_chance', 0))}% to win"


def units_list(state: dict, everything: bool, kind: str | None = None) -> str:
    units = state.get("units", [])
    if kind:
        units = [u for u in units if u.get("type", "").lower() == kind.strip().lower()]
    shown = units if everything else [u for u in units if u.get("needs_orders")]
    of = f" {kind.strip()}" if kind else ""
    head = (f"UNITS{of} (all {len(units)}) T{state['turn']}" if everything
            else f"UNITS{of} needing orders ({len(shown)} of {len(units)}) T{state['turn']}")
    if not shown:
        rest = "" if everything else " — list_units(filter=\"all\") shows the rest"
        return f"{head}\nnone{rest}"
    lines = [head, *(unit_line(u, detail=True) for u in shown)]
    if not everything and len(shown) > MAX_UNIT_LINES:
        lines.append("many at once → " + batch_hint(shown))
    return "\n".join(lines)


def idle_groups(units: list[dict]) -> list[tuple[str, int]]:
    """(type, count) of units, the commonest first."""
    counts: dict[str, int] = {}
    for u in units:
        counts[u.get("type", "?")] = counts.get(u.get("type", "?"), 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def batch_hint(units: list[dict]) -> str:
    """A unit_orders call for idle units, by type: workers work, settlers settle, the rest fortify."""
    def order(t: str) -> str:
        return "auto_work" if t == "Worker" else "explore" if t in ("Scout", "Explorer") else "fortify"
    groups = [t for t, _ in idle_groups(units) if t != "Settler"][:2]
    if not groups:
        return 'unit_orders(orders=[{"unit": "idle", "order": "..."}])'
    return "unit_orders(orders=[" + ", ".join(f'{{"unit": "idle:{t}", "order": "{order(t)}"}}' for t in groups) + "])"


def batch_lines(results: list[dict], key: str) -> list[str]:
    """One line per result of a batch (unit_orders, set_production for many cities), failures marked."""
    lines = [f"  {r[key]}: {'' if r.get('ok') else '✗ '}{r.get('message', '')}".rstrip()
             for r in results[:MAX_BATCH_LINES]]
    if len(results) > MAX_BATCH_LINES:
        lines.append(f"  +{len(results) - MAX_BATCH_LINES} more")
    return lines


def is_military(u: dict) -> bool:
    return "fortify" in u.get("orders", [])


# ---- cities ----

def stalled(c: dict) -> str:
    """Why a city makes no progress on its item."""
    if c.get("disorder"):
        return "city in disorder"
    if not c.get("shields_per_turn"):
        return "0 shields/t"
    return "waits for the city to grow"


def production_text(c: dict) -> str:
    item = c.get("producing")
    if not item:
        return "NOTHING in production"
    if not c.get("production_cost"):
        return f"{item} (shields become gold)" if item == "Wealth" else item
    text = f"{item} {c.get('production_stored', 0)}/{c['production_cost']}"
    eta, need = c.get("turns_to_complete"), MIN_SIZE.get(item, 0)
    if c.get("capped"):
        text += f" FULL, waits for size {need}" + (f" → {eta}t" if eta else "")
    elif eta:
        text += f" → {eta}t" + (f" (delivered at size {need})" if c.get("size", 0) < need else "")
    else:
        text += f" no progress ({stalled(c)})"
    if c.get("producing_source") == "engine":
        text += " (engine pick)"
    return text


def growth_text(c: dict) -> str:
    fpt = c.get("food_per_turn", 0)
    if fpt < 0:
        return f"!! {fpt}/t starving"
    eta = c.get("turns_to_grow")
    return f"{fpt:+d}/t" + (f" grows {eta}t" if eta else " not growing")


def city_flags(c: dict) -> list[str]:
    flags = []
    if c.get("disorder"):
        flags.append("!! DISORDER")
    elif c.get("riot_risk"):
        flags.append("riot risk")
    if c.get("defenders") == 0:
        flags.append("no defender")
    return flags


def city_line(c: dict) -> str:
    parts = [f"{c['id']} {c['name']} {pos(c)} size {c['size']}", "food " + growth_text(c), production_text(c)]
    if c.get("queue"):
        parts.append("then " + ", ".join(c["queue"]))
    return " · ".join(parts + city_flags(c))


def needs_look(c: dict) -> bool:
    """A city whose line the brief keeps: an engine pick or nothing to build, disorder or its risk, starving, full
    production, or no defender."""
    return (not c.get("producing") or c.get("producing_source") == "engine" or bool(city_flags(c))
            or c.get("food_per_turn", 0) < 0 or bool(c.get("capped")))


def cities_table(state: dict) -> str:
    """Every city in one line each, those that need a look first."""
    cities = sorted(state.get("cities", []), key=lambda c: not needs_look(c))
    pending = sum(1 for c in cities if not c.get("producing") or c.get("producing_source") == "engine")
    lines = [f"CITIES ({len(cities)}, {pending} waiting on a production choice) T{state['turn']}"]
    lines += ["  " + city_line(c) for c in cities]
    lines.append('city_info(city="c1") lists what one city can build · set_production(city="pending", item=..., '
                 'then=[...]) sets every waiting city at once')
    return "\n".join(lines)


def mood_text(c: dict) -> str | None:
    if c.get("unhappy") is None:
        return None
    text = f"mood happy {c.get('happy', 0)} content {c.get('content', 0)} unhappy {c['unhappy']}"
    if c.get("defenders") is not None:
        text += f" · defenders {c['defenders']}"
    return text


def option_text(o: dict, c: dict) -> str:
    if o.get("kind") == "wealth":
        return o["name"]
    eta = f"{o['turns']}t" if o.get("turns") is not None else f"no progress: {stalled(c)}"
    return f"{o['name']} {o['cost']} ({eta})"


def city_detail(c: dict) -> str:
    head = [f"{c['id']} {c['name']} {pos(c)} size {c['size']}" + (" capital" if c.get("capital") else ""),
            f"food {c.get('food_stored', 0)}/{c.get('food_needed', '?')} {growth_text(c)}",
            f"shields {c.get('shields_per_turn', 0)}/t"]
    worked = c.get("tiles_worked")
    if worked is not None:
        head.append(f"works {len(worked) if isinstance(worked, list) else worked} tiles")
    lines = [" · ".join(head + city_flags(c))]
    if mood := mood_text(c):
        lines.append(f"  {mood}" + (" · one more citizen riots" if c.get("riot_risk") else ""))
    lines.append(f"  {'producing ' if c.get('producing') else ''}{production_text(c)}")
    if c.get("shields_lost_last_turn"):
        lines.append(f"  {c['shields_lost_last_turn']} shields lost last turn (production full)")
    if c.get("buildings"):
        lines.append("  buildings: " + ", ".join(c["buildings"]))
    opts = [option_text(o, c) for o in c.get("options", [])]
    if opts:
        lines.append("  can build: " + " · ".join(opts))
    return "\n".join(lines)


# ---- fixes ----

def rates_fix(state: dict, luxury: int = 2) -> str | None:
    """A set_rates call that moves up to `luxury` tenths from science to luxury, if the rates allow it."""
    r = state.get("rates") or {}
    sci, lux = r.get("science"), r.get("luxury")
    if sci is None or lux is None:
        return None
    step = min(luxury, sci, r.get("max", 10) - lux)
    return call("set_rates", science=sci - step, luxury=lux + step) if step > 0 else None


def garrison_fix(state: dict, city: dict) -> str | None:
    """Send the nearest military unit that is not guarding a city into `city`."""
    towns = {(c["x"], c["y"]) for c in state.get("cities", [])}
    free = [u for u in state.get("units", []) if is_military(u) and (u["x"], u["y"]) not in towns]
    if not free:
        return None
    u = min(free, key=lambda u: (abs(u["x"] - city["x"]) + abs(u["y"] - city["y"]), u["id"]))
    return call("unit_order", unit=u["id"], order="goto", x=city["x"], y=city["y"]) + " then fortify"


def riots_line(riots: list[dict], state: dict) -> str:
    """Several cities in civil disorder (or about to be) as one line: the luxury rate that calms them all at once."""
    named = [f"{b['id']} {_city(state, b['id']).get('name', '?')} {b.get('unhappy', '?')}:{b.get('happy', '?')}"
             + ("" if b.get("now", True) else " at turn end") for b in riots[:MAX_PICKS]]
    more = f", +{len(riots) - MAX_PICKS} more" if len(riots) > MAX_PICKS else ""
    fixes = []
    if needed := [b["luxury"] for b in riots if b.get("luxury")]:
        luxury = max(needed)
        science = min((state.get("rates") or {}).get("science", 0), 10 - luxury)
        stubborn = [b["id"] for b in riots if not b.get("luxury")]
        fixes.append(call("set_rates", science=science, luxury=luxury) + " calms "
                     + (f"all but {', '.join(stubborn)}" if stubborn else "them all"))
    fixes.append("each military unit fortified in a city calms one unhappy citizen")
    return (f"!! {len(riots)} cities riot, producing nothing (unhappy:happy citizens): {', '.join(named)}{more} → "
            + "; ".join(fixes) + "; end_turn(skip_idle=true) accepts it")


def disorder_fix(state: dict, city: dict) -> str:
    fixes = [f for f in (rates_fix(state), garrison_fix(state, city)) if f]
    if fixes:
        return " or ".join(fixes)
    return "no free military unit and no room for more luxury: it calms down as it shrinks"


# ---- turn brief ----

def _milestone(targets: Iterable[tuple[int, int]], turn: int, have: int, base: int = 0) -> str:
    done = [(t, n + base) for t, n in targets if t <= turn]
    if done and have < done[-1][1]:
        return f"BEHIND ({done[-1][1]} by T{done[-1][0]})"
    upcoming = [(t, n + base) for t, n in targets if t > turn]
    return f"ok (next: {upcoming[0][1]} by T{upcoming[0][0]})" if upcoming else "ok"


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _race_name(c: dict) -> str:
    return "you" if c.get("you") else c.get("civ") or "an unmet civ"


def _shares(c: dict) -> str:
    return f"{round(100 * c['land'])}%/{round(100 * c['pop'])}%"


def score_race(state: dict) -> str | None:
    """Where the seat stands in the score race (the top score wins at the turn limit)."""
    r = state.get("race")
    if not r or not r.get("you"):
        return None
    lead = r["leader"]
    if r["rank"] == 1:
        return f"{'1st' if lead.get('you') else 'tied 1st'} of {r['civs_left']} civs"
    return f"{ordinal(r['rank'])} of {r['civs_left']} civs ({_race_name(lead)} leads, {lead['score']})"


def domination_race(state: dict) -> str | None:
    """The seat's shares of the land and population, and the nearest civ's, against domination's two thirds."""
    r = state.get("race")
    if not r or not r.get("you"):
        return None
    near = r["nearest_domination"]
    return f"land/pop {_shares(r['you'])}" + ("" if near.get("you") else f", top {_race_name(near)} {_shares(near)}")


def pace_line(state: dict, start_techs: int) -> str:
    s, turn = state["score"], state["turn"]
    return (f"PACE cities {s['cities']} {_milestone(CITY_TARGETS, turn, s['cities'])} · "
            f"techs {s['techs']} {_milestone(TECHS_LEARNED_TARGETS, turn, s['techs'], start_techs)}")


def vs_line(turn: int, baselines: dict[str, dict | str | None]) -> str:
    def text(s):
        return score_text(s, short=True) if isinstance(s, dict) else s or "computing…"
    parts = [f"{BASELINE_LABELS.get(p, p)} {text(s)}" for p, s in baselines.items()]
    return f"VS T{turn} (same seed) " + " · ".join(parts)


def is_urgent(e: dict) -> bool:
    return e.get("kind") in URGENT and not (e.get("kind") == "threat" and "(at peace)" in (e.get("text") or ""))


def event_text(e: dict) -> str:
    text = e.get("text") or e.get("kind", "")
    if "x" in e and "y" in e and pos(e) not in text:
        text = f"{text.rstrip('.')} {pos(e)}"
    return ("!! " if is_urgent(e) else "") + text


def _rank(e: dict) -> int:
    if e.get("kind") == "threat":
        return 2 if is_urgent(e) else 3
    return 0 if e.get("kind") in FIRST else 1


def short_event(e: dict) -> dict:
    """An event as the brief lists it: disorder without its fixes, which NEEDS ORDERS gives while it lasts."""
    if e.get("kind") in ("disorder", "disorder_started") and ": " in e.get("text", ""):
        return {**e, "text": e["text"].split(": ", 1)[0] + "."}
    return e


def events_lines(events: list[dict], cap: int = MAX_EVENTS) -> list[str]:
    """Events grouped by turn; an event repeated on several turns is shown once, at its first turn."""
    seen: dict[tuple, list[int]] = {}
    for e in events:
        seen.setdefault((e.get("kind"), event_text(e)), []).append(e.get("turn", 0))
    unique = list(seen.items())
    keep = sorted(range(len(unique)), key=lambda i: _rank({"kind": unique[i][0][0], "text": unique[i][0][1]}))[:cap]
    by_turn: dict[int, list[str]] = {}
    for i in sorted(keep):
        (_, text), turns = unique[i]
        repeat = f" (T{turns[0]}–T{turns[-1]})" if len(turns) > 1 else ""
        by_turn.setdefault(turns[0], []).append(text + repeat)
    lines = [f"T{t}: " + " · ".join(texts) for t, texts in sorted(by_turn.items())]
    if len(unique) > cap:
        lines.append(f"+{len(unique) - cap} more")
    return lines


def _city(state: dict, cid: str | None) -> dict:
    return next((c for c in state.get("cities", []) if c["id"] == cid), {})


def blocker_line(b: dict, state: dict, site: dict | None = None) -> str:
    kind, bid = b.get("kind"), b.get("id")
    if kind == "idle_unit":
        u = next((u for u in state.get("units", []) if u["id"] == bid), None)
        if u is None:
            return b.get("message", bid)
        line = unit_line(u)
        here = (u.get("can_found_city") or {}).get("ok")
        if site and (site["x"], site["y"]) == (u["x"], u["y"]):
            line, site = line + " (the best site)", None
        if here:
            line += " → " + call("unit_order", unit=bid, order="found_city")
        if site:
            line += f" · best site {pos(site)} {rel(site)} score {num(site['score'])}"
            if not here:
                line += " → " + call("unit_order", unit=bid, order="settle", x=site["x"], y=site["y"])
        elif not here:
            line += " · orders: " + " ".join(u.get("orders", []))
        return line
    message = b.get("message", str(b)).rstrip(".")
    if kind == "no_production":
        return f"{message} → {call('set_production', city=bid, item='...')}"
    if kind == "no_research":
        return f"{message} → research() lists techs"
    if kind == "choose_production":
        item = _city(state, bid).get("producing")
        keep = call("set_production", city=bid, item=item) + " keeps it, " if item else ""
        return f"{message} → {keep}{call('city_info', city=bid)} lists options"
    if kind == "choose_research":
        tech = (state.get("research") or {}).get("current")
        keep = call("research", tech=tech) + " keeps it, " if tech else ""
        return f"{message} → {keep}research() lists options"
    if kind == "disorder":
        return f"!! {message} → {disorder_fix(state, _city(state, bid) or {'x': 0, 'y': 0})}"
    return message


def attention_lines(state: dict) -> list[str]:
    """What needs a look without blocking the turn: riot risk, empty garrisons, full production, idle gold."""
    cities = state.get("cities", [])
    out = []
    risk = [c for c in cities if c.get("riot_risk") and not c.get("disorder")]
    if risk:
        names = ", ".join(f"{c['id']} {c['name']} (defenders {c.get('defenders', '?')})" for c in risk)
        out.append(f"riot risk at the next citizen: {names} → a military unit inside calms one unhappy citizen; "
                   "more luxury with set_rates also helps")
    bare = [f"{c['id']} {c['name']}" for c in cities if c.get("defenders") == 0]
    if bare:
        out.append("no defender: " + ", ".join(bare))
    for c in (c for c in cities if c.get("capped")):
        lost = f", {c['shields_lost_last_turn']} shields lost last turn" if c.get("shields_lost_last_turn") else ""
        out.append(f"{c['id']} {c['name']} {production_text(c)}{lost}")
    if line := upgrades_line(state):
        out.append(line)
    gold = state.get("gold", 0)
    if gold >= IDLE_GOLD:
        hints = [h for h in (science_fix(state),) if h]
        if state.get("government") not in FORCED_LABOUR and cities:
            hints.append('buy(city="...") rushes a city\'s production')
        out.append(f"gold {gold} unspent" + (" → " + " · ".join(hints) if hints else ""))
    return out


def upgrades_line(state: dict) -> str | None:
    """Units in their cities that can upgrade now, by type, with the gold it takes and how many the treasury pays for
    (in unit order, as unit_orders runs them), and the call for the commonest type it pays for."""
    units = state.get("units", [])
    ready = [u for u in units if (u.get("upgrade") or {}).get("ok")]
    if not ready:
        return None
    kinds: dict[tuple[str, str], int] = {}
    for u in ready:
        key = (u["type"], u["upgrade"]["to"])
        kinds[key] = kinds.get(key, 0) + 1
    top = sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))
    named = ", ".join(f"{n} {a}→{b}" for (a, b), n in top[:3]) + (f", +{len(top) - 3} more" if len(top) > 3 else "")
    gold, paid, payable = state.get("gold", 0), 0, []
    for u in ready:
        if paid + u["upgrade"]["gold"] <= gold:
            paid += u["upgrade"]["gold"]
            payable.append(u)
    total = sum(u["upgrade"]["gold"] for u in ready)
    text = f"{len(ready)} unit{'s' if len(ready) > 1 else ''} can upgrade for {total} gold in all ({named})"
    if len(payable) < len(ready):
        text += f"; your {gold} gold pays for {len(payable)} now"
    if not payable:
        return text
    counts: dict[str, int] = {}
    for u in payable:
        counts[u["type"]] = counts.get(u["type"], 0) + 1
    first = min(counts, key=lambda t: (-counts[t], t))
    chosen = [u for u in payable if u["type"] == first]
    # "all:Type" only when it names exactly these units; otherwise their ids (a few), so no order of the call fails.
    if len(chosen) == sum(1 for u in units if u["type"] == first):
        orders = [{"unit": f"all:{first}", "order": "upgrade"}]
    else:
        orders = [{"unit": u["id"], "order": "upgrade"} for u in chosen[:4]]
    return f"{text} → " + call("unit_orders", orders=orders)


def science_fix(state: dict) -> str | None:
    """Move tax into science: a concrete call when the government's maximum rate is known."""
    r = state.get("rates") or {}
    sci, lux = r.get("science"), r.get("luxury", 0)
    if sci is None:
        return None
    if "max" not in r:
        return 'set_rates(science=..., luxury=...) moves tax into research'
    top = min(r["max"], 10 - lux)
    return call("set_rates", science=top, luxury=lux) + " moves tax into research" if top > sci else None


def rates_text(state: dict) -> str | None:
    r = state.get("rates")
    if not r:
        return None
    return f"tax {r.get('tax', 0) * 10}% sci {r.get('science', 0) * 10}% lux {r.get('luxury', 0) * 10}%"


def brief(state: dict, *, start_techs: int, plan: str | None = None, plan_turn: int | None = None,
          baselines: dict[str, dict | str | None] | None = None, sites: dict[str, dict] | None = None,
          events: bool = True, notices: list[dict] | None = None, messages: list[str] | None = None) -> str:
    s = state
    head = [f"T{s['turn']}/{s['turn_limit']}" + (f" ({s['date']})" if s.get("date") else ""), s.get("civ", "?"),
            s.get("government", "?")]
    if s.get("anarchy_until"):
        head.append(f"anarchy until T{s['anarchy_until']}")
    head.append(f"gold {s.get('gold', 0)} ({s.get('gold_per_turn', 0):+d}/t)")
    if rates := rates_text(s):
        head.append(rates)
    wars = [r["civ"] + (" (offers peace)" if r.get("peace_offered")
                        else f" (peace: {r['peace_price']} gold)" if r.get("peace_price") is not None else "")
            for r in s.get("rivals", []) if r.get("at_war")]
    if wars:
        head.append("!! at war with " + ", ".join(wars))
    if s.get("defeated"):
        head.append("DEFEATED")
    lines = [" · ".join(head)]
    lines += [f"!! {n['text']}" for n in notices or []]
    if line := match_line(s):
        lines.append(line)

    r = s.get("research") or {}
    if r.get("current"):
        then = [t for t in r.get("queue", []) if t != r["current"]]
        lines.append(f"RESEARCH {r['current']} {r.get('beakers', 0)}/{r.get('cost', '?')}"
                     + (f" → {r['turns_left']}t" if r.get("turns_left") else " no progress (0 beakers/t)")
                     + (f" (then {', '.join(then[:3])})" if then else "")
                     + (" (engine pick)" if r.get("source") == "engine" else ""))
    else:
        lines.append("RESEARCH none — research() lists techs")
    if s.get("revolution_target"):
        lines.append(f"GOVERNMENT anarchy, then {s['revolution_target']}")
    elif s.get("governments"):
        penalty = ": -1 on any tile yield above 2" if s.get("tile_penalty") else ""
        options = "; ".join(f"{g['name']} ({government_traits(g)})" for g in s["governments"])
        better = next((g for g in s["governments"] if not g.get("tile_penalty")), s["governments"][0])
        lines.append(f"GOVERNMENT {s.get('government')}{penalty} · can choose {options} → "
                     + call("revolution", government=better["name"]) + " after a few turns of anarchy")
    rank = score_race(s)
    lines.append(f"SCORE {score_text(s['score'])}" + (f" · {rank}" if rank else "")
                 + f" · explored {num(s.get('explored_pct', 0))}%")
    lines.append(pace_line(s, start_techs) + (f" · {d}" if (d := domination_race(s)) else ""))
    if baselines:
        lines.append(vs_line(s["turn"], baselines))

    attention = attention_lines(s)
    blockers = s.get("blockers", [])
    if blockers:
        lines.append(f"NEEDS ORDERS ({len(blockers)})")
        units = [b for b in blockers if b.get("kind") == "idle_unit"]
        picks = [b for b in blockers if b.get("kind") == "choose_production"]
        grouped = picks if len(picks) > 2 else []
        riots = [b for b in blockers if b.get("kind") == "disorder"]
        riots = riots if len(riots) > 2 else []
        if riots:
            lines.append("  " + riots_line(riots, s))
        for b in [b for b in blockers if b.get("kind") != "idle_unit" and b not in grouped and b not in riots] \
                + units[:MAX_UNIT_LINES]:
            lines.append("  " + blocker_line(b, s, (sites or {}).get(b.get("id"))))
        if grouped:
            named = [f"{b['id']} {_city(s, b['id']).get('name', '?')}: {_city(s, b['id']).get('producing', '?')}"
                     for b in grouped[:MAX_PICKS]]
            more = f", +{len(grouped) - MAX_PICKS} more" if len(grouped) > MAX_PICKS else ""
            lines.append(f"  {len(grouped)} cities: the engine picked their next item ({', '.join(named)}{more}) → "
                         'set_production(city="pending", item="...", then=[...]) sets them all; a queue (then) '
                         "saves the choice after each completion")
        if len(units) > MAX_UNIT_LINES:
            idle = [u for u in s.get("units", []) if u.get("needs_orders")]
            kinds = ", ".join(f"{n} {t}" for t, n in idle_groups(idle))
            lines.append(f"  +{len(units) - MAX_UNIT_LINES} more idle units ({kinds} in all) → list_units() · "
                         + batch_hint(idle))
        if any(b.get("kind") in ("choose_production", "choose_research") for b in blockers):
            lines.append("  end_turn(skip_idle=true) accepts the engine's picks and holds idle units")
    elif attention:
        lines.append("NEEDS ORDERS none — see ATTENTION, then end_turn()")
    else:
        lines.append("NEEDS ORDERS none — end_turn() or end_turn(until_attention=true)")
    if attention:
        lines.append("ATTENTION")
        lines += ["  " + line for line in attention]

    standing = [u for u in s.get("units", []) if not u.get("needs_orders") and u.get("status") not in ("idle", "done")]
    if standing:
        items = [f"{u['id']} {u['type']} {status_text(u)}" for u in standing[:MAX_STANDING]]
        if len(standing) > MAX_STANDING:
            items.append(f"+{len(standing) - MAX_STANDING} more")
        lines.append("STANDING " + " · ".join(items))

    cities = s.get("cities", [])
    if cities:
        lines.append("CITIES")
        # the cities that need a look first (an engine pick, disorder, starving, ...), then the rest in order
        shown = sorted(cities, key=lambda c: not needs_look(c))[:MAX_CITY_LINES] if len(cities) > MAX_CITY_LINES \
            else cities
        lines += ["  " + city_line(c) for c in shown]
        if len(cities) > MAX_CITY_LINES:
            lines.append(f"  +{len(cities) - MAX_CITY_LINES} more → city_info()")
    else:
        lines.append("CITIES none yet — found one: find_city_sites(), then unit_order(order=\"settle\", ...)")

    if events and s.get("last_events"):
        lines.append("EVENTS " + " | ".join(events_lines([short_event(e) for e in s["last_events"]])))
    if messages:
        shown = messages[-MAX_MESSAGES:]
        while len(shown) > 1 and sum(map(len, shown)) > MESSAGE_CHARS:
            shown = shown[1:]
        lines.append("MESSAGES" + (f" (the last {len(shown)} of {len(messages)})"
                                   if len(shown) < len(messages) else ""))
        lines += ["  " + m for m in shown]
    lines.append(f"PLAN (T{plan_turn}) {plan}" if plan else "PLAN none — record your strategy with plan(text=...)")
    return "\n".join(lines)


# ---- government and diplomacy ----

def government_traits(o: dict) -> str:
    traits = ["tile penalty" if o.get("tile_penalty") else "no tile penalty",
              "+1 commerce on tiles with commerce" if o.get("trade_bonus") else None,
              f"hurry with {o['hurry']}" if o.get("hurry") and o["hurry"] != "none" else "no hurrying",
              f"corruption {o['corruption']}" if o.get("corruption") else None]
    return ", ".join(t for t in traits if t)


def governments(g: dict) -> str:
    head = f"Government: {g.get('current', '?')}"
    head += f", anarchy until T{g['anarchy_until']}" if g.get("anarchy_until") else ""
    head += f", then {g['revolution_target']}" if g.get("revolution_target") else ""
    lines = [head]
    for o in g.get("available", []):
        free = f", {o['free_units_per_city']} free units per city" if o.get("free_units_per_city") else ""
        lines.append(f"  {o['name']}: {government_traits(o)}{free}")
    return "\n".join(lines)


def offer_text(o: dict) -> str:
    return (f" with {o['gold']} gold" if o.get("gold") else "") + f", until T{o['until_turn']}"


def civ_line(c: dict) -> str:
    sc = c.get("score") or {}
    if c.get("at_war") and c.get("agent"):
        terms = (f"offers peace{offer_text(c['peace_offered'])}: accept with "
                 + call("diplomacy", action="propose_peace", civ=c["civ"]) if c.get("peace_offered")
                 else f"you offered peace{offer_text(c['you_offered'])}" if c.get("you_offered")
                 else "peace when both propose it")
        relation = f"AT WAR ({terms})"
    elif c.get("at_war"):
        terms = (f"talks refused until T{c['refuses_talks_until']}" if c.get("refuses_talks_until")
                 else f"peace for {c['peace_price']} gold" if c.get("peace_price") is not None else "no peace yet")
        relation = f"AT WAR ({terms})"
    else:
        relation = "at peace"
    score = f"score {sc.get('total', '?')} ({sc.get('cities', '?')} cities, {sc.get('techs', '?')} techs)"
    parts = [c["civ"] + (" (another agent)" if c.get("agent") else ""), relation, score, c.get("government")]
    if c.get("military_vs_yours") is not None:
        parts.append(f"military {num(c['military_vs_yours'])}× yours")
    if c.get("at_war_with"):
        parts.append("at war with " + ", ".join(c["at_war_with"]))
    return " · ".join(p for p in parts if p)


def diplomacy(d: dict) -> str:
    civs = d.get("civs", [])
    lines = [f"CIVILIZATIONS you know ({len(civs)}; {d.get('unmet', 0)} not met yet)"]
    lines += ["  " + civ_line(c) for c in civs] or ["  none yet: explore to meet them"]
    if any(c.get("at_war") and not c.get("agent") for c in civs):
        lines.append('Peace: diplomacy(action="propose_peace", civ="...", gold=...) at the price above.')
    if any(c.get("at_war") and c.get("agent") for c in civs):
        lines.append("Peace with another agent's civ: both propose it (" + call("diplomacy", action="propose_peace",
                     civ="...") + "), the second within a turn of the first; each pays the gold it offers.")
    return "\n".join(lines)


# ---- end turn ----

def auto_lines(autos: list[dict]) -> list[str]:
    trades = [a for a in autos if a.get("kind") == "trade_declined"]
    others = [a for a in autos if a.get("kind") != "trade_declined"]
    lines = [f"auto: {a.get('text') or a.get('kind')}" for a in others + trades[:MAX_AUTO]]
    if len(trades) > MAX_AUTO:
        lines.append(f"auto: +{len(trades) - MAX_AUTO} more trade offers declined (the env declines every offer)")
    return lines


def turn_report(result: dict, prev_turn: int) -> str:
    n = result.get("turns_advanced", 0)
    lines = [f"TURN T{prev_turn} → T{result['turn']} ({n} turn{'s' if n != 1 else ''})"]
    lines += ["  " + line for line in events_lines(result.get("events", []), cap=15)]
    lines += ["  " + line for line in auto_lines(result.get("auto", []))]
    return "\n".join(lines)


def waiting(turn: int, others: list[str], seconds: int) -> str:
    who = f"{', '.join(others)} {'is' if len(others) == 1 else 'are'}" if others else "the others are"
    return (f"WAITING — you ended turn T{turn}; {who} still playing it (waited {seconds // 60} min). The turn "
            "advances once every civilization has ended it: call end_turn() again to keep waiting.")


def blocked(result: dict, state: dict) -> str:
    blockers = result.get("blockers", [])
    plural = "s" if len(blockers) != 1 else ""
    lines = [f"END TURN BLOCKED (T{state['turn']}) — {len(blockers)} decision{plural} pending:"]
    for b in blockers:
        kind, bid = b.get("kind"), b.get("id")
        if kind == "idle_unit":
            u = next((u for u in state.get("units", []) if u["id"] == bid), {})
            hint = (call("unit_order", unit=bid, order="...") + " — valid: " + ", ".join(u.get("orders", []))
                    if u else "list_units()")
        elif kind == "no_production":
            hint = call("set_production", city=bid, item="...") + f" — options: {call('city_info', city=bid)}"
        elif kind == "no_research":
            hint = 'research(tech="...") — options: research()'
        elif kind in ("choose_production", "choose_research", "disorder"):
            hint = blocker_line(b, state).split(" → ", 1)[-1]
        else:
            hint = "get_turn_brief()"
        lines.append(f"  {b.get('message', kind).rstrip('.')} → {hint}")
    lines.append("Or end_turn(skip_idle=true): idle units hold this turn, research is auto-picked and the engine's "
                 "production and research picks are accepted.")
    return "\n".join(lines)


def match_line(s: dict) -> str | None:
    """The rules of a game other agents play too, so a prompt need not explain them."""
    agents = [r["civ"] for r in s.get("rivals", []) if r.get("agent")]
    if not agents:
        return None
    ai = [r["civ"] for r in s.get("rivals", []) if not r.get("agent")]
    return (f"MATCH vs agents {', '.join(agents)}" + (f" and the AI's {', '.join(ai)}" if ai else "")
            + " · every agent plays each turn at once; end_turn waits for the others"
            + f" · it ends at T{s['turn_limit']}, or once one agent's civilization is the last an agent plays"
            + " (conquest) or any civ holds 2/3 of the world's land and population (domination); else the top"
            + " score wins"
            + " · message() talks to the other agents")


def game_over(state: dict, baselines: dict[str, dict | str | None] | None) -> str:
    s = state
    v = s.get("victory")
    why = (f"{'you' if v['civ'] == s.get('civ') else v['civ']} won "
           f"{'on score' if v['kind'] == 'score' else 'by ' + v['kind']} on T{v['turn']}" if v
           else f"your civilization was destroyed (T{s['turn']})" if s.get("defeated")
           else f"turn {s['turn']}/{s['turn_limit']} reached")
    lines = [f"GAME OVER — {why}.",
             f"FINAL score {score_text(s['score'])} · units {len(s.get('units', []))} · gold {s.get('gold', 0)}"
             f" · explored {num(s.get('explored_pct', 0))}%"]
    if baselines:
        lines.append(vs_line(s["turn"], baselines))
    lines.append("The game is over; no further actions are possible.")
    return "\n".join(lines)


def leader(civ: str, label: str | None) -> str:
    """A leader in a match, as messages name it: `Greece (sonnet)`."""
    return f"{civ} ({label})" if label else civ


def message_line(sender: str, recipient: str, text: str) -> str:
    """A message as its reader sees it: `Greece (sonnet) to you: "…"`, `you to all: "…"`."""
    return f'{sender} to {recipient}: "{text}"'


# ---- research and rates ----

def techs_list(t: dict) -> str:
    known = t.get("known", [])
    cur = f"{t['current']}" + (f" → {t['turns_left']}t" if t.get("turns_left") else "") if t.get("current") else "none"
    lines = [f"RESEARCH current: {cur} · known {len(known)}: {', '.join(known)}", "AVAILABLE"]
    for a in t.get("available", []):
        unlocks = f" → {', '.join(a['unlocks'])}" if a.get("unlocks") else ""
        eta = f"{a['turns']}t" if a.get("turns") is not None else "no progress yet (0 beakers/t)"
        lines.append(f"  {a['name']} {a.get('cost', '?')} beakers {eta}{unlocks}")
    lines.append('research(tech="...") sets it; a later tech is accepted too, with its prerequisites queued first.')
    return "\n".join(lines)


def rates_result(res: dict) -> str:
    r = res.get("rates") or {}
    parts = [f"tax {r.get('tax', 0) * 10}% · science {r.get('science', 0) * 10}% · luxury {r.get('luxury', 0) * 10}%"]
    if res.get("gold_per_turn") is not None:
        parts.append(f"gold {res['gold_per_turn']:+d}/t")
    if res.get("turns_left_research") is not None:
        parts.append(f"research {res['turns_left_research']}t")
    return " · ".join(parts)


# ---- city sites ----

def site_text(s: dict, turn: int | None = None, turn_limit: int | None = None) -> str:
    y = s.get("yield") or {}
    traits = [t for t, on in (("river", s.get("river")), ("coast", s.get("coastal"))) if on]
    parts = [f"{pos(s)} score {num(s['score'])}", rel(s)]
    if s.get("turns") is not None:
        parts.append(f"{s['turns']}t away")
    if y:
        parts.append(f"area food {y.get('food')} shields {y.get('shields')} commerce {y.get('commerce')}")
    if s.get("terrain"):
        parts.append(s["terrain"].lower())
    if late := arrives_late(s, turn, turn_limit):
        traits.append(late)
    return " · ".join(parts + traits)


def arrives_late(site: dict, turn: int | None, turn_limit: int | None) -> str | None:
    if site.get("turns") is None or turn is None or turn_limit is None or turn + site["turns"] < turn_limit:
        return None
    return f"arrives T{turn + site['turns']}, too late for the turn limit"


def sites_list(result: dict, unit: dict | None, turn: int | None = None, turn_limit: int | None = None) -> str:
    origin = result.get("origin") or {}
    who = f"{unit['id']} {unit['type']} at {pos(origin)}" if unit else pos(origin)
    sites = result.get("sites", [])
    lines = [f"CITY SITES from {who}" + (f" ({result['note']})" if result.get("note") else "")]
    ranked = {(s["x"], s["y"]) for s in sites}
    nearby = [s for s in result.get("nearby") or [] if (s["x"], s["y"]) not in ranked]
    if not sites and not nearby:
        lines.append("none found — explore more: unit_order(unit=..., order=\"explore\")")
        return "\n".join(lines)
    lines += [f"#{i} {site_text(s, turn, turn_limit)}" for i, s in enumerate(sites, 1)]
    if nearby:
        lines.append("nearby legal sites (within 4 tiles): " + " · ".join(
            f"{pos(s)} score {num(s['score'])} {rel(s)}" for s in nearby[:8]))
    if unit and "settle" not in unit.get("orders", []):
        lines.append(f"{unit['id']} {unit['type']} cannot found cities; sites are ranked from where it stands.")
    elif unit and (best := next((s for s in sites if not arrives_late(s, turn, turn_limit)), None)):
        lines.append("Do this: " + call("unit_order", unit=unit["id"], order="settle", x=best["x"], y=best["y"])
                     + "  # walks there and founds the city on arrival")
    return "\n".join(lines)


# ---- map ----

def _glyph(t: dict) -> str:
    name = (t.get("overlay") or t.get("terrain") or "?").lower().replace(" ", "")
    return TERRAIN.get(name, name[:1] or "?")


def _occupant(t: dict) -> str:
    if t.get("city"):
        return "@" if t["city"].get("id") else "C"
    units = t.get("units") or []
    if any("id" not in u for u in units):
        return "!"
    return "#" if units else " "


def map_view(m: dict, *, label: str, width: int | None = None, wrap_x: bool = False,
             sites: list[dict] | None = None, hostile: Iterable[str] = ("Barbarians",)) -> str:
    cx, cy = m["center"]["x"], m["center"]["y"]

    def dx_of(x: int) -> int:
        d = x - cx
        if wrap_x and width:
            d = (d + width // 2) % width - width // 2
        return d

    cells = {(dx_of(t["x"]), t["y"] - cy): t for t in m.get("tiles", [])}
    lines = [f"MAP {label}, radius {m.get('radius')} — explored tiles only"]
    if not cells:
        return "\n".join(lines + ["nothing explored here"])
    dxs, dys = [k[0] for k in cells], [k[1] for k in cells]
    x0, x1, y0, y1 = min(dxs + [0]), max(dxs + [0]), min(dys + [0]), max(dys + [0])
    xs = range(x0, x1 + 1)

    def xlabel(dx: int) -> int:
        return (cx + dx) % width if wrap_x and width else cx + dx

    lines.append("   y\\x" + "".join(f"{xlabel(d):>3}" for d in xs))
    used: set[str] = set()
    for dy in range(y0, y1 + 1):
        row = []
        for dx in xs:
            t = cells.get((dx, dy))
            if t is None:
                row.append("   ")
                continue
            g = _glyph(t)
            used.add(g)
            mark = "*" if t.get("resource") else "'" if t.get("river") else " "
            row.append(f"{_occupant(t)}{g}{mark}")
        lines.append(f"{cy + dy:>5} " + "".join(row).rstrip())
    terrain = " ".join(f"{g} {TERRAIN_NAMES.get(g, '?')}" for g in sorted(used, key=str.lower))
    lines.append("legend: @ your city  C foreign city  # your units  ! foreign units  * resource  ' river · "
                 + terrain)
    lines.append("Each tile is one 3-char cell under its x; rows alternate. N=(x,y-2) E=(x+2,y) NE=(x+1,y-1).")
    lines += _notable(m, cells, sites or [], set(hostile))
    return "\n".join(lines)


def _notable(m: dict, cells: dict, sites: list[dict], hostile: set[str]) -> list[str]:
    tiles = sorted(cells.values(), key=lambda t: t.get("dist", 0))
    out = ["notable (distance and direction from the center):"]
    center = cells.get((0, 0))
    if center:
        y = center.get("yield") or {}
        desc = center.get("terrain", "?") + (f"/{center['overlay']}" if center.get("overlay") else "")
        desc += (" river" if center.get("river") else "") + (f" {center['resource']}" if center.get("resource") else "")
        line = f"center {pos(m['center'])} {desc}, yield {y.get('food')}/{y.get('shields')}/{y.get('commerce')} f/s/c"
        cs = center.get("city_site")
        if cs and not center.get("city"):
            line += " · found city here: " + ("yes" if cs.get("ok") else f"no — {cs.get('reason', '')}")
        out.append(line)
    for label, wanted in (("!! hostile units", True), ("foreign units", False)):
        found = [f"{u.get('owner', '?')} {u['type']}" + (f" x{u['count']}" if u.get("count", 1) > 1 else "")
                 + f" {pos(t)} {rel(t)}" for t in tiles for u in t.get("units") or []
                 if "id" not in u and (u.get("owner") in hostile) == wanted]
        if found:
            out.append(f"{label}: " + " · ".join(found))
    cities = [(f"{t['city']['id']} " if t["city"].get("id") else "") + t["city"]["name"]
              + ("" if t["city"].get("id") else f" ({t['city'].get('owner', '?')})")
              + f" size {t['city'].get('size', '?')} {pos(t)} {rel(t)}" for t in tiles if t.get("city")]
    if cities:
        out.append("cities: " + " · ".join(cities))
    own = [f"{u['id']} {u['type']} {pos(t)} {rel(t)}" for t in tiles for u in t.get("units") or [] if "id" in u]
    if own:
        out.append("your units: " + " · ".join(own))
    by_xy = {(t["x"], t["y"]): t for t in tiles}
    good = [f"{pos(s)} score {num(s['score'])} {rel(by_xy[s['x'], s['y']])}"
            for s in sites if (s["x"], s["y"]) in by_xy]
    if good:
        out.append("good city sites: " + " · ".join(good))
    res = [f"{t['resource']} {pos(t)} {rel(t)}" for t in tiles if t.get("resource")]
    if res:
        out.append("resources: " + " · ".join(res))
    rivers = [pos(t) for t in tiles if t.get("river")]
    if rivers:
        out.append("river tiles: " + " ".join(rivers[:12]) + (f" +{len(rivers) - 12} more" if len(rivers) > 12 else ""))
    return out


# ---- errors ----

def alternatives_text(alts: list) -> str:
    if not alts:
        return ""
    if all(isinstance(a, dict) and "x" in a and "y" in a for a in alts):
        head = "Nearest valid sites" if any("score" in a for a in alts) else "Valid tiles"
        return f"{head}: " + " | ".join(
            pos(a) + (f" {rel(a)}" if "dist" in a else "") + (f" · {a['turns']}t" if a.get("turns") is not None else "")
            + (f" · score {num(a['score'])}" if a.get("score") is not None else "") for a in alts[:5])
    names = [a if isinstance(a, str) else str(a.get("name", a)) for a in alts]
    more = f" (+{len(names) - 30} more)" if len(names) > 30 else ""
    return "Valid: " + ", ".join(names[:30]) + more


def error(e: BridgeError) -> str:
    lines = [e.message]
    named = all(isinstance(a, str) and a in e.message for a in e.alternatives)
    if not named and (alt := alternatives_text(e.alternatives)):
        lines.append(alt)
    if e.suggest:
        lines.append(f"Do this: {e.suggest}")
    return "\n".join(lines)
