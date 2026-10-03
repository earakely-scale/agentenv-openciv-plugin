"""Game recordings rendered from the bridge's per-turn snapshots (docs/recording.md): mp4, gif, html or png.

A frame is a 1920x1080 "broadcast" of one turn, drawn from the viewer's data (`matchdata.MatchData`): the map cut
at the column with the least land and cropped to the land, the standings with each agent's calls and latest
action, the territory, the key moments and the score chart. A frame for turn T shows nothing after T. The html
format is the match viewer (docs/viewer.md) with the data embedded.

Pillow only (plus ffmpeg on PATH for mp4), so harnesses and tests can render a directory of snapshots:

    python -m agentenv_openciv3.recording <record dir> --out <dir> [--actions actions.jsonl ...] [--formats mp4,html]
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import shutil
import subprocess
import tempfile
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import matchdata
from .actionlog import describe
from .matchdata import MatchData, is_barbarian  # noqa: F401  (re-exported for live.py)

FORMATS = {"mp4": "video/mp4", "gif": "image/gif", "html": "text/html", "png": "image/png"}
BASELINE_LABELS = {"engine_ai": "built-in AI", "settler_bot": "settler bot", "found_capital": "capital only",
                   "null": "do-nothing"}
URGENT = {"threat", "unit_lost", "war_declared", "city_destroyed", "city_captured", "disorder", "disorder_started",
          "city_starved", "gold_stolen", "defenseless", "riot_risk", "civ_destroyed"}

# ---- the frame's look ----

W, H = 1920, 1080
SS = 2                      # supersampling for the map and the chart
PAD, GAP = 16, 12
HEADER_H = 64
PANEL_X = 1268              # the right panel starts here; the map gets PAD..PANEL_X-PAD
CHART_MIN_H = 260
MAX_HW = 26                 # the largest half tile width, for small maps
GIF_SIZE = (960, 540)

BG, PANEL, TEXT, MUTED, GRID = (20, 22, 26), (27, 30, 36), (232, 233, 236), (139, 144, 154), (42, 46, 54)
SOFT = (196, 199, 206)      # secondary ink, between text and muted
DARK = (12, 13, 16)         # rings and label halos
UP, DOWN = (110, 184, 132), (216, 112, 112)
TRACK, HILITE, ALERT_BG, ALERT = (36, 40, 47), (33, 37, 44), (48, 32, 35), (240, 116, 112)
FOG = (13, 15, 19)
DIM, WARN = MUTED, DOWN     # kept for callers of the old renderer

TERRAIN = {
    "ocean": (21, 35, 56), "sea": (25, 42, 67), "coast": (37, 60, 88), "grassland": (74, 95, 66),
    "plains": (102, 100, 70), "desert": (132, 122, 94), "tundra": (104, 108, 106), "floodplain": (84, 103, 68),
    "hills": (96, 88, 68), "mountains": (112, 108, 104), "forest": (52, 77, 56), "jungle": (45, 79, 61),
    "marsh": (63, 81, 76), "volcano": (92, 64, 58),
}
RIVER = (64, 98, 132)
TINT_LAND, TINT_WATER = 0.35, 0.18

# Key moments: a moment's rank is its turn minus an age handicap by kind, so an elimination stays on the board
# ~40 turns longer than a city founding, and techs only fill the list when nothing else happened.
PRIORITY = {"victory": 0, "civ_destroyed": 0, "city_captured": 1, "city_destroyed": 2, "war_declared": 2,
            "peace_signed": 3, "lead_change": 3, "government_changed": 4, "city_founded": 5, "tech_learned": 6}
HANDICAP = {"victory": 0, "civ_destroyed": 0, "city_captured": 4, "city_destroyed": 6, "war_declared": 4,
            "peace_signed": 10, "lead_change": 8, "government_changed": 14, "city_founded": 20, "tech_learned": 70}
TAG = {"victory": "VICTORY", "civ_destroyed": "OUT", "city_captured": "CAPTURED", "city_destroyed": "RAZED",
       "war_declared": "WAR", "peace_signed": "PEACE", "lead_change": "LEAD", "government_changed": "GOV'T",
       "city_founded": "FOUNDED", "tech_learned": "TECH"}
STRONG = {"victory", "civ_destroyed", "city_captured", "city_destroyed", "war_declared"}
MOMENT_WINDOW, MAX_MOMENTS = 80, 11
MARK_TURNS = 4              # a capture or a razing is ringed on the map for this many turns

FONTS = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
]
ASCII = str.maketrans({"→": "->", "–": "-", "—": "-", "…": "...", "✗": "x", "×": "x", "▲": "+", "▼": "-",
                       "◆": "*", "·": "-"})


@dataclass(frozen=True)
class File:
    name: str
    content_type: str
    data: bytes


# ---- input ----

load_snapshots = matchdata.load_snapshots


def timeline_from_log(path: str | Path) -> dict[int, list[dict]]:
    """Per-turn game actions from an env action log (OPENCIV_ACTION_LOG)."""
    out: dict[int, list[dict]] = {}
    for line in Path(path).read_text().splitlines():
        row = json.loads(line)
        if (text := describe(row.get("tool"), row.get("args") or {})) and row.get("turn") is not None:
            ok = bool(row.get("ok"))
            out.setdefault(row["turn"], []).append({"text": text if ok else f"{text} ({row.get('error_code')})",
                                                    "ok": ok})
    return out


def by_player(per_turn: dict | None, players: list[dict]) -> dict:
    """Per-turn, per-seat data (actions or calls) keyed by player index. Keys may already be indices, or name a
    seat's civ or label (multi-seat action logs), or be -1 (a single-seat log: the game's only seat)."""
    if not per_turn:
        return {}
    seats = [p for p in players if p.get("seat") is not None] or [p for p in players if not p["barbarian"]][:1]
    indices = {p["index"] for p in players}
    names = {str(n).lower(): p["index"] for p in players for n in (p["civ"], p.get("label")) if n}

    def key(k) -> int | None:
        if isinstance(k, str) and k.lstrip("-").isdecimal():
            k = int(k)
        if isinstance(k, int) and k in indices:
            return k
        if isinstance(k, str):
            return names.get(k.lower())
        return seats[0]["index"] if len(seats) == 1 else None

    out: dict[int, dict] = {}
    for t, rows in per_turn.items():
        for k, v in rows.items():
            if (i := key(k)) is not None:
                cur = out.setdefault(int(t), {})
                if isinstance(v, list):
                    cur.setdefault(i, []).extend(v)
                else:
                    c = cur.setdefault(i, {"ok": 0, "failed": 0})
                    c["ok"] += v.get("ok", 0)
                    c["failed"] += v.get("failed", 0)
    return out


def split_timeline(timeline: dict | None, players: list[dict]) -> dict:
    """The old merged timeline (turn -> [{"text", "ok"}], each text prefixed "name: " when several seats play) as
    per-seat actions (turn -> player index -> [...])."""
    if not timeline:
        return {}
    seats = [p for p in players if p.get("seat") is not None] or [p for p in players if not p["barbarian"]][:1]
    names = sorted(((str(n), p["index"]) for p in seats for n in (p.get("label"), p["civ"]) if n),
                   key=lambda kv: -len(kv[0]))
    out: dict[int, dict] = {}
    for t, rows in timeline.items():
        for a in rows:
            text, who = a.get("text", ""), seats[0]["index"] if len(seats) == 1 else None
            for n, i in names if len(seats) > 1 else ():
                if text.startswith(n + ": "):
                    text, who = text[len(n) + 2:], i
                    break
            if who is not None:
                out.setdefault(int(t), {}).setdefault(who, []).append({**a, "text": text})
    return out


# ---- fonts and text ----

@cache
def _font_files() -> tuple[str, str] | None:
    if path := os.environ.get("OPENCIV_FONT"):
        return path, os.environ.get("OPENCIV_FONT_BOLD", path)
    for regular, bold in FONTS:
        if Path(regular).is_file():
            return regular, bold if Path(bold).is_file() else regular
    return None


@cache
def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    files = _font_files()
    return ImageFont.truetype(files[bold], size) if files else ImageFont.load_default(size=size)


def txt(s: str) -> str:
    """Pillow's built-in font has no accents or arrows: fold to ASCII when no system font was found."""
    if _font_files():
        return s
    s = unicodedata.normalize("NFKD", s.translate(ASCII))
    return "".join(c for c in s if not unicodedata.combining(c) and c.isascii())


