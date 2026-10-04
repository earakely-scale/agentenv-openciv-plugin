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
from itertools import pairwise
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CMD = shlex.split(os.environ.get("CIVBRIDGE_CMD", str(ROOT / "build" / "bridge" / "CivBridge")))

pytestmark = pytest.mark.skipif(not Path(CMD[0]).exists(), reason="CivBridge is not built; run scripts/build-bridge.sh")

SEED = 1
# Seeds on which, with Raging barbarians, the do-nothing player loses its settler within 60 turns; the test plays them
# in order until one does. Which seeds do moves whenever a patch shifts the random stream (15, then 21, no longer do on
# the build with patches 0016-0022): measured then, seed 7 loses it at T23 (T21 on main and on every feature branch but
# naval's), seed 49 at T53 on main, on each feature branch and on them all together.
DEFEAT_SEEDS = (7, 49)
# Seeds on which, with the engine AI in every seat and Raging barbarians, the seat takes a city well within the test's
# window; played in order until one does. Measured on the build with patches 0016-0022: seed 24 at T121 (the test is
# done at T185), seed 1 at T196; each also on main and on every feature branch alone, by T262. (Seed 14 no longer does:
# 23 of seeds 1-24 have the seat take a city by T390, seed 14 not, the seat losing four cities instead.)
CAPTURE_SEEDS = (24, 1)
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
    assert s["civ"] == "Rome" and s["government"] == "Despotism" and s["anarchy_until"] is None and s["era"] == 0
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
                     "peace_offered": None, "trade_offered": None, "cities_seen": 0}
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
    check_city_screen(info)
    # The new capital loses nothing to corruption: its shields are its production.
    assert sum(t[3] for t in info["worked"]) == info["shields_per_turn"] and len(info["worked"]) == 2
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


def check_city_screen(info: dict) -> None:
    """The city command's worked and workable tiles: the centre first, then the citizens' tiles with the engine's
    yields, which add up to the city's food and production."""
    worked, workable = info["worked"], {tuple(t) for t in info["workable"]}
    assert all(len(t) == 5 and all(isinstance(v, int) for v in t) for t in worked)
    assert worked[0][:2] == [info["x"], info["y"]] and len(worked) <= info["size"] + 1
    assert worked[1:] == [[t["x"], t["y"], t["yield"]["food"], t["yield"]["shields"], t["yield"]["commerce"]]
                          for t in info["tiles_worked"]]
    assert len({(t[0], t[1]) for t in worked}) == len(worked)
    assert all((t[0], t[1]) in workable for t in worked[1:]) and (info["x"], info["y"]) not in workable
    assert all(len(t) == 2 for t in info["workable"]) and len(workable) == len(info["workable"]) >= len(worked) - 1
    # City.CurrentFoodYield and CurrentProductionYield sum these; a citizen eats two food, and corruption or disorder
    # only take shields away (buildings add some, patches/0016).
    assert sum(t[2] for t in worked) - 2 * info["size"] == info["food_per_turn"]
    assert sum(t[3] for t in worked) + info["shields"]["from_buildings"] >= info["shields_per_turn"]
    check_city_figures(info)
    # The rest of the city screen. Shields: its tiles' production, split into useful and corrupt (specialists add none),
    # plus what its buildings add (patches/0016).
    assert info["shields"]["total"] - info["shields"]["from_buildings"] == sum(t[3] for t in worked)
    # Commerce: its tiles' commerce, plus what each specialist adds, plus Wealth's, plus what its buildings add.
    specialists = info["specialists"]
    assert info["commerce"]["total"] - info["commerce"]["from_buildings"] == sum(t[4] for t in worked) + info[
        "commerce"]["wealth"] + sum(s["count"] * (s["taxes"] + s["research"] + s["luxuries"]) for s in specialists)
    # Culture: City.GetCulturePerTurn, GetCulture and the next border level's 10^n, as "Total: x/y".
    culture = info["culture"]
    assert set(culture) == {"per_turn", "total", "next_border"} and culture["per_turn"] >= 0 and culture["total"] >= 0
    assert culture["next_border"] == 10 ** len(str(max(1, culture["total"]))) > culture["total"]
    if "Palace" in info["buildings"]:
        assert culture["per_turn"] > 0
    # Resources: name, icon (resources.png index) and count, strategic and luxury apart.
    for kind in ("strategic", "luxuries"):
        assert all(set(r) == {"name", "icon", "count"} and r["icon"] >= 0 and r["count"] >= 1 for r in info[kind])
    assert not {r["name"] for r in info["strategic"]} & {r["name"] for r in info["luxuries"]}
    # Citizens, one per resident in the client's order: laborers happy, content, unhappy, then the specialists.
    citizens = info["citizens"]
    assert len(citizens) == info["size"]
    order = {"happy": 0, "content": 1, "unhappy": 2, None: 3}
    assert [order[c["mood"]] for c in citizens] == sorted(order[c["mood"]] for c in citizens)
    laborers = [c for c in citizens if c["works"] == "tile"]
    assert all(c["mood"] is not None and "specialist" not in c for c in laborers)
    assert sorted(tuple(c["tile"]) for c in laborers) == sorted((t["x"], t["y"]) for t in info["tiles_worked"])
    workers = [c for c in citizens if c["works"] == "specialist"]
    assert all(c["mood"] is None and c["specialist"] for c in workers)
    assert [s["type"] for s in specialists] == list(dict.fromkeys(c["specialist"] for c in workers))
    assert all(s["index"] >= 1 and s["count"] >= 1 for s in specialists)
    assert sum(s["count"] for s in specialists) == len(workers) == len(citizens) - len(laborers)
    # The happy/content/unhappy counts are the engine's over every resident (a specialist keeps a mood it no longer
    # updates); the laborers' moods are within them.
    for mood in ("happy", "content", "unhappy"):
        assert sum(c["mood"] == mood for c in laborers) <= info[mood]
    assert info["happy"] + info["content"] + info["unhappy"] == info["size"]


def check_city_figures(c: dict) -> None:
    """The domestic advisor's per-city figures, in state's cities and the city command alike."""
    commerce, shields = c["commerce"], c["shields"]
    assert set(commerce) == {"total", "taxes", "science", "luxury", "corrupt", "wealth", "from_buildings"}
    assert commerce["total"] == sum(v for k, v in commerce.items() if k not in ("total", "from_buildings"))
    assert all(v >= 0 for v in commerce.values())
    assert set(shields) == {"total", "useful", "corrupt", "from_buildings"}
    assert shields["total"] == shields["useful"] + shields["corrupt"]
    assert 0 <= shields["from_buildings"] <= shields["useful"]
    # What the buildings add (patches/0016) is in the parts above, and only from the buildings' percentages.
    assert set(c["bonus"]) == {"science", "tax", "luxury", "shields"} and all(v >= 0 for v in c["bonus"].values())
    if not any(c["bonus"].values()):
        assert commerce["from_buildings"] == shields["from_buildings"] == 0
    assert shields["useful"] == c["shields_per_turn"] and shields["corrupt"] >= 0
    if c["disorder"]:
        assert shields["useful"] == 0
    assert c["food_eaten"] == 2 * c["size"] and c["maintenance"] >= 0


