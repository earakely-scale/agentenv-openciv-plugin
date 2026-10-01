"""Same-seed baselines without an LLM, through the env's autoplay extension.

    python playtest/baseline.py --seeds 1,2,3 --turns 60 --out playtest/runs/baselines.json

Per seed, one env process plays one game per policy (new game, autoplay to the turn limit,
data/get). `null` ends turns holding everything; `found_capital` founds the capital and automates
workers; `settler_bot` follows the env's own suggestions (the bar gate v2 uses); `engine_ai` lets
OpenCiv3's own AI play the seat. `--add` runs only the policies an existing --out file lacks. Output JSON:
{"turn_limit", "scenario", "policies", "seeds": {"<seed>": {"<policy>": {"score", "metrics", ...}}}}.
"""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from envctl import AUTOPLAY_URI, DEFAULT_SERVER_CMD, POLICIES, Env, HarnessError, free_ports, score_total

HERE = Path(__file__).resolve().parent
FIELDS = ("score", "metrics", "turn", "turn_limit", "game_over", "defeated")


def run_seed(seed: int, args, port: int | None, log_dir: Path) -> dict:
    if args.url:
        env = Env(args.url)
    else:
        port = port or free_ports()[0]
        env_vars = {"OPENCIV_SEED": seed, "OPENCIV_TURN_LIMIT": args.turns, "OPENCIV_BASELINES": 0,
                    **dict(kv.split("=", 1) for kv in args.env)}
        cmd = args.server_cmd.format(port=port, seed=seed, turns=args.turns, run_dir=log_dir)
        env = Env.start(cmd, port, env_vars, log_dir / f"baseline-seed{seed}.log")
    scenario = {"seed": seed, "turn_limit": args.turns, **args.scenario} if args.url or args.scenario else None
    out = {}
    try:
        env.wait_ready()
        for policy in args.policies:
            t0 = time.monotonic()
            try:
                env.new_game(scenario)
                s = env.summary()
                if s["turn"] < s["turn_limit"]:
                    env.extension(AUTOPLAY_URI, timeout=args.autoplay_timeout, turns=s["turn_limit"] - s["turn"],
                                  policy=policy)
                s = env.summary()
                out[policy] = {**{k: s.get(k) for k in FIELDS}, "seconds": round(time.monotonic() - t0, 1)}
            except HarnessError as e:
                out[policy] = {"error": str(e)}
    except HarnessError as e:
        out = {p: {"error": str(e)} for p in args.policies}
    finally:
        env.stop()
    return out


def run_baselines(args) -> dict:
    log_dir = (args.out.parent if args.out else HERE / "runs").resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    workers = 1 if args.url else max(1, args.parallel)
    ports = [None] * len(args.seeds) if args.url else free_ports(len(args.seeds))
    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(lambda sp: run_seed(sp[0], args, sp[1], log_dir), zip(args.seeds, ports, strict=True)))
    return {"turn_limit": args.turns, "scenario": args.scenario, "policies": args.policies,
            "seeds": {str(s): r for s, r in zip(args.seeds, results, strict=True)}}


def add_arguments(ap: argparse.ArgumentParser) -> None:
    """Options batch.py shares."""
    ap.add_argument("--policies", type=lambda s: s.split(","), default=list(POLICIES))
    ap.add_argument("--autoplay-timeout", type=float, default=1800, help="seconds allowed for one autoplay call")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", type=lambda s: [int(x) for x in s.split(",")], default=[1, 2, 3])
    ap.add_argument("--turns", type=int, default=60)
    ap.add_argument("--server-cmd", default=DEFAULT_SERVER_CMD)
    ap.add_argument("--url", help="use a running env (seeds run one after another, via data/add)")
    ap.add_argument("--env", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument("--scenario", type=json.loads, default={})
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument("--out", type=Path, help="write the JSON here (default: stdout)")
    ap.add_argument("--add", action="store_true", help="keep --out's results and run only the policies it lacks")
    add_arguments(ap)
    args = ap.parse_args()
    old = json.loads(args.out.read_text()) if args.add and args.out and args.out.exists() else None
    if old:
        if (old.get("turn_limit"), old.get("scenario") or {}) != (args.turns, args.scenario):
            raise SystemExit(f"{args.out} is for turn limit {old.get('turn_limit')} and scenario {old.get('scenario')}")
        have = old.get("seeds") or {}
        args.policies = [p for p in args.policies
                         if any("score" not in (have.get(str(s)) or {}).get(p, {}) for s in args.seeds)]
    result = run_baselines(args) if args.policies else {"policies": [], "seeds": {}}
    if old:
        for seed, runs in result["seeds"].items():
            old["seeds"].setdefault(seed, {}).update(runs)
        old["policies"] = list(dict.fromkeys(old["policies"] + result["policies"]))
        result = old
    text = json.dumps(result, indent=2) + "\n"
    if args.out:
        args.out.write_text(text)
    else:
        print(text, end="")
    failed = 0
    for seed, policies in result["seeds"].items():
        for policy, r in policies.items():
            failed += "error" in r
            outcome = f"ERROR {r['error']}" if "error" in r else \
                f"score {score_total(r['score'])} at T{r['turn']}/{r['turn_limit']} ({r['seconds']}s)"
            print(f"seed {seed} {policy}: {outcome}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
