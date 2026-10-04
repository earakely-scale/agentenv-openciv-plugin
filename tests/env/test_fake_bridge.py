"""The fake bridge's replies have the shapes docs/protocol.md specifies, so the env tests exercise the real contract."""

import json
import re
from pathlib import Path

import pytest

from agentenv_openciv3.bridge import Bridge, BridgeError

pytestmark = pytest.mark.anyio

DOCS = Path(__file__).resolve().parents[2] / "docs"
PROTOCOL = (DOCS / "protocol.md").read_text()
RECORDING = (DOCS / "recording.md").read_text()
EXAMPLES = [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", PROTOCOL, re.S)]
STATE_EXAMPLE = next(e for e in EXAMPLES if "blockers" in e)
MAP_EXAMPLE = next(e for e in EXAMPLES if "center" in e)
SNAPSHOT_EXAMPLE = json.loads(re.search(r"```json\n(.*?)\n```", RECORDING, re.S).group(1))
SCORE = {"total", "cities", "pop", "tiles", "techs"}


def assert_shape(example, actual, where="result", doc=PROTOCOL):
    """Every documented key at every level, and no key the doc never names; lists compare first elements."""
    if isinstance(example, dict):
        assert isinstance(actual, dict), where
        assert set(example) <= set(actual), f"{where}: missing {set(example) - set(actual)}"
        undocumented = [k for k in set(actual) - set(example) if not re.search(rf"\b{k}\b", doc)]
        assert not undocumented, f"{where}: undocumented {undocumented}"
        for k, v in example.items():
            if v is not None and actual[k] is not None:
                assert_shape(v, actual[k], f"{where}.{k}", doc)
    elif isinstance(example, list) and example and actual:
        assert_shape(example[0], actual[0], f"{where}[0]", doc)


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
    assert set(sites) == {"origin", "sites", "nearby", "note"}
    assert set(sites["nearby"][0]) == set(sites["sites"][0])
    assert set(sites["sites"][0]) == {"x", "y", "score", "dist", "dir", "turns", "terrain", "river", "coastal",
                                      "yield"}
    order = await bridge.call("unit_order", unit="u3", order="settle", x=16, y=12)
    assert set(order) == {"message", "unit", "city", "path", "battle"} and set(order["path"]) == {"length", "turns"}
    assert order["battle"] is None
    order = await bridge.call("unit_order", unit="u1", order="found_city")
    city = await bridge.call("city", city="c1")
    assert set(city) == set(order["city"]) | {"options", "tiles_worked", "worked", "workable", "culture", "strategic",
                                              "luxuries", "specialists", "citizens"}
    assert set(city["culture"]) == {"per_turn", "total", "next_border"} and len(city["citizens"]) == city["size"]
    assert set(city["commerce"]) == {"total", "taxes", "science", "luxury", "corrupt", "wealth", "from_buildings"}
    assert set(city["shields"]) == {"total", "useful", "corrupt", "from_buildings"}
    assert set(city["bonus"]) == {"science", "tax", "luxury", "shields"}
    finance = (await bridge.call("state"))["finance"]
    assert finance["income"]["total"] - finance["expenses"]["total"] == (await bridge.call("state"))["gold_per_turn"]
    assert city["worked"][0][:2] == [city["x"], city["y"]] and len(city["worked"]) == city["size"] + 1
    assert all(len(t) == 5 for t in city["worked"]) and all(len(t) == 2 for t in city["workable"])
    assert set(city["options"][0]) == {"name", "kind", "cost", "turns"}
    buildings = [o for o in city["options"] if o["kind"] == "building"]
    assert buildings and all(set(o) == {"name", "kind", "cost", "turns", "effects"} for o in buildings)
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
    rates = await bridge.call("set_rates", science=4, luxury=2)
    assert set(rates) == {"message", "rates", "gold_per_turn", "turns_left_research"}
    await bridge.call("_city", id="c1", size=4)
    bought = await bridge.call("hurry", city="c1")
    assert set(bought) == {"message", "gold_cost", "pop_cost", "city"}
    score = await bridge.call("score")
    assert set(score) == {"turn", "human", "players", "human_share"} and set(score["human_share"]) == {"land", "pop"}
    assert set(score["players"][0]) == {"civ", "is_human", "defeated", "score"}


async def test_documented_error_codes(bridge):
    cases = [("unit_order", {"unit": "u9", "order": "hold"}, "unknown_unit"),
             ("unit_order", {"unit": "u2", "order": "settle", "x": 16, "y": 12}, "invalid_order"),
             ("unit_order", {"unit": "u4", "order": "goto", "x": 13, "y": 12}, "bad_target"),
             ("set_production", {"city": "c4", "item": "Warrior"}, "unknown_city"),
             ("set_research", {"tech": "Alchemy"}, "unknown_tech"),
             ("set_research", {"tech": "Alphabet"}, "already_known"),
             ("unit_order", {"unit": "u4", "order": "goto", "x": 18, "y": 6}, "occupied"),
             ("set_rates", {"science": 9, "luxury": 0}, "bad_rates"),
             ("hurry", {"city": "c1"}, "cannot_hurry"),
             ("new_game", {"seed": 2}, "already_started")]
    await bridge.call("unit_order", unit="u1", order="found_city")
    for cmd, args, code in cases:
        with pytest.raises(BridgeError) as info:
            await bridge.call(cmd, **args)
        assert info.value.code == code, cmd


async def test_world_snapshot_matches_recording_md(fake_cmd, tmp_path):
    b = Bridge([*fake_cmd, "--record", str(tmp_path)])
    try:
        await b.new_game(seed=3, turn_limit=10)
        await b.call("unit_order", unit="u1", order="found_city")
        await b.call("end_turn", skip_idle=True, until_attention=True, max_turns=3)
        world = await b.call("world")
    finally:
        await b.close()
    assert_shape(SNAPSHOT_EXAMPLE, world, doc=RECORDING)
    assert len(world["tiles"]) == 60 * 60 // 2 and len(world["tiles"][0]) == 10
    assert world["players"][-1]["civ"] == "Barbarians"
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files[0] == "turn-0001.json.gz" and len(files) == 1 + (world["turn"] - 1)
