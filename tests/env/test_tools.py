import base64
import io
import json
import re
import shutil

import anyio
import pytest
from agentenv_protocol import DataPart
from PIL import Image

from agentenv_openciv3.server import OpenCiv3Env

pytestmark = pytest.mark.anyio

FOOTER = re.compile(r"\[T\d+/\d+ · (needs orders: [\w, ]+|nothing needs orders)\]$")


async def found_capital(tools):
    return await tools("unit_order", unit="u1", order="found_city")


async def settle_everything(tools):
    """Leave nothing pending: capital, production, research and standing orders for every unit."""
    await found_capital(tools)
    await tools("set_production", city="c1", item="Warrior")
    await tools("research", tech="Pottery")
    await tools("unit_order", unit="u2", order="auto_work")
    await tools("unit_order", unit="u3", order="settle", x=16, y=12)
    await tools("unit_order", unit="u4", order="explore")


async def test_sixteen_tools_with_descriptions(env):
    listed = {t.name: t for t in await env.mcp.list_tools()}
    assert set(listed) == {"get_turn_brief", "list_units", "view_map", "find_city_sites", "unit_order", "unit_orders",
                           "city_info", "set_production", "research", "set_rates", "buy", "revolution", "diplomacy",
                           "end_turn", "plan", "message"}
    assert all(t.description and t.outputSchema is None for t in listed.values())
    assert "Lost context? Call get_turn_brief." in listed["get_turn_brief"].description


async def test_revolution_changes_government_after_anarchy(env, tools):
    await found_capital(tools)
    brief = await tools("get_turn_brief")
    assert ("GOVERNMENT Despotism: -1 on any tile yield above 2 · can choose Monarchy (no tile penalty, hurry with "
            'gold, corruption problematic) → revolution(government="Monarchy") after a few turns of anarchy') in brief
    text = await tools("revolution", government="monarchy")
    assert text.startswith("(read 'monarchy' as 'Monarchy') Revolution: anarchy until turn 3, then Monarchy.")
    assert "Government: Anarchy, anarchy until T3, then Monarchy" in text
    assert "  Monarchy: no tile penalty, hurry with gold, corruption problematic, 3 free units per city" in text
    assert "GOVERNMENT anarchy, then Monarchy" in await tools("get_turn_brief")
    await tools("end_turn", skip_idle=True)
    await tools("end_turn", skip_idle=True)
    brief = await tools("get_turn_brief")
    assert brief.startswith("T3/8 (3850 BC) · Rome · Monarchy") and "GOVERNMENT" not in brief


async def test_diplomacy_war_and_the_price_of_peace(env, tools):
    assert "none yet: explore to meet them" in await tools("diplomacy")
    await env.bridge.call("_game", met=True)
    env.seat.cache = None
    assert "Greece · at peace · score" in await tools("diplomacy")
    text = await tools("diplomacy", action="declare_war", civ="greece")
    assert text.startswith("(read 'greece' as 'Greece') Rome declared war on Greece")
    assert "Greece · AT WAR (talks refused until T3)" in text
    assert "!! at war with Greece" in await tools("get_turn_brief")
    assert "refuses to talk to you until turn 3" in await tools.error("diplomacy", action="propose_peace", civ="Greece")
    await tools("end_turn", skip_idle=True)
    await tools("end_turn", skip_idle=True)
    assert "!! at war with Greece (peace: 50 gold)" in await tools("get_turn_brief")
    err = await tools.error("diplomacy", action="propose_peace", civ="Greece")
    assert "Greece wants 50 gold for peace" in err and 'diplomacy(action="propose_peace", civ="Greece", gold=50)' in err
    text = await tools("diplomacy", action="propose_peace", civ="Greece", gold=50)
    assert text.startswith("Peace with Greece, for 50 gold.") and "Greece · at peace" in text
    assert "needs civ" in await tools.error("diplomacy", action="declare_war")


async def test_attack_targets_are_listed_and_taken(env, tools):
    await tools("end_turn", skip_idle=True)
    await tools("end_turn", skip_idle=True)
    await env.bridge.call("_unit", id="u4", pos=[17, 11], moves=1.0)
    env.seat.cache = None
    assert "attack: (18,12) SE Barbarians Warrior 3/3 hp 50% to win" in await tools("list_units", filter="all")
    text = await tools("unit_order", unit="u4", order="attack", x=18, y=12)
    assert "and won: the Barbarians Warrior was destroyed." in text
    # The battle record is for the play page's animation; the agent's text stays the message.
    [battle] = (await env.bridge.call("known_map"))["battles"]
    assert battle["attacker"]["id"] == "u4" and battle["winner"] == "attacker"
    assert "rounds" not in text and "hp_before" not in text


