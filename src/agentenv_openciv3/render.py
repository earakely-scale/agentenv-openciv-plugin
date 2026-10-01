"""Compact text for the agent, rendered from CivBridge results (docs/protocol.md)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from .bridge import BridgeError

URGENT = {"threat", "unit_lost", "war_declared", "city_destroyed", "disorder", "city_starved",
          "settle_failed", "goto_blocked"}
CITY_TARGETS = ((1, 1), (15, 2), (30, 3), (45, 4), (60, 5), (80, 6), (100, 7))
TECHS_LEARNED_TARGETS = ((20, 2), (40, 4), (60, 6), (80, 8), (100, 10))
TERRAIN = {"grassland": "g", "plains": "p", "desert": "d", "tundra": "t", "floodplain": "f", "hills": "h",
           "mountains": "m", "forest": "F", "jungle": "j", "marsh": "s", "volcano": "v", "coast": "~",
           "sea": "~", "ocean": "~"}
TERRAIN_NAMES = {"g": "grassland", "p": "plains", "d": "desert", "t": "tundra", "f": "flood plain", "h": "hills",
                 "m": "mountains", "F": "forest", "j": "jungle", "s": "marsh", "v": "volcano", "~": "water"}
BASELINE_LABELS = {"engine_ai": "built-in AI", "null": "do-nothing"}
MAX_UNIT_LINES, MAX_STANDING, MAX_CITY_LINES, MAX_EVENTS = 5, 6, 6, 5
# An item completes only when the city is bigger than its population cost (Settler 2, Worker 1 in the ruleset).
MIN_SIZE = {"Settler": 3, "Worker": 2}


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
    pending = [b.get("id") or "research" for b in state.get("blockers", [])]
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
    return " · ".join(parts)


def units_list(state: dict, everything: bool) -> str:
    units = state.get("units", [])
    shown = units if everything else [u for u in units if u.get("needs_orders")]
    head = (f"UNITS (all {len(units)}) T{state['turn']}" if everything
            else f"UNITS needing orders ({len(shown)} of {len(units)}) T{state['turn']}")
    if not shown:
        rest = "" if everything else " — list_units(filter=\"all\") shows the rest"
        return f"{head}\nnone{rest}"
    return "\n".join([head, *(unit_line(u, detail=True) for u in shown)])


# ---- cities ----

def production_text(c: dict) -> str:
    item = c.get("producing")
    if not item:
        return "NOTHING in production"
    eta = c.get("turns_to_complete")
    text = f"{item} {c.get('production_stored', 0)}/{c.get('production_cost', '?')}" + (f" → {eta}t" if eta else "")
    if c.get("size", 0) < MIN_SIZE.get(item, 0):
        text += f" (completes only at size {MIN_SIZE[item]}+)"
    return text


def growth_text(c: dict) -> str:
    fpt = c.get("food_per_turn", 0)
    if fpt < 0:
        return f"!! {fpt}/t starving"
    eta = c.get("turns_to_grow")
    return f"{fpt:+d}/t" + (f" grows {eta}t" if eta else " not growing")


def city_line(c: dict) -> str:
    parts = [f"{c['id']} {c['name']} {pos(c)} size {c['size']}", "food " + growth_text(c), production_text(c)]
    if c.get("disorder"):
        parts.append("!! DISORDER")
    return " · ".join(parts)


def city_detail(c: dict) -> str:
    head = [f"{c['id']} {c['name']} {pos(c)} size {c['size']}" + (" capital" if c.get("capital") else ""),
            f"food {c.get('food_stored', 0)}/{c.get('food_needed', '?')} {growth_text(c)}",
            f"shields {c.get('shields_per_turn', 0)}/t"]
    worked = c.get("tiles_worked")
    if worked is not None:
        head.append(f"works {len(worked) if isinstance(worked, list) else worked} tiles")
    if c.get("disorder"):
        head.append("!! DISORDER")
    item = production_text(c)
    lines = [" · ".join(head), f"  {'producing ' if c.get('producing') else ''}{item}"]
    if c.get("buildings"):
        lines.append("  buildings: " + ", ".join(c["buildings"]))
    opts = [o["name"] if o.get("kind") == "wealth" else f"{o['name']} {o['cost']} ({o['turns']}t)"
            for o in c.get("options", [])]
    if opts:
        lines.append("  can build: " + " · ".join(opts))
    return "\n".join(lines)


# ---- turn brief ----

def _milestone(targets: Iterable[tuple[int, int]], turn: int, have: int, base: int = 0) -> str:
    done = [(t, n + base) for t, n in targets if t <= turn]
    if done and have < done[-1][1]:
        return f"BEHIND ({done[-1][1]} by T{done[-1][0]})"
    upcoming = [(t, n + base) for t, n in targets if t > turn]
    return f"ok (next: {upcoming[0][1]} by T{upcoming[0][0]})" if upcoming else "ok"


def pace_line(state: dict, start_techs: int) -> str:
    s, turn = state["score"], state["turn"]
    return (f"PACE cities {s['cities']} {_milestone(CITY_TARGETS, turn, s['cities'])} · "
            f"techs {s['techs']} {_milestone(TECHS_LEARNED_TARGETS, turn, s['techs'], start_techs)}")


def vs_line(turn: int, baselines: dict[str, dict | None]) -> str:
    parts = [f"{BASELINE_LABELS[p]} " + (score_text(s, short=True) if s else "computing…")
             for p, s in baselines.items()]
    return f"VS T{turn} (same seed) " + " · ".join(parts)


def event_text(e: dict) -> str:
    text = e.get("text") or e.get("kind", "")
    if "x" in e and "y" in e and pos(e) not in text:
        text = f"{text.rstrip('.')} {pos(e)}"
    return ("!! " if e.get("kind") in URGENT else "") + text


def events_lines(events: list[dict], cap: int = MAX_EVENTS) -> list[str]:
    keep = sorted(range(len(events)), key=lambda i: events[i].get("kind") not in URGENT)[:cap]
    by_turn: dict[int, list[str]] = {}
    for i in sorted(keep):
        by_turn.setdefault(events[i].get("turn", 0), []).append(event_text(events[i]))
    lines = [f"T{t}: " + " · ".join(texts) for t, texts in by_turn.items()]
    if len(events) > cap:
        lines.append(f"+{len(events) - cap} more")
    return lines


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
    if kind == "no_production":
        return f"{b.get('message', bid)} → {call('set_production', city=bid, item='...')}"
    if kind == "no_research":
        return f"{b.get('message', 'nothing being researched')} → research() lists techs"
    return b.get("message", str(b))


def brief(state: dict, *, start_techs: int, plan: str | None = None, plan_turn: int | None = None,
          baselines: dict[str, dict | None] | None = None, sites: dict[str, dict] | None = None,
          events: bool = True) -> str:
    s = state
    head = [f"T{s['turn']}/{s['turn_limit']}", s.get("civ", "?"), s.get("government", "?")]
    if s.get("anarchy_until"):
        head.append(f"anarchy until T{s['anarchy_until']}")
    head.append(f"gold {s.get('gold', 0)} ({s.get('gold_per_turn', 0):+d}/t)")
    wars = [r["civ"] for r in s.get("rivals", []) if r.get("at_war")]
    if wars:
        head.append("!! at war with " + ", ".join(wars))
    if s.get("defeated"):
        head.append("DEFEATED")
    lines = [" · ".join(head)]

    r = s.get("research") or {}
    if r.get("current"):
        then = [t for t in r.get("queue", []) if t != r["current"]]
        lines.append(f"RESEARCH {r['current']} {r.get('beakers', 0)}/{r.get('cost', '?')}"
                     + (f" → {r['turns_left']}t" if r.get("turns_left") else "")
                     + (f" (then {', '.join(then[:3])})" if then else ""))
    else:
        lines.append("RESEARCH none — research() lists techs")
    lines.append(f"SCORE {score_text(s['score'])} · explored {num(s.get('explored_pct', 0))}%")
    lines.append(pace_line(s, start_techs))
    if baselines:
        lines.append(vs_line(s["turn"], baselines))

    blockers = s.get("blockers", [])
    if blockers:
        lines.append(f"NEEDS ORDERS ({len(blockers)})")
        units = [b for b in blockers if b.get("kind") == "idle_unit"]
        for b in [b for b in blockers if b.get("kind") != "idle_unit"] + units[:MAX_UNIT_LINES]:
            lines.append("  " + blocker_line(b, s, (sites or {}).get(b.get("id"))))
        if len(units) > MAX_UNIT_LINES:
            lines.append(f"  +{len(units) - MAX_UNIT_LINES} more idle units → list_units()")
    else:
        lines.append("NEEDS ORDERS none — end_turn() or end_turn(until_attention=true)")

    standing = [u for u in s.get("units", []) if not u.get("needs_orders") and u.get("status") not in ("idle", "done")]
    if standing:
        items = [f"{u['id']} {u['type']} {status_text(u)}" for u in standing[:MAX_STANDING]]
        if len(standing) > MAX_STANDING:
            items.append(f"+{len(standing) - MAX_STANDING} more")
        lines.append("STANDING " + " · ".join(items))

    cities = s.get("cities", [])
    if cities:
        lines.append("CITIES")
        lines += ["  " + city_line(c) for c in cities[:MAX_CITY_LINES]]
        if len(cities) > MAX_CITY_LINES:
            lines.append(f"  +{len(cities) - MAX_CITY_LINES} more → city_info()")
    else:
        lines.append("CITIES none yet — found one: find_city_sites(), then unit_order(order=\"settle\", ...)")

    if events and s.get("last_events"):
        lines.append("EVENTS " + " | ".join(events_lines(s["last_events"])))
    lines.append(f"PLAN (T{plan_turn}) {plan}" if plan else "PLAN none — record your strategy with plan(text=...)")
    return "\n".join(lines)


# ---- end turn ----

def turn_report(result: dict, prev_turn: int) -> str:
    n = result.get("turns_advanced", 0)
    lines = [f"TURN T{prev_turn} → T{result['turn']} ({n} turn{'s' if n != 1 else ''})"]
    lines += ["  " + line for line in events_lines(result.get("events", []), cap=15)]
    lines += [f"  auto: {a.get('text') or a.get('kind')}" for a in result.get("auto", [])]
    return "\n".join(lines)


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
        else:
            hint = "get_turn_brief()"
        lines.append(f"  {b.get('message', kind)} → {hint}")
    lines.append("Or end_turn(skip_idle=true): idle units hold this turn and research is auto-picked.")
    return "\n".join(lines)


def game_over(state: dict, baselines: dict[str, dict | None] | None) -> str:
    s = state
    why = (f"your civilization was destroyed (T{s['turn']})" if s.get("defeated")
           else f"turn {s['turn']}/{s['turn_limit']} reached")
    lines = [f"GAME OVER — {why}.",
             f"FINAL score {score_text(s['score'])} · units {len(s.get('units', []))} · gold {s.get('gold', 0)}"
             f" · explored {num(s.get('explored_pct', 0))}%"]
    if baselines:
        lines.append(vs_line(s["turn"], baselines))
    lines.append("The game is over; no further actions are possible.")
    return "\n".join(lines)


# ---- research ----

def techs_list(t: dict) -> str:
    known = t.get("known", [])
    cur = f"{t['current']}" + (f" → {t['turns_left']}t" if t.get("turns_left") else "") if t.get("current") else "none"
    lines = [f"RESEARCH current: {cur} · known {len(known)}: {', '.join(known)}", "AVAILABLE"]
    for a in t.get("available", []):
        unlocks = f" → {', '.join(a['unlocks'])}" if a.get("unlocks") else ""
        lines.append(f"  {a['name']} {a.get('cost', '?')} beakers {a.get('turns', '?')}t{unlocks}")
    lines.append('research(tech="...") sets it; any tech works — missing prerequisites are queued first.')
    return "\n".join(lines)


# ---- city sites ----

def site_text(s: dict) -> str:
    y = s.get("yield") or {}
    traits = [t for t, on in (("river", s.get("river")), ("coast", s.get("coastal"))) if on]
    parts = [f"{pos(s)} score {num(s['score'])}", rel(s)]
    if s.get("turns") is not None:
        parts.append(f"{s['turns']}t away")
    if y:
        parts.append(f"area food {y.get('food')} shields {y.get('shields')} commerce {y.get('commerce')}")
    if s.get("terrain"):
        parts.append(s["terrain"].lower())
    return " · ".join(parts + traits)


def sites_list(result: dict, unit: dict | None) -> str:
    origin = result.get("origin") or {}
    who = f"{unit['id']} {unit['type']} at {pos(origin)}" if unit else pos(origin)
    sites = result.get("sites", [])
    lines = [f"CITY SITES from {who}" + (f" ({result['note']})" if result.get("note") else "")]
    if not sites:
        lines.append("none found — explore more: unit_order(unit=..., order=\"explore\")")
        return "\n".join(lines)
    lines += [f"#{i} {site_text(s)}" for i, s in enumerate(sites, 1)]
    if unit:
        lines.append("Do this: " + call("unit_order", unit=unit["id"], order="settle", x=sites[0]["x"], y=sites[0]["y"])
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
             sites: list[dict] | None = None) -> str:
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
    lines += _notable(m, cells, sites or [])
    return "\n".join(lines)


def _notable(m: dict, cells: dict, sites: list[dict]) -> list[str]:
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
    foreign = [f"{u.get('owner', '?')} {u['type']}" + (f" x{u['count']}" if u.get("count", 1) > 1 else "")
               + f" {pos(t)} {rel(t)}" for t in tiles for u in t.get("units") or [] if "id" not in u]
    if foreign:
        out.append("!! foreign units: " + " · ".join(foreign))
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
