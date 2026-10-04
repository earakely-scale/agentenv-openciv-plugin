"""Buildings multiply the city's science, gold and shields (patches/0016), and city's options say what each adds.

Each test edits a save the bridge wrote (a save carries its own building definitions) and loads it in fresh bridges,
so the variants differ only in the buildings of one city.
"""

from __future__ import annotations

import json
from pathlib import Path

import test_protocol
from test_protocol import SEED, Bridge, check_city_screen, check_finance

launch = test_protocol.launch  # the fixture

SCIENCE, LUXURY = 3, 3   # tax 4: every share of a city's commerce shows


def saved_game(launch, tmp_path: Path) -> tuple[dict, dict, str]:
    """An engine_ai game at T100 and its save; returns the save, the seat's city with the most useful commerce (in
    the save) and the seat's player id."""
    b = launch("--autosave", str(tmp_path / "a"))
    b.call("new_game", seed=SEED, turn_limit=400)
    b.call("autoplay", turns=100, policy="engine_ai", timeout=600)
    b.call("end_turn", skip_idle=True)
    s = b.call("state")
    best = max(s["cities"], key=lambda c: c["commerce"]["total"] - c["commerce"]["corrupt"])
    save = json.loads((tmp_path / "a" / "autosave.json").read_text())
    me = next(p["id"] for p in save["game"]["players"] if p["human"])
    city = next(c for c in save["game"]["cities"] if c["name"] == best["name"] and c["owner"] == me)
    return save, city, me


def load_with(launch, tmp_path: Path, save: dict, edit) -> Bridge:
    """A fresh bridge on the save after `edit(game)`, at the test's rates."""
    data = json.loads(json.dumps(save))
    edit(data["game"])
    path = tmp_path / f"edited-{len(list(tmp_path.glob('edited-*')))}.json"
    path.write_text(json.dumps(data))
    b = launch()
    b.call("load", path=str(path))
    b.call("set_rates", science=SCIENCE, luxury=LUXURY)
    return b


ECONOMY = {"Library", "University", "Research Lab", "Copernicus' Observatory", "Newton's University", "Marketplace",
           "Bank", "Stock Exchange", "Factory", "Manufacturing Plant", "Wall Street"}


