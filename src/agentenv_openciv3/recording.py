"""Game recordings rendered from the bridge's per-turn snapshots (docs/recording.md): mp4, gif, html or png.

Pillow only (plus ffmpeg on PATH for mp4), so harnesses and tests can render a directory of snapshots:

    python -m agentenv_openciv3.recording <record dir> --out <dir> [--actions actions.jsonl] [--formats mp4,html]
"""

from __future__ import annotations

import argparse
import base64
import gzip
import html
import io
import json
import os
import shutil
import subprocess
import tempfile
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .actionlog import describe

FORMATS = {"mp4": "video/mp4", "gif": "image/gif", "html": "text/html", "png": "image/png"}
BASELINE_LABELS = {"engine_ai": "built-in AI", "settler_bot": "settler bot", "found_capital": "capital only",
                   "null": "do-nothing"}
URGENT = {"threat", "unit_lost", "war_declared", "city_destroyed", "disorder", "disorder_started", "city_starved",
          "gold_stolen", "defenseless", "riot_risk", "civ_destroyed"}

TERRAIN = {
    "ocean": (24, 52, 98), "sea": (30, 68, 122), "coast": (54, 108, 160), "grassland": (84, 140, 58),
    "plains": (158, 150, 76), "desert": (212, 190, 130), "tundra": (156, 162, 150), "floodplain": (122, 158, 72),
    "hills": (128, 110, 70), "mountains": (116, 108, 102), "forest": (36, 94, 46), "jungle": (26, 110, 70),
    "marsh": (76, 110, 94), "volcano": (98, 58, 50),
}
UNKNOWN, BG, PANEL, ROW = (8, 10, 14), (18, 22, 28), (26, 31, 40), (46, 55, 70)
TEXT, DIM, WARN, RIVER = (230, 233, 238), (140, 148, 162), (240, 128, 96), (88, 160, 238)
MAP_TARGET, PANEL_W, HEADER_H, CHART_H, PAD, MIN_H = 920, 360, 40, 150, 14, 720
TINT = 0.38
FONTS = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
]
ASCII = str.maketrans({"→": "->", "–": "-", "—": "-", "…": "...", "✗": "x", "×": "x"})


@dataclass(frozen=True)
class File:
    name: str
    content_type: str
    data: bytes


# ---- input ----

def load_snapshots(directory: str | Path) -> list[dict]:
    """The `turn-NNNN.json.gz` snapshots in turn order; a turn written twice (after a restart) keeps the last."""
    snaps: dict[int, dict] = {}
    for path in sorted(Path(directory).glob("turn-*.json.gz")):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                snap = json.load(f)
        except (OSError, EOFError, ValueError):
            continue    # cut short by a crash
        snaps[snap["turn"]] = snap
    return [snaps[t] for t in sorted(snaps)]


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
    return "".join(c for c in s if not unicodedata.combining(c) and (c.isascii() or c == "·"))


def fit(s: str, f: ImageFont.FreeTypeFont, width: float) -> str:
    s = txt(s)
    if f.getlength(s) <= width:
        return s
    ell = txt("…")
    while s and f.getlength(s + ell) > width:
        s = s[:-1]
    return s + ell


# ---- geometry ----

@dataclass(frozen=True)
class Layout:
    width: int
    height: int
    hw: int          # half a tile's width; a tile is a 2hw x hw diamond
    map_w: int
    map_h: int
    map_x: int = PAD
    map_y: int = HEADER_H

    @classmethod
    def of(cls, m: dict) -> Layout:
        hw = max(6, MAP_TARGET // (m["width"] + 1) // 2 * 2)
        map_w, map_h = (m["width"] + 1) * hw, (m["height"] + 1) * hw // 2
        width = PAD + map_w + PAD + PANEL_W
        height = max(MIN_H, HEADER_H + map_h + PAD + CHART_H + PAD)
        return cls(width + width % 2, height + height % 2, hw, map_w, map_h)

    @property
    def chart(self) -> tuple[int, int, int, int]:
        top = self.map_y + self.map_h + PAD
        return self.map_x, top, self.map_x + self.map_w, self.height - PAD

    @property
    def panel_x(self) -> int:
        return self.map_x + self.map_w + PAD

    def center(self, x: int, y: int) -> tuple[int, int]:
        return self.map_x + self.hw * (x + 1), self.map_y + self.hw // 2 * (y + 1)

    def diamond(self, x: int, y: int, scale: float = 1.0) -> list[tuple[float, float]]:
        cx, cy = self.center(x, y)
        w, h = self.hw * scale, self.hw // 2 * scale
        return [(cx, cy - h), (cx + w, cy), (cx, cy + h), (cx - w, cy)]


def blend(a: tuple, b: tuple, t: float) -> tuple[int, int, int]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b, strict=False))