def check_finance(s: dict) -> None:
    """state.finance is Player.AggregateFlows as the domestic advisor shows it: the parts add up, the net is
    gold_per_turn, and the cities' figures add up to its city lines."""
    f = s["finance"]
    income, expenses = f["income"], f["expenses"]
    assert set(income) == {"cities", "taxmen", "other_civs", "interest", "total"}
    assert set(expenses) == {"science", "entertainment", "corruption", "maintenance", "unit_costs", "other_civs",
                             "total"}
    assert income["total"] == sum(v for k, v in income.items() if k != "total")
    assert expenses["total"] == sum(v for k, v in expenses.items() if k != "total")
    assert income["total"] - expenses["total"] == s["gold_per_turn"]
    cities = s["cities"]
    for c in cities:
        check_city_figures(c)
    total = {k: sum(c["commerce"][k] for c in cities) for k in ("total", "science", "luxury", "corrupt")}
    # The cities' commerce is the advisor's "From cities" plus "From taxmen" (it moves the tax collectors' share).
    assert total["total"] == income["cities"] + income["taxmen"]
    assert (total["science"], total["luxury"], total["corrupt"]) == (
        expenses["science"], expenses["entertainment"], expenses["corruption"])
    assert sum(c["maintenance"] for c in cities) == expenses["maintenance"]


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


def test_a_tech_got_out_of_order_leaves_the_research_queue(launch, tmp_path):
    """patches/0015: a tech further down the research queue that the seat got another way (a trade) stayed in it;
    once it came to the head the engine picked it again and again, and the turn hung."""
    b = launch("--autosave", str(tmp_path / "a"))
    b.call("new_game", seed=SEED, turn_limit=200)
    found_capital(b)
    queue = b.call("set_research", tech="Monarchy")["queue"]
    assert len(queue) >= 3
    b.call("end_turn", skip_idle=True)
    path = tmp_path / "a" / "autosave.json"
    save = json.loads(path.read_text())
    me = next(p for p in save["game"]["players"] if p["human"])
    me["knownTechs"].append(me["researchQueue"][1])   # as if traded for
    path.write_text(json.dumps(save))
    r = launch("--timeout", "15")
    r.call("load", path=str(path))
    res = r.call("autoplay", turns=60, policy="null")
    research = r.call("state")["research"]
    assert res["turn"] > 30 and queue[0] in r.call("techs")["known"]
    assert queue[1] not in research["queue"] and research["current"] not in r.call("techs")["known"]


def score_victory(b: Bridge, turn: int) -> dict | None:
    """The victory the score race gives at `turn`: the top score's, or no one's on a tie."""
    totals = sorted((p["score"]["total"], p["civ"]) for p in b.call("score")["players"] if not p["defeated"])
    if totals[-1][0] == totals[-2][0]:
        return None
    return {"kind": "score", "civ": totals[-1][1], "label": None, "turn": turn}


def test_the_top_score_wins_at_the_turn_limit(launch):
    """Civ III's score victory: at the turn limit the highest score (of every civilization, the AI's included)
    wins, and a tie on top is no one's; the state dates each turn, and its race says where the seat stands."""
    b = launch()
    b.call("new_game", seed=SEED, turn_limit=3)
    s = b.call("state")
    assert s["date"] == "4000 BC" and s["victory"] is None
    race = s["race"]
    assert race["civs_left"] == len([p for p in b.call("score")["players"] if not p["defeated"]])
    assert race["you"]["you"] and race["you"]["civ"] == "Rome" and race["domination"] == 0.667
    assert {"civ", "you", "score", "land", "pop", "culture"} == set(race["leader"]) == set(race["nearest_domination"])
    found_capital(b)
    while not (res := b.call("end_turn", skip_idle=True))["game_over"]:
        pass
    assert b.call("state")["date"] == "3850 BC"
    assert score_victory(b, 3) is None and b.call("state")["victory"] is None   # everyone founded a capital: a tie
    assert "victory" not in [e["kind"] for e in res["events"]]

    b = launch()
    b.call("new_game", seed=SEED, turn_limit=50)
    b.call("autoplay", turns=49, policy="engine_ai")
    res = b.call("end_turn", skip_idle=True)
    victory = score_victory(b, 50)
    assert victory and victory["civ"] != "Rome" and res["game_over"]   # an AI's victory ends the game too
    assert b.call("state")["victory"] == b.call("score")["victory"] == victory
    top = next(p for p in b.call("score")["players"] if p["civ"] == victory["civ"])
    assert res["events"][-1]["kind"] == "victory" and res["events"][-1]["text"].startswith(
        f"{victory['civ']} won on score at the turn limit: {top['score']['total']} to ")
    assert f"{victory['civ']} won on score on turn 50" in b.error("end_turn", skip_idle=True)["message"]


def test_the_last_civilization_left_wins_by_conquest(launch, tmp_path):
    """Civ III's conquest victory in a one-seat game: every other civilization is gone (the save edited so)."""
    b = launch("--autosave", str(tmp_path / "a"))
    b.call("new_game", seed=SEED, turn_limit=50)
    found_capital(b)
    b.call("end_turn", skip_idle=True)
    path = tmp_path / "a" / "autosave.json"
    save = json.loads(path.read_text())
    g = save["game"]
    me = next(p for p in g["players"] if p["human"])["id"]
    others = {p["id"] for p in g["players"] if not p["human"] and "Barbarian" not in p["civilization"]}
    g["cities"] = [c for c in g["cities"] if c["owner"] not in others]
    g["units"] = [u for u in g["units"] if u["owner"] not in others]
    for p in g["players"]:
        p["defeated"] = p["defeated"] or p["id"] in others
    path.write_text(json.dumps(save))
    r = launch()
    r.call("load", path=str(path))
    assert r.call("state")["race"]["civs_left"] == 1 and r.call("state")["victory"] is None
    res = r.call("end_turn", skip_idle=True)
    assert me and res["game_over"] and r.call("state")["victory"] == {
        "kind": "conquest", "civ": "Rome", "label": None, "turn": 2}
    won = "Rome won by conquest: it is the last civilization left."
    assert res["events"][-1] == {"turn": 1, "kind": "victory", "text": won}


def test_two_thirds_of_the_land_and_people_win_by_domination(launch, tmp_path):
    """Civ III's domination victory: the save edited so that Rome holds all but one of its rival's cities."""
    b = launch("--autosave", str(tmp_path / "a"))
    b.call("new_game", seed=SEED, size="Tiny", opponents=1, turn_limit=400)
    b.call("autoplay", turns=250, policy="engine_ai")
    b.call("end_turn", skip_idle=True)
    path = tmp_path / "a" / "autosave.json"
    save = json.loads(path.read_text())
    g = save["game"]
    me = next(p for p in g["players"] if p["human"])["id"]
    rival = next(p for p in g["players"] if not p["human"] and "Barbarian" not in p["civilization"])["id"]
    given = [c for c in g["cities"] if c["owner"] == rival][1:]
    assert given
    spots = set()
    for c in given:
        c["owner"], c["capital"] = me, False
        c["perPlayerCulture"][me] = c["perPlayerCulture"].pop(rival, 0)
        c["buildings"] = [x for x in c.get("buildings", []) if x["building"] != "Palace"]
        spots.add((c["location"]["x"], c["location"]["y"]))
    g["units"] = [u for u in g["units"] if not (u["owner"] == rival and (
        u["currentLocation"]["x"], u["currentLocation"]["y"]) in spots)]
    path.write_text(json.dumps(save))
    r = launch()
    r.call("load", path=str(path))
    you = r.call("state")["race"]["you"]
    assert you["land"] >= 2 / 3 and you["pop"] >= 2 / 3, you
    res = r.call("end_turn", skip_idle=True)
    turn = r.call("state")["turn"]
    assert res["game_over"] and r.call("state")["victory"] == {
        "kind": "domination", "civ": "Rome", "label": None, "turn": turn}
    assert res["events"][-1]["kind"] == "victory" and res["events"][-1]["text"].startswith("Rome won by domination: ")


