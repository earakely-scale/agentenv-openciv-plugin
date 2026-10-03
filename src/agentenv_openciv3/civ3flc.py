"""Civilization III unit animations (FLC files with Civ3's header extensions), decoded for the browser (webart.py).

The decoder follows the format as OpenCiv3 reads it (vendor/OpenCiv3/C7, AnimationManager.cs and Textures/):
  header (128 bytes): 0x06 frame count, 0x08/0x0A width/height, 0x10 ms per frame, 0x50 offset of the first frame,
    0x60 animations (directions, 8), 0x62 frames per animation, 0x64/0x66 the frame's offset inside the original
    canvas, 0x68/0x6A that canvas (240x240);
  body: per direction, its frames (BYTE_RUN first, then DELTA_FLC) and one "ring" frame back to the first, which is
    skipped. The 256-colour palette rides in the first frame.
Palette rules (PCXToGodot.cs): 255 is transparent; 240-254 a black shadow with alpha (255 - i) * 16; 224-239 white
"smoke" with alpha (i - 224) / 15; the civ colour is i < 16, or i < 64 and even, drawn as a separate tinted layer.
"""
from __future__ import annotations

import struct
from pathlib import Path

DIRECTIONS = ["SW", "S", "SE", "E", "NE", "N", "NW", "W"]   # the FLC's row order (AnimationManager.cs)


class Flc:
    """A decoded FLC: `frames[direction][frame]` are bytes of palette indexes, `palette` 256 (r, g, b)."""

    def __init__(self, path: Path):
        b = self._b = Path(path).read_bytes()
        _, magic, self.nframes, self.width, self.height, _, _, self.speed = struct.unpack_from("<IHHHHHHI", b, 0)
        if magic != 0xAF12:
            raise ValueError(f"{path}: not an FLC (magic {magic:#x})")
        self.first = struct.unpack_from("<I", b, 0x50)[0]
        self.num_anims, self.fpa = struct.unpack_from("<HH", b, 0x60)
        self.x_offset, self.y_offset, self.orig_w, self.orig_h = struct.unpack_from("<HHHH", b, 0x64)
        if self.num_anims == 0:                      # a plain FLC
            self.num_anims, self.fpa = 1, self.nframes
        self.palette: list[tuple[int, int, int]] = [(0, 0, 0)] * 256
        self.frames: list[list[bytes]] = []
        self._decode()

    def _decode(self) -> None:
        b, w, h, off = self._b, self.width, self.height, self.first
        cur = bytearray(w * h)
        for _ in range(self.num_anims):
            row = []
            for f in range(self.fpa + 1):            # + the ring frame
                if off + 16 > len(b):
                    break
                size, _, chunks = struct.unpack_from("<IHH", b, off)
                at = off + 16
                for _ in range(chunks):
                    length, kind = struct.unpack_from("<IH", b, at)
                    if kind in (4, 11):
                        self._palette(at + 6, six_bit=kind == 11)
                    elif kind == 15:
                        self._brun(at + 6, cur)
                    elif kind == 7:
                        self._delta(at + 6, cur)
                    elif kind == 16:
                        cur[:] = b[at + 6:at + 6 + w * h]
                    elif kind == 13:
                        cur[:] = bytes(w * h)
                    at += length
                off += size
                if f < self.fpa:
                    row.append(bytes(cur))
            self.frames.append(row)

    def _palette(self, p: int, six_bit: bool) -> None:
        b, idx = self._b, 0
        (packets,) = struct.unpack_from("<H", b, p)
        p += 2
        for _ in range(packets):
            idx += b[p]
            count = b[p + 1] or 256
            p += 2
            for _ in range(count):
                r, g, bl = b[p], b[p + 1], b[p + 2]
                p += 3
                self.palette[idx] = (r << 2, g << 2, bl << 2) if six_bit else (r, g, bl)
                idx += 1

    def _brun(self, p: int, cur: bytearray) -> None:
        b, w = self._b, self.width
        for y in range(self.height):
            p += 1                                   # the obsolete packet count
            x, base = 0, y * w
            while x < w:
                (n,) = struct.unpack_from("<b", b, p)
                p += 1
                if n > 0:                            # the next byte, n times
                    cur[base + x:base + x + n] = bytes([b[p]]) * n
                    p += 1
                elif n < 0:                          # -n bytes as they are
                    n = -n
                    cur[base + x:base + x + n] = b[p:p + n]
                    p += n
                else:
                    raise ValueError("a zero BYTE_RUN count")
                x += n

    def _delta(self, p: int, cur: bytearray) -> None:
        b, w = self._b, self.width
        (lines,) = struct.unpack_from("<H", b, p)
        p, y = p + 2, 0
        for _ in range(lines):
            while True:
                (word,) = struct.unpack_from("<H", b, p)
                p += 2
                if word & 0xC000 == 0xC000:          # skip lines
                    y += 0x10000 - word
                elif word & 0xC000 == 0x8000:        # the line's last pixel
                    cur[y * w + w - 1] = word & 0xFF
                else:
                    packets = word
                    break
            x = 0
            for _ in range(packets):
                x += b[p]
                (n,) = struct.unpack_from("<b", b, p + 1)
                p += 2
                base = y * w + x
                if n > 0:                            # n words as they are
                    cur[base:base + 2 * n] = b[p:p + 2 * n]
                    p += 2 * n
                    x += 2 * n
                elif n < 0:                          # one word, -n times
                    cur[base:base - 2 * n] = b[p:p + 2] * -n
                    p += 2
                    x -= 2 * n
            y += 1


