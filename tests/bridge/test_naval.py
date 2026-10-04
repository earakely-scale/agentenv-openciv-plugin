"""Naval play over the bridge: units board ships and cross water (board/unload, aboard/cargo/capacity), a ship lost at
sea takes its passengers with it (patches/0020), the AI explores by sea (patches/0021) and ferries settlers overseas
(patches/0022). Run like test_protocol.py, whose Bridge and fixtures these share."""

from __future__ import annotations

import json
from pathlib import Path

import test_protocol
from test_protocol import SEED, Bridge, found_capital, unit

launch = test_protocol.launch   # the fixture

WATER = {"coast", "sea", "ocean"}
STEPS = ((2, 0), (-2, 0), (0, 2), (0, -2), (1, 1), (1, -1), (-1, 1), (-1, -1))


class Geo:
    """The map of a save: terrain, neighbours (x wraps) and the engine's continent ids, which number each landmass and
    each body of water (two water tiles that only touch diagonally between two land tiles are not connected)."""

    def __init__(self, save: dict):
        g = save["game"]
        self.width, self.wrap = g["map"]["tilesWide"], g["map"]["wrapHorizontally"]
        self.tiles = {(t["x"], t["y"]): t for t in g["map"]["tiles"]}
        self.part = {p: t["continent"] for p, t in self.tiles.items()}
        self.sizes: dict[int, int] = {}
        for c in self.part.values():
            self.sizes[c] = self.sizes.get(c, 0) + 1

    def water(self, p: tuple[int, int]) -> bool:
        return self.tiles[p]["baseTerrain"] in WATER

    def neighbours(self, p: tuple[int, int]) -> list[tuple[int, int]]:
        out = []
        for dx, dy in STEPS:
            x, y = p[0] + dx, p[1] + dy
            if self.wrap:
                x %= self.width
            if (x, y) in self.tiles:
                out.append((x, y))
        return out

    def size(self, p: tuple[int, int]) -> int:
        return self.sizes[self.part[p]]


def at(o: dict) -> tuple[int, int]:
    loc = o.get("location") or o.get("currentLocation") or o
    return loc["x"], loc["y"]


def check_cargo(world: dict) -> None:
    """The invariants of units at sea: a land unit on water is aboard a live ship of its owner on its tile; a ship
    carries no more than its capacity, only land units, all on its tile; a sea unit on land is in its owner's city; no
    tile holds two owners' units; and no city stands on water (a settler founds one only ashore)."""
    water = {(t[0], t[1]) for t in world["tiles"] if t[2] in WATER}
    assert not [c for c in world["cities"] if at(c) in water], "a city on water"
    by_id = {u["id"]: u for u in world["units"]}
    cities = {(c["x"], c["y"]): c["owner"] for c in world["cities"]}
    ships = {u["id"] for u in world["units"] if u["type"] in SHIPS}
    owners: dict[tuple[int, int], set[int]] = {}
    for u in world["units"]:
        p = at(u)
        owners.setdefault(p, set()).add(u["owner"])
        if u["type"] in SHIPS:
            assert not u["aboard"] and (p in water or cities.get(p) == u["owner"]), u
            continue
        if u["aboard"]:
            ship = by_id.get(u["aboard"])
            assert ship and ship["id"] in ships and at(ship) == p and ship["owner"] == u["owner"], u
        else:
            assert p not in water, f"{u} stands on the water with no ship"
    for sid in ships:
        aboard = [u for u in world["units"] if u["aboard"] == sid]
        assert len(aboard) <= CAPACITY.get(by_id[sid]["type"], 0), (by_id[sid], aboard)
    assert all(len(o) == 1 for o in owners.values()), {p: o for p, o in owners.items() if len(o) > 1}


def sea_units() -> dict[str, int]:
    """Every sea unit of the ruleset the bridge ships with, by its capacity (land units it carries)."""
    rules = json.loads((Path(test_protocol.CMD[0]).parent / "Lua" / "civ3" / "ruleset.json").read_text())
    return {p["name"]: p.get("capacity", 0) for p in rules["unitPrototypes"] if "Sea" in p.get("categories", [])}


CAPACITY = sea_units()
SHIPS = set(CAPACITY)


