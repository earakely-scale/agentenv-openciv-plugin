"""Metrics and the acceptance gates for one playtest run directory.

Reads transcript.jsonl (claude stream-json), actions.jsonl (the env's action log, the authoritative
record of what reached the game), summary.json (final data/get), meta.json (written by run.py) and,
optionally, baselines from baseline.py.

    python playtest/analyze.py RUN_DIR [--baselines BASELINES_JSON]

Gate v2 (the gate): the run is valid (no harness or engine failure, no autoplay on the agent's game),
reaches the turn limit undefeated, has an agent error rate below 10% (harness artifacts, i.e. calls to
tool names the client doesn't have and permission denials, are counted separately), has no stall or
repeat flag, and scores strictly above the scripted settler_bot. Gate v1 (round 1, kept for
comparison): valid, limit reached, every failed call below 15%, no stall or repeat, above null.
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

from envctl import POLICIES, RECORDING_FILES, score_total

TOOL_PREFIX = "mcp__openciv3__"
BAR = "settler_bot"
MAX_ERROR_RATE = 0.10
MAX_ERROR_RATE_V1 = 0.15
STALL_CALLS = 25    # calls within one game turn; the env starts suggesting end_turn at this count
REPEAT_FAILS = 3    # the same failing call this many times in one turn; the env flags it too
ERROR_STREAK = 5
DISORDER_TURNS = 10
GAME_CHANGES = ("unit_order", "set_production", "set_rates", "buy")
# Action-log error codes that mean the engine or env failed, not the agent. `engine_restarted` is not one:
# the env restored the game from its autosave, which is flagged but leaves the run valid.
ENV_FAILURES = ("engine_error", "turn_failed", "engine_failed", "bridge_failed", "bridge_down", "timeout",
                "internal_error")
TURN_RE = re.compile(r"\bT(\d+)/(\d+)\b")
FAKE_CALL_RE = re.compile(r"<invoke|<function_calls")


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def load_json(path: Path):
    return json.loads(path.read_text()) if path.exists() else None


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def _is_game_change(tool: str, args: dict) -> bool:
    return tool in GAME_CHANGES or (tool == "research" and bool(args.get("tech")))


def _per_turn(turns: list) -> dict:
    counts = Counter(t for t in turns if t is not None)
    return {"max_calls_in_turn": max(counts.values(), default=0),
            "stall_turns": sorted(t for t, n in counts.items() if n >= STALL_CALLS)}


def transcript_metrics(rows: list[dict], start_turn: int | None = None) -> dict:
    calls, by_id, fake_calls, turn = [], {}, 0, start_turn
    for r in rows:
        content = (r.get("message") or {}).get("content")
        if r.get("type") not in ("assistant", "user") or not isinstance(content, list):
            continue
        for c in content:
            kind = c.get("type")
            if kind == "tool_use":
                name = c.get("name", "")
                call = {"id": c.get("id"), "tool": name.removeprefix(TOOL_PREFIX), "mcp": name.startswith(TOOL_PREFIX),
                        "args": c.get("input") or {}, "turn": turn, "error": None, "text": ""}
                by_id[call["id"]] = call
                calls.append(call)
            elif kind == "tool_result" and c.get("tool_use_id") in by_id:
                call = by_id[c["tool_use_id"]]
                call["error"], call["text"] = bool(c.get("is_error")), _text(c.get("content"))
                if found := TURN_RE.findall(call["text"]):
                    turn = int(found[-1][0])
            elif kind == "text" and FAKE_CALL_RE.search(c.get("text") or ""):
                fake_calls += 1

    results = [r for r in rows if r.get("type") == "result"]
    denied = {d.get("tool_use_id") for r in results for d in r.get("permission_denials") or []}
    for c in calls:
        c["artifact"] = "permission" if c["id"] in denied else "bare_name" if c["error"] and not c["mcp"] else None
    agent = [c for c in calls if not c["artifact"]]
    errors = [c for c in calls if c["error"]]
    agent_errors = [c for c in agent if c["error"]]
    streak = max_streak = 0
    for c in agent:
        streak = streak + 1 if c["error"] else 0
        max_streak = max(max_streak, streak)
    repeats = Counter((c["turn"], c["tool"], json.dumps(c["args"], sort_keys=True)) for c in agent_errors)
    worst = repeats.most_common(1)[0] if repeats else None
    noop = acted = 0
    for c in agent:
        if c["tool"] == "end_turn" and not c["error"]:
            noop += not acted
            acted = False
        elif not c["error"] and _is_game_change(c["tool"], c["args"]):
            acted = True

    inits = [r for r in rows if r.get("type") == "system" and r.get("subtype") == "init"]
    # Each claude session reports cumulative totals, so its last result counts once.
    sessions = list({r.get("session_id"): r for r in results}.values())
    usage = [u for r in sessions for u in (r.get("modelUsage") or {}).values()]
    return {
        "model": next((i.get("model") for i in inits), None),
        "mcp_servers": inits[0].get("mcp_servers") if inits else None,
        "tool_calls": len(calls),
        "errors": len(errors),
        "error_rate": _rate(len(errors), len(calls)),
        "agent_calls": len(agent),
        "agent_errors": len(agent_errors),
        "agent_error_rate": _rate(len(agent_errors), len(agent)),
        "artifacts": dict(Counter(c["artifact"] for c in calls if c["artifact"])),
        "max_consecutive_errors": max_streak,
        "max_repeated_failures": worst[1] if worst else 0,
        "worst_repeated_failure": {"turn": worst[0][0], "tool": worst[0][1], "args": json.loads(worst[0][2])}
        if worst else None,
        "by_tool": dict(Counter(c["tool"] for c in calls).most_common()),
        "errors_by_tool": dict(Counter(c["tool"] for c in agent_errors).most_common()),
        "error_samples": [f'{c["tool"]}({json.dumps(c["args"], sort_keys=True)}): {" ".join(c["text"].split())[:200]}'
                          for c in agent_errors[:5]],
        "end_turns": sum(c["tool"] == "end_turn" for c in calls),
        "blocked_end_turns": sum(c["tool"] == "end_turn" and "END TURN BLOCKED" in c["text"] for c in calls),
        "noop_end_turns": noop,
        **_per_turn([c["turn"] for c in agent]),
        "non_mcp_calls": sum(not c["mcp"] for c in calls),
        "fake_tool_text": fake_calls,
        "permission_denials": sum(len(r.get("permission_denials") or []) for r in results),
        "segments": len(results),
        "terminal_reasons": [r.get("terminal_reason") or r.get("subtype") for r in results],
        "num_turns": sum(r.get("num_turns") or 0 for r in results),
        "cost_usd": round(sum(r.get("total_cost_usd") or 0 for r in sessions), 4) if sessions else None,
        "sessions": len(sessions),
        "api_seconds": round(sum(r.get("duration_api_ms") or 0 for r in results) / 1000, 1),
        "tokens": {k: sum(m.get(k) or 0 for m in usage)
                   for k in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")},
    }


def action_metrics(rows: list[dict]) -> dict:
    """From the env's action log. A no-op end_turn left units needing orders without any game change."""
    noop = acted = blocked = 0
    for r in rows:
        if r.get("tool") == "end_turn" and r.get("ok"):
            blocked += r.get("turns_advanced") == 0
            noop += not acted and (r.get("idle_units") or 0) > 0
            acted = False
        elif r.get("ok") and _is_game_change(r.get("tool"), r.get("args") or {}):
            acted = True
    failed = [r for r in rows if not r.get("ok")]
    ended = [r for r in rows if r.get("tool") == "end_turn"]
    return {
        "calls": len(rows),
        "invalid": len(failed),
        "error_rate": _rate(len(failed), len(rows)),
        "error_codes": dict(Counter(r.get("error_code") for r in failed).most_common()),
        "env_failures": sum(r.get("error_code") in ENV_FAILURES for r in failed),
        "end_turns": len(ended),
        "blocked_end_turns": blocked,
        "noop_end_turns": noop,
        "idle_units_skipped": sum(r.get("idle_units") or 0 for r in ended if r.get("turns_advanced")),
        "turns_advanced": sum(r.get("turns_advanced") or 0 for r in ended),
        "mean_ms": round(sum(r.get("ms") or 0 for r in rows) / len(rows), 1) if rows else None,
        **_per_turn([r.get("turn") for r in rows]),
    }


