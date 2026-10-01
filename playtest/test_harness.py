"""Offline tests for the harness: no LLM, no engine. Run with `pytest playtest` (the stub needs mcp)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import analyze

HERE = Path(__file__).resolve().parent

FAKE_CLAUDE = """#!{python}
import json, os, sys
status = os.environ.get("FAKE_MCP_STATUS", "connected")
print(json.dumps({{"type": "system", "subtype": "init", "model": "fake", "mcp_servers": [{{"name": "openciv3", "status": status}}],
                  "tools": ["mcp__openciv3__end_turn"] if status == "connected" else []}}), flush=True)
for n, line in enumerate(sys.stdin, 1):
    print(json.dumps({{"type": "result", "subtype": "success", "terminal_reason": "completed", "total_cost_usd": n / 1000}}), flush=True)
"""


def _call(i, name, args, text, error=False):
    return [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"t{i}", "name": f"mcp__openciv3__{name}", "input": args}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}", "is_error": error,
                                                      "content": [{"type": "text", "text": text}]}]}}]


def _write(path: Path, rows) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_analyze_flags_errors_repeats_and_stalls(tmp_path):
    bad = {"unit": "u1", "order": "found_city"}
    rows = [{"type": "system", "subtype": "init", "model": "m", "mcp_servers": [{"name": "openciv3", "status": "connected"}]}]
    rows += _call(0, "get_turn_brief", {}, "T1/5 · Rome")
    for i in range(1, 4):
        rows += _call(i, "unit_order", bad, "Error executing tool unit_order: cannot found\n[T1/5 · needs orders: u1]", error=True)
    rows += _call(4, "end_turn", {"skip_idle": True}, "Ended T1.\n[T2/5 · needs orders: none]")
    rows += [{"type": "result", "subtype": "success", "terminal_reason": "completed", "total_cost_usd": 0.5}]
    _write(tmp_path / "transcript.jsonl", rows)
    _write(tmp_path / "actions.jsonl", [{"turn": 2, "tool": "view_map", "args": {}, "ok": True}] * 25)
    (tmp_path / "meta.json").write_text(json.dumps({"start_turn": 1, "stop": "game_over", "nudges": 0}))
    (tmp_path / "summary.json").write_text(json.dumps({"turn": 5, "turn_limit": 5, "game_over": True, "seed": 1,
                                                       "score": {"total": 30}, "baselines": {"null": {"final": 4}}}))
    m = analyze.analyze(tmp_path)
    assert m["agent"]["error_rate"] == 0.6
    assert m["agent"]["max_repeated_failures"] == 3
    assert m["env_log"]["stall_turns"] == [2]
    assert m["baselines"] == {"null": 4} and m["vs_null"] == 26
    assert set(m["gate"]["failed"]) == {"error_rate", "no_stall", "no_repeat"}
    assert m["gate"]["beats_engine_ai"] is None


def _run(tmp_path, *extra, **env):
    pytest.importorskip("mcp")
    fake = tmp_path / "claude"
    fake.write_text(FAKE_CLAUDE.format(python=sys.executable))
    fake.chmod(0o755)
    run_dir = tmp_path / "run"
    proc = subprocess.run([sys.executable, str(HERE / "run.py"), "--turns", "4", "--run-dir", str(run_dir),
                           "--claude", str(fake), "--server-cmd", f"{sys.executable} {HERE / 'stub_env.py'}", *extra],
                          capture_output=True, text=True, timeout=120, env={**os.environ, **env})
    return proc, json.loads((run_dir / "meta.json").read_text()), run_dir


def test_run_stops_when_the_game_stalls(tmp_path):
    proc, meta, run_dir = _run(tmp_path)
    assert proc.returncode == 1, proc.stderr
    assert meta["stop"] == "stalled" and meta["nudges"] == 2
    assert json.loads((run_dir / "summary.json").read_text())["turn"] == 1


def test_run_fails_as_harness_error_when_mcp_is_not_connected(tmp_path):
    proc, meta, _ = _run(tmp_path, FAKE_MCP_STATUS="failed")
    assert proc.returncode == 2, proc.stderr
    assert "not connected" in meta["harness_error"]