async def test_turn_brief(tools):
    text = await tools("get_turn_brief")
    lines = text.splitlines()
    assert lines[0].startswith("T1/8 (3950 BC) · Rome · Despotism · gold 10")
    assert "RESEARCH none" in text
    assert "PACE cities 0 BEHIND (1 by T1)" in text
    assert "NEEDS ORDERS (4)" in text
    assert 'u1 Settler (12,10) 1/1mv · idle · found here: yes → unit_order(unit="u1", order="found_city")' in text
    assert 'found here: yes → unit_order(unit="u3", order="found_city") · best site' in text
    assert "u4 Warrior (14,10) 1/1mv · idle · orders: explore goto fortify hold disband" in text
    assert "VS" not in text  # baselines are off in this fixture
    assert lines[-1] == "PLAN none — record your strategy with plan(text=...)"


async def test_brief_after_founding_points_settler_to_best_site(tools):
    await found_capital(tools)
    text = await tools("get_turn_brief")
    assert ("u3 Settler (13,11) 1/1mv · idle · found here: no — adjacent to Rome (12,10); cities need one empty tile "
            'between them · best site (16,12) 2 E score 41 → unit_order(unit="u3", order="settle", x=16, y=12)') in text
    assert ('the engine picked Warrior for Rome → set_production(city="c1", item="Warrior") keeps it, '
            'city_info(city="c1") lists options') in text
    assert "nothing is being researched → research() lists techs" in text
    assert "CITIES\n  c1 Rome (12,10) size 1 · food +2/t grows 8t · Warrior 0/10 → 5t (engine pick) · no defender" \
        in text
    assert "ATTENTION\n  no defender: c1 Rome" in text


async def test_list_units(tools):
    text = await tools("list_units")
    assert text.splitlines()[0] == "UNITS needing orders (4 of 4) T1"
    assert "u1 Settler (12,10) 1/1mv · idle · orders: settle found_city goto hold disband · found here: yes" in text
    await tools("unit_order", unit="u4", order="fortify")
    text = await tools("list_units")
    assert "u4" not in text
    everything = await tools("list_units", filter="all")
    assert "u4 Warrior (14,10) 1/1mv · fortified · orders: explore goto fortify hold disband wake" in everything
    assert "Worker" in everything and "found here" not in everything.splitlines()[2]


async def test_list_units_filter_is_validated(tools):
    assert "Input should be 'needs_orders' or 'all'" in await tools.error("list_units", filter="idle")


async def test_view_map(tools):
    await found_capital(tools)
    text = await tools("view_map")
    lines = text.splitlines()
    assert lines[0] == "MAP around c1 (12,10), radius 3 — explored tiles only"
    header = lines[1]
    row10 = next(line for line in lines if line.startswith("   10 "))
    col = header.index(" 12")
    assert row10[col:col + 3] == "@g "  # your city on grassland, in the 3 columns of its x label
    col14 = header.index(" 14")
    assert row10[col14:col14 + 3] == "#g'"  # your warrior on a river tile
    row9 = next(line for line in lines if line.startswith("    9 "))
    col13 = header.index(" 13")
    assert row9[col13:col13 + 3] == " g*"  # wheat
    assert "legend: @ your city" in text and "g grassland" in text
    assert "center (12,10) Grassland, yield 2/0/0 f/s/c" in text
    assert "cities: c1 Rome size 1 (12,10) here" in text
    assert "resources: Wheat (13,9) 1 NE" in text
    assert "good city sites: (16,12) score 41 3 SE" in text
    assert len(text) // 4 < 700


async def test_view_map_marks_foreign_units_and_cities(tools):
    await settle_everything(tools)
    await tools("end_turn", until_attention=True, max_turns=3)
    text = await tools("view_map", x=16, y=10, radius=4)
    assert "!! hostile units: Barbarians Warrior (18,12) 2 SE" in text
    assert "Athens (Greece) size 2 (18,6) 3 NE" in text
    row12 = next(line for line in text.splitlines() if line.startswith("   12 "))
    header = text.splitlines()[1]
    col = header.index(" 18")
    assert row12[col] == "!"
    row6 = next(line for line in text.splitlines() if line.startswith("    6 "))
    assert row6[col] == "C"


async def test_view_map_around_a_unit_and_bad_input(tools):
    text = await tools("view_map", around="u3", radius=2)
    assert text.startswith("MAP around u3 Settler (13,11), radius 2")
    assert "center (13,11) Plains" in text
    err = await tools.error("view_map", around="u99")
    assert "there is no unit or city 'u99'." in err and "Valid: u1, u2, u3, u4" in err
    assert "give both x and y" in await tools.error("view_map", x=12)
    assert "less than or equal to 6" in await tools.error("view_map", radius=7)