def baseline_scores(summary: dict, baselines: dict | None, seed, turn_limit) -> dict:
    """Baseline scores at the turn limit: baseline.py's when it ran this seed and limit, else the env's own."""
    out = {p: s for p, e in (summary.get("baselines") or {}).items()
           if (s := score_total(e.get("final") if isinstance(e, dict) else e)) is not None}
    if baselines and baselines.get("turn_limit") in (None, turn_limit):
        for policy, r in ((baselines.get("seeds") or {}).get(str(seed)) or {}).items():
            if isinstance(r, dict) and not r.get("error") and (s := score_total(r.get("score"))) is not None:
                out[policy] = s
    return dict(sorted(out.items(), key=lambda kv: POLICIES.index(kv[0]) if kv[0] in POLICIES else len(POLICIES)))


def decision_share(summary: dict) -> dict | None:
    """Who chose the completed items and learned techs: data/get `decisions`, plus the agent's share."""
    decisions = summary.get("decisions")
    if not isinstance(decisions, dict):
        return None
    out = {}
    for kind in ("production", "research"):
        d = decisions.get(kind) or {}
        agent, engine = d.get("agent") or 0, d.get("engine") or 0
        out[kind] = {"agent": agent, "engine": engine, "agent_share": _rate(agent, agent + engine)}
    return out


