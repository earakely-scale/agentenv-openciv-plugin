"""End-to-end tests for CivBridge: launch the built binary and drive it over the protocol in docs/protocol.md.

Build first with scripts/build-bridge.sh. CIVBRIDGE_CMD overrides the binary (default build/bridge/CivBridge).
"""

from __future__ import annotations

import functools
import gzip
import json
import os
import queue
import re
import shlex
import subprocess
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CMD = shlex.split(os.environ.get("CIVBRIDGE_CMD", str(ROOT / "build" / "bridge" / "CivBridge")))

pytestmark = pytest.mark.skipif(not Path(CMD[0]).exists(), reason="CivBridge is not built; run scripts/build-bridge.sh")

SEED = 1
# With Raging barbarians the do-nothing player on seed 15 loses its settler mid-game (checked when written,
# before and after patches 0005-0009, which shift the random stream).
DEFEAT_SEED = 15
SCORE_KEYS = {"total", "cities", "pop", "tiles", "techs"}
DIRS = {"N", "NE", "E", "SE", "S", "SW", "W", "NW", "here"}


class Bridge:
    """One CivBridge process. Every stdout line must be a JSON object; anything else fails the test."""

    def __init__(self, *extra: str, stderr):
        self.proc = subprocess.Popen([*CMD, *extra], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                                     text=True, encoding="utf-8")
        self.lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.last_id = 0
        self.ready = self.read(60)

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def read(self, timeout: float) -> dict:
        line = self.lines.get(timeout=timeout)
        assert line is not None, f"CivBridge exited with {self.proc.wait()}"
        reply = json.loads(line)
        assert isinstance(reply, dict) and set(reply) <= {"id", "ok", "result", "error"}, line
        return reply

    def send(self, cmd: str, timeout: float = 120, **args) -> dict:
        self.last_id += 1
        self.proc.stdin.write(json.dumps({"id": self.last_id, "cmd": cmd, "args": args}) + "\n")
        self.proc.stdin.flush()
        reply = self.read(timeout)
        assert reply["id"] == self.last_id
        return reply

    def call(self, cmd: str, timeout: float = 120, **args) -> dict:
        reply = self.send(cmd, timeout, **args)
        assert reply["ok"], reply["error"]
        return reply["result"]

    def error(self, cmd: str, **args) -> dict:
        reply = self.send(cmd, **args)
        assert not reply["ok"], reply["result"]
        assert reply["error"]["message"].endswith((".", "!")), reply["error"]
        return reply["error"]

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.stdin.close()
            self.proc.wait(10)


@pytest.fixture
def launch(tmp_path):
    bridges: list[Bridge] = []

    def start(*extra: str) -> Bridge:
        bridge = Bridge(*extra, stderr=open(tmp_path / f"stderr-{len(bridges)}.log", "w"))
        bridges.append(bridge)
        return bridge

    yield start
    for b in bridges:
        b.close()


@pytest.fixture
def game(launch) -> Bridge:
    bridge = launch()
    bridge.call("new_game", seed=SEED)
    return bridge


def unit(state: dict, uid: str) -> dict:
    return next(u for u in state["units"] if u["id"] == uid)


def found_capital(b: Bridge) -> dict:
    res = b.call("unit_order", unit="u1", order="found_city")
    assert res["city"]["id"] == "c1" and res["unit"] is None
    return res["city"]


def test_ready_new_game_and_already_started(launch):
    b = launch()
    assert b.ready["id"] == 0 and b.ready["ok"] and b.ready["result"]["ready"] is True
    assert set(b.ready["result"]) == {"ready", "version", "autosave"}
    assert b.error("state")["code"] == "no_game"
    assert b.error("fly")["code"] == "unknown_command"
    game = b.call("new_game", seed=SEED)
    assert game["turn"] == 0 and game["turn_limit"] == 60 and game["seed"] == SEED and game["civ"] == "Rome"
    assert len(game["opponents"]) == 3 and "Rome" not in game["opponents"]
    assert game["map"] == {"width": 60, "height": 60, "wrap_x": True}
    assert b.error("new_game", seed=2)["code"] == "already_started"


def test_new_game_validates_scenario(launch):
    b = launch()
    err = b.error("new_game", seed=1, civ="Atlantis")
    assert err["code"] == "bad_args" and "Greece" in err["alternatives"]
    sizes = ["Tiny", "Small", "Standard", "Large", "Huge"]
    assert b.error("new_game", seed=1, size="Enormous")["alternatives"] == sizes
    assert b.error("new_game", seed=1, opponents=12)["code"] == "bad_args"
    assert b.error("new_game")["code"] == "bad_args"
    game = b.call("new_game", seed=4, civ="greece", opponents=1, size="Small", difficulty="Chieftain",
                  barbarians="None", landform="Continents", ocean=60, turn_limit=10)
    assert game["civ"] == "Greece" and len(game["opponents"]) == 1 and game["turn_limit"] == 10
    assert game["map"]["width"] == 80


def test_bad_request_lines(launch):
    b = launch()
    b.proc.stdin.write("not json\n")
    b.proc.stdin.flush()
    reply = b.read(30)
    assert reply["ok"] is False and reply["id"] is None and reply["error"]["code"] == "bad_request"


def test_initial_state(game):
    s = game.call("state")
    assert (s["turn"], s["turn_limit"], s["game_over"], s["defeated"]) == (0, 60, False, False)
    assert s["civ"] == "Rome" and s["government"] == "Despotism" and s["anarchy_until"] is None
    assert set(s["rates"]) == {"tax", "science", "luxury"} and sum(s["rates"].values()) == 10
    assert s["research"] == {
        "current": None, "turns_left": None, "beakers": 0, "cost": None, "queue": [], "source": None}
    techs = len(s["known_techs"])
    assert s["score"] == {"total": 4 * techs, "cities": 0, "pop": 0, "tiles": 0, "techs": techs}
    assert s["cities"] == [] and s["last_events"] == []
    assert [(u["id"], u["type"]) for u in s["units"]] == [("u1", "Settler"), ("u2", "Worker")]
    settler = unit(s, "u1")
    assert settler["status"] == "idle" and settler["needs_orders"] and settler["target"] is None
    assert (settler["x"] + settler["y"]) % 2 == 0
    assert settler["can_found_city"] == {"ok": True}
    assert {"settle", "found_city", "goto", "hold", "disband"} <= set(settler["orders"])
    assert "can_found_city" not in unit(s, "u2") and "auto_work" in unit(s, "u2")["orders"]
    assert [(b["kind"], b["id"]) for b in s["blockers"]] == [("idle_unit", "u1"), ("idle_unit", "u2")]
    assert {r["civ"] for r in s["rivals"]} == set(game.call("score")["players"][i]["civ"] for i in (1, 2, 3))
    assert all(r == {"civ": r["civ"], "agent": False, "met": False, "at_war": False, "peace_price": None,
                     "peace_offered": None, "cities_seen": 0}
               for r in s["rivals"])


