"""A stand-in for CivBridge: speaks the docs/protocol.md JSON-lines protocol over a small scripted game.

The world is hand-made around the start at (12,10): coast to the west, a river, a few resources, a Greek
city to the northeast and a barbarian that shows up on turn 3. Shapes follow protocol.md and
docs/recording.md exactly. Flags: --record <dir>, --autosave <dir>, --saves <dir> (others are accepted and ignored).
Test hooks: FAKE_BRIDGE_STDERR_KB (log noise before ready), FAKE_BRIDGE_STDOUT_NOISE (a stray non-JSON
line), FAKE_BRIDGE_CRASH_ON (a command that kills the process), and the commands `_sleep`, `_big`,
`_crash`, `_city` (overwrite city fields) and `_game` (overwrite game attributes).
Seats (new_game `seats`, each an opponent civ) share the one scripted game: every seat sees and orders the same
units and cities; only `end_turn` is per seat (it waits for every seat, as protocol.md's Seats section says).
"""

from __future__ import annotations

import base64
import gzip
import json
import math
import os
import pickle
import sys
import time
from pathlib import Path

OUT = sys.stdout
DIRS = ["E", "NE", "N", "NW", "W", "SW", "S", "SE"]
START = (12, 10)
WIDTH = HEIGHT = 60
RIVER = {(13, 9), (14, 10), (15, 11), (16, 12), (17, 13)}
RESOURCES = {(13, 9): "Wheat", (16, 8): "Horses", (10, 14): "Gems", (8, 8): "Fish"}
# The strategic and luxury ones, with their icon in resources.png (the client's ruleset.json).
TRADE_GOODS = {"Horses": ("strategic", 0), "Gems": ("luxuries", 15)}
OVERLAY = {(14, 8): "Hills", (15, 7): "Forest", (11, 13): "Forest", (16, 10): "Mountains", (10, 12): "Marsh"}
SIZES = ["Tiny", "Small", "Standard", "Large", "Huge"]
CITY_NAMES = ["Rome", "Veii", "Antium", "Cumae", "Neapolis", "Ravenna"]
CIVS = ["Greece", "Egypt", "Babylon", "Germany", "Persia"]
# The OpenCiv3 client's (primary, secondary) colour index per civ (ruleset.json) and the hex of those indexes
# (standalone textures.lua), for known_map's players[].color.
CIV_COLOR_INDEXES = {"Barbarians": (0, 0), "Rome": (1, 1), "Greece": (10, 10), "Egypt": (3, 3), "Babylon": (1, 13),
                     "Germany": (6, 6), "Persia": (10, 14)}
CLIENT_COLORS = {0: "#f0f8ff", 1: "#e6194b", 3: "#ffe119", 6: "#000075", 10: "#aaffc3", 13: "#808000", 14: "#dcbeff"}
COLORS = {"Rome": [196, 52, 52], "Greece": [52, 96, 196], "Egypt": [212, 180, 40], "Babylon": [120, 60, 160],
          "Germany": [90, 90, 90], "Persia": [40, 160, 140], "Barbarians": [30, 30, 30]}
TECHS = {  # name: (cost, prerequisites, unlocks)
    "Alphabet": (20, [], ["Writing", "Code of Laws"]),
    "Bronze Working": (16, [], ["Colossus", "Currency"]),
    "Masonry": (24, [], ["Great Wall", "Pyramids", "Walls"]),
    "Pottery": (16, [], ["Granary", "Hanging Gardens"]),
    "Ceremonial Burial": (16, [], ["Temple"]),
    "Warrior Code": (20, [], ["Archer"]),
    "Horseback Riding": (20, [], ["Horseman"]),
    "Writing": (40, ["Alphabet"], ["Library"]),
    "Code of Laws": (36, ["Alphabet"], ["Courthouse"]),
    "Currency": (36, ["Bronze Working"], ["Marketplace"]),
    "Iron Working": (36, ["Bronze Working", "Warrior Code"], ["Swordsman"]),
    "Monarchy": (60, ["Ceremonial Burial", "Code of Laws"], ["Monarchy government"]),
}
ITEMS = {  # name: (kind, cost, required tech)
    "Settler": ("unit", 30, None), "Worker": ("unit", 10, None), "Warrior": ("unit", 10, None),
    "Archer": ("unit", 20, "Warrior Code"), "Barracks": ("building", 40, None), "Granary": ("building", 60, "Pottery"),
    "Temple": ("building", 40, "Ceremonial Burial"), "Walls": ("building", 30, "Masonry"),
    "Wealth": ("wealth", 0, None),
}
POP_COST = {"Settler": 2, "Worker": 1}
ORDERS = {
    "Settler": ["settle", "found_city", "goto", "hold", "disband"],
    "Worker": ["auto_work", "goto", "build_road", "build_mine", "irrigate", "hold", "disband"],
    "Warrior": ["explore", "goto", "fortify", "hold", "disband"],
}
UNIT_STATS = {"Settler": (1, 3), "Worker": (1, 3), "Warrior": (1, 3)}  # moves, hp
GOVERNMENTS = ["Monarchy"]
PEACE_PRICE = 50
MAX_RATE, BORN_CONTENT, POLICE_LIMIT = 6, 2, 2
ATTENTION = {"disorder_started", "riot_risk", "unit_lost", "city_destroyed", "gold_stolen", "war_declared",
             "defenseless", "threat"}


class Refused(Exception):
    def __init__(self, code, message, alternatives=None, suggest=None):
        super().__init__(message)
        self.error = {"code": code, "message": message}
        if alternatives:
            self.error["alternatives"] = alternatives
        if suggest:
            self.error["suggest"] = suggest


def dist(a, b) -> int:
    return (abs(a[0] - b[0]) + abs(a[1] - b[1])) // 2


def direction(src, dst) -> str:
    dx, dy = dst[0] - src[0], dst[1] - src[1]
    if (dx, dy) == (0, 0):
        return "here"
    return DIRS[round(math.atan2(-dy, dx) / (math.pi / 4)) % 8]


def rel(src, dst) -> dict:
    return {"dist": dist(src, dst), "dir": direction(src, dst)}


def terrain(x, y) -> str:
    if x <= 5:
        return "Ocean" if x <= 3 else "Coast"
    return "Plains" if (x * 3 + y) % 5 == 0 else "Grassland"


def is_land(p) -> bool:
    return terrain(*p) not in ("Coast", "Ocean", "Sea")


def on_map(p) -> bool:
    return 0 <= p[0] < WIDTH and 0 <= p[1] < HEIGHT and (p[0] + p[1]) % 2 == 0


def area(c, r):
    return [(x, y) for x in range(c[0] - 2 * r, c[0] + 2 * r + 1) for y in range(c[1] - 2 * r, c[1] + 2 * r + 1)
            if on_map((x, y)) and dist(c, (x, y)) <= r]


def tile_yield(p) -> dict:
    if not is_land(p):
        return {"food": 1, "shields": 0, "commerce": 2}
    food, shields = {"Grassland": (2, 0), "Plains": (1, 1)}[terrain(*p)]
    if OVERLAY.get(p) in ("Hills", "Forest", "Mountains"):
        food, shields = (1 if OVERLAY[p] == "Hills" else 0 if OVERLAY[p] == "Mountains" else 1), 2
    if RESOURCES.get(p) == "Wheat":
        food += 2
    return {"food": food, "shields": shields, "commerce": 1 if p in RIVER else 0}


