"""The OpenCiv3 client's art, made ready for the browser's play page (docs/play.md, section 6).

    python -m agentenv_openciv3.webart <C7 dir> <out dir>

<C7 dir> is the client's `C7` directory, with its `Assets` (C7-Game/Assets, the community art) and `Fonts`. The
client image runs this when it is built (Dockerfile), so the art never leaves that image. Out comes `manifest.json`
and the sheets it names:

- terrain, rivers, improvements, resources, borders, fog, cities, the selection cursor and the disorder fire: the
  client's PNGs as they are (only the variants this art set really has: many of its variant sheets are identical);
- units: from each unit's FLC animations, its idle and fortified poses and its moving (`Run`) animation, as one
  sheet with a row per action and direction, plus a mask of its civ-colour pixels to tint for each civ (civ3flc.py);
- the advisor screens' and the city screen's art (SCREENS: backgrounds, heads, buttons, boxes, icons), which the
  page loads when a screen opens, and from the client's ruleset (Lua/civ3/ruleset.json) the science advisor's tech
  tree as the client lays it out, the units' stats and icons and the buildings' icons (`techs`, `unit_info`,
  `building_icons`: _ruleset);
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
    # the HUD: the status scroll and its next-turn dome, the minimap frame, the unit order buttons (normal, hover,
    # pressed)
    "status_box": "interface/box right color.png", "next_turn": "interface/nextturn states color.png",
    "minimap_box": "interface/box left color.png", "buttons": "interface/NormButtons.png",
    "buttons_hover": "interface/rolloverbuttons.png", "buttons_pressed": "interface/highlightedbuttons.png",
    # the city screen's yield icons, which the map draws on the worked tiles while the screen is open
    "yield_icons": "city screen/CityIcons.png",
}
# The advisors' and the city screen's art (screens.js), from the client's Assets/Art: name -> path. The page loads
# these when a screen opens (the backgrounds are large), so the manifest lists them apart from the map's sheets.
SCREENS = {
    "domestic": "Advisors/domestic.png", "foreign": "Advisors/foreign.png", "foreign_tab": "Advisors/foreignTAB.png",
    "science_0": "Advisors/science_ancient.png", "science_1": "Advisors/science_middle.png",
    "science_2": "Advisors/science_industrial_new.png", "science_3": "Advisors/science_modern.png",
    "dialogbox": "Advisors/dialogbox.png", "domestic_button": "Advisors/domesticBUTTON.png",
    "plusminus": "Advisors/domestic_plusminus.png", "techboxes": "Advisors/techboxes.png",
    "non_required": "Advisors/non_required.png", "exit": "exitBox-backgroundStates.png",
    "head_domestic": "SmallHeads/popupDOMESTIC.png", "head_foreign": "SmallHeads/popupFOREIGN.png",
    "head_science": "SmallHeads/popupSCIENCE.png", "pop_heads": "SmallHeads/popHeads.png",
    "science_nav": "Tech Chooser/scienceNAV.png", "unit_small": "Civilopedia/icons/units/unit_small.png",
    "city_bg": "city screen/background.png", "city_buttons": "city screen/cityMgmtButtons.png",
    "prod_button": "city screen/ProdButton.png", "prod_queue": "city screen/ProductionQueueBox.png",
    "buildings_small": "city screen/buildings-small.png", "buildings_large": "city screen/buildings-large.png",
    "luxury_icons": "city screen/luxuryicons_small.png",
    "units_32": "Units/units_32.png", "popup": "popupborders.png", "xo": "X-o_ALLstates-sprite.png",
    "orbs": "buttonsFINAL.png",
    "tech_placeholder": "Tech Chooser/Icons/placeholder.png",
}
# The tech icons the standalone client has, by tech id (Lua/standalone/textures.lua); every other tech shows the
# placeholder. Each is a screen sheet "tech_<id>".
TECH_ICONS = {"tech-2": "Masonry", "tech-3": "Alphabet", "tech-5": "TheWheel", "tech-6": "WarriorCode",
              "tech-7": "CeremonialBurial", "tech-10": "Mysticism", "tech-13": "Code of Laws", "tech-14": "Literature",
              "tech-15": "MapMaking", "tech-16": "HorsebackRiding"}
SCREENS.update({f"tech_{t}": f"Tech Chooser/Icons/{n}.png" for t, n in TECH_ICONS.items()})
ERAS = ["ERAS_Ancient_Times", "ERAS_Middle_Ages", "ERAS_Industrial_Age", "ERAS_Modern_Era"]
# A terraform's unit-command button on interface/NormButtons.png (the `buttons` sheet): its 32x32 cell (column, row),
# which the science advisor shows on the tech that allows it (Lua/civ3/textures/unit_control.lua).
TERRAFORM_BUTTON = {"unit_build_fortress": [0, 3], "unit_build_railroad": [7, 2], "unit_plant_forest": [5, 3],
                    "unit_build_airfield": [1, 4], "unit_build_radar_tower": [2, 4], "unit_build_outpost": [3, 4],
                    "unit_build_barricade": [4, 4]}
# Unit type -> art folder under Assets/Art/Units, as the client's standalone ruleset maps them
# (C7/Lua/standalone/ruleset.lua, as patches/0018 extends it: the later and unique units borrow the folder of the
# unit they replace or descend from). Types without art are drawn as the page's own markers.
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
    **dict.fromkeys(("Rifleman", "Infantry", "Guerilla", "Marine", "Paratrooper", "Mech Infantry", "TOW Infantry",
                     "Modern Paratrooper", "Musketeer"), "Generic Musketman 1600"),
    **dict.fromkeys(("Artillery", "Radar Artillery", "Hwach'a"), "French Artillery 17th C"),
    **dict.fromkeys(("Tank", "Modern Armor", "Cossack", "Sipahi", "Panzer"), "1750 British Dragoon"),
    **dict.fromkeys(("Ironclad", "Destroyer", "Cruiser", "Battleship", "AEGIS Cruiser", "Submarine", "Man-O-War"),
                    "HeavyFrigate"),
    "Transport": "East_Indiaman1",
    **dict.fromkeys(("Jaguar Warrior", "Enkidu Warrior"), "Tribal Mediterranean Warrior"),
    **dict.fromkeys(("Bowman", "Javelin Thrower"), "Chichimeca Archer"),
    **dict.fromkeys(("Hoplite", "Impi"), "European Spearman"), "Numidian Mercenary": "Medieval Spearman",
    "Legionary": "Serbian Regular Swordsman", **dict.fromkeys(("Immortals", "Gallic Swordsman"), "European Swordsman"),
    **dict.fromkeys(("War Chariot", "Three-Man Chariot"), "thracian chariot"), "Mounted Warrior": "Serbian Horseman",
    "Swiss Mercenary": "Elf Spearman", "Berserk": "Gothic Swordsman",
    **dict.fromkeys(("Rider", "Samurai", "War Elephant", "Keshik", "Ansar Warrior", "Conquistador"),
                    "Medieval European Horse Spearman"),
    "Chasqui Scout": "Euro Scout", "Dromon": "Bireme", "Carrack": "Cog",
}
FONTS = ("NotoSans-Regular.ttf", "NotoSans-Bold.ttf", "NotoSans-Italic.ttf", "LICENSE-NotoSans.txt")


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
    sheets, screens = {}, {}
    for names, folder, into in ((SHEETS, "sheets", sheets), (SCREENS, "screens", screens)):
        (out / folder).mkdir(exist_ok=True)
        for name, rel in names.items():
            src = art / rel
            if not src.is_file():
                raise FileNotFoundError(f"the art has no {rel}")
            _small_png(src, out / folder / f"{name}.png")
            into[name] = f"{folder}/{name}.png"
    _techboxes_fit(art / SCREENS["techboxes"], out / "screens" / "techboxes_fit.png")
    screens["techboxes_fit"] = "screens/techboxes_fit.png"
    rules = _ruleset(c7)
    units = {}
    (out / "units").mkdir()
    done: dict[str, dict] = {}
    for unit, folder in UNIT_ART.items():
        if folder not in done:
            done[folder] = civ3flc.convert_unit(art / "Units" / folder, out / "units", slug(folder))
        units[unit] = done[folder]
    (out / "fonts").mkdir()
    for f in FONTS:
        if (c7 / "Fonts" / f).is_file():
            shutil.copyfile(c7 / "Fonts" / f, out / "fonts" / f)
    digest = hashlib.sha256()
    for p in sorted(out.rglob("*")):
        if p.is_file():
            digest.update(p.relative_to(out).as_posix().encode() + p.read_bytes())
    fonts = sorted(f for f in FONTS if f.endswith(".ttf") and (out / "fonts" / f).is_file())
    manifest = {"version": 1, "id": digest.hexdigest()[:12], "pinned": _pinned(c7), "sheets": sheets,
                "units": units, "fonts": fonts, "screens": screens, **rules}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    return manifest


def _ruleset(c7: Path) -> dict:
    """From the client's ruleset (Lua/civ3/ruleset.json, as the standalone mode trims it to the units with art):

    - `techs`: the science advisor's tree (ScienceAdvisor.cs, TechBox.cs): per tech its id, name, era (0-3), the box's
      top-left in the 1024x768 screen (x, y), whether the era needs it (`required`), its prerequisites' ids, and what
      sizes its box: the buildings it allows or makes obsolete (`buildings`, a count: the client draws no icon for
      them), the units it allows (`units`, names; a civ counts those it can build) and the terraforms it allows
      (`terraforms`, their button's cell on the `buttons` sheet); `icon` is its screen sheet;
    - `unit_info`: per unit with art its attack, defence, moves and bombard (the production list's "Warrior 1.1.1"),
      its icon on the `units_32` sheet (`icon`, and `icon_era` by era where it changes) and, when not every civ can
      build it, the civs that can (`civs`);
    - `building_icons`: per building its row on the `buildings_small` and `buildings_large` sheets."""
    path = c7 / "Lua" / "civ3" / "ruleset.json"
    if not path.is_file():
        raise FileNotFoundError(f"no ruleset at {path}: the client's Lua/civ3/ruleset.json is missing")
    r = json.loads(path.read_text(encoding="utf-8"))
    all_civs = {c["name"] for c in r.get("civilizations", []) if not c.get("isBarbarian")}
    protos = [u for u in r.get("unitPrototypes", []) if u["name"] in UNIT_ART]
    buildings = r.get("buildings", [])
    techs = []
    for t in r.get("techs", []):
        tid = t["id"]
        tf = [TERRAFORM_BUTTON[k] for x in r.get("terraForms", []) if x.get("requiredTech") == tid
              for k in [x.get("buttonTexture", "").rsplit(".", 1)[-1]] if k in TERRAFORM_BUTTON]
        era = t.get("eraCivilopediaName")
        techs.append({
            "id": tid, "name": t["name"], "era": ERAS.index(era) if era in ERAS else 0,
            "x": t.get("x", 0), "y": t.get("y", 0), "required": bool(t.get("requiredForEraAdvancement", True)),
            "prereqs": t.get("prerequisites", []),
            "buildings": sum((b.get("requiredTech") == tid) + (b.get("renderedObsoleteBy") == tid) for b in buildings),
            "units": [u["name"] for u in protos if u.get("requiredTech") == tid], "terraforms": tf,
            "icon": f"tech_{tid}" if tid in TECH_ICONS else "tech_placeholder"})
    units = {}
    for u in protos:
        thumb = (u.get("art") or {}).get("thumbnailArt") or {}
        info = {"a": u.get("attack", 0), "d": u.get("defense", 0), "m": u.get("movement", 1), "b": u.get("bombard", 0),
                "icon": thumb.get("defaultIndex", 0)}
        era = {str(ERAS.index(k)): v for k, v in (thumb.get("variations") or {}).items() if k in ERAS}
        if era:
            info["icon_era"] = era
        civs = set(u.get("producibleBy") or [])
        if civs and all_civs - civs:
            info["civs"] = sorted(civs)
        units[u["name"]] = info
    icons = {b["name"]: b["iconRowIndex"] for b in buildings if b.get("iconRowIndex") is not None}
    return {"techs": techs, "unit_info": units, "building_icons": icons}


# The science advisor's tech boxes (Lua/civ3/textures/tech_boxes.lua): a box's crop size by its key, and the x offset
# of each state's boxes on techboxes.png.
TECHBOX_SIZES = {"small": (106, 82), "medium": (163, 82), "long": (188, 82), "large": (163, 106)}
TECHBOX_STATES = {"known": 0, "in_progress": 189, "possible": 378, "blocked": 567}


def _techboxes_fit(src: Path, dst: Path) -> None:
    """Every tech box the science advisor draws, in each state, as one sheet: a slot of 190x108 per box, a column
    per size (TECHBOX_SIZES' order) and a row per state (TECHBOX_STATES' order), the box at the slot's top-left.

    This art draws only the ancient era's small boxes: the larger ones and the other eras' are empty, so the client
    shows those techs on bare parchment. Each box here is the ancient small box of its state stretched to the size,
    its frame kept: of the 98x64 frame at (2, 8) of the crop, columns 0..48 (the border and the colour column) and
    92..98 and rows 0..24 (the border and the colour band) and 58..64 stay as they are; columns 48..92 and rows 24..58
    stretch."""
    from PIL import Image

    with Image.open(src) as im:
        sheet = im.convert("RGBA")
    out = Image.new("RGBA", (190 * len(TECHBOX_SIZES), 108 * len(TECHBOX_STATES)), (0, 0, 0, 0))
    for row, sx in enumerate(TECHBOX_STATES.values()):
        frame = sheet.crop((1 + sx + 2, 1 + 8, 1 + sx + 100, 1 + 72))
        for col, (w, h) in enumerate(TECHBOX_SIZES.values()):
            fw, fh = w - 8, h - 18
            for x0, x1, dx0, dx1 in ((0, 48, 0, 48), (48, 92, 48, fw - 6), (92, 98, fw - 6, fw)):
                for y0, y1, dy0, dy1 in ((0, 24, 0, 24), (24, 58, 24, fh - 6), (58, 64, fh - 6, fh)):
                    if dx1 > dx0 and dy1 > dy0:
                        part = frame.crop((x0, y0, x1, y1)).resize((dx1 - dx0, dy1 - dy0))
                        out.alpha_composite(part, (col * 190 + 2 + dx0, row * 108 + 8 + dy0))
    civ3flc.save_small(out, dst)


def _small_png(src: Path, dst: Path) -> None:
    """The sheet as the smallest lossless PNG: indexed when it has 256 colours or fewer, else re-encoded."""
    from PIL import Image

    with Image.open(src) as im:
        civ3flc.save_small(im.convert("RGBA"), dst)
    if dst.stat().st_size > src.stat().st_size:
        shutil.copyfile(src, dst)


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
    print(f"{a.out}: {len(m['sheets'])} sheets, {len(m['screens'])} screen sheets, {len(m['units'])} units, "
          f"{len(m['techs'])} techs, {size / 1e6:.1f} MB (art {m['id']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
