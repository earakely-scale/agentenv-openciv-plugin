"""Scripted players for test matches: no LLM, but every move goes through the env's MCP tools, as an agent's would.

    .venv/bin/python playtest/bots.py --out /tmp/match-9                     # 9 seats, Standard map, 200 turns
    .venv/bin/python playtest/bots.py --seats 3 --turns 30 --out /tmp/match-3 --fast
    .venv/bin/python playtest/bots.py --seats 2 --humans Rome=you --turns 50     # you play Rome in the browser

Starts the env locally (no Docker) on a copy of the bridge, starts a match the way the `frontier` task does (the
`urn:openciv3:new-game/v1` extension with civs, seats, labels, size and turn limit), and plays one asyncio task per
seat. Each is an MCP client sending its seat's `X-OpenCiv3-Seat` header and plays its turns with real tool calls:
get_turn_brief, list_units, find_city_sites + settle, set_production, research, auto_work, fortify and explore,
buy, revolution, set_rates, plan, view_map, diplomacy and message, then end_turn with a note on what it did. Seats
have personalities (expansionist, builder, aggressive); the aggressive ones threaten a neighbour, declare war on it
mid-game, build an army, march on its cities and attack them, and later offer peace; the others now and then greet,
warn or court the other leaders, and answer what they are sent. So a match shows everything spectators can see: plans,
notes and messages too. About 2% of the calls are deliberately wrong (an unknown unit, an item the city cannot build,
...) so the failure display has something to show.

The output directory gets everything a viewer needs: record/ (the bridge's turn-*.json.gz snapshots), saves/ (the
engine's per-turn saves), autosave/, actions.jsonl (the env's action log, every seat) and actions/<label>.jsonl,
summary.json (data/get at the end), bots.json (the bots' own counters and notable moves), server.log, bots.log, and
match.json (seats, labels, seed, the live URL, timings and what happened: wars, peace, cities captured and razed).

With --humans, those civs are human seats (docs/play.md): the bots leave them alone, and the script prints each one's
play link (`PLAY Rome (you): http://.../play#token=...`) and writes them to play.json, for a person or a browser
script (playtest/play_e2e.mjs) to play.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import datetime
import gzip
import json
import os
import random
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from agentenv_protocol import client as env_client
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
NEW_GAME = "urn:openciv3:new-game/v1"
SEAT_HEADER = "X-OpenCiv3-Seat"
DEFAULT_SEATS = [("Rome", "opus"), ("Greece", "sonnet"), ("Egypt", "haiku"), ("America", "sol"), ("Babylon", "luna"),
                 ("Persia", "terra"), ("England", "gemini"), ("Carthage", "grok"), ("China", "kimi")]
DEFAULT_PERSONALITIES = {"opus": "aggressive", "sonnet": "builder", "haiku": "expansionist", "sol": "expansionist",
                         "luna": "builder", "terra": "aggressive", "gemini": "builder", "grok": "aggressive",
                         "kimi": "expansionist"}
PERSONALITIES = ("expansionist", "builder", "aggressive")

# Land units of the Civ III ruleset: (attack, defense, movement). Bombard-only units are left out.
UNITS = {
    "Warrior": (1, 1, 1), "Archer": (2, 1, 1), "Spearman": (1, 2, 1), "Swordsman": (3, 2, 1), "Chariot": (1, 1, 2),
    "Horseman": (2, 1, 2), "Pikeman": (1, 3, 1), "Longbowman": (4, 1, 1), "Musketman": (2, 4, 1),
    "Knight": (4, 3, 2), "Crusader": (5, 3, 1), "Rifleman": (4, 6, 1), "Cavalry": (6, 3, 3), "Infantry": (6, 10, 1),
    "Tank": (16, 8, 2), "Mech Infantry": (12, 18, 2), "Modern Armor": (24, 16, 3), "Marine": (12, 6, 1),
    "Guerilla": (6, 6, 1), "Medieval Infantry": (4, 2, 1), "Ancient Cavalry": (3, 2, 2),
    "Jaguar Warrior": (1, 1, 2), "Bowman": (2, 2, 1), "Hoplite": (1, 3, 1), "Impi": (1, 2, 2), "Legionary": (3, 3, 1),
    "Immortals": (4, 2, 1), "War Chariot": (2, 1, 2), "Rider": (4, 3, 3), "Mounted Warrior": (3, 1, 2),
    "Musketeer": (2, 5, 1), "Samurai": (4, 4, 2), "War Elephant": (4, 3, 2), "Cossack": (6, 3, 3),
    "Panzer": (16, 8, 3), "Keshik": (4, 2, 2), "Conquistador": (3, 2, 2), "Berserk": (6, 2, 1), "Sipahi": (8, 3, 3),
    "Gallic Swordsman": (3, 2, 2), "Ansar Warrior": (4, 2, 3), "Numidian Mercenary": (2, 3, 1),
    "Enkidu Warrior": (1, 2, 1), "Three-Man Chariot": (2, 2, 2), "Swiss Mercenary": (1, 4, 1),
    "Javelin Thrower": (2, 2, 1),
}
CIVILIAN = {"Settler", "Worker", "Scout", "Explorer", "Leader", "Army", "Catapult", "Cannon", "Artillery", "Trebuchet",
            "Hwach'a", "Radar Artillery", "Chasqui Scout"}
BUILDINGS = {
    "builder": ["Temple", "Library", "Granary", "Marketplace", "Harbor", "Courthouse", "Aqueduct", "Colosseum",
                "University", "Cathedral", "Bank", "Walls"],
    "expansionist": ["Granary", "Temple", "Library", "Marketplace", "Harbor", "Aqueduct", "Courthouse", "Colosseum"],
    "aggressive": ["Barracks", "Temple", "Granary", "Library", "Marketplace", "Walls", "Aqueduct", "Colosseum"],
}
WONDERS = ["The Pyramids", "The Great Library", "The Colossus", "The Hanging Gardens", "The Temple of Artemis",
           "The Oracle", "The Great Wall", "The Statue of Zeus", "The Mausoleum of Mausollos", "The Great Lighthouse",
           "Sun Tzu's Art of War", "Copernicus' Observatory", "Sistine Chapel", "Shakespeare's Theater"]
RESEARCH = {
    "builder": ["Bronze Working", "Pottery", "Ceremonial Burial", "Writing", "Code of Laws", "Monarchy", "Literature",
                "Currency", "Philosophy", "The Republic", "Mathematics", "Astronomy", "Monotheism", "Banking",
                "Education", "Invention", "Sanitation"],
    "expansionist": ["Pottery", "Bronze Working", "Ceremonial Burial", "Code of Laws", "Monarchy", "Currency",
                     "Writing", "Map Making", "Literature", "The Republic", "Construction", "Philosophy",
                     "Feudalism", "Navigation", "Banking"],
    "aggressive": ["Bronze Working", "Ceremonial Burial", "Iron Working", "Horseback Riding", "Monarchy",
                   "Mathematics", "The Wheel", "Feudalism", "Chivalry", "Construction", "Gunpowder",
                   "Military Tradition", "Metallurgy"],
}
GOVERNMENTS = {"builder": ["Republic", "Monarchy"], "expansionist": ["Monarchy", "Republic"],
               "aggressive": ["Monarchy", "Feudalism"]}
FORCED_LABOUR = {"Despotism", "Communism", "Anarchy"}
PLANS = {
    "expansionist": "Expand fast: settlers from every city of size 2+, a warrior in each city, workers on auto. "
                    "Then granaries, temples and libraries. Stay at peace.",
    "builder": "Grow tall: a handful of cities, then temples, libraries, markets and a wonder in the capital. "
               "Republic when available. Defend, never attack; accept every peace offer.",
    "aggressive": "Settle 5-6 cities, then barracks and an army. Around T{war} declare war on the nearest "
                  "neighbour, march on its cities and take them, then make peace and look for the next target.",
}
WAR_SHARE = (0.30, 0.42)        # when an aggressive seat starts its war, as a share of the turn limit
WAR_LENGTH = (0.15, 0.25)       # how long it fights before offering peace, as a share of the turn limit
MAX_CALLS_PER_TURN = 90
MAX_ATTACKS_PER_TURN = 10
MESSAGES_PER_TURN = 2           # of the env's 3
# What a turn's note leads with: the most notable kind of move.
NOTABLE = {"war": 6, "captured": 6, "razed": 6, "peace": 5, "attack": 4, "revolution": 4, "found": 3, "settle": 3,
           "march": 3, "buy": 2, "research": 1, "build": 1, "explore": 1}
QUIET_NOTES = {"expansionist": "settlers on their way, every city growing",
               "builder": "cities growing, buildings coming", "aggressive": "the army gathers"}
THREATS = ["{target}, your borders are thin and your cities are rich. Send tribute, or {me} marches before T{turn}.",
           "{target}, {city} sits on land {me} wants. Give it up, or we take it.",
           "Last warning, {target}: {me}'s legions are ready."]
WAR_CRIES = ["War, {target}. {city} will fly the banner of {me}.", "War, {target}. Your cities will fly our banner."]
WAR_NEWS = ["{me} is at war with {target}. Keep out of it, and you keep your cities."]
TAUNTS = ["{target}, your lines are breaking. Give up {city} and live.",
          "Every turn you hold out costs you, {target}.", "{city} is next, {target}."]
OFFERS = ["{target}, enough blood. I offer peace: take it before I change my mind.",
          "{target}, this war has made its point. Peace, now."]
GREETINGS = ["Greetings from {me}. We seek peace and open roads with every leader.",
             "{me} sends its greetings. Trade, not war."]
COURTING = ["{target}, {rival} grows too fast for either of us. An alliance?",
            "{target}, {me} would rather have you as a friend. Peace between us for good?",
            "{target}, keep your settlers off our borders and we stay friends."]
ATTACKED = ["{target} attacked {me} without cause. Who stands with us?",
            "We will defend every city, {target}. Make peace while you can."]
DEFIANT = ["We will not kneel, {target}. Come and try.", "Your threats change nothing, {target}."]
FRIENDLY = ["Agreed, {target}. Let our borders stay quiet.", "Well said, {target}. We are with you."]
HOSTILE_WORDS = ("tribute", "war", "march", "legions", "take it", "give up", "next", "costs you", "breaking",
                 "kneel", "threats")

UNIT_LINE = re.compile(r"^(u\d+) (.+?) \((-?\d+),(-?\d+)\) ([\d.]+)/([\d.]+)mv · (.*)$")
TARGET = re.compile(r"\((-?\d+),(-?\d+)\) (\S+) (.+?) (\d+)% to win")
CITY_HEAD = re.compile(r"^(c\d+) (.+?) \((-?\d+),(-?\d+)\) size (\d+)( capital)?")
OPTION = re.compile(r"^(.+?) (\d+) \(")
FOREIGN_CITY = re.compile(r"^(.+?) \(([^()]+)\) size (\S+) \((-?\d+),(-?\d+)\)")
CIV_LINE = re.compile(r"^  (.+?)( \(another agent\))? · (at peace|AT WAR[^·]*?) · score (\d+)")
SETTLE_CALL = re.compile(r'unit_order\(unit="(u\d+)", order="settle", x=(-?\d+), y=(-?\d+)\)')
SITE_LINE = re.compile(r"^#\d+ \((-?\d+),(-?\d+)\) score")
RATES_CALL = re.compile(r"set_rates\(science=(\d+), luxury=(\d+)\)")
# A message read: a reply's ✉ line, or a brief's MESSAGES line ("you to ..." are the seat's own).
RECEIVED = re.compile(r'^(?:✉ |  T\d+ )(.+?)(?: \([^()]*\))? to (you|all): "(.*)"$', re.M)


def ts() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---- what the tools say, parsed ----

@dataclass
class Unit:
    id: str
    type: str
    x: int
    y: int
    moves: float
    status: str
    hp: tuple[int, int] | None = None
    orders: list[str] = field(default_factory=list)
    can_found: bool = False
    targets: list[dict] = field(default_factory=list)

    @property
    def idle(self) -> bool:
        return self.status == "idle"

    @property
    def military(self) -> bool:
        if self.type in UNITS:
            return True
        return self.type not in CIVILIAN and "fortify" in self.orders and "settle" not in self.orders \
            and "auto_work" not in self.orders

    @property
    def wounded(self) -> bool:
        return self.hp is not None and self.hp[0] * 3 <= self.hp[1]


@dataclass
class City:
    id: str
    name: str
    x: int
    y: int
    size: int
    capital: bool
    producing: str | None = None
    engine_pick: bool = False
    stored: int = 0
    defenders: int | None = None
    disorder: bool = False
    riot: bool = False
    buildings: list[str] = field(default_factory=list)
    options: dict[str, int] = field(default_factory=dict)


def parse_units(text: str, known_civs: set[str]) -> list[Unit]:
    units = []
    for line in text.splitlines():
        m = UNIT_LINE.match(line)
        if not m:
            continue
        parts = m[7].split(" · ")
        u = Unit(m[1], m[2], int(m[3]), int(m[4]), float(m[5]), parts[0])
        for p in parts[1:]:
            if p.startswith("hp "):
                a, b = p[3:].split("/")
                u.hp = (int(a), int(b))
            elif p.startswith("orders: "):
                u.orders = p[8:].split()
            elif p == "found here: yes":
                u.can_found = True
            elif p.startswith("attack: "):
                u.targets = [parse_target(t, known_civs) for t in TARGET.finditer(p)]
        units.append(u)
    return units


def parse_target(m: re.Match, known_civs: set[str]) -> dict:
    what = m[4]
    city = None
    if " in " in what:
        what, city = what.rsplit(" in ", 1)
    owner = next((c for c in sorted(known_civs | {"Barbarians"}, key=len, reverse=True) if what.startswith(c + " ")),
                 what.split(" ", 1)[0])
    defender = what[len(owner):].strip()
    return {"x": int(m[1]), "y": int(m[2]), "owner": owner, "city": city,
            "defended": defender != "undefended", "chance": int(m[5]) / 100}


def parse_cities(text: str) -> list[City]:
    cities, c = [], None
    for line in text.splitlines():
        if m := CITY_HEAD.match(line):
            c = City(m[1], m[2], int(m[3]), int(m[4]), int(m[5]), bool(m[6]))
            c.disorder, c.riot = "DISORDER" in line, "riot risk" in line
            if "no defender" in line:
                c.defenders = 0
            cities.append(c)
        elif c is None:
            continue
        elif line.startswith("  mood ") and (d := re.search(r"defenders (\d+)", line)):
            c.defenders = int(d[1])
        elif line.startswith("  producing "):
            rest = line[len("  producing "):]
            c.engine_pick = "(engine pick)" in rest
            m = re.match(r"(.+?) (\d+)/\d+", rest)
            c.producing = m[1] if m else rest.split(" (")[0].strip()
            c.stored = int(m[2]) if m else 0
        elif line.startswith("  NOTHING in production"):
            c.producing = None
        elif line.startswith("  buildings: "):
            c.buildings = line[len("  buildings: "):].split(", ")
        elif line.startswith("  can build: "):
            for opt in line[len("  can build: "):].split(" · "):
                m = OPTION.match(opt)
                c.options[m[1] if m else opt.strip()] = int(m[2]) if m else 0
    return cities


def parse_map(text: str) -> dict[tuple[int, int], tuple[str, str]]:
    """view_map's grid: (x, y) -> (occupant, terrain glyph) for every explored tile shown."""
    lines = text.splitlines()
    head = next((i for i, line in enumerate(lines) if line.startswith("   y\\x")), None)
    if head is None:
        return {}
    xs = [int(lines[head][6 + 3 * i:9 + 3 * i]) for i in range((len(lines[head]) - 6) // 3)]
    tiles = {}
    for line in lines[head + 1:]:
        if line.startswith("legend:") or len(line) < 6 or not line[:5].strip().lstrip("-").isdigit():
            break
        y = int(line[:5])
        for i, x in enumerate(xs):
            cell = line[6 + 3 * i:9 + 3 * i]
            if len(cell) >= 2 and cell[1] != " ":
                tiles[(x, y)] = (cell[0], cell[1])
    return tiles


def parse_foreign_cities(text: str) -> list[dict]:
    for line in text.splitlines():
        if line.startswith("cities: "):
            out = []
            for item in line[len("cities: "):].split(" · "):
                if (m := FOREIGN_CITY.match(item)) and not re.match(r"c\d+ ", item):
                    out.append({"name": m[1], "owner": m[2], "x": int(m[4]), "y": int(m[5])})
            return out
    return []


def parse_civs(text: str) -> dict[str, dict]:
    civs = {}
    for line in text.splitlines():
        if m := CIV_LINE.match(line):
            civs[m[1]] = {"agent": bool(m[2]), "at_war": m[3].startswith("AT WAR"),
                          "offers_peace": "offers peace" in m[3], "score": int(m[4])}
    return civs


@dataclass
class Brief:
    turn: int = 0
    limit: int = 0
    over: bool = False
    gold: int = 0
    government: str = "Despotism"
    anarchy: bool = False
    wars: dict[str, str] = field(default_factory=dict)       # civ -> "" | "offers peace" | "peace: N gold"
    research_none: bool = False
    research_engine: bool = False
    research_current: str | None = None
    gov_choices: list[str] = field(default_factory=list)
    disorder: bool = False
    rates_fix: tuple[int, int] | None = None
    text: str = ""


def parse_brief(text: str) -> Brief:
    b = Brief(text=text)
    if "GAME OVER" in text.split("\n", 1)[0] or text.rstrip().endswith("[GAME OVER]") or \
            "no further actions are possible" in text:
        b.over = True
    first = text.splitlines()[0] if text else ""
    if m := re.match(r"T(\d+)/(\d+)", first):
        b.turn, b.limit = int(m[1]), int(m[2])
    parts = first.split(" · ")
    if len(parts) > 2:
        b.government = parts[2]
    b.anarchy = "anarchy until" in first
    if m := re.search(r"gold (-?\d+)", first):
        b.gold = int(m[1])
    for p in parts:
        if p.startswith("!! at war with "):
            for w in p[len("!! at war with "):].split(", "):
                name, _, note = w.partition(" (")
                b.wars[name] = note.rstrip(")")
    for line in text.splitlines():
        if line.startswith("RESEARCH "):
            b.research_none = line.startswith("RESEARCH none")
            b.research_engine = "(engine pick)" in line
            b.research_current = None if b.research_none else line[len("RESEARCH "):].split(" ")[0]
        elif line.startswith("GOVERNMENT ") and " can choose " in line:
            choices = line.split(" can choose ", 1)[1].split(" → ")[0]
            b.gov_choices = [c.split(" (")[0].strip() for c in choices.split("; ")]
        if "DISORDER" in line or line.lstrip().startswith("!! ") and "disorder" in line.lower():
            b.disorder = True
            if m := RATES_CALL.search(line):
                b.rates_fix = (int(m[1]), int(m[2]))
    return b


def dist(a: tuple[int, int], b: tuple[int, int], width: int) -> int:
    dx = abs(a[0] - b[0])
    dx = min(dx, width - dx) if width else dx
    return (dx + abs(a[1] - b[1])) // 2


def neighbours(x: int, y: int, width: int) -> list[tuple[int, int]]:
    return [((x + dx) % width if width else x + dx, y + dy)
            for dx, dy in ((1, -1), (2, 0), (1, 1), (0, 2), (-1, 1), (-2, 0), (-1, -1), (0, -2))]


# ---- one seat ----

class Stats:
    def __init__(self):
        self.calls = Counter()
        self.failed = Counter()
        self.injected = 0
        self.events: list[dict] = []


class Bot:
    """One seat: an MCP client with the seat header, playing its turns with tool calls."""

    def __init__(self, match: Match, civ: str, label: str, personality: str, index: int):
        self.m, self.civ, self.label, self.personality = match, civ, label, personality
        self.rng = random.Random(match.args.seed * 1000 + index)
        self.stats = Stats()
        self.session: ClientSession | None = None
        self.turn = 0
        self.limit = match.turn_limit
        self.calls_this_turn = 0
        self.brief = Brief()
        self.units: list[Unit] = []
        self.cities: list[City] = []
        self.known_civs: dict[str, dict] = {}
        self.foreign: dict[tuple[int, int], dict] = {}    # foreign cities seen: (x, y) -> {name, owner, seen}
        self.explorers: set[str] = set()
        self.known_techs: set[str] = set()
        self.bad_goals: set[str] = set()
        self.revolutions = 0
        self.no_sites_until = -1
        self.last_buy = -10
        self.last_rates = -10
        self.last_diplomacy = -10
        self.last_scout = -10
        self.war_turn = int(self.limit * self.rng.uniform(*WAR_SHARE))
        self.target: str | None = None                     # the civ this seat is at war with by choice
        self.war_started: int | None = None
        self.war_until: int | None = None
        self.peace_cooldown_until = 0
        self.wars_declared: list[str] = []
        self.staging: dict[tuple[int, int], tuple[int, list[tuple[int, int]]]] = {}
        self.unreachable: Counter = Counter()
        self.over = False
        self.result = ""
        self.did: list[tuple[str, str]] = []              # this turn's moves for its note: (kind, text)
        self.inbox: list[tuple[str, bool, str]] = []      # messages read and not yet answered: (from, to all, text)
        self.heard: set[tuple[str, str]] = set()
        self.sent_this_turn = 0
        self.threatened: str | None = None
        self.greeted = False
        self.last_taunt = -10
        self.defending: set[str] = set()                  # the leaders who attacked us, told off once per war

    # -- the MCP client --

    def log(self, text: str, notable: bool = False) -> None:
        line = f"{ts()} T{self.turn:<3} {self.label:>7} ({self.civ}): {text}"
        self.m.log(line, echo=notable or self.m.args.verbose)
        if notable:
            self.stats.events.append({"turn": self.turn, "text": text})

    async def think(self) -> None:
        lo, hi = self.m.think
        if hi > 0:
            await asyncio.sleep(self.rng.uniform(lo, hi))

    async def call(self, tool: str, args: dict | None = None, *, inject: bool = True) -> tuple[bool, str]:
        """One tool call; maybe first a deliberately wrong one, as agents sometimes make. Never raises."""
        args = args or {}
        if inject and self.rng.random() < self.m.args.error_rate and (bad := self.mangle(tool, args)):
            self.stats.injected += 1
            await self._call(*bad)
        return await self._call(tool, args)

    async def _call(self, tool: str, args: dict) -> tuple[bool, str]:
        await self.think()
        self.calls_this_turn += 1
        self.stats.calls[tool] += 1
        for attempt in range(4):
            try:
                res = await self.session.call_tool(tool, args)
                text = "\n".join(getattr(c, "text", "") for c in res.content)
                if res.isError:
                    self.stats.failed[tool] += 1
                    self.log(f"✗ {tool}({json.dumps(args)}): {text.splitlines()[0][:160] if text else ''}")
                else:
                    self.log(f"✓ {tool}({json.dumps(args)})")
                for m in RECEIVED.finditer(text):
                    if m[1] != "you" and (m[1], m[3]) not in self.heard:
                        self.heard.add((m[1], m[3]))
                        self.inbox.append((m[1], m[2] == "all", m[3]))
                return not res.isError, text
            except Exception as e:      # the transport broke: reconnect and retry
                self.log(f"! {tool} transport error ({type(e).__name__}: {e}); reconnecting", notable=True)
                await asyncio.sleep(1 + attempt * 2)
                with contextlib.suppress(Exception):
                    await self.reconnect()
        self.stats.failed[tool] += 1
        return False, "transport failed"

    def mangle(self, tool: str, args: dict) -> tuple[str, dict] | None:
        """A plausible wrong version of a call: a typo'd id, an item or tech that isn't there yet, a bad order."""
        r = self.rng
        if tool == "unit_order":
            unit = next((u for u in self.units if u.id == args.get("unit")), None)
            options = [("unit_order", {**args, "unit": f"u{r.randint(400, 999)}"})]
            if unit and unit.military:
                options.append(("unit_order", {"unit": unit.id, "order": r.choice(["found_city", "auto_work",
                                                                                   "build_mine"])}))
            if unit and unit.type == "Worker":
                options.append(("unit_order", {"unit": unit.id, "order": "attack", "x": unit.x + 1, "y": unit.y + 1}))
            return r.choice(options)
        if tool == "set_production":
            return tool, {**args, "item": r.choice(["Musketman", "Cathedral", "Laser Battery", "Tank", "Bank"])}
        if tool == "research" and args.get("tech"):
            return tool, {"tech": r.choice(["Alphabet", "Warrior Code", "Warp Drive", "Fusion Power"])}
        if tool in ("city_info", "buy"):
            return tool, {"city": f"c{r.randint(40, 99)}"}
        if tool == "find_city_sites":
            return tool, {**args, "unit": f"u{r.randint(400, 999)}"}
        if tool == "view_map" and args.get("x") is not None:
            return tool, {**args, "x": args["x"] + 1}
        if tool == "diplomacy" and args.get("action") in ("declare_war", "propose_peace"):
            return tool, {**args, "civ": r.choice(["Atlantis", "Lemuria"])}
        if tool == "message":
            return tool, {**args, "to": r.choice(["Atlantis", "Lemuria"])}
        if tool == "get_turn_brief":
            city = r.choice(self.cities).id if self.cities else "c1"
            return r.choice([("city_info", {"city": f"c{r.randint(40, 99)}"}),
                             ("set_production", {"city": city, "item": r.choice(["Knight", "Bank", "Granery Plus"])}),
                             ("unit_order", {"unit": f"u{r.randint(400, 999)}", "order": "fortify"}),
                             ("research", {"tech": r.choice(["Alphabet", "Steam Engine", "Magic"])})])
        return None

    async def connect(self) -> None:
        self.stack = contextlib.AsyncExitStack()
        http = await self.stack.enter_async_context(
            httpx.AsyncClient(headers={SEAT_HEADER: self.label}, timeout=httpx.Timeout(900, connect=30)))
        read, write, _ = await self.stack.enter_async_context(streamable_http_client(self.m.mcp_url, http_client=http))
        self.session = await self.stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()

    async def reconnect(self) -> None:
        with contextlib.suppress(Exception):
            await self.stack.aclose()
        await self.connect()

    # -- the game loop --

    async def play(self) -> None:
        await self.connect()
        try:
            stalls = 0
            while not self.over:
                before = self.turn
                try:
                    await asyncio.wait_for(self.play_turn(), self.m.args.turn_budget)
                except TimeoutError:
                    self.log(f"turn took over {self.m.args.turn_budget}s; ending it", notable=True)
                except Exception as e:      # a bot bug must not stall the game: end the turn anyway
                    self.log(f"bot error {type(e).__name__}: {e}", notable=True)
                if self.over:
                    break
                await self.end_turn()
                stalls = stalls + 1 if self.turn == before else 0
                if stalls > 20 and not self.m.humans:     # a person may take their time
                    self.log("the turn has not advanced 20 times in a row; giving up", notable=True)
                    break
        finally:
            with contextlib.suppress(Exception):
                await self.stack.aclose()
        self.log(f"done: {self.result or 'stopped'}", notable=True)

    def note(self) -> str:
        """One line for the people watching: the turn's two most notable moves."""
        if not self.did:
            score = re.search(r"^SCORE (\d+)", self.brief.text, re.M)
            where = f"{len(self.cities)} cit{'y' if len(self.cities) == 1 else 'ies'}" + (
                f", score {score[1]}" if score else "")
            return f"Quiet turn at {where}: {QUIET_NOTES[self.personality]}."
        moves = list(dict.fromkeys(text for _, text in sorted(self.did, key=lambda d: -NOTABLE[d[0]])))[:2]
        line = "; ".join(moves)
        return line[0].upper() + line[1:] + "."

    async def end_turn(self) -> None:
        skip = self.rng.random() < 0.9
        note = self.note()
        for _ in range(30):
            ok, text = await self.call("end_turn", {"skip_idle": True, "note": note} if skip else {"note": note},
                                       inject=False)
            if "GAME OVER" in text or "no further actions are possible" in text:
                self.over = True
                self.result = next((line for line in text.splitlines() if line.startswith("GAME OVER")), "GAME OVER")
                return
            if ok and text.startswith("WAITING"):
                continue
            if not ok and "END TURN BLOCKED" in text:
                skip = True
                continue
            if ok:
                if m := re.search(r"TURN T\d+ → T(\d+)", text):
                    self.turn = int(m[1])
                self.note_turn_events(text)
                return
            if "engine" in text or "transport failed" in text:
                return      # the turn restarts or the env is gone: play it again from the brief
            skip = True
        self.log("end_turn kept failing", notable=True)

    def note_turn_events(self, text: str) -> None:
        """Log what the turn did to this civ: wars, peace, a city of ours destroyed."""
        for line in text.splitlines():
            for item in re.sub(r"^\s*T\d+: ", "", line).split(" · "):
                item = item.strip().removeprefix("!! ")
                if self.civ in item and ("declared war" in item or "made peace" in item or
                                         re.search(r"\(.+\) was destroyed", item)):
                    self.log(f"event: {item[:200]}", notable=True)

    async def play_turn(self) -> None:
        self.calls_this_turn = self.sent_this_turn = 0
        self.did = []
        ok, text = await self.call("get_turn_brief")
        if not ok:
            if "game is over" in text or "GAME OVER" in text:
                self.over = True
            return
        b = self.brief = parse_brief(text)
        if b.over:
            self.over = True
            self.result = text.splitlines()[0]
            return
        self.turn, self.limit = b.turn, b.limit or self.limit
        if self.turn in (0, 1) or self.turn % 30 == 0 and self.rng.random() < 0.7:
            await self.call("plan", {"text": PLANS[self.personality].format(war=self.war_turn)})
        await self.refresh_units()
        if any(c.startswith("CITIES none") for c in text.splitlines()):
            self.cities = []
        else:
            ok, ctext = await self.call("city_info")
            if ok:
                self.cities = parse_cities(ctext)
        await self.research()
        await self.government()
        await self.diplomacy()
        await self.talk()
        await self.scout()
        await self.settlers()
        await self.workers()
        await self.fight()
        await self.military()
        await self.production()
        await self.buy()
        await self.rates()

    async def refresh_units(self) -> None:
        ok, text = await self.call("list_units", {"filter": "all"})
        if ok:
            self.units = parse_units(text, set(self.known_civs))

    def budget(self, reserve: int = 2) -> bool:
        return self.calls_this_turn < MAX_CALLS_PER_TURN - reserve

    # -- research and government --

    async def research(self) -> None:
        b = self.brief
        if not (b.research_none or b.research_engine):
            return
        ok, text = await self.call("research")
        if not ok:
            return
        if m := re.search(r"known \d+: (.*)", text):
            self.known_techs = {t.strip() for t in m[1].split(",")}
        available = [m[1] for line in text.splitlines() if (m := re.match(r"  (.+?) \d+ beakers", line))]
        goal = next((t for t in RESEARCH[self.personality] if t not in self.known_techs and t not in self.bad_goals),
                    None)
        if goal is None:
            if not b.research_none or not available:
                return
            goal = self.rng.choice(available)
        if goal == b.research_current:
            return
        ok, text = await self.call("research", {"tech": goal})
        if ok:
            self.did.append(("research", f"researching {goal}"))
        elif "already" not in text:
            self.bad_goals.add(goal)

    async def government(self) -> None:
        b = self.brief
        if b.anarchy or not b.gov_choices or self.revolutions >= 2:
            return
        for want in GOVERNMENTS[self.personality]:
            if want == b.government:
                return
            if want in b.gov_choices:
                if b.government != "Despotism" and self.rng.random() > 0.2:
                    return
                ok, _ = await self.call("revolution", {"government": want})
                if ok:
                    self.revolutions += 1
                    self.did.append(("revolution", f"revolution: {b.government} to {want}"))
                    self.log(f"revolution: {b.government} → {want}", notable=True)
                return

    # -- diplomacy and war --

    async def diplomacy(self) -> None:
        b = self.brief
        due = self.turn - self.last_diplomacy >= (3 if self.personality == "aggressive" else 8)
        if not (due or b.wars and self.turn - self.last_diplomacy >= 2):
            return
        ok, text = await self.call("diplomacy")
        if not ok:
            return
        self.last_diplomacy = self.turn
        self.known_civs = parse_civs(text)
        at_war = {c for c, info in self.known_civs.items() if info["at_war"]}
        if self.target and self.target not in self.known_civs:
            self.log(f"{self.target} is gone; the war is over", notable=True)
            self.target, self.war_until = None, None
        # peace: accept offers, or ask for it
        for civ in sorted(at_war):
            info = self.known_civs[civ]
            mine = civ == self.target
            if info["offers_peace"]:
                accept = not mine or self.turn >= (self.war_until or 0) - 5 or self.rng.random() < 0.1
                if self.personality != "aggressive" or accept:
                    ok, _ = await self.call("diplomacy", {"action": "propose_peace", "civ": civ})
                    if ok:
                        self.did.append(("peace", f"accepted peace with {civ}"))
                        self.log(f"accepted peace with {civ}", notable=True)
                        if mine:
                            self.end_war()
                continue
            if mine and self.turn >= (self.war_until or 10 ** 6):
                ok, _ = await self.call("diplomacy", {"action": "propose_peace", "civ": civ})
                if ok:
                    self.did.append(("peace", f"offered {civ} peace"))
                    await self.say(civ, OFFERS)
                    self.log(f"proposed peace to {civ} after {self.turn - (self.war_started or 0)} turns of war",
                             notable=True)
            elif not mine and self.personality != "aggressive" and self.rng.random() < 0.12:
                ok, _ = await self.call("diplomacy", {"action": "propose_peace", "civ": civ})
                if ok:
                    self.log(f"asked {civ} for peace", notable=True)
        if self.target and self.target not in at_war and self.war_started is not None and \
                self.turn > self.war_started + 1:
            self.log(f"peace with {self.target} holds", notable=True)
            self.end_war()
        if self.personality == "aggressive" and not self.target and self.turn >= self.war_turn and \
                self.turn >= self.peace_cooldown_until and self.turn < self.limit - max(5, self.limit // 12):
            await self.declare_war(at_war)

    def end_war(self) -> None:
        self.target, self.war_started, self.war_until = None, None, None
        self.peace_cooldown_until = self.turn + int(self.limit * 0.08)
        self.war_turn = self.peace_cooldown_until

    async def declare_war(self, at_war: set[str]) -> None:
        home = self.home()
        width = self.m.width
        candidates = []
        for civ, info in self.known_civs.items():
            if civ in self.wars_declared[-1:] and len(self.known_civs) > 1:
                continue
            cities = [xy for xy, c in self.foreign.items() if c["owner"] == civ]
            if not cities or home is None:
                continue
            near = min(dist(home, xy, width) for xy in cities)
            candidates.append((near + (0 if info["agent"] else 3) - (5 if civ in at_war else 0), civ))
        if not candidates:
            if self.turn > self.war_turn + max(3, self.limit // 10) and self.known_civs:  # none seen: any civ
                candidates = [(0, self.rng.choice(sorted(self.known_civs)))]
            else:
                return
        civ = min(candidates)[1]
        if civ not in at_war:
            ok, text = await self.call("diplomacy", {"action": "declare_war", "civ": civ})
            if not ok and "already" not in text:
                return
        self.target, self.war_started = civ, self.turn
        self.war_until = self.turn + int(self.limit * self.rng.uniform(*WAR_LENGTH))
        self.wars_declared.append(civ)
        self.did.append(("war", f"declared war on {civ}"))
        await self.say(civ, WAR_CRIES)
        if self.rng.random() < 0.5:
            await self.say("all", WAR_NEWS, target=civ)
        self.log(f"DECLARED WAR on {civ} (until about T{self.war_until})", notable=True)

    # -- talk --

    def leaders(self) -> list[str]:
        """The other civs played by an agent or a person: the ones that read messages."""
        return [c for c in self.m.leaders if c != self.civ]

    def city_of(self, civ: str) -> str | None:
        return next((c["name"] for c in self.foreign.values() if c["owner"] == civ), None)

    async def say(self, to: str, lines: list[str], **fields: str) -> None:
        """Send one of `lines` (filled in; one naming a city only when it knows one) to a leader, or to all; within
        this turn's share."""
        target = fields.pop("target", to)
        city = self.city_of(target)
        lines = [line for line in lines if city or "{city}" not in line]
        if self.sent_this_turn >= MESSAGES_PER_TURN or (to != "all" and to not in self.m.leaders) or not lines:
            return
        text = self.rng.choice(lines).format(me=self.civ, target=target, city=city, turn=self.war_turn, **fields)
        ok, _ = await self.call("message", {"to": to, "text": text})
        if ok:
            self.sent_this_turn += 1
            self.log(f"to {to}: {text}")

    async def talk(self) -> None:
        """An aggressive seat threatens the leader it means to attack and taunts it during the war (its war cry and
        peace offer go with the diplomacy calls); the others greet, court and warn now and then, and protest a war on
        them. Everyone answers some of what it is sent."""
        others = self.leaders()
        if not others:
            return
        r = self.rng
        heard, self.inbox = self.inbox, []
        for sender, to_all, text in heard:
            if not to_all and sender in others and r.random() < 0.4:
                hostile = sender in self.at_war_with() or any(w in text.lower() for w in HOSTILE_WORDS)
                await self.say(sender, DEFIANT if hostile else FRIENDLY)
                break
        if self.personality == "aggressive":
            if self.target and self.turn - self.last_taunt >= 4 and r.random() < 0.6:
                self.last_taunt = self.turn
                await self.say(self.target, TAUNTS)
            elif not self.target and self.threatened is None and self.war_turn - 4 <= self.turn < self.war_turn:
                self.threatened = self.likely_target()
                await self.say(self.threatened, THREATS)
            return
        for enemy in sorted(set(self.brief.wars) & set(others) - self.defending):
            self.defending.add(enemy)
            await self.say("all", ATTACKED[:1], target=enemy)
            await self.say(enemy, ATTACKED[1:])
        self.defending &= set(self.brief.wars)
        if not self.greeted and self.turn <= 3 and r.random() < 0.3:
            self.greeted = True
            await self.say("all", GREETINGS)
        elif r.random() < 0.06:
            to = r.choice(others)
            rival = max((c for c in self.known_civs if c != to), key=lambda c: self.known_civs[c]["score"], default="")
            await self.say(to, COURTING if rival else COURTING[1:], rival=rival)

    def likely_target(self) -> str:
        """The leader an aggressive seat will most likely attack: the nearest with a city it has seen, else one it
        has met, else any."""
        home = self.home()
        seen = [(dist(home, xy, self.m.width), c["owner"]) for xy, c in self.foreign.items()
                if home and c["owner"] in self.m.leaders]
        met = sorted(set(self.known_civs) & set(self.leaders()))
        return min(seen)[1] if seen else met[0] if met else self.rng.choice(self.leaders())

    # -- seeing the world --

    def home(self) -> tuple[int, int] | None:
        cap = next((c for c in self.cities if c.capital), None) or (self.cities[0] if self.cities else None)
        return (cap.x, cap.y) if cap else None

    async def scout(self) -> None:
        """Look at the map around far-out units (and the capital) to find the other civs' cities."""
        every = 3 if self.personality == "aggressive" else 10
        if self.turn - self.last_scout < every or not self.budget(20):
            return
        self.last_scout = self.turn
        home = self.home()
        far = sorted((u for u in self.units if u.military or u.type in ("Scout", "Explorer")),
                     key=lambda u: -dist(home, (u.x, u.y), self.m.width) if home else 0)
        looks = [{"around": u.id, "radius": 6} for u in far[:2 if self.personality == "aggressive" else 1]]
        if home and self.rng.random() < 0.5:
            looks.append({"radius": 6})
        for args in looks:
            ok, text = await self.call("view_map", args)
            if ok:
                self.see(text)

    def see(self, text: str) -> None:
        tiles = parse_map(text)
        for (x, y), (occ, _) in tiles.items():       # a city we knew of is gone: razed
            if (x, y) in self.foreign and occ != "C":
                self.foreign.pop((x, y))
        for c in parse_foreign_cities(text):
            if c["owner"] != self.civ:
                self.foreign[(c["x"], c["y"])] = {"name": c["name"], "owner": c["owner"], "seen": self.turn}

    # -- units --

    async def settlers(self) -> None:
        for u in [u for u in self.units if "settle" in u.orders and u.idle]:
            if not self.budget(10):
                return
            if u.can_found and (not self.cities or self.rng.random() < 0.25):
                ok, _ = await self.call("unit_order", {"unit": u.id, "order": "found_city"})
                if ok:
                    self.did.append(("found", "founded a city"))
                    continue
            ok, text = await self.call("find_city_sites", {"unit": u.id, "top": 3})
            if not ok:
                continue
            m = SETTLE_CALL.search(text)
            site = (int(m[2]), int(m[3])) if m else None
            if site is None and (s := next((SITE_LINE.match(line) for line in text.splitlines()
                                            if SITE_LINE.match(line)), None)):
                site = (int(s[1]), int(s[2]))
            if site is None:
                self.no_sites_until = self.turn + 15
                if u.can_found:
                    await self.call("unit_order", {"unit": u.id, "order": "found_city"})
                else:
                    await self.call("unit_order", {"unit": u.id, "order": "hold"})
                continue
            ok, text = await self.call("unit_order", {"unit": u.id, "order": "settle", "x": site[0], "y": site[1]})
            if ok:
                self.did.append(("settle", f"sent a settler to ({site[0]},{site[1]})"))
            elif u.can_found:
                await self.call("unit_order", {"unit": u.id, "order": "found_city"})

    async def workers(self) -> None:
        if not self.cities:
            return
        for u in [u for u in self.units if "auto_work" in u.orders and u.idle and "settle" not in u.orders]:
            if not self.budget(8):
                return
            ok, _ = await self.call("unit_order", {"unit": u.id, "order": "auto_work"})
            if not ok and self.rng.random() < 0.5:
                await self.call("unit_order", {"unit": u.id, "order": "build_road"})

    def city_at(self, x: int, y: int) -> City | None:
        return next((c for c in self.cities if (c.x, c.y) == (x, y)), None)

    def at_war_with(self) -> set[str]:
        return set(self.brief.wars) | ({self.target} if self.target else set())

    async def fight(self) -> None:
        """Attack from every unit that has a target worth it: barbarians, and civs we are at war with."""
        wars = self.at_war_with() | {"Barbarians"}
        attacks = 0
        tried: set[str] = set()
        while attacks < MAX_ATTACKS_PER_TURN and self.budget(6):
            best = None
            for u in self.units:
                if u.id in tried or not u.targets or not u.military or u.moves <= 0:
                    continue
                for t in u.targets:
                    if t["owner"] not in wars:
                        continue
                    need = self.threshold(t, u)
                    if t["chance"] >= need and (best is None or t["chance"] > best[1]["chance"]):
                        best = (u, t)
            if best is None:
                return
            u, t = best
            tried.add(u.id)
            ok, text = await self.call("unit_order", {"unit": u.id, "order": "attack", "x": t["x"], "y": t["y"]})
            attacks += 1
            if ok:
                what = f"{t['owner']} " + (f"city {t['city']}" if t["city"] else "unit")
                self.did.append(("attack", f"attacked {what}"))
                self.log(f"{u.id} {u.type} attacked {what} at ({t['x']},{t['y']}) ({round(t['chance'] * 100)}%): "
                         f"{text.splitlines()[0][:160]}", notable=bool(t["city"]) or "razed" in text)
                if "is yours now" in text:      # taken (patches/0011)
                    self.foreign.pop((t["x"], t["y"]), None)
                    self.did.append(("captured", f"took {t['city']} from {t['owner']}"))
                    self.log(f"CAPTURED {t['city']} of {t['owner']} — {text.splitlines()[0][:200]}", notable=True)
                elif "razed" in text or "fell" in text:
                    self.foreign.pop((t["x"], t["y"]), None)
                    self.did.append(("razed", f"razed {t['city']} of {t['owner']}"))
                    self.log(f"RAZED {t['city']} of {t['owner']} — {text.splitlines()[0][:200]}", notable=True)
            await self.refresh_units()

    def threshold(self, t: dict, u: Unit) -> float:
        if not t["defended"]:
            return 0.0
        aggressive = self.personality == "aggressive"
        if t["city"]:
            if t["owner"] == "Barbarians":
                return 0.4
            return 0.3 if aggressive and t["owner"] == self.target else 0.55
        if u.wounded:
            return 0.8
        return 0.4 if aggressive else 0.6

    async def military(self) -> None:
        width = self.m.width
        mil = [u for u in self.units if u.military]
        at_war = bool(self.at_war_with())
        want = 2 if at_war or self.turn > self.limit * 0.6 else 1
        garrison: dict[str, list[Unit]] = {c.id: [] for c in self.cities}
        for u in mil:
            c = self.city_at(u.x, u.y)
            if c and u.status == "fortified":
                garrison[c.id].append(u)
        free = [u for u in mil if u.idle or u.status == "done" and u.moves > 0]
        # explorers: one per seat early on, two for the aggressive ones
        explorers = {u.id for u in mil if u.status.startswith("exploring")}
        self.explorers = explorers
        n_explore = 2 if self.personality == "aggressive" else 1
        army = []
        for u in [u for u in free if u.idle]:
            if not self.budget(4):
                return
            here = self.city_at(u.x, u.y)
            if u.wounded:
                await self.call("unit_order", {"unit": u.id, "order": "fortify"})
                continue
            if here and len(garrison[here.id]) < want:
                ok, _ = await self.call("unit_order", {"unit": u.id, "order": "fortify"})
                if ok:
                    garrison[here.id].append(u)
                    continue
            bare = [c for c in self.cities if len(garrison[c.id]) < want and not any(
                g.status.startswith("goto") and g.x == c.x for g in mil)]
            if len(explorers) < n_explore and self.cities and self.turn < self.limit * 0.7:
                ok, _ = await self.call("unit_order", {"unit": u.id, "order": "explore"})
                if ok:
                    explorers.add(u.id)
                    self.did.append(("explore", f"sent a {u.type} exploring"))
                    continue
            if self.target and (self.personality == "aggressive"):
                army.append(u)
                continue
            if bare:
                c = min(bare, key=lambda c: dist((u.x, u.y), (c.x, c.y), width))
                ok, _ = await self.call("unit_order", {"unit": u.id, "order": "goto", "x": c.x, "y": c.y})
                if ok:
                    garrison[c.id].append(u)
                    continue
            if self.personality == "aggressive" and self.turn >= self.war_turn - 10 and not here:
                await self.call("unit_order", {"unit": u.id, "order": "hold"})
                continue
            await self.call("unit_order", {"unit": u.id, "order": "fortify"})
        if self.target:
            # the army: every offensive unit not guarding a city, plus the idle ones above
            for u in mil:
                if u in army or u.id in explorers or u.status != "fortified" or self.city_at(u.x, u.y) is None:
                    continue
                c = self.city_at(u.x, u.y)
                if len(garrison[c.id]) > 1 and UNITS.get(u.type, (0, 0))[0] > UNITS.get(u.type, (0, 0))[1]:
                    garrison[c.id].remove(u)
                    army.append(u)
            await self.march(army)

    async def march(self, army: list[Unit]) -> None:
        """Send the army to a tile next to the nearest known city of the civ we fight."""
        if not army:
            return
        width = self.m.width
        cities = [xy for xy, c in self.foreign.items() if c["owner"] == self.target and self.unreachable[xy] < 3]
        if not cities:
            for u in army[:2]:
                if self.budget(4):
                    await self.call("unit_order", {"unit": u.id, "order": "explore"})
            return
        self.did.append(("march", f"{len(army)} units marching on {self.target}"))
        for u in army:
            if not self.budget(4):
                return
            if u.targets:
                continue
            goal = min(cities, key=lambda xy: dist((u.x, u.y), xy, width))
            if dist((u.x, u.y), goal, width) <= 1:
                await self.call("unit_order", {"unit": u.id, "order": "fortify"})
                continue
            tiles = await self.staging_tiles(goal)
            tiles = sorted(tiles, key=lambda xy: dist((u.x, u.y), xy, width))
            sent = False
            for xy in tiles[:2]:
                if (u.x, u.y) == xy:
                    sent = True
                    break
                ok, text = await self.call("unit_order", {"unit": u.id, "order": "goto", "x": xy[0], "y": xy[1]})
                if ok:
                    sent = True
                    break
                if "no route" in text or "moves on land" in text:
                    self.unreachable[goal] += 1
            if not sent:
                await self.call("unit_order", {"unit": u.id, "order": "hold"})

    async def staging_tiles(self, city: tuple[int, int]) -> list[tuple[int, int]]:
        cached = self.staging.get(city)
        if cached and cached[0] == self.turn:
            return cached[1]
        ok, text = await self.call("view_map", {"x": city[0], "y": city[1], "radius": 2})
        tiles: list[tuple[int, int]] = []
        if ok:
            self.see(text)
            grid = parse_map(text)
            if city not in self.foreign:
                self.staging[city] = (self.turn, [])
                return []
            for xy in neighbours(*city, self.m.width):
                occ, glyph = grid.get(xy, ("?", "?"))
                if glyph not in ("~", "?") and occ in (" ", "#", "@"):
                    tiles.append(xy)
        self.staging[city] = (self.turn, tiles)
        return tiles

    # -- cities --

    def counts(self) -> Counter:
        n = Counter(u.type for u in self.units)
        for c in self.cities:
            if c.producing:
                n["building:" + c.producing] += 1
        return n

    def best(self, c: City, role: str) -> str | None:
        """The city's best unit to attack (role 'attack') or defend with."""
        i = 0 if role == "attack" else 1
        units = [(UNITS[name][i], -cost, name) for name, cost in c.options.items() if name in UNITS]
        if role == "attack":
            units = [x for x in units if UNITS[x[2]][0] > UNITS[x[2]][1] or UNITS[x[2]][0] >= 3]
        return max(units)[2] if units else None

    def choose(self, c: City, n: Counter) -> str | None:
        p, opts = self.personality, c.options
        cities = len(self.cities)
        settlers = n["Settler"] + n["building:Settler"]
        workers = n["Worker"] + n["building:Worker"]
        defenders = c.defenders or 0
        at_war = bool(self.at_war_with())
        if (defenders == 0 or (at_war and defenders < 2)) and (d := self.best(c, "defend")):
            return d
        if at_war and p != "aggressive" and "Walls" in opts and self.rng.random() < 0.5:
            return "Walls"
        max_cities = {"expansionist": 14, "builder": 6, "aggressive": 7}[p]
        if "Settler" in opts and (c.size >= 2 or cities < 3) and cities + settlers < max_cities \
                and self.turn >= self.no_sites_until and settlers < max(2, cities // 2) \
                and self.turn < self.limit * 0.75:
            return "Settler"
        if "Worker" in opts and workers < max(1, int(cities * (0.8 if p == "expansionist" else 0.6))):
            return "Worker"
        if p == "aggressive" and (self.target or self.turn >= self.war_turn - int(self.limit * 0.1)):
            if self.rng.random() < (0.8 if self.target else 0.6) and (a := self.best(c, "attack")):
                return a
        if c.disorder or c.riot:
            for b in ("Temple", "Colosseum", "Cathedral"):
                if b in opts:
                    return b
        if p == "builder" and c.capital and self.rng.random() < 0.5:
            wonders = [w for w in WONDERS if w in opts]
            if wonders:
                return wonders[0]
        for b in BUILDINGS[p]:
            if b in opts:
                return b
        if p == "aggressive" and (a := self.best(c, "attack")):
            return a
        if self.rng.random() < 0.3 and (d := self.best(c, "defend")):
            return d
        return "Wealth" if "Wealth" in opts else None

    async def production(self) -> None:
        n = self.counts()
        for c in self.cities:
            if not self.budget(3):
                return
            urgent = (c.defenders == 0 and c.producing not in UNITS and c.stored < 10)
            if not (c.producing is None or c.engine_pick or urgent):
                continue
            item = self.choose(c, n)
            if item is None or item == c.producing:
                if c.engine_pick and c.producing and self.rng.random() < 0.5:
                    await self.call("set_production", {"city": c.id, "item": c.producing})   # keep it
                continue
            ok, _ = await self.call("set_production", {"city": c.id, "item": item})
            if ok:
                self.did.append(("build", f"{c.name} builds {item}"))
                if c.producing:
                    n["building:" + c.producing] -= 1
                n["building:" + item] += 1
                c.producing, c.engine_pick = item, False

    async def buy(self) -> None:
        b = self.brief
        if not self.cities or self.turn - self.last_buy < 4 or not self.budget(2):
            return
        if b.government in FORCED_LABOUR:
            # forced labour costs citizens: only now and then, in a big city
            big = [c for c in self.cities if c.size >= 5 and c.producing and c.producing != "Wealth"]
            if not big or self.rng.random() > 0.04:
                return
            c = self.rng.choice(big)
        else:
            if b.gold < (100 if self.target else 160):
                return
            wanted = [c for c in self.cities if c.producing and c.producing != "Wealth" and
                      (c.producing in UNITS if self.target else c.producing not in WONDERS)]
            if not wanted:
                return
            c = max(wanted, key=lambda c: c.stored)
        self.last_buy = self.turn
        ok, text = await self.call("buy", {"city": c.id})
        if ok:
            self.did.append(("buy", f"bought {c.producing} in {c.name}"))
            self.log(f"bought {c.producing} in {c.name}")

    async def rates(self) -> None:
        """Disorder: move science into luxury as the brief suggests; a rich treasury: more science."""
        b = self.brief
        if self.turn - self.last_rates < 4:
            return
        if b.disorder and b.rates_fix:
            self.last_rates = self.turn
            await self.call("set_rates", {"science": b.rates_fix[0], "luxury": b.rates_fix[1]})
        elif b.gold > 250 and (m := re.search(r"set_rates\(science=(\d+), luxury=(\d+)\) moves tax", b.text)):
            self.last_rates = self.turn
            await self.call("set_rates", {"science": int(m[1]), "luxury": int(m[2])})


# ---- the match ----

class Match:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.out: Path = args.out
        self.think = (0.0, 0.0) if args.fast else tuple(args.think)
        self.port = args.port or free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.mcp_url = f"{self.base}/mcp"
        self.live_url = f"{self.base}/live"
        self.turn_limit = args.turns
        self.width = 0
        self.proc: subprocess.Popen | None = None
        self.logf = None
        self.bots: list[Bot] = []
        self.leaders: set[str] = set()         # the civs of every seat, bots' and people's
        self.humans: dict[str, str] = {}        # civ -> label
        if args.humans:
            self.humans = dict(tuple(h.split("=", 1)) if "=" in h else (h, h.lower()) for h in args.humans.split(","))
        self.play: dict[str, str] = {}          # civ -> play URL
        self.over = False

    def log(self, line: str, echo: bool = True) -> None:
        if self.logf:
            self.logf.write(line + "\n")
            self.logf.flush()
        if echo:
            print(line, flush=True)

    def seats(self) -> list[tuple[str, str, str]]:
        pairs = DEFAULT_SEATS
        if self.args.civs:
            pairs = [tuple(p.split("=", 1)) if "=" in p else (p, p.lower()) for p in self.args.civs.split(",")]
        pairs = [p for p in pairs if p[0] not in self.humans][:self.args.seats]
        kinds = dict(DEFAULT_PERSONALITIES)
        if self.args.personalities:
            kinds.update(dict(p.split("=", 1) for p in self.args.personalities.split(",")))
        order = list(PERSONALITIES)
        out = []
        for i, (civ, label) in enumerate(pairs):
            kind = kinds.get(label) or kinds.get(civ) or ("aggressive" if i == 0 else order[i % 3])
            if kind not in PERSONALITIES:
                raise SystemExit(f"unknown personality {kind!r}; one of {', '.join(PERSONALITIES)}")
            out.append((civ, label, kind))
        return out

    def start_env(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        tmp = self.out / "tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        bridge = self.args.bridge
        if bridge is None:
            src = ROOT / "build" / "bridge"
            if not (src / "CivBridge").is_file():
                raise SystemExit(f"no bridge at {src}: run scripts/build-bridge.sh, or pass --bridge")
            dst = self.out / "bridge"
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst)       # others may rebuild build/bridge meanwhile
            bridge = shlex.quote(str(dst / "CivBridge"))
        if self.args.saves:
            # The env passes --saves only when the client is installed; the bridge takes it from CIVBRIDGE_CMD too.
            saves = self.out / "saves"
            shutil.rmtree(saves, ignore_errors=True)
            bridge += f" --saves {shlex.quote(str(saves))}"
        for name in ("actions.jsonl", "server.log"):
            (self.out / name).unlink(missing_ok=True)
        env = {**os.environ, "CIVBRIDGE_CMD": bridge, "MCP_HOST": "127.0.0.1", "MCP_PORT": str(self.port),
               "OPENCIV_SEED": str(self.args.seed), "OPENCIV_TURN_LIMIT": str(self.args.turns),
               "OPENCIV_ACTION_LOG": str(self.out / "actions.jsonl"), "OPENCIV_BASELINES": "0",
               "OPENCIV_RECORD": "1", "TMPDIR": str(tmp)}
        with open(self.out / "server.log", "ab") as log:
            self.proc = subprocess.Popen([sys.executable, "-m", "agentenv_openciv3.server"], env=env,
                                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                         start_new_session=True)

    async def wait_ready(self) -> dict:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise SystemExit(f"the env exited ({self.proc.returncode}); see {self.out / 'server.log'}")
            with contextlib.suppress(Exception):
                card = await env_client.get_card(self.base, 5)
                if card.get("name"):
                    return card
            await asyncio.sleep(0.5)
        raise SystemExit(f"the env did not come up; see {self.out / 'server.log'}")

    def game_dir(self) -> Path | None:
        games = sorted((self.out / "tmp").glob("openciv3-*/game-*"), key=lambda p: p.stat().st_mtime)
        return games[-1] if games else None

    def stop_env(self) -> None:
        if not self.proc or self.proc.poll() is not None:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, sig)
            try:
                self.proc.wait(15)
                return
            except subprocess.TimeoutExpired:
                pass

    def write_meta(self, **extra) -> None:
        meta = {
            "seats": [{"civ": b.civ, "label": b.label, "personality": b.personality} for b in self.bots]
            or [{"civ": c, "label": la, "personality": k} for c, la, k in self.seats()],
            "labels": {b.civ: b.label for b in self.bots} | self.humans,
            "humans": self.humans, "play": self.play,
            "seed": self.args.seed, "size": self.args.size, "difficulty": self.args.difficulty,
            "barbarians": self.args.barbarians, "turn_limit": self.args.turns, "ai_opponents": self.args.ai_opponents,
            "live_url": self.live_url, "mcp_url": self.mcp_url, "port": self.port,
            "record_dir": "record", "saves_dir": "saves" if self.args.saves else None, "action_log": "actions.jsonl",
            "think_seconds": list(self.think), "error_rate": self.args.error_rate, **extra,
        }
        (self.out / "match.json").write_text(json.dumps(meta, indent=2) + "\n")

    async def run(self) -> int:
        self.out.mkdir(parents=True, exist_ok=True)
        self.logf = open(self.out / "bots.log", "w")
        seats = self.seats()
        self.start_env()
        started = time.time()
        try:
            card = await self.wait_ready()
            civs = [s[0] for s in seats] + list(self.humans)
            args = {"seed": self.args.seed, "size": self.args.size, "difficulty": self.args.difficulty,
                    "barbarians": self.args.barbarians, "turn_limit": self.args.turns, "civ": civs[0],
                    "opponents": len(civs) - 1 + self.args.ai_opponents, "seats": civs[1:],
                    "labels": {s[0]: s[1] for s in seats} | self.humans}
            if self.humans:
                args |= {"humans": list(self.humans), "human_turn_seconds": self.args.human_turn_seconds}
            if self.args.min_turn_seconds:
                args["min_turn_seconds"] = self.args.min_turn_seconds
            if self.args.broadcast:
                args["broadcast"] = json.loads(self.args.broadcast)
            game = await env_client.invoke_extension(self.base, card, NEW_GAME, args, 600)
            self.play = {civ: self.base + path for civ, path in (game.get("play") or {}).items()}
            self.turn_limit, self.width = game["turn_limit"], game["map"]["width"] if game["map"].get("wrap_x") else 0
            self.log(f"{ts()} match: seed {game['seed']}, {self.args.size} map {game['map']['width']}x"
                     f"{game['map']['height']}, {len(seats)} seats, {self.turn_limit} turns")
            for civ, label, kind in seats:
                self.log(f"         {label:>7} plays {civ} ({kind})")
            for civ, url in self.play.items():
                self.log(f"PLAY {civ} ({self.humans.get(civ, civ)}): {url}")
            (self.out / "play.json").write_text(json.dumps(
                {"live_url": self.live_url, "base": self.base, "humans": self.humans, "play": self.play}, indent=2))
            self.log(f"LIVE VIEW: {self.live_url}    (MCP: {self.mcp_url}; output: {self.out})")
            self.bots = [Bot(self, civ, label, kind, i) for i, (civ, label, kind) in enumerate(seats)]
            self.leaders = set(civs)
            self.write_meta(started=datetime.datetime.fromtimestamp(started).isoformat(timespec="seconds"),
                            status="playing")
            await asyncio.gather(*(b.play() for b in self.bots), self.progress(), self.watch_game())
            elapsed = time.time() - started
            summary = {}
            with contextlib.suppress(Exception):
                res = await env_client.get_data(self.base, 120)
                summary = res.parts[0].data if hasattr(res.parts[0], "data") else res.parts[0].root.data
            (self.out / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
            if self.args.recording:
                await self.save_recording(card)
            if self.args.linger:
                self.log(f"{ts()} game over; the env stays up {self.args.linger}s at {self.live_url}")
                await asyncio.sleep(self.args.linger)
            return self.finish(started, elapsed, summary)
        finally:
            self.collect()
            self.stop_env()
            shutil.rmtree(self.out / "tmp", ignore_errors=True)
            if self.logf:
                self.logf.close()

    async def watch_game(self) -> None:
        """With no bot seats left playing (only people), the game's end comes from data/get."""
        while not self.over:
            await asyncio.sleep(2)
            if self.bots and not all(b.over for b in self.bots):
                continue
            if not self.humans:
                break
            with contextlib.suppress(Exception):
                res = await env_client.get_data(self.base, 30)
                data = res.parts[0].data if hasattr(res.parts[0], "data") else res.parts[0].root.data
                if data.get("game_over"):
                    break
        self.over = True

    async def progress(self) -> None:
        """A line per turn the game reaches (every 10th on the console): the time since the previous one."""
        last, t0, start = 0, time.time(), time.time()
        while not self.over:
            await asyncio.sleep(0.2)
            turn = min((b.turn for b in self.bots if not b.over), default=last)
            if turn > last:
                now = time.time()
                self.log(f"{ts()} ---- T{turn} ({(now - t0) / (turn - last):.1f}s/turn, {now - start:.0f}s in) ----",
                         echo=self.args.verbose or turn // 10 > last // 10)
                last, t0 = turn, now

    async def save_recording(self, card: dict) -> None:
        try:
            res = await env_client.invoke_extension(self.base, card, "urn:openciv3:recording/v1",
                                                    {"formats": self.args.recording.split(",")}, 3600)
            for f in res.get("files") or []:
                (self.out / f["name"]).write_bytes(base64.b64decode(f["base64"]))
                self.log(f"{ts()} recording: {self.out / f['name']}")
        except Exception as e:
            self.log(f"{ts()} recording failed: {e}")

    def collect(self) -> None:
        """Copy the game's snapshots and autosave out of the env's temp dir before it goes away."""
        game = self.game_dir()
        if game is None:
            return
        for name in ("record", "autosave"):
            if (game / name).is_dir():
                shutil.rmtree(self.out / name, ignore_errors=True)
                shutil.copytree(game / name, self.out / name)
        log = self.out / "actions.jsonl"
        if log.exists():
            per: dict[str, list[str]] = {}
            labels = {b.civ: b.label for b in self.bots}
            for line in log.read_text().splitlines():
                with contextlib.suppress(ValueError):
                    seat = json.loads(line).get("seat") or (self.bots[0].civ if self.bots else "seat")
                    per.setdefault(labels.get(seat, seat), []).append(line)
            (self.out / "actions").mkdir(exist_ok=True)
            for label, lines in per.items():
                (self.out / "actions" / f"{label}.jsonl").write_text("\n".join(lines) + "\n")

    def finish(self, started: float, elapsed: float, summary: dict) -> int:
        self.collect()
        history = analyze(self.out / "record")
        bots = {b.label: {"civ": b.civ, "personality": b.personality, "result": b.result,
                          "calls": sum(b.stats.calls.values()), "failed": sum(b.stats.failed.values()),
                          "injected_errors": b.stats.injected, "by_tool": dict(b.stats.calls),
                          "failed_by_tool": dict(b.stats.failed), "wars_declared": b.wars_declared,
                          "notable": b.stats.events} for b in self.bots}
        (self.out / "bots.json").write_text(json.dumps(bots, indent=2) + "\n")
        calls = sum(b["calls"] for b in bots.values())
        failed = sum(b["failed"] for b in bots.values())
        turns = summary.get("turn") or max((b.turn for b in self.bots), default=0)
        self.write_meta(started=datetime.datetime.fromtimestamp(started).isoformat(timespec="seconds"),
                        status="finished", elapsed_seconds=round(elapsed, 1), turns_played=turns,
                        seconds_per_turn=round(elapsed / max(1, turns), 2), calls=calls, failed_calls=failed,
                        failure_rate=round(failed / max(1, calls), 4), victory=summary.get("victory"),
                        standings=summary.get("standings"), history=history)
        self.log(f"{ts()} done in {elapsed / 60:.1f} min ({elapsed / max(1, turns):.1f}s/turn over {turns} turns); "
                 f"{calls} calls, {failed} failed ({100 * failed / max(1, calls):.1f}%)")
        self.log(f"  wars declared: {len(history['wars'])}, peace signed: {len(history['peace'])}, "
                 f"cities captured: {len(history['captured'])}, razed: {len(history['razed'])}")
        for e in history["captured"] + history["razed"]:
            self.log(f"    T{e['turn']}: {e['text']}")
        for s in summary.get("standings") or []:
            self.log(f"  {s.get('seat') or s['civ']:>8} {s['civ']:<9} score {s['score']}"
                     + (" (defeated)" if s.get("defeated") else ""))
        self.log(f"  output: {self.out}")
        return 0


def analyze(record: Path) -> dict:
    """Wars, peace, and cities that changed hands or were razed, from consecutive snapshots."""
    out = {"wars": [], "peace": [], "captured": [], "razed": [], "eliminated": []}
    prev = None
    for path in sorted(record.glob("turn-*.json.gz")):
        try:
            with gzip.open(path, "rt") as f:
                snap = json.load(f)
        except (OSError, ValueError, EOFError):
            continue
        names = {p["index"]: (p.get("label") or p["civ"]) + f" ({p['civ']})" for p in snap["players"]}
        if prev is not None:
            t = snap["turn"]
            before = {(c["x"], c["y"]): c for c in prev["cities"]}
            now = {(c["x"], c["y"]): c for c in snap["cities"]}
            for xy, c in now.items():
                if xy in before and before[xy]["owner"] != c["owner"]:
                    out["captured"].append({"turn": t, "city": c["name"], "from": before[xy]["owner"],
                                            "to": c["owner"], "text": f"{names.get(c['owner'])} took {c['name']} "
                                            f"from {names.get(before[xy]['owner'])}"})
            for xy, c in before.items():
                if xy not in now:
                    out["razed"].append({"turn": t, "city": c["name"], "owner": c["owner"],
                                         "text": f"{c['name']} of {names.get(c['owner'])} was razed"})
            old = {p["index"]: p for p in prev["players"]}
            for p in snap["players"]:
                o = old.get(p["index"])
                if not o:
                    continue
                if p.get("defeated") and not o.get("defeated"):
                    out["eliminated"].append({"turn": t, "civ": p["civ"]})
                for j in set(p.get("at_war") or ()) - set(o.get("at_war") or ()):
                    if p["index"] < j and not snap["players"][j].get("civ", "").startswith("A Barbarian") \
                            if j < len(snap["players"]) else True:
                        out["wars"].append({"turn": t, "a": names.get(p["index"]), "b": names.get(j)})
                for j in set(o.get("at_war") or ()) - set(p.get("at_war") or ()):
                    if p["index"] < j:
                        out["peace"].append({"turn": t, "a": names.get(p["index"]), "b": names.get(j)})
        prev = snap
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--out", type=Path, default=None, help="output dir (default: playtest/runs/bots-<time>)")
    p.add_argument("--seats", type=int, default=9, help="how many seats, from the default list (default 9)")
    p.add_argument("--civs", help="civ=label,... instead of the default seats (Rome=opus,Greece=sonnet,...)")
    p.add_argument("--personalities", help="label=kind,... with kind expansionist, builder or aggressive")
    p.add_argument("--size", default="Standard")
    p.add_argument("--difficulty", default="Regent")
    p.add_argument("--barbarians", default="Roaming")
    p.add_argument("--turns", type=int, default=200)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--humans", help="civ=label,... human seats, played in the browser (docs/play.md)")
    p.add_argument("--human-turn-seconds", type=int, default=900,
                   help="a human seat idle this long has its turn ended for it; 0: never (default 900)")
    p.add_argument("--ai-opponents", type=int, default=0, help="engine-AI civs besides the seats (default 0)")
    p.add_argument("--min-turn-seconds", type=int, default=0,
                   help="the broadcast pace: no turn ends sooner than this after it began (default 0)")
    p.add_argument("--broadcast", help='the match\'s broadcast as JSON, e.g. \'{"title": "Test", "casters": true}\' '
                                       "(docs/tools.md)")
    p.add_argument("--port", type=int, default=0, help="env port (default: a free one)")
    p.add_argument("--bridge", help="CIVBRIDGE_CMD (default: a copy of build/bridge in the output dir)")
    p.add_argument("--no-saves", dest="saves", action="store_false", help="don't keep the engine's per-turn saves")
    p.add_argument("--think", type=float, nargs=2, default=(0.05, 0.3), metavar=("MIN", "MAX"),
                   help="random delay before each call, seconds (default 0.05 0.3)")
    p.add_argument("--fast", action="store_true", help="no think delay")
    p.add_argument("--error-rate", type=float, default=0.015,
                   help="share of calls preceded by a deliberately wrong call (default 0.015; with the game's own "
                        "refusals about 2%% of calls fail)")
    p.add_argument("--turn-budget", type=float, default=300, help="seconds a seat may spend on a turn (default 300)")
    p.add_argument("--recording", default="", help="also render the env's recording, e.g. html or mp4,html")
    p.add_argument("--linger", type=float, default=0, help="keep the env up this many seconds after the game")
    p.add_argument("-v", "--verbose", action="store_true", help="print every call")
    args = p.parse_args()
    if args.out is None:
        args.out = ROOT / "playtest" / "runs" / f"bots-{datetime.datetime.now():%Y%m%d-%H%M%S}"
    args.out = args.out.resolve()
    if not (0 if args.humans else 1) <= args.seats <= len(DEFAULT_SEATS) and not args.civs:
        p.error(f"--seats is 1-{len(DEFAULT_SEATS)}")
    match = Match(args)
    try:
        return asyncio.run(match.run())
    except KeyboardInterrupt:
        match.stop_env()
        return 130


if __name__ == "__main__":
    sys.exit(main())