def client_colors(civs: list[str]) -> list[int]:
    """The client's colour index per player (C7/Textures/PlayerTextureUtil.cs): the primary, or the secondary when
    the primary is taken; then in order a player whose colour is shared drops it and picks again."""
    used: dict[int, int] = {}

    def load(i: int) -> None:
        if i not in used:
            first, second = CIV_COLOR_INDEXES[civs[i]]
            used[i] = second if first in used.values() else first

    for i in range(len(civs)):
        load(i)
    for i in range(len(civs)):
        if list(used.values()).count(used[i]) > 1:
            del used[i]
            load(i)
    return [used[i] for i in range(len(civs))]


def ceil_div(a, b) -> int:
    return -(-a // b)


class Game:
    def __init__(self, args):
        self.seed = args["seed"]
        self.turn_limit = args.get("turn_limit", 60)
        self.civ = args.get("civ", "Rome")
        self.opponents = CIVS[: args.get("opponents", 3)]
        self.seats = [self.civ, *(args.get("seats") or [])]
        self.labels = dict(args.get("labels") or {})
        if unknown := [c for c in self.seats[1:] if c not in self.opponents]:
            raise Refused("bad_args", f"seats {unknown} are not opponents.", self.opponents)
        self.ready = set()
        self.turn = 1
        self.units = {}
        self.next_unit = self.next_city = 1
        for kind, p in (("Settler", START), ("Worker", START), ("Settler", (13, 11)), ("Warrior", (14, 10))):
            self.add_unit(kind, p)
        self.cities = {}
        self.known = ["Alphabet", "Bronze Working"]
        self.research, self.research_source, self.research_pending = None, None, False
        self.queue, self.beakers = [], 0
        self.gold = 10
        self.rates = {"science": 6, "luxury": 0}
        self.decisions = {"production": {"agent": 0, "engine": 0}, "research": {"agent": 0, "engine": 0}}
        self.last_events = []
        self.explored = set(area(START, 5))
        self.foreign_city = {"name": "Athens", "owner": "Greece", "size": 2, "pos": (18, 6)}
        self.met = False
        self.government, self.anarchy_until, self.revolution_target = "Despotism", None, None
        self.wars, self.talks_from, self.barbarian_killed = set(), {}, False
        self.battles = []  # known_map's, every one the seat's own attack
        self.threats_seen = set()
        self.risk_seen = set()

    # ---- units ----

    def add_unit(self, kind, p):
        uid = f"u{self.next_unit}"
        self.next_unit += 1
        moves, hp = UNIT_STATS[kind]
        self.units[uid] = {"id": uid, "type": kind, "pos": p, "moves": float(moves), "hp": hp, "status": "idle",
                           "target": None, "steps": 0}
        return uid

    def can_found(self, p) -> dict:
        if not is_land(p) or OVERLAY.get(p) in ("Mountains", "Marsh"):
            what = OVERLAY.get(p) or terrain(*p)
            return {"ok": False, "reason": f"cities cannot be built on {what}"}
        for c in self.cities.values():
            if dist(c["pos"], p) <= 1:
                where = f"{c['name']} ({c['pos'][0]},{c['pos'][1]})"
                return {"ok": False, "reason": f"adjacent to {where}; cities need one empty tile between them"}
        if dist(self.foreign_city["pos"], p) <= 1:
            return {"ok": False, "reason": "adjacent to Athens"}
        return {"ok": True, "reason": None}

    def unit_view(self, u) -> dict:
        moves, hp = UNIT_STATS[u["type"]]
        status = u["status"] if u["status"] != "idle" or u["moves"] > 0 else "done"
        orders = list(ORDERS[u["type"]]) + (["wake"] if u["status"] not in ("idle", "done") else [])
        targets = self.attack_targets(u)
        orders += ["attack"] if targets else []
        target = None
        if u["target"]:
            target = {"x": u["target"][0], "y": u["target"][1], **rel(u["pos"], u["target"])}
        return {"id": u["id"], "type": u["type"], "x": u["pos"][0], "y": u["pos"][1], "moves_left": u["moves"],
                "moves_max": moves, "hp": u["hp"], "hp_max": hp, "status": status, "target": target,
                "can_found_city": self.can_found(u["pos"]) if u["type"] == "Settler" else None,
                "orders": orders, "needs_orders": u["status"] == "idle" and u["moves"] > 0,
                **({"attack_targets": targets} if targets else {})}

    def unit(self, uid):
        if uid not in self.units:
            raise Refused("unknown_unit", f"you have no unit {uid!r}.", sorted(self.units))
        return self.units[uid]

    # ---- cities ----

    def found(self, u) -> dict:
        cid = f"c{self.next_city}"
        self.next_city += 1
        name = CITY_NAMES[len(self.cities) % len(CITY_NAMES)]
        self.cities[cid] = {"id": cid, "name": name, "pos": u["pos"], "size": 1, "food": 0, "shields": 0,
                            "producing": "Warrior", "source": "engine", "pending": True, "lost": 0, "capped_seen": None,
                            "disorder": False, "buildings": ["Palace"] if not self.cities else [], "founded": self.turn}
        del self.units[u["id"]]
        return self.cities[cid]

    def defenders(self, c) -> int:
        return sum(u["type"] == "Warrior" and u["pos"] == c["pos"] for u in self.units.values())

    def mood(self, c, extra=0) -> tuple[int, int, int]:
        """(happy, content, unhappy) at the current luxury rate and garrison, with `extra` more citizens."""
        size = c["size"] + extra
        happy = min(self.rates["luxury"] // 2, size)
        unhappy = max(0, size - BORN_CONTENT - happy - min(self.defenders(c), POLICE_LIMIT))
        return happy, size - happy - unhappy, unhappy

    def riot_risk(self, c) -> bool:
        happy, _, unhappy = self.mood(c, extra=1)
        return not c["disorder"] and unhappy > happy

    def spt(self, c) -> int:
        return 0 if c["disorder"] else 1 + c["size"]

    def capped(self, c) -> bool:
        item = c["producing"]
        return bool(item) and item in POP_COST and c["shields"] >= ITEMS[item][1] and c["size"] <= POP_COST[item]

    def maintenance(self, c) -> int:
        return sum(b != "Palace" for b in c["buildings"])

    def commerce(self) -> dict:
        """Each city's commerce split as the engine does, its taxes making up gpt() (plus upkeep), so the domestic
        advisor's figures add up."""
        cities = list(self.cities.values())
        upkeep = sum(self.maintenance(c) for c in cities)
        taxes = [(self.gpt() + upkeep) // len(cities)] * len(cities) if cities else []
        if taxes:
            taxes[0] += self.gpt() + upkeep - sum(taxes)
        out = {}
        for c, tax in zip(cities, taxes, strict=True):
            science, luxury = self.rates["science"] * (1 + c["size"]) // 6, self.rates["luxury"] * c["size"] // 4
            corrupt = 0 if "Palace" in c["buildings"] else 1
            out[c["id"]] = {"total": tax + science + luxury + corrupt, "taxes": tax, "science": science,
                            "luxury": luxury, "corrupt": corrupt, "wealth": 0}
        return out

    def finance(self) -> dict:
        commerce = self.commerce().values()
        parts = {k: sum(c[k] for c in commerce) for k in ("total", "science", "luxury", "corrupt")}
        income = {"cities": parts["total"], "taxmen": 0, "other_civs": 0, "interest": 0}
        expenses = {"science": parts["science"], "entertainment": parts["luxury"], "corruption": parts["corrupt"],
                    "maintenance": sum(self.maintenance(c) for c in self.cities.values()), "unit_costs": 0,
                    "other_civs": 0}
        return {"income": {**income, "total": sum(income.values())},
                "expenses": {**expenses, "total": sum(expenses.values())}}

    def city_screen(self, c, worked) -> dict:
        """The city command's culture, resources and citizens (the client's city screen)."""
        per_turn = sum({"Palace": 1, "Temple": 2}.get(b, 0) for b in c["buildings"])
        total = per_turn * max(0, self.turn - c.get("founded", self.turn))
        goods = {"strategic": [], "luxuries": []}
        for p in area(c["pos"], 2):
            if RESOURCES.get(p) in TRADE_GOODS:
                kind, icon = TRADE_GOODS[RESOURCES[p]]
                goods[kind].append({"name": RESOURCES[p], "icon": icon, "count": 1})
        happy, content, unhappy = self.mood(c)
        moods = ["happy"] * happy + ["content"] * content + ["unhappy"] * unhappy
        return {"culture": {"per_turn": per_turn, "total": total, "next_border": 10 ** len(str(max(1, total)))},
                **goods, "specialists": [],
                "citizens": [{"mood": m, "works": "tile",
                              "tile": [worked[i][0], worked[i][1]] if i < len(worked) else None}
                             for i, m in enumerate(moods)]}

    def city_view(self, c) -> dict:
        food_pt, spt = 2, self.spt(c)
        needed = 10 + 5 * c["size"]
        item = c["producing"]
        cost = ITEMS[item][1] if item else None
        happy, content, unhappy = self.mood(c)
        return {"id": c["id"], "name": c["name"], "x": c["pos"][0], "y": c["pos"][1], "size": c["size"],
                "capital": "Palace" in c["buildings"], "food_stored": c["food"], "food_needed": needed,
                "food_per_turn": food_pt, "turns_to_grow": ceil_div(needed - c["food"], food_pt),
                "shields_per_turn": spt, "producing": item, "producing_source": c["source"] if item else None,
                "production_stored": c["shields"], "production_cost": cost,
                "turns_to_complete": ceil_div(cost - c["shields"], spt) if item and cost and spt else None,
                "disorder": c["disorder"], "happy": happy, "content": content, "unhappy": unhappy,
                "defenders": self.defenders(c), "riot_risk": self.riot_risk(c), "capped": self.capped(c),
                "shields_lost_last_turn": c["lost"], "buildings": c["buildings"],
                "food_eaten": 2 * c["size"], "commerce": self.commerce()[c["id"]],
                "shields": {"total": spt or 1 + c["size"], "useful": spt, "corrupt": 0 if spt else 1 + c["size"]},
                "maintenance": self.maintenance(c)}

    def city(self, key):
        found = self.cities.get(key)
        found = found or next((c for c in self.cities.values() if c["name"].lower() == key.lower()), None)
        if found is None:
            raise Refused("unknown_city", f"you have no city {key!r}.", sorted(self.cities))
        return found

    def options(self, c) -> list:
        spt = self.spt(c)
        return [{"name": n, "kind": k, "cost": cost, "turns": ceil_div(cost, spt) if cost and spt else None}
                for n, (k, cost, tech) in ITEMS.items() if tech is None or tech in self.known]

    # ---- research ----

    def available(self):
        return [t for t, (_, pre, _u) in TECHS.items() if t not in self.known and all(p in self.known for p in pre)]

    def bpt(self) -> int:
        return (2 + 2 * len(self.cities)) * self.rates["science"] // 6

    def tech_turns(self, t):
        bpt = self.bpt()
        return ceil_div(TECHS[t][0] - (self.beakers if t == self.research else 0), bpt) if bpt else None

    def gpt(self) -> int:
        return len(self.cities) * (10 - self.rates["science"] - self.rates["luxury"]) // 4

    # ---- views ----

    def score(self) -> dict:
        tiles = {p for c in self.cities.values() for p in area(c["pos"], 1) if is_land(p)}
        cities, pop = len(self.cities), sum(c["size"] for c in self.cities.values())
        return {"total": 10 * cities + 3 * pop + len(tiles) + 4 * len(self.known), "cities": cities, "pop": pop,
                "tiles": len(tiles), "techs": len(self.known)}

    def blockers(self) -> list:
        out = []
        if self.cities and not self.research:
            out.append({"kind": "no_research", "message": "nothing is being researched"})
        elif self.research_pending:
            out.append({"kind": "choose_research", "message": f"the engine picked {self.research} to research next"})
        for c in self.cities.values():
            if c["disorder"]:
                out.append({"kind": "disorder", "id": c["id"], "message": (
                    f"{c['name']} is in civil disorder: raise luxury (set_rates), move a military unit into the "
                    "city, or let it shrink")})
            if not c["producing"]:
                out.append({"kind": "no_production", "id": c["id"], "message": f"{c['name']} is producing nothing"})
            elif c["pending"]:
                out.append({"kind": "choose_production", "id": c["id"],
                            "message": f"the engine picked {c['producing']} for {c['name']}"})
        out += [{"kind": "idle_unit", "id": u["id"], "message": f"{u['id']} {u['type']} has moves and no orders"}
                for u in self.units.values() if self.unit_view(u)["needs_orders"]]
        return out

    def game_over(self) -> bool:
        return self.turn >= self.turn_limit

    def state(self) -> dict:
        r = self.research
        return {
            "turn": self.turn, "turn_limit": self.turn_limit, "game_over": self.game_over(), "defeated": False,
            "civ": self.civ, "era": 0, "government": self.government, "anarchy_until": self.anarchy_until,
            "tile_penalty": self.government == "Despotism",
            "governments": [g for g in self.governments_view()["available"]
                            if g["name"] != self.government and not self.anarchy_until],
            "revolution_target": self.revolution_target,
            "gold": self.gold, "gold_per_turn": self.gpt(), "finance": self.finance(),
            "rates": {"tax": 10 - self.rates["science"] - self.rates["luxury"], **self.rates},
            "research": {"current": r, "turns_left": self.tech_turns(r) if r else None, "beakers": self.beakers,
                         "cost": TECHS[r][0] if r else None, "queue": list(self.queue),
                         "source": self.research_source if r else None},
            "known_techs": list(self.known), "score": self.score(),
            "explored_pct": round(100 * len(self.explored) / (WIDTH * HEIGHT / 2), 1),
            "cities": [self.city_view(c) for c in self.cities.values()],
            "units": [self.unit_view(u) for u in self.units.values()],
            "rivals": [{"civ": o, "met": o == "Greece" and self.met, "at_war": o in self.wars,
                        "peace_price": self.peace_price(o), "cities_seen": 1 if o == "Greece" else 0}
                       for o in self.opponents],
            "blockers": self.blockers(), "decisions": json.loads(json.dumps(self.decisions)),
            "last_events": self.last_events,
        }

    def barbarian(self):
        return (18, 12) if self.turn >= 3 and not self.barbarian_killed else None

    # ---- government and diplomacy ----

    def peace_price(self, civ):
        return PEACE_PRICE if civ in self.wars and self.turn >= self.talks_from.get(civ, 0) else None

    def civ_view(self, civ) -> dict:
        war = civ in self.wars
        return {"civ": civ, "at_war": war, "talks": self.turn >= self.talks_from.get(civ, 0),
                "refuses_talks_until": (self.talks_from[civ] if war and self.turn < self.talks_from.get(civ, 0)
                                        else None),
                "peace_price": self.peace_price(civ), "government": "Despotism", "military_vs_yours": 1.5,
                "score": {"total": 30 + self.turn, "cities": 1, "pop": 2, "tiles": 9, "techs": 3}, "at_war_with": []}

    def met_civ(self, name) -> str:
        met = ["Greece"] if self.met else []
        if name not in met:
            raise Refused("unknown_civ", f"'{name}' is not a civilization you have met.", met)
        return name

    def governments_view(self) -> dict:
        return {"current": self.government, "anarchy_until": self.anarchy_until,
                "revolution_target": self.revolution_target,
                "available": [{"name": g, "corruption": "problematic", "hurry": "gold", "tile_penalty": False,
                               "trade_bonus": False, "unit_cost": 1, "free_units_per_city": 3} for g in GOVERNMENTS]}

    def revolution(self, a) -> dict:
        if a["government"] not in GOVERNMENTS:
            raise Refused("unknown_government", f"'{a['government']}' is not a government you can choose.", GOVERNMENTS)
        self.government, self.anarchy_until, self.revolution_target = "Anarchy", self.turn + 2, a["government"]
        return {"message": f"Revolution: anarchy until turn {self.anarchy_until}, then {a['government']}.",
                "government": self.governments_view()}

    def declare_war(self, a) -> dict:
        civ = self.met_civ(a["civ"])
        if civ in self.wars:
            raise Refused("already_at_war", f"You are already at war with {civ}.")
        self.wars.add(civ)
        self.talks_from[civ] = self.turn + 2
        return {"message": f"Rome declared war on {civ}, which refuses to talk until turn {self.talks_from[civ]}.",
                "civ": self.civ_view(civ)}

    def propose_peace(self, a) -> dict:
        civ, gold = self.met_civ(a["civ"]), a.get("gold", 0)
        if civ not in self.wars:
            raise Refused("not_at_war", f"You are not at war with {civ}.")
        if self.turn < self.talks_from[civ]:
            raise Refused("no_talks", f"{civ} refuses to talk to you until turn {self.talks_from[civ]}.")
        if gold < PEACE_PRICE:
            raise Refused("price", f"{civ} wants {PEACE_PRICE} gold for peace; you offered {gold}.",
                          suggest=f'diplomacy(action="propose_peace", civ="{civ}", gold={PEACE_PRICE})')
        self.wars.discard(civ)
        self.gold -= gold
        return {"message": f"Peace with {civ}, for {gold} gold.", "civ": self.civ_view(civ)}

    def attack_targets(self, u) -> list:
        b = self.barbarian()
        if u["type"] != "Warrior" or u["moves"] <= 0 or b is None or dist(u["pos"], b) != 1:
            return []
        return [{"x": b[0], "y": b[1], **rel(u["pos"], b), "owner": "Barbarians", "defender": "Warrior 3/3 hp",
                 "city": None, "win_chance": 0.5}]

    def occupant(self, p) -> str | None:
        if p == self.foreign_city["pos"]:
            return "Athens (Greece)"
        if p == self.barbarian():
            return "a Barbarians Warrior"
        return None

    def map(self, a) -> dict:
        c, radius = (a["x"], a["y"]), min(a.get("radius", 3), 8)
        if not on_map(c):
            raise Refused("bad_target", f"({c[0]},{c[1]}) is not a tile; x+y must be even and on the map.")
        visible = {p for u in self.units.values() for p in area(u["pos"], 2)} | \
                  {p for ci in self.cities.values() for p in area(ci["pos"], 2)}
        tiles = []
        for p in sorted(area(c, radius), key=lambda q: (q[1], q[0])):
            if p not in self.explored:
                continue
            city = next(({"name": ci["name"], "owner": self.civ, "size": ci["size"], "id": ci["id"]}
                         for ci in self.cities.values() if ci["pos"] == p), None)
            if p == self.foreign_city["pos"]:
                city = {k: self.foreign_city[k] for k in ("name", "owner", "size")}
            units = [{"owner": self.civ, "type": u["type"], "count": 1, "id": u["id"]}
                     for u in self.units.values() if u["pos"] == p]
            if p in visible and p == self.barbarian():
                units.append({"owner": "Barbarians", "type": "Warrior", "count": 1})
            tiles.append({"x": p[0], "y": p[1], **rel(c, p), "visible": p in visible, "terrain": terrain(*p),
                          "overlay": OVERLAY.get(p), "resource": RESOURCES.get(p), "river": p in RIVER,
                          "improvements": [], "owner": self.civ if self.owned(p) else None,
                          "city": city, "units": units, "yield": tile_yield(p), "city_site": self.can_found(p)})
        return {"center": {"x": c[0], "y": c[1]}, "radius": radius, "tiles": tiles}

    def owned(self, p) -> bool:
        return any(dist(p, ci["pos"]) <= 1 for ci in self.cities.values())

    def site_score(self, p) -> int:
        ys = [tile_yield(q) for q in area(p, 1)]
        return sum(2 * y["food"] + y["shields"] + y["commerce"] for y in ys) + (5 if p in RIVER else 0)

    def site(self, origin, p, with_turns=True) -> dict:
        ys = [tile_yield(q) for q in area(p, 1)]
        return {"x": p[0], "y": p[1], "score": self.site_score(p), **rel(origin, p),
                "turns": dist(origin, p) if with_turns else None, "terrain": terrain(*p), "river": p in RIVER,
                "coastal": any(not is_land(q) for q in area(p, 1)),
                "yield": {k: sum(y[k] for y in ys) for k in ("food", "shields", "commerce")}}

    def sites(self, origin, top, with_turns=True) -> list:
        cands = [p for p in self.explored if is_land(p) and self.can_found(p)["ok"]
                 and all(dist(p, ci["pos"]) >= 3 for ci in self.cities.values())]
        ranked = sorted(cands, key=lambda p: (-self.site_score(p), dist(origin, p), p))[:top]
        return [self.site(origin, p, with_turns) for p in ranked]

    def nearby(self, origin, with_turns=True) -> list:
        cands = [p for p in area(origin, 4) if p in self.explored and self.can_found(p)["ok"]]
        return [self.site(origin, p, with_turns) for p in sorted(cands, key=lambda p: (-self.site_score(p), p))]

    # ---- orders ----

    def unit_order(self, a) -> dict:
        if self.game_over():
            raise Refused("game_over", "the game is over.")
        u = self.unit(a["unit"])
        order = a["order"]
        view = self.unit_view(u)
        if order not in view["orders"]:
            raise Refused("invalid_order", f"{u['id']} {u['type']} cannot {order}.", view["orders"])
        target = (a["x"], a["y"]) if "x" in a and "y" in a else None
        if order in ("settle", "goto"):
            if target is None or not on_map(target) or target not in self.explored:
                raise Refused("bad_target", f"{target} is not an explored tile; x+y must be even.")
            if who := self.occupant(target):
                where = f"({target[0]},{target[1]})"
                raise Refused("occupied", f"{where} is occupied by {who}; goto only moves peacefully.")
            if order == "settle" and not self.can_found(target)["ok"]:
                self.raise_cannot_found(u, target)
        if order in ("found_city", "build_road", "build_mine", "irrigate") and u["moves"] <= 0:
            raise Refused("no_moves", f"{u['id']} {u['type']} has no moves left this turn; give the order next turn.")
        city, path, msg = None, None, ""
        if order == "attack":
            if target != self.barbarian():
                raise Refused("bad_target", f"There is nothing to attack at {target}.", self.attack_targets(u))
            self.barbarian_killed, u["moves"] = True, 0.0
            civs = [self.civ, *self.opponents, "Barbarians"]
            battle = {"id": len(self.battles) + 1, "turn": self.turn, "kind": "attack",
                      "attacker": {"owner": civs.index(a.get("seat", self.civ)), "type": "Warrior", "x": u["pos"][0],
                                   "y": u["pos"][1], "id": u["id"], "hp_before": u["hp"], "hp_after": u["hp"] - 1,
                                   "hp_max": UNIT_STATS["Warrior"][1]},
                      "defender": {"owner": len(civs) - 1, "type": "Warrior", "x": target[0], "y": target[1],
                                   "id": None, "hp_before": 3, "hp_after": 0, "hp_max": 3},
                      "rounds": ["a", "d", "a", "a"], "winner": "attacker", "city": None, "razed": False}
            u["hp"] -= 1
            self.battles.append(battle)
            return {"message": f"{u['id']} Warrior attacked the Barbarians Warrior at ({target[0]},{target[1]}) "
                               "(win chance about 50%) and won: the Barbarians Warrior was destroyed.",
                    "unit": self.unit_view(u), "city": None, "path": None, "battle": battle}
        if order == "found_city" or (order == "settle" and target == u["pos"]):
            if not self.can_found(u["pos"])["ok"]:
                self.raise_cannot_found(u, u["pos"])
            c = self.found(u)
            city, msg = self.city_view(c), f"Founded {c['name']} at ({c['pos'][0]},{c['pos'][1]})."
        elif order in ("settle", "goto"):
            d = dist(u["pos"], target)
            u.update(status=order, target=target, steps=d, moves=0.0)
            path = {"length": d, "turns": d}
            msg = (f"{u['id']} {u['type']} will walk to ({target[0]},{target[1]})"
                   + (" and found a city on arrival." if order == "settle" else "."))
        elif order == "disband":
            del self.units[u["id"]]
            msg = f"{u['id']} {u['type']} disbanded."
        else:
            status = {"explore": "exploring", "auto_work": "auto_work", "fortify": "fortified", "wake": "idle",
                      "hold": "idle"}.get(order, f"working:{order}")
            u.update(status=status, target=None, steps=3)
            if order == "hold":
                u["moves"] = 0.0
            msg = f"{u['id']} {u['type']}: {order}."
        return {"message": msg, "unit": self.unit_view(u) if u["id"] in self.units else None, "city": city,
                "path": path, "battle": None}

    def raise_cannot_found(self, u, p):
        sites = self.sites(u["pos"], 3)
        suggest = (f'unit_order(unit="{u["id"]}", order="settle", x={sites[0]["x"]}, y={sites[0]["y"]})'
                   if sites else None)
        raise Refused("cannot_found", f"cannot found a city at ({p[0]},{p[1]}): {self.can_found(p)['reason']}.",
                      sites, suggest)

    def set_rates(self, a) -> dict:
        science = a.get("science", self.rates["science"])
        luxury = a.get("luxury", self.rates["luxury"])
        if not all(isinstance(v, int) and 0 <= v <= MAX_RATE for v in (science, luxury)) or science + luxury > 10:
            raise Refused("bad_rates", f"Under Despotism each rate is 0-{MAX_RATE} (in tenths) and science + luxury "
                          f"is at most 10; got science {science}, luxury {luxury}.")
        self.rates = {"science": science, "luxury": luxury}
        self.refresh_moods()
        r = self.research
        return {"message": f"Rates set: science {science}0%, tax {10 - science - luxury}0%, luxury {luxury}0%.",
                "rates": {"tax": 10 - science - luxury, **self.rates}, "gold_per_turn": self.gpt(),
                "turns_left_research": self.tech_turns(r) if r else None}

    def hurry(self, a) -> dict:
        c = self.city(a["city"])
        item = c["producing"]
        if c["disorder"]:
            raise Refused("cannot_hurry", f"{c['name']} is in disorder and cannot hurry production.")
        if not item or ITEMS[item][0] == "wealth":
            raise Refused("cannot_hurry", f"{c['name']} is not building anything that can be hurried.")
        pop = ceil_div(ITEMS[item][1] - c["shields"], 10)
        if pop > c["size"] / 2:
            raise Refused("cannot_hurry", f"Hurrying {item} in {c['name']} would take the lives of too many citizens "
                          f"({pop}); it has {c['size']}.")
        c["size"] -= pop
        c["shields"] = ITEMS[item][1]
        return {"message": f"{c['name']} hurried {item}: {pop} citizen(s) were put to work; it completes next turn.",
                "gold_cost": 0, "pop_cost": pop, "city": self.city_view(c)}

    def refresh_moods(self) -> list:
        events = []
        for c in self.cities.values():
            happy, _, unhappy = self.mood(c)
            now = unhappy > happy
            if now and not c["disorder"]:
                events.append({"turn": self.turn, "kind": "disorder_started", "text": f"{c['name']} fell into civil "
                               "disorder", "x": c["pos"][0], "y": c["pos"][1]})
            elif c["disorder"] and not now:
                events.append({"turn": self.turn, "kind": "disorder_ended", "text": f"{c['name']} is calm again",
                               "x": c["pos"][0], "y": c["pos"][1]})
            c["disorder"] = now
        return events

    # ---- turns ----

    def advance(self, policy=None) -> tuple[list, list]:
        events, auto = [], []
        if not self.research and self.available():
            self.research, self.research_source = self.available()[0], "engine"
            auto.append({"kind": "research_picked", "text": f"research picked: {self.research}"})
        if policy == "engine_ai":
            self.play_ai()
        elif policy == "settler_bot":
            self.play_ai()
        elif policy == "found_capital" and not self.cities:
            settler = next(u for u in self.units.values() if u["type"] == "Settler")
            self.found(settler)
        if policy:
            self.research_pending = False
            for c in self.cities.values():
                c["pending"] = False
        t = self.turn
        for u in list(self.units.values()):
            if u["status"] in ("settle", "goto"):
                u["steps"] -= 1
                if u["steps"] <= 0:
                    u["pos"] = u["target"]
                    self.explored |= set(area(u["pos"], 2))
                    if u["status"] == "settle":
                        if self.can_found(u["pos"])["ok"]:
                            c = self.found(u)
                            events.append({"turn": t, "kind": "city_founded", "text": f"{c['name']} founded",
                                           "x": c["pos"][0], "y": c["pos"][1]})
                            continue
                        events.append({"turn": t, "kind": "settle_failed", "text": f"{u['id']} could not found a city "
                                       f"at ({u['pos'][0]},{u['pos'][1]})", "x": u["pos"][0], "y": u["pos"][1]})
                    u.update(status="idle", target=None)
            elif u["status"] == "exploring":
                self.explored |= set(area(u["pos"], 4))
                u["steps"] -= 1
                if u["steps"] <= 0:
                    u["status"] = "idle"
                    events.append({"turn": t, "kind": "explore_done", "text": f"{u['id']} {u['type']} has nothing "
                                   "reachable left to explore", "x": u["pos"][0], "y": u["pos"][1]})
            elif u["status"].startswith("working:"):
                u["steps"] -= 1
                if u["steps"] <= 0:
                    job, u["status"] = u["status"].split(":", 1)[1], "idle"
                    events.append({"turn": t, "kind": "job_done", "text": f"{u['id']} Worker finished {job}",
                                   "x": u["pos"][0], "y": u["pos"][1]})
        for c in list(self.cities.values()):
            self.grow_and_build(c, t, events)
        if self.research:
            self.beakers += self.bpt()
            if self.beakers >= TECHS[self.research][0]:
                events.append({"turn": t, "kind": "tech_learned", "text": f"learned {self.research}"})
                self.decisions["research"][self.research_source or "engine"] += 1
                self.known.append(self.research)
                self.beakers = 0
                self.queue = [q for q in self.queue if q != self.research]
                if self.queue:
                    self.research, self.research_source = self.queue[0], self.research_source
                elif self.available():
                    self.research, self.research_source = self.available()[0], "engine"
                    self.research_pending = not policy
                else:
                    self.research = None
        events += self.refresh_moods()
        at_risk = {c["id"] for c in self.cities.values() if self.riot_risk(c)}
        events += [{"turn": t, "kind": "riot_risk", "text": f"{c['name']} will riot if it grows: garrison it or raise "
                    "luxury", "x": c["pos"][0], "y": c["pos"][1]}
                   for c in self.cities.values() if c["id"] in at_risk - self.risk_seen]
        self.risk_seen = at_risk
        if self.turn + 1 >= 3 and self.cities and "barbarian" not in self.threats_seen:
            self.threats_seen.add("barbarian")
            capital = next(iter(self.cities.values()))
            events.append({"turn": t + 1, "kind": "threat", "text": f"Barbarians Warrior 3 tiles E of "
                           f"{capital['name']}", "x": 18, "y": 12})
            if not self.defenders(capital):
                events.append({"turn": t + 1, "kind": "defenseless", "text": f"{capital['name']} has no defender "
                               "and a hostile unit is 3 tiles away", "x": capital["pos"][0], "y": capital["pos"][1]})
        if not self.met and any(dist(u["pos"], self.foreign_city["pos"]) <= 4 for u in self.units.values()):
            self.met = True
            events.append({"turn": t, "kind": "contact", "text": "met Greece"})
        self.turn += 1
        if self.anarchy_until and self.turn >= self.anarchy_until:
            self.government, self.anarchy_until, self.revolution_target = self.revolution_target, None, None
            auto.append({"kind": "government_picked",
                         "text": f"Anarchy ended and the government became {self.government}."})
        self.gold += self.gpt()
        for u in self.units.values():
            u["moves"] = float(UNIT_STATS[u["type"]][0])
        return events, auto

    def grow_and_build(self, c, t, events):
        view = self.city_view(c)
        c["lost"] = 0
        c["food"] += view["food_per_turn"]
        if c["food"] >= view["food_needed"]:
            c["food"], c["size"] = 0, c["size"] + 1
            events.append({"turn": t, "kind": "city_grew", "text": f"{c['name']} grew to size {c['size']}"})
        item = c["producing"]
        if not item or c["disorder"]:
            return
        cost = ITEMS[item][1]
        c["shields"] += view["shields_per_turn"]
        if cost and c["shields"] >= cost and c["size"] > POP_COST.get(item, 0):
            c["shields"] = 0
            if ITEMS[item][0] == "unit":
                c["size"] -= POP_COST.get(item, 0)
                self.add_unit(item, c["pos"])
            else:
                c["buildings"].append(item)
            self.decisions["production"][c["source"]] += 1
            events.append({"turn": t, "kind": "built", "text": f"{c['name']} completed {item}"})
            c["producing"], c["source"], c["pending"] = ("Warrior" if item == "Settler" else item), "engine", True
            c["capped_seen"] = None
        elif cost and c["shields"] > cost:
            c["lost"], c["shields"] = c["shields"] - cost, cost
            if c["capped_seen"] != item:
                c["capped_seen"] = item
                events.append({"turn": t, "kind": "production_capped", "text": f"{c['name']}'s {item} is complete "
                               f"but waits for size {POP_COST[item] + 1}", "x": c["pos"][0], "y": c["pos"][1]})

    def play_ai(self):
        for u in list(self.units.values()):
            if u["type"] == "Settler" and u["status"] == "idle":
                if self.can_found(u["pos"])["ok"] and all(dist(u["pos"], c["pos"]) >= 3 for c in self.cities.values()):
                    self.found(u)
                elif sites := self.sites(u["pos"], 1):
                    d = dist(u["pos"], (sites[0]["x"], sites[0]["y"]))
                    u.update(status="settle", target=(sites[0]["x"], sites[0]["y"]), steps=d)
            elif u["status"] == "idle":
                u["status"] = "auto_work" if u["type"] == "Worker" else "fortified"
        for c in self.cities.values():
            c["producing"] = "Settler" if c["size"] >= 2 else "Warrior"

    def end_turn(self, a, on_turn) -> dict:
        if self.game_over():
            raise Refused("game_over", "the game is over.")
        blockers = self.blockers()
        if blockers and not a.get("skip_idle"):
            return {"blocked": True, "blockers": blockers}
        if len(self.seats) > 1:
            self.ready.add(a.get("seat", self.civ))
            if waiting := [c for c in self.seats if c not in self.ready]:
                return {"blocked": False, "turns_advanced": 0, "turn": self.turn, "waiting_for": waiting}
            self.ready.clear()
            res = self._advance({}, on_turn)
            return {"turn": self.turn, "seats": {c: dict(res) for c in self.seats}}
        return self._advance(a, on_turn)

    def _advance(self, a, on_turn) -> dict:
        max_turns = min(a.get("max_turns", 1), 20) if a.get("until_attention") else 1
        events, auto, n = [], [], 0
        while True:
            for u in self.units.values():
                if self.unit_view(u)["needs_orders"]:
                    u["moves"] = 0.0
            self.research_pending = False
            for c in self.cities.values():
                c["pending"] = False
            ev, au = self.advance()
            self.last_events = ev
            on_turn()
            events += ev
            auto += au
            n += 1
            if self.game_over() or n >= max_turns or self.blockers() or any(e["kind"] in ATTENTION for e in ev):
                break
        self.last_events = events
        return {"blocked": False, "turns_advanced": n, "turn": self.turn, "game_over": self.game_over(),
                "defeated": False, "events": events, "auto": auto}

    def autoplay(self, a, on_turn) -> dict:
        trajectory = []
        for _ in range(a["turns"]):
            if self.game_over():
                break
            for u in self.units.values():
                if self.unit_view(u)["needs_orders"]:
                    u["moves"] = 0.0
            self.last_events, _ = self.advance(policy=a.get("policy", "null"))
            on_turn()
            trajectory.append({"turn": self.turn, "score": self.score()})
        out = {"turn": self.turn, "game_over": self.game_over(), "defeated": False, "score": self.score()}
        if a.get("record"):
            out["trajectory"] = trajectory
        return out

    def world(self) -> dict:
        """The snapshot, schema 2 (docs/viewer.md): one seat, the player's own; the opponents' fields are made up."""
        civs = [self.civ, *self.opponents, "Barbarians"]
        index = {civ: i for i, civ in enumerate(civs)}
        players = [{"index": i, "civ": civ, "is_human": i == 0, "label": None, "defeated": False,
                    "color": COLORS.get(civ, [128, 128, 128]),
                    "score": self.score() if i == 0 else {"total": 30 + self.turn, "cities": 1, "pop": 2, "tiles": 9,
                                                          "techs": 3}} for i, civ in enumerate(civs)]
        for p in players[:-1]:
            me = p["index"] == 0
            p.update(gold=self.gold if me else 20, government=self.government if me else "Despotism",
                     research=self.research if me else None,
                     at_war=sorted(index[c] for c in self.wars) if me else [0] if p["civ"] in self.wars else [],
                     contacts=[index["Greece"]] if me and self.met else [0] if p["civ"] == "Greece" and self.met
                     else [])
        athens = self.foreign_city["pos"]
        tiles = []
        for y in range(HEIGHT):
            for x in range(y % 2, WIDTH, 2):
                p = (x, y)
                base = terrain(x, y).lower()
                over = OVERLAY.get(p)
                owner = 0 if self.owned(p) else 1 if dist(p, athens) <= 1 else -1
                tiles.append([x, y, base, over.lower() if over else None, owner, int(p in RIVER),
                              int(p in self.explored)])
        cities = [{"id": int(c["id"][1:]), "x": c["pos"][0], "y": c["pos"][1], "name": c["name"], "owner": 0,
                   "size": c["size"], "capital": "Palace" in c["buildings"], "production": c["producing"]}
                  for c in self.cities.values()]
        cities.append({"id": 1000, "x": athens[0], "y": athens[1], "name": "Athens", "owner": 1, "size": 2,
                       "capital": True, "production": "Warrior"})
        units = [{"id": int(u["id"][1:]), "x": u["pos"][0], "y": u["pos"][1], "owner": 0, "type": u["type"]}
                 for u in self.units.values()]
        if b := self.barbarian():
            units.append({"id": 1000, "x": b[0], "y": b[1], "owner": len(civs) - 1, "type": "Warrior"})
        return {"schema": 2, "turn": self.turn, "turn_limit": self.turn_limit, "seed": self.seed,
                "map": {"width": WIDTH, "height": HEIGHT, "wrap_x": True},
                "seats": [{"index": index[c], "civ": c, "label": self.labels.get(c)} for c in self.seats],
                "players": players, "tiles": tiles,
                "cities": cities, "units": units, "events": list(self.last_events)}

    def known_map(self, a) -> dict:
        """docs/play.md section 3: the explored tiles, the cities on them and the units on visible ones. The seat
        asking owns the scripted game's units and cities (every seat plays the same ones)."""
        me = a.get("seat", self.civ)
        civs = [self.civ, *self.opponents, "Barbarians"]
        mine = civs.index(me)
        visible = {p for u in self.units.values() for p in area(u["pos"], 2)} | \
                  {p for ci in self.cities.values() for p in area(ci["pos"], 2)}
        athens = self.foreign_city["pos"]
        greece = civs.index("Greece") if "Greece" in civs else -1
        tiles = []
        for p in sorted(self.explored, key=lambda q: (q[1], q[0])):
            if not on_map(p):
                continue
            over = OVERLAY.get(p)
            owner = mine if self.owned(p) else greece if dist(p, athens) <= 1 else -1
            # The river runs along the NE edges of the RIVER tiles (bit 1); its far bank is left out, like in `map`.
            # No bonus grassland: the scripted grassland yields no shield.
            tiles.append([p[0], p[1], terrain(*p).lower(), over.lower() if over else None, int(p in RIVER), owner,
                          int(p in visible), RESOURCES.get(p), [], 0])
        cities = [{"x": c["pos"][0], "y": c["pos"][1], "name": c["name"], "owner": mine, "size": c["size"],
                   "capital": "Palace" in c["buildings"], "era": 0, "walls": "Walls" in c["buildings"],
                   "disorder": c["disorder"], "id": c["id"], "producing": c["producing"],
                   "turns_to_complete": self.city_view(c)["turns_to_complete"],
                   "turns_to_grow": self.city_view(c)["turns_to_grow"],
                   "starving": self.city_view(c)["food_per_turn"] < 0} for c in self.cities.values()]
        if athens in self.explored:
            cities.append({"x": athens[0], "y": athens[1], "name": "Athens", "owner": greece, "size": 2,
                           "capital": True, "era": 0, "walls": False, "disorder": False})
        units = [{"x": u["pos"][0], "y": u["pos"][1], "owner": mine, "type": u["type"], "count": 1, "id": u["id"],
                  "hp": u["hp"], "hp_max": UNIT_STATS[u["type"]][1], "fortified": u["status"] == "fortified",
                  "combat": u["type"] not in ("Settler", "Worker")} for u in self.units.values()]
        if (b := self.barbarian()) and b in visible:
            units.append({"x": b[0], "y": b[1], "owner": len(civs) - 1, "type": "Warrior", "count": 1,
                          "hp": 3, "hp_max": 3, "fortified": False, "combat": True})
        colors = client_colors(civs)
        return {"turn": self.turn, "width": WIDTH, "height": HEIGHT, "wrap_x": True,
                "players": [{"index": i, "civ": c, "barbarian": c == "Barbarians", "me": i == mine,
                             "color": CLIENT_COLORS[colors[i]]} for i, c in enumerate(civs)],
                "tiles": tiles, "cities": cities, "units": units,
                "battles": [b for b in self.battles if b["turn"] >= self.turn - 1]}

    def handle(self, cmd, a, on_turn) -> dict:
        if a.get("seat", self.civ) not in self.seats:
            raise Refused("unknown_seat", f"{a['seat']!r} is not a seat.", self.seats)
        if cmd == "state":
            return self.state()
        if cmd == "known_map":
            return self.known_map(a)
        if cmd == "map":
            return self.map(a)
        if cmd == "world":
            return self.world()
        if cmd == "city_sites":
            if "unit" in a:
                origin, with_turns = self.unit(a["unit"])["pos"], True
            else:
                settler = next((u for u in self.units.values() if u["type"] == "Settler"), None)
                capital = next(iter(self.cities.values()), None)
                origin = settler["pos"] if settler else capital["pos"] if capital else START
                with_turns = settler is not None
            return {"origin": {"x": origin[0], "y": origin[1]},
                    "sites": self.sites(origin, a.get("top", 5), with_turns),
                    "nearby": self.nearby(origin, with_turns),
                    "note": "only sites on the unit's continent are scored"}
        if cmd == "unit_order":
            return self.unit_order(a)
        if cmd == "city":
            c = self.city(a["city"])
            tiles = area(c["pos"], 1)[: c["size"] + 1]
            worked = [p for p in tiles if p != c["pos"]][: c["size"]]
            return {**self.city_view(c), "options": self.options(c),
                    "tiles_worked": [{"x": p[0], "y": p[1]} for p in tiles],
                    "worked": [[p[0], p[1], *tile_yield(p).values()] for p in [c["pos"], *worked]],
                    "workable": [[p[0], p[1]] for p in area(c["pos"], 2) if p != c["pos"] and on_map(p)],
                    **self.city_screen(c, worked)}
        if cmd == "set_production":
            c = self.city(a["city"])
            names = [o["name"] for o in self.options(c)]
            item = next((n for n in names if n.lower() == a["item"].lower()), None)
            if item is None:
                raise Refused("unknown_item", f"{c['name']} cannot build {a['item']!r}.", names)
            c["producing"], c["source"], c["pending"] = item, "agent", False
            return {"message": f"{c['name']} now builds {item}.", "city": self.city_view(c)}
        if cmd == "set_rates":
            return self.set_rates(a)
        if cmd == "revolution":
            return self.revolution(a)
        if cmd == "diplomacy":
            return {"civs": [self.civ_view("Greece")] if self.met else [], "unmet": len(self.opponents) - self.met}
        if cmd == "declare_war":
            return self.declare_war(a)
        if cmd == "propose_peace":
            return self.propose_peace(a)
        if cmd == "hurry":
            return self.hurry(a)
        if cmd == "techs":
            r = self.research
            return {"current": r, "turns_left": self.tech_turns(r) if r else None, "known": list(self.known),
                    "available": [{"name": t, "cost": TECHS[t][0], "turns": self.tech_turns(t), "era": "Ancient",
                                   "unlocks": TECHS[t][2]} for t in self.available()]}
        if cmd == "set_research":
            tech = next((t for t in TECHS if t.lower() == a["tech"].lower()), None)
            if tech is None:
                raise Refused("unknown_tech", f"there is no tech {a['tech']!r}.", self.available())
            if tech in self.known:
                raise Refused("already_known", f"you already know {tech}.")
            queue, todo = [], [tech]
            while todo:
                t = todo.pop()
                if t in self.known or t in queue:
                    continue
                missing = [p for p in TECHS[t][1] if p not in self.known and p not in queue]
                if missing:
                    todo += [t] + missing
                    continue
                queue.append(t)
            self.queue, self.research = queue, queue[0]
            self.research_source, self.research_pending = "agent", False
            turns = self.tech_turns(queue[0])
            return {"message": f"Researching {queue[0]} ({turns} turns).", "current": queue[0], "queue": queue}
        if cmd == "end_turn":
            return self.end_turn(a, on_turn)
        if cmd == "autoplay":
            return self.autoplay(a, on_turn)
        if cmd == "score":
            return {"turn": self.turn, "human": self.score(),
                    "players": [{"civ": self.civ, "is_human": True, "defeated": False, "score": self.score()}]
                    + [{"civ": o, "is_human": False, "defeated": False,
                        "score": {"total": 30 + self.turn, "cities": 1, "pop": 2, "tiles": 9, "techs": 3}}
                       for o in self.opponents],
                    "human_share": {"land": 0.05, "pop": 0.2}}
        if cmd == "_city":
            c = self.city(a.pop("id"))
            c.update(a)
            self.refresh_moods()
            return self.city_view(c)
        if cmd == "_unit":
            u = self.unit(a.pop("id"))
            u.update({k: tuple(v) if k == "pos" else v for k, v in a.items()})
            return self.unit_view(u)
        if cmd == "_game":
            for k, v in a.items():
                setattr(self, k, v)
            return {}
        raise Refused("unknown_command", f"unknown command {cmd!r}.")


def reply(rid, result=None, error=None):
    msg = {"id": rid, "ok": error is None}
    msg["result" if error is None else "error"] = result if error is None else error
    OUT.write(json.dumps(msg) + "\n")
    OUT.flush()


def flags(argv) -> dict:
    out, it = {}, iter(argv)
    for arg in it:
        if arg.startswith("--"):
            out[arg[2:]] = next(it, None)
    return out


def main():
    opts = flags(sys.argv[1:])
    record = Path(opts["record"]) if opts.get("record") else None
    autosave = Path(opts["autosave"]) if opts.get("autosave") else None
    saves = Path(opts["saves"]) if opts.get("saves") else None
    crash_on = os.environ.get("FAKE_BRIDGE_CRASH_ON")
    noise_kb = int(os.environ.get("FAKE_BRIDGE_STDERR_KB", "0"))
    for _ in range(noise_kb):
        sys.stderr.write("[INF] engine log line padding padding padding padding padding padding padding pad\n" * 13)
    sys.stderr.flush()
    if os.environ.get("FAKE_BRIDGE_STDOUT_NOISE"):
        OUT.write("Loading ruleset civ3...\n")
    reply(0, {"ready": True, "version": "fake-2"})
    game = None

    def on_turn():
        """What the bridge does at every human turn start: snapshot for the recording, then the autosave."""
        if record:
            record.mkdir(parents=True, exist_ok=True)
            with gzip.open(record / f"turn-{game.turn:04d}.json.gz", "wt") as f:
                json.dump(game.world(), f)
        if autosave:
            autosave.mkdir(parents=True, exist_ok=True)
            blob = base64.b64encode(pickle.dumps(game)).decode()
            (autosave / "autosave.json").write_text(json.dumps({"pickle": blob}))
        if saves:
            saves.mkdir(parents=True, exist_ok=True)
            with gzip.open(saves / f"turn-{game.turn:04d}.json.gz", "wt") as f:
                json.dump({"format": 1, "bridge": {}, "game": {"turn": game.turn}}, f)

    def started():
        seats = {"seats": [{"civ": c, "label": game.labels.get(c)} for c in game.seats]} if len(game.seats) > 1 else {}
        return {"turn": game.turn, "turn_limit": game.turn_limit, "seed": game.seed, "civ": game.civ,
                "opponents": game.opponents, "map": {"width": WIDTH, "height": HEIGHT, "wrap_x": True}, **seats}

    for line in sys.stdin:
        req = json.loads(line)
        rid, cmd, a = req["id"], req["cmd"], req.get("args") or {}
        if cmd == crash_on:
            os._exit(4)
        try:
            if cmd == "_sleep":
                time.sleep(a["seconds"])
                result = {}
            elif cmd == "_big":
                result = {"blob": "x" * a["size"]}
            elif cmd == "_crash":
                sys.stderr.write("Unhandled exception. System.NullReferenceException: boom\n")
                sys.stderr.flush()
                os._exit(3)
            elif cmd in ("new_game", "load"):
                if game is not None:
                    raise Refused("already_started", "a game is already running in this process.")
                if cmd == "load":
                    try:
                        game = pickle.loads(base64.b64decode(json.loads(Path(a["path"]).read_text())["pickle"]))
                    except (OSError, ValueError, KeyError) as e:
                        raise Refused("bad_save", f"cannot load {a.get('path')!r}: {e}") from None
                else:
                    if a.get("size", "Tiny") not in SIZES:
                        raise Refused("bad_args", f"unknown size {a['size']!r}.", SIZES)
                    if a.get("civ", "Rome") not in ["Rome", *CIVS]:
                        raise Refused("bad_args", f"unknown civ {a['civ']!r}.", ["Rome", *CIVS])
                    game = Game(a)
                    on_turn()
                result = started()
            elif game is None:
                raise Refused("no_game", "no game has been started; send new_game first.")
            else:
                result = game.handle(cmd, a, on_turn)
            reply(rid, result)
        except Refused as e:
            reply(rid, error=e.error)


if __name__ == "__main__":
    main()