# ---- one unit's art as browser sheets ----

def is_civ_color(i: int) -> bool:
    return i < 16 or (i < 64 and i % 2 == 0)


def base_rgba(i: int, pal: list[tuple[int, int, int]]) -> tuple[int, int, int, int]:
    if is_civ_color(i):
        return (0, 0, 0, 0)                          # the tint layer draws these
    if 224 <= i <= 239:
        return (255, 255, 255, round((i - 224) / 15 * 255))
    if i >= 240:
        return (0, 0, 0, (255 - i) * 16)
    r, g, b = pal[i]
    return (r, g, b, 255)


def read_ini(folder: Path) -> dict[str, dict[str, str]]:
    """The unit's INI: `<folder>.INI`, else the folder's only INI (OpenCiv3 itself fails on the Catapult and the
    Galleon, whose INIs are named otherwise)."""
    inis = sorted(p for p in folder.iterdir() if p.suffix.lower() == ".ini")
    path = next((p for p in inis if p.stem.lower() == folder.name.lower()), inis[0] if inis else None)
    if path is None:
        raise FileNotFoundError(f"no INI in {folder}")
    sections: dict[str, dict[str, str]] = {}
    cur = None
    for raw in path.read_text(encoding="latin-1").splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            cur = sections.setdefault(line[1:-1].strip().upper(), {})
        elif "=" in line and cur is not None:
            k, v = line.split("=", 1)
            cur[k.strip().upper()] = v.strip()
    return sections


def _find(folder: Path, name: str) -> Path | None:
    want = name.replace("\\", "/").lower()
    return next((p for p in folder.iterdir() if p.name.lower() == want), None)


def _bbox(frame: bytes, w: int, h: int, clear: bytes) -> tuple[int, int, int, int] | None:
    m = frame.translate(clear)
    x0, y0, x1, y1 = w, h, -1, -1
    for y in range(h):
        row = m[y * w:(y + 1) * w]
        if 1 in row:
            x0, x1 = min(x0, row.index(1)), max(x1, w - 1 - row[::-1].index(1))
            y0, y1 = min(y0, y), y
    return None if x1 < 0 else (x0, y0, x1 + 1, y1 + 1)


def save_small(im, path: Path) -> None:
    """An RGBA image as an indexed PNG when it has 256 colours or fewer (the FLC's palette makes it so: a third of
    the size), else as it is. Lossless either way."""
    from PIL import Image

    colors = im.getcolors(256)
    if colors is None:
        im.save(path, optimize=True)
        return
    index = {c: i for i, (_, c) in enumerate(colors)}
    out = Image.new("P", im.size)
    data = im.get_flattened_data() if hasattr(im, "get_flattened_data") else im.getdata()
    out.putdata([index[c] for c in data])
    out.putpalette([v for _, c in colors for v in c], rawmode="RGBA")
    out.save(path, optimize=True)


# What the play page shows: an idle unit holds its last DEFAULT frame (its last FORTIFY frame when fortified), as
# OpenCiv3 draws it; RUN plays once as it moves; in a battle both sides play ATTACK1 each round and the loser DEATH
# (MapUnit_Actions.cs). (INI key, last frame only)
ACTIONS = (("DEFAULT", True), ("FORTIFY", True), ("RUN", False), ("ATTACK1", False), ("DEATH", False))