async def test_find_city_sites(tools):
    text = await tools("find_city_sites")
    lines = text.splitlines()
    assert lines[0] == "CITY SITES from u1 Settler at (12,10) (only sites on the unit's continent are scored)"
    assert lines[1].startswith("#1 (13,9) score 46 · 1 NE · 1t away · area food 18 shields 3 commerce 2 · grassland")
    assert lines[1].endswith("river")
    assert lines[-1].startswith('Do this: unit_order(unit="u1", order="settle", x=13, y=9)')
    text = await tools("find_city_sites", unit="u3", top=2)
    assert len([line for line in text.splitlines() if line.startswith("#")]) == 2
    assert "CITY SITES from u3 Settler at (13,11)" in text


async def test_unit_order_success_has_footer(tools):
    text = await found_capital(tools)
    assert text.splitlines()[0] == "Founded Rome at (12,10)."
    assert "c1 Rome (12,10) size 1" in text
    assert text.splitlines()[-1] == "[T1/8 · needs orders: research, c1, u2, u3, u4]"
    text = await tools("unit_order", unit="u3", order="settle", x=16, y=12)
    assert text.splitlines()[0] == ("u3 Settler will walk to (16,12) and found a city on arrival. "
                                    "(path 2 tiles, 2t)")
    assert "u3 Settler (13,11) 0/1mv · settle→(16,12) 2 E" in text
    assert FOOTER.search(text)


async def test_unit_order_errors_are_actionable(tools):
    await found_capital(tools)
    err = await tools.error("unit_order", unit="u3", order="found_city")
    assert err.startswith("Error executing tool unit_order: cannot found a city at (13,11): adjacent to Rome (12,10)")
    assert "Nearest valid sites: (16,12) 2 E · 2t · score 41 | (17,13) 3 SE · 3t · score 41" in err
    assert 'Do this: unit_order(unit="u3", order="settle", x=16, y=12)' in err
    assert err.endswith("[T1/8 · needs orders: research, c1, u2, u3, u4]")

    err = await tools.error("unit_order", unit="u2", order="explore")
    assert "u2 Worker cannot explore." in err and "Valid: auto_work, goto, build_road" in err
    err = await tools.error("unit_order", unit="u9", order="hold")
    assert "you have no unit 'u9'." in err and "Valid: u2, u3, u4" in err
    err = await tools.error("unit_order", unit="u3", order="goto", x=13, y=12)
    assert "x+y must be even" in err


async def test_city_info(tools):
    assert (await tools("city_info")).startswith("No cities yet")
    await found_capital(tools)
    text = await tools("city_info", city="c1")
    assert text.splitlines()[0] == ("c1 Rome (12,10) size 1 capital · food 0/15 +2/t grows 8t · shields 2/t "
                                    "· works 2 tiles · no defender")
    assert text.splitlines()[1] == "  mood happy 0 content 1 unhappy 0 · defenders 0"
    assert "  producing Warrior 0/10 → 5t (engine pick)" in text
    assert ("  can build: Settler 30 (15t) · Worker 10 (5t) · Warrior 10 (5t) · Barracks 40 (20t): veteran land units; "
            "upkeep 1 · Wealth") in text
    assert text == await tools("city_info")
    err = await tools.error("city_info", city="c7")
    assert "you have no city 'c7'." in err and "Valid: c1" in err


async def test_set_production(tools):
    await found_capital(tools)
    text = await tools("set_production", city="c1", item="Settler")
    assert text.splitlines()[0] == "Rome now builds Settler."
    assert "Settler 0/30 → 15t (delivered at size 3) · no defender" in text
    assert "c1" not in text.splitlines()[-1].split("needs orders:")[-1]
    err = await tools.error("set_production", city="c1", item="Pyramids")
    assert "Rome cannot build 'Pyramids'." in err and "Valid: Settler, Worker, Warrior, Barracks, Wealth" in err
    assert FOOTER.search(err)
    text = await tools("set_production", city="c1", item="warriors")
    assert text.splitlines()[0] == "(read 'warriors' as 'Warrior') Rome now builds Warrior."


async def test_production_for_many_cities_and_a_queue(tools, env_vars):
    await found_capital(tools)
    text = await tools("set_production", city="c1", item="Warrior", then=["Settler", "Warrior"])
    assert text.splitlines()[0] == "Rome now builds Warrior."
    assert " · then Settler, Warrior" in text.splitlines()[1]
    text = await tools("set_production", city="all", item="Settler")
    assert text.splitlines()[0] == "1 cities now build Settler." and "then Settler, Warrior" in text   # queue kept
    err = await tools.error("set_production", city="all", item="Pyramids")   # no city can: the first one's reason
    assert "Rome cannot build 'Pyramids'." in err
    log = [json.loads(line) for line in open(env_vars["OPENCIV_ACTION_LOG"])]
    assert any(r["tool"] == "set_production" and r["args"].get("then") == ["Settler", "Warrior"] for r in log)