def test_map(game):
    u = unit(game.call("state"), "u1")
    m = game.call("map", x=u["x"], y=u["y"])
    assert m["center"] == {"x": u["x"], "y": u["y"]} and m["radius"] == 3 and m["tiles"]
    keys = {"x", "y", "dist", "dir", "visible", "terrain", "overlay", "resource", "river", "improvements", "owner",
            "city", "units", "yield", "city_site"}
    for t in m["tiles"]:
        assert set(t) == keys and (t["x"] + t["y"]) % 2 == 0 and t["dist"] <= 3 and t["dir"] in DIRS
        assert set(t["yield"]) == {"food", "shields", "commerce"}
    here = next(t for t in m["tiles"] if t["dist"] == 0)
    assert here["dir"] == "here" and here["visible"]
    assert [(x["id"], x["type"], x["count"]) for x in here["units"]] == [("u1", "Settler", 1), ("u2", "Worker", 1)]
    north = next(t for t in m["tiles"] if (t["x"], t["y"]) == (u["x"], u["y"] - 2))
    assert north["dir"] == "N" and north["dist"] == 1
    assert game.call("map", x=u["x"], y=u["y"], radius=50)["radius"] == 8
    err = game.error("map", x=u["x"] + 1, y=u["y"])
    assert err["code"] == "bad_target" and err["alternatives"]


def test_city_sites(game):
    u = unit(game.call("state"), "u1")
    res = game.call("city_sites", top=4)
    assert res["origin"] == {"x": u["x"], "y": u["y"]} and res["note"]
    sites = res["sites"]
    assert len(sites) == 4 and [s["score"] for s in sites] == sorted((s["score"] for s in sites), reverse=True)
    for s in sites:
        assert list(s) == ["x", "y", "score", "dist", "dir", "turns", "terrain", "river", "coastal", "yield"]
        assert s["turns"] is not None and s["dir"] in DIRS
    assert game.call("city_sites", unit="u1", top=4) == res
    assert game.error("city_sites", unit="u9")["code"] == "unknown_unit"


def test_settle_walks_and_founds_on_arrival(game):
    site = next(s for s in game.call("city_sites", top=30)["sites"] if s["dist"] >= 2)
    res = game.call("unit_order", unit="u1", order="settle", x=site["x"], y=site["y"])
    assert res["path"]["length"] >= 2 and res["path"]["turns"] >= 1 and res["city"] is None
    assert res["unit"]["status"] == "settle" and not res["unit"]["needs_orders"]
    assert res["unit"]["target"]["x"] == site["x"]
    events = []
    for _ in range(10):
        r = game.call("end_turn", skip_idle=True)
        events += r["events"]
        if any(e["kind"] == "city_founded" for e in r["events"]):
            break
    founded = next(e for e in events if e["kind"] == "city_founded")
    assert (founded["x"], founded["y"]) == (site["x"], site["y"]) and founded["turn"] < r["turn"]
    s = game.call("state")
    assert [(c["id"], c["x"], c["y"], c["capital"]) for c in s["cities"]] == [("c1", site["x"], site["y"], True)]
    assert s["score"]["cities"] == 1 and s["score"]["tiles"] > 0
    assert s["last_events"] == r["events"]


def test_cannot_found_explains_and_suggests(game):
    u = unit(game.call("state"), "u1")
    bad = next(t for t in game.call("map", x=u["x"], y=u["y"], radius=8)["tiles"] if not t["city_site"]["ok"])
    err = game.error("unit_order", unit="u1", order="settle", x=bad["x"], y=bad["y"])
    assert err["code"] == "cannot_found" and bad["city_site"]["reason"] in err["message"]
    assert {"x", "y", "score", "dist", "dir"} <= set(err["alternatives"][0])
    best = err["alternatives"][0]
    assert err["suggest"] == f'unit_order(unit="u1", order="settle", x={best["x"]}, y={best["y"]})'


def test_found_city_production_and_city(game):
    city = found_capital(game)
    assert city["size"] == 1 and city["producing"] and city["buildings"] == ["Palace"]
    s = game.call("state")
    assert [b["kind"] for b in s["blockers"]] == ["no_research", "choose_production", "idle_unit"]
    info = game.call("city", city="c1")
    options = {o["name"]: o for o in info["options"]}
    assert options["Warrior"]["kind"] == "unit" and options["Wealth"]["kind"] == "wealth"
    assert info["tiles_worked"] and set(info["tiles_worked"][0]) == {"x", "y", "terrain", "yield"}
    assert game.call("city", city="rome")["id"] == "c1"
    err = game.error("set_production", city="c1", item="Granary")
    assert err["code"] == "unknown_item" and "Pottery" in err["message"]
    assert err["suggest"] == 'research(tech="Pottery")'
    assert set(err["alternatives"]) == set(options)
    res = game.call("set_production", city="c1", item="warrior")
    assert res["city"]["producing"] == "Warrior" and res["city"]["production_cost"] == options["Warrior"]["cost"]
    res = game.call("set_production", city="c1", item="Settler")
    assert "size 3" in res["message"] and res["city"]["turns_to_complete"] >= res["city"]["turns_to_grow"]
    assert game.error("set_production", city="c7", item="Warrior")["alternatives"] == ["c1"]
    assert game.error("city", city="c2")["code"] == "unknown_city"


def test_techs_and_research(game):
    found_capital(game)
    techs = game.call("techs")
    assert techs["current"] is None and techs["known"] == game.call("state")["known_techs"]
    pottery = next(t for t in techs["available"] if t["name"] == "Pottery")
    assert pottery["unlocks"] == ["Granary"] and pottery["era"] == "Ancient Times" and pottery["cost"] > 0
    err = game.error("set_research", tech="Alchemy Of Doom")
    assert err["code"] == "unknown_tech" and set(err["alternatives"]) == {t["name"] for t in techs["available"]}
    assert game.error("set_research", tech=techs["known"][0])["code"] == "already_known"
    res = game.call("set_research", tech="monarchy")
    assert res["queue"][-1] == "Monarchy" and res["current"] == res["queue"][0] != "Monarchy"
    assert "Ceremonial Burial" in res["queue"]
    research = game.call("state")["research"]
    assert research["current"] == res["current"] and research["queue"] == res["queue"] and research["cost"] > 0
    assert game.call("set_research", tech="Pottery")["queue"] == ["Pottery"]


def test_end_turn_blocked_then_skip_idle(game):
    r = game.call("end_turn")
    assert r == {"blocked": True, "blockers": game.call("state")["blockers"]}
    assert game.call("state")["turn"] == 0
    r = game.call("end_turn", skip_idle=True, max_turns=5)
    assert (r["blocked"], r["turns_advanced"], r["turn"], r["game_over"], r["defeated"]) == (False, 1, 1, False, False)
    assert isinstance(r["events"], list) and isinstance(r["auto"], list)
    s = game.call("state")
    assert s["turn"] == 1 and unit(s, "u1")["moves_left"] == 1 and unit(s, "u1")["needs_orders"]


