"""The client's art for the play page (docs/play.md, section 6): the FLC decoder and unit sheets (civ3flc.py), the
conversion (webart.py) and the env's /play/art route, on small synthetic art."""

import json
import struct
from pathlib import Path

import pytest
from PIL import Image
from starlette.requests import Request

from agentenv_openciv3 import civ3flc, webart

W, H, FRAMES = 6, 4, 2
CANVAS = 240
OFFSET = (117, 115)            # the frame's place in the 240x240 canvas: the tile centre (120, 120) falls at (3, 5)


def flc_bytes(frames: list[list[bytes]], speed: int = 100) -> bytes:
    """A Civ3 unit FLC: 8 directions of `frames`, each frame W*H palette indexes as one BYTE_RUN, plus a ring frame
    per direction; the palette in the first frame (index i is (i, 255 - i, 7))."""
    def chunk(kind: int, data: bytes) -> bytes:
        return struct.pack("<IH", 6 + len(data), kind) + data

    def brun(px: bytes) -> bytes:
        return b"".join(bytes([1, (-W) & 0xFF]) + px[y * W:(y + 1) * W] for y in range(H))

    def frame(subs: list[bytes]) -> bytes:
        body = b"".join(subs)
        return struct.pack("<IHH8x", 16 + len(body), 0xF1FA, len(subs)) + body

    palette = struct.pack("<H", 1) + bytes([0, 0]) + b"".join(bytes([i, 255 - i, 7]) for i in range(256))
    body = b""
    for d, frs in enumerate(frames):
        for f, px in enumerate(frs + [frs[0]]):
            subs = ([chunk(4, palette)] if d == 0 and f == 0 else []) + [chunk(15, brun(px))]
            body += frame(subs)
    header = bytearray(128)
    struct.pack_into("<IHHHHHHI", header, 0, 128 + len(body), 0xAF12, 8 * FRAMES, W, H, 8, 0, speed)
    struct.pack_into("<I", header, 0x50, 128)
    struct.pack_into("<HHHHHHH", header, 0x60, 8, FRAMES, *OFFSET, CANVAS, CANVAS, speed * FRAMES)
    return bytes(header) + body


def pixels(d: int, f: int) -> bytes:
    """Direction d, frame f: transparent (255) round a 2x2 block of colour 100 + d, one civ-colour pixel (index 2),
    one shadow pixel (250); frame f moves the block f pixels right."""
    px = bytearray([255] * (W * H))
    for y in (1, 2):
        for x in (1 + f, 2 + f):
            px[y * W + x] = 100 + d
    px[3 * W + 1] = 2
    px[3 * W + 2] = 250
    return bytes(px)


def unit_folder(root: Path, name: str = "Test Unit", fortify: bool = False) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    frames = [[pixels(d, f) for f in range(FRAMES)] for d in range(8)]
    (folder / "TestDefault.flc").write_bytes(flc_bytes(frames))
    (folder / "TestRun.flc").write_bytes(flc_bytes(frames, speed=40))
    (folder / f"{name}.INI").write_text(
        f"[Animations]\nDEFAULT=TestDefault.flc\nRUN=TestRun.flc\nFORTIFY={'TestDefault.flc' if fortify else ''}\n")
    return folder


def test_the_flc_decoder_reads_civ3_unit_animations(tmp_path):
    path = unit_folder(tmp_path) / "TestDefault.flc"
    flc = civ3flc.Flc(path)
    assert (flc.num_anims, flc.fpa, flc.width, flc.height, flc.speed) == (8, FRAMES, W, H, 100)
    assert (flc.x_offset, flc.y_offset, flc.orig_w, flc.orig_h) == (*OFFSET, CANVAS, CANVAS)
    assert flc.palette[100] == (100, 155, 7)
    assert [[f for f in d] for d in flc.frames] == [[pixels(d, f) for f in range(FRAMES)] for d in range(8)]


