"""Renderers on hand-built bridge results, including the token budgets (chars/4) of docs/tools.md."""

from agentenv_openciv3 import render
from agentenv_openciv3.bridge import BridgeError

TERRAINS = ["Grassland", "Plains", "Desert", "Tundra", "Flood Plain", "Coast"]


def score(cities, pop, tiles, techs):
    return {"total": 10 * cities + 3 * pop + tiles + 4 * techs, "cities": cities, "pop": pop, "tiles": tiles,
            "techs": techs}


def unit(n, kind, x, y, status="idle", needs=False, target=None):
    orders = {"Settler": ["settle", "found_city", "goto", "hold", "disband"],
              "Worker": ["auto_work", "goto", "build_road", "build_mine", "irrigate", "hold", "disband"]}.get(
        kind, ["explore", "goto", "fortify", "hold", "disband"])
    return {"id": f"u{n}", "type": kind, "x": x, "y": y, "moves_left": 1.0 if needs else 0.0, "moves_max": 1,
            "hp": 3, "hp_max": 3, "status": status, "target": target,
            "can_found_city": {"ok": False, "reason": "adjacent to Rome (12,10); cities need one empty tile between "
                                                      "them"} if kind == "Settler" else None,
            "orders": orders, "needs_orders": needs}


def city(n, name, x, y, size, item="Warrior"):
    return {"id": f"c{n}", "name": name, "x": x, "y": y, "size": size, "capital": n == 1, "food_stored": 4,
            "food_needed": 20, "food_per_turn": 2, "turns_to_grow": 8, "shields_per_turn": 3, "producing": item,
            "production_stored": 12, "production_cost": 30, "turns_to_complete": 6, "disorder": False,
            "buildings": ["Palace"] if n == 1 else []}


def state(n_cities, idle, standing, n_events, turn=34):
    names = ["Rome", "Veii", "Antium", "Cumae", "Neapolis", "Ravenna", "Arretium", "Mediolanum", "Capua"]
    cities = [city(i + 1, names[i], 12 + 4 * i, 10 + 2 * (i % 2), 2 + i % 3, ["Settler", "Warrior", "Granary"][i % 3])
              for i in range(n_cities)]
    units = [unit(i + 1, ["Settler", "Warrior", "Worker"][i % 3], 14 + i, 10 + i % 2 * 1 + i % 2, needs=True)
             for i in range(idle)]
    kinds = [("exploring", None), ("auto_work", None), ("fortified", None),
             ("settle", {"x": 21, "y": 7, "dist": 3, "dir": "NE"}), ("goto", {"x": 9, "y": 13, "dist": 2, "dir": "SW"})]
    units += [unit(idle + i + 1, ["Warrior", "Worker", "Warrior", "Settler", "Warrior"][i % 5], 12, 10,
                   status=kinds[i % 5][0], target=kinds[i % 5][1]) for i in range(standing)]
    events = [{"turn": turn - 1, "kind": k, "text": t, **({"x": 19, "y": 13} if k == "threat" else {})} for k, t in
              [("city_grew", "Rome grew to size 3"), ("built", "Veii completed Warrior"),
               ("threat", "Barbarian Warrior 3 tiles E of Veii"), ("tech_learned", "learned Pottery"),
               ("job_done", "u5 Worker finished build_road"), ("contact", "met Greece"),
               ("city_grew", "Antium grew to size 2"), ("explore_done", "u3 Warrior has nothing left to explore")]
              ][:n_events]
    blockers = [{"kind": "idle_unit", "id": u["id"], "message": f"{u['id']} {u['type']} has moves and no orders"}
                for u in units if u["needs_orders"]]
    return {"turn": turn, "turn_limit": 60, "game_over": False, "defeated": False, "civ": "Rome",
            "government": "Despotism", "anarchy_until": None, "gold": 34, "gold_per_turn": 3,
            "rates": {"tax": 4, "science": 6, "luxury": 0},
            "research": {"current": "Bronze Working", "turns_left": 3, "beakers": 8, "cost": 20,
                         "queue": ["Bronze Working", "Currency"]},
            "known_techs": ["Alphabet", "Pottery"], "score": score(n_cities, 9, 30, 5), "explored_pct": 18.5,
            "cities": cities, "units": units,
            "rivals": [{"civ": "Greece", "met": True, "at_war": False, "cities_seen": 1}],
            "blockers": blockers, "last_events": events}