async def test_unit_orders_gives_many_units_their_orders(tools):
    await found_capital(tools)
    text = await tools("unit_orders", orders=[{"unit": "idle:Worker", "order": "auto_work"},
                                              {"unit": "u4", "order": "explore"}, {"unit": "u99", "order": "hold"}])
    lines = text.splitlines()
    assert lines[0] == "2 orders done, 1 failed."
    assert lines[1].startswith("  u2: ") and lines[2].startswith("  u4: ") and lines[3].startswith("  u99: ✗ ")
    assert FOOTER.search(lines[-1])
    units = await tools("list_units", filter="all", type="worker")
    assert units.splitlines()[0].startswith("UNITS worker (all 1)") and "auto" in units
    err = await tools.error("unit_orders", orders=[])
    assert "orders" in err


async def test_research(tools):
    text = await tools("research")
    assert text.startswith("RESEARCH current: none · known 2: Alphabet, Bronze Working\nAVAILABLE")
    assert "  Pottery 16 beakers 8t → Granary, Hanging Gardens" in text
    text = await tools("research", tech="Monarchy")
    assert text.splitlines()[0].startswith("Researching ")
    assert "Monarchy" in text.splitlines()[0] and FOOTER.search(text)
    err = await tools.error("research", tech="Alchemy")
    assert "there is no tech 'Alchemy'." in err and "Valid: Masonry, Pottery" in err
    text = await tools("research", tech="potery")
    assert text.startswith("(read 'potery' as 'Pottery') ")
    assert "you already know Alphabet." in await tools.error("research", tech="Alphabet")


async def test_end_turn_blocked(tools, action_log):
    await found_capital(tools)
    err = await tools.error("end_turn")
    assert "END TURN BLOCKED (T1) — 5 decisions pending:" in err
    assert '  nothing is being researched → research(tech="...") — options: research()' in err
    assert ('  the engine picked Warrior for Rome → set_production(city="c1", item="Warrior") keeps it, '
            'city_info(city="c1") lists options') in err
    assert ('  u4 Warrior has moves and no orders → unit_order(unit="u4", order="...") — valid: explore, goto, '
            "fortify, hold, disband") in err
    assert "Or end_turn(skip_idle=true)" in err
    assert err.endswith("[T1/8 · needs orders: research, c1, u2, u3, u4]")
    row = action_log()[-1]
    assert row["tool"] == "end_turn" and row["ok"] is False and row["error_code"] == "blocked"
    assert row["idle_units"] == 3 and row["turns_advanced"] == 0


async def test_end_turn_report_and_next_brief(tools, action_log):
    await settle_everything(tools)
    text = await tools("end_turn")
    assert text.startswith("TURN T1 → T2 (1 turn)")
    report, brief = text.split("\n---\n")
    assert "T2/8 (3900 BC) · Rome" in brief and "EVENTS" not in brief
    assert brief.splitlines()[-1] == "[T2/8 · nothing needs orders]"
    text = await tools("end_turn", until_attention=True, max_turns=5)
    report, brief = text.split("\n---\n")
    assert report == ("TURN T2 → T3 (1 turn)\n  T2: Veii founded (16,12)\n"
                      "  T3: !! Barbarians Warrior 3 tiles E of Rome (18,12) · !! Rome has no defender and a hostile "
                      "unit is 3 tiles away (12,10)")
    assert 'the engine picked Warrior for Veii → set_production(city="c2", item="Warrior") keeps it' in brief
    row = action_log()[-1]
    assert row["ok"] and row["turns_advanced"] == 1 and row["idle_units"] == 0


async def test_skip_idle_holds_units_and_picks_research(tools, action_log):
    text = await tools("end_turn", skip_idle=True)
    assert "auto: research picked: Masonry" in text
    assert action_log()[-1]["idle_units"] == 4