def test_a_riot_says_which_luxury_rate_ends_it(launch):
    """A disorder blocker carries the moods and the lowest luxury rate that calms the city (the brief folds several
    riots into one line with the rate that calms them all)."""
    b = launch()
    b.call("new_game", seed=SEED, turn_limit=200)
    b.call("autoplay", turns=80, policy="engine_ai")
    b.call("set_rates", science=6, luxury=0)
    [riot] = [x for x in b.call("state")["blockers"] if x["kind"] == "disorder"]
    assert riot["unhappy"] > riot["happy"] and riot["now"] is False and riot["luxury"] > 0
    assert f"raise luxury to {riot['luxury'] * 10}%" in riot["message"]
    b.call("set_rates", science=6, luxury=riot["luxury"])
    assert not [x for x in b.call("state")["blockers"] if x["kind"] == "disorder"]


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
    assert s["victory"] is None   # three civs are left: no one has won
    assert game.error("end_turn", skip_idle=True)["code"] == "game_over"


def test_the_last_civ_left_wins_when_the_seat_disbands_itself(launch):
    """A one-seat game ends in the seat's own turn when it is defeated; the last civ left has won by conquest."""
    b = launch()
    b.call("new_game", seed=SEED, size="Tiny", opponents=1)
    b.call("unit_order", unit="u2", order="disband")
    res = b.call("unit_order", unit="u1", order="disband")
    rival = next(p["civ"] for p in b.call("score")["players"] if not p["is_human"])
    assert res["message"].endswith(f"the game is over. {rival} won by conquest: it is the last civilization left.")
    assert b.call("state")["victory"] == {"kind": "conquest", "civ": rival, "label": None, "turn": 0}


def test_a_unit_disbanded_in_its_city_adds_its_shields(game):
    """patches/0014: the ruleset's disband script runs (it failed anywhere inside the civ's borders) and gives the
    city on the unit's tile a share of the unit's cost."""
    city = found_capital(game)
    worker = unit(game.call("state"), "u2")
    assert (worker["x"], worker["y"]) == (city["x"], city["y"])
    before = game.call("city", city="c1")["production_stored"]
    res = game.call("unit_order", unit="u2", order="disband")
    after = game.call("city", city="c1")["production_stored"]
    assert after > before
    gained = f"Rome gained {after - before} shields toward {city['producing']}."
    assert res["message"] == f"u2 Worker was disbanded. {gained}"


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
    for seed in DEFEAT_SEEDS:
        b = launch()
        b.call("new_game", seed=seed, barbarians="Raging")
        res = b.call("autoplay", turns=60, policy="null", record=True, timeout=120)
        assert res["turn"] == 60 and res["game_over"]
        if res["defeated"]:
            break
    else:
        pytest.fail(f"no seed of {DEFEAT_SEEDS} loses its settler; find another")
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

    # The battle, as known_map has it: this unit against the target, and the unit lives on unless it lost.
    battle, km = res["battle"], b.call("known_map")
    if target["defender"] is None:
        assert battle is None
        return
    me_index = next(p["index"] for p in km["players"] if p["me"])
    check_battle(battle, me_index, km)
    assert battle in km["battles"] and battle["turn"] == km["turn"] and battle["kind"] == "attack"
    a, d = battle["attacker"], battle["defender"]
    assert (a["id"], a["type"], a["x"], a["y"], a["owner"]) == (unit["id"], unit["type"], me["x"], me["y"], me_index)
    assert (d["x"], d["y"], d["id"]) == (target["x"], target["y"], None)
    assert target["defender"] == f"{d['type']} {d['hp_before']}/{d['hp_max']} hp"
    assert (res["unit"] is None) == (battle["winner"] == "defender")
    assert res["unit"] is None or res["unit"]["hp"] >= a["hp_after"]  # a winner may be promoted, one hp more
    assert battle["city"] == (None if target["city"] is None else {"x": d["x"], "y": d["y"], "name": target["city"]})
    assert battle["razed"] == ("fell and was destroyed" in res["message"])
    assert battle["captured"] == ("is yours now" in res["message"])


def test_battles_the_seat_saw_this_turn_and_the_last(launch):
    """The engine AI plays every civ; barbarians and AIs attack. Each battle the seat saw shows for two turns, the same
    record each time, then goes."""
    b = launch()
    b.call("new_game", seed=SEED, turn_limit=400, barbarians="Raging")
    records, gone = {}, set()
    for _ in range(40):
        b.call("autoplay", turns=1, policy="engine_ai")
        km = b.call("known_map")
        check_known_map(km, b.call("world"), b.call("state"))
        now = {x["id"]: x for x in km["battles"]}
        assert all(records.get(i, x) == x for i, x in now.items()), "a battle's record never changes"
        assert not gone & set(now), "a battle that went does not come back"
        gone |= {i for i, x in records.items() if x["turn"] < km["turn"] - 1}
        assert not gone & set(now)
        records.update(now)
        if len(records) >= 3 and gone:
            break
    else:
        pytest.fail(f"only {len(records)} battles in sight by T{km['turn']}")
    me = next(p["index"] for p in km["players"] if p["me"])
    # Ids grow with each battle; the seat fights in most, and sees the rest from its tiles.
    assert sorted(records) == list(records)
    assert any(me in (x["attacker"]["owner"], x["defender"]["owner"]) for x in records.values())
    for x in records.values():
        if me not in (x["attacker"]["owner"], x["defender"]["owner"]):
            known = {(t[0], t[1]) for t in km["tiles"]}
            assert {(x[side]["x"], x[side]["y"]) for side in ("attacker", "defender")} & known


def check_cities_and_borders(world: dict) -> None:
    """Every city has citizens and sits on a tile its owner owns; a civ owns tiles only while it has a city; every
    civ with a city has one capital (patches/0013: the palace moves when the capital falls)."""
    owner_at = {(t[0], t[1]): t[4] for t in world["tiles"]}
    with_cities = {c["owner"] for c in world["cities"]}
    for c in world["cities"]:
        assert c["size"] >= 1, c
        assert owner_at[(c["x"], c["y"])] == c["owner"], f"{c['name']}'s tile is not its owner's"
    assert {o for o in owner_at.values() if o >= 0} <= with_cities, "a civ with no city owns tiles"
    capitals = [c["owner"] for c in world["cities"] if c["capital"]]
    assert len(capitals) == len(set(capitals)), "a civ has two capitals"
    assert set(capitals) == with_cities, f"civs with cities and no capital: {with_cities - set(capitals)}"


