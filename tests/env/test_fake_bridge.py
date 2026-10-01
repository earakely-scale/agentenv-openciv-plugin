"""The fake bridge's replies have the shapes docs/protocol.md specifies, so the env tests exercise the real contract."""

import json
import re
from pathlib import Path

import pytest

from agentenv_openciv3.bridge import Bridge, BridgeError

pytestmark = pytest.mark.anyio

PROTOCOL = (Path(__file__).resolve().parents[2] / "docs" / "protocol.md").read_text()
EXAMPLES = [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", PROTOCOL, re.S)]
STATE_EXAMPLE = next(e for e in EXAMPLES if "blockers" in e)
MAP_EXAMPLE = next(e for e in EXAMPLES if "tiles" in e)
SCORE = {"total", "cities", "pop", "tiles", "techs"}


def assert_shape(example, actual, where="result"):
    """Same keys at every level; lists are compared by their first element."""
    if isinstance(example, dict):
        assert isinstance(actual, dict) and set(actual) == set(example), f"{where}: {set(actual) ^ set(example)}"
        for k, v in example.items():
            if v is not None and actual[k] is not None:
                assert_shape(v, actual[k], f"{where}.{k}")
    elif isinstance(example, list) and example and actual:
        assert_shape(example[0], actual[0], f"{where}[0]")


@pytest.fixture
async def bridge(fake_cmd):
    b = Bridge(fake_cmd)
    await b.new_game(seed=1, turn_limit=10)
    yield b
    await b.close()


async def test_state_and_map_match_the_documented_examples(bridge):
    await bridge.call("unit_order", unit="u1", order="found_city")
    await bridge.call("set_production", city="c1", item="Warrior")
    await bridge.call("set_research", tech="Pottery")
    await bridge.call("end_turn", skip_idle=True)
    state = await bridge.call("state")
    settler = next(u for u in state["units"] if u["type"] == "Settler")
    assert_shape(STATE_EXAMPLE, {**state, "units": [settler], "last_events": [
        {k: v for k, v in e.items() if k not in ("x", "y")} for e in state["last_events"]]})
    m = await bridge.call("map", x=12, y=10, radius=2)
    assert_shape(MAP_EXAMPLE, {**m, "tiles": [next(t for t in m["tiles"] if t["city"] and t["units"])]})


async def test_documented_result_keys(bridge):
    sites = await bridge.call("city_sites", unit="u1", top=2)
    assert set(sites) == {"origin", "sites", "note"}
    assert set(sites["sites"][0]) == {"x", "y", "score", "dist", "dir", "turns", "terrain", "river", "coastal",
                                      "yield"}
    order = await bridge.call("unit_order", unit="u3", order="settle", x=16, y=12)
    assert set(order) == {"message", "unit", "city", "path"} and set(order["path"]) == {"length", "turns"}
    order = await bridge.call("unit_order", unit="u1", order="found_city")
    city = await bridge.call("city", city="c1")
    assert set(city) == set(order["city"]) | {"options", "tiles_worked"}
    assert set(city["options"][0]) == {"name", "kind", "cost", "turns"}
    assert set(await bridge.call("set_production", city="c1", item="Warrior")) == {"message", "city"}
    techs = await bridge.call("techs")
    assert set(techs) == {"current", "turns_left", "known", "available"}
    assert set(techs["available"][0]) == {"name", "cost", "turns", "era", "unlocks"}
    assert set(await bridge.call("set_research", tech="Writing")) == {"message", "current", "queue"}
    blocked = await bridge.call("end_turn")
    assert set(blocked) == {"blocked", "blockers"} and blocked["blocked"]
    done = await bridge.call("end_turn", skip_idle=True)
    assert set(done) == {"blocked", "turns_advanced", "turn", "game_over", "defeated", "events", "auto"}
    played = await bridge.call("autoplay", turns=2, policy="engine_ai", record=True)
    assert set(played) == {"turn", "game_over", "defeated", "score", "trajectory"}
    assert set(played["score"]) == SCORE and set(played["trajectory"][0]) == {"turn", "score"}
    score = await bridge.call("score")
    assert set(score) == {"turn", "human", "players"}
    assert set(score["players"][0]) == {"civ", "is_human", "defeated", "score"}


async def test_documented_error_codes(bridge):
    cases = [("unit_order", {"unit": "u9", "order": "hold"}, "unknown_unit"),
             ("unit_order", {"unit": "u2", "order": "settle", "x": 16, "y": 12}, "invalid_order"),
             ("unit_order", {"unit": "u1", "order": "goto", "x": 13, "y": 12}, "bad_target"),
             ("set_production", {"city": "c4", "item": "Warrior"}, "unknown_city"),
             ("set_research", {"tech": "Alchemy"}, "unknown_tech"),
             ("set_research", {"tech": "Alphabet"}, "already_known"),
             ("new_game", {"seed": 2}, "already_started")]
    for cmd, args, code in cases:
        with pytest.raises(BridgeError) as info:
            await bridge.call(cmd, **args)
        assert info.value.code == code, cmd