def test_end_turn_auto_research_and_until_attention(game):
    found_capital(game)
    game.call("unit_order", unit="u2", order="fortify")
    assert game.call("end_turn")["blocked"]  # no research chosen yet
    r = game.call("end_turn", skip_idle=True)
    assert [a["kind"] for a in r["auto"]][:1] == ["research_picked"]
    assert game.call("state")["research"]["current"]
    r = game.call("end_turn", until_attention=True, max_turns=20)
    assert 1 <= r["turns_advanced"] <= 20
    if r["turns_advanced"] < 20:
        assert game.call("state")["blockers"]


def test_goto_explore_and_worker_jobs(game):
    s = game.call("state")
    u = unit(s, "u2")
    dest = next(t for t in game.call("map", x=u["x"], y=u["y"], radius=2)["tiles"]
                if t["dist"] == 2 and t["city_site"]["ok"])
    res = game.call("unit_order", unit="u2", order="goto", x=dest["x"], y=dest["y"])
    assert res["path"]["length"] >= 2 and res["unit"]["status"] == "goto" and res["unit"]["moves_left"] == 0
    assert res["unit"]["target"]["x"] == dest["x"] and res["unit"]["target"]["dist"] == 1
    for _ in range(4):
        game.call("unit_order", unit="u1", order="hold")
        game.call("end_turn", skip_idle=True)
        w = unit(game.call("state"), "u2")
        if w["status"] != "goto":
            break
    assert (w["x"], w["y"]) == (dest["x"], dest["y"]) and w["status"] in ("idle", "done") and w["target"] is None
    if w["moves_left"] == 0:
        game.call("end_turn", skip_idle=True)
    res = game.call("unit_order", unit="u2", order="build_road")
    assert res["unit"]["status"] == "working:build_road" and "Road" in res["message"]
    err = game.error("unit_order", unit="u2", order="build_road")
    assert err["code"] in ("no_moves", "invalid_order")
    done = []
    for _ in range(6):
        done += [e for e in game.call("end_turn", skip_idle=True)["events"] if e["kind"] == "job_done"]
        if done:
            break
    assert done and "Road" in done[0]["text"]
    res = game.call("unit_order", unit="u2", order="explore")
    assert res["unit"]["status"] == "exploring"
    assert game.call("unit_order", unit="u2", order="wake")["unit"]["status"] in ("idle", "done")


def test_unit_order_errors(game):
    s = game.call("state")
    u = unit(s, "u1")
    err = game.error("unit_order", unit="u9", order="hold")
    assert err["code"] == "unknown_unit" and err["alternatives"] == ["u1", "u2"]
    err = game.error("unit_order", unit="u2", order="fly")
    assert err["code"] == "invalid_order" and err["alternatives"] == unit(s, "u2")["orders"]
    assert err["suggest"] == 'unit_order(unit="u2", order="auto_work")'
    err = game.error("unit_order", unit="u2", order="found_city")
    assert err["code"] == "invalid_order" and "cannot found" in err["message"]
    assert game.error("unit_order", unit="u1", order="goto", x=u["x"] + 1, y=u["y"])["code"] == "bad_target"
    assert game.error("unit_order", unit="u1", order="goto", x=u["x"], y=200)["code"] == "bad_target"
    assert game.error("unit_order", unit="u1", order="goto")["code"] == "bad_target"
    unexplored = game.error("unit_order", unit="u1", order="goto", x=(u["x"] + 30) % 60, y=u["y"])
    assert unexplored["code"] == "bad_target" and "explored" in unexplored["message"]
    game.call("unit_order", unit="u1", order="hold")
    err = game.error("unit_order", unit="u1", order="found_city")
    assert err["code"] == "no_moves"
    assert err["suggest"] == f'unit_order(unit="u1", order="settle", x={u["x"]}, y={u["y"]})'
    res = game.call("unit_order", unit="u1", order="settle", x=u["x"], y=u["y"])
    assert res["unit"]["status"] == "settle" and res["city"] is None
    r = game.call("end_turn", skip_idle=True)
    assert [e["kind"] for e in r["events"]].count("city_founded") == 1


def test_game_over_at_turn_limit(launch):
    b = launch()
    b.call("new_game", seed=SEED, turn_limit=2)
    assert b.call("end_turn", skip_idle=True)["game_over"] is False
    r = b.call("end_turn", skip_idle=True, until_attention=True, max_turns=5)
    assert r["turn"] == 2 and r["game_over"] is True
    assert b.call("state")["game_over"] and b.call("state")["blockers"] == []
    assert b.error("end_turn", skip_idle=True)["code"] == "game_over"
    assert b.error("unit_order", unit="u1", order="hold")["code"] == "game_over"


def test_disbanding_everything_is_defeat(game):
    game.call("unit_order", unit="u2", order="disband")
    res = game.call("unit_order", unit="u1", order="disband")
    assert res["unit"] is None and "defeated" in res["message"]
    s = game.call("state")
    assert s["defeated"] and s["game_over"] and s["units"] == []
    assert game.error("end_turn", skip_idle=True)["code"] == "game_over"


@pytest.mark.parametrize("policy", ["null", "found_capital", "engine_ai"])
def test_autoplay_policies(launch, policy):
    b = launch()
    b.call("new_game", seed=SEED, turn_limit=40)
    res = b.call("autoplay", turns=40, policy=policy, record=True)
    assert (res["turn"], res["game_over"]) == (40, True)
    trajectory = res["trajectory"]
    assert [p["turn"] for p in trajectory] == list(range(41))
    assert trajectory[-1]["score"] == res["score"] == b.call("score")["human"] and set(res["score"]) == SCORE_KEYS
    s = b.call("state")
    if policy == "null":
        assert res["score"]["cities"] == 0 and [u["status"] for u in s["units"]] == ["idle", "idle"]
    elif policy == "found_capital":
        assert res["score"]["cities"] == 1 and any(u["status"] == "auto_work" for u in s["units"])
    else:
        assert res["score"]["cities"] >= 2
    assert "trajectory" not in b.call("autoplay", turns=1)
    policies = ["null", "found_capital", "settler_bot", "engine_ai"]
    assert b.error("autoplay", turns=1, policy="random")["alternatives"] == policies


def test_engine_ai_beats_found_capital_beats_null(launch):
    totals = {}
    for policy in ("null", "found_capital", "engine_ai"):
        b = launch()
        b.call("new_game", seed=SEED)
        totals[policy] = b.call("autoplay", turns=60, policy=policy)["score"]["total"]
    assert totals["engine_ai"] > totals["found_capital"] > totals["null"]


def test_score(game):
    found_capital(game)
    sc = game.call("score")
    assert sc["turn"] == 0 and sc["human"] == game.call("state")["score"]
    assert [p["is_human"] for p in sc["players"]] == [True, False, False, False]
    assert all(set(p["score"]) == SCORE_KEYS and not p["defeated"] for p in sc["players"])
    s = sc["human"]
    assert s["total"] == 10 * s["cities"] + 3 * s["pop"] + s["tiles"] + 4 * s["techs"]


