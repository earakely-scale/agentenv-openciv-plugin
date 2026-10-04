# ruff: noqa: F811
"""Later eras (patches 0018 and 0019): the standalone ruleset keeps every land and sea unit with its upgrades, so old
units go obsolete, and units upgrade in their own cities for gold, the agent's by order and the AI's on its own."""

from __future__ import annotations

import json
from collections.abc import Callable

# launch is test_protocol's fixture, imported for these tests to take as an argument.
from test_protocol import SEED, Bridge, found_capital, launch, unit  # noqa: F401

INVENTION, WARRIOR_CODE, BRONZE_WORKING, IRON_WORKING, FEUDALISM, ASTRONOMY = (
    "tech-27", "tech-6", "tech-1", "tech-8", "tech-23", "tech-33")
# What the standalone ruleset leaves out: units the engine cannot play (air, missiles, nukes, what serves air) and the
# leaders and armies no rule produces.
LEFT_OUT = {"Fighter", "Bomber", "Helicopter", "Jet Fighter", "Stealth Fighter", "Stealth Bomber", "F-15",
            "Cruise Missile", "Tactical Nuke", "ICBM", "Carrier", "Nuclear Submarine", "Flak", "Mobile SAM", "Leader",
            "Army", "Princess", "Caesar", "Lincoln"}


def edited_game(launch, tmp_path, edit: Callable[[dict, dict, tuple[int, int]], None], **game) -> Bridge:
    """Rome's capital founded and a turn played, then the autosave edited by `edit(game, me, capital)` and loaded."""
    b = launch("--autosave", str(tmp_path / "a"))
    b.call("new_game", seed=SEED, **game)
    city = found_capital(b)
    b.call("end_turn", skip_idle=True)
    save = json.loads((tmp_path / "a" / "autosave.json").read_text())
    me = next(p for p in save["game"]["players"] if p["human"])
    edit(save["game"], me, (city["x"], city["y"]))
    path = tmp_path / "edited.json"
    path.write_text(json.dumps(save))
    b = launch("--autosave", str(tmp_path / "b"))
    b.call("load", path=str(path))
    return b


def add_unit(g: dict, owner: str, proto: str, at: tuple[int, int], n: int, **extra) -> str:
    """A copy of the game's first unit as a `proto` of `owner` at `at`, with id "<proto>-<n>"; returns the id."""
    uid = f"{proto}-{n}"
    g["units"].append({**g["units"][0], "id": uid, "name": proto, "prototype": proto, "owner": owner,
                       "nationality": next(p["civilization"] for p in g["players"] if p["id"] == owner),
                       "currentLocation": {"x": at[0], "y": at[1]}, "previousLocation": {"x": at[0], "y": at[1]},
                       "movePointsRemaining": 1, "isAutomated": False, "experience": "Regular", **extra})
    return uid


def at(state: dict, xy: tuple[int, int], kind: str) -> dict:
    return next(u for u in state["units"] if (u["x"], u["y"]) == xy and u["type"] == kind)


def test_obsolete_units_leave_the_production_list(launch, tmp_path):
    """patches/0018: with Invention known the Longbowman replaces the Archer, which leaves the city's options and is
    refused with the reason. The standalone ruleset dropped every unit's upgrades, so nothing ever went obsolete."""
    b = edited_game(launch, tmp_path, lambda g, me, c: me["knownTechs"].extend([WARRIOR_CODE, INVENTION]))
    options = [o["name"] for o in b.call("city", city="c1")["options"] if o["kind"] == "unit"]
    assert "Longbowman" in options and "Archer" not in options
    err = b.error("set_production", city="c1", item="Archer")
    assert err["code"] == "unknown_item" and "obsolete" in err["message"] and "Longbowman" in err["message"]