async def test_game_over(tools, action_log):
    await settle_everything(tools)
    text = await tools("end_turn", skip_idle=True, until_attention=True, max_turns=20)
    while "GAME OVER" not in text:
        text = await tools("end_turn", skip_idle=True, until_attention=True, max_turns=20)
    assert "GAME OVER — turn 8/8 reached." in text
    assert re.search(r"FINAL score \d+ \(cities \d+, pop \d+, tiles \d+, techs \d+\) · units \d+ · gold \d+", text)
    assert "no further actions are possible" in text and text.endswith("[GAME OVER T8/8]")
    for tool, args in (("end_turn", {}), ("unit_order", {"unit": "u2", "order": "hold"}),
                       ("set_production", {"city": "c1", "item": "Warrior"}), ("research", {"tech": "Writing"})):
        err = await tools.error(tool, **args)
        assert "the game is over (T8/8)" in err and err.endswith("[GAME OVER T8/8]")
    assert (await tools("get_turn_brief")).startswith("GAME OVER")
    assert "UNITS" in await tools("list_units", filter="all")
    assert [r["error_code"] for r in action_log() if r["tool"] == "research"][-1] == "game_over"


async def test_plan(tools):
    assert (await tools("plan")).startswith("No plan yet")
    assert await tools("plan", text="  Settle (16,12), then Pottery.  ") == \
        "Plan saved at T1 (29/1000 chars); every brief shows it."
    assert await tools("plan") == "PLAN (T1) Settle (16,12), then Pottery."
    assert "PLAN (T1) Settle (16,12), then Pottery." in await tools("get_turn_brief")
    err = await tools.error("plan", text="x" * 1001)
    assert "the plan is 1,001 characters; the limit is 1,000." in err
    assert await tools("plan") == "PLAN (T1) Settle (16,12), then Pottery."


async def test_same_error_three_times(tools):
    for _ in range(2):
        assert "same error" not in await tools.error("unit_order", unit="u2", order="settle", x=16, y=12)
    err = await tools.error("unit_order", unit="u2", order="settle", x=16, y=12)
    assert "same error 3x — try one of: get_turn_brief() | end_turn(skip_idle=true)" in err
    await found_capital(tools)
    for _ in range(2):
        await tools.error("unit_order", unit="u3", order="found_city")
    err = await tools.error("unit_order", unit="u3", order="found_city")
    assert ('same error 3x — try one of: unit_order(unit="u3", order="settle", x=16, y=12) | get_turn_brief() | '
            "end_turn(skip_idle=true)") in err


async def test_repeat_counts_reset_next_turn(tools):
    for _ in range(2):
        await tools.error("end_turn")
    await tools("end_turn", skip_idle=True)
    await tools.error("research", tech="Alchemy")
    assert "same error" not in await tools.error("research", tech="Alchemy")


async def test_call_budget_nudge(tools):
    for _ in range(24):
        await tools("plan")
    assert "consider end_turn" not in await tools("list_units")
    assert (await tools("list_units")).endswith("(26 calls this turn — consider end_turn(skip_idle=true))")
    assert "consider end_turn(skip_idle=true)" in await tools.error("research", tech="Alchemy")
    await tools("end_turn", skip_idle=True)
    assert "consider end_turn" not in await tools("plan")


async def test_action_log_rows(tools, action_log):
    await tools("get_turn_brief")
    await tools("view_map", around="u1")
    await tools.error("unit_order", unit="u2", order="found_city")
    rows = action_log()
    assert [r["tool"] for r in rows] == ["get_turn_brief", "view_map", "unit_order"]
    assert set(rows[0]) == {"ts", "turn", "tool", "args", "ok", "error_code", "ms"}
    assert rows[1]["args"] == {"radius": 3, "around": "u1"} and rows[1]["turn"] == 1
    assert rows[2]["ok"] is False and rows[2]["error_code"] == "invalid_order"
    assert rows[0]["ts"].endswith("+00:00") and rows[0]["ms"] >= 0


async def test_data_plane(env, tools):
    await tools("unit_order", unit="u1", order="found_city")
    await tools.error("research", tech="Alchemy")
    [part] = await env.data_get()
    data = part.data
    assert {k: data[k] for k in ("turn", "turn_limit", "game_over", "defeated", "seed", "civ")} == \
        {"turn": 1, "turn_limit": 8, "game_over": False, "defeated": False, "seed": 3, "civ": "Rome"}
    assert data["score"] == {"total": 30, "cities": 1, "pop": 1, "tiles": 9, "techs": 2}
    assert data["metrics"] == {"cities": 1, "pop": 1, "techs": 2, "tiles": 9, "units": 3, "gold": 10,
                               "explored_pct": 6.7, "government": "Despotism"}
    assert data["actions"] == {"ok": 1, "invalid": 1, "max_consecutive_errors": 1}
    assert data["baselines"] == {p: {"status": "disabled"} for p in ("engine_ai", "settler_bot", "null")}
    assert data["decisions"] == {"production": {"agent": 0, "engine": 0}, "research": {"agent": 0, "engine": 0}}
    assert data["harness"] == {"autoplay_turns": 0, "new_games": 1, "extension_calls": 0, "engine_restarts": 0}
    assert data["engine_failed"] is False

    await env.data_add([DataPart(data={"scenario": {"seed": 9, "turn_limit": 20}})])
    [part] = await env.data_get()
    assert (part.data["seed"], part.data["turn_limit"], part.data["score"]["cities"]) == (9, 20, 0)
    assert part.data["actions"]["ok"] == 0
    await tools("plan", text="keep me?")
    await env.data_reset()
    assert (await tools("plan")).startswith("No plan yet")
    with pytest.raises(ValueError, match="unknown scenario keys"):
        await env.data_add([DataPart(data={"scenario": {"map_size": "tiny"}})])
    with pytest.raises(ValueError, match="expects a DataPart"):
        await env.data_add([DataPart(data={"seed": 1})])
    with pytest.raises(Exception, match="unknown size 'Gigantic'"):
        await env.data_add([DataPart(data={"scenario": {"size": "Gigantic"}})])
    assert env.scenario == {"seed": 9, "turn_limit": 20}
    assert (await tools("get_turn_brief")).startswith("T1/20")