def _scripted_game(b: Bridge, observe: bool) -> list:
    b.call("new_game", seed=3)
    b.call("unit_order", unit="u1", order="found_city")
    b.call("set_research", tech="Bronze Working")
    b.call("unit_order", unit="u2", order="auto_work")
    for _ in range(25):
        if observe:
            s = b.call("state")
            b.call("map", x=s["cities"][0]["x"], y=s["cities"][0]["y"], radius=8)
            b.call("city_sites")
            b.call("techs")
            b.call("city", city="c1")
            b.call("score")
            b.call("known_map")
        b.call("end_turn", skip_idle=True)
    b.call("autoplay", turns=15, policy="engine_ai")
    return [b.call("state"), b.call("score"), b.call("map", x=30, y=30, radius=8)]


def test_same_seed_and_commands_give_the_same_game(launch):
    first = _scripted_game(launch(), observe=False)
    assert _scripted_game(launch(), observe=False) == first
    # Observations neither draw from the engine RNG nor shift ids.
    assert _scripted_game(launch(), observe=True) == first


def test_null_autoplay_survives_defeat(launch):
    b = launch()
    b.call("new_game", seed=DEFEAT_SEED, barbarians="Raging")
    res = b.call("autoplay", turns=60, policy="null", record=True, timeout=120)
    assert res["turn"] == 60 and res["game_over"]
    assert res["defeated"], "seed no longer loses its settler; pick another DEFEAT_SEED"
    s = b.call("state")
    assert s["defeated"] and s["units"] == [] and s["blockers"] == []
    assert len(res["trajectory"]) == 61
    assert b.error("end_turn", skip_idle=True)["code"] == "game_over"


def until(b: Bridge, done, turns: int, chunk: int = 20) -> dict:
    """Autoplay the engine AI in chunks until done(state) holds; returns that state."""
    for _ in range(0, turns, chunk):
        state = b.call("state")
        if done(state):
            return state
        b.call("autoplay", turns=chunk, policy="engine_ai")
    state = b.call("state")
    assert done(state), f"not reached by T{state['turn']}"
    return state


def test_a_refused_building_says_why(launch):
    b = launch()
    b.call("new_game", seed=SEED, turn_limit=400)
    state = until(b, lambda s: len(s["cities"]) >= 2, 120)
    capital, other = state["cities"][0], state["cities"][1]
    assert "Rome already has it" in b.error("set_production", city=capital["id"], item="Palace")["message"]
    assert "the Palace cannot be moved" in b.error("set_production", city=other["id"], item="Palace")["message"]


def test_revolution_ends_in_the_chosen_government(launch):
    b = launch()
    b.call("new_game", seed=SEED, turn_limit=400)
    state = until(b, lambda s: s["governments"], 300)
    target = state["governments"][-1]["name"]
    assert state["tile_penalty"] and set(state["governments"][0]) == {
        "name", "corruption", "hurry", "tile_penalty", "trade_bonus", "unit_cost", "free_units_per_city"}
    assert b.error("revolution", government="Fascism")["code"] == "unknown_government"
    res = b.call("revolution", government=target)
    assert res["government"]["revolution_target"] == target and res["government"]["anarchy_until"] > state["turn"]
    assert set(res["government"]["available"][0]) == {"name", "corruption", "hurry", "tile_penalty", "trade_bonus",
                                                      "unit_cost", "free_units_per_city"}
    autos = []
    for _ in range(10):
        if b.call("state")["government"] == target:
            break
        autos += b.call("end_turn", skip_idle=True)["auto"]
    after = b.call("state")
    assert after["government"] == target and after["revolution_target"] is None
    assert any(x["kind"] == "government_picked" and target in x["text"] and "rates are back" in x["text"]
               for x in autos)
    assert after["rates"]["science"] == state["rates"]["science"]
    assert after["rates"]["luxury"] == state["rates"]["luxury"]


def test_war_then_peace_at_the_asked_price(launch):
    b = launch()
    b.call("new_game", seed=SEED, turn_limit=400)
    state = until(b, lambda s: any(r["met"] for r in s["rivals"]), 120)
    civ = next(r["civ"] for r in state["rivals"] if r["met"])
    assert b.error("propose_peace", civ=civ)["code"] == "not_at_war"
    war = b.call("declare_war", civ=civ)
    assert war["civ"]["at_war"] and war["civ"]["refuses_talks_until"] > state["turn"]
    assert b.error("declare_war", civ=civ)["code"] == "already_at_war"
    assert b.error("propose_peace", civ=civ)["code"] == "no_talks"
    assert b.error("declare_war", civ="Atlantis")["code"] == "unknown_civ"
    # A stronger civ asks gold at first; its price fades to 0 over the 60 turns after the 10-turn minimum.
    for _ in range(80):
        b.call("end_turn", skip_idle=True)
        them = next(c for c in b.call("diplomacy")["civs"] if c["civ"] == civ)
        if them["peace_price"] is not None and them["peace_price"] <= b.call("state")["gold"]:
            peace = b.call("propose_peace", civ=civ, gold=them["peace_price"])
            assert not peace["civ"]["at_war"] and peace["message"].startswith(f"Peace with {civ}")
            break
    else:
        raise AssertionError(f"{civ} never offered terms the human could pay")
    assert not next(r for r in b.call("state")["rivals"] if r["civ"] == civ)["at_war"]


def test_attack_an_adjacent_enemy_with_its_win_chance(launch):
    b = launch()
    b.call("new_game", seed=SEED, turn_limit=400)
    state = until(b, lambda s: any(r["met"] for r in s["rivals"]), 140)
    civ = next(r["civ"] for r in state["rivals"] if r["met"])
    b.call("declare_war", civ=civ)
    world = b.call("world")
    enemy = next(p["index"] for p in world["players"] if p["civ"] == civ)
    cities = [c for c in world["cities"] if c["owner"] == enemy]
    soldiers = [u for u in state["units"] if u["type"] not in ("Settler", "Worker")]
    unit, city = min(((u, c) for u in soldiers for c in cities),
                     key=lambda uc: abs(uc[0]["x"] - uc[1]["x"]) + abs(uc[0]["y"] - uc[1]["y"]))
    near = [(city["x"] + dx, city["y"] + dy) for dx, dy in ((1, 1), (-1, 1), (1, -1), (-1, -1), (2, 0), (-2, 0))]
    for _ in range(30):
        me = next((u for u in b.call("state")["units"] if u["id"] == unit["id"]), None)
        assert me is not None, "the unit died on the way"
        if me.get("attack_targets"):
            break
        for x, y in near:
            if b.send("unit_order", unit=unit["id"], order="goto", x=x, y=y)["ok"]:
                break
        b.call("end_turn", skip_idle=True)
    else:
        raise AssertionError("never got next to an enemy")
    target = me["attack_targets"][0]
    assert {"x", "y", "dir", "owner", "defender", "city", "win_chance"} <= set(target)
    assert 0 <= target["win_chance"] <= 1
    assert "attack" in me["orders"]
    far = b.send("unit_order", unit=unit["id"], order="attack", x=me["x"] + 4, y=me["y"])
    assert far["error"]["code"] == "bad_target" and far["error"]["alternatives"]
    res = b.call("unit_order", unit=unit["id"], order="attack", x=target["x"], y=target["y"])
    assert f"{unit['id']} {unit['type']} attacked" in res["message"] or "entered" in res["message"]
    assert (res["unit"] is None) == ("was destroyed" in res["message"] and "lost" in res["message"])