def test_a_unit_goes_obsolete_only_once_it_can_upgrade(launch, tmp_path):
    """patches/0019: obsolescence follows the civ's own upgrade chain. With Feudalism and Iron but not Invention, Rome
    can build the Medieval Infantry, a "sibling" of the Archer's line, yet the Archer cannot upgrade to the Longbowman:
    it stays buildable (the engine called it obsolete, and the agent could neither build nor upgrade it)."""
    def edit(g, me, capital):
        me["knownTechs"].extend([WARRIOR_CODE, BRONZE_WORKING, IRON_WORKING, FEUDALISM])
        next(t for t in g["map"]["tiles"] if (t["x"], t["y"]) == capital)["resource"] = "Iron"
        add_unit(g, me["id"], "Archer", capital, 9001)

    b = edited_game(launch, tmp_path, edit)
    options = [o["name"] for o in b.call("city", city="c1")["options"] if o["kind"] == "unit"]
    assert {"Archer", "Medieval Infantry", "Pikeman"} <= set(options) and "Spearman" not in options
    archer = next(u for u in b.call("state")["units"] if u["type"] == "Archer")
    assert "upgrade" not in archer
    assert "Rome now builds Archer" in b.call("set_production", city="c1", item="Archer")["message"]


def test_a_queued_unit_that_went_obsolete_leaves_the_queue_with_the_reason(launch, tmp_path):
    """patches/0019 with set_production's `then`: an Archer queued after Invention is dropped when the city gets to
    it, and the note says what replaces it."""
    b = edited_game(launch, tmp_path, lambda g, me, c: me["knownTechs"].extend([WARRIOR_CODE, INVENTION]))
    b.call("set_production", city="c1", item="Warrior", then=["Archer", "Warrior"])
    for _ in range(20):
        built = [e["text"] for e in b.call("end_turn", skip_idle=True)["events"] if e["kind"] == "built"]
        if built:
            break
    assert built and built[-1].endswith("; next from your queue: Warrior (Archer left the queue: it is obsolete now "
                                        "that Rome can build the Longbowman that replaces it)."), built


def test_later_and_unique_units_are_in_the_ruleset(launch, tmp_path):
    """patches/0018: the Industrial and Modern land and sea units and the civs' unique units are in the game (Rome's
    Legionary, the Rifleman, Infantry and Artillery); air units, missiles and leaders are not."""
    later = {"tech-44", "tech-58", "tech-59"}   # Nationalism, Replaceable Parts, Flight
    names = set()

    def know_all_but_later(g, me, capital):
        me["knownTechs"] = [t["id"] for t in g["techs"] if t["id"] not in later]
        me["eraCivilopediaName"] = "ERAS_Modern_Era"
        names.update(p["name"] for p in g["unitPrototypes"])

    b = edited_game(launch, tmp_path, know_all_but_later)
    assert len(names) == 76 and not names & LEFT_OUT
    assert {"Legionary", "Samurai", "Rifleman", "Tank", "Modern Armor", "Ironclad", "Transport", "Battleship"} <= names
    unlocks = {t["name"]: t["unlocks"] for t in b.call("techs")["available"]}
    assert "Rifleman" in unlocks["Nationalism"]
    assert {"Infantry", "Artillery"} <= set(unlocks["Replaceable Parts"])
    assert not {"Fighter", "Bomber"} & set(unlocks["Flight"])
    options = {o["name"] for o in b.call("city", city="c1")["options"]}
    assert not options & LEFT_OUT and "Warrior" not in options
    err = b.error("set_production", city="c1", item="Samurai")
    assert "unique unit of Japan" in err["message"]


def test_the_spearman_upgrades_to_the_pikeman(launch, tmp_path):
    """patches/0018: the Spearman's upgrades list the Pikeman and, for civs without one, the Musketman; the next step
    is the Pikeman (the engine took the alphabetically first, the Musketman, and logged a warning)."""
    def edit(g, me, capital):
        me["knownTechs"].extend([BRONZE_WORKING, IRON_WORKING, FEUDALISM])
        me["gold"] = 200
        next(t for t in g["map"]["tiles"] if (t["x"], t["y"]) == capital)["resource"] = "Iron"
        add_unit(g, me["id"], "Spearman", capital, 9001)

    b = edited_game(launch, tmp_path, edit)
    s = b.call("state")
    spear = at(s, (s["cities"][0]["x"], s["cities"][0]["y"]), "Spearman")
    assert spear["upgrade"] == {"to": "Pikeman", "gold": 30, "ok": True}
    options = [o["name"] for o in b.call("city", city="c1")["options"]]
    assert "Pikeman" in options and "Spearman" not in options


