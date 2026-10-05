"""The viewer's data (docs/viewer.md): per-turn snapshots folded into one compact document, turn by turn.

The static map is kept once; each turn keeps what changed (tile owners, what each seat knows, how tiles look: overlay,
resource, improvements), the cities, the units, the scores and stats, the units' moves and the battles, and the
events: the bridge's own plus those derived from consecutive snapshots for every civ. The agents' actions come from
the env's action logs and are merged in when the document is written.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Callable, Iterable
from pathlib import Path

SCHEMA = 1
# Nine civ colours for a dark surface, validated as a categorical set (adjacent pairs: CVD dE >= 8.4); civs past
# nine reuse them in order, always next to a name.
PALETTE = ("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767", "#2b9db5")
BARBARIAN = "#6b6f78"
TERRAIN = ("ocean", "sea", "coast", "grassland", "plains", "desert", "tundra", "floodplain", "hills", "mountains",
           "forest", "jungle", "marsh", "volcano")
WATER = {"ocean", "sea", "coast"}
CIVILIAN = ("Settler", "Worker", "Explorer", "Galley", "Caravel", "Curragh", "Galleon", "Transport", "Leader",
            "Great Leader", "Scientific Leader", "Army")
# Bridge events the snapshots can't show; the rest are per-seat chatter or derived for every civ instead.
BRIDGE_KINDS = {"unit_lost", "unit_promoted", "gold_stolen", "disorder", "disorder_started", "city_starved",
                "contact", "defenseless", "victory", "engine_restarted", "attacked", "bombarded"}
SCORE_KEYS = ("total", "cities", "pop", "tiles", "techs")
# A player's stats per turn: the snapshot's own, then what the race needs (culture, era, shares of land and people;
# snapshots from before them have none) and what is counted here (military units, great wonders).
STAT_KEYS = ("gold", "government", "research", "at_war", "culture", "city_culture", "era", "land", "pop")
NONCOMBAT = set(CIVILIAN) | {"Scout"}   # the army counts the rest
ERAS = ("Ancient Times", "Middle Ages", "Industrial Age", "Modern Era")

Actions = dict[int, dict[int, list[dict]]]     # turn -> player index -> [{"text", "ok"}]
Calls = dict[int, dict[int, dict[str, int]]]   # turn -> player index -> {"ok", "failed"}
Notes = dict[int, dict[int, str]]              # turn -> player index -> text (an end_turn note, or a plan)
Messages = dict[int, list[dict]]               # turn -> [{"from": player index, "to": [index] | "all", "text"}]


def load_snapshots(directory: str | Path) -> list[dict]:
    """The `turn-NNNN.json.gz` snapshots in turn order; a turn written twice (after a restart) keeps the last."""
    snaps: dict[int, dict] = {}
    for path in sorted(Path(directory).glob("turn-*.json.gz")):
        if (snap := read_snapshot(path)) is not None:
            snaps[snap["turn"]] = snap
    return [snaps[t] for t in sorted(snaps)]


def read_snapshot(path: Path) -> dict | None:
    """A snapshot, or None when it is cut short (a crash, or the bridge is still writing it)."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, EOFError, ValueError):
        return None


def is_barbarian(p: dict) -> bool:
    return "barbarian" in str(p.get("civ", "")).lower()


def player_colors(players: Iterable[dict]) -> dict[int, str]:
    """Each player's colour by index, as the viewer gives them: the palette in order over the players that are not
    barbarians (in the order given, the players' index order), barbarians grey."""
    out, slot = {}, 0
    for p in players:
        barb = is_barbarian(p)
        out[p["index"]] = BARBARIAN if barb else PALETTE[slot % len(PALETTE)]
        slot += not barb
    return out


def seam(snap: dict) -> int:
    """An even x column with the least land around it: drawn as the left edge, no continent is cut in two."""
    w = snap["map"]["width"]
    land = [0] * w
    for row in snap["tiles"]:
        if row[2] not in WATER:
            land[row[0] % w] += 1
    window = [sum(land[(x + d) % w] for d in range(-3, 4)) for x in range(w)]
    best = min(range(w), key=window.__getitem__)
    return best - best % 2


