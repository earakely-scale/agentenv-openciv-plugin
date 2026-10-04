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