def _metric(summary: dict, key: str):
    return (summary.get("metrics") or {}).get(key, summary.get(key))


def analyze(run_dir, baselines: dict | None = None) -> dict:
    run = Path(run_dir)
    meta = load_json(run / "meta.json") or {}
    summary = load_json(run / "summary.json") or {}
    if baselines is None and meta.get("baselines_file") and Path(meta["baselines_file"]).exists():
        baselines = load_json(Path(meta["baselines_file"]))
    t = transcript_metrics(load_jsonl(run / "transcript.jsonl"), meta.get("start_turn"))
    actions = load_jsonl(run / "actions.jsonl")
    a = action_metrics(actions) if actions else None

    seed = summary.get("seed", meta.get("seed"))
    turn, limit = summary.get("turn"), summary.get("turn_limit", meta.get("turn_limit"))
    score = score_total(summary.get("score"))
    base = baseline_scores(summary, baselines, seed, limit)
    turns_played = (turn - meta["start_turn"]) if turn is not None and meta.get("start_turn") is not None else None
    stall_turns = (a or t)["stall_turns"]
    noop = (a or t)["noop_end_turns"]
    harness = summary.get("harness") if isinstance(summary.get("harness"), dict) else None
    autoplay_turns = (harness or {}).get("autoplay_turns")
    disorder = _metric(summary, "disorder_city_turns")
    env_failed = bool((a or {}).get("env_failures") or summary.get("engine_failed"))

    common = {
        "valid_run": not meta.get("harness_error") and not env_failed,
        "reached_turn_limit": bool(summary.get("game_over")) and not summary.get("defeated")
                              and turn is not None and limit is not None and turn >= limit,
        "no_stall": not stall_turns and meta.get("stop") != "stalled",
        "no_repeat": t["max_repeated_failures"] < REPEAT_FAILS,
    }
    checks = {
        **common,
        "no_autoplay": not autoplay_turns,
        "error_rate": t["agent_error_rate"] is not None and t["agent_error_rate"] < MAX_ERROR_RATE,
        f"beats_{BAR}": score is not None and base.get(BAR) is not None and score > base[BAR],
    }
    checks_v1 = {
        **common,
        "error_rate": t["error_rate"] is not None and t["error_rate"] < MAX_ERROR_RATE_V1,
        "beats_null": score is not None and base.get("null") is not None and score > base["null"],
    }
    flags = [name for name, on in [
        ("harness_error", bool(meta.get("harness_error"))),
        ("env_failure", env_failed),
        ("engine_restarted", bool((harness or {}).get("engine_restarts"))),
        ("autoplay", bool(autoplay_turns)),
        ("stall", not checks["no_stall"]),
        ("repeat", not checks["no_repeat"]),
        ("error_streak", t["max_consecutive_errors"] >= ERROR_STREAK),
        ("passive", bool(turns_played) and noop > turns_played / 2),
        ("disorder", (disorder or 0) >= DISORDER_TURNS),
        ("defeated", bool(summary.get("defeated"))),
        ("budget_exhausted", "budget_exhausted" in t["terminal_reasons"] or meta.get("stop") == "budget"),
        ("fake_tool_text", t["fake_tool_text"] > 0),
        ("harness_artifacts", bool(t["artifacts"])),
    ] if on]
    engine_ai = base.get("engine_ai")
    return {
        "run_dir": str(run),
        "seed": seed,
        "model": t["model"] or meta.get("model"),
        "stop": meta.get("stop"),
        "harness_error": meta.get("harness_error"),
        "turn": turn,
        "turn_limit": limit,
        "turns_played": turns_played,
        "game_over": summary.get("game_over"),
        "defeated": summary.get("defeated"),
        "score": score,
        "score_components": summary.get("score") if isinstance(summary.get("score"), dict) else None,
        "metrics": summary.get("metrics"),
        "baselines": base,
        "margins": {p: score - s for p, s in base.items()} if score is not None else {},
        "decisions": decision_share(summary),
        "harness": harness,
        "disorder_city_turns": disorder,
        "shields_lost": _metric(summary, "shields_lost"),
        "recording": [f for f in RECORDING_FILES.values() if (run / f).exists()],
        "nudges": meta.get("nudges"),
        "sessions": t["sessions"],
        "wall_seconds": meta.get("wall_seconds"),
        "cost_usd": t["cost_usd"],
        "cost_per_turn": round(t["cost_usd"] / turns_played, 4) if t["cost_usd"] and turns_played else None,
        "calls_per_turn": round(t["tool_calls"] / turns_played, 2) if turns_played else None,
        "agent": t,
        "env_log": a,
        "flags": flags,
        "gate": {"passed": all(checks.values()), "checks": checks, "failed": [k for k, ok in checks.items() if not ok]},
        "gate_v1": {
            "passed": all(checks_v1.values()),
            "checks": checks_v1,
            "failed": [k for k, ok in checks_v1.items() if not ok],
            "beats_engine_ai": None if score is None or engine_ai is None else score >= engine_ai,
        },
    }