def test_saves_keeps_every_turn_as_a_loadable_save(launch, tmp_path):
    b = launch("--saves", str(tmp_path / "saves"))
    b.call("new_game", seed=SEED, turn_limit=10)
    b.call("autoplay", turns=3, policy="settler_bot")
    kept = sorted(p.name for p in (tmp_path / "saves").iterdir())
    assert kept == ["turn-0000.json.gz", "turn-0001.json.gz", "turn-0002.json.gz", "turn-0003.json.gz"]
    plain = tmp_path / "turn-2.json"
    plain.write_bytes(gzip.decompress((tmp_path / "saves" / "turn-0002.json.gz").read_bytes()))
    assert launch().call("load", path=str(plain))["turn"] == 2


def snapshots(path: Path) -> list[dict]:
    return [json.loads(gzip.decompress(f.read_bytes())) for f in sorted(path.glob("turn-*.json.gz"))]


def test_world_snapshot_schema_2(launch, tmp_path):
    b = launch("--record", str(tmp_path / "rec"))
    b.call("new_game", seed=SEED)
    found_capital(b)
    b.call("set_production", city="c1", item="Warrior")
    b.call("unit_order", unit="u2", order="explore")
    for _ in range(4):
        b.call("end_turn", skip_idle=True)
    world, state = b.call("world"), b.call("state")
    snaps = snapshots(tmp_path / "rec")
    assert [s["turn"] for s in snaps] == [0, 1, 2, 3, 4] and snaps[-1] == world
    rome = next(p for p in world["players"] if p["civ"] == "Rome")
    assert world["schema"] == 2 and world["seats"] == [{"index": rome["index"], "civ": "Rome", "label": None}]

    # One seat: `known` is bit 0, the tiles the human has explored.
    assert {row[6] for row in world["tiles"]} == {0, 1}
    known = sum(row[6] for row in world["tiles"])
    assert round(100 * known / len(world["tiles"]), 1) == state["explored_pct"]

    assert (rome["gold"], rome["government"], rome["research"]) == (
        state["gold"], state["government"], state["research"]["current"])
    assert rome["research"] and rome["at_war"] == [] and rome["contacts"] == []
    for p in world["players"]:
        assert set(p) >= {"gold", "government", "research", "at_war", "contacts"}
        assert isinstance(p["gold"], int) and isinstance(p["government"], str)
    assert next(p for p in world["players"] if p["civ"] == "Barbarians")["at_war"] == []

    # Cities and units carry the engine's ids, unique in a snapshot and kept from turn to turn.
    [city] = [c for c in world["cities"] if c["owner"] == rome["index"]]
    assert city["production"] == state["cities"][0]["producing"] == "Warrior"
    assert all(c["id"] == city["id"] for s in snaps[1:] for c in s["cities"] if c["owner"] == rome["index"])
    for s in snaps:
        ids = [u["id"] for u in s["units"]] + [c["id"] for c in s["cities"]]
        assert all(isinstance(i, str) for i in ids) and len(set(ids)) == len(ids)
    [worker] = [u for u in snaps[0]["units"] if u["owner"] == rome["index"] and u["type"] == "Worker"]
    trail = [next(u for u in s["units"] if u["id"] == worker["id"]) for s in snaps]
    assert all((u["owner"], u["type"]) == (rome["index"], "Worker") for u in trail)
    assert len({(u["x"], u["y"]) for u in trail}) > 1, "the exploring worker never moved"
    assert (trail[-1]["x"], trail[-1]["y"]) == (unit(state, "u2")["x"], unit(state, "u2")["y"])


# Opposite river edges: NE=1/SW=4, SE=2/NW=8, N=16/S=64, E=32/W=128, with the (dx, dy) of the neighbour across each.
RIVER_EDGES = ((1, 4, 1, -1), (2, 8, 1, 1), (16, 64, 0, -2), (32, 128, 2, 0))


def check_rivers(world: dict) -> None:
    """The snapshot's river masks over the whole map: an edge is flagged on both banks, with the opposite bit."""
    width, wrap = world["map"]["width"], world["map"]["wrap_x"]
    rivers = {(r[0], r[1]): r[5] for r in world["tiles"]}
    assert all(isinstance(m, int) and 0 <= m < 256 for m in rivers.values())
    assert sum(1 for m in rivers.values() if m & 15) > 20, "the map should have rivers"
    edges = 0
    for (x, y), m in rivers.items():
        for bit, opposite, dx, dy in RIVER_EDGES:
            nx = (x + dx) % width if wrap else x + dx
            if (nx, y + dy) in rivers:
                assert bool(m & bit) == bool(rivers[nx, y + dy] & opposite), f"river edge at ({x},{y}) bit {bit}"
                edges += bool(m & bit)
    assert edges > 20


@functools.cache
def client_color_table() -> tuple[dict, list[str]]:
    """The OpenCiv3 client's civ colour indexes (ruleset.json) and its standalone hex colours (textures.lua)."""
    lua = ROOT / "vendor" / "OpenCiv3" / "C7" / "Lua"
    ruleset = json.loads((lua / "civ3" / "ruleset.json").read_text(encoding="utf-8"))
    civs = {("Barbarians" if c.get("isBarbarian") else c["name"]): (c["primaryColorIndex"], c["secondaryColorIndex"])
            for c in ruleset["civilizations"]}
    textures = (lua / "standalone" / "textures.lua").read_text(encoding="utf-8")
    table = textures.split("local civ_colors = {")[1].split("}")[0]
    return civs, ["#" + h.lower() for h in re.findall(r'"([0-9A-Fa-f]{6})"', table)]


def client_colors(world: dict) -> list[str]:
    """Each player's colour as the client picks it (C7/Textures/PlayerTextureUtil.cs): the primary colour, the secondary
    when the primary is taken, then in order a player whose colour is shared drops it and picks again."""
    civs, hexes = client_color_table()
    pairs = [civs[p["civ"]] for p in world["players"]]
    used: dict[int, int] = {}

    def load(i: int) -> None:
        if i not in used:
            used[i] = pairs[i][1] if pairs[i][0] in used.values() else pairs[i][0]

    for i in range(len(pairs)):
        load(i)
    for i in range(len(pairs)):
        if list(used.values()).count(used[i]) > 1:
            del used[i]
            load(i)
    return [hexes[used[i]] for i in range(len(pairs))]


