"""Variant A, "broadcast frame": one 1920x1080 frame per turn from match.json (see prepare.py), plus an mp4.

    python prototypes/viz/broadcast.py prototypes/viz/out/match.json --out prototypes/viz/out \
        [--turns 60,130,200] [--no-video]

Map left (seam-rotated, cropped to land, supersampled), standings / territory / key moments right, score chart below.
Pillow only, plus ffmpeg on PATH for the mp4.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from agentenv_openciv3.recording import fit, font, spread, txt  # noqa: E402

W, H = 1920, 1080
FPS, HOLD_S = 6, 2
SS = 2                      # supersampling for the map and the chart
PAD, GAP = 16, 12
HEADER_H = 64
PANEL_X = 1268              # the right panel starts here; the map gets PAD..PANEL_X-PAD (1236 px)
CHART_MIN_H = 260

BG, PANEL, TEXT, MUTED, GRID = (20, 22, 26), (27, 30, 36), (232, 233, 236), (139, 144, 154), (42, 46, 54)
SOFT = (196, 199, 206)      # secondary ink, between text and muted
DARK = (12, 13, 16)         # rings and label halos
UP, DOWN = (110, 184, 132), (216, 112, 112)
TRACK = (36, 40, 47)

TERRAIN = {
    "ocean": (21, 35, 56), "sea": (25, 42, 67), "coast": (37, 60, 88), "grassland": (74, 95, 66),
    "plains": (102, 100, 70), "desert": (132, 122, 94), "tundra": (104, 108, 106), "floodplain": (84, 103, 68),
    "hills": (96, 88, 68), "mountains": (112, 108, 104), "forest": (52, 77, 56), "jungle": (45, 79, 61),
    "marsh": (63, 81, 76), "volcano": (92, 64, 58),
}
WATER = {"ocean", "sea", "coast"}
RIVER = (64, 98, 132)
TINT_LAND, TINT_WATER = 0.35, 0.18

PRIORITY = {"civ_destroyed": 0, "city_captured": 1, "city_destroyed": 2, "lead_change": 3, "city_founded": 4,
            "tech_learned": 5}
# a moment's rank is its turn minus an age handicap by kind: an elimination stays on the board ~40 turns longer than
# a city founding; techs only fill the list when nothing else happened
HANDICAP = {"civ_destroyed": 0, "city_captured": 4, "city_destroyed": 6, "lead_change": 8, "city_founded": 20,
            "tech_learned": 70}
TAG = {"civ_destroyed": "OUT", "city_captured": "CAPTURED", "city_destroyed": "RAZED", "lead_change": "LEAD",
       "city_founded": "FOUNDED", "tech_learned": "TECH"}
MOMENT_WINDOW = 80
MAX_MOMENTS = 11


def rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def blend(a, b, t: float) -> tuple[int, int, int]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b, strict=False))


def nice_step(top: float, n: int = 5) -> int:
    for s in (10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000):
        if top / s <= n:
            return s
    return 10000


def star(cx: float, cy: float, r: float) -> list[tuple[float, float]]:
    pts = []
    for i in range(10):
        a = -math.pi / 2 + i * math.pi / 5
        rr = r if i % 2 == 0 else r * 0.45
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    return pts


def text_w(s: str, f) -> float:
    return f.getlength(txt(s))


# ---------------------------------------------------------------------------------------------------------------
class Match:
    """match.json, plus per-turn derived state: cumulative owners, score series, ranks."""

    def __init__(self, path: Path):
        d = json.loads(path.read_text())
        self.meta = d["meta"]
        self.players = {p["index"]: p for p in d["players"]}
        self.civs = [p for p in d["players"] if not p["barbarian"]]
        self.color = {i: rgb(p["color"]) for i, p in self.players.items()}
        self.name = {i: p["label"] or p["civ"] for i, p in self.players.items()}
        self.tiles = d["static"]["tiles"]
        self.turns = d["turns"]
        self.limit = self.meta.get("turn_limit") or self.turns[-1]["turn"] or 1
        self.index = {t["turn"]: i for i, t in enumerate(self.turns)}
        self.series = {p["index"]: [t["scores"].get(str(p["index"]), [0] * 6)[0] for t in self.turns]
                       for p in self.civs}
        self.top = max([v for s in self.series.values() for v in s] + [10])
        self.ranks = [self._ranks(i) for i in range(len(self.turns))]
        self.lead_changes = [(t["turn"], e["owner"]) for t in self.turns for e in t["events"]
                             if e.get("kind") == "lead_change"]

    def score(self, i: int, p: int) -> list[int]:
        return self.turns[i]["scores"].get(str(p), [0] * 6)

    def _ranks(self, i: int) -> dict[int, int]:
        order = sorted(self.civs, key=lambda p: (-self.score(i, p["index"])[0], p["index"]))
        return {p["index"]: r + 1 for r, p in enumerate(order)}

    def owners(self):
        """Yields (turn index, owner list) cumulatively; the list is reused, copy it to keep it."""
        own = [-1] * len(self.tiles)
        for i, t in enumerate(self.turns):
            for k, o in t["owners"]:
                own[k] = o
            yield i, own


# ---------------------------------------------------------------------------------------------------------------
class Broadcast:
    def __init__(self, m: Match):
        self.m = m
        mp = m.meta["map"]
        self.mw, self.mh, self.wrap = mp["width"], mp["height"], mp.get("wrap_x", True)
        seam = m.meta.get("seam", 0) if self.wrap else 0
        self.seam = seam - (seam % 2)        # shift by an even amount: keeps x+y parity
        self._layout()
        self.base = self._terrain_layer()
        self.rivers = self._river_segments()
        self.chrome = self._chrome()
        self._tint: dict[tuple[int, int], tuple] = {}
        self.ss_font = {}

    # -- geometry --

    def rot(self, x: int) -> int:
        return (x - self.seam) % self.mw if self.wrap else x

    def _layout(self) -> None:
        land = [(self.rot(t[0]), t[1]) for t in self.m.tiles if self.m.meta["terrain"][t[2]] not in WATER]
        margin = 2
        self.x_lo = min(x for x, _ in land) - margin
        self.x_hi = max(x for x, _ in land) + margin
        self.y_lo = min(y for _, y in land) - margin
        self.y_hi = max(y for _, y in land) + margin
        wu = self.x_hi - self.x_lo + 2                  # tile centres span x_lo..x_hi, plus a half diamond each side
        hu = (self.y_hi - self.y_lo + 2) / 2
        box_w = PANEL_X - 2 * PAD
        box_h = H - HEADER_H - GAP - GAP - CHART_MIN_H - PAD
        self.hw = min(box_w / wu, box_h / hu)
        self.map_w, self.map_h = round(wu * self.hw), round(hu * self.hw)
        self.map_x = PAD + (box_w - self.map_w) // 2
        self.map_y = HEADER_H + GAP
        self.chart_box = (PAD, self.map_y + self.map_h + GAP, PANEL_X - PAD, H - PAD)
        # pixel centres (supersampled) for every tile in view
        self.centre: list[tuple[float, float] | None] = []
        h2 = self.hw * SS
        for t in self.m.tiles:
            x, y = self.rot(t[0]), t[1]
            if self.wrap and x < self.x_lo - 1:
                x += self.mw
            elif self.wrap and x > self.x_hi + 1 + self.mw // 2:
                x -= self.mw
            if self.x_lo - 1 <= x <= self.x_hi + 1 and self.y_lo - 1 <= y <= self.y_hi + 1:
                self.centre.append((h2 * (x - self.x_lo + 1), h2 / 2 * (y - self.y_lo + 1)))
            else:
                self.centre.append(None)
        self.at = {(t[0], t[1]): i for i, t in enumerate(self.m.tiles)}

    def diamond(self, c: tuple[float, float], s: float = 1.0) -> list[tuple[float, float]]:
        cx, cy = c
        w, h = self.hw * SS * s, self.hw * SS / 2 * s
        return [(cx, cy - h), (cx + w, cy), (cx, cy + h), (cx - w, cy)]

    def to_frame(self, x: int, y: int) -> tuple[float, float] | None:
        i = self.at.get((x, y))
        c = self.centre[i] if i is not None else None
        return None if c is None else (self.map_x + c[0] / SS, self.map_y + c[1] / SS)

    def neighbour(self, x: int, y: int, dx: int, dy: int) -> int | None:
        nx = (x + dx) % self.mw if self.wrap else x + dx
        return self.at.get((nx, y + dy))

    # -- static layers --

    def terrain(self, t: list) -> tuple[str, tuple[int, int, int]]:
        names = self.m.meta["terrain"]
        base = names[t[2]]
        key = names[t[3]] if t[3] is not None and t[3] >= 0 else base
        c = TERRAIN.get(key, (90, 90, 90))
        return base, blend(c, (0, 0, 0), ((t[0] * 7 + t[1] * 13) % 5) * 0.018)

    def _terrain_layer(self) -> Image.Image:
        img = Image.new("RGB", (self.map_w * SS, self.map_h * SS), TERRAIN["ocean"])
        d = ImageDraw.Draw(img)
        self.tcol = []
        for t, c in zip(self.m.tiles, self.centre, strict=True):
            base, col = self.terrain(t)
            self.tcol.append((base in WATER, col))
            if c is not None:
                d.polygon(self.diamond(c), fill=col, outline=col)
        return img

    def _river_segments(self) -> list[tuple]:
        # river tiles only carry a flag: join them with a spanning forest, so adjacent river tiles read as a
        # branching line rather than a lattice of little diamonds
        parent: dict[int, int] = {}

        def root(i: int) -> int:
            while parent.setdefault(i, i) != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        segs = []
        for i, t in enumerate(self.m.tiles):
            if not t[4] or self.centre[i] is None:
                continue
            for dx, dy in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
                j = self.neighbour(t[0], t[1], dx, dy)
                if j is None or not self.m.tiles[j][4] or self.centre[j] is None \
                        or abs(self.centre[j][0] - self.centre[i][0]) > self.hw * SS * 1.5:
                    continue
                a, b = root(i), root(j)
                if a != b:
                    parent[a] = b
                    segs.append((i, j))
        # corner-cut the tree: a zigzag along a row of diamonds becomes a straight run through the edge midpoints
        mids: dict[int, list] = {}
        for i, j in segs:
            (ax, ay), (bx, by) = self.centre[i], self.centre[j]
            mid = ((ax + bx) / 2, (ay + by) / 2)
            mids.setdefault(i, []).append(mid)
            mids.setdefault(j, []).append(mid)
        lines = []
        for i, ms in mids.items():
            if len(ms) == 2:
                lines.append((ms[0], ms[1]))
            else:
                lines += [(self.centre[i], q) for q in ms]
        return lines

    def _chrome(self) -> Image.Image:
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, HEADER_H - 1], fill=PANEL)
        d.rectangle([PANEL_X, HEADER_H, W, H], fill=PANEL)
        d.line([PANEL_X, HEADER_H, PANEL_X, H], fill=GRID)
        x0, y0, x1, y1 = self.chart_box
        d.rounded_rectangle([x0, y0, x1, y1], radius=6, fill=PANEL)
        return img

    # -- per-frame map --

    def tint(self, i: int, o: int) -> tuple:
        k = (i, o)
        if k not in self._tint:
            water, col = self.tcol[i]
            self._tint[k] = blend(col, self.m.color.get(o, MUTED), TINT_WATER if water else TINT_LAND)
        return self._tint[k]

    def draw_map(self, frame: Image.Image, ti: int, own: list[int]) -> None:
        m, hw2 = self.m, self.hw * SS
        img = self.base.copy()
        d = ImageDraw.Draw(img)
        owned = [(i, o) for i, o in enumerate(own) if o >= 0 and self.centre[i] is not None]
        for i, o in owned:
            col = self.tint(i, o)
            d.polygon(self.diamond(self.centre[i]), fill=col, outline=col)
        rw = max(SS, round(hw2 * 0.06))
        for a, b in self.rivers:
            d.line([a, b], fill=RIVER, width=rw)
        # borders: each owner draws its own side of every edge it shares with someone else, inset so two civs'
        # borders sit side by side
        bw = max(4, round(2.5 * SS))
        k = (bw / 2) / (0.447 * hw2)
        edges = ((1, -1, 0, 1), (1, 1, 1, 2), (-1, 1, 2, 3), (-1, -1, 3, 0))
        for i, o in owned:
            t = m.tiles[i]
            c = self.centre[i]
            pts = None
            for dx, dy, a, b in edges:
                j = self.neighbour(t[0], t[1], dx, dy)
                if j is not None and own[j] == o:
                    continue
                pts = pts or self.diamond(c)
                (ax, ay), (bx, by) = pts[a], pts[b]
                mx, my = (ax + bx) / 2, (ay + by) / 2
                ox, oy = (c[0] - mx) * k, (c[1] - my) * k
                # extend a touch past the vertices so neighbouring segments join without notches
                ex, ey = (bx - ax) * 0.04, (by - ay) * 0.04
                d.line([(ax + ox - ex, ay + oy - ey), (bx + ox + ex, by + oy + ey)], fill=m.color.get(o, MUTED),
                       width=bw)
        turn = m.turns[ti]
        towns = {(c[0], c[1]) for c in turn["cities"]}
        ur = 2.3 * SS
        for x, y, o, _civil, mil in turn["units"]:
            if not mil or (x, y) in towns:
                continue
            i = self.at.get((x, y))
            if i is None or self.centre[i] is None:
                continue
            cx, cy = self.centre[i]
            d.ellipse([cx - ur, cy - ur, cx + ur, cy + ur], fill=m.color.get(o, MUTED), outline=DARK, width=SS)
        self.cities = []
        for x, y, name, o, size, cap in sorted(turn["cities"], key=lambda c: c[4]):
            i = self.at.get((x, y))
            if i is None or self.centre[i] is None:
                continue
            cx, cy = self.centre[i]
            r = self.city_r(size)
            R = r * SS
            ring = 2 * SS
            d.ellipse([cx - R - ring, cy - R - ring, cx + R + ring, cy + R + ring], fill=DARK)
            d.ellipse([cx - R, cy - R, cx + R, cy + R], fill=m.color.get(o, MUTED))
            if cap:
                d.polygon(star(cx, cy + R * 0.05, R * 0.78), fill=(255, 255, 255))
            self.cities.append((self.map_x + cx / SS, self.map_y + cy / SS, r + 2, name, o, size, cap))
        frame.paste(img.reduce(SS), (self.map_x, self.map_y))
        fd = ImageDraw.Draw(frame)
        fd.rectangle([self.map_x - 1, self.map_y - 1, self.map_x + self.map_w, self.map_y + self.map_h], outline=GRID)
        self.draw_labels(fd)

    @staticmethod
    def city_r(size: int) -> float:
        return 2.4 + 1.45 * math.sqrt(max(1, size))

    def draw_labels(self, d: ImageDraw.ImageDraw) -> None:
        cap_f, cap_sub, city_f = font(15, bold=True), font(12), font(12)
        sizes = sorted((c[5] for c in self.cities if not c[6]), reverse=True)
        threshold = max(3, min(6, sizes[min(len(sizes), 30) - 1] if sizes else 6))
        x_lo, y_lo = self.map_x + 3, self.map_y + 3
        x_hi, y_hi = self.map_x + self.map_w - 3, self.map_y + self.map_h - 3
        discs = [(cx - r, cy - r, cx + r, cy + r) for cx, cy, r, *_ in self.cities]
        placed: list[tuple[float, float, float, float]] = []

        def free(box, own_disc, discs_too=True) -> bool:
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

        cands = [(k, c) for k, c in enumerate(self.cities) if c[6] or c[5] >= threshold]
        cands.sort(key=lambda kc: (not kc[1][6], -kc[1][5], kc[1][3]))
        for k, (cx, cy, r, name, o, _size, cap) in cands:
            main_f = cap_f if cap else city_f
            sub = f" {self.m.name[o]}" if cap else ""
            w1 = text_w(name, main_f)
            w2 = text_w(sub, cap_sub) if sub else 0
            w, h = w1 + w2, (15 if cap else 12)
            spots = [(cx - w / 2, cy - r - 3 - h), (cx - w / 2, cy + r + 3), (cx + r + 5, cy - h / 2 - 1),
                     (cx - r - 5 - w, cy - h / 2 - 1)]
            tries = [(lx, ly, True) for lx, ly in spots] + ([(lx, ly, False) for lx, ly in spots] if cap else [])
            for lx, ly, strict in tries:
                box = (lx - 1, ly - 1, lx + w + 1, ly + h + 3)
                if free(box, k, strict):
                    placed.append(box)
                    d.text((lx, ly + h), txt(name), font=main_f, fill=TEXT if cap else SOFT, anchor="ls",
                           stroke_width=3, stroke_fill=DARK)
                    if sub:
                        d.text((lx + w1, ly + h), txt(sub), font=cap_sub, fill=MUTED, anchor="ls",
                               stroke_width=2, stroke_fill=DARK)
                    break

    # -- header --

    def draw_header(self, d: ImageDraw.ImageDraw, turn: int) -> None:
        m = self.m
        right_x = W - PAD - 4
        big, small, lab = font(34, bold=True), font(20), font(16)
        limit = f" / {m.limit}"
        tw = text_w(limit, small)
        d.text((right_x, 44), txt(limit), font=small, fill=MUTED, anchor="rs")
        d.text((right_x - tw, 44), str(turn), font=big, fill=TEXT, anchor="rs")
        nw = text_w(str(turn), big)
        d.text((right_x - tw - nw - 10, 44), "TURN", font=font(15, bold=True), fill=MUTED, anchor="rs")
        turn_left = right_x - tw - nw - 10 - text_w("TURN", font(15, bold=True))
        # left: title, ellipsized against the turn block
        n_agents = sum(1 for p in m.civs if p.get("seat"))
        title, rest = "OpenCiv3", f"  ·  {n_agents or len(m.civs)} agents  ·  seed {m.meta.get('seed', '?')}"
        tf = font(24, bold=True)
        x = PAD + 4
        room = turn_left - 40 - x
        title = fit(title, tf, room)
        d.text((x, 42), title, font=tf, fill=TEXT, anchor="ls")
        x += tf.getlength(title)
        d.text((x, 42), fit(rest, lab, max(0, turn_left - 40 - x)), font=lab, fill=MUTED, anchor="ls")
        # progress bar along the bottom edge of the header
        y = HEADER_H - 3
        d.rectangle([0, y, W, HEADER_H - 1], fill=TRACK)
        d.rectangle([0, y, round(W * min(1, turn / m.limit)), HEADER_H - 1], fill=SOFT)

    # -- panel --

    def heading(self, d, x, y, text, right: str | None = None, x1: int | None = None) -> None:
        d.text((x, y), txt(text), font=font(13, bold=True), fill=MUTED)
        if right and x1:
            d.text((x1, y), txt(right), font=font(12), fill=MUTED, anchor="ra")

    def draw_panel(self, frame: Image.Image, ti: int, own: list[int]) -> None:
        m = self.m
        d = ImageDraw.Draw(frame)
        x0, x1 = PANEL_X + 22, W - PAD - 4
        y = HEADER_H + 20
        # ---- standings
        cols = dict(rank=x0 + 14, delta=x0 + 22, chip=x0 + 58, name=x0 + 76, bar0=x0 + 236, bar1=x0 + 360,
                    score=x0 + 412, cities=x0 + 450, pop=x0 + 492, techs=x0 + 534, spark=x1 - 60)
        hf = font(11, bold=True)
        self.heading(d, x0, y, "STANDINGS")
        for key, label in (("score", "SCORE"), ("cities", "CTY"), ("pop", "POP"), ("techs", "TECH")):
            d.text((cols[key], y + 2), label, font=hf, fill=MUTED, anchor="ra")
        d.text((x1, y + 2), "LAST 40", font=hf, fill=MUTED, anchor="ra")
        y += 26
        ranks, prev = m.ranks[ti], m.ranks[ti - 10] if ti >= 10 else None
        order = sorted(m.civs, key=lambda p: ranks[p["index"]])
        lead_score = max(1, m.score(ti, order[0]["index"])[0])
        row_h = 40
        for p in order:
            i = p["index"]
            total, cities, pop, _tiles, techs, dead = m.score(ti, i)
            col = m.color[i]
            ink = MUTED if dead else TEXT
            cy = y + row_h // 2
            d.line([x0, y, x1, y], fill=GRID)
            if ranks[i] == 1:
                d.rectangle([x0 - 10, y + 1, x1 + 6, y + row_h - 1], fill=(33, 37, 44))
            d.text((cols["rank"], cy), str(ranks[i]), font=font(16, bold=True), fill=ink, anchor="rm")
            if prev is None or prev[i] == ranks[i]:
                d.text((cols["delta"] + 8, cy), "–", font=font(12), fill=MUTED, anchor="mm")
            else:
                up = prev[i] > ranks[i]
                d.text((cols["delta"], cy), txt(f"{'▲' if up else '▼'}{abs(prev[i] - ranks[i])}"), font=font(12),
                       fill=UP if up else DOWN, anchor="lm")
            d.rounded_rectangle([cols["chip"], cy - 6, cols["chip"] + 12, cy + 6], radius=3, fill=col)
            nf, cf = font(16, bold=True), font(12)
            room = cols["bar0"] - 12 - cols["name"]
            label = fit(p["label"] or p["civ"], nf, room)
            d.text((cols["name"], cy + 6), label, font=nf, fill=ink, anchor="ls")
            lw = nf.getlength(label) + 6
            civ = p["civ"] + (" · out" if dead else "") if p["label"] else ("out" if dead else "")
            if civ and room - lw > 20:
                d.text((cols["name"] + lw, cy + 6), fit(civ, cf, room - lw), font=cf, fill=MUTED, anchor="ls")
            # score bar: square at the baseline, 4px rounded data end
            b0, b1 = cols["bar0"], cols["bar1"]
            d.rectangle([b0, cy - 5, b1, cy + 5], fill=TRACK)
            bl = (b1 - b0) * total / lead_score
            if bl >= 1:
                d.rounded_rectangle([b0, cy - 5, b0 + max(bl, 9), cy + 5], radius=4, fill=col,
                                    corners=(False, True, True, False))
            d.text((cols["score"], cy), str(total), font=font(16, bold=True), fill=ink, anchor="rm")
            sf = font(13)
            for key, v in (("cities", cities), ("pop", pop), ("techs", techs)):
                d.text((cols["cities" if key == "cities" else key], cy), str(v), font=sf, fill=MUTED, anchor="rm")
            self.sparkline(frame, cols["spark"], cy - 8, m.series[i][max(0, ti - 39): ti + 1], col,
                           (33, 37, 44) if ranks[i] == 1 else PANEL)
            y += row_h
        d.line([x0, y, x1, y], fill=GRID)
        y += 28
        # ---- territory
        counts: dict[int, int] = {}
        for o in own:
            if o > 0 and not m.players.get(o, {}).get("barbarian"):
                counts[o] = counts.get(o, 0) + 1
        total = sum(counts.values())
        self.heading(d, x0, y, "TERRITORY")
        if total:
            lead, n = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))
            share = f"{100 * n / total:.0f}%"
            sf, bf = font(13), font(13, bold=True)
            tail = " of claimed tiles"
            d.text((x1, y + 1), tail, font=sf, fill=MUTED, anchor="ra")
            xx = x1 - sf.getlength(tail)
            d.text((xx, y + 1), share, font=bf, fill=TEXT, anchor="ra")
            xx -= bf.getlength(share) + 5
            d.text((xx, y + 1), txt(f"{m.name[lead]} holds"), font=sf, fill=MUTED, anchor="ra")
            by = y + 24
            segs = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            span = x1 - x0 - 2 * (len(segs) - 1)
            acc, xs = 0, x0
            for o, c in segs:
                acc += c
                xe = x0 + round(span * acc / total) + 2 * (segs.index((o, c)))
                if xe - 1 >= xs:
                    d.rectangle([xs, by, xe - 1, by + 15], fill=m.color[o])
                xs = xe + 2
            y = by + 16 + 30
        else:
            d.text((x0, y + 24), "no claimed tiles yet", font=font(13), fill=MUTED)
            y += 24 + 16 + 30
        # ---- key moments
        self.heading(d, x0, y, "KEY MOMENTS")
        y += 26
        line_h = 36
        n = max(1, min(MAX_MOMENTS, (H - PAD - 6 - y) // line_h))
        now = m.turns[ti]["turn"]
        tf = font(10, bold=True)
        moments = self.moments(ti, n)
        if not moments:
            d.text((x0, y + 8), "nothing notable yet", font=font(13), fill=MUTED)
        for turn, e in moments:
            cy = y + line_h // 2
            d.line([x0, y, x1, y], fill=GRID)
            d.text((x0, cy), f"T{turn}", font=font(13), fill=MUTED, anchor="lm")
            d.rounded_rectangle([x0 + 50, cy - 6, x0 + 62, cy + 6], radius=3, fill=m.color.get(e["owner"], MUTED))
            tag = TAG.get(e["kind"], "")
            strong = PRIORITY.get(e["kind"], 9) <= 2
            tw = tf.getlength(tag)
            d.rounded_rectangle([x1 - tw - 12, cy - 9, x1, cy + 9], radius=4, outline=GRID, width=1,
                                fill=(48, 34, 36) if strong else PANEL)
            d.text((x1 - 6, cy), tag, font=tf, fill=DOWN if strong else MUTED, anchor="rm")
            f = font(15, bold=strong or e["kind"] == "lead_change")
            ink = TEXT if (turn >= now - 2 or strong or e["kind"] == "lead_change") else SOFT
            d.text((x0 + 76, cy), fit(e["text"], f, x1 - tw - 24 - x0 - 76), font=f, fill=ink, anchor="lm")
            y += line_h

    def moments(self, ti: int, n: int) -> list[tuple[int, dict]]:
        m, now = self.m, self.m.turns[ti]["turn"]
        pool = [(t["turn"], e) for t in m.turns[: ti + 1] if t["turn"] > now - MOMENT_WINDOW
                for e in t["events"] if e.get("kind") in PRIORITY]
        pool.sort(key=lambda pe: (-(pe[0] - HANDICAP[pe[1]["kind"]]), PRIORITY[pe[1]["kind"]]))
        pick = pool[:n]
        return sorted(pick, key=lambda pe: (-pe[0], PRIORITY[pe[1]["kind"]]))

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
        frame.paste(img.resize((w, h), Image.LANCZOS), (x, y))

    # -- chart --

    def draw_chart(self, frame: Image.Image, ti: int) -> None:
        m = self.m
        bx0, by0, bx1, by1 = self.chart_box
        bw, bh = bx1 - bx0, by1 - by0
        left, right, top, bottom = 54, bw - 108, 34, bh - 30
        now = m.turns[ti]["turn"]
        # The y scale follows the turns played so far, so early turns aren't squashed against the axis.
        seen = max([v for vals in m.series.values() for v in vals[: ti + 1]] + [10])
        step = nice_step(seen * 1.06)
        ytop = max(seen * 1.06, step)
        S = SS
        img = Image.new("RGB", (bw * S, bh * S), BG)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, bw * S - 1, bh * S - 1], radius=6 * S, fill=PANEL)

        def X(turn: float) -> float:
            return left + (right - left) * turn / m.limit

        def Y(v: float) -> float:
            return bottom - (bottom - top) * v / ytop

        for v in range(0, int(ytop) + 1, step):
            d.line([(left * S, Y(v) * S), (right * S, Y(v) * S)], fill=GRID if v else (58, 63, 72), width=S)
        xstep = 50 if m.limit >= 150 else 25
        for t in range(0, m.limit + 1, xstep):
            d.line([(X(t) * S, bottom * S), (X(t) * S, (bottom + 4) * S)], fill=(58, 63, 72), width=S)
        # cursor
        d.line([(X(now) * S, (top - 10) * S), (X(now) * S, bottom * S)], fill=(84, 90, 100), width=S)
        leader = min(m.civs, key=lambda p: m.ranks[ti][p["index"]])["index"]
        order = sorted(m.civs, key=lambda p: (p["index"] == leader, -m.ranks[ti][p["index"]]))
        ends = []
        for p in order:
            i = p["index"]
            vals = m.series[i][: ti + 1]
            pts = [(X(m.turns[k]["turn"]) * S, Y(v) * S) for k, v in enumerate(vals)]
            if len(pts) > 1:
                d.line(pts, fill=m.color[i], width=4 if i == leader else 3, joint="curve")
            if pts:
                ends.append((pts[-1][1] / S, pts[-1][0] / S, i))
        for ex, ey, i in [(e[1], e[0], e[2]) for e in ends]:
            r = 3 * S
            d.ellipse([ex * S - r, ey * S - r, ex * S + r, ey * S + r], fill=m.color[i], outline=PANEL, width=S)
        # lead changes: diamonds on the x axis
        for t, o in m.lead_changes:
            if t <= now:
                cx, cy, r = X(t) * S, bottom * S, 5 * S
                d.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], fill=m.color.get(o, MUTED),
                          outline=PANEL, width=S)
        chart = img.resize((bw, bh), Image.LANCZOS)
        # text at 1x
        cd = ImageDraw.Draw(chart)
        sf = font(11)
        cd.text((16, 12), "SCORE", font=font(13, bold=True), fill=MUTED)
        lx = right + 6
        cd.text((right + 100, 12), txt("◆ lead change"), font=sf, fill=MUTED, anchor="ra")
        for v in range(0, int(ytop) + 1, step):
            cd.text((left - 8, Y(v)), str(v), font=sf, fill=MUTED, anchor="rm")
        for t in range(0, m.limit + 1, xstep):
            cd.text((X(t), bottom + 8), f"T{t}", font=sf, fill=MUTED, anchor="mt")
        if 0 < now < m.limit:
            cd.text((X(now), top - 12), f"T{now}", font=font(11, bold=True), fill=SOFT, anchor="mb")
        ys = spread([e[0] for e in ends], 15, top, bottom)
        for (ey, ex, i), ly in zip(ends, ys, strict=True):
            lx = ex + 10
            if abs(ly - ey) > 2:
                cd.line([(ex + 4, ey), (lx - 3, ly)], fill=m.color[i], width=1)
            nf = font(12, bold=(i == leader))
            name = txt(m.name[i])
            cd.text((lx, ly), name, font=nf, fill=TEXT, anchor="lm")
            cd.text((lx + nf.getlength(name) + 5, ly), str(m.series[i][ti]), font=sf, fill=MUTED, anchor="lm")
        frame.paste(chart, (bx0, by0))

    # -- frame --

    def frame(self, ti: int, own: list[int]) -> Image.Image:
        img = self.chrome.copy()
        self.draw_map(img, ti, own)
        self.draw_chart(img, ti)
        self.draw_panel(img, ti, own)
        self.draw_header(ImageDraw.Draw(img), self.m.turns[ti]["turn"])
        return img


# ---------------------------------------------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("match", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--turns", default="60,130,200", help="turns also saved as a-broadcast-TNNN.png")
    ap.add_argument("--no-video", action="store_true")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    m = Match(args.match)
    b = Broadcast(m)
    stills = {int(t) for t in args.turns.split(",") if t}
    ff = None
    mp4 = args.out / "a-broadcast.mp4"
    if not args.no_video:
        exe = shutil.which("ffmpeg")
        if not exe:
            print("ffmpeg not on PATH: skipping the mp4", file=sys.stderr)
        else:
            ff = subprocess.Popen([exe, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                                   "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-crf", "23",
                                   "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(mp4)],
                                  stdin=subprocess.PIPE)
    t0, n, last = time.time(), 0, None
    for ti, own in m.owners():
        turn = m.turns[ti]["turn"]
        if ff is None and turn not in stills:
            continue
        img = b.frame(ti, own)
        n += 1
        if turn in stills:
            img.save(args.out / f"a-broadcast-T{turn:03d}.png", optimize=False)
        if ff is not None:
            ff.stdin.write(img.tobytes())
        last = img
    if ff is not None and last is not None:
        for _ in range(FPS * HOLD_S):
            ff.stdin.write(last.tobytes())
        ff.stdin.close()
        if ff.wait() != 0:
            print("ffmpeg failed", file=sys.stderr)
    dt = time.time() - t0
    print(f"{n} frames in {dt:.1f}s ({dt / max(n, 1) * 1000:.0f} ms/frame) -> {args.out}")


if __name__ == "__main__":
    main()
