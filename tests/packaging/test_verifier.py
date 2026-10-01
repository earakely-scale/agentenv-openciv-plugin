"""The bundle's outcome verifier, scored the way the env_outcome_verifier step scores it."""

import importlib.util
from importlib.resources import as_file, files

import pytest
from agent_env.task_step.task_steps.verifiers.scoring import ScoreAggregator, aggregate_score

with as_file(files("agentenv_openciv3.bundles").joinpath("openciv3/artifacts/outcome-verifier/verify.py")) as path:
    spec = importlib.util.spec_from_file_location("outcome_verifier", path)
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)


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
