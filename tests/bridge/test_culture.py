"""Civ III's cultural victory in the bridge (Seats.cs CheckVictory): a city with 20,000 culture points, or a
civilization with 100,000 and at least twice the next one's. The saves are edited to raise the culture."""

from __future__ import annotations

import json
from pathlib import Path

import test_protocol
from test_protocol import SEED, Bridge, found_capital

launch = test_protocol.launch   # the fixture


def edit(path: Path, change) -> Path:
    save = json.loads(path.read_text())
    change(save["game"])
    path.write_text(json.dumps(save))
    return path


def player_ids(g: dict) -> tuple[str, list[str]]:
    me = next(p for p in g["players"] if p["human"])["id"]
    return me, [p["id"] for p in g["players"] if not p["human"] and "Barbarian" not in p["civilization"]]


def spread(g: dict, owner: str, total: int) -> None:
    """Gives the owner's cities `total` culture points in all, about evenly but no two equal: the engine's border
    rules fail on two cities that claim a tile at the same distance with the same culture (GameData.cs, Law VI)."""
    cities = [c for c in g["cities"] if c["owner"] == owner]
    values = [total // len(cities) + i for i in range(len(cities))]
    values[0] += total - sum(values)
    assert max(values) < 19_000 and len(set(values)) == len(values)
    for c, v in zip(cities, values, strict=True):
        c["perPlayerCulture"][owner] = v


def city_game(launch, tmp_path) -> Path:
    b = launch("--autosave", str(tmp_path / "a"))
    b.call("new_game", seed=SEED, turn_limit=50)
    found_capital(b)
    b.call("end_turn", skip_idle=True)
    b.close()
    return tmp_path / "a" / "autosave.json"


def civ_game(launch, tmp_path) -> Path:
    """Tiny, one rival, 100 engine-AI turns: Rome has enough cities to hold 100,000 culture under 20,000 a city."""
    b = launch("--autosave", str(tmp_path / "a"))
    b.call("new_game", seed=SEED, size="Tiny", opponents=1, turn_limit=400)
    b.call("autoplay", turns=100, policy="engine_ai", timeout=600)
    b.close()
    return tmp_path / "a" / "autosave.json"


def loaded(launch, path: Path, *extra: str) -> Bridge:
    r = launch(*extra)
    r.call("load", path=str(path))
    return r


def set_capital(culture: int):
    def change(g: dict) -> None:
        me, _ = player_ids(g)
        next(c for c in g["cities"] if c["owner"] == me)["perPlayerCulture"][me] = culture
    return change


def test_a_city_of_20000_culture_wins_by_culture(launch, tmp_path):
    """The capital one point short of 20,000: its palace's point this turn makes it, and Rome wins by culture; the
    victory, a kind string, survives a load."""
    path = edit(city_game(launch, tmp_path), set_capital(19_999))
    r = loaded(launch, path, "--autosave", str(tmp_path / "b"))
    s = r.call("state")
    assert s["victory"] is None and s["race"]["best_city"] == {"civ": "Rome", "you": True, "name": "Rome",
                                                                "culture": 19_999}
    res = r.call("end_turn", skip_idle=True)
    turn = r.call("state")["turn"]
    victory = {"kind": "culture", "civ": "Rome", "label": None, "turn": turn}
    assert res["game_over"] and r.call("state")["victory"] == r.call("score")["victory"] == victory
    assert r.call("city", city="c1")["culture"]["total"] >= 20_000
    assert res["events"][-1]["kind"] == "victory"
    assert res["events"][-1]["text"].startswith("Rome won by culture: Rome has 20,00")
    assert res["events"][-1]["text"].endswith(" culture points (20,000 win).")
    assert f"Rome won by culture on turn {turn}" in r.error("end_turn", skip_idle=True)["message"]

    again = loaded(launch, tmp_path / "b" / "autosave.json")
    assert again.call("state")["victory"] == victory and again.call("state")["game_over"]
    assert again.error("end_turn", skip_idle=True)["code"] == "game_over"
    assert again.error("unit_order", unit="u2", order="hold")["code"] == "game_over"


def test_no_culture_victory_below_20000(launch, tmp_path):
    r = loaded(launch, edit(city_game(launch, tmp_path), set_capital(19_000)))
    res = r.call("end_turn", skip_idle=True)
    assert not res["game_over"] and r.call("state")["victory"] is None
    assert "victory" not in [e["kind"] for e in res["events"]]
    assert 19_000 < r.call("state")["race"]["best_city"]["culture"] < 20_000


def test_an_ai_city_of_20000_culture_wins_for_the_ai(launch, tmp_path):
    """Any civilization's city counts, the AI's too, and the seat that has not met it reads no name."""
    def change(g: dict) -> None:
        _, rivals = player_ids(g)
        city = next(c for c in g["cities"] if c["owner"] in rivals)
        city["perPlayerCulture"][city["owner"]] = 19_999
    r = loaded(launch, edit(city_game(launch, tmp_path), change))
    best = r.call("state")["race"]["best_city"]
    assert best["culture"] == 19_999 and not best["you"]
    assert best["civ"] is None and best["name"] is None   # an unmet civ's city: no names
    res = r.call("end_turn", skip_idle=True)
    victory = r.call("state")["victory"]
    assert res["game_over"] and victory["kind"] == "culture" and victory["civ"] != "Rome"
    assert res["events"][-1]["text"].startswith(f"{victory['civ']} won by culture: ")


def test_100000_culture_wins_only_at_twice_the_next_civ(launch, tmp_path):
    """Rome's cities hold 100,000 in all (none near 20,000): against the rival's 60,000 (1.67x) it has not won,
    against 45,000 it has; 90,000 against next to nothing is short of 100,000."""
    path = civ_game(launch, tmp_path)
    original = path.read_text()

    def culture(rome: int, rival: int):
        def change(g: dict) -> None:
            me, [other] = player_ids(g)
            spread(g, me, rome)
            spread(g, other, rival)
        path.write_text(original)
        return loaded(launch, edit(path, change))

    r = culture(100_000, 60_000)
    race = r.call("state")["race"]
    assert race["you"]["culture"] == 100_000 and race["nearest_culture"]["you"]
    assert race["culture_runner_up"]["culture"] == 60_000 and race["best_city"]["culture"] < 20_000
    res = r.call("end_turn", skip_idle=True)
    assert not res["game_over"] and r.call("state")["victory"] is None

    r = culture(90_000, 7)
    res = r.call("end_turn", skip_idle=True)
    assert not res["game_over"] and r.call("state")["victory"] is None
    assert r.call("state")["race"]["you"]["culture"] < 100_000

    r = culture(100_000, 45_000)
    res = r.call("end_turn", skip_idle=True)
    turn = r.call("state")["turn"]
    assert res["game_over"] and r.call("state")["victory"] == {"kind": "culture", "civ": "Rome", "label": None,
                                                                "turn": turn}
    players = {p["civ"]: p["culture"] for p in r.call("score")["players"]}
    rival = next(c for c in players if c != "Rome")
    assert players["Rome"] >= 100_000 and players["Rome"] >= 2 * players[rival]
    assert res["events"][-1]["text"] == (f"Rome won by culture: {players['Rome']:,} culture points, at least twice "
                                         f"{rival}'s {players[rival]:,}.")


def test_race_carries_culture(launch, tmp_path):
    """The race and score carry each civ's culture (its cities' culture, as the engine's history counts it), the
    city with the most and the goals; reading them changes nothing."""
    b = launch()
    b.call("new_game", seed=SEED, turn_limit=200)
    b.call("autoplay", turns=60, policy="engine_ai", timeout=600)
    s = b.call("state")
    race = s["race"]
    assert race["culture_goal"] == 100_000 and race["city_culture_goal"] == 20_000
    mine = [b.call("city", city=c["id"])["culture"]["total"] for c in s["cities"]]
    assert mine and race["you"]["culture"] == sum(mine) > 0
    assert race["best_city"]["culture"] >= max(mine)
    players = b.call("score")["players"]
    assert all(p["culture"] >= 0 for p in players)
    alive = sorted((p["culture"] for p in players if not p["defeated"]), reverse=True)
    assert race["nearest_culture"]["culture"] == alive[0] and race["culture_runner_up"]["culture"] == alive[1]
    assert next(p for p in players if p["is_human"])["culture"] == race["you"]["culture"]
    for key in ("you", "leader", "nearest_domination", "nearest_culture", "culture_runner_up"):
        assert set(race[key]) == {"civ", "you", "score", "land", "pop", "culture"}
    assert set(race["best_city"]) == {"civ", "you", "name", "culture"}
    assert b.call("state")["race"] == race
