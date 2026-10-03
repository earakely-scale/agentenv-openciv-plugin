"""The bundle's outcome verifier, scored the way the env_outcome_verifier step scores it."""

import importlib.util
from importlib.resources import as_file, files

import pytest
from agent_env.task_step.task_steps.verifiers.scoring import ScoreAggregator, aggregate_score


def load(artifact: str):
    with as_file(files("agentenv_openciv3.bundles").joinpath(f"openciv3/artifacts/{artifact}/verify.py")) as path:
        spec = importlib.util.spec_from_file_location(artifact.replace("-", "_"), path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


verifier = load("outcome-verifier")
full_game = load("full-game-verifier")
victor_verifier = load("victor-verifier")


def summary(total=100, cities=4, agent_calls=0, autoplay_turns=30, engine_failed=False, **baselines) -> dict:
    baselines = {"null": 8, "engine_ai": 61, "settler_bot": 100} | baselines
    return {
        "turn": 30, "turn_limit": 30, "game_over": True, "defeated": False, "seed": 7, "civ": "Rome",
        "score": {"total": total, "cities": cities, "pop": 6, "tiles": 20, "techs": 3},
        "metrics": {"cities": cities, "pop": 6, "techs": 3, "tiles": 20, "units": 5, "gold": 30, "explored_pct": 9.0},
        "decisions": {"production": {"agent": 0, "engine": 0}, "research": {"agent": 0, "engine": 0}},
        "baselines": {p: {"status": "done", "at_turn": {"total": t}} for p, t in baselines.items()},
        "actions": {"ok": agent_calls, "invalid": 0, "max_consecutive_errors": 0},
        "harness": {"autoplay_turns": autoplay_turns, "new_games": 1, "extension_calls": 2, "engine_restarts": 0},
        "engine_failed": engine_failed,
    }


def full_summary(total=4106, rank=1, land=0.58, pop=0.62, engine_ai=667) -> dict:
    standings = [{"civ": f"AI {i}", "you": False, "defeated": False, "score": 1000 - i} for i in range(7)]
    standings.insert(rank - 1, {"civ": "Rome", "you": True, "defeated": False, "score": total})
    return summary(total=total, agent_calls=1499, autoplay_turns=0, engine_ai=engine_ai) | {
        "turn": 540, "turn_limit": 540, "standings": standings, "share": {"land": land, "pop": pop}}


def score(rows: list[dict]) -> float:
    return aggregate_score(rows, ScoreAggregator.WEIGHTED_AVERAGE)


def test_a_harness_game_that_matches_the_settler_bot_passes():
    assert score(verifier.grade(summary())) == 1.0


def test_an_agent_game_is_graded_against_the_settler_bot_and_reports_every_baseline():
    rows = verifier.grade(summary(total=60, agent_calls=40, autoplay_turns=0))
    assert score(rows) == pytest.approx((3 + 2 * 0.6) / 5)
    assert rows[3]["baselines"] == {"null": 8, "engine_ai": 61, "settler_bot": 100}


def test_turns_the_harness_played_for_an_agent_zero_the_grade():
    rows = verifier.grade(summary(total=150, agent_calls=2, autoplay_turns=29))
    assert rows[-1]["result"] is False
    assert score(rows) == 0.0


def test_a_failed_engine_zeroes_the_grade():
    rows = verifier.grade(summary(engine_failed=True))
    assert [r["criterion"] for r in rows if r["result"] is False] == ["the engine kept running"]
    assert score(rows) == 0.0


def test_a_missing_reference_baseline_fails_that_criterion():
    rows = verifier.grade(summary(settler_bot=None))
    assert (rows[3]["result"], rows[3]["score"]) == (False, 0.0)


@pytest.mark.anyio
async def test_an_env_that_cannot_report_is_a_failed_grade_not_an_error():
    rows = await verifier.verify("http://127.0.0.1:9/mcp")
    assert [r["result"] for r in rows] == [False]
    assert "ConnectError" in rows[0]["error"]
    assert score(rows) == 0.0


def test_a_full_game_is_graded_on_the_engine_ai_its_rank_and_its_share_of_the_world():
    rows = full_game.grade(full_summary())
    assert [r["result"] for r in rows] == [True, True, True, True, False, True, True]
    assert rows[4]["score"] == pytest.approx(0.58 / (2 / 3))
    assert score(rows) == pytest.approx((1 + 1 + 2 + 1 + 0.58 / (2 / 3)) / 6)


def test_a_full_game_behind_the_engine_ai_and_third_scores_partially():
    rows = full_game.grade(full_summary(total=500, rank=3, land=0.1, pop=0.1))
    assert rows[2]["score"] == pytest.approx(500 / 667) and rows[3]["score"] == pytest.approx(5 / 7)
    assert rows[3]["rank"] == 3 and not rows[3]["result"]


def match_summary(turn=300, totals=(700, 900, 400), defeated=(False, False, False), auto_ended=(0, 2, 0),
                  victory=None, engine_failed=False) -> dict:
    def seat(civ, label, total, lost, idle):
        return {"civ": civ, "label": label, "defeated": lost, "score": {"total": total}, "metrics": {"cities": 9},
                "share": {"land": 0.2, "pop": 0.3}, "decisions": {}, "actions": {"ok": 900}, "auto_ended_turns": idle}
    civs = zip(("Rome", "Greece", "Egypt"), ("Opus", "Sonnet", "Haiku"), totals, defeated, auto_ended, strict=True)
    return {"turn": turn, "turn_limit": 300, "game_over": victory is not None or turn >= 300, "victory": victory,
            "seats": [seat(*c) for c in civs], "engine_failed": engine_failed, "harness": {"engine_restarts": 0}}


def test_at_the_turn_limit_the_top_score_wins_the_match():
    rows = victor_verifier.grade(match_summary())
    assert [(r["criterion"], r["result"]) for r in rows] == [
        ("the match has a victor", True),
        ("Opus (Rome): rank 2 of 3", False),
        ("Sonnet (Greece): rank 1 of 3", True),
        ("Haiku (Egypt): rank 3 of 3", False),
        ("the match was played to its end", True),
        ("the engine kept running", True),
        ("every agent played: the env ended at most 10% of any seat's turns", True)]
    assert {k: rows[0][k] for k in ("victor", "civ", "how", "turn", "margin")} == {
        "victor": "Sonnet", "civ": "Greece", "how": "score", "turn": 300, "margin": 200}
    assert [(p["civ"], p["rank"]) for p in rows[0]["standings"]] == [("Greece", 1), ("Rome", 2), ("Egypt", 3)]
    assert [(r["rank"], r["score"], r["game_score"]) for r in rows[1:4]] == [
        (2, 0.5, 700), (1, 1.0, 900), (3, 0.0, 400)]
    assert score(rows) == 1.0


def test_a_conquest_before_the_turn_limit_ends_the_match_with_its_victor():
    conquest = {"kind": "conquest", "civ": "Rome", "label": "Opus", "turn": 120}
    rows = victor_verifier.grade(match_summary(turn=120, defeated=(False, True, True), victory=conquest))
    assert {k: rows[0][k] for k in ("victor", "civ", "how", "turn", "margin")} == {
        "victor": "Opus", "civ": "Rome", "how": "conquest", "turn": 120, "margin": None}
    assert [r["rank"] for r in rows[1:4]] == [1, 2, 3]
    assert rows[4]["result"] is True
    assert score(rows) == 1.0


def test_a_domination_victory_wins_over_a_higher_score():
    domination = {"kind": "domination", "civ": "Egypt", "label": "Haiku", "turn": 250}
    rows = victor_verifier.grade(match_summary(turn=250, totals=(700, 900, 850), victory=domination))
    assert (rows[0]["civ"], rows[0]["how"]) == ("Egypt", "domination")
    assert [(r["rank"], r["result"]) for r in rows[1:4]] == [(3, False), (2, False), (1, True)]
    assert score(rows) == 1.0


def test_a_tie_on_top_score_is_no_victor_and_half_the_grade():
    rows = victor_verifier.grade(match_summary(totals=(900, 900, 400)))
    assert (rows[0]["result"], rows[0]["tied"]) == (False, ["Opus (Rome)", "Sonnet (Greece)"])
    assert [(r["rank"], r["result"]) for r in rows[1:4]] == [(1, False), (1, False), (3, False)]
    assert score(rows) == 0.5


def test_defeated_seats_rank_after_the_undefeated_ones():
    rows = victor_verifier.grade(match_summary(defeated=(False, True, False)))
    assert (rows[0]["civ"], rows[0]["margin"]) == ("Rome", 300)
    assert [r["rank"] for r in rows[1:4]] == [1, 3, 2]


def test_an_unfinished_match_a_failed_engine_or_a_sat_out_agent_does_not_count():
    unfinished = victor_verifier.grade(match_summary(turn=120))
    assert [r["criterion"] for r in unfinished if r["result"] is False and r.get("weight") != 0] == [
        "the match has a victor", "the match was played to its end"]
    assert score(unfinished) == 0.0
    failed = victor_verifier.grade(match_summary(engine_failed=True))
    assert [r["criterion"] for r in failed if r["result"] is False and r.get("weight") != 0] == [
        "the engine kept running"]
    assert score(failed) == 0.0
    sat_out = victor_verifier.grade(match_summary(auto_ended=(0, 31, 0)))
    assert sat_out[-1]["result"] is False and sat_out[-1]["auto_ended_turns"] == {"Rome": 0, "Greece": 31, "Egypt": 0}
    assert score(sat_out) == 0.0


def test_a_human_seat_is_ranked_like_an_agent_but_its_idle_turns_are_not_gated():
    s = match_summary(auto_ended=(0, 2, 60))
    for seat in s["seats"]:
        seat["human"] = seat["civ"] == "Egypt"
    rows = victor_verifier.grade(s)
    assert [(p["civ"], p["human"]) for p in rows[0]["standings"]] == [
        ("Greece", False), ("Rome", False), ("Egypt", True)]
    assert [r["human"] for r in rows[1:4]] == [False, False, True]
    assert (rows[-1]["result"], rows[-1]["auto_ended_turns"], rows[-1]["human_auto_ended_turns"]) == (
        True, {"Rome": 0, "Greece": 2}, {"Egypt": 60})
    assert score(rows) == 1.0
    won = match_summary(totals=(700, 900, 1200))
    won["seats"][2]["human"] = True
    rows = victor_verifier.grade(won)
    assert (rows[0]["victor"], rows[0]["civ"]) == ("Haiku", "Egypt")


@pytest.mark.anyio
async def test_a_victor_verifier_env_that_cannot_report_is_a_failed_grade():
    rows = await victor_verifier.verify("http://127.0.0.1:9/mcp")
    assert [r["result"] for r in rows] == [False]
    assert score(rows) == 0.0
