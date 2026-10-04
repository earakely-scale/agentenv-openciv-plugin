"""The cultural victory in the env: the brief's CULTURE line once culture matters, the rules text and GAME OVER."""

from __future__ import annotations

from test_render import brief_of, state

from agentenv_openciv3 import render


def civ(name: str | None, culture: int, you: bool = False) -> dict:
    return {"civ": name, "you": you, "score": 60, "land": 0.1, "pop": 0.1, "culture": culture}


def cultured(you: int, top: dict, second: dict, best: dict) -> dict:
    s = state(3, idle=2, standing=4, n_events=4)
    s["race"] |= {"you": civ("Rome", you, you=True), "nearest_culture": top, "culture_runner_up": second,
                  "best_city": best, "culture_goal": 100_000, "city_culture_goal": 20_000}
    return s


def test_the_culture_line_waits_until_culture_matters():
    s = cultured(900, civ("Greece", 9_999), civ("Rome", 900, you=True),
                 {"civ": "Greece", "you": False, "name": "Athens", "culture": 1_999})
    assert render.culture_race(s) is None and "CULTURE" not in render.brief(s, start_techs=2)
    s["race"]["best_city"]["culture"] = 2_000
    assert render.culture_race(s) == ("CULTURE you 900, top Greece 9.9k (11.1x the next) · best city Athens (Greece) "
                                      "2k · a civ wins at 100k and 2x the next, or a city at 20k")
    assert render.culture_race({**s, "race": None}) is None
    assert render.culture_race({**s, "race": {k: v for k, v in s["race"].items() if k != "nearest_culture"}}) is None


def test_the_culture_line_when_the_seat_leads_or_an_unmet_civ_does():
    s = cultured(99_999, civ("Rome", 99_999, you=True), civ(None, 50_000),
                 {"civ": "Rome", "you": True, "name": "Rome", "culture": 19_999})
    line = render.culture_race(s)
    assert line.startswith("CULTURE you 99.9k (1.9x the next) · best city Rome (yours) 19.9k · ")   # never rounded up
    s = cultured(12_000, civ(None, 31_000), civ("Rome", 12_000, you=True),
                 {"civ": None, "you": False, "name": None, "culture": 4_321})
    assert render.culture_race(s).startswith("CULTURE you 12k, top an unmet civ 31k (2.5x the next) · best city ? "
                                             "(an unmet civ) 4.3k · ")
    s["race"]["culture_runner_up"] = None   # one civ left
    assert "x the next)" not in render.culture_race(s)


def test_a_brief_with_the_culture_line_fits_its_budget():
    s = cultured(52_000, civ("Hittites", 61_000), civ("Rome", 52_000, you=True),
                 {"civ": "Egypt", "you": False, "name": "Thebes", "culture": 5_800})
    text = brief_of(s, plan_chars=300)
    assert "\nCULTURE you 52k, top Hittites 61k (1.1x the next) · best city Thebes (Egypt) 5.8k · " in text
    assert len(text) / 4 < 600, len(text) / 4


def test_rules_text_and_game_over_name_the_cultural_victory():
    rivals = [{"civ": "Greece", "agent": True}, {"civ": "Zululand", "agent": False}]
    line = render.match_line({"turn_limit": 300, "rivals": rivals})
    assert "or has a city of 20,000 culture or 100,000 culture and twice the next civ's (culture); else the top" \
           in line
    over = {"turn": 480, "turn_limit": 540, "civ": "Rome", "score": {"total": 9, "cities": 1, "pop": 1, "tiles": 1,
                                                                     "techs": 1}}
    victory = {"kind": "culture", "civ": "Greece", "label": None, "turn": 480}
    assert render.game_over({**over, "victory": victory}, None).startswith("GAME OVER — Greece won by culture on T480.")
    assert render.game_over({**over, "victory": {**victory, "civ": "Rome"}}, None).startswith(
        "GAME OVER — you won by culture on T480.")