def test_buildings_multiply_the_citys_yields(launch, tmp_path):
    """A Library adds half the science left after corruption and the rates, a University half more, Copernicus'
    Observatory all of it again; a Marketplace half the taxes and luxury; a Factory and a Manufacturing Plant a
    quarter of the useful shields each. Corruption is unchanged, `from_buildings` and `bonus` say what was added, and
    the city's Library and Marketplace options said beforehand exactly what they would add."""
    save, city, me = saved_game(launch, tmp_path)
    techs = {b["name"]: b.get("requiredTech") for b in save["game"]["buildings"]}

    def variant(*names: str) -> dict:
        def edit(g: dict) -> None:
            c = next(x for x in g["cities"] if x["id"] == city["id"])
            c["buildings"] = [b for b in c["buildings"] if b["building"] not in ECONOMY]
            c["buildings"] += [{"building": n, "builtByPlayer": me, "year": 1, "totalCulture": 0} for n in names]
            player = next(p for p in g["players"] if p["id"] == me)
            known = player["knownTechs"]
            known += [techs[n] for n in ("Library", "Marketplace") if techs[n] not in known]
        b = load_with(launch, tmp_path, save, edit)
        info = b.call("city", city=city["name"])
        info["state"] = b.call("state")
        b.close()
        assert not info["disorder"]
        return info

    base = variant()
    sci, tax, lux = (base["commerce"][k] for k in ("science", "taxes", "luxury"))
    useful = base["shields"]["useful"]
    assert min(sci, tax, lux) >= 2 and useful >= 2, base
    expected = {
        ("Library",): ({"science": sci * 150 // 100}, {"science": 50}),
        ("Library", "University"): ({"science": sci * 200 // 100}, {"science": 100}),
        ("Library", "Copernicus' Observatory"): ({"science": sci * 250 // 100}, {"science": 150}),
        ("Marketplace",): ({"taxes": tax * 150 // 100, "luxury": lux * 150 // 100}, {"tax": 50, "luxury": 50}),
        ("Factory", "Manufacturing Plant"): ({"useful": useful * 150 // 100}, {"shields": 50}),
    }
    got = {names: variant(*names) for names in expected}
    for names, (figures, _) in expected.items():
        info = got[names]
        parts = {k: info["commerce"][k] for k in ("science", "taxes", "luxury")} | {"useful": info["shields"]["useful"]}
        assert parts == {"science": sci, "taxes": tax, "luxury": lux, "useful": useful} | figures, names
        # The corruption and the tiles are the base's.
        assert info["commerce"]["corrupt"] == base["commerce"]["corrupt"], names
        assert info["shields"]["corrupt"] == base["shields"]["corrupt"], names
    assert got[("Library",)]["commerce"]["science"] > sci and got[("Marketplace",)]["commerce"]["taxes"] > tax

    # What was added is from_buildings, and bonus is the percentages; the city's figures still add up.
    assert base["bonus"] == {"science": 0, "tax": 0, "luxury": 0, "shields": 0}
    assert base["commerce"]["from_buildings"] == base["shields"]["from_buildings"] == 0
    for names, (_, bonus) in [((), ({}, {})), *expected.items()]:
        info = got.get(names, base)
        check_city_screen(info)
        check_finance(info["state"])
        assert info["bonus"] == {"science": 0, "tax": 0, "luxury": 0, "shields": 0} | bonus, names
        assert info["commerce"]["from_buildings"] == sum(
            info["commerce"][k] - base["commerce"][k] for k in ("science", "taxes", "luxury")), names
        assert info["shields"]["from_buildings"] == info["shields"]["useful"] - useful, names

    # city's options said what the Library and the Marketplace would add here, and it is what they added.
    options = {o["name"]: o for o in base["options"]}
    library, market = got[("Library",)]["commerce"], got[("Marketplace",)]["commerce"]
    assert options["Library"]["effects"][0] == f"+50% science (+{library['science'] - sci} here)"
    assert options["Marketplace"]["effects"][0] == (
        f"+50% tax and luxury (+{market['taxes'] - tax} gold, +{market['luxury'] - lux} luxury here)")
    assert "+3 culture" in options["Library"]["effects"] and "upkeep 1" in options["Library"]["effects"]
    assert all(o["effects"] for o in base["options"] if o["kind"] == "building")
    assert all("effects" not in o for o in base["options"] if o["kind"] != "building")


def test_wall_street_pays_interest(launch, tmp_path):
    """Wall Street earns 5% of the treasury a turn, at most 50 gold (Rules.TreasuryInterestRate and MaxInterest), and
    the gold arrives at the turn's end."""
    save, city, me = saved_game(launch, tmp_path)
    for gold, interest in ((400, 20), (3000, 50)):
        def edit(g: dict, gold=gold) -> None:
            next(x for x in g["cities"] if x["id"] == city["id"])["buildings"].append(
                {"building": "Wall Street", "builtByPlayer": me, "year": 1, "totalCulture": 0})
            next(p for p in g["players"] if p["id"] == me)["gold"] = gold
        b = load_with(launch, tmp_path, save, edit)
        s = b.call("state")
        assert s["finance"]["income"]["interest"] == interest
        check_finance(s)
        assert "Wall Street" in b.call("city", city=city["name"])["buildings"]
        b.call("end_turn", skip_idle=True)
        assert b.call("state")["gold"] == gold + s["gold_per_turn"]
        b.close()


def test_the_ai_values_a_building_by_what_it_adds_in_order(launch, tmp_path):
    """The engine picks what a city builds next by ChooseProducible's scores: a capital whose science comes from eight
    Scientists, at the end of a unit that costs no population, picks a Library, which adds half of it (with only its
    culture to count, it picks a Settler). In a city that riots and makes nothing, a Library's option says what it
    would add once order returns, the figure the AI counts too, not +0.

    At T100 the civ is still expanding however the game went, and a Settler scores up to about 45 before the +/-10%
    draw (40 for sites at home, 45 with a ship to take one overseas); the Library scores 5 a beaker it adds. Eight
    Scientists make that 76 here against the Settler's 40, and a Library on each of seeds 1-8. With four, as this
    test once had, the two were close and which won turned on how many cities and sites the civ had. A Settler
    finished there would take two citizens, and the re-assignment can take Scientists with them, so the capital
    finishes a unit that costs none."""
    save, _, me = saved_game(launch, tmp_path)
    capital = next(c for c in save["game"]["cities"] if c["owner"] == me and c["capital"])
    nationality = capital["residents"][0]["nationality"]
    free = {u["name"] for u in save["game"]["unitPrototypes"] if not u.get("populationCost")}

    def scientists(g: dict) -> None:
        c = next(x for x in g["cities"] if x["id"] == capital["id"])
        c["buildings"] = [b for b in c["buildings"] if b["building"] not in ECONOMY]
        c["shieldsStored"] = 400   # whatever it builds is done at the turn's end
        c["residents"] += [{"citizenType": "CitizenType-4", "nationality": nationality, "city": c["id"]}] * 8
    b = load_with(launch, tmp_path, save, scientists)
    b.call("set_rates", science=6, luxury=0)
    options = b.call("city", city=capital["name"])["options"]
    unit = next(o["name"] for o in options if o["kind"] == "unit" and o["name"] in free)
    assert b.call("set_production", city=capital["name"], item=unit)["city"]["producing"] == unit
    info = b.call("city", city=capital["name"])
    assert info["commerce"]["science"] >= 30 and not info["disorder"]
    assert "Library" in {o["name"] for o in info["options"]}
    b.call("end_turn", skip_idle=True)
    assert b.call("city", city=capital["name"])["producing"] == "Library"
    b.close()

    # Six more laborers on the free tiles next to the capital: at the turn's end it riots.
    def crowded(g: dict) -> None:
        c = next(x for x in g["cities"] if x["id"] == capital["id"])
        c["buildings"] = [b for b in c["buildings"] if b["building"] not in ECONOMY]
        x, y = c["location"]["x"], c["location"]["y"]
        worked = {(r["tileWorked"]["x"], r["tileWorked"]["y"]) for d in g["cities"] for r in d["residents"]
                  if "tileWorked" in r}
        near = [(x + dx, y + dy) for dx, dy in ((1, -1), (1, 1), (-1, -1), (-1, 1), (2, 0), (-2, 0), (0, 2), (0, -2),
                                                (3, -1), (3, 1), (-3, -1), (-3, 1), (2, -2), (2, 2), (-2, -2), (-2, 2))]
        free = [t for t in near if t not in worked and t[0] >= 0][:6]
        assert len(free) == 6
        c["residents"] += [{"citizenType": "CitizenType-1", "nationality": nationality, "city": c["id"],
                            "tileWorked": {"x": tx, "y": ty}} for tx, ty in free]
    b = load_with(launch, tmp_path, save, crowded)
    b.call("set_rates", science=6, luxury=0)
    in_order = b.call("city", city=capital["name"])
    assert not in_order["disorder"] and in_order["commerce"]["science"] >= 4
    library = next(o for o in in_order["options"] if o["name"] == "Library")["effects"][0]
    b.call("end_turn", skip_idle=True)
    riot = b.call("city", city=capital["name"])
    assert riot["disorder"] and riot["commerce"]["science"] == riot["shields"]["useful"] == 0
    # The same tiles, rates and corruption as before the riot: the same figure, which is more than nothing.
    assert next(o for o in riot["options"] if o["name"] == "Library")["effects"][0] == library
    assert library != "+50% science (+0 here)"
    b.close()