def play_until_the_seat_takes_a_city(b: Bridge, seed: int) -> tuple[bool, list[tuple]]:
    """Plays the engine AI in every seat of a new game on `seed`, checking each turn that every city that changed
    hands did so as patches/0011 says, until the seat has taken a city and two have changed hands, or T390. Returns
    whether the seat took one, and the cities taken: (turn, name, old owner, new owner)."""
    b.call("new_game", seed=seed, opponents=5, barbarians="Raging", turn_limit=400)
    prev = b.call("world")
    taken = []
    for _ in range(390):
        b.call("autoplay", turns=1, policy="engine_ai")
        world, state = b.call("world"), b.call("state")
        check_cities_and_borders(world)
        me = next(p["index"] for p in world["players"] if p["is_human"])
        # By id: two civs can found cities of the same name.
        before = {c["id"]: c for c in prev["cities"]}
        kinds = {e["kind"] for e in state["last_events"]}
        for c in world["cities"]:
            was = before.get(c["id"])
            if was is None or was["owner"] == c["owner"]:
                continue
            taken.append((world["turn"], c["name"], was["owner"], c["owner"]))
            assert not c["capital"], f"{c['name']} is still a capital"
            assert 1 <= c["size"] <= was["size"], (was, c)   # a citizen fewer (it may have grown back since)
            if c["owner"] == me:
                mine = next(x for x in state["cities"] if x["name"] == c["name"])
                info = b.call("city", city=mine["id"])
                assert "Palace" not in info["buildings"] and not info["capital"]
                assert info["producing"] is not None
                assert "city_captured" in kinds
            if was["owner"] == me:
                assert "city_lost" in kinds and all(x["name"] != c["name"] for x in state["cities"])
        check_known_map(b.call("known_map"), world, state)
        prev = world
        if any(t[3] == me for t in taken) and len(taken) >= 2:
            return True, taken
    return False, taken


def test_cities_change_hands(launch, tmp_path):
    """The engine AI plays every civ, at war with raging barbarians. A city taken (patches/0011) changes hands with a
    citizen fewer, its capital status and palace gone, its borders its new owner's, and the seats are told; one of
    size 1 is destroyed instead. Checked each turn, until the seat has taken a city."""
    tried = {}
    for seed in CAPTURE_SEEDS:
        saves = tmp_path / f"seed{seed}"
        b = launch("--autosave", str(saves))
        done, tried[seed] = play_until_the_seat_takes_a_city(b, seed)
        if done:
            break
    else:
        pytest.fail(f"the seat takes no city by T390 on any of {CAPTURE_SEEDS}; cities taken: {tried}")

    # The game, saved after the captures, loads as it was.
    restored = launch()
    restored.call("load", path=str(saves / "autosave.json"))
    assert restored.call("world")["cities"] == b.call("world")["cities"]


def city_next_to_a_soldier(launch, tmp_path, size: int, capital: bool, owner_cities: int = 1,
                           turns: int = 40, homeless: bool = False) -> tuple[Bridge, dict, dict, dict]:
    """A saved game edited so that one of the seat's soldiers stands next to an enemy city of `size` (its capital or
    not) with no defender in it, only an enemy Worker; loaded, at war with the city's owner. `homeless`: the seat has
    no city left. Returns the bridge, the soldier (state), the city (world) and the save's game."""
    b = launch("--autosave", str(tmp_path / "a"))
    b.call("new_game", seed=SEED, opponents=3, turn_limit=400)
    b.call("autoplay", turns=turns, policy="engine_ai")
    b.call("end_turn", skip_idle=True)
    save = json.loads((tmp_path / "a" / "autosave.json").read_text())
    g = save["game"]
    me_player = next(p for p in g["players"] if p["human"])
    me, met = me_player["id"], set(me_player["playerRelationships"])
    land = {(t["x"], t["y"]) for t in g["map"]["tiles"] if t["baseTerrain"] not in ("coast", "sea", "ocean")}

    def at(o: dict) -> tuple[int, int]:
        loc = o.get("location") or o["currentLocation"]
        return loc["x"], loc["y"]

    occupied = {at(u) for u in g["units"]} | {at(c) for c in g["cities"]}
    free = land - occupied

    def free_next_to(c: dict) -> list[tuple[int, int]]:
        x, y = at(c)
        return [(x + dx, y + dy) for dx, dy in ((1, 1), (-1, 1), (1, -1), (-1, -1)) if (x + dx, y + dy) in free]

    city = next(c for c in g["cities"] if c["owner"] in met and c["capital"] == capital and free_next_to(c)
                and sum(x["owner"] == c["owner"] for x in g["cities"]) >= owner_cities)
    cx, cy = at(city)
    spot = free_next_to(city)[0]
    soldier = next(u for u in g["units"]
                   if u["owner"] == me and u["prototype"] in ("Warrior", "Archer", "Spearman", "Horseman"))
    soldier["currentLocation"] = {"x": spot[0], "y": spot[1]}
    soldier["movePointsRemaining"] = 1
    worker = next(u for u in g["units"] if u["prototype"] == "Worker")
    g["units"] = [u for u in g["units"] if not (u["owner"] == city["owner"] and at(u) == (cx, cy))]
    g["units"].append({**worker, "id": "Worker-999", "owner": city["owner"], "currentLocation": {"x": cx, "y": cy},
                       "previousLocation": {"x": cx, "y": cy}, "isAutomated": False})
    if homeless:
        g["cities"] = [c for c in g["cities"] if c["owner"] != me]
    while len(city["residents"]) > size:
        city["residents"].pop()
    while len(city["residents"]) < size:
        city["residents"].append({**city["residents"][0], "tileWorked": {"x": cx, "y": cy}})
    edited = tmp_path / "edited.json"
    edited.write_text(json.dumps(save))
    b = launch()
    b.call("load", path=str(edited))
    world = b.call("world")
    owner = next(c for c in world["cities"] if (c["x"], c["y"]) == (cx, cy))["owner"]
    b.call("declare_war", civ=world["players"][owner]["civ"])
    state = b.call("state")
    me_unit = next(u for u in state["units"] if (u["x"], u["y"]) == spot)
    return b, me_unit, next(c for c in world["cities"] if (c["x"], c["y"]) == (cx, cy)), g


def test_taking_a_city_keeps_it(launch, tmp_path):
    """patches/0011: a soldier walks into an undefended enemy capital of size 3. The seat holds it at size 2, with
    no palace, empty production and food boxes and something new to build; the enemy Worker in it is gone, and the
    borders around it are the seat's. patches/0013: the old owner's palace moves to its largest city left."""
    b, soldier, city, _ = city_next_to_a_soldier(launch, tmp_path, size=3, capital=True)
    before = b.call("world")
    loser = city["owner"]
    res = b.call("unit_order", unit=soldier["id"], order="attack", x=city["x"], y=city["y"])
    assert "is yours now" in res["message"]
    # The Worker left in the city is its last "defender": the soldier beats it, then walks in.
    assert res["battle"] is None or (res["battle"]["winner"], res["battle"]["captured"]) == ("attacker", True)
    world, state = b.call("world"), b.call("state")
    check_cities_and_borders(world)
    me = next(p["index"] for p in world["players"] if p["is_human"])
    now = next(c for c in world["cities"] if c["name"] == city["name"])
    assert (now["owner"], now["size"], now["capital"]) == (me, 2, False)
    left = [c for c in world["cities"] if c["owner"] == loser]
    capitals = [c for c in left if c["capital"]]
    assert len(capitals) == (1 if left else 0), capitals
    assert not left or capitals[0]["size"] == max(c["size"] for c in left), "the palace went to the largest city"
    assert sum(c["capital"] for c in world["cities"] if c["owner"] == me) == 1, "the seat's own capital stays the one"
    assert not [u for u in world["units"] if (u["x"], u["y"]) == (city["x"], city["y"]) and u["owner"] != me]
    mine = next(c for c in state["cities"] if c["name"] == city["name"])
    assert mine["id"] in res["message"]
    info = b.call("city", city=mine["id"])
    assert "Palace" not in info["buildings"] and not info["capital"]
    assert (info["production_stored"], info["food_stored"]) == (0, 0) and info["producing"]
    assert len(info["citizens"]) == 2 and info["worked"][0][:2] == [city["x"], city["y"]]
    owner_at = {(t[0], t[1]): t[4] for t in world["tiles"]}
    assert all(owner_at[(x, y)] == me for x, y, *_ in info["worked"])
    assert len(world["cities"]) == len(before["cities"])
    # The game goes on: the next turn plays, and the city is still a city.
    b.call("end_turn", skip_idle=True)
    check_cities_and_borders(b.call("world"))