def convert_unit(folder: Path, out: Path, slug: str, actions=ACTIONS) -> dict:
    """Write `<slug>.png` (the unit without its civ colour) and `<slug>.mask.png` (its civ-colour pixels, grey the
    artist's shade) into `out`: one cell size for every frame, a row per action and direction (DIRECTIONS order), a
    column per frame. Returns the manifest entry. Draw a cell with its `anchor` on the tile centre, at zoom 1."""
    from PIL import Image

    ini = read_ini(folder).get("ANIMATIONS", {})
    loaded: dict[Path, Flc] = {}
    plan = []                                        # (action, flc, last frame only)
    for key, last in actions:
        name = ini.get(key)
        if not name:                                 # an empty entry falls back to DEFAULT (AnimationManager.cs)
            continue
        path = _find(folder, name)
        if path is None:
            continue
        plan.append((key.lower(), loaded.setdefault(path, Flc(path)), last))
    if not plan or plan[0][0] != "default":
        raise FileNotFoundError(f"{folder}: no DEFAULT animation")
    # one cell for every frame: the union of their boxes in the original canvas
    x0 = y0 = 10 ** 9
    x1 = y1 = -10 ** 9
    for _, flc, last in plan:
        clear = bytes(int(base_rgba(i, flc.palette)[3] > 0 or is_civ_color(i)) for i in range(256))
        for d in range(flc.num_anims):
            for fr in flc.frames[d][-1:] if last else flc.frames[d]:
                if bb := _bbox(fr, flc.width, flc.height, clear):
                    x0, y0 = min(x0, flc.x_offset + bb[0]), min(y0, flc.y_offset + bb[1])
                    x1, y1 = max(x1, flc.x_offset + bb[2]), max(y1, flc.y_offset + bb[3])
    cw, ch = x1 - x0, y1 - y0
    rows, out_actions, cells = 0, {}, []
    for key, flc, last in plan:
        frames = 1 if last else flc.fpa
        out_actions[key] = {"row": rows, "frames": frames, "ms": flc.speed}
        for d in range(8):
            for c, fr in enumerate(flc.frames[d][-1:] if last else flc.frames[d]):
                cells.append((rows + d, c, flc, fr))
        rows += 8
    out_actions.setdefault("fortify", out_actions["default"])
    out_actions.setdefault("run", out_actions["default"])
    out_actions.setdefault("attack1", out_actions["default"])     # no DEATH: the unit fades out
    cols = max(a["frames"] for a in out_actions.values())
    base, mask = Image.new("RGBA", (cols * cw, rows * ch)), Image.new("LA", (cols * cw, rows * ch))
    civ = False
    luts: dict[int, tuple[list, list]] = {}
    for r, c, flc, fr in cells:
        if id(flc) not in luts:
            luts[id(flc)] = ([base_rgba(i, flc.palette) for i in range(256)],
                             [(max(flc.palette[i]), 255) if is_civ_color(i) else (0, 0) for i in range(256)])
        to_rgba, to_mask = luts[id(flc)]
        sx, sy = x0 - flc.x_offset, y0 - flc.y_offset
        data = Image.frombytes("P", (flc.width, flc.height), fr).crop((sx, sy, sx + cw, sy + ch)).tobytes()
        cell, mcell = Image.new("RGBA", (cw, ch)), Image.new("LA", (cw, ch))
        cell.putdata([to_rgba[v] for v in data])
        mvals = [to_mask[v] for v in data]
        civ = civ or any(a for _, a in mvals)
        mcell.putdata(mvals)
        base.paste(cell, (c * cw, r * ch))
        mask.paste(mcell, (c * cw, r * ch))
    out.mkdir(parents=True, exist_ok=True)
    save_small(base, out / f"{slug}.png")
    if civ:
        save_small(mask.convert("RGBA"), out / f"{slug}.mask.png")
    orig = plan[0][1].orig_w // 2, plan[0][1].orig_h // 2
    return {"sheet": f"units/{slug}.png", "mask": f"units/{slug}.mask.png" if civ else None, "w": cw, "h": ch,
            "anchor": [orig[0] - x0, orig[1] - y0], "directions": DIRECTIONS, "actions": out_actions}
