"""Offline tests for the harness: no LLM, no engine. Run with `pytest playtest`."""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import analyze
import batch
import replay
from envctl import Env, free_ports
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

HERE = Path(__file__).resolve().parent
STUB = f"{sys.executable} {HERE / 'stub_env.py'}"

FAKE_CLAUDE = """#!{python}
import json, os, sys
status = os.environ.get("FAKE_MCP_STATUS", "connected")
servers = [{{"name": "openciv3", "status": status}}]
tools = ["mcp__openciv3__end_turn"] if status == "connected" else []
print(json.dumps({{"type": "system", "subtype": "init", "model": "fake", "mcp_servers": servers, "tools": tools}}),
      flush=True)
session = sys.argv[sys.argv.index("--session-id") + 1]
context = int(os.environ.get("FAKE_CONTEXT", "0"))
for n, line in enumerate(sys.stdin, 1):
    usage = {{"input_tokens": 2, "cache_read_input_tokens": context, "cache_creation_input_tokens": 100}}
    print(json.dumps({{"type": "assistant", "message": {{"content": [], "usage": usage}}}}), flush=True)
    result = {{"type": "result", "subtype": "success", "terminal_reason": "completed", "total_cost_usd": n / 1000,
              "session_id": session}}
    print(json.dumps(result), flush=True)
"""
INIT = {"type": "system", "subtype": "init", "model": "m", "mcp_servers": [{"name": "openciv3", "status": "connected"}]}


def _call(i, name, args, text, error=False, prefix="mcp__openciv3__"):
    use = {"type": "tool_use", "id": f"t{i}", "name": prefix + name, "input": args}
    result = {"type": "tool_result", "tool_use_id": f"t{i}", "is_error": error,
              "content": [{"type": "text", "text": text}]}
    return [{"type": "assistant", "message": {"content": [use]}}, {"type": "user", "message": {"content": [result]}}]


def _write(path: Path, rows) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _run_dir(path: Path, transcript: list, summary: dict, actions=(), **meta) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _write(path / "transcript.jsonl", transcript)
    _write(path / "actions.jsonl", actions)
    (path / "meta.json").write_text(json.dumps({"start_turn": 0, "stop": "game_over", "nudges": 0, **meta}))
    (path / "summary.json").write_text(json.dumps(summary))
    return path


def _summary(score, autoplay_turns=0, **baselines):
    return {"turn": 10, "turn_limit": 10, "game_over": True, "seed": 1, "score": {"total": score},
            "baselines": {p: {"final": {"total": s}} for p, s in baselines.items()},
            "decisions": {"production": {"agent": 3, "engine": 1}, "research": {"agent": 0, "engine": 2}},
            "harness": {"autoplay_turns": autoplay_turns, "new_games": 1, "extension_calls": 0}}


def _clean_game(errors: int = 1) -> list:
    """20 calls over turns 0-9: one bare tool name, one permission denial, `errors` agent errors."""
    rows = [INIT, *_call(0, "list_units", {}, "No such tool available: list_units", error=True, prefix="")]
    rows += _call(1, "get_turn_brief", {}, "denied", error=True)
    for i in range(2, 20):
        rows += _call(i, "end_turn", {}, f"Ended.\n[T{i // 2}/10 · needs orders: none]", error=i < 2 + errors)
    return rows + [{"type": "result", "subtype": "success", "total_cost_usd": 0.1,
                    "permission_denials": [{"tool_name": "mcp__openciv3__get_turn_brief", "tool_use_id": "t1"}]}]


def test_analyze_flags_errors_repeats_and_stalls(tmp_path):
    bad = {"unit": "u1", "order": "found_city"}
    rows = [INIT, *_call(0, "get_turn_brief", {}, "T1/5 · Rome")]
    for i in range(1, 4):
        rows += _call(i, "unit_order", bad, "Error executing tool unit_order: cannot found\n[T1/5 · needs orders: u1]",
                      error=True)
    rows += _call(4, "end_turn", {"skip_idle": True}, "Ended T1.\n[T2/5 · needs orders: none]")
    rows += [{"type": "result", "subtype": "success", "terminal_reason": "completed", "total_cost_usd": 0.5}]
    summary = {"turn": 5, "turn_limit": 5, "game_over": True, "seed": 1, "score": {"total": 30},
               "baselines": {"null": {"final": 4}}}
    run = _run_dir(tmp_path, rows, summary, [{"turn": 2, "tool": "view_map", "args": {}, "ok": True}] * 25,
                   start_turn=1)
    m = analyze.analyze(run)
    assert m["agent"]["error_rate"] == 0.6 and m["agent"]["agent_error_rate"] == 0.6
    assert m["agent"]["max_repeated_failures"] == 3
    assert m["env_log"]["stall_turns"] == [2]
    assert m["baselines"] == {"null": 4} and m["margins"] == {"null": 26}
    assert set(m["gate_v1"]["failed"]) == {"error_rate", "no_stall", "no_repeat"}
    assert set(m["gate"]["failed"]) == {"error_rate", "no_stall", "no_repeat", "beats_settler_bot"}
    assert m["gate_v1"]["beats_engine_ai"] is None