def test_a_unit_becomes_a_sheet_a_civ_colour_mask_and_its_manifest(tmp_path):
    entry = civ3flc.convert_unit(unit_folder(tmp_path), tmp_path / "out", "test_unit")
    # the cell is the union of the frames kept (the last idle frame, every run frame): x 1..3, y 1..3
    assert (entry["w"], entry["h"]) == (3, 3)
    assert entry["anchor"] == [120 - (OFFSET[0] + 1), 120 - (OFFSET[1] + 1)]
    assert entry["directions"] == ["SW", "S", "SE", "E", "NE", "N", "NW", "W"]
    # idle: the last DEFAULT frame; no FORTIFY or ATTACK1 in the INI: DEFAULT's; RUN: every frame; no DEATH: none
    default = {"row": 0, "frames": 1, "ms": 100}
    assert entry["actions"] == {"default": default, "run": {"row": 8, "frames": 2, "ms": 40}, "fortify": default,
                                "attack1": default}
    sheet = Image.open(tmp_path / "out" / "test_unit.png").convert("RGBA")
    mask = Image.open(tmp_path / "out" / "test_unit.mask.png").convert("RGBA")
    assert sheet.size == mask.size == (2 * 3, 16 * 3)
    # direction NE (row 4), idle (its last frame, f = 1): the block at x 1..2 of the cell, colour 104
    row = 4 * 3
    assert sheet.getpixel((1, row)) == (104, 151, 7, 255) and sheet.getpixel((0, row)) == (0, 0, 0, 0)
    assert sheet.getpixel((0, row + 2)) == (0, 0, 0, 0)                  # the civ-colour pixel is the mask's
    assert sheet.getpixel((1, row + 2)) == (0, 0, 0, 5 * 16)             # shadow 250: black, alpha (255 - i) * 16
    assert mask.getpixel((0, row + 2)) == (253, 253, 253, 255)           # grey: the palette entry's brightest
    assert mask.getpixel((1, row))[3] == 0
    # run, direction SW, frame 0: the block at x 0..1 of the cell
    assert sheet.getpixel((0, 8 * 3)) == (100, 155, 7, 255)


def test_a_unit_with_a_fortify_animation_gets_its_own_rows(tmp_path):
    entry = civ3flc.convert_unit(unit_folder(tmp_path, fortify=True), tmp_path / "out", "u")
    assert entry["actions"]["fortify"] == {"row": 8, "frames": 1, "ms": 100}


def test_an_ini_named_otherwise_is_still_found(tmp_path):
    folder = unit_folder(tmp_path, "Catapult Art")
    (folder / "Catapult Art.INI").rename(folder / "Bizantine Onager.INI")   # as the Catapult's art ships
    assert civ3flc.convert_unit(folder, tmp_path / "out", "c")["actions"]["default"]["frames"] == 1


# A ruleset as the client's Lua/civ3/ruleset.json has it, cut down: two techs, a unit two civs can build, a unit the
# standalone mode drops (no art), buildings and a terraform.
RULESET = {
    "civilizations": [{"name": "Rome"}, {"name": "Greece"}, {"name": "Egypt"}, {"name": "Barbarians", "isBarbarian": True}],
    "techs": [
        {"id": "tech-2", "name": "Masonry", "eraCivilopediaName": "ERAS_Ancient_Times", "requiredForEraAdvancement": True,
         "x": 85, "y": 182},
        {"id": "tech-30", "name": "Education", "eraCivilopediaName": "ERAS_Middle_Ages", "requiredForEraAdvancement": False,
         "x": 331, "y": 374, "prerequisites": ["tech-2"]}],
    "unitPrototypes": [
        {"name": "Warrior", "attack": 1, "defense": 1, "movement": 1, "bombard": 0, "requiredTech": "tech-2",
         "producibleBy": ["Rome", "Greece"], "art": {"thumbnailArt": {"defaultIndex": 4, "variations": {"ERAS_Modern_Era": 70}}}},
        {"name": "Tank", "attack": 16, "defense": 8, "movement": 2, "requiredTech": "tech-2", "producibleBy": ["Rome"]}],
    "buildings": [{"name": "Walls", "requiredTech": "tech-2", "iconRowIndex": 7},
                  {"name": "Pyramids", "renderedObsoleteBy": "tech-30", "iconRowIndex": 12}],
    "terraForms": [{"name": "Outpost", "requiredTech": "tech-2", "buttonTexture": "ui.unit_control.unit_build_outpost"}],
}


def client_tree(root: Path) -> Path:
    """A C7 directory with every sheet webart takes (a small PNG each), one unit, the fonts, the ruleset."""
    c7 = root / "C7"
    art = c7 / "Assets" / "Art"
    for rel in [*webart.SHEETS.values(), *webart.SCREENS.values()]:
        (art / rel).parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", (4, 4), (10, 20, 30, 255)).save(art / rel)
    (c7 / "Lua" / "civ3").mkdir(parents=True)
    (c7 / "Lua" / "civ3" / "ruleset.json").write_text(json.dumps(RULESET))
    unit_folder(art / "Units", "Tribal Mediterranean Warrior")
    (c7 / "Fonts").mkdir(parents=True)
    for f in webart.FONTS:
        (c7 / "Fonts" / f).write_bytes(b"font " + f.encode())
    (c7 / "Assets" / ".pinned").write_text("716625c\n")
    return c7