SITE = {"x": 17, "y": 7, "score": 41, "dist": 2, "dir": "NE", "turns": 2, "terrain": "Grassland", "river": True,
        "coastal": False, "yield": {"food": 18, "shields": 6, "commerce": 4}}
BASELINES = {"engine_ai": score(3, 6, 27, 6), "null": score(1, 3, 9, 4)}


def brief_of(s, plan_chars):
    sites = {u["id"]: SITE for u in s["units"] if u["type"] == "Settler" and u["needs_orders"]}
    return render.brief(s, start_techs=2, plan=("Expand to the river; " * 60)[:plan_chars], plan_turn=30,
                        baselines=BASELINES, sites=sites)


def test_typical_brief_fits_600_tokens():
    text = brief_of(state(3, idle=2, standing=4, n_events=4), plan_chars=300)
    assert len(text) / 4 < 600, len(text) / 4
    assert "VS T34 (same seed) built-in AI 99 (3 cities, pop 6, techs 6) · do-nothing 44 (1 city, pop 3, techs 4)" \
        in text
    assert "!! Barbarian Warrior 3 tiles E of Veii (19,13)" in text


def test_crowded_brief_stays_bounded():
    s = state(9, idle=9, standing=12, n_events=8)
    text = brief_of(s, plan_chars=1000)
    assert len(text) / 4 < 800, len(text) / 4
    assert len(brief_of(s, plan_chars=0)) / 4 < 600
    assert "+4 more idle units → list_units()" in text
    assert "+6 more" in text and "+3 more → city_info()" in text
    events = next(line for line in text.splitlines() if line.startswith("EVENTS"))
    assert "!! Barbarian Warrior" in events and events.endswith("+3 more")


def test_brief_sections():
    text = brief_of(state(2, idle=1, standing=5, n_events=2), plan_chars=40)
    lines = text.splitlines()
    assert lines[0] == "T34/60 · Rome · Despotism · gold 34 (+3/t)"
    assert lines[1] == "RESEARCH Bronze Working 8/20 → 3t (then Currency)"
    assert lines[2] == "SCORE 97 (cities 2, pop 9, tiles 30, techs 5) · explored 18.5%"
    assert lines[3] == "PACE cities 2 BEHIND (3 by T30) · techs 5 ok (next: 6 by T40)"
    assert ('  u1 Settler (14,10) 1/1mv · idle · found here: no — adjacent to Rome (12,10); cities need one empty '
            'tile between them · best site (17,7) 2 NE score 41 → unit_order(unit="u1", order="settle", x=17, y=7)'
            in lines)
    assert ("STANDING u2 Warrior exploring (auto) · u3 Worker auto-working · u4 Warrior fortified · "
            "u5 Settler settle→(21,7) 3 NE · u6 Warrior goto→(9,13) 2 SW") in lines
    assert "  c1 Rome (12,10) size 2 · food +2/t grows 8t · Settler 12/30 → 6t (completes only at size 3+)" in lines
    assert lines[-1].startswith("PLAN (T30) Expand to the river;")


def test_pace_milestones():
    s = state(1, 0, 0, 0, turn=20)
    assert render.pace_line(s, start_techs=2) == "PACE cities 1 BEHIND (2 by T15) · techs 5 ok (next: 6 by T40)"
    s = state(2, 0, 0, 0, turn=22)
    assert render.pace_line(s, start_techs=4) == "PACE cities 2 ok (next: 3 by T30) · techs 5 BEHIND (6 by T20)"


def test_footer():
    s = state(1, idle=2, standing=0, n_events=0)
    s["blockers"].insert(0, {"kind": "no_research", "message": "nothing is being researched"})
    assert render.footer(s) == "[T34/60 · needs orders: research, u1, u2]"
    s["blockers"] = []
    assert render.footer(s) == "[T34/60 · nothing needs orders]"
    s["game_over"] = True
    assert render.footer(s) == "[GAME OVER T34/60]"


