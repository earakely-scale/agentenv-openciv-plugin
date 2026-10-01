import re

import anyio
import pytest
from agentenv_protocol import DataPart

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


async def test_ten_tools_with_descriptions(env):
    listed = {t.name: t for t in await env.mcp.list_tools()}
    assert set(listed) == {"get_turn_brief", "list_units", "view_map", "find_city_sites", "unit_order", "city_info",
                           "set_production", "research", "end_turn", "plan"}
    assert all(t.description and t.outputSchema is None for t in listed.values())
    assert "Lost context? Call get_turn_brief." in listed["get_turn_brief"].description


async def test_turn_brief(tools):
    text = await tools("get_turn_brief")
    lines = text.splitlines()
    assert lines[0].startswith("T1/8 · Rome · Despotism · gold 10")
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
    assert 'Rome is producing nothing → set_production(city="c1", item="...")' in text
    assert "nothing is being researched → research() lists techs" in text
    assert "CITIES\n  c1 Rome (12,10) size 1 · food +2/t grows 8t · NOTHING in production" in text


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
    assert "!! foreign units: Barbarians Warrior (18,12) 2 SE" in text
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
                                    "· works 2 tiles")
    assert "  NOTHING in production" in text
    assert "  can build: Settler 30 (15t) · Worker 10 (5t) · Warrior 10 (5t) · Barracks 40 (20t) · Wealth" in text
    assert text == await tools("city_info")
    err = await tools.error("city_info", city="c7")
    assert "you have no city 'c7'." in err and "Valid: c1" in err


async def test_set_production(tools):
    await found_capital(tools)
    text = await tools("set_production", city="c1", item="Settler")
    assert text.splitlines()[0] == "Rome now builds Settler."
    assert "Settler 0/30 → 15t (completes only at size 3+)" in text
    assert "c1" not in text.splitlines()[-1].split("needs orders:")[-1]
    err = await tools.error("set_production", city="c1", item="Pyramids")
    assert "Rome cannot build 'Pyramids'." in err and "Valid: Settler, Worker, Warrior, Barracks, Wealth" in err
    assert FOOTER.search(err)


async def test_research(tools):
    text = await tools("research")
    assert text.startswith("RESEARCH current: none · known 2: Alphabet, Bronze Working\nAVAILABLE")
    assert "  Pottery 16 beakers 8t → Granary, Hanging Gardens" in text
    text = await tools("research", tech="Monarchy")
    assert text.splitlines()[0].startswith("Researching ")
    assert "Monarchy" in text.splitlines()[0] and FOOTER.search(text)
    err = await tools.error("research", tech="Alchemy")
    assert "there is no tech 'Alchemy'." in err and "Valid: Masonry, Pottery" in err
    assert "you already know Alphabet." in await tools.error("research", tech="Alphabet")


async def test_end_turn_blocked(tools, action_log):
    await found_capital(tools)
    err = await tools.error("end_turn")
    assert "END TURN BLOCKED (T1) — 5 decisions pending:" in err
    assert '  nothing is being researched → research(tech="...") — options: research()' in err
    assert '  Rome is producing nothing → set_production(city="c1", item="...") — options: city_info(city="c1")' in err
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
    assert "T2/8 · Rome" in brief and "EVENTS" not in brief
    assert brief.splitlines()[-1] == "[T2/8 · nothing needs orders]"
    text = await tools("end_turn", until_attention=True, max_turns=5)
    report, brief = text.split("\n---\n")
    assert report == ("TURN T2 → T3 (1 turn)\n  T2: Veii founded (16,12)\n"
                      "  T3: !! Barbarian Warrior 3 tiles E of Rome (18,12)")
    assert 'Veii is producing nothing → set_production(city="c2", item="...")' in brief
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
    assert "no further actions are possible" in text
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
                               "explored_pct": 6.7}
    assert data["actions"] == {"ok": 1, "invalid": 1, "max_consecutive_errors": 1}
    assert data["baselines"] == {"null": {"status": "disabled"}, "engine_ai": {"status": "disabled"}}

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
        vs = r"VS T1 \(same seed\) built-in AI \d+ \(\d+ cit\w+, pop \d+, techs \d+\) · do-nothing \d+"
        assert re.search(vs, text)
        [part] = await env.data_get()
        b = part.data["baselines"]
        assert b["engine_ai"]["status"] == "done" and b["engine_ai"]["final_turn"] == 8
        assert b["engine_ai"]["final"]["cities"] >= 1 and b["null"]["final"]["cities"] == 0
        assert b["null"]["at_turn"] is not None
    finally:
        await env.close()