def test_gate_v2_counts_harness_artifacts_apart_and_needs_the_scripted_bar(tmp_path):
    m = analyze.analyze(_run_dir(tmp_path / "a", _clean_game(), _summary(50, null=8, settler_bot=40, engine_ai=60)))
    a = m["agent"]
    assert a["artifacts"] == {"bare_name": 1, "permission": 1}
    assert (a["tool_calls"], a["errors"], a["error_rate"]) == (20, 3, 0.15)
    assert (a["agent_calls"], a["agent_errors"], a["agent_error_rate"]) == (18, 1, 0.0556)
    assert m["gate"]["passed"] and not m["gate_v1"]["passed"] and m["gate_v1"]["failed"] == ["error_rate"]
    assert m["margins"] == {"null": 42, "settler_bot": 10, "engine_ai": -10}
    assert m["decisions"]["production"]["agent_share"] == 0.75 and m["decisions"]["research"]["agent_share"] == 0
    assert "harness_artifacts" in m["flags"]

    tie = analyze.analyze(_run_dir(tmp_path / "b", _clean_game(), _summary(40, settler_bot=40)))
    assert tie["gate"]["failed"] == ["beats_settler_bot"]
    autoplayed = analyze.analyze(_run_dir(tmp_path / "c", _clean_game(), _summary(50, 5, settler_bot=40)))
    assert autoplayed["gate"]["failed"] == ["no_autoplay"] and "autoplay" in autoplayed["flags"]
    sloppy = analyze.analyze(_run_dir(tmp_path / "d", _clean_game(errors=2), _summary(50, settler_bot=40)))
    assert sloppy["agent"]["agent_error_rate"] == 0.1111 and sloppy["gate"]["failed"] == ["error_rate"]
    crashed = [{"turn": 3, "tool": "end_turn", "args": {}, "ok": False, "error_code": "bridge_down"}]
    broken = analyze.analyze(_run_dir(tmp_path / "e", _clean_game(), _summary(50, settler_bot=40), crashed))
    assert broken["gate"]["failed"] == ["valid_run"] and "env_failure" in broken["flags"]


def test_batch_passes_when_two_thirds_of_the_runs_pass(tmp_path):
    def runs(*scores):
        out = []
        for seed, score in enumerate(scores, 1):
            run = _run_dir(tmp_path / f"{len(scores)}{scores}" / f"seed-{seed}", _clean_game(),
                           _summary(score, null=8, settler_bot=40))
            (run / "recording.mp4").write_bytes(b"x")
            out.append(analyze.analyze(run))
        return out

    config = {"model": "m", "seeds": [1, 2, 3], "turns": 60, "budget_usd": 1, "created": "now"}
    md, data = batch.build_report("b", runs(50, 41, 40), config)
    assert data["gate"]["passed"] and data["gate"]["runs_passed"] == 2 and data["gate"]["needs"] == 2
    assert not data["gate"]["v1"]["passed"]
    assert "**Gate v2: PASS**" in md and "[mp4](seed-1/recording.mp4)" in md
    _, data = batch.build_report("b", runs(50, 40, 30), config)
    assert not data["gate"]["passed"]


def test_replay_expands_end_turns_and_skips_failed_and_read_only_calls(tmp_path):
    rows = [{"tool": "unit_order", "args": {"unit": "u1", "order": "found_city"}, "ok": True},
            {"tool": "set_production", "args": {"city": "c1", "item": "Spaceship"}, "ok": False},
            {"tool": "research", "args": {}, "ok": True},
            {"tool": "plan", "args": {"text": "grow"}, "ok": True},
            {"tool": "end_turn", "args": {"until_attention": True}, "ok": True, "turns_advanced": 3}]
    assert [r["tool"] for r in rows if replay.replayable(r)] == ["unit_order", "end_turn"]
    assert replay.steps(rows[4]) == [("end_turn", {"skip_idle": True})] * 3
    run = tmp_path / "batch" / "seed-4"
    run.mkdir(parents=True)
    (run.parent / "report.json").write_text(json.dumps({"config": {"scenario": {"size": "Small"}}}))
    assert replay.scenario_of(run) == {"size": "Small"}
    (run / "meta.json").write_text(json.dumps({"scenario": {}}))
    assert replay.scenario_of(run) == {}