def tiles_around(cx, cy, radius, width=60):
    out = []
    for dx in range(-2 * radius, 2 * radius + 1):
        for dy in range(-2 * radius, 2 * radius + 1):
            if (dx + dy) % 2 or (abs(dx) + abs(dy)) // 2 > radius:
                continue
            x, y = (cx + dx) % width, cy + dy
            t = {"x": x, "y": y, "dist": (abs(dx) + abs(dy)) // 2, "dir": "E" if dx > 0 else "W" if dx < 0 else
                 "N" if dy < 0 else "S" if dy else "here", "visible": True, "terrain": TERRAINS[(x + y) % 6],
                 "overlay": "Forest" if (x * y) % 7 == 0 else None,
                 "resource": "Wheat" if (x + 2 * y) % 9 == 0 else None,
                 "river": (x - y) % 5 == 0, "improvements": [], "owner": "Rome", "city": None, "units": [],
                 "yield": {"food": 2, "shields": 1, "commerce": 1}, "city_site": {"ok": True, "reason": None}}
            if (dx, dy) == (0, 0):
                t["city"] = {"name": "Rome", "owner": "Rome", "size": 3, "id": "c1"}
                t["units"] = [{"owner": "Rome", "type": "Warrior", "count": 1, "id": "u2"}]
            if (dx, dy) == (3, 1):
                t["units"] = [{"owner": "Barbarians", "type": "Warrior", "count": 2}]
            out.append(t)
    return {"center": {"x": cx, "y": cy}, "radius": radius, "tiles": out}


def test_map_budget_and_layout():
    text = render.map_view(tiles_around(20, 20, 3), label="around c1 (20,20)", width=60, wrap_x=True,
                           sites=[SITE | {"x": 22, "y": 18}])
    assert len(text) / 4 < 700, len(text) / 4
    lines = text.splitlines()
    header = lines[1]
    assert header.split()[1:] == [str(x) for x in range(14, 27)]
    row20 = next(line for line in lines if line.startswith("   20 "))
    col = header.index(" 20")
    assert row20[col] == "@"
    row21 = next(line for line in lines if line.startswith("   21 "))
    assert row21[header.index(" 23")] == "!"
    assert "!! foreign units: Barbarians Warrior x2 (23,21) 2 E" in text
    assert "good city sites: (22,18) score 41 2 E" in text
    assert "your units: u2 Warrior (20,20) here" in text
    big = render.map_view(tiles_around(20, 20, 6), label="at (20,20)", width=60, wrap_x=True)
    assert len(big) / 4 < 1400, len(big) / 4


def test_map_wraps_across_the_date_line():
    text = render.map_view(tiles_around(1, 20, 2), label="at (1,20)", width=60, wrap_x=True)
    header = text.splitlines()[1]
    assert header.split()[1:] == ["57", "58", "59", "0", "1", "2", "3", "4", "5"]


def test_unexplored_is_blank():
    m = tiles_around(20, 20, 2)
    m["tiles"] = [t for t in m["tiles"] if t["x"] <= 20]
    text = render.map_view(m, label="at (20,20)")
    header, *rows = text.splitlines()[1:8]
    assert header.split()[-1] == "20"
    assert render.map_view({"center": {"x": 1, "y": 1}, "radius": 3, "tiles": []}, label="x").endswith(
        "nothing explored here")