def test_losing_the_capital_moves_the_palace(launch, tmp_path):
    """patches/0013: a civ whose capital is taken gets a new one at once, free: its largest city left."""
    b, soldier, city, _ = city_next_to_a_soldier(launch, tmp_path, size=3, capital=True, owner_cities=2, turns=80)
    loser = city["owner"]
    left = [c for c in b.call("world")["cities"] if c["owner"] == loser and c["name"] != city["name"]]
    assert left and not any(c["capital"] for c in left)
    res = b.call("unit_order", unit=soldier["id"], order="attack", x=city["x"], y=city["y"])
    assert "is yours now" in res["message"]
    world = b.call("world")
    check_cities_and_borders(world)
    now = [c for c in world["cities"] if c["owner"] == loser]
    [capital] = [c for c in now if c["capital"]]
    assert capital["size"] == max(c["size"] for c in now)
    b.call("end_turn", skip_idle=True)
    assert [c["name"] for c in b.call("world")["cities"] if c["owner"] == loser and c["capital"]] == [capital["name"]]


def test_a_civ_with_no_city_makes_its_conquest_its_capital(launch, tmp_path):
    """patches/0013: a civ that holds no city when it takes one gets its palace there (as one founding its first
    city does); it had none, so every civ with cities keeps exactly one capital."""
    b, soldier, city, _ = city_next_to_a_soldier(launch, tmp_path, size=3, capital=False, homeless=True)
    assert not b.call("state")["cities"]
    res = b.call("unit_order", unit=soldier["id"], order="attack", x=city["x"], y=city["y"])
    assert "is yours now" in res["message"]
    [mine] = b.call("state")["cities"]
    assert mine["capital"] and "Palace" in mine["buildings"]
    check_cities_and_borders(b.call("world"))


def test_taking_a_city_of_size_1_destroys_it(launch, tmp_path):
    b, soldier, city, _ = city_next_to_a_soldier(launch, tmp_path, size=1, capital=False)
    before = b.call("world")
    res = b.call("unit_order", unit=soldier["id"], order="attack", x=city["x"], y=city["y"])
    assert "fell and was destroyed" in res["message"]
    world = b.call("world")
    check_cities_and_borders(world)
    assert city["name"] not in {c["name"] for c in world["cities"]}
    assert len(world["cities"]) == len(before["cities"]) - 1


def test_a_game_with_captures_replays_the_same(launch):
    """Taking a city draws from the game's own random numbers only: the same seed plays the same game."""
    worlds = []
    for _ in range(2):
        b = launch()
        b.call("new_game", seed=SEED, opponents=5, barbarians="Raging", turn_limit=400)
        b.call("autoplay", turns=150, policy="engine_ai")
        w = b.call("world")
        worlds.append({k: w[k] for k in ("turn", "players", "cities", "units", "tiles")})
        b.close()
    assert worlds[0] == worlds[1]


def test_a_production_queue_goes_before_the_engines_pick(launch, tmp_path):
    """set_production's `then`: each time the city completes something it builds the next queued item it can, and no
    choice waits on the agent; an item it cannot build leaves the queue with a note; once the queue is empty the
    engine picks again. The queue survives a save."""
    b = launch("--autosave", str(tmp_path / "a"))
    b.call("new_game", seed=SEED, turn_limit=100)
    found_capital(b)
    res = b.call("set_production", city="c1", item="Warrior", then=["Palace", "Warrior", "warrior"])
    assert res["city"]["queue"] == ["Palace", "Warrior", "Warrior"]
    assert "Then: Palace, Warrior, Warrior." in res["message"]
    assert b.call("state")["cities"][0]["queue"] == ["Palace", "Warrior", "Warrior"]
    built = []
    for _ in range(40):
        events = b.call("end_turn", skip_idle=True)["events"]
        built += [e["text"] for e in events if e["kind"] == "built"]
        s = b.call("state")
        if s["cities"][0]["queue"] == ["Warrior"]:
            assert not any(x["kind"] == "choose_production" for x in s["blockers"])
            assert s["cities"][0]["producing"] == "Warrior" and s["cities"][0]["producing_source"] == "agent"
            break
    else:
        pytest.fail(f"the queue never moved: {built}")
    assert "next from your queue: Warrior (Palace left the queue: " in built[-1], built
    # saved and restored with the game
    restored = launch()
    restored.call("load", path=str(tmp_path / "a" / "autosave.json"))
    assert restored.call("state")["cities"][0]["queue"] == ["Warrior"]
    for _ in range(40):
        events = b.call("end_turn", skip_idle=True)["events"]
        texts = [e["text"] for e in events if e["kind"] == "built"]
        if texts:
            break
    assert "next from your queue: Warrior" in texts[-1] and b.call("state")["cities"][0]["queue"] == []
    for _ in range(40):
        events = b.call("end_turn", skip_idle=True)["events"]
        texts = [e["text"] for e in events if e["kind"] == "built"]
        if texts:
            break
    assert "the engine picked" in texts[-1]
    assert any(x["kind"] == "choose_production" for x in b.call("state")["blockers"])
    # what a queue takes
    assert b.error("set_production", city="c1", item="Warrior", then=["Warrior"] * 11)["code"] == "bad_args"
    err = b.error("set_production", city="c1", item="Warrior", then=["Warior"])
    assert err["code"] == "unknown_item" and "The closest is Warrior." in err["message"]
    assert b.call("set_production", city="c1", item="Warrior", then=[])["city"]["queue"] == []


