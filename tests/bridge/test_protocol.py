"""End-to-end tests for CivBridge: launch the built binary and drive it over the protocol in docs/protocol.md.

Build first with scripts/build-bridge.sh. CIVBRIDGE_CMD overrides the binary (default build/bridge/CivBridge).
"""

from __future__ import annotations

import json
import os
import queue
import shlex
import subprocess
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CMD = shlex.split(os.environ.get("CIVBRIDGE_CMD", str(ROOT / "build" / "bridge" / "CivBridge")))

pytestmark = pytest.mark.skipif(not Path(CMD[0]).exists(), reason="CivBridge is not built; run scripts/build-bridge.sh")

SEED = 1
# With Raging barbarians the do-nothing player on seed 8 loses its settler mid-game (checked when written).
DEFEAT_SEED = 8
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
    assert all(r == {"civ": r["civ"], "met": False, "at_war": False, "cities_seen": 0} for r in s["rivals"])


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


def test_watchdog_answers_timeout_and_exits(launch):
    b = launch("--timeout", "0.05")
    reply = b.send("new_game", seed=SEED)
    assert reply["ok"] is False and reply["error"]["code"] == "timeout" and reply["id"] == b.last_id
    assert b.proc.wait(10) != 0