async def test_extensions(env):
    game = await env.new_game(seed=4, turn_limit=12)
    assert game["seed"] == 4 and game["scenario"]["turn_limit"] == 12
    res = await env.autoplay(turns=3, policy="engine_ai")
    assert res["turn"] == 4 and [p["turn"] for p in res["trajectory"]] == [2, 3, 4]
    assert res["score"]["cities"] >= 1


async def test_action_log_directory_is_created(env_vars, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCIV_ACTION_LOG", str(tmp_path / "run" / "actions.jsonl"))
    env = OpenCiv3Env()
    env.create_app()
    try:
        await env.mcp.call_tool("plan", {})
    finally:
        await env.close()
    assert (tmp_path / "run" / "actions.jsonl").read_text().count("\n") == 1


async def test_scenario_from_env_vars(env_vars, monkeypatch):
    monkeypatch.setenv("OPENCIV_SIZE", "Small")
    monkeypatch.setenv("OPENCIV_OPPONENTS", "5")
    assert OpenCiv3Env().scenario == {"seed": 3, "turn_limit": 8, "size": "Small", "opponents": 5}


async def test_baselines_feed_the_brief_and_data_get(env_vars, monkeypatch):
    monkeypatch.setenv("OPENCIV_BASELINES", "1")
    env = OpenCiv3Env()
    env.create_app()
    try:
        tools_text = (await env.mcp.call_tool("get_turn_brief", {}))[0].text
        assert "VS T1 (same seed)" in tools_text
        with anyio.fail_after(20):
            while any(r.status == "running" for r in env.baselines.runs.values()):
                await anyio.sleep(0.05)
        text = (await env.mcp.call_tool("get_turn_brief", {}))[0].text
        vs = (r"VS T1 \(same seed\) built-in AI \d+ \(\d+ cit\w+, pop \d+, techs \d+\) · settler bot \d+ .* · "
              r"do-nothing \d+")
        assert re.search(vs, text)
        [part] = await env.data_get()
        b = part.data["baselines"]
        assert b["engine_ai"]["status"] == "done" and b["engine_ai"]["final_turn"] == 8
        assert b["settler_bot"]["status"] == "done" and b["settler_bot"]["final"]["cities"] >= 1
        assert b["engine_ai"]["final"]["cities"] >= 1 and b["null"]["final"]["cities"] == 0
        assert b["null"]["at_turn"] is not None
    finally:
        await env.close()


async def test_set_rates(tools):
    await found_capital(tools)
    text = await tools("set_rates", luxury=2)
    assert text.splitlines()[:2] == ["Rates set: science 60%, tax 20%, luxury 20%.",
                                     "tax 20% · science 60% · luxury 20% · gold +0/t"]
    assert "tax 20% sci 60% lux 20%" in await tools("get_turn_brief")
    err = await tools.error("set_rates", science=8)
    assert "each rate is 0-6" in err and FOOTER.search(err)
    assert "give science, luxury or both" in await tools.error("set_rates")
    assert "less than or equal to 10" in await tools.error("set_rates", science=11)


async def test_buy(env, tools):
    await found_capital(tools)
    err = await tools.error("buy", city="c1")
    assert "would take the lives of too many citizens (1); it has 1." in err
    await env.bridge.call("_city", id="c1", size=2)
    text = await tools("buy", city="c1")
    assert text.splitlines()[0] == ("Rome hurried Warrior: 1 citizen(s) were put to work; it completes next turn. "
                                    "(cost: 1 population)")
    assert "c1 Rome (12,10) size 1 · food +2/t grows 8t · Warrior 10/10" in text


async def test_disorder_blocks_with_its_fixes(env, tools):
    await settle_everything(tools)
    await env.bridge.call("_city", id="c1", size=5)
    env.seat.cache = None
    brief = await tools("get_turn_brief")
    assert ("!! Rome is in civil disorder: raise luxury (set_rates), move a military unit into the city, or let it "
            'shrink → set_rates(science=4, luxury=2) or unit_order(unit="u4", order="goto", x=12, y=10) then fortify'
            ) in brief
    assert "Warrior 0/10 no progress (city in disorder) · !! DISORDER · no defender" in brief
    err = await tools.error("end_turn")
    assert 'set_rates(science=4, luxury=2) or unit_order(unit="u4", order="goto", x=12, y=10) then fortify' in err
    await tools("set_rates", science=0, luxury=6)
    assert "DISORDER" not in await tools("get_turn_brief")


async def test_skip_idle_accepts_engine_picks(env, tools):
    await found_capital(tools)
    await tools("end_turn", skip_idle=True)
    [part] = await env.data_get()
    assert part.data["turn"] == 2
    brief = await tools("get_turn_brief")
    assert "the engine picked" not in brief and "Warrior 2/10 → 4t (engine pick)" in brief


async def test_lone_surrogates_never_reach_the_transport(tools):
    await tools("plan", text="settle east \ud83c")
    assert await tools("plan") == "PLAN (T1) settle east"
    assert "PLAN (T1) settle east" in await tools("get_turn_brief")
    err = await tools.error("view_map", around="u\ud800")
    err.encode("utf-8")
    assert "there is no unit or city 'u\\ud800'" in err


async def test_find_city_sites_for_a_worker_never_suggests_settling(tools):
    text = await tools("find_city_sites", unit="u2")
    assert "settle" not in text
    assert text.splitlines()[-1] == "u2 Worker cannot found cities; sites are ranked from where it stands."
    assert "nearby legal sites (within 4 tiles):" in await tools("find_city_sites", unit="u1")


async def test_goto_onto_a_foreign_unit_names_the_occupant(tools):
    err = await tools.error("unit_order", unit="u4", order="goto", x=18, y=6)
    assert "(18,6) is occupied by Athens (Greece)" in err


async def test_killed_engine_is_restored_from_the_autosave(env, tools):
    await settle_everything(tools)
    await tools("end_turn")
    await tools("unit_order", unit="u2", order="hold")
    before = await tools("list_units", filter="all")
    env.bridge._proc.kill()
    await env.bridge._proc.wait()
    after = await tools("list_units", filter="all")
    assert after.splitlines()[0] == ("!! the engine restarted from the start of turn 2; orders given since then are "
                                     "lost.")
    assert "u2 Worker (12,10) 0/1mv · working hold" not in after and before != after
    assert "!! the engine restarted from the start of turn 2" in await tools("get_turn_brief")
    [part] = await env.data_get()
    assert part.data["harness"]["engine_restarts"] == 1 and part.data["turn"] == 2
    await tools("end_turn", skip_idle=True)


async def test_engine_that_keeps_dying_ends_the_game_cleanly(env_vars, monkeypatch, tool_env):
    env, tools = await tool_env(FAKE_BRIDGE_CRASH_ON="end_turn")
    await found_capital(tools)
    err = await tools.error("end_turn", skip_idle=True)
    assert "the game engine exited during end_turn" in err
    assert "!! the engine restarted from the start of turn 1; orders given since then are lost." in err
    assert "Do this: get_turn_brief()" in err
    for _ in range(3):
        err = await tools.error("end_turn", skip_idle=True)
    assert "the game engine failed 3 times on turn 1; the game cannot continue." in err
    assert "the game cannot continue" in await tools.error("list_units")
    [part] = await env.data_get()
    assert part.data["engine_failed"] is True and part.data["turn"] == 1 and part.data["score"]["cities"] == 0


async def test_unrestorable_engine_never_serves_stale_state(env, tools):
    await found_capital(tools)
    await tools("get_turn_brief")
    (env.game_dir / "autosave" / "autosave.json").write_text("not a save")
    env.bridge._proc.kill()
    await env.bridge._proc.wait()
    err = await tools.error("list_units")
    assert "could not be restarted" in err and "UNITS" not in err
    [part] = await env.data_get()
    assert part.data["engine_failed"] is True and part.data["score"]["cities"] == 1


async def test_failed_new_game_keeps_the_running_game(env, tools):
    await settle_everything(tools)
    await tools("end_turn")
    with pytest.raises(Exception, match="unknown civ 'Atlantis'"):
        await env.data_add([DataPart(data={"scenario": {"civ": "Atlantis"}})])
    with pytest.raises(ValueError, match="opponents must be an integer 1-11"):
        await env.data_add([DataPart(data={"scenario": {"opponents": "3"}})])
    with pytest.raises(ValueError, match="turn_limit must be an integer 1-1000"):
        await env.new_game(turn_limit=100000)
    [part] = await env.data_get()
    assert (part.data["turn"], part.data["seed"], part.data["score"]["cities"]) == (2, 3, 1)
    assert part.data["harness"]["new_games"] == 1
    assert (await tools("get_turn_brief")).startswith("T2/8")


async def test_autoplay_is_counted_and_chunked(env, tools, monkeypatch):
    monkeypatch.setattr("agentenv_openciv3.server.AUTOPLAY_CHUNK", 3)
    await env.new_game(turn_limit=20)
    res = await env.autoplay(turns=7, policy="settler_bot")
    assert res["turn"] == 8 and [p["turn"] for p in res["trajectory"]] == list(range(2, 9))
    [part] = await env.data_get()
    assert part.data["harness"] == {"autoplay_turns": 7, "new_games": 1, "extension_calls": 2, "engine_restarts": 0}
    assert env.seat.actions.timeline[1] == [{"text": "autoplay 7 turns (settler_bot)", "ok": True}]


async def test_recording_extension(env, tools):
    await settle_everything(tools)
    await tools.error("unit_order", unit="u2", order="explore")
    await tools("end_turn", until_attention=True, max_turns=3)
    res = await env.recording(formats=["html", "png", "gif"], fps=2)
    assert res["turns"] == 3 and res["notes"] == []
    files = {f["name"]: f for f in res["files"]}
    assert set(files) == {"openciv3-seed3.html", "openciv3-seed3.png", "openciv3-seed3.gif"}
    page = base64.b64decode(files["openciv3-seed3.html"]["base64"]).decode()
    doc = json.loads(page.split("window.OPENCIV_DATA = ", 1)[1].split("; window.OPENCIV_VIDEOS = ", 1)[0])
    assert [t["turn"] for t in doc["turns"]] == [1, 2, 3] and doc["static"]["tiles"]
    assert {"text": "u3 settle → (16,12)", "ok": True} in doc["turns"][1]["actions"]["0"]
    assert {"text": "u2 explore ✗ invalid_order", "ok": False} in doc["turns"][1]["actions"]["0"]
    png = Image.open(io.BytesIO(base64.b64decode(files["openciv3-seed3.png"]["base64"])))
    assert png.size[0] % 2 == 0 and png.size[1] >= 720
    assert files["openciv3-seed3.gif"]["content_type"] == "image/gif"
    with pytest.raises(ValueError, match="unknown formats"):
        await env.recording(formats=["avi"])


async def test_recording_mp4_or_gif_fallback(env, tools, monkeypatch):
    await found_capital(tools)
    monkeypatch.setenv("PATH", "")
    res = await env.recording(formats=["mp4"])
    assert [f["name"] for f in res["files"]] == ["openciv3-seed3.gif"]
    assert "the mp4 was replaced by an animated gif" in res["notes"][0]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
async def test_client_recording_from_the_kept_saves(fake_client, tool_env):
    env, tools = await tool_env()
    await found_capital(tools)
    await tools("end_turn", skip_idle=True)
    assert "--saves" in env.bridge.cmd
    res = await env.recording()
    files = {f["name"]: f for f in res["files"]}
    assert set(files) == {"openciv3-seed3.mp4", "openciv3-seed3.html", "openciv3-seed3.client.mp4"}
    assert files["openciv3-seed3.client.mp4"]["content_type"] == "video/mp4" and res["notes"] == []


async def test_client_mp4_without_a_client_is_a_note(env, tools, env_vars):
    await found_capital(tools)
    res = await env.recording(formats=["png", "client_mp4"])
    assert [f["name"] for f in res["files"]] == ["openciv3-seed3.png"]
    assert res["notes"] == [f"client_mp4 skipped: the OpenCiv3 client is not installed at {env_vars['OPENCIV_CLIENT']} "
                            "(build the image with --target client)"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
async def test_a_failed_client_render_is_a_note(fake_client, tool_env):
    (fake_client / "capture.sh").write_text("#!/bin/sh\nexit 0\n")
    env, tools = await tool_env()
    await found_capital(tools)
    res = await env.recording(formats=["png", "client_mp4"])
    assert res["notes"][0].startswith("client_mp4 failed: the client rendered 0 of 1 turns (exit 0)")


async def test_recording_can_be_off(env_vars, monkeypatch, tool_env):
    env, tools = await tool_env(OPENCIV_RECORD="0")
    await found_capital(tools)
    assert "--record" not in env.bridge.cmd
    with pytest.raises(ValueError, match="recording is off"):
        await env.recording()