def fmt(x, spec="") -> str:
    return "–" if x is None else format(x, spec)


def share(m: dict, kind: str) -> str:
    d = (m.get("decisions") or {}).get(kind)
    return f"{fmt(d['agent_share'], '.0%')} ({d['agent']}/{d['agent'] + d['engine']})" if d else "–"


def summary_line(m: dict) -> str:
    a = m["agent"]
    base = " ".join(f"{k}={fmt(v)}" for k, v in m["baselines"].items()) or "none"
    verdict = "PASS" if m["gate"]["passed"] else "FAIL(" + ",".join(m["gate"]["failed"]) + ")"
    artifacts = sum(a["artifacts"].values())
    return (f"seed {m['seed']}: {verdict} (v1 {'PASS' if m['gate_v1']['passed'] else 'FAIL'}) "
            f"T{fmt(m['turn'])}/{fmt(m['turn_limit'])} score {fmt(m['score'])} (baselines {base}) "
            f"calls {a['tool_calls']} agent err {fmt(a['agent_error_rate'], '.1%')}"
            + (f" (+{artifacts} harness artifacts)" if artifacts else "")
            + f" agent picks: production {share(m, 'production')}, research {share(m, 'research')}"
            + f" nudges {fmt(m['nudges'])} ${fmt(m['cost_usd'], '.3f')} {fmt(m['wall_seconds'], '.0f')}s"
            + (f" flags {','.join(m['flags'])}" if m["flags"] else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--baselines", type=Path, help="baselines JSON written by baseline.py")
    ap.add_argument("--quiet", action="store_true", help="print only the summary line")
    args = ap.parse_args()
    m = analyze(args.run_dir, load_json(args.baselines) if args.baselines else None)
    (args.run_dir / "metrics.json").write_text(json.dumps(m, indent=2) + "\n")
    if not args.quiet:
        print(json.dumps(m, indent=2))
    print(summary_line(m), file=sys.stderr)
    return 0 if m["gate"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
