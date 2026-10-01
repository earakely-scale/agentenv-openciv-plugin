"""Playtest several seeds and report against the acceptance gate.

    python playtest/batch.py --seeds 1,2,3 --turns 60 --model sonnet --parallel 3 --budget-usd 25

Writes playtest/runs/<name>/: baselines.json (baseline.py), seed-<n>/ (one run.py run each, with its
own env process and port, plus its recording), report.md and report.json.
Gate v2: at least 2/3 of the runs pass analyze.py's gate v2, which includes a score above the scripted
settler_bot. Gate v1 (every run above null; stretch: at or above the built-in AI on 2/3) is reported
for comparison.
"""
import argparse
import json
import math
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import analyze
import baseline
from analyze import fmt as _fmt
from envctl import DEFAULT_SERVER_CMD, free_ports

HERE = Path(__file__).resolve().parent
ACCEPTANCE = {"seeds": 3, "turns": 60}
_print_lock = threading.Lock()


def run_seed(seed: int, port: int, args, batch_dir: Path, baselines_file: Path | None) -> int:
    run_dir = batch_dir / f"seed-{seed}"
    run_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(HERE / "run.py"), "--seed", str(seed), "--turns", str(args.turns),
           "--port", str(port), "--run-dir", str(run_dir), "--model", args.model,
           "--budget-usd", str(args.budget_usd), "--max-agent-turns", str(args.max_agent_turns),
           "--max-nudges", str(args.max_nudges), "--max-stalled-nudges", str(args.max_stalled_nudges),
           "--timeout-min", str(args.timeout_min), "--server-cmd", args.server_cmd, "--prompt", str(args.prompt),
           "--claude", args.claude, *[f"--env={e}" for e in args.env], *[f"--claude-arg={c}" for c in args.claude_arg]]
    if args.scenario:
        cmd += ["--scenario", json.dumps(args.scenario)]
    if baselines_file:
        cmd += ["--baselines-file", str(baselines_file)]
    with open(run_dir / "harness.log", "w") as log:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:
            log.write(line)
            with _print_lock:
                print(f"[seed {seed}] {line}", end="", file=sys.stderr, flush=True)
    return proc.wait()


def _mean(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 3) if values else None


def _pooled_share(runs: list[dict], kind: str) -> float | None:
    picks = [(m["decisions"] or {}).get(kind) for m in runs]
    agent, total = sum(d["agent"] for d in picks if d), sum(d["agent"] + d["engine"] for d in picks if d)
    return round(agent / total, 3) if total else None


def _links(m: dict) -> str:
    run = Path(m["run_dir"]).name
    return " · ".join(f"[{Path(f).suffix.lstrip('.')}]({run}/{f})" for f in m["recording"]) or "–"