def check_known_map(km: dict, world: dict, state: dict, k: int = 0) -> None:
    """known_map for seats[k] against the world snapshot (which sees through the fog) and the seat's own state."""
    me = world["seats"][k]["index"]
    assert list(km) == ["turn", "width", "height", "wrap_x", "players", "tiles", "cities", "units"]
    assert (km["turn"], km["width"], km["height"], km["wrap_x"]) == (
        world["turn"], world["map"]["width"], world["map"]["height"], world["map"]["wrap_x"])
    assert km["players"] == [{"index": p["index"], "civ": p["civ"], "barbarian": p["civ"] == "Barbarians",
                              "me": p["index"] == me, "color": client_colors(world)[p["index"]]}
                             for p in world["players"]]
    check_rivers(world)

    # Every tile the seat knows and nothing else; terrain, overlay, river and owner as in the snapshot.
    rows = {(r[0], r[1]): r for r in world["tiles"]}
    known = {xy for xy, r in rows.items() if r[6] >> k & 1}
    assert {(t[0], t[1]) for t in km["tiles"]} == known and len(km["tiles"]) == len(known)
    for x, y, terrain, overlay, river, owner, visible, resource, improvements, bonus in km["tiles"]:
        w = rows[x, y]
        assert (terrain, overlay, owner, river) == (w[2], w[3], w[4], w[5])
        assert visible in (0, 1) and (resource is None or isinstance(resource, str))
        assert all(isinstance(i, str) and i != "barbarianCamp" for i in improvements)
        assert bonus in (0, 1) and not (bonus and (terrain, overlay) != ("grassland", None))
    visible = {(t[0], t[1]) for t in km["tiles"] if t[6]}
    assert visible and visible < known, "after exploring, some known tiles should be out of sight"

    # Cities on known tiles; the seat's own carry its ids and plans, as in state.
    own_cities = {c["id"]: c for c in state["cities"]}
    expected = [{k_: c[k_] for k_ in ("x", "y", "name", "owner", "size", "capital")}
                for c in world["cities"] if (c["x"], c["y"]) in known]
    base = [{k_: c[k_] for k_ in ("x", "y", "name", "owner", "size", "capital")} for c in km["cities"]]
    assert sorted(base, key=lambda c: (c["x"], c["y"])) == sorted(expected, key=lambda c: (c["x"], c["y"]))
    eras = {}
    for c in km["cities"]:
        # The city art's inputs, for every city: the owner's era (one per owner), walls and disorder.
        assert c["era"] in (0, 1, 2, 3) and eras.setdefault(c["owner"], c["era"]) == c["era"]
        assert isinstance(c["walls"], bool) and isinstance(c["disorder"], bool)
        if c["owner"] != me:
            assert set(c) == {"x", "y", "name", "owner", "size", "capital", "era", "walls", "disorder"}
            continue
        mine = own_cities.pop(c["id"])
        assert (c["name"], c["x"], c["y"], c["size"]) == (mine["name"], mine["x"], mine["y"], mine["size"])
        assert (c["producing"], c["turns_to_complete"], c["turns_to_grow"]) == (
            mine["producing"], mine["turns_to_complete"], mine["turns_to_grow"])
        assert (c["disorder"], c["walls"], c["starving"]) == (
            mine["disorder"], "Walls" in mine["buildings"], mine["food_per_turn"] < 0)
    assert own_cities == {}

    # Units on visible tiles only: the seat's own one by one with ids, everyone else's counted by owner and type.
    assert all((u["x"], u["y"]) in visible for u in km["units"])
    own = [u for u in km["units"] if u["owner"] == me]
    assert all(u["count"] == 1 for u in own)
    assert sorted((u["id"], u["type"], u["x"], u["y"]) for u in own) == sorted(
        (u["id"], u["type"], u["x"], u["y"]) for u in state["units"])
    # The hit point bar beside every unit: hp as in state for the seat's own, the fortified frame, and only for
    # units that can fight (one answer per type).
    mine = {u["id"]: u for u in state["units"]}
    for u in own:
        s = mine[u["id"]]
        assert (u["hp"], u["hp_max"]) == (s["hp"], s["hp_max"])
        assert u["fortified"] if s["status"] == "fortified" else u["fortified"] in (True, False)
    combat = {}
    for u in km["units"]:
        assert 0 < u["hp"] <= u["hp_max"] and isinstance(u["fortified"], bool)
        assert combat.setdefault(u["type"], u["combat"]) == u["combat"]
    assert combat.get("Settler", False) is False and combat.get("Worker", False) is False
    assert combat.get("Warrior", True) is True
    foreign = [u for u in km["units"] if u["owner"] != me]
    assert all(set(u) == {"x", "y", "owner", "type", "count", "hp", "hp_max", "fortified", "combat"} for u in foreign)
    groups = {}
    for u in world["units"]:
        if u["owner"] != me and (u["x"], u["y"]) in visible:
            key = (u["x"], u["y"], u["owner"], u["type"])
            groups[key] = groups.get(key, 0) + 1
    assert {(u["x"], u["y"], u["owner"], u["type"]): u["count"] for u in foreign} == groups
    assert len(foreign) == len(groups)


def test_known_map_draws_what_the_seat_knows(launch):
    b = launch()
    b.call("new_game", seed=SEED, turn_limit=100)
    start = b.call("known_map")
    assert start["turn"] == 0 and start["cities"] == [] and all(t[6] == 1 for t in start["tiles"])
    assert [(u["id"], u["type"], u["count"]) for u in start["units"]] == [("u1", "Settler", 1), ("u2", "Worker", 1)]
    found_capital(b)
    b.call("set_production", city="c1", item="Warrior")
    b.call("unit_order", unit="u2", order="explore")
    for _ in range(3):
        b.call("end_turn", skip_idle=True)
    km = b.call("known_map")
    check_known_map(km, b.call("world"), b.call("state"))
    [city] = km["cities"]
    assert city["id"] == "c1" and city["producing"] == "Warrior" and city["capital"]
    assert (city["era"], city["walls"], city["disorder"], city["starving"]) == (0, False, False, False)
    assert any(t[5] == city["owner"] for t in km["tiles"]), "the capital's borders are known"
    # Rome red, then the opponents; no two players share a colour in this game.
    colors = [p["color"] for p in km["players"]]
    assert colors[:2] == ["#f0f8ff", "#e6194b"] and len(set(colors)) == len(colors)

    # Rivers, bonus grassland and camps against what `map` reports from the engine around the capital: river tiles
    # border a river, plain grassland yields its bonus shield, and a camp is why a city cannot be founded.
    rows = {(t[0], t[1]): t for t in km["tiles"]}
    near = b.call("map", x=city["x"], y=city["y"], radius=8)["tiles"]
    assert all(bool(rows[t["x"], t["y"]][4]) == t["river"] for t in near)
    plain = [t for t in near
             if (t["terrain"], t["overlay"], t["resource"], t["city"]) == ("Grassland", None, None, None)]
    assert all(rows[t["x"], t["y"]][9] == t["yield"]["shields"] for t in plain)
    assert {rows[t["x"], t["y"]][9] for t in plain} == {0, 1}, "both kinds of grassland near the capital"

    # Later, with neighbours met: foreign cities and units show (theirs without ids), still only what is known.
    me = next(p["index"] for p in km["players"] if p["me"])
    for _ in range(8):
        b.call("autoplay", turns=10, policy="engine_ai")
        km = b.call("known_map")
        check_known_map(km, b.call("world"), b.call("state"))
        if any(c["owner"] != me for c in km["cities"]) and any(u["owner"] != me for u in km["units"]):
            break
    else:
        pytest.fail("no foreign city and unit in sight by T83")
    assert len(km["tiles"]) < km["width"] * km["height"] // 2
    assert b.call("known_map") == km

    # A unit the seat fortifies gets the frame.
    state = b.call("state")
    unit_id = next(u["id"] for u in state["units"] if "fortify" in u["orders"])
    b.call("unit_order", unit=unit_id, order="fortify")
    after = b.call("known_map")
    check_known_map(after, b.call("world"), b.call("state"))
    assert next(u for u in after["units"] if u.get("id") == unit_id)["fortified"] is True
    # Still in the Ancient era while an Ancient tech is left to learn.
    if any(t["era"] == "Ancient Times" for t in b.call("techs")["available"]):
        assert all(c["era"] == 0 for c in km["cities"] if c["owner"] == me)

    camps = [(t[0], t[1]) for t in km["tiles"] if "barbarian_camp" in t[8]]
    assert camps, "a barbarian camp should be known by now"
    for x, y in camps:
        [tile] = b.call("map", x=x, y=y, radius=0)["tiles"]
        assert "barbarian_camp" in tile["improvements"]
        assert tile["city_site"] == {"ok": False, "reason": "a barbarian camp is here"}