def test_orders_for_many_units_and_cities_at_once(launch):
    """unit_orders: one call orders many units, by id or by group (idle, idle:Type, all:Type); one order that fails
    doesn't stop the rest. set_production names several cities at once: a list, all, or pending."""
    b = launch()
    b.call("new_game", seed=SEED, turn_limit=200)
    res = b.call("unit_orders", orders=[
        {"unit": "u1", "order": "found_city"}, {"unit": "idle:worker", "order": "auto_work"},
        {"unit": "u99", "order": "fortify"}, {"unit": "all", "order": "fortify"},
        {"unit": "idle:Tank", "order": "fortify"}, {"unit": "idle", "order": "hold"}])
    assert [(r["unit"], r["ok"]) for r in res["results"]] == [
        ("u1", True), ("u2", True), ("u99", False), ("all", False), ("idle:Tank", False), ("idle", False)]
    codes = [r.get("code") for r in res["results"] if not r["ok"]]
    assert codes == ["unknown_unit", "bad_args", "unknown_unit", "no_units"]
    assert (res["ok"], res["failed"]) == (2, 4) and res["message"] == "2 orders done, 4 failed."
    s = b.call("state")
    assert len(s["cities"]) == 1 and unit(s, "u2")["status"] == "auto_work"
    assert not [x for x in s["blockers"] if x["kind"] == "idle_unit"]
    assert b.error("unit_orders", orders=[])["code"] == "bad_args"
    assert b.error("unit_orders", orders=[{"unit": "u2", "order": "hold"}] * 101)["code"] == "bad_args"
    # many cities: the engine AI builds an empire to try it on
    b.call("autoplay", turns=60, policy="engine_ai")
    cities = [c["id"] for c in b.call("state")["cities"]]
    assert len(cities) >= 3
    res = b.call("set_production", city="all", item="Warrior", then=["Warrior"])
    assert res["ok"] == len(cities) and res["failed"] == 0 and {c["producing"] for c in res["cities"]} == {"Warrior"}
    assert all(c["queue"] == ["Warrior"] for c in res["cities"])
    err = b.error("set_production", city=f"{cities[0]},{cities[1]}", item="Palace")
    assert err["code"] == "unknown_item"     # none of them can: the first city's reason
    res = b.call("set_production", city=f"{cities[0]},{cities[1]}", item="Warrior")
    assert [r["city"] for r in res["results"]] == cities[:2] and res["ok"] == 2
    assert b.error("set_production", city="c1,c999", item="Warrior")["code"] == "unknown_city"
    assert b.error("set_production", city=" , ", item="Warrior")["code"] == "unknown_city"
    pending = b.call("set_production", city="pending", item="Warrior") if any(
        x["kind"] in ("choose_production", "no_production") for x in b.call("state")["blockers"]) else None
    assert pending is None or pending["ok"] >= 1
    assert b.error("set_production", city="pending", item="Warrior")["code"] == "no_cities"


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


def test_known_map_has_each_step_the_seat_saw(launch):
    """patches/0012: a unit sent somewhere walks there a step at a time, and known_map has each step: the seat's own
    goto as a run of steps with its id, other civs' and barbarians' units in sight as theirs, without ids. Without the
    patch there are no steps at all."""
    b = launch()
    b.call("new_game", seed=SEED, turn_limit=400, barbarians="Raging")
    u = unit(b.call("state"), "u2")
    tiles = b.call("map", x=u["x"], y=u["y"], radius=2)["tiles"]
    dest = next(t for t in tiles if t["dist"] == 2 and t["city_site"]["ok"])
    res = b.call("unit_order", unit="u2", order="goto", x=dest["x"], y=dest["y"])
    km = b.call("known_map")
    me = next(p["index"] for p in km["players"] if p["me"])
    [walk] = km["moves"]
    assert (walk["id"], walk["owner"], walk["type"], walk["turn"]) == ("u2", me, "Worker", 0)
    assert walk["path"][0] == [u["x"], u["y"]] and walk["path"][-1] == [res["unit"]["x"], res["unit"]["y"]]
    check_moves(km, me)
    # An entry stays the same while it is listed, and no step is ever in two entries: a page plays each once.
    entries, covered = {}, {}
    seen_foreign = False
    for _ in range(40):
        b.call("autoplay", turns=1, policy="engine_ai")
        km = b.call("known_map")
        check_known_map(km, b.call("world"), b.call("state"))
        for m in km["moves"]:
            assert entries.setdefault(m["seq"], m) == m, (m, entries[m["seq"]])
            for k in range(m["seq"], m["seq"] + len(m["path"]) - 1):
                assert covered.setdefault(k, m["seq"]) == m["seq"], f"step {k} in two entries"
        seen_foreign |= any(m["owner"] != me for m in km["moves"])
        if seen_foreign and km["turn"] >= 20:
            break
    else:
        pytest.fail(f"no other civ's unit seen moving by T{km['turn']}")


def test_a_goto_over_turns_lists_each_step_once(launch):
    """A seat's worker sent two tiles away takes a step on the order, the turn's last move (no AI civ, the other seat
    has ended), and the next on its standing order, the new turn's first: each turn's steps are an entry of their own,
    which stays the same while listed, so a page never walks a step twice."""
    b = launch()
    b.call("new_game", seed=SEED, turn_limit=100, opponents=1, seats=["Greece"])
    u = unit(b.call("state", seat="Greece"), "u2")
    far = next(t for t in b.call("map", seat="Greece", x=u["x"], y=u["y"], radius=2)["tiles"] if t["dist"] == 2)
    b.call("unit_order", seat="Greece", unit="u2", order="goto", x=far["x"], y=far["y"])
    entries, covered, turns = {}, {}, set()
    for _ in range(4):
        km = b.call("known_map", seat="Greece")
        for m in km["moves"]:
            assert entries.setdefault(m["seq"], m) == m, (m, entries[m["seq"]])
            for k in range(m["seq"], m["seq"] + len(m["path"]) - 1):
                assert covered.setdefault(k, m["seq"]) == m["seq"], f"step {k} in two entries"
            turns |= {m["turn"]} if m["id"] == "u2" else set()
        for civ in ("Rome", "Greece"):
            b.call("end_turn", seat=civ, skip_idle=True)
    assert turns == {0, 1}, turns   # a step on the order, the next on the standing order


def test_snapshots_say_how_every_unit_moved(launch, tmp_path):
    """Every snapshot has each unit's steps since the last one, and every battle, in one sequence: a unit's runs lead
    from where the last snapshot had it to where this one has it."""
    b = launch("--record", str(tmp_path / "rec"))
    b.call("new_game", seed=SEED, opponents=4, turn_limit=400, barbarians="Raging")
    b.call("autoplay", turns=40, policy="engine_ai")
    snaps = snapshots(tmp_path / "rec")
    seqs = [m["seq"] for s in snaps for m in s["moves"]] + [x["seq"] for s in snaps for x in s["battles"]]
    assert len(seqs) == len(set(seqs)) and len(seqs) > 100
    traced = followed = 0
    for before, now in pairwise(snaps):
        assert [m["seq"] for m in now["moves"]] == sorted(m["seq"] for m in now["moves"])
        for m in now["moves"]:
            assert list(m) == ["seq", "unit", "owner", "type", "path", "seen"] and len(m["path"]) >= 2
            assert isinstance(m["seen"], int) and m["seen"] in (0, 1)
        for x in now["battles"]:
            assert "id" not in x["attacker"] and "id" not in x["defender"] and x["seen"] in (0, 1)
            assert x["winner"] in ("attacker", "defender", "retreat") and x["turn"] == now["turn"] - 1
        was = {u["id"]: u for u in before["units"]}
        for u in now["units"]:
            runs = sorted((m for m in now["moves"] if m["unit"] == u["id"]), key=lambda m: m["seq"])
            if not runs or u["id"] not in was:
                continue
            traced += 1
            path = [[was[u["id"]]["x"], was[u["id"]]["y"]]]
            for m in runs:
                if m["path"][0] != path[-1]:
                    break
                path += m["path"][1:]
            else:
                followed += path[-1] == [u["x"], u["y"]]
    # Units carried aboard move without steps of their own; no others.
    assert traced > 50 and followed >= 0.95 * traced, (followed, traced)