def harbour(launch, tmp_path, turns: int = 1) -> tuple[Bridge, dict]:
    """Archipelago seed 1, the capital founded and the save edited: the seat knows the whole map, and a Settler and a
    Warrior stand on a shore of its island next to a Galley on the ocean, with a second Galley and a Warrior in the
    capital and a Spearman with no moves on the shore. Returns the loaded bridge and the plan: the shore, the Galley's
    tile, and a water tile next to a site on another island."""
    b = launch("--autosave", str(tmp_path / "a"))
    b.call("new_game", seed=SEED, size="Small", landform="Archipelago", turn_limit=300)
    capital = found_capital(b)
    for _ in range(turns):
        b.call("end_turn", skip_idle=True)
    path = tmp_path / "a" / "autosave.json"
    save = json.loads(path.read_text())
    g = save["game"]
    geo = Geo(save)
    me = next(p for p in g["players"] if p["human"])
    me["tileKnowledge"] = [{"x": x, "y": y} for x, y in geo.tiles]
    home = geo.part[at(capital)]
    taken = {at(u) for u in g["units"]} | {at(c) for c in g["cities"]}
    near = [c for c in g["cities"] if c["owner"] != me["id"]]
    ocean = max((c for p, c in geo.part.items() if geo.water(p)), key=geo.sizes.__getitem__)

    def site(p: tuple[int, int]) -> bool:
        return (not geo.water(p) and geo.part[p] != home and p not in taken and geo.size(p) >= 20
                and geo.tiles[p]["baseTerrain"] in ("grassland", "plains")
                and all(max(abs(p[0] - at(c)[0]), abs(p[1] - at(c)[1])) > 6 for c in near))

    # The shore nearest the capital on the ocean (the capital itself is on a lake), then the nearest site overseas.
    shores = sorted(((s, w) for s in geo.tiles if geo.part[s] == home and s not in taken
                     for w in geo.neighbours(s) if geo.part[w] == ocean and w not in taken),
                    key=lambda sw: abs(sw[0][0] - at(capital)[0]) + abs(sw[0][1] - at(capital)[1]))
    shore, sea = shores[0]
    landings = sorted(((w, s) for s in geo.tiles if site(s) for w in geo.neighbours(s)
                       if geo.part[w] == ocean and w not in taken),
                      key=lambda ws: abs(ws[0][0] - sea[0]) + abs(ws[0][1] - sea[1]))
    landing, target = landings[0]

    template = next(u for u in g["units"] if u["owner"] == me["id"])

    def add(uid: str, kind: str, p: tuple[int, int], moves: float) -> None:
        g["units"].append({**template, "id": uid, "name": kind, "prototype": kind,
                           "currentLocation": {"x": p[0], "y": p[1]},
                           "previousLocation": {"x": p[0], "y": p[1]}, "hitPointsRemaining": 3,
                           "movePointsRemaining": moves, "isAutomated": False, "workerProgressTowardsJob": 0})

    add("Galley-901", "Galley", sea, 3)
    add("Galley-902", "Galley", at(capital), 3)
    add("Settler-903", "Settler", shore, 1)
    add("Warrior-904", "Warrior", shore, 1)
    add("Warrior-905", "Warrior", at(capital), 1)
    add("Spearman-906", "Spearman", shore, 0)
    edited = tmp_path / "harbour.json"
    edited.write_text(json.dumps(save))
    r = launch("--autosave", str(tmp_path / "b"))
    r.call("load", path=str(edited))
    s = r.call("state")

    def find(kind: str, p: tuple[int, int]) -> str:
        return next(u["id"] for u in s["units"] if u["type"] == kind and at(u) == p)

    return r, {"capital": at(capital), "shore": shore, "sea": sea, "landing": landing, "site": target, "home": home,
               "geo": geo, "galley": find("Galley", sea), "port_galley": find("Galley", at(capital)),
               "settler": find("Settler", shore), "warrior": find("Warrior", shore),
               "guard": find("Warrior", at(capital)),
               "spearman": find("Spearman", shore)}


def sail(b: Bridge, ship: str, to: tuple[int, int], turns: int = 20) -> dict:
    """Orders the ship to `to` and ends turns until it is there; returns its state."""
    b.call("unit_order", unit=ship, order="goto", x=to[0], y=to[1])
    for _ in range(turns):
        u = unit(b.call("state"), ship)
        if at(u) == to:
            return u
        b.call("end_turn", skip_idle=True)
    raise AssertionError(f"{ship} never reached {to}")