def fit(s: str, f: ImageFont.FreeTypeFont, width: float) -> str:
    s = txt(s)
    if f.getlength(s) <= width:
        return s
    ell = txt("…")
    while s and f.getlength(s + ell) > width:
        s = s[:-1]
    return (s.rstrip() + ell) if s else ""


# ---- small helpers ----

def blend(a: tuple, b: tuple, t: float) -> tuple[int, int, int]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b, strict=False))


def hex_color(c: Iterable[int]) -> str:
    return "#" + "".join(f"{v:02x}" for v in c)


def rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def spread(ys: list[float], gap: float, lo: float, hi: float) -> list[float]:
    """Positions as close to `ys` as possible, at least `gap` apart, within [lo, hi] when they fit."""
    order = sorted(range(len(ys)), key=ys.__getitem__)
    out, prev = list(ys), lo - gap
    for i in order:
        prev = out[i] = max(ys[i], prev + gap)
    nxt = hi + gap
    for i in reversed(order):
        nxt = out[i] = min(out[i], nxt - gap)
    return out


def nice_step(top: float, n: int = 5) -> int:
    for s in (10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000, 20000, 25000, 50000):
        if top / s <= n:
            return s
    return 100000


def star(cx: float, cy: float, r: float) -> list[tuple[float, float]]:
    pts = []
    for i in range(10):
        a = -math.pi / 2 + i * math.pi / 5
        rr = r if i % 2 == 0 else r * 0.45
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    return pts


# ---- frames ----

