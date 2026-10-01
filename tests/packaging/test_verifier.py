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