def test_a_unit_boards_a_ship_and_founds_a_city_overseas(launch, tmp_path):
    """A Settler and a Warrior board a Galley from the shore (each steps onto it, which takes its moves), the Galley
    sails them to another island, and the Settler founds a city there straight from the ship."""
    b, h = harbour(launch, tmp_path)
    galley, settler, warrior = h["galley"], h["settler"], h["warrior"]
    s = b.call("state")
    assert "board" in unit(s, settler)["orders"] and unit(s, galley)["capacity"] == 2 and unit(s, galley)["cargo"] == []
    assert "aboard" not in unit(s, settler)

    res = b.call("unit_order", unit=settler, order="board", x=h["sea"][0], y=h["sea"][1])
    assert res["message"] == (f"{settler} Settler went aboard {galley} Galley at ({h['sea'][0]},{h['sea'][1]}) (1/2), "
                              "which took its moves; the ship carries it from now on.")
    assert res["unit"]["aboard"] == galley and res["unit"]["status"] == "aboard" and not res["unit"]["needs_orders"]
    assert at(res["unit"]) == h["sea"]
    b.call("unit_order", unit=warrior, order="board", x=h["sea"][0], y=h["sea"][1])
    s = b.call("state")
    assert unit(s, galley)["cargo"] == [settler, warrior] and unit(s, warrior)["aboard"] == galley
    assert "board" not in unit(s, galley)["orders"]
    check_cargo(b.call("world"))
    # A passenger never holds up the turn, even woken: it waits for its ship.
    assert b.call("unit_order", unit=warrior, order="wake")["message"] == f"{warrior} Warrior had no standing order."
    w = unit(b.call("state"), warrior)
    assert (w["status"], w["needs_orders"], w["aboard"]) == ("aboard", False, galley) and "wake" not in w["orders"]
    assert all(x.get("id") != warrior for x in b.call("state")["blockers"])

    # The ship carries them; the city sites of a unit aboard are on the islands along its waters.
    sail(b, galley, h["landing"])
    s = b.call("state")
    assert at(unit(s, settler)) == at(unit(s, warrior)) == h["landing"] and unit(s, settler)["aboard"] == galley
    sites = b.call("city_sites", unit=settler)["sites"]
    assert sites and all(h["geo"].part[at(x)] != h["home"] for x in sites)
    check_cargo(b.call("world"))

    # Straight from the ship: the settler steps ashore (which takes its moves) and founds the city at the start of the
    # next turn; the Warrior goes ashore with it.
    res = b.call("unit_order", unit=settler, order="settle", x=h["site"][0], y=h["site"][1])
    assert at(res["unit"]) == h["site"] and "aboard" not in res["unit"], res["message"]
    assert unit(b.call("state"), galley)["cargo"] == [warrior]
    res = b.call("unit_order", unit=warrior, order="goto", x=h["site"][0], y=h["site"][1])
    assert at(res["unit"]) == h["site"] and "aboard" not in res["unit"], res["message"]
    assert unit(b.call("state"), galley)["cargo"] == []
    check_cargo(b.call("world"))
    events = b.call("end_turn", skip_idle=True)["events"]
    [city] = [c for c in b.call("state")["cities"] if at(c) == h["site"]]
    assert any(e["kind"] == "city_founded" and city["name"] in e["text"] for e in events), events
    assert h["geo"].part[h["site"]] != h["home"]


def test_a_ship_lost_at_sea_takes_its_passengers(launch, tmp_path):
    """patches/0020: a Galley disbanded at sea takes the Settler and Warrior aboard with it (without the patch they
    stayed on the water, aboard a ship that no longer existed); one disbanded in port puts its passenger ashore."""
    b, h = harbour(launch, tmp_path)
    galley, settler, warrior = h["galley"], h["settler"], h["warrior"]
    for u in (settler, warrior):
        b.call("unit_order", unit=u, order="board", x=h["sea"][0], y=h["sea"][1])
    res = b.call("unit_order", unit=galley, order="disband")
    lost = f"{settler} Settler, {warrior} Warrior aboard were lost with it."
    assert res["message"] == f"{galley} Galley was disbanded. {lost}"
    s = b.call("state")
    assert not {galley, settler, warrior} & {u["id"] for u in s["units"]}
    assert b.error("unit_order", unit=warrior, order="goto", x=h["shore"][0], y=h["shore"][1])["code"] == "unknown_unit"
    check_cargo(b.call("world"))

    port, guard = h["port_galley"], h["guard"]
    res = b.call("unit_order", unit=guard, order="board")
    assert res["message"] == (f"{guard} Warrior is aboard {port} Galley at ({h['capital'][0]},{h['capital'][1]}) "
                              "(1/2); the ship carries it from now on.")
    res = b.call("unit_order", unit=port, order="disband")
    assert res["message"].endswith(f" {guard} Warrior went ashore.")
    g = unit(b.call("state"), guard)
    assert "aboard" not in g and at(g) == h["capital"] and g["status"] == "idle"
    check_cargo(b.call("world"))
    b.call("end_turn", skip_idle=True)
    check_cargo(b.call("world"))