def test_watchdog_answers_timeout_and_exits(launch):
    b = launch("--timeout", "0.05")
    reply = b.send("new_game", seed=SEED)
    assert reply["ok"] is False and reply["error"]["code"] == "timeout" and reply["id"] == b.last_id
    assert b.proc.wait(10) != 0


SEATS = ("Rome", "Greece", "Egypt")


def seat_game(launch, *extra: str) -> Bridge:
    b = launch(*extra)
    b.call("new_game", seed=SEED, opponents=2, seats=["Greece", "Egypt"], labels={"Rome": "A", "Greece": "B"},
           turn_limit=80)
    return b


def end_round(b: Bridge) -> dict:
    """Every seat ends the turn with skip_idle, Egypt last; returns each seat's result."""
    for civ in SEATS[:-1]:
        assert b.call("end_turn", seat=civ, skip_idle=True)["waiting_for"]
    return b.call("end_turn", seat=SEATS[-1], skip_idle=True)["seats"]


def kinds(result: dict) -> list[tuple[str, str]]:
    return [(e["kind"], e["text"]) for e in result["events"]]


def test_seats_play_their_own_civs_and_end_turns_together(launch):
    b = launch()
    info = b.call("new_game", seed=SEED, opponents=2, seats=["greece", "Egypt"], labels={"Rome": "A", "Greece": "B"})
    assert info["opponents"] == ["Greece", "Egypt"]
    assert info["seats"] == [{"civ": "Rome", "label": "A"}, {"civ": "Greece", "label": "B"},
                             {"civ": "Egypt", "label": None}]
    for civ in SEATS:
        s = b.call("state", seat=civ)
        assert s["civ"] == civ and [u["id"] for u in s["units"]] == ["u1", "u2"]
        assert [(r["civ"], r["agent"]) for r in s["rivals"]] == [(c, True) for c in SEATS if c != civ]
    assert b.call("state")["civ"] == "Rome"
    assert b.error("state", seat="Babylon")["alternatives"] == list(SEATS)
    assert b.error("autoplay", turns=1)["code"] == "multi_seat"
    assert b.error("new_game", seed=SEED)["code"] == "already_started"

    b.call("unit_order", seat="Greece", unit="u1", order="found_city")
    assert b.call("end_turn", seat="Rome", skip_idle=True) == {
        "blocked": False, "turns_advanced": 0, "turn": 0, "waiting_for": ["Greece", "Egypt"]}
    assert b.call("end_turn", seat="Greece")["blocked"]
    assert b.call("end_turn", seat="Greece", skip_idle=True)["waiting_for"] == ["Egypt"]
    assert b.call("state", seat="Rome")["turn"] == 0
    res = b.call("end_turn", seat="Egypt", skip_idle=True)
    assert res["turn"] == 1 and list(res["seats"]) == list(SEATS)
    assert all(r["turns_advanced"] == 1 and r["turn"] == 1 and not r["game_over"] for r in res["seats"].values())
    players = b.call("score", seat="Greece")["players"]
    assert [(p["civ"], p["seat"], p["is_human"]) for p in players] == [
        ("Rome", "A", False), ("Greece", "B", True), ("Egypt", "Egypt", False)]
    assert [p["score"]["cities"] for p in players] == [0, 1, 0]


def test_seat_snapshots_know_tiles_per_seat(launch, tmp_path):
    b = seat_game(launch, "--record", str(tmp_path / "rec"))
    for civ in SEATS:
        b.call("unit_order", seat=civ, unit="u1", order="found_city")
        b.call("set_production", seat=civ, city="c1", item="Warrior")
        b.call("unit_order", seat=civ, unit="u2", order="explore")
    for _ in range(3):
        end_round(b)
    world = b.call("world")
    assert snapshots(tmp_path / "rec")[-1] == world
    index = {p["civ"]: p["index"] for p in world["players"]}
    assert world["schema"] == 2 and world["seats"] == [
        {"index": index["Rome"], "civ": "Rome", "label": "A"},
        {"index": index["Greece"], "civ": "Greece", "label": "B"},
        {"index": index["Egypt"], "civ": "Egypt", "label": None}]
    assert all(0 <= row[6] < 1 << len(SEATS) for row in world["tiles"])
    known = [{(row[0], row[1]) for row in world["tiles"] if row[6] >> k & 1} for k in range(len(SEATS))]
    assert all(known) and len({frozenset(k) for k in known}) == len(SEATS)

    # Bit k is what seats[k] knows: around its capital it matches that seat's own map.
    width, wrap = world["map"]["width"], world["map"]["wrap_x"]
    tiles = {(row[0], row[1]) for row in world["tiles"]}
    for k, civ in enumerate(SEATS):
        city = b.call("state", seat=civ)["cities"][0]
        assert city["producing"] == "Warrior"
        assert any(c["production"] == "Warrior" and c["owner"] == index[civ]
                   and (c["x"], c["y"]) == (city["x"], city["y"]) for c in world["cities"])
        seen = {(t["x"], t["y"]) for t in b.call("map", seat=civ, x=city["x"], y=city["y"], radius=4)["tiles"]}
        area = {((city["x"] + dx) % width if wrap else city["x"] + dx, city["y"] + dy)
                for dx in range(-8, 9) for dy in range(-8, 9) if (dx + dy) % 2 == 0 and abs(dx) + abs(dy) <= 8} & tiles
        assert seen and seen == area & known[k]