def test_error_text():
    sites = [SITE, SITE | {"x": 11, "y": 14, "score": 37, "dist": 3, "dir": "SW", "turns": 3}]
    e = BridgeError("cannot_found", "cannot found a city at (15,11): adjacent to Veii (16,12).", sites,
                    'unit_order(unit="u7", order="settle", x=17, y=7)')
    assert render.error(e) == (
        "cannot found a city at (15,11): adjacent to Veii (16,12).\n"
        "Nearest valid sites: (17,7) 2 NE · 2t · score 41 | (11,14) 3 SW · 3t · score 37\n"
        'Do this: unit_order(unit="u7", order="settle", x=17, y=7)')
    e = BridgeError("unknown_item", "Rome cannot build 'Pyramid'.", [f"Item{i}" for i in range(35)])
    assert render.error(e).endswith("Item29 (+5 more)")
    assert render.error(BridgeError("no_moves", "u3 has no moves left.")) == "u3 has no moves left."
    e = BridgeError("unknown_unit", "There is no unit 'u9'. Your units: u1, u2.", ["u1", "u2"])
    assert render.error(e) == "There is no unit 'u9'. Your units: u1, u2."
    e = BridgeError("bad_target", "(3,4) is not a tile: x + y must be even.", [{"x": 4, "y": 4}, {"x": 3, "y": 5}])
    assert render.error(e).splitlines()[1] == "Valid tiles: (4,4) | (3,5)"


def test_settler_on_the_best_site_is_told_to_found_here():
    s = state(1, idle=1, standing=0, n_events=0)
    s["units"][0]["can_found_city"] = {"ok": True}
    line = render.blocker_line(s["blockers"][0], s, SITE | {"x": 14, "y": 10, "dist": 0, "dir": "here"})
    assert line == ('u1 Settler (14,10) 1/1mv · idle · found here: yes (the best site) → '
                    'unit_order(unit="u1", order="found_city")')


def test_turn_report_groups_events_by_turn():
    res = {"blocked": False, "turns_advanced": 2, "turn": 25, "game_over": False, "defeated": False,
           "events": [{"turn": 23, "kind": "built", "text": "Rome completed Warrior.", "x": 12, "y": 10},
                      {"turn": 24, "kind": "unit_lost", "text": "u5 Worker was killed", "x": 14, "y": 9},
                      {"turn": 24, "kind": "city_grew", "text": "Veii grew to size 2"}],
           "auto": [{"kind": "trade_declined", "text": "declined a trade offer from Greece"}]}
    assert render.turn_report(res, 23) == (
        "TURN T23 → T25 (2 turns)\n"
        "  T23: Rome completed Warrior (12,10)\n"
        "  T24: !! u5 Worker was killed (14,9) · Veii grew to size 2\n"
        "  auto: declined a trade offer from Greece")


def test_city_detail_and_techs():
    c = city(1, "Rome", 12, 10, 3) | {"options": [
        {"name": "Settler", "kind": "unit", "cost": 30, "turns": 6}, {"name": "Wealth", "kind": "wealth", "cost": 0,
                                                                      "turns": None}],
        "tiles_worked": [{"x": 13, "y": 9}, {"x": 12, "y": 8}, {"x": 11, "y": 11}]}
    assert render.city_detail(c) == (
        "c1 Rome (12,10) size 3 capital · food 4/20 +2/t grows 8t · shields 3/t · works 3 tiles\n"
        "  producing Warrior 12/30 → 6t\n"
        "  buildings: Palace\n"
        "  can build: Settler 30 (6t) · Wealth")
    t = {"current": "Pottery", "turns_left": 2, "known": ["Alphabet"],
         "available": [{"name": "Masonry", "cost": 24, "turns": 6, "era": "Ancient", "unlocks": ["Walls"]}]}
    assert render.techs_list(t).splitlines()[:3] == [
        "RESEARCH current: Pottery → 2t · known 1: Alphabet", "AVAILABLE", "  Masonry 24 beakers 6t → Walls"]


def test_resolve_name_takes_only_unambiguous_matches():
    from agentenv_openciv3.server import resolve_name
    options = ["Settler", "Worker", "Warrior", "Archer", "Barracks", "Wealth"]
    assert resolve_name("Warriors", options) == "Warrior"
    assert resolve_name("barracks", options) == "Barracks"
    assert resolve_name("Setler", options) == "Settler"
    assert resolve_name("Pyramids", options) is None
    assert resolve_name("Bronze working", ["Bronze Working", "Iron Working"]) == "Bronze Working"
    assert resolve_name("Working", ["Bronze Working", "Iron Working"]) is None