def test_unload_only_in_a_city(launch, tmp_path):
    """At sea a passenger lands by goto or settle, not by unload; in a city unload puts every passenger ashore."""
    b, h = harbour(launch, tmp_path)
    b.call("unit_order", unit=h["settler"], order="board", x=h["sea"][0], y=h["sea"][1])
    for who in (h["galley"], h["settler"]):
        err = b.error("unit_order", unit=who, order="unload")
        assert err["code"] == "invalid_order"
        assert "at sea, order each passenger to goto or settle a land tile" in err["message"]
        assert "unload" not in err["alternatives"]

    port, guard, worker = h["port_galley"], h["guard"], "u2"
    assert unit(b.call("state"), worker)["x"] == h["capital"][0]
    for u in (guard, worker):
        b.call("unit_order", unit=u, order="board")
    s = b.call("state")
    assert unit(s, port)["cargo"] == [worker, guard] and "unload" in unit(s, port)["orders"]
    assert "board" not in unit(s, guard)["orders"]
    err = b.error("unit_order", unit=guard, order="board")
    assert err["message"].startswith(f"{guard} Warrior is already aboard {port} Galley.")
    res = b.call("unit_order", unit=port, order="unload")
    assert res["message"] == f"{worker} Worker, {guard} Warrior went ashore in Rome and await orders."
    s = b.call("state")
    assert unit(s, port)["cargo"] == [] and all("aboard" not in unit(s, u) for u in (guard, worker))
    assert unit(s, guard)["needs_orders"]
    # A passenger can also go ashore by itself.
    b.call("unit_order", unit=guard, order="board")
    res = b.call("unit_order", unit=guard, order="unload")
    assert res["message"] == f"{guard} Warrior went ashore in Rome and awaits orders."


def test_board_refuses_what_it_cannot_do(launch, tmp_path):
    """Every refusal names what went wrong and, where there is one, the call that works."""
    b, h = harbour(launch, tmp_path)
    galley, settler, warrior = h["galley"], h["settler"], h["warrior"]
    x, y = h["sea"]
    err = b.error("unit_order", unit=galley, order="board")
    assert err["code"] == "invalid_order" and "only land units can" in err["message"]
    err = b.error("unit_order", unit=settler, order="board")
    board = f'unit_order(unit="{settler}", order="board", x={x}, y={y})'
    assert err["code"] == "no_transport" and err["suggest"] == board
    far = h["landing"]
    assert b.error("unit_order", unit=settler, order="board", x=far[0], y=far[1])["code"] == "bad_target"
    land = next(p for p in h["geo"].neighbours(h["shore"]) if not h["geo"].water(p))
    assert b.error("unit_order", unit=settler, order="board", x=land[0], y=land[1])["code"] == "bad_target"
    err = b.error("unit_order", unit=settler, order="goto", x=x, y=y)
    assert err["code"] == "bad_target" and err["suggest"] == board
    err = b.error("unit_order", unit=h["spearman"], order="board", x=x, y=y)
    assert err["code"] == "no_moves"
    assert err["message"] == f"{h['spearman']} Spearman has no moves left this turn; board {galley} Galley next turn."
    b.call("unit_order", unit=settler, order="board", x=x, y=y)
    b.call("unit_order", unit=warrior, order="board", x=x, y=y)
    err = b.error("unit_order", unit=h["spearman"], order="board", x=x, y=y)
    assert err["code"] == "no_transport" and err["message"].startswith(f"Every ship of yours at ({x},{y}) is full.")
    err = b.error("unit_order", unit=warrior, order="board", x=x, y=y)
    assert err["code"] == "invalid_order"
    assert err["message"].startswith(f"{warrior} Warrior is already aboard {galley} Galley.")
    assert unit(b.call("state"), galley)["cargo"] == [settler, warrior]
    check_cargo(b.call("world"))