def build_report(name: str, runs: list[dict], config: dict) -> tuple[str, dict]:
    n = len(runs)
    need = math.ceil(2 * n / 3)
    passed = sum(m["gate"]["passed"] for m in runs)
    passed_v1 = sum(m["gate_v1"]["passed"] for m in runs)
    wins = sum(bool(m["gate_v1"]["beats_engine_ai"]) for m in runs)
    meets_config = n >= ACCEPTANCE["seeds"] and config["turns"] >= ACCEPTANCE["turns"]
    policies = [p for p in analyze.POLICIES if any(p in m["baselines"] for m in runs)]
    gate = {
        "passed": n > 0 and passed >= need,
        "runs_passed": passed,
        "runs": n,
        "needs": need,
        "bar": analyze.BAR,
        "acceptance_config": meets_config,
        "v1": {"passed": n > 0 and passed_v1 == n, "runs_passed": passed_v1,
               "stretch_passed": n > 0 and wins >= need, "beats_engine_ai": wins},
    }
    aggregate = {
        "mean_score": _mean(m["score"] for m in runs),
        **{f"mean_{p}": _mean(m["baselines"].get(p) for m in runs) for p in policies},
        **{f"mean_margin_{p}": _mean(m["margins"].get(p) for m in runs) for p in policies},
        "agent_share_production": _pooled_share(runs, "production"),
        "agent_share_research": _pooled_share(runs, "research"),
        "mean_agent_error_rate": _mean(m["agent"]["agent_error_rate"] for m in runs),
        "harness_artifacts": sum(sum(m["agent"]["artifacts"].values()) for m in runs),
        "mean_calls_per_turn": _mean(m["calls_per_turn"] for m in runs),
        "total_tool_calls": sum(m["agent"]["tool_calls"] for m in runs),
        "total_cost_usd": round(sum(m["cost_usd"] or 0 for m in runs), 4),
        "mean_cost_per_turn": _mean(m["cost_per_turn"] for m in runs),
        "max_wall_seconds": max((m["wall_seconds"] or 0 for m in runs), default=0),
    }
    v1 = gate["v1"]
    scenario = ", ".join(f"{k} {v}" for k, v in (config.get("scenario") or {}).items()) or "defaults"
    lines = [
        f"# Playtest {name}",
        "",
        f"Model `{config['model']}`, seeds {', '.join(map(str, config['seeds']))}, turn limit {config['turns']}, "
        f"scenario {scenario}, budget ${config['budget_usd']} per run. Created {config['created']}.",
        "",
        f"**Gate v2: {'PASS' if gate['passed'] else 'FAIL'}** ({passed}/{n} runs pass, {need} needed; a run must "
        f"beat `{analyze.BAR}`)."
        + ("" if meets_config else f" Note: below the acceptance config of {ACCEPTANCE['seeds']} seeds x "
                                    f"{ACCEPTANCE['turns']} turns."),
        "",
        f"Gate v1, for comparison: {'PASS' if v1['passed'] else 'FAIL'} ({passed_v1}/{n}); stretch (≥ built-in AI on "
        f"{need}/{n}): {'PASS' if v1['stretch_passed'] else 'FAIL'} ({wins}/{n}).",
        "",
        "## Score against the baselines",
        "",
        "| Seed | Gate v2 | v1 | Turn | Score | " + " | ".join(policies) + " | "
        + " | ".join(f"Δ {p}" for p in policies) + " | Cities | Pop | Tiles | Techs |",
        "|" + "---|" * (9 + 2 * len(policies)),
    ]
    for m in runs:
        c = m["score_components"] or {}
        lines.append("| " + " | ".join([
            str(m["seed"]), "PASS" if m["gate"]["passed"] else "FAIL", "PASS" if m["gate_v1"]["passed"] else "FAIL",
            f"{_fmt(m['turn'])}/{_fmt(m['turn_limit'])}", _fmt(m["score"]),
            *[_fmt(m["baselines"].get(p)) for p in policies], *[_fmt(m["margins"].get(p), "+") for p in policies],
            _fmt(c.get("cities")), _fmt(c.get("pop")), _fmt(c.get("tiles")), _fmt(c.get("techs"))]) + " |")
    lines += [
        "",
        "## Play",
        "",
        "Agent error rate excludes harness artifacts (calls to tool names the client doesn't have, permission "
        "denials), counted on their own. Agent picks: the share of completed items and learned techs the agent chose "
        "rather than the engine.",
        "",
        "| Seed | Calls | Agent errors | Artifacts | Max streak | Max repeat | Stall turns | No-op end_turns "
        "| Production by agent | Research by agent | Autoplay turns | Disorder city-turns | Shields lost | Nudges "
        "| Cost | Time | Recording | Failed checks / flags |",
        "|" + "---|" * 18,
    ]
    for m in runs:
        a, log = m["agent"], m["env_log"] or m["agent"]
        notes = ", ".join(m["gate"]["failed"] + [f for f in m["flags"] if f not in m["gate"]["failed"]])
        lines.append("| " + " | ".join([
            str(m["seed"]), str(a["tool_calls"]), f"{a['agent_errors']} ({_fmt(a['agent_error_rate'], '.1%')})",
            ", ".join(f"{k} {v}" for k, v in a["artifacts"].items()) or "0", str(a["max_consecutive_errors"]),
            str(a["max_repeated_failures"]), str(len(log["stall_turns"])), str(log["noop_end_turns"]),
            analyze.share(m, "production"), analyze.share(m, "research"),
            _fmt((m["harness"] or {}).get("autoplay_turns")), _fmt(m["disorder_city_turns"]), _fmt(m["shields_lost"]),
            _fmt(m["nudges"]), f"${_fmt(m['cost_usd'], '.3f')}", f"{_fmt(m['wall_seconds'], '.0f')}s", _links(m),
            notes or "–"]) + " |")
    lines += [
        "",
        "## Aggregate",
        "",
        *[f"- {k.replace('_', ' ')}: {_fmt(v)}" for k, v in aggregate.items()],
        "",
        "## Notes per run",
        "",
    ]
    for m in runs:
        a = m["agent"]
        lines.append(f"- Seed {m['seed']}: stop `{m['stop']}`, segments {a['terminal_reasons']}, "
                     f"tools {a['by_tool']}, agent errors {a['errors_by_tool'] or '{}'}"
                     + (f", harness error: {m['harness_error']}" if m["harness_error"] else ""))
        lines += [f"  - `{s}`" for s in a["error_samples"][:3]]
    data = {"batch": name, "config": config, "gate": gate, "aggregate": aggregate, "runs": runs}
    return "\n".join(lines) + "\n", data


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", type=lambda s: [int(x) for x in s.split(",")], default=[1, 2, 3])
    ap.add_argument("--turns", type=int, default=60)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument("--name", default=None, help="batch directory under playtest/runs (default: a timestamp)")
    ap.add_argument("--budget-usd", type=float, default=25.0, help="per run")
    ap.add_argument("--max-agent-turns", type=int, default=300)
    ap.add_argument("--max-nudges", type=int, default=30)
    ap.add_argument("--max-stalled-nudges", type=int, default=3)
    ap.add_argument("--timeout-min", type=float, default=240)
    ap.add_argument("--server-cmd", default=DEFAULT_SERVER_CMD)
    ap.add_argument("--env", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument("--scenario", type=json.loads, default={})
    ap.add_argument("--prompt", type=Path, default=HERE / "prompt.md")
    ap.add_argument("--claude", default="claude")
    ap.add_argument("--claude-arg", action="append", default=[])
    ap.add_argument("--baselines-file", type=Path, help="reuse baselines from baseline.py instead of computing them")
    ap.add_argument("--no-baselines", action="store_true", help="rely on the baselines the env reports itself")
    ap.add_argument("--report-only", action="store_true",
                    help="re-analyze an existing batch (its seeds, turns and model come from its report.json)")
    baseline.add_arguments(ap)
    args = ap.parse_args()

    name = args.name or datetime.now().strftime("%Y%m%d-%H%M%S")
    batch_dir = (HERE / "runs" / name).resolve()
    batch_dir.mkdir(parents=True, exist_ok=True)
    baselines_file = args.baselines_file.resolve() if args.baselines_file else batch_dir / "baselines.json"
    config = {"model": args.model, "seeds": args.seeds, "turns": args.turns, "budget_usd": args.budget_usd,
              "max_agent_turns": args.max_agent_turns, "scenario": args.scenario,
              "created": datetime.now(UTC).isoformat(timespec="seconds")}
    if args.report_only and (previous := analyze.load_json(batch_dir / "report.json")):
        config = previous["config"]

    if not args.report_only:
        if not args.no_baselines and not args.baselines_file:
            print(f"[batch] baselines {','.join(args.policies)} for seeds {args.seeds}", file=sys.stderr, flush=True)
            ns = argparse.Namespace(**vars(args), url=None, out=baselines_file)
            baselines_file.write_text(json.dumps(baseline.run_baselines(ns), indent=2) + "\n")
        ports = free_ports(len(args.seeds))
        runs_baselines = None if args.no_baselines else baselines_file
        with ThreadPoolExecutor(max(1, args.parallel)) as pool:
            codes = list(pool.map(lambda sp: run_seed(*sp, args, batch_dir, runs_baselines),
                                  zip(args.seeds, ports, strict=True)))
        config["exit_codes"] = dict(zip(map(str, args.seeds), codes, strict=True))

    baselines = analyze.load_json(baselines_file) if not args.no_baselines else None
    runs = []
    for seed in config["seeds"]:
        run_dir = batch_dir / f"seed-{seed}"
        if (run_dir / "meta.json").exists():
            m = analyze.analyze(run_dir, baselines)
            (run_dir / "metrics.json").write_text(json.dumps(m, indent=2) + "\n")
            runs.append(m)
    md, data = build_report(batch_dir.name, runs, config)
    (batch_dir / "report.md").write_text(md)
    (batch_dir / "report.json").write_text(json.dumps(data, indent=2) + "\n")
    print(md)
    print(f"[batch] wrote {batch_dir / 'report.md'}", file=sys.stderr)
    return 0 if data["gate"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