def hex_color(c: Iterable[int]) -> str:
    return "#" + "".join(f"{v:02x}" for v in c)


def terrain_color(row: list) -> tuple[int, int, int]:
    x, y, base, overlay = row[:4]
    key = (overlay or base or "").lower().replace(" ", "").replace("_", "")
    c = TERRAIN.get(key, (110, 110, 110))
    return blend(c, (0, 0, 0), ((x * 7 + y * 13) % 5) * 0.02)


# ---- frames ----

def is_barbarian(p: dict) -> bool:
    return "barbarian" in str(p.get("civ", "")).lower()


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


class Renderer:
    """Draws frames for one game; the static terrain layer is drawn once."""

    def __init__(self, snapshots: list[dict], *, view: str = "spectator", actions: dict | None = None,
                 baselines: dict[str, dict] | None = None):
        self.snaps = snapshots
        self.view = view
        self.actions = {int(t): v for t, v in (actions or {}).items()}
        self.baselines = {p: {int(t): s for t, s in scores.items()} for p, scores in (baselines or {}).items()}
        first = snapshots[-1]
        self.map = first["map"]
        self.layout = Layout.of(self.map)
        self.width = self.map["width"]
        self.limit = max(s.get("turn_limit") or 0 for s in snapshots) or snapshots[-1]["turn"] or 1
        self.top = max([p["score"]["total"] for s in snapshots for p in s["players"] if not is_barbarian(p)]
                       + [sc["total"] for scores in self.baselines.values() for sc in scores.values()] + [10])
        self.base = self._terrain_layer(first["tiles"])
        self.rivers = self._river_segments(first["tiles"])

    # -- map --

    def _terrain_layer(self, tiles: list[list]) -> Image.Image:
        L = self.layout
        img = Image.new("RGB", (L.width, L.height), BG)
        draw = ImageDraw.Draw(img)
        draw.rectangle([L.map_x, L.map_y, L.map_x + L.map_w - 1, L.map_y + L.map_h - 1], fill=UNKNOWN)
        for row in tiles:
            draw.polygon(L.diamond(row[0], row[1]), fill=terrain_color(row))
        return img

    def _neighbour(self, x: int, y: int, dx: int, dy: int) -> tuple[int, int]:
        nx = x + dx
        if self.map.get("wrap_x"):
            nx %= self.width
        return nx, y + dy

    def _river_segments(self, tiles: list[list]) -> list[tuple[tuple[int, int], tuple[int, int]]]:
        river = {(r[0], r[1]) for r in tiles if r[5]}
        segs = []
        for x, y in river:
            for dx, dy in ((1, -1), (1, 1)):
                n = self._neighbour(x, y, dx, dy)
                if n in river and abs(n[0] - x) == 1:
                    segs.append(((x, y), n))
        return segs

    def _draw_map(self, img: Image.Image, snap: dict) -> None:
        L, agent = self.layout, self.view == "agent"
        draw = ImageDraw.Draw(img)
        colors = {p["index"]: tuple(p.get("color") or (128, 128, 128)) for p in snap["players"]}
        known = {(r[0], r[1]) for r in snap["tiles"] if r[6]}
        owner = {(r[0], r[1]): r[4] for r in snap["tiles"] if r[4] >= 0 and (not agent or r[6])}
        for row in snap["tiles"]:
            x, y = row[0], row[1]
            if agent and not row[6]:
                draw.polygon(L.diamond(x, y), fill=UNKNOWN)
            elif row[4] >= 0:
                draw.polygon(L.diamond(x, y), fill=blend(terrain_color(row), colors.get(row[4], DIM), TINT))
        line = max(2, L.hw // 4)
        for a, b in self.rivers:
            if not agent or (a in known and b in known):
                draw.line([L.center(*a), L.center(*b)], fill=RIVER, width=line)
        edges = ((1, -1, 0, 1), (1, 1, 1, 2), (-1, 1, 2, 3), (-1, -1, 3, 0))    # neighbour, then diamond vertices
        for (x, y), o in owner.items():
            pts = L.diamond(x, y, 0.86)
            color = blend(colors.get(o, DIM), (255, 255, 255), 0.25)
            for dx, dy, i, j in edges:
                if owner.get(self._neighbour(x, y, dx, dy)) != o:
                    draw.line([pts[i], pts[j]], fill=color, width=max(2, L.hw // 6))
        self._draw_units(draw, snap, colors, known)
        self._draw_cities(draw, snap, colors, known)

    def _draw_units(self, draw: ImageDraw.ImageDraw, snap: dict, colors: dict, known: set) -> None:
        L, r = self.layout, max(2, self.layout.hw // 4)
        towns = {(c["x"], c["y"]) for c in snap["cities"]}
        stacks: dict[tuple[int, int], list[int]] = {}
        for u in snap["units"]:
            if self.view == "agent" and (u["x"], u["y"]) not in known:
                continue
            owners = stacks.setdefault((u["x"], u["y"]), [])
            if u["owner"] not in owners:
                owners.append(u["owner"])
        for (x, y), owners in stacks.items():
            cx, cy = L.center(x, y)
            if (x, y) in towns:
                cx, cy = cx + L.hw * 0.55, cy + L.hw * 0.3
            for i, o in enumerate(owners[:3]):
                ox = cx + (i - (len(owners[:3]) - 1) / 2) * r * 1.8
                outline = (255, 255, 255) if o in self._seats(snap) else (0, 0, 0)
                draw.ellipse([ox - r, cy - r, ox + r, cy + r], fill=colors.get(o, DIM), outline=outline)

    def _draw_cities(self, draw: ImageDraw.ImageDraw, snap: dict, colors: dict, known: set) -> None:
        L = self.layout
        r = max(4, round(L.hw * 0.6))
        label = font(max(10, min(13, L.hw)), bold=True)
        seats = self._seats(snap)
        for c in snap["cities"]:
            if self.view == "agent" and (c["x"], c["y"]) not in known:
                continue
            cx, cy = L.center(c["x"], c["y"])
            mine = c["owner"] in seats
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=colors.get(c["owner"], DIM),
                         outline=(255, 255, 255) if mine else (0, 0, 0), width=2)
            if c.get("capital"):
                q = max(1, r // 3)
                draw.ellipse([cx - q, cy - q, cx + q, cy + q], fill=(255, 255, 255) if mine else (20, 20, 20))
            draw.text((cx, cy - r - 2), txt(f"{c['name']} {c['size']}"), font=label, fill=TEXT, anchor="mb",
                      stroke_width=2, stroke_fill=(0, 0, 0))

    @staticmethod
    def _seats(snap: dict) -> set[int]:
        """The players agents play: one, or one per seat."""
        return {p["index"] for p in snap["players"] if p.get("is_human")}

    # -- chart --

    def _draw_chart(self, img: Image.Image, upto: int) -> None:
        draw = ImageDraw.Draw(img)
        x0, y0, x1, y1 = self.layout.chart
        draw.rectangle([x0, y0, x1, y1], fill=PANEL)
        small = font(11)
        left, right, top, bottom = x0 + 34, x1 - 86, y0 + 18, y1 - 18
        draw.text((left + 4, y0 + 4), "score", font=small, fill=DIM)
        for value in (0, self.top):
            y = bottom - (bottom - top) * value / self.top
            draw.line([left, y, right, y], fill=ROW)
            draw.text((left - 4, y), str(value), font=small, fill=DIM, anchor="rm")
        for turn in (0, self.limit):
            draw.text((left + (right - left) * turn / self.limit, bottom + 4), f"T{turn}", font=small, fill=DIM,
                      anchor="lt" if turn == 0 else "mt")

        def xy(turn: int, total: int) -> tuple[float, float]:
            return left + (right - left) * turn / self.limit, bottom - (bottom - top) * total / self.top

        labels: list[tuple[float, float, str, ImageFont.FreeTypeFont, tuple]] = []
        for policy, scores in self.baselines.items():
            pts = [xy(t, s["total"]) for t, s in sorted(scores.items()) if t <= upto]
            if len(pts) > 1:
                for a, b in zip(pts[::2], pts[1::2], strict=False):
                    draw.line([a, b], fill=DIM, width=1)
                labels.append((pts[-1][1], pts[-1][0] + 4, txt(BASELINE_LABELS.get(policy, policy)), small, DIM))
        series: dict[int, list] = {}
        for snap in self.snaps:
            if snap["turn"] > upto:
                break
            for p in snap["players"]:
                if not is_barbarian(p):
                    series.setdefault(p["index"], []).append(xy(snap["turn"], p["score"]["total"]))
        last = self.snaps[0]
        for snap in self.snaps:
            if snap["turn"] <= upto:
                last = snap
        for p in sorted(last["players"], key=lambda p: bool(p.get("is_human"))):
            pts = series.get(p["index"], [])
            if len(pts) > 1:
                draw.line(pts, fill=tuple(p.get("color") or DIM), width=3 if p.get("is_human") else 1)
            if pts and p.get("is_human"):
                labels.append((pts[-1][1], pts[-1][0] + 4, txt(p.get("label") or p["civ"]), font(11, bold=True), TEXT))
        for (_, x, text, f, fill), y in zip(labels, spread([lb[0] for lb in labels], 12, top, bottom), strict=True):
            draw.text((x, y), text, font=f, fill=fill, anchor="lm")

    # -- side panel --

    def scoreboard(self, snap: dict) -> list[dict]:
        rows = [p for p in snap["players"] if not is_barbarian(p)]
        return sorted(rows, key=lambda p: (-p["score"]["total"], p["index"]))

    def baselines_at(self, turn: int) -> dict[str, dict]:
        out = {}
        for policy, scores in self.baselines.items():
            known = [t for t in scores if t <= turn]
            if known and max(scores) >= turn:
                out[policy] = scores[max(known)]
        return out

    def turn_actions(self, snap: dict) -> list[dict]:
        return self.actions.get(snap["turn"] - 1, [])

    def _draw_panel(self, img: Image.Image, snap: dict) -> None:
        L = self.layout
        draw = ImageDraw.Draw(img)
        x0, x1 = L.panel_x, L.width - PAD
        draw.rectangle([L.panel_x - PAD // 2, 0, L.width, L.height], fill=PANEL)
        width = x1 - x0
        draw.text((x0, 16), f"Turn {snap['turn']} / {snap.get('turn_limit', '?')}", font=font(26, bold=True),
                  fill=TEXT)
        view = "spectator view" if self.view != "agent" else "agent view (explored tiles)"
        draw.text((x0, 50), fit(f"OpenCiv3 · {agents(snap)} · seed {snap.get('seed', '?')} · {view}",
                                font(12), width), font=font(12), fill=DIM)
        y = 80
        y = self._heading(draw, x0, y, "SCORE  ·  cities  pop  techs")
        body = font(14)
        for p in self.scoreboard(snap):
            s = p["score"]
            if p.get("is_human"):
                draw.rectangle([x0 - 4, y - 2, x1 + 2, y + 18], fill=ROW)
            f = font(14, bold=bool(p.get("is_human")))
            draw.rectangle([x0, y + 3, x0 + 11, y + 14], fill=tuple(p.get("color") or DIM))
            color = DIM if p.get("defeated") else TEXT
            draw.text((x0 + 18, y), fit(player_name(p) + (" (out)" if p.get("defeated") else ""), f, 150), font=f,
                      fill=color)
            draw.text((x0 + 210, y), str(s["total"]), font=f, fill=color, anchor="ra")
            draw.text((x1, y), f"{s['cities']}  {s['pop']}  {s['techs']}", font=body, fill=DIM, anchor="ra")
            y += 21
        if base := self.baselines_at(snap["turn"]):
            y += 4
            for policy, s in base.items():
                draw.text((x0 + 18, y), txt(f"{BASELINE_LABELS.get(policy, policy)}, same seed"), font=font(13),
                          fill=DIM)
                draw.text((x0 + 210, y), str(s["total"]), font=font(13), fill=DIM, anchor="ra")
                draw.text((x1, y), f"{s['cities']}  {s['pop']}  {s['techs']}", font=font(13), fill=DIM, anchor="ra")
                y += 19
        actions = [(a["text"], a.get("ok", True)) for a in self.turn_actions(snap)]
        events = [(event_text(snap, e), e.get("kind") not in URGENT) for e in snap.get("events", [])]
        room = (L.height - PAD - y - 2 * 34) // 18
        n_actions = min(len(actions), max(room - min(len(events), room // 2), room // 2))
        prev = snap["turn"] - 1
        y = self._lines(draw, x0, y + 12, width, f"AGENT ACTIONS T{prev}" if prev >= 0 else "AGENT ACTIONS",
                        actions, n_actions, "none")
        self._lines(draw, x0, y + 8, width, f"EVENTS T{prev}" if prev >= 0 else "EVENTS", events,
                    max(1, room - max(n_actions, 1)), "none")

    def _heading(self, draw: ImageDraw.ImageDraw, x: int, y: int, text: str) -> int:
        draw.text((x, y), txt(text), font=font(12, bold=True), fill=DIM)
        return y + 20

    def _lines(self, draw, x, y, width, title, items, cap, empty) -> int:
        y = self._heading(draw, x, y, title)
        f = font(13)
        shown = items[:cap] if len(items) <= cap else items[:max(cap - 1, 0)]
        for text, ok in shown:
            draw.text((x, y), fit(("" if ok else "! ") + text, f, width), font=f, fill=TEXT if ok else WARN)
            y += 18
        if len(items) > len(shown):
            draw.text((x, y), f"+{len(items) - len(shown)} more", font=f, fill=DIM)
            y += 18
        if not items:
            draw.text((x, y), empty, font=f, fill=DIM)
            y += 18
        return y

    def frame(self, snap: dict) -> Image.Image:
        img = self.base.copy()
        self._draw_map(img, snap)
        self._draw_chart(img, snap["turn"])
        self._draw_panel(img, snap)
        draw = ImageDraw.Draw(img)
        draw.text((PAD, 12), txt(f"{agents(snap)} · T{snap['turn']}"), font=font(16, bold=True), fill=TEXT)
        return img

    def frames(self) -> Iterable[tuple[dict, Image.Image]]:
        for snap in self.snaps:
            yield snap, self.frame(snap)

    @property
    def map_box(self) -> tuple[int, int, int, int]:
        return 0, 0, self.layout.panel_x - PAD // 2, self.layout.height


# ---- outputs ----

def _png(img: Image.Image, colors: int | None = None) -> bytes:
    out = io.BytesIO()
    (img.quantize(colors=colors, method=Image.Quantize.FASTOCTREE) if colors else img).save(out, "PNG", optimize=True)
    return out.getvalue()


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


def render(snapshots: list[dict], *, formats: Iterable[str] = ("mp4", "html"), view: str = "spectator",
           fps: int = 4, name: str = "openciv3", actions: dict | None = None,
           baselines: dict[str, dict] | None = None) -> tuple[list[File], list[str]]:
    """Render the requested formats; returns the files and notes (e.g. that a gif replaced the mp4)."""
    if not snapshots:
        raise ValueError("no snapshots to render")
    formats = list(dict.fromkeys(formats))
    r = Renderer(snapshots, view=view, actions=actions, baselines=baselines)
    mp4 = Mp4(fps) if "mp4" in formats else None
    gif: list[Image.Image] = []
    pages: list[dict] = []
    last = None
    for snap, img in r.frames():
        last = img
        if mp4:
            mp4.add(img)
        if "gif" in formats or (mp4 and not shutil.which("ffmpeg")):
            gif.append(img.quantize(colors=128, method=Image.Quantize.FASTOCTREE))
        if "html" in formats:
            pages.append(_page(r, snap, img))
    files, notes = [], []
    if mp4:
        if (video := mp4.finish()) is not None:
            files.append(File(f"{name}.mp4", FORMATS["mp4"], video))
        else:
            notes.append("ffmpeg is not on PATH (or failed), so the mp4 was replaced by an animated gif")
            if "gif" not in formats:
                formats.append("gif")
            if not gif:
                gif = [img.quantize(colors=128, method=Image.Quantize.FASTOCTREE) for _, img in r.frames()]
    if "gif" in formats:
        out = io.BytesIO()
        durations = [1000 // fps] * (len(gif) - 1) + [2000]
        gif[0].save(out, "GIF", save_all=True, append_images=gif[1:], duration=durations, loop=0, optimize=False)
        files.append(File(f"{name}.gif", FORMATS["gif"], out.getvalue()))
    if "html" in formats:
        files.append(File(f"{name}.html", FORMATS["html"], _html(pages, fps, name).encode()))
    if "png" in formats:
        files.append(File(f"{name}.png", FORMATS["png"], _png(last)))
    return files, notes


def player_name(p: dict) -> str:
    return p["civ"] + (f" ({p['label']})" if p.get("label") else "")


def agents(snap: dict) -> str:
    """The civs agents play, e.g. "Rome" or "Rome (Opus) vs Greece (Sonnet)"."""
    return " vs ".join(player_name(p) for p in snap["players"] if p.get("is_human")) or "?"


def event_text(snap: dict, e: dict) -> str:
    """An event line; with several seats each event names the seat it happened to."""
    text = e.get("text") or e.get("kind", "")
    if not e.get("civ"):
        return text
    p = next((p for p in snap["players"] if p["civ"] == e["civ"]), {})
    return f"{p.get('label') or e['civ']}: {text}"


def _page(r: Renderer, snap: dict, img: Image.Image) -> dict:
    data = base64.b64encode(_png(img.crop(r.map_box), colors=128)).decode()
    return {
        "turn": snap["turn"], "limit": snap.get("turn_limit"), "img": f"data:image/png;base64,{data}",
        "players": [{"civ": player_name(p), "color": hex_color(p.get("color") or DIM), "human": bool(p.get("is_human")),
                     "defeated": bool(p.get("defeated")), **p["score"]} for p in r.scoreboard(snap)],
        "baselines": [{"label": BASELINE_LABELS.get(k, k), **v} for k, v in r.baselines_at(snap["turn"]).items()],
        "actions": r.turn_actions(snap),
        "events": [{"text": event_text(snap, e), "urgent": e.get("kind") in URGENT} for e in snap.get("events", [])],
    }


HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<style>
body{{margin:0;background:#12161c;color:#e6e9ee;font:14px/1.4 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{display:flex;gap:16px;padding:16px;align-items:flex-start}}
#map{{max-width:calc(100vw - 420px);height:auto;border-radius:6px;image-rendering:auto}}
aside{{width:360px;flex:none}}
h1{{font-size:24px;margin:0 0 4px}} h2{{font-size:12px;letter-spacing:.06em;color:#8c94a2;margin:16px 0 6px}}
.controls{{display:flex;gap:8px;align-items:center;margin:8px 0 12px}} input[type=range]{{flex:1}}
button{{background:#2e3746;color:#e6e9ee;border:0;border-radius:4px;padding:6px 12px;cursor:pointer}}
table{{width:100%;border-collapse:collapse}} td{{padding:2px 4px}} td.n{{text-align:right;color:#8c94a2}}
tr.me{{background:#2e3746;font-weight:600}} tr.base td{{color:#8c94a2;font-style:italic}}
.sw{{display:inline-block;width:11px;height:11px;margin-right:6px;vertical-align:-1px}}
ul{{list-style:none;padding:0;margin:0}} li{{padding:1px 0}} .bad,.urgent{{color:#f08060}} .none{{color:#8c94a2}}
</style></head><body><main>
<img id="map" alt="map">
<aside><h1 id="turn"></h1><div class="none">{title}</div>
<div class="controls"><button id="play">Play</button><button id="prev">&lsaquo;</button>
<input id="slider" type="range" min="0" value="0"><button id="next">&rsaquo;</button></div>
<h2>SCORE</h2><table id="score"></table>
<h2 id="acts-h">AGENT ACTIONS</h2><ul id="acts"></ul><h2 id="evts-h">EVENTS</h2><ul id="evts"></ul>
</aside></main>
<script id="data" type="application/json">{data}</script>
<script>
const pages = JSON.parse(document.getElementById("data").textContent), fps = {fps};
const $ = id => document.getElementById(id), slider = $("slider");
slider.max = pages.length - 1;
const esc = s => String(s).replace(/[&<>"]/g, c => ({{"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}})[c]);
const list = (items, cls) => items.length ? items.map(i => `<li class="${{cls(i)}}">${{esc(i.text)}}</li>`).join("")
  : '<li class="none">none</li>';
function show(i) {{
  const p = pages[i]; slider.value = i;
  $("map").src = p.img; $("turn").textContent = `Turn ${{p.turn}} / ${{p.limit}}`;
  $("score").innerHTML = '<tr><td></td><td class="n">score</td><td class="n">cities</td><td class="n">pop</td>' +
    '<td class="n">techs</td></tr>' + p.players.map(r => `<tr class="${{r.human ? "me" : ""}}"><td><span class="sw" ` +
    `style="background:${{r.color}}"></span>${{esc(r.civ)}}${{r.defeated ? " (out)" : ""}}</td><td class="n">` +
    `${{r.total}}</td><td class="n">${{r.cities}}</td><td class="n">${{r.pop}}</td><td class="n">${{r.techs}}</td></tr>`
    ).join("") + p.baselines.map(b => `<tr class="base"><td>${{esc(b.label)}}, same seed</td><td class="n">` +
    `${{b.total}}</td><td class="n">${{b.cities}}</td><td class="n">${{b.pop}}</td><td class="n">${{b.techs}}</td></tr>`
    ).join("");
  const prev = p.turn - 1, at = prev >= 0 ? ` T${{prev}}` : "";
  $("acts-h").textContent = "AGENT ACTIONS" + at; $("evts-h").textContent = "EVENTS" + at;
  $("acts").innerHTML = list(p.actions, a => a.ok === false ? "bad" : "");
  $("evts").innerHTML = list(p.events, e => e.urgent ? "urgent" : "");
}}
let timer = null;
function stop() {{ clearInterval(timer); timer = null; $("play").textContent = "Play"; }}
$("play").onclick = () => {{
  if (timer) return stop();
  if (+slider.value === pages.length - 1) show(0);
  $("play").textContent = "Pause";
  timer = setInterval(() => +slider.value < pages.length - 1 ? show(+slider.value + 1) : stop(), 1000 / fps);
}};
slider.oninput = () => {{ stop(); show(+slider.value); }};
$("prev").onclick = () => {{ stop(); show(Math.max(0, +slider.value - 1)); }};
$("next").onclick = () => {{ stop(); show(Math.min(pages.length - 1, +slider.value + 1)); }};
document.onkeydown = e => e.key === "ArrowLeft" ? $("prev").onclick() : e.key === "ArrowRight" ? $("next").onclick()
  : e.key === " " ? (e.preventDefault(), $("play").onclick()) : null;
show(pages.length - 1);
</script></body></html>
"""


def _html(pages: list[dict], fps: int, name: str) -> str:
    data = json.dumps(pages, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    return HTML.format(title=html.escape(name), data=data, fps=fps)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Render a game recording from a directory of bridge snapshots.")
    ap.add_argument("record_dir", type=Path)
    ap.add_argument("--out", type=Path, default=Path("."))
    ap.add_argument("--formats", default="mp4,html", help="comma-separated: " + ", ".join(FORMATS))
    ap.add_argument("--view", choices=("spectator", "agent"), default="spectator")
    ap.add_argument("--fps", type=int, default=4)
    ap.add_argument("--actions", type=Path, help="an env action log (OPENCIV_ACTION_LOG) for the actions panel")
    ap.add_argument("--name", default="openciv3")
    args = ap.parse_args(argv)
    files, notes = render(load_snapshots(args.record_dir), formats=args.formats.split(","), view=args.view,
                          fps=args.fps, name=args.name,
                          actions=timeline_from_log(args.actions) if args.actions else None)
    args.out.mkdir(parents=True, exist_ok=True)
    for f in files:
        (args.out / f.name).write_bytes(f.data)
        print(f"{args.out / f.name} ({len(f.data):,} bytes)")
    for note in notes:
        print(f"note: {note}")


if __name__ == "__main__":
    main()