def test_cargo_survives_a_save(launch, tmp_path):
    """Passengers at sea are still aboard after the game is saved and loaded in a new bridge, and the engine AI playing
    the seat afterwards leaves no unit on the water without a ship."""
    b, h = harbour(launch, tmp_path)
    galley, settler, warrior = h["galley"], h["settler"], h["warrior"]
    for u in (settler, warrior):
        b.call("unit_order", unit=u, order="board", x=h["sea"][0], y=h["sea"][1])
    b.call("unit_order", unit=galley, order="goto", x=h["landing"][0], y=h["landing"][1])
    b.call("end_turn", skip_idle=True)
    s = b.call("state")
    out = at(unit(s, galley))
    assert out != h["sea"] and h["geo"].water(out) and unit(s, galley)["cargo"] == [settler, warrior]

    r = launch()
    r.call("load", path=str(tmp_path / "b" / "autosave.json"))
    s = r.call("state")
    assert unit(s, galley)["cargo"] == [settler, warrior] and at(unit(s, galley)) == out
    assert all(unit(s, u)["aboard"] == galley and unit(s, u)["status"] == "aboard" for u in (settler, warrior))
    check_cargo(r.call("world"))
    r.call("autoplay", turns=5, policy="engine_ai")
    check_cargo(r.call("world"))


def with_units(launch, tmp_path, *added: tuple[str, str]) -> Bridge:
    """Archipelago seed 1 a turn after the capital is founded, the save edited to add units (type, "capital") there."""
    b = launch("--autosave", str(tmp_path / "a"))
    b.call("new_game", seed=SEED, size="Small", landform="Archipelago", turn_limit=300)
    capital = found_capital(b)
    b.call("end_turn", skip_idle=True)
    save = json.loads((tmp_path / "a" / "autosave.json").read_text())
    g = save["game"]
    me = next(p for p in g["players"] if p["human"])["id"]
    template = next(u for u in g["units"] if u["owner"] == me)
    for n, (kind, _) in enumerate(added):
        g["units"].append({**template, "id": f"{kind}-{900 + n}", "name": kind, "prototype": kind,
                           "currentLocation": {"x": capital["x"], "y": capital["y"]},
                           "previousLocation": {"x": capital["x"], "y": capital["y"]}, "hitPointsRemaining": 3,
                           "movePointsRemaining": 2, "isAutomated": False, "workerProgressTowardsJob": 0})
    edited = tmp_path / "edited.json"
    edited.write_text(json.dumps(save))
    r = launch()
    r.call("load", path=str(edited))
    return r


def test_a_boat_on_a_lake_has_nothing_to_explore(launch, tmp_path):
    """patches/0021: Rome, on seed 1's Archipelago, sits on a lake of a few tiles with no way to the ocean. A Curragh
    built there has nothing it can reach to explore; it used to accept explore and never leave the lake."""
    b = with_units(launch, tmp_path, ("Curragh", "capital"))
    s = b.call("state")
    boat = next(u for u in s["units"] if u["type"] == "Curragh")
    assert "explore" in boat["orders"]
    err = b.error("unit_order", unit=boat["id"], order="explore")
    assert err["code"] == "invalid_order"
    assert err["message"].startswith(f"There is nothing left that {boat['id']} Curragh can reach and explore.")


def known_water(world: dict, seat: int = 0) -> int:
    return sum(1 for t in world["tiles"] if t[2] in WATER and t[6] >> seat & 1)


def test_the_ai_explores_the_sea(launch):
    """patches/0021: the AI builds a few boats and explores the ocean with them. On Archipelago seed 1 the engine AI in
    the seat knew 245 water tiles at T100 when no AI built a boat; now it knows the oceans around its islands."""
    b = launch()
    b.call("new_game", seed=SEED, size="Small", landform="Archipelago", turn_limit=300)
    boats: dict[int, set[str]] = {}
    for _ in range(10):
        b.call("autoplay", turns=10, policy="engine_ai", timeout=300)
        world = b.call("world")
        ai = {p["index"] for p in world["players"] if p["civ"] != "Barbarians"}
        for u in world["units"]:
            if u["owner"] in ai and u["type"] in SHIPS:
                boats.setdefault(u["owner"], set()).add(u["id"])
        check_cargo(world)
    assert known_water(world) > 500, known_water(world)
    assert len(boats) >= 2, boats
    assert all(len(v) <= 8 for v in boats.values()), boats