class MatchData:
    """The viewer's document for one game, built a turn at a time with `add`."""

    def __init__(self, game: str = "game", labels: dict[str, str] | None = None, humans: Iterable[str] = (),
                 names: dict[str, str] | None = None):
        self.game = game
        self.labels = labels or {}           # civ -> label, for recordings made before seats had labels
        self.names = names or {}             # label -> the name the broadcast shows ("opus": "Opus 5.5")
        self.humans = {h.lower() for h in humans}   # civs people play (docs/play.md)
        self.meta: dict = {}
        self.players: list[dict] = []
        self.tiles: list[list] = []
        self.turns: list[dict] = []
        self._index: dict[tuple[int, int], int] = {}
        self._unit_types: dict[str, int] = {}
        self._ids: dict[str | int, int] = {}
        self._owners: list[int] = []
        self._known: list[int] = []
        self._looks: list[tuple] = []
        self._resources: dict[str, int] = {}
        self._improvements: dict[str, int] = {}
        self._prev: dict | None = None
        self._leader: int | None = None
        self._wonders: set[str] | None = None    # the great wonders built so far; None before the first snapshot

    # ---- building ----

    @classmethod
    def from_snapshots(cls, snaps: Iterable[dict], **kw) -> MatchData:
        m = cls(**kw)
        for s in snaps:
            m.add(s)
        return m

    def add(self, snap: dict) -> None:
        """Append the turn of `snap`; a turn at or before the last one is ignored."""
        if self.turns and snap["turn"] <= self.turns[-1]["turn"]:
            return
        if not self.turns:
            self._start(snap)
        self._players(snap)
        seats = self._seat_bits(snap)
        owners, known, looks = [], [], []
        for row in snap["tiles"]:
            i = self._index.get((row[0], row[1]))
            if i is None:
                continue
            if self._owners[i] != row[4]:
                self._owners[i] = row[4]
                owners.append([i, row[4]])
            mask = seats(row[6] if len(row) > 6 else 0)
            if self._known[i] != mask:
                self._known[i] = mask
                known.append([i, mask])
            look = self._look(row)
            if self._looks[i] != look:
                self._looks[i] = look
                looks.append([i, *look])
        turn = {
            "turn": snap["turn"],
            **({"date": snap["date"]} if snap.get("date") else {}),
            "owners": owners,
            "known": known,
            "cities": [[c["x"], c["y"], c["name"], c["owner"], c["size"], int(bool(c.get("capital"))),
                        c.get("production"), self._id(c.get("id")), c.get("era", 0), int(bool(c.get("walls")))]
                       for c in snap["cities"]],
            "units": [self._unit_row(u) for u in snap["units"]],
            "scores": {str(p["index"]): [p["score"][k] for k in SCORE_KEYS] + [int(bool(p.get("defeated")))]
                       for p in snap["players"] if not is_barbarian(p)},
            "stats": {str(p["index"]): self._stats(p, snap) for p in snap["players"] if not is_barbarian(p)},
            "events": self._events(snap),
        }
        if looks:
            turn["looks"] = looks
        if moves := [self._move_row(m, seats) for m in snap.get("moves") or ()]:
            turn["moves"] = moves
        if battles := [self._battle_row(b, seats) for b in snap.get("battles") or ()]:
            turn["battles"] = battles
        self.turns.append(turn)
        if snap.get("victory"):
            self.meta["victory"] = snap["victory"]
        self._prev = snap

    def _start(self, snap: dict) -> None:
        self.meta = {"seed": snap.get("seed"), "turn_limit": snap.get("turn_limit"), "map": snap["map"],
                     "seam": seam(snap), "terrain": list(TERRAIN), "unit_types": [], "civilian": list(CIVILIAN),
                     "resources": [], "improvements": [], "victory": None}
        for row in snap["tiles"]:
            self._index[(row[0], row[1])] = len(self.tiles)
            terrain = TERRAIN.index(row[2]) if row[2] in TERRAIN else 0
            overlay = TERRAIN.index(row[3]) if row[3] in TERRAIN else -1
            # The river's edges (NE=1, SE=2, SW=4, NW=8, ...); older snapshots have 0/1. Non-zero: a river.
            self.tiles.append([row[0], row[1], terrain, overlay, int(row[5] or 0)])
            self._looks.append((overlay, -1, 0, 0))
        self._owners = [-1] * len(self.tiles)
        self._known = [0] * len(self.tiles)

    def _players(self, snap: dict) -> None:
        """Players keep their colour for the whole game; a seat's number comes from `seats` (schema 2)."""
        known = {p["index"]: p for p in self.players}
        seat_of = {s["index"]: k for k, s in enumerate(snap.get("seats") or [])}
        if not seat_of:   # schema 1: the seats are the players marked is_human, in index order
            seat_of = {p["index"]: k for k, p in enumerate(
                p for p in snap["players"] if p.get("is_human") or p["civ"] in self.labels)}
        slot = sum(not p["barbarian"] for p in self.players)
        for p in snap["players"]:
            if p["index"] in known:
                have = known[p["index"]]
                have["label"] = p.get("label") or have["label"]
                if self.names.get(have["label"]):
                    have["name"] = self.names[have["label"]]
                continue
            barb = is_barbarian(p)
            self.players.append({
                "index": p["index"], "civ": p["civ"], "label": p.get("label") or self.labels.get(p["civ"]),
                **({"name": self.names[label]} if (label := p.get("label") or self.labels.get(p["civ"])) in self.names
                   else {}),
                **({"human": True} if p["civ"].lower() in self.humans else {}),
                "barbarian": barb, "seat": seat_of.get(p["index"]),
                "color": BARBARIAN if barb else PALETTE[slot % len(PALETTE)],
                "engine_color": "#" + "".join(f"{v:02x}" for v in (p.get("color") or (128, 128, 128))),
            })
            slot += not barb

    def _seat_bits(self, snap: dict) -> Callable[[int], int]:
        """Maps a tile's `known` to a mask over seats: schema 2 already is one; schema 1's 0/1 means every seat."""
        if snap.get("seats"):
            return lambda v: int(v)
        every = (1 << max(1, sum(p["seat"] is not None for p in self.players))) - 1
        return lambda v: every if v else 0

    def _id(self, engine_id: str | int | None) -> int:
        """A small int for the engine's string id (`"Warrior-12"`), stable for the game; -1 when there is none."""
        if engine_id is None:
            return -1
        return self._ids.setdefault(engine_id, len(self._ids))

    def _look(self, row: list) -> tuple[int, int, int, int]:
        """How a tile looks beyond its terrain: (overlay, resource, improvements mask, bonus grassland), indexing
        TERRAIN, meta.resources and meta.improvements; -1 for no overlay or resource. Snapshots before the art
        fields (schema 2 without columns 7-9) have no resource, improvements or bonus."""
        overlay = TERRAIN.index(row[3]) if row[3] in TERRAIN else -1
        resource = row[7] if len(row) > 7 else None
        res = -1 if resource is None else self._table(self._resources, "resources", resource)
        imp = 0
        for key in (row[8] if len(row) > 8 else None) or ():
            imp |= 1 << self._table(self._improvements, "improvements", key)
        return overlay, res, imp, int(bool(row[9])) if len(row) > 9 else 0

    def _table(self, index: dict[str, int], key: str, name: str) -> int:
        if name not in index:
            index[name] = len(self.meta[key])
            self.meta[key].append(name)
        return index[name]

    def _unit_row(self, u: dict) -> list:
        """[id, x, y, owner, type], then [hp, hp_max, fortified] unless the unit is a healthy 3-hp unit that isn't
        fortified (most of them)."""
        row = [self._id(u.get("id")), u["x"], u["y"], u["owner"], self._unit_type(u["type"])]
        hp, hp_max, fortified = u.get("hp"), u.get("hp_max"), bool(u.get("fortified"))
        if hp is not None and hp_max is not None and (hp != hp_max or hp_max != 3 or fortified):
            row += [hp, hp_max, int(fortified)]
        return row

    def _move_row(self, m: dict, seats: Callable[[int], int]) -> list:
        """[seq, unit id, owner, type, seen, x0, y0, x1, y1, ...]: one unit's run of steps."""
        return [m["seq"], self._id(m.get("unit")), m["owner"], self._unit_type(m["type"]), seats(m.get("seen", 0)),
                *(v for xy in m["path"] for v in xy)]

    def _battle_row(self, b: dict, seats: Callable[[int], int]) -> list:
        """[seq, kind, winner, rounds, city, seen, *attacker, *defender]: kind 0 an attack, 1 a bombardment; winner
        "a", "d" or "r" (a retreat); rounds one letter a round, "a" the attacker won it; city 0 none, 1 it stood, 2
        taken, 3 destroyed; each side [owner, type, x, y, hp_before, hp_after, hp_max]."""
        city = 0 if not b.get("city") else 3 if b.get("razed") else 2 if b.get("captured") else 1
        side = [[s["owner"], self._unit_type(s["type"]), s["x"], s["y"], s["hp_before"], s["hp_after"], s["hp_max"]]
                for s in (b["attacker"], b["defender"])]
        return [b["seq"], int(b.get("kind") == "bombard"), {"attacker": "a", "defender": "d"}.get(b["winner"], "r"),
                "".join(b.get("rounds") or ()), city, seats(b.get("seen", 0)), *side[0], *side[1]]

    def _stats(self, p: dict, snap: dict) -> dict:
        i = p["index"]
        out = {k: p[k] for k in STAT_KEYS if k in p}
        out["military"] = sum(1 for u in snap["units"] if u["owner"] == i and u["type"] not in NONCOMBAT)
        out["wonders"] = sum(len(c.get("wonders") or ()) for c in snap["cities"] if c["owner"] == i)
        return out

    def _unit_type(self, name: str) -> int:
        if name not in self._unit_types:
            self._unit_types[name] = len(self.meta["unit_types"])
            self.meta["unit_types"].append(name)
        return self._unit_types[name]

    def name(self, index: int) -> str:
        p = next((p for p in self.players if p["index"] == index), None)
        return (p.get("name") or p["label"] or p["civ"]) if p else "?"

    def _events(self, snap: dict) -> list[dict]:
        prev, t, out = self._prev, snap["turn"], []
        live = [p for p in snap["players"] if not is_barbarian(p)]
        if prev is not None:
            name = self.name
            # a city by its engine id (schema 2): a civ that lost a city may found another of the same name
            key = (lambda c: c.get("id") or c["name"]) if all(c.get("id") for c in snap["cities"]) and all(
                c.get("id") for c in prev["cities"]) else (lambda c: c["name"])
            before = {key(c): c for c in prev["cities"]}
            now = {key(c): c for c in snap["cities"]}
            for c in snap["cities"]:
                was = before.get(key(c))
                if was is None:
                    out.append(_ev("city_founded", c["owner"], f"{name(c['owner'])} founded {c['name']}", c))
                elif was["owner"] != c["owner"]:
                    out.append(_ev("city_captured", c["owner"], f"{name(c['owner'])} took {c['name']} from "
                                   f"{name(was['owner'])}", c, frm=was["owner"]))
            for k, c in before.items():
                if k not in now:
                    out.append(_ev("city_destroyed", c["owner"], f"{name(c['owner'])} lost {c['name']}; it was razed",
                                   c))
            old = {p["index"]: p for p in prev["players"]}
            for p in live:
                o = old.get(p["index"])
                if o is None:
                    continue
                i = p["index"]
                if p["score"]["techs"] > o["score"]["techs"]:
                    learned = o.get("research") if o.get("research") != p.get("research") else None
                    out.append(_ev("tech_learned", i, f"{name(i)} learned {learned}" if learned else
                                   f"{name(i)} learned a tech ({p['score']['techs']} known)"))
                if p.get("government") and o.get("government") and p["government"] != o["government"]:
                    out.append(_ev("government_changed", i, f"{name(i)} became a {p['government']}"))
                if p.get("defeated") and not o.get("defeated"):
                    out.append(_ev("civ_destroyed", i, f"{name(i)} was eliminated"))
                for j in sorted(set(p.get("at_war") or ()) - set(o.get("at_war") or ())):
                    if i < j or j not in {q["index"] for q in live}:
                        out.append(_ev("war_declared", i, f"{name(i)} and {name(j)} are at war", frm=j))
                for j in sorted(set(o.get("at_war") or ()) - set(p.get("at_war") or ())):
                    if i < j:
                        out.append(_ev("peace_signed", i, f"{name(i)} and {name(j)} made peace", frm=j))
        if live and t > 5:
            top = max(live, key=lambda p: (p["score"]["total"], -p["index"]))["index"]
            if self._leader is not None and top != self._leader:
                out.append(_ev("lead_change", top, f"{self.name(top)} takes the lead from {self.name(self._leader)}",
                               frm=self._leader))
            self._leader = top
        elif live and self._leader is None:
            self._leader = max(live, key=lambda p: (p["score"]["total"], -p["index"]))["index"]
        out += self._world_events(snap)
        derived_contact = any("contacts" in p for p in snap["players"])
        civs = {p["civ"]: p["index"] for p in snap["players"]}
        for e in snap.get("events") or ():
            if e.get("kind") not in BRIDGE_KINDS or (e["kind"] == "contact" and derived_contact):
                continue
            owner = civs.get(e.get("civ"), next((p["index"] for p in snap["players"] if p.get("is_human")), -1))
            ev = {"kind": e["kind"], "owner": owner, "text": f"{self.name(owner)}: {e.get('text') or e['kind']}",
                  "source": "bridge"}
            if "x" in e and "y" in e:
                ev.update(x=e["x"], y=e["y"])
            out.append(ev)
        return out

    def _world_events(self, snap: dict) -> list[dict]:
        """What every civ did that the scores don't show: great wonders, new eras, first contacts, the seats' trades,
        units upgraded and units landed from ships (docs/viewer.md, the stream's stories)."""
        prev, name, out = self._prev, self.name, []
        # a wonder new in the world; the first snapshot with the field (a game begun on an older bridge) only seeds them
        if any("wonders" in c for c in snap["cities"]):
            wonders = {(w, c["name"], c["owner"], c["x"], c["y"])
                       for c in snap["cities"] for w in c.get("wonders") or ()}
            if self._wonders is not None:
                for w, city, owner, x, y in sorted(wonders):
                    if w not in self._wonders:
                        out.append(_ev("wonder_built", owner, f"{name(owner)} completed {w} in {city}",
                                       {"x": x, "y": y}, wonder=w, city=city))
            self._wonders = (self._wonders or set()) | {w[0] for w in wonders}
        for t in snap.get("trades") or ():
            out.append(_ev("trade", t["a"], f"{name(t['a'])} traded {t['a_gave']} to {name(t['b'])} for {t['b_gave']}",
                           frm=t["b"], gave=t["a_gave"], got=t["b_gave"]))
        if prev is None:
            return out
        old = {p["index"]: p for p in prev["players"]}
        live = {p["index"] for p in snap["players"] if not is_barbarian(p) and not p.get("defeated")}
        for p in snap["players"]:
            o = old.get(p["index"])
            if o is None or p["index"] not in live:
                continue
            i = p["index"]
            if (p.get("era") or 0) > (o.get("era") or 0) and "era" in o:
                era = ERAS[min(p["era"], len(ERAS) - 1)]
                out.append(_ev("era_entered", i, f"{name(i)} enters the {era}", era=era))
            if "contacts" in p and "contacts" in o:
                for j in sorted(set(p["contacts"]) - set(o["contacts"])):
                    if i < j and j in live:
                        out.append(_ev("contact", i, f"First contact: {name(i)} meets {name(j)}", frm=j))
        # units by engine id: one whose type changed was upgraded; one off a ship onto land landed there
        before = {u["id"]: u for u in prev["units"] if u.get("id")}
        ships = {u["id"]: u for u in snap["units"] if u.get("id")}
        upgraded: dict[int, list[tuple[dict, str]]] = {}
        landed: dict[tuple[int, int], list[dict]] = {}
        for u in snap["units"]:
            was = before.get(u.get("id"))
            if was is None or was["owner"] != u["owner"]:
                continue
            if was["type"] != u["type"]:
                upgraded.setdefault(u["owner"], []).append((u, was["type"]))
            if was.get("aboard") and not u.get("aboard") and not self._water(u["x"], u["y"]):
                # off a ship that was at sea, or that sailed from port since (not one that stayed in port: walking
                # out of it is no landing), onto land not its own (ashore at home is no story)
                ship_was, ship = before.get(was["aboard"]), ships.get(was["aboard"])
                at = (lambda v: (v["x"], v["y"]) if v else None)   # noqa: E731
                moved = ship is not None and ship_was is not None and at(ship) != at(ship_was)
                sailed = self._water(was["x"], was["y"]) or moved
                land_of = self._owner_at(snap, u["x"], u["y"])
                if sailed and land_of != u["owner"]:
                    landed.setdefault((u["owner"], land_of), []).append(u)
        for owner, ups in sorted(upgraded.items()):
            pairs: dict[tuple[str, str], int] = {}
            for u, was in ups:
                pairs[(was, u["type"])] = pairs.get((was, u["type"]), 0) + 1
            (frm, to), n = max(pairs.items(), key=lambda kv: kv[1])
            what = f"{n} {frm} to {to}" + (f" and {len(ups) - n} more" if len(ups) > n else "")
            out.append(_ev("units_upgraded", owner, f"{name(owner)} upgraded {what}", ups[0][0]))
        for (owner, land_of), units in sorted(landed.items()):
            u, n = units[0], len(units)
            where = f" in {name(land_of)}'s land" if land_of >= 0 else " on unclaimed land"
            out.append(_ev("landing", owner, f"{name(owner)} landed {n} unit{'s' if n > 1 else ''} from the sea{where}",
                           u, frm=land_of if land_of >= 0 else None))
        return out

    def _owner_at(self, snap: dict, x: int, y: int) -> int:
        i = self._index.get((x, y))
        return self._owners[i] if i is not None else -1

    def _water(self, x: int, y: int) -> bool:
        i = self._index.get((x, y))
        return i is not None and TERRAIN[self.tiles[i][2]] in WATER

    # ---- writing ----

    def document(self, since: int = -1, *, actions: Actions | None = None, calls: Calls | None = None,
                 notes: Notes | None = None, plans: Notes | None = None, messages: Messages | None = None) -> dict:
        """The document with the turns after `since`; `static` only when since < 0. `actions[t]` are the actions
        of turn t, shown on entry t + 1, as are the calls, end_turn notes, plans and messages (docs/viewer.md);
        notes, plans and messages only when there are some."""
        turns = []
        for entry in self.turns:
            if entry["turn"] <= since:
                continue
            made = entry["turn"] - 1
            turn = {**entry,
                    "actions": {str(i): a for i, a in ((actions or {}).get(made) or {}).items()},
                    "calls": {str(i): c for i, c in ((calls or {}).get(made) or {}).items()}}
            for key, by_turn in (("notes", notes), ("plans", plans)):
                if said := (by_turn or {}).get(made):
                    turn[key] = {str(i): text for i, text in said.items()}
            if said := (messages or {}).get(made):
                turn["messages"] = list(said)
            turns.append(turn)
        doc = {"schema": SCHEMA, "game": self.game, "meta": self.meta, "players": self.players, "turns": turns}
        if since < 0:
            doc["static"] = {"tiles": self.tiles}
        return doc

    def last_turn(self) -> int:
        return self.turns[-1]["turn"] if self.turns else -1