async def _play(url: str, calls: list) -> None:
    async with streamable_http_client(url) as (read, write, _), ClientSession(read, write) as session:
        await session.initialize()
        for tool, args in calls:
            await session.call_tool(tool, args)


def test_replay_reproduces_a_game_and_refuses_a_different_one(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    port = free_ports()[0]
    env = Env.start(STUB, port, {"OPENCIV_SEED": 1, "OPENCIV_TURN_LIMIT": 9,
                                 "OPENCIV_ACTION_LOG": run / "actions.jsonl"}, tmp_path / "server.log")
    try:
        env.wait_ready(60)
        asyncio.run(_play(env.mcp_url, [
            ("unit_order", {"unit": "u1", "order": "found_city"}),
            ("set_production", {"city": "c1", "item": "Spaceship"}),
            ("set_production", {"city": "c1", "item": "Warrior"}),
            ("research", {"tech": "Pottery"}),
            ("unit_order", {"unit": "u2", "order": "explore"}),
            ("end_turn", {"until_attention": True, "max_turns": 6}),
            ("set_rates", {"science": 7}),
            ("list_units", {}),
            ("end_turn", {"skip_idle": True, "until_attention": True, "max_turns": 20}),
            *[("end_turn", {"skip_idle": True})] * 6,
        ]))
        summary = env.summary()
    finally:
        env.stop()
    assert summary["game_over"] and summary["decisions"]["production"]["engine"] > 0
    (run / "summary.json").write_text(json.dumps(summary))
    (run / "meta.json").write_text(json.dumps({"seed": 1, "turn_limit": 9, "scenario": {}}))

    def replay_run():
        return subprocess.run([sys.executable, str(HERE / "replay.py"), str(run), "--server-cmd", STUB],
                              capture_output=True, text=True, timeout=120)

    proc = replay_run()
    assert proc.returncode == 0, proc.stderr
    assert f"score {summary['score']['total']}" in proc.stdout
    assert "set_rates" in (run / "replay.html").read_text()
    assert (run / "recording.mp4").exists()

    for f in ("recording.mp4", "replay.html"):
        (run / f).unlink()
    summary["score"]["total"] += 1
    (run / "summary.json").write_text(json.dumps(summary))
    proc = replay_run()
    assert proc.returncode == 1 and "REPLAY MISMATCH" in proc.stderr
    assert not (run / "replay.html").exists()


def _run(tmp_path, *extra, **env):
    fake = tmp_path / "claude"
    fake.write_text(FAKE_CLAUDE.format(python=sys.executable))
    fake.chmod(0o755)
    run_dir = tmp_path / "run"
    proc = subprocess.run([sys.executable, str(HERE / "run.py"), "--turns", "4", "--run-dir", str(run_dir),
                           "--claude", str(fake), "--server-cmd", STUB, *extra],
                          capture_output=True, text=True, timeout=120, env={**os.environ, **env})
    return proc, json.loads((run_dir / "meta.json").read_text()), run_dir


def test_run_stops_when_the_game_stalls_and_saves_the_recording(tmp_path):
    proc, meta, run_dir = _run(tmp_path)
    assert proc.returncode == 1, proc.stderr
    assert meta["stop"] == "stalled" and meta["nudges"] == 2
    assert json.loads((run_dir / "summary.json").read_text())["turn"] == 1
    assert meta["recording"] == {"files": ["recording.mp4", "replay.html"]}
    assert (run_dir / "replay.html").read_text().startswith("<!doctype html>")


def test_run_starts_a_new_session_when_the_context_passes_the_cap(tmp_path):
    proc, meta, run_dir = _run(tmp_path, "--context-cap", "50000", FAKE_CONTEXT="60000")
    assert proc.returncode == 1, proc.stderr
    assert [s["end"] for s in meta["sessions"]] == ["context", "context", "stalled"] and meta["nudges"] == 0
    assert len({s["session_id"] for s in meta["sessions"]}) == 3 and meta["cost_usd"] == 0.003
    metrics = json.loads((run_dir / "metrics.json").read_text())
    assert metrics["sessions"] == 3 and metrics["cost_usd"] == 0.003


def test_run_fails_as_harness_error_when_mcp_is_not_connected(tmp_path):
    proc, meta, _ = _run(tmp_path, FAKE_MCP_STATUS="failed")
    assert proc.returncode == 2, proc.stderr
    assert "not connected" in meta["harness_error"]