def landmasses(save: dict) -> dict[str, list[int]]:
    """Each civ's cities' landmasses (engine continent ids), its oldest city first."""
    g = save["game"]
    geo = Geo(save)
    civ = {p["id"]: p["civilization"] for p in g["players"]}
    out: dict[str, list[int]] = {}
    for c in sorted(g["cities"], key=lambda c: int(c["id"].split("-")[1])):
        out.setdefault(civ[c["owner"]], []).append(geo.part[at(c)])
    return out


def test_the_ai_settles_another_island(launch, tmp_path):
    """patches/0022: on Archipelago seed 1 every civ's cities stayed on the island of its first city (0 overseas by T300
    when nothing crossed water). The engine AI now ferries settlers to other islands, and leaves no unit on the water
    without a ship.

    Played to T300, the horizon docs/full-game.md measures. Overseas settling starts only once a civ's home island is
    nearly full, around T150 on this seed, so a count at T150 hung on just when that was: 5 overseas cities there with
    patches 0020-0022 alone, 2 once the other features shifted the game (seeds 2 and 3 had none by T150 even with the
    naval patches alone). By T300 it was 36 with the naval patches alone and 33 with every feature, in 3 civs each."""
    b = launch("--autosave", str(tmp_path / "a"))
    b.call("new_game", seed=SEED, size="Small", landform="Archipelago", turn_limit=300)
    for _ in range(30):
        b.call("autoplay", turns=10, policy="engine_ai", timeout=300)
        check_cargo(b.call("world"))
    save = json.loads((tmp_path / "a" / "autosave.json").read_text())
    assert save["game"]["turnNumber"] == 300
    by_civ = landmasses(save)
    overseas = {civ: sum(x != lands[0] for x in lands) for civ, lands in by_civ.items()}
    assert sum(overseas.values()) >= 10 and sum(n > 0 for n in overseas.values()) >= 3, overseas


def test_the_same_seed_ferries_the_same_way(launch):
    """The ferry and sea exploration AI draw nothing from the game's RNG and go through units in a fixed order, so an
    Archipelago game replays identically."""
    worlds = []
    for _ in range(2):
        b = launch()
        b.call("new_game", seed=SEED, size="Small", landform="Archipelago", turn_limit=300)
        b.call("autoplay", turns=150, policy="engine_ai", timeout=600)
        worlds.append((b.call("world"), b.call("state")))
    assert worlds[0] == worlds[1]


def edited(launch, tmp_path, found: bool = True, turns: int = 2) -> tuple[dict, Geo, str, list[dict]]:
    """Archipelago seed 1 after `turns` turns (the capital founded or not), saved: the save, its map, the seat's player
    id and the AI's cities, to edit and load with `reload`."""
    b = launch("--autosave", str(tmp_path / "a"))
    b.call("new_game", seed=SEED, size="Small", landform="Archipelago", turn_limit=300)
    if found:
        found_capital(b)
    for _ in range(turns):
        b.call("end_turn", skip_idle=True)
    save = json.loads((tmp_path / "a" / "autosave.json").read_text())
    g = save["game"]
    me = next(p for p in g["players"] if p["human"])["id"]
    return save, Geo(save), me, [c for c in g["cities"] if c["owner"] != me]


def reload(launch, tmp_path, save: dict) -> Bridge:
    path = tmp_path / "edited.json"
    path.write_text(json.dumps(save))
    b = launch()
    b.call("load", path=str(path))
    return b


def new_unit(template: dict, uid: str, kind: str, owner: dict, p: tuple[int, int], moves: float,
             on: str | None = None) -> dict:
    """A unit of `owner` (a player of the save) and of its nationality, at `p`; `on` loads it on that ship."""
    u = {**template, "id": uid, "name": kind, "prototype": kind, "owner": owner["id"],
         "nationality": owner["civilization"], "currentLocation": {"x": p[0], "y": p[1]},
         "previousLocation": {"x": p[0], "y": p[1]}, "hitPointsRemaining": 3, "movePointsRemaining": moves,
         "isAutomated": False, "workerProgressTowardsJob": 0}
    if on:
        u["loadedOnUnitId"], u["action"] = on, "fortified"
    return u


def open_sea(geo: Geo, taken: set[tuple[int, int]], far: int = 3) -> tuple[int, int]:
    """An ocean tile with nothing but water `far` steps around it (no land unit can step ashore in one turn)."""
    def ring(p: tuple[int, int], n: int) -> set[tuple[int, int]]:
        seen = {p}
        for _ in range(n):
            seen |= {q for s in seen for q in geo.neighbours(s)}
        return seen
    return next(p for p in sorted(geo.tiles) if geo.tiles[p]["baseTerrain"] == "ocean" and p not in taken
                and all(geo.water(q) for q in ring(p, far)))


