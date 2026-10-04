"""city_info's building effects and the city's bonus from its buildings (bridge patches/0016)."""

from test_render import city

from agentenv_openciv3 import render


def test_a_building_option_shows_its_first_two_effects():
    c = city(1, "Rome", 12, 10, 3)
    library = {"name": "Library", "kind": "building", "cost": 80, "turns": 6,
               "effects": ["+50% science (+2 here)", "+3 culture", "upkeep 1"]}
    assert render.option_text(library, c) == "Library 80 (6t): +50% science (+2 here); +3 culture"
    walls = {"name": "Walls", "kind": "building", "cost": 20, "turns": 2, "effects": ["+50% defence up to size 6"]}
    assert render.option_text(walls, c) == "Walls 20 (2t): +50% defence up to size 6"
    # No effects (a unit, or an older bridge): the option as before.
    assert render.option_text({"name": "Settler", "kind": "unit", "cost": 30, "turns": 6}, c) == "Settler 30 (6t)"
    assert render.option_text({**library, "effects": []}, c) == "Library 80 (6t)"


def test_city_detail_shows_what_its_buildings_add():
    c = city(1, "Rome", 12, 10, 3, buildings=["Palace", "Library", "Marketplace"],
             bonus={"science": 50, "tax": 50, "luxury": 50, "shields": 0}, options=[
                 {"name": "University", "kind": "building", "cost": 160, "turns": 20,
                  "effects": ["+50% science (+3 here)", "+3 culture"]}])
    text = render.city_detail(c)
    assert "  buildings: Palace, Library, Marketplace · bonus +50% science +50% tax +50% luxury" in text
    assert "  can build: University 160 (20t): +50% science (+3 here); +3 culture" in text
    # Nothing added, or no bonus field: no bonus.
    assert render.bonus_text(city(1, "Rome", 12, 10, 3, bonus={"science": 0, "tax": 0, "luxury": 0,
                                                                "shields": 0})) is None
    assert "bonus" not in render.city_detail(city(1, "Rome", 12, 10, 3))
