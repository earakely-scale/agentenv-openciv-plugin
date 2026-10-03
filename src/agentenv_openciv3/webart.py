"""The OpenCiv3 client's art, made ready for the browser's play page (docs/play.md, section 6).

    python -m agentenv_openciv3.webart <C7 dir> <out dir>

<C7 dir> is the client's `C7` directory, with its `Assets` (C7-Game/Assets, the community art) and `Fonts`. The
client image runs this when it is built (Dockerfile), so the art never leaves that image. Out comes `manifest.json`
and the sheets it names:

- terrain, rivers, improvements, resources, borders, fog, cities, the selection cursor and the disorder fire: the
  client's PNGs as they are (only the variants this art set really has: many of its variant sheets are identical);
- units: each unit's idle (`Default`) and moving (`Run`) animations from its FLC, as one sheet per animation, a row
  per direction, plus a mask of the civ-colour pixels for tinting each civ's units;
- the Noto Sans fonts the client's labels use, with their licence.

The play page draws them the way the client does (map.js, ArtPainter); without them it draws its own map.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

from . import civ3flc

# The sheets the play page draws, from the client's Assets/Art: name -> path. Variants the client would pick (the
# forest sheet by base terrain, hills by vegetation, irrigation by terrain, delta rivers) are pixel-identical in this
# art set, so one of each is enough; the manifest lists the duplicates' names too, so a later art drop can split them.
SHEETS = {
    **{f: f"Terrain/{f}.png" for f in ("xtgc", "xpgc", "xdgc", "xdpc", "xdgp", "wCSO", "wSSS", "wOOO")},
    "hills": "Terrain/xhills.png", "mountains": "Terrain/Mountains.png", "volcanos": "Terrain/Volcanos.png",
    "forests": "Terrain/grassland forests.png", "marsh": "Terrain/marsh.png", "rivers": "Terrain/mtnRivers.png",
    "roads": "Terrain/roads.png", "railroads": "Terrain/railroads.png", "irrigation": "Terrain/irrigation.png",
    "pollution": "Terrain/pollution.png", "craters": "Terrain/craters.png", "buildings": "Terrain/TerrainBuildings.png",
    "tnt": "Terrain/tnt.png", "territory": "Terrain/Territory.png", "fog": "Terrain/FogOfWar.png",
    "resources": "resources.png", "cities": "Cities/rMIDEAST.png", "walls": "Cities/MIDEASTWALL.png",
    "ruins": "Cities/DESTROY.png", "city_icons": "Cities/city icons.png", "cursor": "Animations/Cursor.png",
    "disorder": "Animations/DisorderDefault.png", "led": "interface/MovementLED.png",
}
# Unit type -> art folder under Assets/Art/Units, as the client's standalone ruleset maps them
# (vendor/OpenCiv3/C7/Lua/standalone/ruleset.lua). Types without art are drawn as the page's own markers.
UNIT_ART = {
    "Settler": "Carthaginian Settler", "Worker": "Carthaginian Worker", "Scout": "Euro Scout",
    "Explorer": "German Explorer", "Warrior": "Tribal Mediterranean Warrior", "Archer": "Chichimeca Archer",
    "Spearman": "European Spearman", "Swordsman": "European Swordsman", "Chariot": "thracian chariot",
    "Horseman": "Serbian Horseman", "Pikeman": "Elf Spearman", "Longbowman": "Longbowman",
    "Musketman": "Generic Musketman 1600", "Knight": "Medieval European Horse Spearman",
    "Cavalry": "1750 British Dragoon", "Catapult": "Dark Ages Onager", "Cannon": "French Artillery 17th C",
    "Galley": "Bireme", "Caravel": "Cog", "Frigate": "HeavyFrigate", "Galleon": "East_Indiaman1",
    "Privateer": "Pirate Ship", "Medieval Infantry": "Gothic Swordsman", "Trebuchet": "Medieval Trebuchet",
    "Crusader": "Black Hospitaller Swordsman", "Ancient Cavalry": "Oscan Companion", "Curragh": "MinoanGalley",
}
FONTS = ("NotoSans-Regular.ttf", "NotoSans-Bold.ttf", "LICENSE-NotoSans.txt")
ANIMATIONS = ("Default", "Run")


def find() -> Path | None:
    """The converted art the env serves: OPENCIV_WEB_ART, else `webart` in the client's home (the client image puts
    it there); None when there is none, and the play page draws its own map."""
    from . import client
    for d in (os.environ.get("OPENCIV_WEB_ART"), client.home() / "webart"):
        if d and (Path(d) / "manifest.json").is_file():
            return Path(d)
    return None


def slug(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_")


def convert(c7: Path, out: Path) -> dict:
    """Write the sheets, units and fonts from the client tree `c7` into `out`; returns the manifest."""
    art = c7 / "Assets" / "Art"
    if not art.is_dir():
        raise FileNotFoundError(f"no art at {art}: the client's Assets are missing")
    shutil.rmtree(out, ignore_errors=True)
    (out / "sheets").mkdir(parents=True)
    sheets = {}
    for name, rel in SHEETS.items():
        src = art / rel
        if not src.is_file():
            raise FileNotFoundError(f"the art has no {rel}")
        shutil.copyfile(src, out / "sheets" / f"{name}.png")
        sheets[name] = f"sheets/{name}.png"
    units = {}
    (out / "units").mkdir()
    for unit, folder in UNIT_ART.items():
        units[unit] = civ3flc.convert_unit(art / "Units" / folder, out / "units", slug(unit), ANIMATIONS)
    (out / "fonts").mkdir()
    for f in FONTS:
        if (c7 / "Fonts" / f).is_file():
            shutil.copyfile(c7 / "Fonts" / f, out / "fonts" / f)
    digest = hashlib.sha256()
    for p in sorted(out.rglob("*")):
        if p.is_file():
            digest.update(p.relative_to(out).as_posix().encode() + p.read_bytes())
    manifest = {"version": 1, "id": digest.hexdigest()[:12], "pinned": _pinned(c7), "sheets": sheets,
                "units": units, "fonts": sorted(f for f in FONTS if f.endswith(".ttf") and (out / "fonts" / f).is_file())}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    return manifest


def _pinned(c7: Path) -> str | None:
    p = c7 / "Assets" / ".pinned"
    return p.read_text().strip() if p.is_file() else None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("c7", type=Path, help="the client's C7 directory (with Assets/ and Fonts/)")
    p.add_argument("out", type=Path, help="where to write manifest.json and the sheets")
    a = p.parse_args(argv)
    m = convert(a.c7, a.out)
    size = sum(f.stat().st_size for f in a.out.rglob("*") if f.is_file())
    print(f"{a.out}: {len(m['sheets'])} sheets, {len(m['units'])} units, {size / 1e6:.1f} MB (art {m['id']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