def test_the_seat_is_defeated_with_a_passenger_at_sea(launch, tmp_path):
    """patches/0020: the seat disbands its last land unit while a Warrior rides a Galley at sea, the Warrior listed
    before its ship. The civilization is destroyed and every unit goes, the passenger with its ship (removing the units
    one by one by index ran past the end of the list once a ship took its passenger with it)."""
    save, geo, me, _ = edited(launch, tmp_path, found=False, turns=1)
    g = save["game"]
    g["units"] = [u for u in g["units"] if not (u["owner"] == me and u["prototype"] == "Settler")]
    template = next(u for u in g["units"] if u["owner"] == me)
    seat = next(p for p in g["players"] if p["id"] == me)
    sea = next(p for p in geo.neighbours(at(template)) if geo.water(p))
    g["units"] += [new_unit(template, "Warrior-900", "Warrior", seat, sea, 1, on="Galley-901"),
                   new_unit(template, "Galley-901", "Galley", seat, sea, 3)]
    b = reload(launch, tmp_path, save)
    s = b.call("state")
    [worker] = [u["id"] for u in s["units"] if u["type"] == "Worker"]
    [galley] = [u["id"] for u in s["units"] if u["type"] == "Galley"]
    assert len(unit(s, galley)["cargo"]) == 1
    res = b.call("unit_order", unit=worker, order="disband")
    assert res["message"].endswith("your civilization is defeated and the game is over."), res["message"]
    s, world = b.call("state"), b.call("world")
    seat = next(p["index"] for p in world["players"] if p["is_human"])
    assert s["defeated"] and s["units"] == [] and not [u for u in world["units"] if u["owner"] == seat]


def last_city_taken(launch, tmp_path, *cargo: str) -> tuple[Bridge, dict, int, dict]:
    """An AI's only city, emptied, falls to a seat Horseman next to it while a Galley of that AI far out at sea carries
    `cargo`, listed before the ship. Returns the bridge, the city (save), the AI's index and the attack's result."""
    save, geo, me, theirs = edited(launch, tmp_path)
    g = save["game"]
    taken = {at(u) for u in g["units"]} | {at(c) for c in g["cities"]}
    city = next(c for c in theirs if any(not geo.water(p) and p not in taken for p in geo.neighbours(at(c))))
    them = city["owner"]
    spot = next(p for p in geo.neighbours(at(city)) if not geo.water(p) and p not in taken)
    sea = open_sea(geo, taken)
    template = next(u for u in g["units"] if u["owner"] == me)
    players = {p["id"]: p for p in g["players"]}
    g["units"] = [u for u in g["units"] if u["owner"] != them]
    g["units"] += [new_unit(template, f"{kind}-{900 + n}", kind, players[them], sea, 1, on="Galley-901")
                   for n, kind in enumerate(cargo, start=2)]
    g["units"] += [new_unit(template, "Galley-901", "Galley", players[them], sea, 3),
                   new_unit(template, "Horseman-900", "Horseman", players[me], spot, 2)]
    players[me].setdefault("playerRelationships", {})[them] = {}
    players[them].setdefault("playerRelationships", {})[me] = {}
    b = reload(launch, tmp_path, save)
    world = b.call("world")
    loser = next(c["owner"] for c in world["cities"] if at(c) == at(city))
    assert {u["type"] for u in world["units"] if u["owner"] == loser} == {"Galley", *cargo}
    war = b.send("declare_war", civ=world["players"][loser]["civ"])
    assert war["ok"] or war["error"]["code"] == "already_at_war", war
    horseman = next(u["id"] for u in b.call("state")["units"] if u["type"] == "Horseman")
    res = b.call("unit_order", unit=horseman, order="attack", x=at(city)[0], y=at(city)[1])
    assert f"{city['name']} fell" in res["message"], res["message"]
    return b, city, loser, res