def test_known_map_per_seat(launch):
    b = seat_game(launch)
    for civ in SEATS:
        b.call("unit_order", seat=civ, unit="u1", order="found_city")
        b.call("set_production", seat=civ, city="c1", item="Warrior")
        b.call("unit_order", seat=civ, unit="u2", order="explore")
    for _ in range(3):
        end_round(b)
    world = b.call("world")
    maps = {civ: b.call("known_map", seat=civ) for civ in SEATS}
    assert b.call("known_map") == maps["Rome"]
    for k, civ in enumerate(SEATS):
        km = maps[civ]
        check_known_map(km, world, b.call("state", seat=civ), k)
        assert [p["civ"] for p in km["players"] if p["me"]] == [civ]
        me = next(p["index"] for p in km["players"] if p["me"])
        # Ids are this seat's own: only on its units and cities, and they are u1, c1, ... like every seat's.
        assert all(("id" in u) == (u["owner"] == me) for u in km["units"])
        assert all(("id" in c) == (c["owner"] == me) for c in km["cities"])
        assert [c["id"] for c in km["cities"] if "id" in c] == ["c1"]
    tiles = [frozenset((t[0], t[1]) for t in maps[civ]["tiles"]) for civ in SEATS]
    assert len(set(tiles)) == len(SEATS)
    # Every seat sees the same colours: the barbarians' white, then Rome, Greece and Egypt with their primaries.
    assert all(maps[civ]["players"] == [dict(p, me=p["civ"] == civ) for p in maps["Rome"]["players"]] for civ in SEATS)
    assert [p["color"] for p in maps["Rome"]["players"]] == ["#f0f8ff", "#e6194b", "#aaffc3", "#ffe119"]


def test_new_game_seats_must_fit_the_opponents(launch):
    b = launch()
    assert "opponent slots" in b.error("new_game", seed=SEED, opponents=1, seats=["Greece", "Egypt"])["message"]
    twice = b.error("new_game", seed=SEED, seats=["Rome"])
    assert twice["message"] == "Rome is given twice; each seat is a different civ."
    assert b.error("new_game", seed=SEED, seats=["Atlantis"])["code"] == "bad_args"
    assert b.error("new_game", seed=SEED, seats="Greece")["message"] == "'seats' must be a list of names."


def test_seats_meet_declare_war_and_make_peace(launch):
    b = seat_game(launch)
    for civ in SEATS:
        b.call("unit_order", seat=civ, unit="u1", order="found_city")
        b.call("set_production", seat=civ, city="c1", item="Warrior")
    contacts = {civ: [] for civ in SEATS}
    for _ in range(60):
        for civ in SEATS:
            for u in b.call("state", seat=civ)["units"]:
                if u["type"] == "Warrior" and u["needs_orders"]:
                    b.call("unit_order", seat=civ, unit=u["id"], order="explore")
        for civ, result in end_round(b).items():
            contacts[civ] += [text for kind, text in kinds(result) if kind == "contact"]
        if all(r["met"] for civ in SEATS for r in b.call("state", seat=civ)["rivals"]):
            break
    else:
        pytest.fail("the seats never all met")
    # Contacts made by another seat's moves reach every seat too, once each.
    events = end_round(b)
    for civ, result in events.items():
        contacts[civ] += [text for kind, text in kinds(result) if kind == "contact"]
    assert {civ: sorted(texts) for civ, texts in contacts.items()} == {
        civ: sorted(f"Met {other}." for other in SEATS if other != civ) for civ in SEATS}

    world = b.call("world")
    index = {p["civ"]: p["index"] for p in world["players"]}
    for p in world["players"]:
        if p["civ"] in SEATS:
            assert p["contacts"] == sorted(index[c] for c in SEATS if c != p["civ"]) and p["at_war"] == []

    assert b.call("declare_war", seat="Rome", civ="Greece")["message"] == "Rome declared war on Greece."
    at_war = {p["civ"]: p["at_war"] for p in b.call("world")["players"] if p["civ"] in SEATS}
    assert at_war == {"Rome": [index["Greece"]], "Greece": [index["Rome"]], "Egypt": []}
    offer = b.call("propose_peace", seat="Rome", civ="Greece")["message"]
    turn = b.call("state")["turn"]
    assert offer == f"Peace offered to Greece; it is signed if Greece proposes peace too before turn {turn + 2}."
    rome = next(c for c in b.call("diplomacy", seat="Greece")["civs"] if c["civ"] == "Rome")
    assert rome["at_war"] and rome["agent"] and rome["peace_price"] is None
    assert rome["peace_offered"] == {"gold": 0, "until_turn": turn + 1}
    events = end_round(b)
    war = ("war_declared", "Rome declared war on Greece.")
    assert war in kinds(events["Greece"]) and war in kinds(events["Egypt"]) and war not in kinds(events["Rome"])
    assert [k for k, _ in kinds(events["Greece"])].count("peace_offered") == 1

    assert b.call("propose_peace", seat="Greece", civ="Rome")["message"] == "Peace with Rome."
    events = end_round(b)
    assert ("peace_signed", "Peace with Greece.") in kinds(events["Rome"])
    assert ("peace_signed", "Rome and Greece made peace.") in kinds(events["Egypt"])
    assert not any(c["at_war"] for c in b.call("diplomacy", seat="Rome")["civs"])
    assert not any(p["at_war"] for p in b.call("world")["players"])


def test_a_seat_game_restores_every_seat(launch, tmp_path):
    b = seat_game(launch, "--autosave", str(tmp_path / "a"))
    for civ in SEATS[1:]:
        b.call("unit_order", seat=civ, unit="u1", order="found_city")
        b.call("unit_order", seat=civ, unit="u2", order="auto_work")
    end_round(b)
    end_round(b)
    restored = launch()
    assert restored.call("load", path=str(tmp_path / "a" / "autosave.json"))["seats"] == [
        {"civ": "Rome", "label": "A"}, {"civ": "Greece", "label": "B"}, {"civ": "Egypt", "label": None}]
    for civ in SEATS:
        assert restored.call("state", seat=civ) == b.call("state", seat=civ)
    worlds = [w.call("world") for w in (b, restored)]
    for key in ("seats", "players", "tiles", "cities"):
        assert worlds[0][key] == worlds[1][key]
    assert sorted(worlds[0]["units"], key=lambda u: u["id"]) == sorted(worlds[1]["units"], key=lambda u: u["id"])
    assert restored.call("end_turn", seat="Egypt", skip_idle=True)["waiting_for"] == ["Rome", "Greece"]


def test_the_last_seat_standing_wins_by_conquest(launch, tmp_path):
    b = launch("--autosave", str(tmp_path / "a"))
    b.call("new_game", seed=SEED, opponents=1, seats=["Greece"], labels={"Rome": "A", "Greece": "B"})
    b.call("unit_order", seat="Greece", unit="u2", order="disband")
    b.call("unit_order", seat="Greece", unit="u1", order="disband")
    assert b.call("state", seat="Greece")["defeated"]
    res = b.call("end_turn", seat="Rome", skip_idle=True)["seats"]["Rome"]
    victory = {"kind": "conquest", "civ": "Rome", "label": "A", "turn": 1}
    won = "Rome (A) won by conquest: it is the last civilization an agent still plays."
    assert res["game_over"] and res["events"][-1] == {"turn": 0, "kind": "victory", "text": won}
    assert b.call("state")["victory"] == victory and b.call("score")["victory"] == victory
    assert "Rome (A) won by conquest on turn 1" in b.error("end_turn", seat="Rome", skip_idle=True)["message"]
    restored = launch()
    restored.call("load", path=str(tmp_path / "a" / "autosave.json"))
    assert restored.call("state", seat="Greece")["victory"] == victory and restored.call("state")["game_over"]