def test_convert_writes_the_manifest_and_what_it_names(tmp_path, monkeypatch):
    monkeypatch.setattr(webart, "UNIT_ART", {"Warrior": "Tribal Mediterranean Warrior"})
    out = tmp_path / "webart"
    manifest = webart.convert(client_tree(tmp_path), out)
    assert json.loads((out / "manifest.json").read_text()) == manifest
    assert manifest["pinned"] == "716625c" and len(manifest["id"]) == 12
    assert set(manifest["sheets"]) == set(webart.SHEETS)
    assert all((out / p).is_file() for p in manifest["sheets"].values())
    unit = manifest["units"]["Warrior"]
    assert unit["sheet"] == "units/tribal_mediterranean_warrior.png" and (out / unit["sheet"]).is_file()
    assert (out / unit["mask"]).is_file()
    assert manifest["fonts"] == ["NotoSans-Bold.ttf", "NotoSans-Italic.ttf", "NotoSans-Regular.ttf"]
    # the screens' art, apart from the map's, with the stretched tech boxes: a 190x108 slot per size and state
    assert set(manifest["screens"]) == {*webart.SCREENS, "techboxes_fit"}
    assert all((out / p).is_file() for p in manifest["screens"].values())
    assert Image.open(out / manifest["screens"]["techboxes_fit"]).size == (4 * 190, 4 * 108)
    assert (out / "fonts" / "LICENSE-NotoSans.txt").is_file()
    # the same art gives the same id; the browser caches the sheets by it
    assert webart.convert(client_tree(tmp_path / "again"), tmp_path / "again-out")["id"] == manifest["id"]


def test_convert_takes_the_tech_tree_units_and_buildings_from_the_ruleset(tmp_path, monkeypatch):
    monkeypatch.setattr(webart, "UNIT_ART", {"Warrior": "Tribal Mediterranean Warrior"})
    m = webart.convert(client_tree(tmp_path), tmp_path / "webart")
    assert m["techs"] == [
        {"id": "tech-2", "name": "Masonry", "era": 0, "x": 85, "y": 182, "required": True, "prereqs": [],
         "buildings": 1, "units": ["Warrior"], "terraforms": [[3, 4]], "icon": "tech_tech-2"},
        {"id": "tech-30", "name": "Education", "era": 1, "x": 331, "y": 374, "required": False, "prereqs": ["tech-2"],
         "buildings": 1, "units": [], "terraforms": [], "icon": "tech_placeholder"}]
    # only the units with art (the standalone mode's); the civs listed when not every civ can build it
    assert m["unit_info"] == {"Warrior": {"a": 1, "d": 1, "m": 1, "b": 0, "icon": 4, "icon_era": {"3": 70},
                                          "civs": ["Greece", "Rome"]}}
    assert m["building_icons"] == {"Walls": 7, "Pyramids": 12}


def test_convert_fails_without_the_ruleset(tmp_path):
    c7 = client_tree(tmp_path)
    (c7 / "Lua" / "civ3" / "ruleset.json").unlink()
    with pytest.raises(FileNotFoundError, match="no ruleset"):
        webart.convert(c7, tmp_path / "out")


def test_convert_fails_without_the_art(tmp_path):
    with pytest.raises(FileNotFoundError, match="no art"):
        webart.convert(tmp_path, tmp_path / "out")


def test_find_takes_openciv_web_art_then_the_clients_home(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCIV_WEB_ART", raising=False)
    monkeypatch.setenv("OPENCIV_CLIENT", str(tmp_path / "client"))
    assert webart.find() is None
    (tmp_path / "client" / "webart").mkdir(parents=True)
    (tmp_path / "client" / "webart" / "manifest.json").write_text("{}")
    assert webart.find() == tmp_path / "client" / "webart"
    (tmp_path / "mine").mkdir()
    (tmp_path / "mine" / "manifest.json").write_text("{}")
    monkeypatch.setenv("OPENCIV_WEB_ART", str(tmp_path / "mine"))
    assert webart.find() == tmp_path / "mine"


def art_request(path: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": f"/play/art/{path}", "query_string": b"", "headers": [],
                    "path_params": {"path": path}})


@pytest.mark.anyio
async def test_the_env_serves_the_art_when_it_has_some(env, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCIV_CLIENT", str(tmp_path / "none"))
    monkeypatch.delenv("OPENCIV_WEB_ART", raising=False)
    assert (await env._play_art(art_request("manifest.json"))).status_code == 404
    art = tmp_path / "art"
    (art / "sheets").mkdir(parents=True)
    (art / "manifest.json").write_text('{"id": "x"}')
    (art / "sheets" / "fog.png").write_bytes(b"png")
    (tmp_path / "secret.txt").write_text("no")
    monkeypatch.setenv("OPENCIV_WEB_ART", str(art))
    r = await env._play_art(art_request("manifest.json"))
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    r = await env._play_art(art_request("sheets/fog.png"))
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"] and r.media_type == "image/png"
    for bad in ("../secret.txt", "sheets/../../secret.txt", "sheets/none.png", "sheets"):
        assert (await env._play_art(art_request(bad))).status_code == 404, bad