def test_a_unit_upgrades_in_its_city_for_gold(launch, tmp_path):
    """patches/0019: an Archer in the capital upgrades to a Longbowman for (40 - 20) x 3 gold, keeping its id and
    experience and using up its moves; one outside a city, one the treasury cannot pay for and a Warrior with nothing
    to become are refused with the reason. The upgrade outlives a save and load."""
    xy = {}

    def edit(g, me, capital):
        me["knownTechs"].extend([WARRIOR_CODE, INVENTION])
        me["gold"] = 100
        xy["out"] = next((t["x"], t["y"]) for t in g["map"]["tiles"]
                         if abs(t["x"] - capital[0]) + abs(t["y"] - capital[1]) == 4 and t["x"] == capital[0]
                         and t["baseTerrain"] not in ("coast", "sea", "ocean"))
        add_unit(g, me["id"], "Archer", capital, 9001, experience="Veteran")
        add_unit(g, me["id"], "Archer", capital, 9002)
        add_unit(g, me["id"], "Archer", xy["out"], 9003)
        add_unit(g, me["id"], "Warrior", capital, 9004)

    b = edited_game(launch, tmp_path, edit)
    s = b.call("state")
    home = (s["cities"][0]["x"], s["cities"][0]["y"])
    archers = [u for u in s["units"] if u["type"] == "Archer" and (u["x"], u["y"]) == home]
    vet, other = archers
    outside = at(s, xy["out"], "Archer")
    assert vet["upgrade"] == {"to": "Longbowman", "gold": 60, "ok": True} and "upgrade" in vet["orders"]
    assert "upgrade" not in outside and "upgrade" not in outside["orders"]
    hp = vet["hp"]

    res = b.call("unit_order", unit=vet["id"], order="upgrade")
    assert res["message"] == (f"{vet['id']} Archer is now a Longbowman (60 gold; 40 left). "
                              "It has no moves left this turn.")
    u = res["unit"]
    assert (u["id"], u["type"], u["x"], u["y"], u["moves_left"], u["hp"]) == (vet["id"], "Longbowman", *home, 0, hp)
    assert b.call("state")["gold"] == 40

    err = b.error("unit_order", unit=other["id"], order="upgrade")
    assert err["code"] == "invalid_order" and "costs 60 gold and you have 40" in err["message"]
    assert unit(b.call("state"), other["id"])["upgrade"] == {
        "to": "Longbowman", "gold": 60, "ok": False,
        "reason": "the upgrade to Longbowman costs 60 gold and you have 40"}
    err = b.error("unit_order", unit=outside["id"], order="upgrade")
    assert "only in one of your cities" in err["message"]
    warrior = at(s, home, "Warrior")
    err = b.error("unit_order", unit=warrior["id"], order="upgrade")
    assert "cannot build Legionary" in err["message"] and "Iron Working" in err["message"]
    assert b.call("state")["gold"] == 40 and unit(b.call("state"), other["id"])["type"] == "Archer"

    # Saved at the next turn's start and loaded again: the same engine id, now a veteran Longbowman.
    b.call("end_turn", skip_idle=True)
    world = b.call("world")
    restored = launch()
    restored.call("load", path=str(tmp_path / "b" / "autosave.json"))
    again = restored.call("world")
    assert next(u for u in again["units"] if u["id"] == "Archer-9001") == next(
        u for u in world["units"] if u["id"] == "Archer-9001")
    assert next(u for u in again["units"] if u["id"] == "Archer-9001")["type"] == "Longbowman"
    save = json.loads((tmp_path / "b" / "autosave.json").read_text())
    assert next(u for u in save["game"]["units"] if u["id"] == "Archer-9001")["experience"] == "Veteran"