def test_the_sequence_goes_on_in_a_restored_game(launch, tmp_path):
    """An autosave keeps the sequence steps and battles share: after a restore they go on from where it was, so a
    page that has played them doesn't skip new ones."""
    b = launch("--autosave", str(tmp_path / "a"))
    b.call("new_game", seed=SEED, turn_limit=100)
    b.call("autoplay", turns=10, policy="engine_ai")
    km = b.call("known_map")
    last = max([m["seq"] for m in km["moves"]] + [x["seq"] for x in km["battles"]])
    restored = launch()
    restored.call("load", path=str(tmp_path / "a" / "autosave.json"))
    assert restored.call("known_map")["moves"] == []
    restored.call("autoplay", turns=1, policy="engine_ai")
    new = [m["seq"] for m in restored.call("known_map")["moves"]]
    assert new and min(new) > last


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

    # What the client's art draws: a tile's resource (once some civ knows of it), improvements and bonus grassland; a
    # city's era and walls; a unit's hit points and whether it is fortified.
    for row in world["tiles"]:
        assert len(row) == 10 and (row[7] is None or isinstance(row[7], str)) and row[9] in (0, 1)
        assert all(isinstance(i, str) for i in row[8])
    assert any(row[7] for row in world["tiles"]) and any(row[8] for row in world["tiles"])
    assert (city["era"], city["walls"]) == (0, False)
    assert all(0 < u["hp"] <= u["hp_max"] and isinstance(u["fortified"], bool) for u in world["units"])

    # Each snapshot says how the worker went from where the last one had it (patches/0012): its runs of steps, in order.
    for before, now in pairwise(snaps):
        runs = sorted((m for m in now["moves"] if m["unit"] == worker["id"]), key=lambda m: m["seq"])
        was = next(u for u in before["units"] if u["id"] == worker["id"])
        at = next(u for u in now["units"] if u["id"] == worker["id"])
        path = [[was["x"], was["y"]]]
        for m in runs:
            assert m["path"][0] == path[-1] and (m["owner"], m["type"], m["seen"]) == (rome["index"], "Worker", 1)
            path += m["path"][1:]
        assert path[-1] == [at["x"], at["y"]]
    assert sum(len(m["path"]) - 1 for s in snaps for m in s["moves"] if m["unit"] == worker["id"]) >= 3


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
    assert list(km) == ["turn", "width", "height", "wrap_x", "players", "tiles", "cities", "units", "battles", "moves"]
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

    # Battles this turn and the last, oldest first, each one the seat fought or had in sight; and the steps it saw.
    for x in km["battles"]:
        check_battle(x, me, km)
    assert [x["id"] for x in km["battles"]] == sorted({x["id"] for x in km["battles"]})
    check_moves(km, me)


def check_moves(km: dict, me: int) -> None:
    """known_map moves (docs/protocol.md): runs of steps, each to a neighbouring tile, oldest first, numbered in one
    sequence with the battles; only the seat's own units carry ids."""
    seqs = [m["seq"] for m in km["moves"]]
    assert seqs == sorted(set(seqs)) and not set(seqs) & {x["seq"] for x in km["battles"]}
    for m in km["moves"]:
        assert list(m) == ["seq", "turn", "owner", "type", "id", "path"]
        assert m["turn"] in (km["turn"] - 1, km["turn"]) and len(m["path"]) >= 2
        assert 0 <= m["owner"] < len(km["players"]) and isinstance(m["type"], str) and m["type"]
        for a, b in pairwise(m["path"]):
            assert neighbours(km, {"x": a[0], "y": a[1]}, {"x": b[0], "y": b[1]}), m
        assert m["id"] is None if m["owner"] != me else m["id"] is None or m["id"].startswith("u")


