"""Playtest several seeds and report against the acceptance gate.

    python playtest/batch.py --seeds 1,2,3 --turns 60 --model sonnet --parallel 3 --budget-usd 25

Writes playtest/runs/<name>/: baselines.json (baseline.py), seed-<n>/ (one run.py run each, with its
own env process and port), report.md and report.json.
Gate: every seed reaches the turn limit with error rate < 15%, no stall or repeat flag, and a score
above the null baseline. Stretch: a score at or above the built-in AI on at least 2/3 of the seeds.
"""
import argparse
import json
import math
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import analyze
import baseline
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


def _fmt(x, spec="") -> str:
    return "–" if x is None else format(x, spec)


def build_report(name: str, runs: list[dict], config: dict) -> tuple[str, dict]:
    n = len(runs)
    passed = sum(m["gate"]["passed"] for m in runs)
    wins = sum(bool(m["gate"]["beats_engine_ai"]) for m in runs)
    need = math.ceil(2 * n / 3)
    meets_config = n >= ACCEPTANCE["seeds"] and config["turns"] >= ACCEPTANCE["turns"]

    def mean(values):
        values = [v for v in values if v is not None]
        return round(sum(values) / len(values), 3) if values else None

    gate = {
        "passed": n > 0 and passed == n,
        "runs_passed": passed,
        "runs": n,
        "stretch_passed": n > 0 and wins >= need,
        "beats_engine_ai": wins,
        "stretch_needs": need,
        "acceptance_config": meets_config,
    }
    aggregate = {
        "mean_score": mean(m["score"] for m in runs),
        "mean_null": mean(m["baselines"].get("null") for m in runs),
        "mean_engine_ai": mean(m["baselines"].get("engine_ai") for m in runs),
        "mean_error_rate": mean(m["agent"]["error_rate"] for m in runs),
        "mean_calls_per_turn": mean(m["calls_per_turn"] for m in runs),
        "total_tool_calls": sum(m["agent"]["tool_calls"] for m in runs),
        "total_cost_usd": round(sum(m["cost_usd"] or 0 for m in runs), 4),
        "mean_cost_per_turn": mean(m["cost_per_turn"] for m in runs),
        "max_wall_seconds": max((m["wall_seconds"] or 0 for m in runs), default=0),
    }
    verdict = "PASS" if gate["passed"] else "FAIL"
    lines = [
        f"# Playtest {name}",
        "",
        f"Model `{config['model']}`, seeds {', '.join(map(str, config['seeds']))}, turn limit {config['turns']}, "
        f"budget ${config['budget_usd']} per run. Created {config['created']}.",
        "",
        f"**Gate: {verdict}** ({passed}/{n} runs pass). **Stretch (≥ built-in AI on {need}/{n}): "
        f"{'PASS' if gate['stretch_passed'] else 'FAIL'}** ({wins}/{n})."
        + ("" if meets_config else f" Note: below the acceptance config of {ACCEPTANCE['seeds']} seeds x "
                                    f"{ACCEPTANCE['turns']} turns."),
        "",
        "| Seed | Gate | Turn | Score | Null | Built-in AI | Cities | Pop | Techs | Calls | Error rate | Max err streak "
        "| Max repeat | Stall turns | No-op end_turns | Nudges | Cost | Time | Failed checks / flags |",
        "|" + "---|" * 19,
    ]
    for m in runs:
        a, met = m["agent"], m["metrics"] or {}
        noop = (m["env_log"] or a)["noop_end_turns"]
        notes = ", ".join(m["gate"]["failed"] + [f for f in m["flags"] if f not in m["gate"]["failed"]])
        lines.append(" | ".join([
            f"| {m['seed']}", "PASS" if m["gate"]["passed"] else "FAIL", f"{_fmt(m['turn'])}/{_fmt(m['turn_limit'])}",
            _fmt(m["score"]), _fmt(m["baselines"].get("null")), _fmt(m["baselines"].get("engine_ai")),
            _fmt(met.get("cities")), _fmt(met.get("pop")), _fmt(met.get("techs")), str(a["tool_calls"]),
            _fmt(a["error_rate"], ".1%"), str(a["max_consecutive_errors"]), str(a["max_repeated_failures"]),
            str(len((m["env_log"] or a)["stall_turns"])), str(noop), _fmt(m["nudges"]),
            f"${_fmt(m['cost_usd'], '.3f')}", f"{_fmt(m['wall_seconds'], '.0f')}s", (notes or "–") + " |"]))
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
                     f"tools {a['by_tool']}, errors {a['errors_by_tool'] or '{}'}"
                     + (f", harness error: {m['harness_error']}" if m["harness_error"] else ""))
        lines += [f"  - `{s}`" for s in a["error_samples"][:3]]
    return "\n".join(lines) + "\n", {"batch": name, "config": config, "gate": gate, "aggregate": aggregate, "runs": runs}


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
    ap.add_argument("--report-only", action="store_true", help="re-analyze an existing batch")
    baseline.add_arguments(ap)
    args = ap.parse_args()

    name = args.name or datetime.now().strftime("%Y%m%d-%H%M%S")
    batch_dir = (HERE / "runs" / name).resolve()
    batch_dir.mkdir(parents=True, exist_ok=True)
    baselines_file = args.baselines_file.resolve() if args.baselines_file else batch_dir / "baselines.json"
    config = {"model": args.model, "seeds": args.seeds, "turns": args.turns, "budget_usd": args.budget_usd,
              "max_agent_turns": args.max_agent_turns, "scenario": args.scenario,
              "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    if not args.report_only:
        if not args.no_baselines and not args.baselines_file:
            print(f"[batch] baselines {','.join(args.policies)} for seeds {args.seeds}", file=sys.stderr, flush=True)
            ns = argparse.Namespace(**vars(args), url=None, out=baselines_file)
            baselines_file.write_text(json.dumps(baseline.run_baselines(ns), indent=2) + "\n")
        ports = free_ports(len(args.seeds))
        with ThreadPoolExecutor(max(1, args.parallel)) as pool:
            codes = list(pool.map(lambda sp: run_seed(*sp, args, batch_dir, None if args.no_baselines else baselines_file),
                                  zip(args.seeds, ports)))
        config["exit_codes"] = dict(zip(map(str, args.seeds), codes))

    baselines = analyze.load_json(baselines_file) if not args.no_baselines else None
    runs = []
    for seed in args.seeds:
        run_dir = batch_dir / f"seed-{seed}"
        if (run_dir / "meta.json").exists():
            m = analyze.analyze(run_dir, baselines)
            (run_dir / "metrics.json").write_text(json.dumps(m, indent=2) + "\n")
            runs.append(m)
    md, data = build_report(name, runs, config)
    (batch_dir / "report.md").write_text(md)
    (batch_dir / "report.json").write_text(json.dumps(data, indent=2) + "\n")
    print(md)
    print(f"[batch] wrote {batch_dir / 'report.md'}", file=sys.stderr)
    return 0 if data["gate"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