class Renderer:
    """Draws the broadcast frames of one match from its viewer document (`MatchData.document()`, with the
    seats' actions and calls merged in). The terrain layer is drawn once; tile owners and what the seats know
    are carried from frame to frame, so frames in turn order are cheapest."""

    def __init__(self, doc: dict, *, view: str = "spectator", baselines: dict[str, dict] | None = None):
        if not doc.get("turns"):
            raise ValueError("no turns to render")
        self.doc, self.view, self.meta = doc, view, doc["meta"]
        self.turns: list[dict] = doc["turns"]
        self.tiles: list[list] = doc["static"]["tiles"]
        self.players = {p["index"]: p for p in doc["players"]}
        self.civs = [p for p in doc["players"] if not p["barbarian"]]
        self.color = {i: rgb(p["color"]) for i, p in self.players.items()}
        self.name = {i: p.get("label") or p["civ"] for i, p in self.players.items()}
        self.limit = max(self.meta.get("turn_limit") or 0, self.turns[-1]["turn"], 1)
        self.baselines = {p: {int(t): s for t, s in sc.items()} for p, sc in (baselines or {}).items() if sc}
        types, civilian = self.meta.get("unit_types") or [], set(self.meta.get("civilian") or ())
        self.military = [t not in civilian for t in types]
        self._series()
        self.has_agents = any(t.get("actions") or t.get("calls") for t in self.turns)
        self.has_war = any("at_war" in s for t in self.turns for s in t.get("stats", {}).values())
        mp = self.meta["map"]
        self.mw, self.mh, self.wrap = mp["width"], mp["height"], bool(mp.get("wrap_x", True))
        seam = self.meta.get("seam", 0) if self.wrap else 0
        self.seam = seam - seam % 2        # shift by an even amount: keeps x + y parity
        self.at = {(t[0], t[1]): i for i, t in enumerate(self.tiles)}
        self._layout()
        self.base = self._terrain_layer()
        self.rivers = self._river_segments()
        self.chrome = self._chrome()
        self._tint: dict[tuple[int, int], tuple] = {}
        self._reset()

    # -- data --

    def _series(self) -> None:
        last: dict[int, list] = {}
        self.score: list[dict[int, list]] = []
        for t in self.turns:
            for p in self.civs:
                if (s := t["scores"].get(str(p["index"]))) is not None:
                    last[p["index"]] = s
            self.score.append({p["index"]: last.get(p["index"], [0, 0, 0, 0, 0, 0]) for p in self.civs})
        self.series = {p["index"]: [s[p["index"]][0] for s in self.score] for p in self.civs}
        self.ranks = []
        for s in self.score:
            order = sorted(self.civs, key=lambda p: (s[p["index"]][5], -s[p["index"]][0], p["index"]))
            self.ranks.append({p["index"]: r + 1 for r, p in enumerate(order)})
        self.lead_changes = [(t["turn"], e["owner"]) for t in self.turns for e in t["events"]
                             if e.get("kind") == "lead_change"]
        # The score leader of each turn, or None while the top score is tied.
        self.top: list[int | None] = []
        for s in self.score:
            best = max((v[0] for v in s.values()), default=0)
            at = [i for i, v in s.items() if v[0] == best]
            self.top.append(at[0] if len(at) == 1 else None)
        self.turn_index = {t["turn"]: k for k, t in enumerate(self.turns)}
        latest: dict[int, tuple[int, dict]] = {}
        self.latest: list[dict[int, tuple[int, dict]]] = []
        for t in self.turns:
            for k, acts in (t.get("actions") or {}).items():
                if acts:
                    latest[int(k)] = (t["turn"] - 1, acts[-1])
            self.latest.append(dict(latest))

    def _reset(self) -> None:
        self._ti = -1
        self.own = [-1] * len(self.tiles)
        self.known = [0] * len(self.tiles)
        self.fog = Image.new("L", self.base.size, 255) if self.view == "agent" else None

    def _advance(self, ti: int) -> None:
        """Carry tile owners (and, in the agent view, the fog) forward to turn index `ti`."""
        if ti < self._ti:
            self._reset()
        fog = ImageDraw.Draw(self.fog) if self.fog is not None else None
        for t in self.turns[self._ti + 1: ti + 1]:
            for k, o in t["owners"]:
                self.own[k] = o
            for k, mask in t["known"]:
                self.known[k] = mask
                if fog is not None and (c := self.centre[k]) is not None:
                    fog.polygon(self.diamond(c, 1.04), fill=0 if mask else 255)
        self._ti = ti

    # -- geometry --

    def rot(self, x: int) -> int:
        return (x - self.seam) % self.mw if self.wrap else x

    def _layout(self) -> None:
        terrain = self.meta["terrain"]
        land = [(self.rot(t[0]), t[1]) for t in self.tiles if terrain[t[2]] not in matchdata.WATER] or \
            [(self.rot(t[0]), t[1]) for t in self.tiles]
        margin = 2
        self.x_lo, self.x_hi = min(x for x, _ in land) - margin, max(x for x, _ in land) + margin
        self.y_lo, self.y_hi = min(y for _, y in land) - margin, max(y for _, y in land) + margin
        wu = self.x_hi - self.x_lo + 2                  # tile centres span x_lo..x_hi, plus half a diamond each side
        hu = (self.y_hi - self.y_lo + 2) / 2
        box_w = PANEL_X - 2 * PAD
        box_h = H - HEADER_H - GAP - GAP - CHART_MIN_H - PAD
        self.hw = min(box_w / wu, box_h / hu, MAX_HW)
        self.map_w, self.map_h = round(wu * self.hw), round(hu * self.hw)
        self.map_x = PAD + (box_w - self.map_w) // 2
        self.map_y = HEADER_H + GAP
        self.chart_box = (PAD, self.map_y + self.map_h + GAP, PANEL_X - PAD, H - PAD)
        self.centre: list[tuple[float, float] | None] = []
        h2 = self.hw * SS
        for t in self.tiles:
            x, y = self.rot(t[0]), t[1]
            if self.wrap and x < self.x_lo - 1:
                x += self.mw
            elif self.wrap and x > self.x_hi + 1 + self.mw // 2:
                x -= self.mw
            if self.x_lo - 1 <= x <= self.x_hi + 1 and self.y_lo - 1 <= y <= self.y_hi + 1:
                self.centre.append((h2 * (x - self.x_lo + 1), h2 / 2 * (y - self.y_lo + 1)))
            else:
                self.centre.append(None)

    def diamond(self, c: tuple[float, float], s: float = 1.0) -> list[tuple[float, float]]:
        cx, cy = c
        w, h = self.hw * SS * s, self.hw * SS / 2 * s
        return [(cx, cy - h), (cx + w, cy), (cx, cy + h), (cx - w, cy)]

    def neighbour(self, x: int, y: int, dx: int, dy: int) -> int | None:
        nx = (x + dx) % self.mw if self.wrap else x + dx
        return self.at.get((nx, y + dy))

    @property
    def map_box(self) -> tuple[int, int, int, int]:
        """The map and the score chart: the live view's frame."""
        return 0, HEADER_H, PANEL_X, H

    # -- static layers --

    def _terrain_layer(self) -> Image.Image:
        names = self.meta["terrain"]
        img = Image.new("RGB", (self.map_w * SS, self.map_h * SS), TERRAIN["ocean"])
        d = ImageDraw.Draw(img)
        self.tcol = []
        for t, c in zip(self.tiles, self.centre, strict=True):
            base = names[t[2]]
            key = names[t[3]] if t[3] is not None and t[3] >= 0 else base
            col = blend(TERRAIN.get(key, (90, 90, 90)), (0, 0, 0), ((t[0] * 7 + t[1] * 13) % 5) * 0.018)
            self.tcol.append((base in matchdata.WATER, col))
            if c is not None:
                d.polygon(self.diamond(c), fill=col, outline=col)
        return img

    def _river_segments(self) -> list[tuple]:
        """River tiles only carry a flag: join them with a spanning forest, so adjacent river tiles read as a
        branching line rather than a lattice of little diamonds; then cut the corners of the zigzags."""
        parent: dict[int, int] = {}

        def root(i: int) -> int:
            while parent.setdefault(i, i) != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        segs = []
        for i, t in enumerate(self.tiles):
            if not t[4] or self.centre[i] is None:
                continue
            for dx, dy in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
                j = self.neighbour(t[0], t[1], dx, dy)
                if j is None or not self.tiles[j][4] or self.centre[j] is None \
                        or abs(self.centre[j][0] - self.centre[i][0]) > self.hw * SS * 1.5:
                    continue
                a, b = root(i), root(j)
                if a != b:
                    parent[a] = b
                    segs.append((i, j))
        mids: dict[int, list] = {}
        for i, j in segs:
            (ax, ay), (bx, by) = self.centre[i], self.centre[j]
            mid = ((ax + bx) / 2, (ay + by) / 2)
            mids.setdefault(i, []).append(mid)
            mids.setdefault(j, []).append(mid)
        lines = []
        for i, ms in mids.items():
            if len(ms) == 2:
                lines.append((i, ms[0], ms[1]))
            else:
                lines += [(i, self.centre[i], q) for q in ms]
        return lines

    def _chrome(self) -> Image.Image:
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, HEADER_H - 1], fill=PANEL)
        d.rectangle([PANEL_X, HEADER_H, W, H], fill=PANEL)
        d.line([PANEL_X, HEADER_H, PANEL_X, H], fill=GRID)
        return img

    # -- the map --

    def tint(self, i: int, o: int) -> tuple:
        k = (i, o)
        if k not in self._tint:
            water, col = self.tcol[i]
            self._tint[k] = blend(col, self.color.get(o, MUTED), TINT_WATER if water else TINT_LAND)
        return self._tint[k]

    def visible(self, x: int, y: int) -> bool:
        if self.view != "agent":
            return True
        i = self.at.get((x, y))
        return i is not None and self.known[i] != 0

    def draw_map(self, frame: Image.Image, ti: int) -> None:
        own, hw2 = self.own, self.hw * SS
        img = self.base.copy()
        d = ImageDraw.Draw(img)
        owned = [(i, o) for i, o in enumerate(own) if o >= 0 and self.centre[i] is not None]
        for i, o in owned:
            col = self.tint(i, o)
            d.polygon(self.diamond(self.centre[i]), fill=col, outline=col)
        rw = max(SS, round(hw2 * 0.06))
        for _i, a, b in self.rivers:
            d.line([a, b], fill=RIVER, width=rw)
        # borders: each owner draws its own side of every edge it shares with someone else, inset so two civs'
        # borders sit side by side
        bw = max(SS * 2, round(min(2.5, self.hw / 4) * SS))
        k = (bw / 2) / (0.447 * hw2)
        edges = ((1, -1, 0, 1), (1, 1, 1, 2), (-1, 1, 2, 3), (-1, -1, 3, 0))
        for i, o in owned:
            t, c, pts = self.tiles[i], self.centre[i], None
            for dx, dy, a, b in edges:
                j = self.neighbour(t[0], t[1], dx, dy)
                if j is not None and own[j] == o:
                    continue
                pts = pts or self.diamond(c)
                (ax, ay), (bx, by) = pts[a], pts[b]
                mx, my = (ax + bx) / 2, (ay + by) / 2
                ox, oy = (c[0] - mx) * k, (c[1] - my) * k
                ex, ey = (bx - ax) * 0.04, (by - ay) * 0.04      # a touch past the vertices: no notches
                d.line([(ax + ox - ex, ay + oy - ey), (bx + ox + ex, by + oy + ey)], fill=self.color.get(o, MUTED),
                       width=bw)
        if self.fog is not None:
            img.paste(FOG, mask=self.fog)
        turn = self.turns[ti]
        towns = {(c[0], c[1]) for c in turn["cities"]}
        ur = max(1.5, min(2.3, self.hw / 3)) * SS
        for _id, x, y, o, typ in turn["units"]:
            if (x, y) in towns or not (typ < len(self.military) and self.military[typ]) or not self.visible(x, y):
                continue
            i = self.at.get((x, y))
            if i is None or (c := self.centre[i]) is None:
                continue
            d.ellipse([c[0] - ur, c[1] - ur, c[0] + ur, c[1] + ur], fill=self.color.get(o, MUTED), outline=DARK,
                      width=SS)
        # captures and razings of the last few turns: a red ring, and the label goes first
        self.marked: dict[tuple[int, int], float] = {}
        for back in self.turns[max(0, ti - MARK_TURNS + 1): ti + 1]:
            for e in back["events"]:
                if e.get("kind") in ("city_captured", "city_destroyed") and "x" in e and self.visible(e["x"], e["y"]):
                    i = self.at.get((e["x"], e["y"]))
                    if i is not None and (c := self.centre[i]) is not None:
                        r = self.city_r(8) + 6
                        R = r * SS
                        d.ellipse([c[0] - R, c[1] - R, c[0] + R, c[1] + R], outline=ALERT, width=3 * SS)
                        self.marked[(e["x"], e["y"])] = r + 2
        self.cities = []
        for x, y, name, o, size, cap, *_ in sorted(turn["cities"], key=lambda c: c[4]):
            i = self.at.get((x, y))
            if i is None or (c := self.centre[i]) is None or not self.visible(x, y):
                continue
            r = self.city_r(size)
            R, ring = r * SS, 2 * SS
            d.ellipse([c[0] - R - ring, c[1] - R - ring, c[0] + R + ring, c[1] + R + ring], fill=DARK)
            d.ellipse([c[0] - R, c[1] - R, c[0] + R, c[1] + R], fill=self.color.get(o, MUTED))
            if cap:
                d.polygon(star(c[0], c[1] + R * 0.05, R * 0.78), fill=(255, 255, 255))
            self.cities.append((self.map_x + c[0] / SS, self.map_y + c[1] / SS, self.marked.get((x, y), r + 2), name,
                                o, size, cap, (x, y) in self.marked))
        frame.paste(img.reduce(SS), (self.map_x, self.map_y))
        fd = ImageDraw.Draw(frame)
        fd.rectangle([self.map_x - 1, self.map_y - 1, self.map_x + self.map_w, self.map_y + self.map_h], outline=GRID)
        self.draw_labels(fd)

    def city_r(self, size: int) -> float:
        return min(2.4 + 1.45 * math.sqrt(max(1, size)), self.hw * 0.9 + 2.4)

    def draw_labels(self, d: ImageDraw.ImageDraw) -> None:
        """Capitals first, then the largest cities; a label that would cover another, or a city, is left out."""
        cap_f, cap_sub, city_f = font(15, bold=True), font(12), font(12)
        sizes = sorted((c[5] for c in self.cities if not c[6]), reverse=True)
        threshold = max(3, min(6, sizes[min(len(sizes), 30) - 1] if sizes else 6))
        x_lo, y_lo = self.map_x + 3, self.map_y + 3
        x_hi, y_hi = self.map_x + self.map_w - 3, self.map_y + self.map_h - 3
        discs = [(cx - r, cy - r, cx + r, cy + r) for cx, cy, r, *_ in self.cities]
        placed: list[tuple[float, float, float, float]] = []

        def free(box, own_disc: int, discs_too: bool) -> bool:
            x0, t, r, b = box
            if x0 < x_lo or t < y_lo or r > x_hi or b > y_hi:
                return False
            for (a, bb, c, dd) in placed:
                if x0 < c + 3 and r > a - 3 and t < dd + 1 and b > bb - 1:
                    return False
            for k, (a, bb, c, dd) in enumerate(discs if discs_too else ()):
                if k != own_disc and x0 < c and r > a and t < dd and b > bb:
                    return False
            return True

        cands = [(k, c) for k, c in enumerate(self.cities) if c[6] or c[7] or c[5] >= threshold]
        cands.sort(key=lambda kc: (not kc[1][7], not kc[1][6], -kc[1][5], kc[1][3]))
        for k, (cx, cy, r, name, o, _size, cap, marked) in cands:
            main_f = cap_f if cap or marked else city_f
            owner = self.name.get(o, "")
            sub = f" {owner}" if (cap or marked) and owner and owner != name else ""
            w1 = main_f.getlength(txt(name))
            w2 = cap_sub.getlength(txt(sub)) if sub else 0
            w, h = w1 + w2, (15 if cap or marked else 12)
            q = r * 0.7
            spots = [(cx - w / 2, cy - r - 3 - h), (cx - w / 2, cy + r + 3), (cx + r + 5, cy - h / 2 - 1),
                     (cx - r - 5 - w, cy - h / 2 - 1), (cx + q + 3, cy - q - 3 - h), (cx - q - 3 - w, cy - q - 3 - h),
                     (cx + q + 3, cy + q + 3), (cx - q - 3 - w, cy + q + 3)]
            tries = [(lx, ly, True) for lx, ly in spots] + (
                [(lx, ly, False) for lx, ly in spots] if cap or marked else [])
            for lx, ly, strict in tries:
                box = (lx - 1, ly - 1, lx + w + 1, ly + h + 3)
                if free(box, k, strict):
                    placed.append(box)
                    d.text((lx, ly + h), txt(name), font=main_f, fill=ALERT if marked else TEXT if cap else SOFT,
                           anchor="ls", stroke_width=3, stroke_fill=DARK)
                    if sub:
                        d.text((lx + w1, ly + h), txt(sub), font=cap_sub, fill=MUTED, anchor="ls",
                               stroke_width=2, stroke_fill=DARK)
                    break

    # -- the header --

    def draw_header(self, d: ImageDraw.ImageDraw, ti: int) -> None:
        turn = self.turns[ti]["turn"]
        right_x = W - PAD - 4
        big, small, lab, tl = font(34, bold=True), font(20), font(16), font(15, bold=True)
        limit = f" / {self.limit}"
        tw = small.getlength(limit)
        d.text((right_x, 44), limit, font=small, fill=MUTED, anchor="rs")
        d.text((right_x - tw, 44), str(turn), font=big, fill=TEXT, anchor="rs")
        nw = big.getlength(str(turn))
        d.text((right_x - tw - nw - 10, 44), "TURN", font=tl, fill=MUTED, anchor="rs")
        turn_left = right_x - tw - nw - 10 - tl.getlength("TURN")
        seats = [p for p in self.civs if p.get("seat") is not None]
        ai = len(self.civs) - len(seats)
        if len(seats) == 1:
            p = seats[0]
            who = f"{p['label']} ({p['civ']})" if p.get("label") else p["civ"]
        else:
            who = f"{len(seats)} agents" if seats else f"{len(self.civs)} civs"
        if seats and ai:
            who += f" vs {ai} AI civ{'s' * (ai != 1)}"
        rest = f"  ·  {who}  ·  seed {self.meta.get('seed', '?')}"
        if self.view == "agent":
            rest += "  ·  what the agents have explored"
        tf = font(24, bold=True)
        x = PAD + 4
        d.text((x, 42), "OpenCiv3", font=tf, fill=TEXT, anchor="ls")
        x += tf.getlength("OpenCiv3")
        v = self.meta.get("victory")
        banner = ""
        if v and v.get("turn") is not None and v["turn"] <= turn:
            i = next((p["index"] for p in self.civs if p["civ"] == v.get("civ")), None)
            banner = f"{self.name.get(i, v.get('civ', '?'))} wins by {v.get('kind', 'victory')} on turn {v['turn']}"
        room = turn_left - 40 - x
        if banner:
            bf = font(17, bold=True)
            bw = min(bf.getlength(txt(banner)), room * 0.6)
            d.text((turn_left - 40, 42), fit(banner, bf, bw), font=bf, fill=(240, 200, 96), anchor="rs")
            room -= bw + 24
        d.text((x, 42), fit(rest, lab, max(0, room)), font=lab, fill=MUTED, anchor="ls")
        y = HEADER_H - 3
        d.rectangle([0, y, W, HEADER_H - 1], fill=TRACK)
        d.rectangle([0, y, round(W * min(1, turn / self.limit)), HEADER_H - 1], fill=SOFT)

    # -- the panel --

    def draw_panel(self, frame: Image.Image, ti: int) -> None:
        d = ImageDraw.Draw(frame)
        x0, x1 = PANEL_X + 22, W - PAD - 4
        y = HEADER_H + 20
        y = self.draw_standings(frame, d, ti, x0, x1, y)
        y = self.draw_territory(d, x0, x1, y + 26)
        self.draw_moments(d, ti, x0, x1, y + 28)

    def draw_standings(self, frame: Image.Image, d: ImageDraw.ImageDraw, ti: int, x0: int, x1: int, y: int) -> int:
        cols = dict(rank=x0 + 14, delta=x0 + 22, chip=x0 + 58, name=x0 + 76, bar0=x0 + 236, bar1=x0 + 360,
                    score=x0 + 412, cities=x0 + 450, pop=x0 + 492, techs=x0 + 534, spark=x1 - 60)
        hf = font(11, bold=True)
        d.text((x0, y), "STANDINGS", font=font(13, bold=True), fill=MUTED)
        for key, label in (("score", "SCORE"), ("cities", "CTY"), ("pop", "POP"), ("techs", "TECH")):
            d.text((cols[key], y + 2), label, font=hf, fill=MUTED, anchor="ra")
        d.text((x1, y + 2), "LAST 40", font=hf, fill=MUTED, anchor="ra")
        y += 26
        two = self.has_agents or self.has_war
        ranks, prev = self.ranks[ti], self.ranks[ti - 10] if ti >= 10 else None
        order = sorted(self.civs, key=lambda p: ranks[p["index"]])
        score = self.score[ti]
        lead_score = max([1] + [score[p["index"]][0] for p in order])
        n = len(order)
        row_h = min(46 if two else 40, max(30 if two else 24, (H - 470 - y) // max(1, n)))
        top = y
        mixed = 0 < sum(q.get("seat") is not None for q in order) < n
        for p in order:
            i = p["index"]
            total, cities, pop, _tiles, techs, dead = score[i]
            col, ink = self.color[i], MUTED if dead else TEXT
            cy = y + (17 if two else row_h // 2)
            d.line([x0, y, x1, y], fill=GRID)
            first = ranks[i] == 1
            if first:
                d.rectangle([x0 - 10, y + 1, x1 + 6, y + row_h - 1], fill=HILITE)
            d.text((cols["rank"], cy), str(ranks[i]), font=font(16, bold=True), fill=ink, anchor="rm")
            if prev is None or prev[i] == ranks[i]:
                d.text((cols["delta"] + 8, cy), txt("–"), font=font(12), fill=MUTED, anchor="mm")
            else:
                up = prev[i] > ranks[i]
                d.text((cols["delta"], cy), txt(f"{'▲' if up else '▼'}{abs(prev[i] - ranks[i])}"), font=font(12),
                       fill=UP if up else DOWN, anchor="lm")
            d.rounded_rectangle([cols["chip"], cy - 6, cols["chip"] + 12, cy + 6], radius=3, fill=col)
            nf, cf = font(16, bold=True), font(12)
            room = cols["bar0"] - 12 - cols["name"]
            label = fit(self.name[i], nf, room)
            d.text((cols["name"], cy + 6), label, font=nf, fill=ink, anchor="ls")
            lw = nf.getlength(label) + 6
            civ = p["civ"] if p.get("label") else "agent" if mixed and p.get("seat") is not None else ""
            civ = f"{civ} · out" if civ and dead else "out" if dead else civ
            if civ and room - lw > 20:
                d.text((cols["name"] + lw, cy + 6), fit(civ, cf, room - lw), font=cf, fill=MUTED, anchor="ls")
            b0, b1 = cols["bar0"], cols["bar1"]
            d.rectangle([b0, cy - 5, b1, cy + 5], fill=TRACK)
            bl = (b1 - b0) * total / lead_score
            if bl >= 1:
                d.rounded_rectangle([b0, cy - 5, b0 + max(bl, 9), cy + 5], radius=4, fill=col,
                                    corners=(False, True, True, False))
            d.text((cols["score"], cy), str(total), font=font(16, bold=True), fill=ink, anchor="rm")
            sf = font(13)
            for key, v in (("cities", cities), ("pop", pop), ("techs", techs)):
                d.text((cols[key], cy), str(v), font=sf, fill=MUTED, anchor="rm")
            self.sparkline(frame, cols["spark"], cy - 8, self.series[i][max(0, ti - 39): ti + 1], col,
                           HILITE if first else PANEL)
            if two:
                self.agent_line(d, ti, p, cols["name"], x1, y + row_h - 9)
            y += row_h
        d.line([x0, y, x1, y], fill=GRID)
        if self.has_agents and ti > 0:
            hx = x0 + font(13, bold=True).getlength("STANDINGS") + 10
            d.text((hx, top - 26 + 1), fit(f"·  agents' calls and last action in T{self.turns[ti]['turn'] - 1}",
                                           font(12), cols["score"] - 60 - hx), font=font(12), fill=MUTED)
        return y

    def agent_line(self, d: ImageDraw.ImageDraw, ti: int, p: dict, x: int, x1: int, base: int) -> None:
        """One small line under a civ's row: an agent's tool calls and latest game action of the turn just played
        (an AI civ's government instead), and who the civ is at war with."""
        i, t = p["index"], self.turns[ti]
        f, fb = font(12), font(12, bold=True)
        stats = (t.get("stats") or {}).get(str(i), {})
        wars = [self.name[j] for j in stats.get("at_war") or () if j in self.players and j != i
                and not self.players[j]["barbarian"]]
        right = x1
        if wars:
            text = fit("at war: " + ", ".join(wars), f, (x1 - x) * 0.45)
            d.text((x1, base), text, font=f, fill=DOWN, anchor="rs")
            right = x1 - f.getlength(text) - 14
        parts: list[tuple[str, tuple, ImageFont.FreeTypeFont]] = []
        if p.get("seat") is not None and self.has_agents:
            calls = (t.get("calls") or {}).get(str(i))
            acts = (t.get("actions") or {}).get(str(i)) or []
            if calls:
                parts.append((f"{calls['ok'] + calls['failed']} calls", SOFT, f))
                if calls["failed"]:
                    parts.append((f" · {calls['failed']} failed", DOWN, f))
            elif ti > 0:
                parts.append(("no calls", MUTED, f))
            if acts:
                last = acts[-1]
                parts.append(("  " if parts else "", MUTED, f))
                parts.append((last["text"], SOFT if last.get("ok", True) else DOWN, fb if last.get("ok", True) else f))
                if len(acts) > 1:
                    parts.append((f"  +{len(acts) - 1}", MUTED, f))
            elif (seen := self.latest[ti].get(i)) is not None:
                parts.append((f"  last T{seen[0]}: ", MUTED, f))
                parts.append((seen[1]["text"], MUTED, f))
        elif p.get("seat") is None:
            gov = stats.get("government")
            parts.append(("AI" + (f" · {gov}" if gov else ""), MUTED, f))
        for text, ink, ff in parts:
            room = right - x
            if room < 12:
                break
            s = fit(text, ff, room)
            d.text((x, base), s, font=ff, fill=ink, anchor="ls")
            x += ff.getlength(s)

    def sparkline(self, frame: Image.Image, x: int, y: int, values: list[int], col, bg) -> None:
        S, w, h = 4, 60, 16
        img = Image.new("RGB", (w * S, h * S), bg)
        d = ImageDraw.Draw(img)
        if len(values) >= 2:
            lo, hi = min(values), max(values)
            if hi - lo < 4:
                lo, hi = lo - 2, hi + 2
            pts = [(1.5 * S + (w - 3) * S * k / (len(values) - 1), (h - 2) * S - (h - 4) * S * (v - lo) / (hi - lo))
                   for k, v in enumerate(values)]
            d.line(pts, fill=col, width=int(1.5 * S), joint="curve")
            ex, ey = pts[-1]
            d.ellipse([ex - 2 * S, ey - 2 * S, ex + 2 * S, ey + 2 * S], fill=col)
        frame.paste(img.reduce(S), (x, y))

    def draw_territory(self, d: ImageDraw.ImageDraw, x0: int, x1: int, y: int) -> int:
        counts: dict[int, int] = {}
        for i, o in enumerate(self.own):
            if o >= 0 and o in self.players and not self.players[o]["barbarian"] and \
                    (self.view != "agent" or self.known[i]):
                counts[o] = counts.get(o, 0) + 1
        total = sum(counts.values())
        d.text((x0, y), "TERRITORY", font=font(13, bold=True), fill=MUTED)
        if not total:
            d.text((x0, y + 24), "no claimed tiles yet", font=font(13), fill=MUTED)
            return y + 24 + 16
        lead, n = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))
        share = f"{100 * n / total:.0f}%"
        sf, bf = font(13), font(13, bold=True)
        tail = " of claimed tiles"
        d.text((x1, y + 1), tail, font=sf, fill=MUTED, anchor="ra")
        xx = x1 - sf.getlength(tail)
        d.text((xx, y + 1), share, font=bf, fill=TEXT, anchor="ra")
        xx -= bf.getlength(share) + 5
        d.text((xx, y + 1), fit(f"{self.name[lead]} holds", sf, xx - x0 - 100), font=sf, fill=MUTED, anchor="ra")
        by = y + 24
        segs = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        span = x1 - x0 - 2 * (len(segs) - 1)
        acc, xs = 0, x0
        for k, (o, c) in enumerate(segs):
            acc += c
            xe = x0 + round(span * acc / total) + 2 * k
            if xe - 1 >= xs:
                d.rectangle([xs, by, xe - 1, by + 15], fill=self.color[o])
            xs = xe + 2
        return by + 16

    def draw_moments(self, d: ImageDraw.ImageDraw, ti: int, x0: int, x1: int, y: int) -> None:
        d.text((x0, y), "KEY MOMENTS", font=font(13, bold=True), fill=MUTED)
        y += 26
        line_h = 36
        n = max(1, min(MAX_MOMENTS, (H - PAD - 6 - y) // line_h))
        now = self.turns[ti]["turn"]
        tf = font(10, bold=True)
        moments = self.moments(ti, n)
        if not moments:
            d.text((x0, y + 8), "nothing notable yet", font=font(13), fill=MUTED)
        for turn, e in moments:
            cy = y + line_h // 2
            kind = e["kind"]
            strong = kind in STRONG
            if strong:
                d.rectangle([x0 - 10, y + 1, x1 + 6, y + line_h - 1], fill=ALERT_BG)
            d.line([x0, y, x1, y], fill=GRID)
            d.text((x0, cy), f"T{turn}", font=font(13), fill=MUTED, anchor="lm")
            d.rounded_rectangle([x0 + 50, cy - 6, x0 + 62, cy + 6], radius=3, fill=self.color.get(e["owner"], MUTED))
            tag = TAG.get(kind, kind.upper())
            tw = tf.getlength(tag)
            d.rounded_rectangle([x1 - tw - 12, cy - 9, x1, cy + 9], radius=4, outline=DOWN if strong else GRID,
                                width=1, fill=(72, 36, 40) if strong else PANEL)
            d.text((x1 - 6, cy), tag, font=tf, fill=(255, 186, 186) if strong else UP if kind == "peace_signed"
                   else MUTED, anchor="rm")
            f = font(15, bold=strong or kind == "lead_change")
            ink = TEXT if (turn >= now - 2 or strong or kind == "lead_change") else SOFT
            d.text((x0 + 76, cy), fit(e.get("text") or kind, f, x1 - tw - 24 - x0 - 76), font=f, fill=ink,
                   anchor="lm")
            y += line_h

    def durable(self, turn: int, owner: int, ti: int) -> bool:
        """Whether a lead change counts at turn index `ti`: the new leader has held the lead every turn since, for up to
        5 turns. In a close race the lead flips straight back again and again. Nothing after `ti` is used."""
        k0 = self.turn_index.get(turn)
        return k0 is None or all(self.top[k] == owner for k in range(k0, min(k0 + 4, ti) + 1))

    def moments(self, ti: int, n: int) -> list[tuple[int, dict]]:
        now = self.turns[ti]["turn"]
        pool = [(t["turn"], e) for t in self.turns[: ti + 1] if t["turn"] > now - MOMENT_WINDOW
                for e in t["events"] if e.get("kind") in PRIORITY
                and (e["kind"] != "lead_change" or self.durable(t["turn"], e["owner"], ti))]
        pool.sort(key=lambda pe: (-(pe[0] - HANDICAP[pe[1]["kind"]]), PRIORITY[pe[1]["kind"]]))
        return sorted(pool[:n], key=lambda pe: (-pe[0], PRIORITY[pe[1]["kind"]]))

    # -- the chart --

    def draw_chart(self, frame: Image.Image, ti: int) -> None:
        bx0, by0, bx1, by1 = self.chart_box
        bw, bh = bx1 - bx0, by1 - by0
        left, right, top, bottom = 54, bw - 136, 34, bh - 30
        now = self.turns[ti]["turn"]
        base = {p: [(t, s["total"]) for t, s in sorted(sc.items()) if t <= now] for p, sc in self.baselines.items()}
        # The y scale follows the turns played so far, so early turns aren't squashed against the axis.
        seen = max([v for vals in self.series.values() for v in vals[: ti + 1]]
                   + [v for pts in base.values() for _, v in pts] + [10])
        step = nice_step(seen * 1.06)
        ytop = max(seen * 1.06, step)
        S = SS
        img = Image.new("RGB", (bw * S, bh * S), BG)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, bw * S - 1, bh * S - 1], radius=6 * S, fill=PANEL)

        # Both scales follow the turns played so far: early on, the game isn't a sliver at the left edge.
        span = max(now, min(self.limit, 10))

        def X(turn: float) -> float:
            return left + (right - left) * turn / span

        def Y(v: float) -> float:
            return bottom - (bottom - top) * v / ytop

        for v in range(0, int(ytop) + 1, step):
            d.line([(left * S, Y(v) * S), (right * S, Y(v) * S)], fill=GRID if v else (58, 63, 72), width=S)
        xstep = next(s for s in (5, 10, 25, 50, 100, 250, 500, 1000, 10 ** 9) if span / s <= 8)
        ticks = list(range(0, span + 1, xstep))
        if ticks[-1] != span and X(span) - X(ticks[-1]) > 36:
            ticks.append(span)
        for t in ticks:
            d.line([(X(t) * S, bottom * S), (X(t) * S, (bottom + 4) * S)], fill=(58, 63, 72), width=S)
        d.line([(X(now) * S, (top - 10) * S), (X(now) * S, bottom * S)], fill=(84, 90, 100), width=S)
        ends: list[tuple[float, float, str, str, tuple, bool]] = []
        for policy, pts in base.items():
            xy = [(X(t) * S, Y(v) * S) for t, v in pts]
            for a, b in zip(xy[::2], xy[1::2], strict=False):
                d.line([a, b], fill=MUTED, width=S)
            if xy:
                ends.append((xy[-1][1] / S, xy[-1][0] / S, BASELINE_LABELS.get(policy, policy), str(pts[-1][1]),
                             MUTED, False))
        ranks = self.ranks[ti]
        leader = min(self.civs, key=lambda p: ranks[p["index"]])["index"] if self.civs else None
        for p in sorted(self.civs, key=lambda p: (p["index"] == leader, -ranks[p["index"]])):
            i = p["index"]
            vals = self.series[i][: ti + 1]
            pts = [(X(self.turns[k]["turn"]) * S, Y(v) * S) for k, v in enumerate(vals)]
            if len(pts) > 1:
                d.line(pts, fill=self.color[i], width=4 if i == leader else 3, joint="curve")
            if pts:
                r = 3 * S
                ex, ey = pts[-1]
                d.ellipse([ex - r, ey - r, ex + r, ey + r], fill=self.color[i], outline=PANEL, width=S)
                ends.append((ey / S, ex / S, self.name[i], str(vals[-1]), self.color[i], i == leader))
        for t, o in self.lead_changes:
            if t <= now and self.durable(t, o, ti):
                cx, cy, r = X(t) * S, bottom * S, 5 * S
                d.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], fill=self.color.get(o, MUTED),
                          outline=PANEL, width=S)
        chart = img.reduce(S)
        cd = ImageDraw.Draw(chart)
        sf = font(11)
        cd.text((16, 12), "SCORE", font=font(13, bold=True), fill=MUTED)
        cd.text((bw - 12, 12), txt("◆ lead change"), font=sf, fill=MUTED, anchor="ra")
        for v in range(0, int(ytop) + 1, step):
            cd.text((left - 8, Y(v)), str(v), font=sf, fill=MUTED, anchor="rm")
        for t in ticks:
            cd.text((X(t), bottom + 8), f"T{t}", font=sf, fill=MUTED, anchor="mt")
        if 0 < now < self.limit:
            cf = font(11, bold=True)
            free_x = 16 + font(13, bold=True).getlength("SCORE") + 18 + cf.getlength(f"T{now}") / 2
            cd.text((max(X(now), free_x), top - 12), f"T{now}", font=cf, fill=SOFT, anchor="mb")
        ys = spread([e[0] for e in ends], 15, top, bottom)
        for (ey, ex, name, value, col, lead), ly in zip(ends, ys, strict=True):
            lx = ex + 10
            if abs(ly - ey) > 2:
                cd.line([(ex + 4, ey), (lx - 3, ly)], fill=col, width=1)
            nf = font(12, bold=lead)
            name = fit(name, nf, bw - 10 - lx - 5 - sf.getlength(value))
            cd.text((lx, ly), name, font=nf, fill=TEXT if col != MUTED else MUTED, anchor="lm")
            cd.text((lx + nf.getlength(name) + 5, ly), value, font=sf, fill=MUTED, anchor="lm")
        frame.paste(chart, (bx0, by0))

    # -- frames --

    def frame(self, ti: int) -> Image.Image:
        """The frame of turn index `ti` (an index into the document's turns)."""
        self._advance(ti)
        img = self.chrome.copy()
        self.draw_map(img, ti)
        self.draw_chart(img, ti)
        self.draw_panel(img, ti)
        self.draw_header(ImageDraw.Draw(img), ti)
        return img

    def frames(self) -> Iterator[tuple[dict, Image.Image]]:
        for ti, t in enumerate(self.turns):
            yield t, self.frame(ti)