def neighbours(km: dict, a: dict, b: dict) -> bool:
    dx, dy = b["x"] - a["x"], b["y"] - a["y"]
    if km["wrap_x"]:
        dx = (dx + km["width"] // 2) % km["width"] - km["width"] // 2
    return (dx, dy) in {(1, -1), (2, 0), (1, 1), (0, 2), (-1, 1), (-2, 0), (-1, -1), (0, -2)}


def check_battle(x: dict, me: int, km: dict) -> None:
    """A known_map battle (docs/protocol.md) against itself: the hit points each side lost are the rounds the other
    won, the winner is the side left standing, and only the seat's own units carry ids."""
    assert list(x) == ["id", "seq", "turn", "kind", "attacker", "defender", "rounds", "winner", "city", "captured",
                       "razed"]
    assert isinstance(x["id"], int) and x["id"] > 0 and x["turn"] in (km["turn"] - 1, km["turn"])
    assert isinstance(x["seq"], int) and x["seq"] >= x["id"]
    assert x["kind"] in ("attack", "bombard") and x["rounds"] and set(x["rounds"]) <= {"a", "d"}
    a, d = x["attacker"], x["defender"]
    for side in (a, d):
        assert list(side) == ["owner", "type", "x", "y", "id", "hp_before", "hp_after", "hp_max"]
        assert 0 <= side["owner"] < len(km["players"]) and isinstance(side["type"], str) and side["type"]
        assert 0 <= side["hp_after"] <= side["hp_before"] <= side["hp_max"]
        if side["owner"] != me:
            assert side["id"] is None
        else:  # only a unit killed before the seat ever saw it has none
            assert isinstance(side["id"], str) and side["id"].startswith("u") or side["hp_after"] == 0
    assert a["owner"] != d["owner"]
    rounds = x["rounds"]
    if x["kind"] == "attack":
        assert neighbours(km, a, d)
        lost_a, lost_d = rounds.count("d"), rounds.count("a")
        if x["winner"] == "retreat":  # the last round's loser withdrew instead of losing its last hit point
            lost_a, lost_d = (lost_a - 1, lost_d) if rounds[-1] == "d" else (lost_a, lost_d - 1)
        assert (a["hp_before"] - a["hp_after"], d["hp_before"] - d["hp_after"]) == (lost_a, lost_d)
        alive = (a["hp_after"] > 0, d["hp_after"] > 0)
        assert alive == {"attacker": (True, False), "defender": (False, True), "retreat": (True, True)}[x["winner"]]
        if x["winner"] != "retreat":
            assert rounds[-1] == x["winner"][0]
    else:  # each round is a shot: "a" a hit, "d" a miss
        assert a["hp_after"] == a["hp_before"] and d["hp_before"] - d["hp_after"] == rounds.count("a")
        assert x["winner"] == ("attacker" if d["hp_after"] == 0 else "defender")
    if x["city"] is not None:
        assert set(x["city"]) == {"x", "y", "name"} and (x["city"]["x"], x["city"]["y"]) == (d["x"], d["y"])
    fell = x["razed"] or x["captured"]
    assert not (x["razed"] and x["captured"])
    assert not fell or (x["winner"] == "attacker" and x["city"] is not None and x["kind"] == "attack")


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
    # Still in the Ancient era while an Ancient tech is left to learn; the state's era is the cities'.
    era = b.call("state")["era"]
    assert era in (0, 1, 2, 3) and all(c["era"] == era for c in km["cities"] if c["owner"] == me)
    if any(t["era"] == "Ancient Times" for t in b.call("techs")["available"]):
        assert era == 0
    # Every city screen's tiles, with a few citizens by now.
    for c in b.call("state")["cities"]:
        check_city_screen(b.call("city", city=c["id"]))

    camps = [(t[0], t[1]) for t in km["tiles"] if "barbarian_camp" in t[8]]
    assert camps, "a barbarian camp should be known by now"
    for x, y in camps:
        [tile] = b.call("map", x=x, y=y, radius=0)["tiles"]
        assert "barbarian_camp" in tile["improvements"]
        assert tile["city_site"] == {"ok": False, "reason": "a barbarian camp is here"}


def test_domestic_advisor_and_city_screen(launch):
    b = launch()
    b.call("new_game", seed=SEED, turn_limit=400)
    s = b.call("state")
    assert s["finance"] == {
        "income": {"cities": 0, "taxmen": 0, "other_civs": 0, "interest": 0, "total": 0},
        "expenses": {"science": 0, "entertainment": 0, "corruption": 0, "maintenance": 0, "unit_costs": 0,
                     "other_civs": 0, "total": 0}}
    found_capital(b)
    check_finance(b.call("state"))
    info = b.call("city", city="c1")
    check_city_screen(info)
    # A new capital: the Palace's culture, none gathered yet, the first border at 10.
    assert info["culture"]["total"] == 0 and info["culture"]["per_turn"] > 0 and info["culture"]["next_border"] == 10
    per_turn = info["culture"]["per_turn"]
    b.call("end_turn", skip_idle=True)
    assert b.call("city", city="c1")["culture"]["total"] == per_turn

    s = until(b, lambda s: len(s["cities"]) >= 4 and s["finance"]["expenses"]["maintenance"] > 0, 300)
    me = next(p["index"] for p in b.call("known_map")["players"] if p["me"])
    for rates in ({"science": 4, "luxury": 2}, {"science": s["rates"]["science"], "luxury": s["rates"]["luxury"]}):
        b.call("set_rates", **rates)
        s = b.call("state")
        check_finance(s)
        if rates["luxury"] == 2:  # 40% tax, 40% science, 20% luxury: every part of the cities' commerce shows
            assert all(s["finance"]["expenses"][k] > 0 for k in ("science", "entertainment", "maintenance"))
            assert sum(c["commerce"]["taxes"] for c in s["cities"]) > 0
        km = b.call("known_map")
        owned: dict[str, int] = {}
        for t in km["tiles"]:
            if t[5] == me and t[7]:
                owned[t[7]] = owned.get(t[7], 0) + 1
        for c in s["cities"]:
            info = b.call("city", city=c["id"])
            check_city_screen(info)
            for key in ("commerce", "shields", "food_eaten", "maintenance"):
                assert info[key] == c[key]
            # A city has the resources on the civ's tiles its road network reaches (TradeNetwork), the city's own
            # tile among them: never more than the civ's tiles with that resource.
            for r in info["strategic"] + info["luxuries"]:
                assert r["count"] <= owned.get(r["name"], 0), (c["name"], r)
    # Rome has gathered culture from its Palace since its founding.
    assert b.call("city", city="c1")["culture"]["total"] > 0


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


def test_battles_reach_the_seats_that_saw_them(launch, tmp_path):
    """A barbarian attacks a seat's unit during the barbarians' turn: that seat sees the battle, a seat far away does
    not, and a restored game still has it."""
    b = launch("--autosave", str(tmp_path / "a"))
    b.call("new_game", seed=SEED, opponents=2, seats=["Greece", "Egypt"], turn_limit=80, barbarians="Raging")
    for civ in SEATS:
        b.call("unit_order", seat=civ, unit="u1", order="found_city")
        b.call("set_production", seat=civ, city="c1", item="Warrior")
        b.call("unit_order", seat=civ, unit="u2", order="explore")
    for _ in range(40):
        end_round(b)
        maps = {civ: b.call("known_map", seat=civ) for civ in SEATS}
        if any(km["battles"] for km in maps.values()):
            break
    else:
        pytest.fail("no battle by T40")
    world = b.call("world")
    index = {p["civ"]: p["index"] for p in world["players"]}
    barbarians = next(p["index"] for p in world["players"] if p["civ"] == "Barbarians")
    battles = {}
    for k, civ in enumerate(SEATS):
        check_known_map(maps[civ], world, b.call("state", seat=civ), k)
        for x in maps[civ]["battles"]:
            assert battles.setdefault(x["id"], x)["rounds"] == x["rounds"]
    assert any(x["attacker"]["owner"] == barbarians for x in battles.values())
    for i, x in battles.items():
        owners = {x["attacker"]["owner"], x["defender"]["owner"]}
        has = {civ for civ in SEATS if any(y["id"] == i for y in maps[civ]["battles"])}
        assert {civ for civ in SEATS if index[civ] in owners} <= has
        assert has != set(SEATS), "the seats far away did not see it"
        for civ in has:
            mine = next(y for y in maps[civ]["battles"] if y["id"] == i)
            for side in ("attacker", "defender"):
                assert {k_: v for k_, v in mine[side].items() if k_ != "id"} == {
                    k_: v for k_, v in x[side].items() if k_ != "id"}
    restored = launch()
    restored.call("load", path=str(tmp_path / "a" / "autosave.json"))
    for civ in SEATS:
        assert restored.call("known_map", seat=civ)["battles"] == maps[civ]["battles"]


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


def test_the_last_civ_left_wins_when_every_seat_disbands_itself(launch):
    """With seats too: once the last seat still playing is defeated in its own turn, no turn ends, so the winner is
    named at once."""
    b = launch()
    b.call("new_game", seed=SEED, opponents=2, seats=["Greece"])
    for civ in ("Greece", "Rome"):
        b.call("unit_order", seat=civ, unit="u2", order="disband")
        res = b.call("unit_order", seat=civ, unit="u1", order="disband")
    ai = next(p["civ"] for p in b.call("score")["players"] if p["seat"] is None)
    assert res["message"].endswith(f"{ai} won by conquest: it is the last civilization left.")
    assert b.call("state", seat="Greece")["victory"] == {"kind": "conquest", "civ": ai, "label": None, "turn": 0}


def test_the_last_seat_standing_wins_by_conquest(launch, tmp_path):
    b = launch("--autosave", str(tmp_path / "a"))
    b.call("new_game", seed=SEED, opponents=1, seats=["Greece"], labels={"Rome": "A", "Greece": "B"})
    b.call("unit_order", seat="Greece", unit="u2", order="disband")
    b.call("unit_order", seat="Greece", unit="u1", order="disband")
    assert b.call("state", seat="Greece")["defeated"]
    res = b.call("end_turn", seat="Rome", skip_idle=True)["seats"]["Rome"]
    victory = {"kind": "conquest", "civ": "Rome", "label": "A", "turn": 1}
    won = "Rome (A) won by conquest: it is the last civilization left."
    assert res["game_over"] and res["events"][-1] == {"turn": 0, "kind": "victory", "text": won}
    assert b.call("state")["victory"] == victory and b.call("score")["victory"] == victory
    assert "Rome (A) won by conquest on turn 1" in b.error("end_turn", seat="Rome", skip_idle=True)["message"]
    restored = launch()
    restored.call("load", path=str(tmp_path / "a" / "autosave.json"))
    assert restored.call("state", seat="Greece")["victory"] == victory and restored.call("state")["game_over"]