def test_many_units_upgrade_until_the_gold_runs_out(launch, tmp_path):
    """patches/0019 through unit_orders: "all:Archer" upgrades each Archer in turn and stops, with a reason per unit,
    once the treasury cannot pay."""
    def edit(g, me, capital):
        me["knownTechs"].extend([WARRIOR_CODE, INVENTION])
        me["gold"] = 130
        for n in range(3):
            add_unit(g, me["id"], "Archer", capital, 9001 + n)

    b = edited_game(launch, tmp_path, edit)
    res = b.call("unit_orders", orders=[{"unit": "all:Archer", "order": "upgrade"}])
    assert [r["ok"] for r in res["results"]] == [True, True, False]
    assert "costs 60 gold and you have 10" in res["results"][2]["message"]
    assert b.call("state")["gold"] == 10


def ai_with_archers_in_its_cities(g: dict, player: dict) -> list[str]:
    player["knownTechs"].extend([WARRIOR_CODE, INVENTION])
    player["gold"] = 1000
    cities = [c for c in g["cities"] if c["owner"] == player["id"]]
    return [add_unit(g, player["id"], "Archer", (c["location"]["x"], c["location"]["y"]), 9100 + i)
            for i, c in enumerate(cities)]


def test_the_engine_ai_upgrades_its_garrisons(launch, tmp_path):
    """patches/0019: an AI with gold upgrades the Archers in its cities to Longbowmen in its turn and keeps a
    reserve; the engine AI does the same for the seat under autoplay's engine_ai, reported as unit_upgraded."""
    ids = {}

    def edit(g, me, capital):
        ai = next(p for p in g["players"] if not p["human"] and p["civilization"] != "Barbarians"
                  and any(c["owner"] == p["id"] for c in g["cities"]))
        ids["ai"] = ai_with_archers_in_its_cities(g, ai)
        ids["me"] = ai_with_archers_in_its_cities(g, me)

    b = edited_game(launch, tmp_path, edit, turn_limit=200)
    b.call("autoplay", turns=1, policy="engine_ai")
    world = b.call("world")
    types = {u["id"]: u["type"] for u in world["units"]}
    assert ids["ai"] and all(types[i] == "Longbowman" for i in ids["ai"])
    assert ids["me"] and all(types[i] == "Longbowman" for i in ids["me"])
    assert all(p["gold"] >= 0 for p in world["players"])
    events = [e for e in b.call("state")["last_events"] if e["kind"] == "unit_upgraded"]
    assert len(events) == len(ids["me"]) and "was upgraded to a Longbowman in" in events[0]["text"]


def test_the_engine_ai_does_not_upgrade_into_a_weaker_unit(launch, tmp_path):
    """patches/0019: some lines end in a weaker unit. The Inca's Chasqui Scout (1/1) upgrades to the Explorer (0/0):
    the agent may do it (and reads "an Explorer"), the engine AI does not, while it upgrades the Archer beside it."""
    def edit(g, me, capital):
        me["knownTechs"].extend([WARRIOR_CODE, INVENTION, ASTRONOMY])
        me["gold"] = 1000
        for n, proto in enumerate(["Chasqui Scout", "Chasqui Scout", "Archer"]):
            add_unit(g, me["id"], proto, capital, 9001 + n)

    b = edited_game(launch, tmp_path, edit, civ="Inca")
    s = b.call("state")
    chasqui = next(u for u in s["units"] if u["type"] == "Chasqui Scout")
    assert chasqui["upgrade"] == {"to": "Explorer", "gold": 3, "ok": True}
    res = b.call("unit_order", unit=chasqui["id"], order="upgrade")
    assert res["message"].startswith(f"{chasqui['id']} Chasqui Scout is now an Explorer (3 gold; ")
    b.call("autoplay", turns=1, policy="engine_ai")
    types = {u["id"]: u["type"] for u in b.call("world")["units"]}
    assert (types["Chasqui Scout-9001"], types["Chasqui Scout-9002"], types["Archer-9003"]) == (
        "Explorer", "Chasqui Scout", "Longbowman")
