"""The viewer's data (docs/viewer.md): per-turn snapshots folded into one compact document, turn by turn.

The static map is kept once; each turn keeps what changed (tile owners, what each seat knows), the cities, the
units, the scores and stats, and the events: the bridge's own plus those derived from consecutive snapshots for
every civ. The agents' actions come from the env's action logs and are merged in when the document is written.
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

Actions = dict[int, dict[int, list[dict]]]     # turn -> player index -> [{"text", "ok"}]
Calls = dict[int, dict[int, dict[str, int]]]   # turn -> player index -> {"ok", "failed"}


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

    def __init__(self, game: str = "game", labels: dict[str, str] | None = None):
        self.game = game
        self.labels = labels or {}           # civ -> label, for recordings made before seats had labels
        self.meta: dict = {}
        self.players: list[dict] = []
        self.tiles: list[list] = []
        self.turns: list[dict] = []
        self._index: dict[tuple[int, int], int] = {}
        self._unit_types: dict[str, int] = {}
        self._ids: dict[str | int, int] = {}
        self._owners: list[int] = []
        self._known: list[int] = []
        self._prev: dict | None = None
        self._leader: int | None = None

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
        owners, known = [], []
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
        self.turns.append({
            "turn": snap["turn"],
            "owners": owners,
            "known": known,
            "cities": [[c["x"], c["y"], c["name"], c["owner"], c["size"], int(bool(c.get("capital"))),
                        c.get("production"), self._id(c.get("id"))] for c in snap["cities"]],
            "units": [[self._id(u.get("id")), u["x"], u["y"], u["owner"], self._unit_type(u["type"])]
                      for u in snap["units"]],
            "scores": {str(p["index"]): [p["score"][k] for k in SCORE_KEYS] + [int(bool(p.get("defeated")))]
                       for p in snap["players"] if not is_barbarian(p)},
            "stats": {str(p["index"]): {k: p[k] for k in ("gold", "government", "research", "at_war") if k in p}
                      for p in snap["players"] if not is_barbarian(p)},
            "events": self._events(snap),
        })
        if snap.get("victory"):
            self.meta["victory"] = snap["victory"]
        self._prev = snap

    def _start(self, snap: dict) -> None:
        self.meta = {"seed": snap.get("seed"), "turn_limit": snap.get("turn_limit"), "map": snap["map"],
                     "seam": seam(snap), "terrain": list(TERRAIN), "unit_types": [], "civilian": list(CIVILIAN),
                     "victory": None}
        for row in snap["tiles"]:
            self._index[(row[0], row[1])] = len(self.tiles)
            terrain = TERRAIN.index(row[2]) if row[2] in TERRAIN else 0
            overlay = TERRAIN.index(row[3]) if row[3] in TERRAIN else -1
            self.tiles.append([row[0], row[1], terrain, overlay, int(bool(row[5]))])
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
                known[p["index"]]["label"] = p.get("label") or known[p["index"]]["label"]
                continue
            barb = is_barbarian(p)
            self.players.append({
                "index": p["index"], "civ": p["civ"], "label": p.get("label") or self.labels.get(p["civ"]),
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

    def _unit_type(self, name: str) -> int:
        if name not in self._unit_types:
            self._unit_types[name] = len(self.meta["unit_types"])
            self.meta["unit_types"].append(name)
        return self._unit_types[name]

    def name(self, index: int) -> str:
        p = next((p for p in self.players if p["index"] == index), None)
        return (p["label"] or p["civ"]) if p else "?"

    def _events(self, snap: dict) -> list[dict]:
        prev, t, out = self._prev, snap["turn"], []
        live = [p for p in snap["players"] if not is_barbarian(p)]
        if prev is not None:
            name = self.name
            before = {c["name"]: c for c in prev["cities"]}
            now = {c["name"]: c for c in snap["cities"]}
            for c in snap["cities"]:
                was = before.get(c["name"])
                if was is None:
                    out.append(_ev("city_founded", c["owner"], f"{name(c['owner'])} founded {c['name']}", c))
                elif was["owner"] != c["owner"]:
                    out.append(_ev("city_captured", c["owner"], f"{name(c['owner'])} took {c['name']} from "
                                   f"{name(was['owner'])}", c, frm=was["owner"]))
            for n, c in before.items():
                if n not in now:
                    out.append(_ev("city_destroyed", c["owner"], f"{name(c['owner'])} lost {n}; it was razed", c))
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
        civs = {p["civ"]: p["index"] for p in snap["players"]}
        for e in snap.get("events") or ():
            if e.get("kind") not in BRIDGE_KINDS:
                continue
            owner = civs.get(e.get("civ"), next((p["index"] for p in snap["players"] if p.get("is_human")), -1))
            ev = {"kind": e["kind"], "owner": owner, "text": f"{self.name(owner)}: {e.get('text') or e['kind']}",
                  "source": "bridge"}
            if "x" in e and "y" in e:
                ev.update(x=e["x"], y=e["y"])
            out.append(ev)
        return out

    # ---- writing ----

    def document(self, since: int = -1, *, actions: Actions | None = None, calls: Calls | None = None) -> dict:
        """The document with the turns after `since`; `static` only when since < 0. `actions[t]` are the actions
        of turn t, shown on entry t + 1 (docs/viewer.md)."""
        turns = []
        for entry in self.turns:
            if entry["turn"] <= since:
                continue
            made = entry["turn"] - 1
            turns.append({**entry,
                          "actions": {str(i): a for i, a in ((actions or {}).get(made) or {}).items()},
                          "calls": {str(i): c for i, c in ((calls or {}).get(made) or {}).items()}})
        doc = {"schema": SCHEMA, "game": self.game, "meta": self.meta, "players": self.players, "turns": turns}
        if since < 0:
            doc["static"] = {"tiles": self.tiles}
        return doc

    def last_turn(self) -> int:
        return self.turns[-1]["turn"] if self.turns else -1


def _ev(kind: str, owner: int, text: str, at: dict | None = None, *, frm: int | None = None) -> dict:
    e: dict = {"kind": kind, "owner": owner, "text": text, "source": "derived"}
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