def test_a_civ_destroyed_with_a_passenger_at_sea(launch, tmp_path):
    """patches/0020: the seat takes an AI's only city while a Warrior of that AI rides a Galley far out at sea, the
    Warrior listed before its ship. The city falls, the civ is destroyed with every unit it had, and the city is gone
    from the map (the engine used to throw while removing the civ's units, leaving the city on its tile)."""
    b, city, loser, _ = last_city_taken(launch, tmp_path, "Warrior")
    world = b.call("world")
    assert world["players"][loser]["defeated"]
    assert not [u for u in world["units"] if u["owner"] == loser]
    assert not [c for c in world["cities"] if at(c) == at(city)]
    [tile] = [t for t in b.call("map", x=at(city)[0], y=at(city)[1], radius=0)["tiles"]]
    assert tile["city"] is None and tile["city_site"]["ok"], tile
    b.call("end_turn", skip_idle=True)
    check_cargo(b.call("world"))


def test_a_ship_with_no_port_left_lands_its_settler(launch, tmp_path):
    """patches/0022: an AI loses its only city while its Galley carries a Settler far out at sea, with no plan (as
    after a load). With no port left the ship sails to the nearest shore and the Settler goes ashore (it used to stay
    aboard for the rest of the game, as the ship only landed cargo next to its own tile), and founds its city on land
    (with no city left it used to found one where it stood, on the water)."""
    b, _, loser, _ = last_city_taken(launch, tmp_path, "Settler")
    assert not b.call("world")["players"][loser]["defeated"], "a civ with a settler lives on"
    for _ in range(20):
        b.call("end_turn", skip_idle=True)
        world = b.call("world")
        check_cargo(world)
        settler = next((u for u in world["units"] if u["owner"] == loser and u["type"] == "Settler"), None)
        if settler is None or not settler["aboard"]:
            break
    assert settler is None or not settler["aboard"], settler
    assert settler or [c for c in world["cities"] if c["owner"] == loser], "the Settler went ashore and founded a city"
    for _ in range(5):
        b.call("end_turn", skip_idle=True)
        check_cargo(b.call("world"))


def test_a_broke_ai_spares_the_ship_carrying_its_settler(launch, tmp_path):
    """patches/0020: an AI out of gold disbands its least useful units, settlers and workers last. A Galley at sea is
    as weak as a Warrior, but it carries a Settler: the AI disbands Warriors and keeps the Galley and the Settler."""
    save, geo, me, theirs = edited(launch, tmp_path)
    g = save["game"]
    taken = {at(u) for u in g["units"]} | {at(c) for c in g["cities"]}
    city = theirs[0]
    them = city["owner"]
    sea = open_sea(geo, taken)
    template = next(u for u in g["units"] if u["owner"] == me)
    ai = next(p for p in g["players"] if p["id"] == them)
    soldiers = [new_unit(template, f"Warrior-{910 + n}", "Warrior", ai, at(city), 1) for n in range(12)]
    g["units"] = [new_unit(template, "Galley-901", "Galley", ai, sea, 3),
                  new_unit(template, "Settler-900", "Settler", ai, sea, 1, on="Galley-901"), *g["units"], *soldiers]
    ai["gold"] = 0
    b = reload(launch, tmp_path, save)
    b.call("end_turn", skip_idle=True)
    world = b.call("world")
    ids = {u["id"] for u in world["units"]}
    assert len({s["id"] for s in soldiers} - ids) >= 3, "the AI is broke and disbands Warriors"
    assert {"Galley-901", "Settler-900"} <= ids
    check_cargo(world)


def test_a_defender_carried_off_its_post_plans_again(launch, tmp_path):
    """patches/0022: the engine AI tells a Warrior with no moves left in the capital to hold it, and the plan waits for
    its moves. Carried off its post (as an escort that boarded in port is when its ship sails, or here by the seat's
    own goto before autoplay), it had no path back: the engine threw and ended the seat's turn every turn after."""
    save, geo, me, _ = edited(launch, tmp_path, turns=1)
    g = save["game"]
    seat = next(p for p in g["players"] if p["id"] == me)
    capital = at(next(c for c in g["cities"] if c["owner"] == me))
    template = next(u for u in g["units"] if u["owner"] == me)
    g["units"].append(new_unit(template, "Warrior-900", "Warrior", seat, capital, 0))
    b = reload(launch, tmp_path, save)
    [warrior] = [u["id"] for u in b.call("state")["units"] if u["type"] == "Warrior"]
    b.call("autoplay", turns=1, policy="engine_ai")
    post = next(p for p in geo.neighbours(capital) if not geo.water(p))
    assert at(b.call("unit_order", unit=warrior, order="goto", x=post[0], y=post[1])["unit"]) == post
    b.call("autoplay", turns=3, policy="engine_ai")
    log = (tmp_path / "stderr-1.log").read_text()
    assert "failed while playing the human seat" not in log, log[:2000]