def _ev(kind: str, owner: int, text: str, at: dict | None = None, *, frm: int | None = None, **fields) -> dict:
    e: dict = {"kind": kind, "owner": owner, "text": text, "source": "derived", **fields}
    if frm is not None:
        e["from"] = frm
    if at is not None:
        e["x"], e["y"] = at["x"], at["y"]
    return e


def actions_from_log(path: str | Path) -> tuple[Actions, Calls]:
    """Per-turn actions and call counts from env action logs (OPENCIV_ACTION_LOG), keyed by the civ in `seat` (a
    single-seat log has none: key -1, which `MatchData.document` callers map to the seat's player)."""
    from .actionlog import describe

    actions: Actions = {}
    calls: Calls = {}
    for line in Path(path).read_text().splitlines():
        row = json.loads(line)
        turn = row.get("turn")
        if turn is None:
            continue
        seat = row.get("seat", -1)
        c = calls.setdefault(turn, {}).setdefault(seat, {"ok": 0, "failed": 0})
        c["ok" if row.get("ok") else "failed"] += 1
        if text := describe(row.get("tool"), row.get("args") or {}):
            ok = bool(row.get("ok"))
            actions.setdefault(turn, {}).setdefault(seat, []).append(
                {"text": text if ok else f"{text} ✗ {row.get('error_code')}", "ok": ok})
    return actions, calls