# ---- outputs ----

def _png(img: Image.Image, colors: int | None = None) -> bytes:
    out = io.BytesIO()
    (img.quantize(colors=colors, method=Image.Quantize.FASTOCTREE) if colors else img).save(out, "PNG", optimize=True)
    return out.getvalue()


@cache
def _x264() -> bool:
    exe = shutil.which("ffmpeg")
    try:
        return bool(exe) and "libx264" in subprocess.run([exe, "-hide_banner", "-encoders"], capture_output=True,
                                                         text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


class Mp4:
    """Frames as fast PNGs in a temp dir, then ffmpeg (libx264, else mpeg4); the last frame is held for 2 s."""

    CODECS = (["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"], ["-c:v", "mpeg4", "-q:v", "4"])

    def __init__(self, fps: int):
        self.fps = fps
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.count = 0

    def add(self, img: Image.Image) -> None:
        img.save(self.dir / f"f{self.count:05d}.png", compress_level=1)
        self.count += 1

    def add_png(self, path: Path) -> None:
        shutil.copyfile(path, self.dir / f"f{self.count:05d}.png")
        self.count += 1

    def finish(self) -> bytes | None:
        try:
            exe, out = shutil.which("ffmpeg"), self.dir / "out.mp4"
            for codec in self.CODECS if exe else ():
                cmd = [exe, "-y", "-loglevel", "error", "-framerate", str(self.fps), "-i", str(self.dir / "f%05d.png"),
                       "-vf", "tpad=stop_mode=clone:stop_duration=2", *codec, "-pix_fmt", "yuv420p",
                       "-movflags", "+faststart", str(out)]
                if subprocess.run(cmd, capture_output=True, timeout=600).returncode == 0:
                    return out.read_bytes()
            return None
        finally:
            self.tmp.cleanup()


class Mp4Pipe:
    """Raw frames piped straight into ffmpeg (libx264, else mpeg4); the last frame is held for 2 s."""

    def __init__(self, fps: int, size: tuple[int, int] = (W, H)):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "out.mp4"
        codec = Mp4.CODECS[0] if _x264() else Mp4.CODECS[1]
        self.proc = subprocess.Popen(
            [shutil.which("ffmpeg") or "ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-s", f"{size[0]}x{size[1]}", "-framerate", str(fps), "-i", "-",
             "-vf", "tpad=stop_mode=clone:stop_duration=2", *codec, "-pix_fmt", "yuv420p", "-movflags", "+faststart",
             str(self.out)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.failed = False

    def add(self, img: Image.Image) -> None:
        if self.failed:
            return
        try:
            self.proc.stdin.write(img.tobytes())
        except (BrokenPipeError, OSError):
            self.failed = True

    def finish(self) -> bytes | None:
        try:
            try:
                self.proc.stdin.close()
            except OSError:
                self.failed = True
            ok = self.proc.wait(timeout=600) == 0 and not self.failed
            return self.out.read_bytes() if ok and self.out.exists() else None
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return None
        finally:
            self.tmp.cleanup()


def document(snapshots: Iterable[dict], *, seat_actions: matchdata.Actions | None = None,
             calls: matchdata.Calls | None = None, actions: dict | None = None,
             labels: dict[str, str] | None = None) -> dict:
    """The viewer's document for these snapshots, with the seats' actions and calls keyed by player index. Without
    `seat_actions`, the old merged `actions` timeline is split by its "name: " prefixes."""
    m = MatchData.from_snapshots(snapshots, labels=labels)
    per_seat = by_player(seat_actions, m.players) if seat_actions else split_timeline(actions, m.players)
    return m.document(actions=per_seat, calls=by_player(calls, m.players))


def map_png(snapshots: Iterable[dict], view: str = "spectator") -> bytes:
    """The map and score chart of the last snapshot, as a PNG (the live view's frame). The snapshots stream
    through MatchData, so a long game's are never all in memory."""
    r = Renderer(MatchData.from_snapshots(snapshots).document(), view=view)
    return _png(r.frame(len(r.turns) - 1).crop(r.map_box), colors=256)


def render(snapshots: list[dict], *, formats: Iterable[str] = ("mp4", "html"), view: str = "spectator",
           fps: int = 4, name: str = "openciv3", actions: dict | None = None,
           baselines: dict[str, dict] | None = None, seat_actions: matchdata.Actions | None = None,
           calls: matchdata.Calls | None = None, client_videos: dict[str, dict] | None = None,
           labels: dict[str, str] | None = None) -> tuple[list[File], list[str]]:
    """Render the requested formats; returns the files and notes (e.g. that a gif replaced the mp4).

    `seat_actions` and `calls` are per turn and per seat (player index, or the civ's name); without
    `seat_actions`, `actions` (the merged timeline, "civ: " prefixed with several seats) stands in.
    `client_videos` goes to the html viewer as is."""
    if not snapshots:
        raise ValueError("no snapshots to render")
    formats = list(dict.fromkeys(formats))
    doc = document(snapshots, seat_actions=seat_actions, calls=calls, actions=actions, labels=labels)
    files, notes = [], []
    drawn = [f for f in formats if f in ("mp4", "gif", "png")]
    if drawn:
        r = Renderer(doc, view=view, baselines=baselines)
        ffmpeg = bool(shutil.which("ffmpeg"))
        mp4 = Mp4Pipe(fps) if "mp4" in formats and ffmpeg else None
        gif_wanted = "gif" in formats or ("mp4" in formats and not ffmpeg)
        gif: list[Image.Image] = []
        last = None
        frames = r.frames() if mp4 or gif_wanted else [(r.turns[-1], r.frame(len(r.turns) - 1))]
        for _t, img in frames:
            last = img
            if mp4:
                mp4.add(img)
            if gif_wanted:
                gif.append(_gif_frame(img))
        if mp4 and (video := mp4.finish()) is not None:
            files.append(File(f"{name}.mp4", FORMATS["mp4"], video))
        elif "mp4" in formats:
            notes.append("ffmpeg is not on PATH (or failed), so the mp4 was replaced by an animated gif")
            if not gif_wanted:
                gif = [_gif_frame(img) for _t, img in r.frames()]
            gif_wanted = True
        if gif_wanted:
            out = io.BytesIO()
            durations = [1000 // fps] * (len(gif) - 1) + [2000]
            gif[0].save(out, "GIF", save_all=True, append_images=gif[1:], duration=durations, loop=0, optimize=False)
            files.append(File(f"{name}.gif", FORMATS["gif"], out.getvalue()))
        if "png" in formats and last is not None:
            files.append(File(f"{name}.png", FORMATS["png"], _png(last)))
    if "html" in formats:
        from .viewer import page

        files.append(File(f"{name}.html", FORMATS["html"], page(doc, client_videos).encode()))
    order = {f: k for k, f in enumerate(formats)}
    files.sort(key=lambda f: order.get(f.name.rsplit(".", 1)[-1], len(order)))
    return files, notes


def _gif_frame(img: Image.Image) -> Image.Image:
    return img.resize(GIF_SIZE, Image.Resampling.BOX).quantize(colors=128, method=Image.Quantize.FASTOCTREE)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Render a game recording from a directory of bridge snapshots.")
    ap.add_argument("record_dir", type=Path)
    ap.add_argument("--out", type=Path, default=Path("."))
    ap.add_argument("--formats", default="mp4,html", help="comma-separated: " + ", ".join(FORMATS))
    ap.add_argument("--view", choices=("spectator", "agent"), default="spectator")
    ap.add_argument("--fps", type=int, default=4)
    ap.add_argument("--actions", type=Path, action="append", default=[],
                    help="an env action log (OPENCIV_ACTION_LOG), for the agents' calls and actions; repeatable")
    ap.add_argument("--labels", default="", help="seat labels for recordings made without them: Rome=opus,...")
    ap.add_argument("--name", default="openciv3")
    args = ap.parse_args(argv)
    seat_actions: matchdata.Actions = {}
    calls: matchdata.Calls = {}
    for log in args.actions:
        acts, cs = matchdata.actions_from_log(log)
        for t, rows in acts.items():
            for k, v in rows.items():
                seat_actions.setdefault(t, {}).setdefault(k, []).extend(v)
        for t, rows in cs.items():
            for k, v in rows.items():
                c = calls.setdefault(t, {}).setdefault(k, {"ok": 0, "failed": 0})
                c["ok"] += v["ok"]
                c["failed"] += v["failed"]
    labels = dict(kv.split("=", 1) for kv in args.labels.split(",") if "=" in kv)
    files, notes = render(load_snapshots(args.record_dir), formats=args.formats.split(","), view=args.view,
                          fps=args.fps, name=args.name, seat_actions=seat_actions or None, calls=calls or None,
                          labels=labels or None)
    args.out.mkdir(parents=True, exist_ok=True)
    for f in files:
        (args.out / f.name).write_bytes(f.data)
        print(f"{args.out / f.name} ({len(f.data):,} bytes)")
    for note in notes:
        print(f"note: {note}")


if __name__ == "__main__":
    main()
