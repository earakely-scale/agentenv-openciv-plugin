"""Rebuild the recording of a playtest by replaying its action log on a fresh env (docs/recording.md §4).

    python playtest/replay.py playtest/runs/<batch>/seed-1 [more run or batch dirs] [--server-cmd ...]

The engine is deterministic, so replaying the run's successful game-changing calls (unit_order,
set_production, research with a tech, set_rates, buy, end_turn) in order, through the env's MCP tools,
on a new game with the run's seed and scenario reproduces the game. end_turn is replayed as single
turns with skip_idle=true, as many as the original advanced, so blockers and attention stops added
since the run was played cannot change what happened. The turn is checked before every replayed call
and the final turn and score against summary.json; on any difference the replay stops, writes
nothing and exits 1. Otherwise the env's recording extension writes recording.mp4 (or .gif) and
replay.html into the run directory. Needs the env's dependencies (the mcp client).
"""
import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from analyze import load_json, load_jsonl
from envctl import DEFAULT_SERVER_CMD, Env, HarnessError, free_ports, save_recording
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

REPLAYED = ("unit_order", "set_production", "research", "set_rates", "buy", "end_turn")
SCORE_KEYS = ("total", "cities", "pop", "tiles", "techs")


class Mismatch(Exception):
    """The replay diverged from the recorded game."""


def replayable(row: dict) -> bool:
    tool, args = row.get("tool"), row.get("args") or {}
    return bool(row.get("ok")) and tool in REPLAYED and (tool != "research" or bool(args.get("tech")))


def steps(row: dict) -> list[tuple[str, dict]]:
    """The tool calls that redo one logged call."""
    if row["tool"] == "end_turn":
        return [("end_turn", {"skip_idle": True})] * (row.get("turns_advanced") or 0)
    return [(row["tool"], row.get("args") or {})]


def scenario_of(run: Path) -> dict:
    """new_game args beyond seed and turn limit: run.py records them in meta.json; older runs, in the batch."""
    meta = load_json(run / "meta.json") or {}
    if "scenario" in meta:
        return meta["scenario"] or {}
    config = (load_json(run.parent / "report.json") or {}).get("config") or {}
    return config.get("scenario") or (load_json(run.parent / "baselines.json") or {}).get("scenario") or {}


def _score(summary: dict) -> tuple:
    return summary.get("turn"), *((summary.get("score") or {}).get(k) for k in SCORE_KEYS)


async def _calls(session: ClientSession, env: Env, rows: list[dict]) -> int:
    made = 0
    for n, row in enumerate(rows, 1):
        if not replayable(row):
            continue
        turn = (await asyncio.to_thread(env.summary))["turn"]
        if turn != row.get("turn"):
            raise Mismatch(f"action {n} ({row['tool']}) was made at T{row.get('turn')}; the replay is at T{turn}")
        for tool, args in steps(row):
            result = await session.call_tool(tool, args)
            made += 1
            if result.isError:
                text = " ".join(getattr(c, "text", "") for c in result.content)
                raise Mismatch(f"action {n} {tool}({json.dumps(args)}) at T{turn} failed on replay: {text[:500]}")
    return made


async def replay(env: Env, rows: list[dict]) -> int:
    """Replay the logged calls on the env's current game; returns how many tool calls were made."""
    async with streamable_http_client(env.mcp_url) as (read, write, _), ClientSession(read, write) as session:
        await session.initialize()
        try:
            return await _calls(session, env, rows)
        except Mismatch as e:
            mismatch = e    # raised outside the client's task group, which would wrap it in an ExceptionGroup
    raise mismatch


def replay_run(run: Path, args) -> str:
    meta, summary = load_json(run / "meta.json") or {}, load_json(run / "summary.json") or {}
    rows = load_jsonl(run / "actions.jsonl")
    seed, limit = summary.get("seed", meta.get("seed")), summary.get("turn_limit", meta.get("turn_limit"))
    if seed is None or limit is None or not summary.get("score") or not rows:
        raise HarnessError(f"{run}: needs summary.json (seed, turn_limit, score) and actions.jsonl")
    scenario = {"seed": seed, "turn_limit": limit, **scenario_of(run), **args.scenario}
    with tempfile.TemporaryDirectory() as tmp:
        port = free_ports()[0]
        env_vars = {"OPENCIV_SEED": seed, "OPENCIV_TURN_LIMIT": limit, "OPENCIV_BASELINES": 0, "OPENCIV_RECORD": 1,
                    **dict(kv.split("=", 1) for kv in args.env)}
        cmd = args.server_cmd.format(port=port, seed=seed, turns=limit, run_dir=tmp)
        env = Env.start(cmd, port, env_vars, Path(tmp) / "server.log")
        try:
            env.wait_ready()
            env.new_game(scenario)
            made = asyncio.run(replay(env, rows))
            final = env.summary()
            if _score(final) != _score(summary):
                raise Mismatch(f"replay ended at T{final.get('turn')} with score {final.get('score')}; "
                               f"summary.json has T{summary.get('turn')} {summary.get('score')}")
            files = [] if args.no_recording else save_recording(env, run, timeout=args.timeout)["files"]
        finally:
            env.stop()
    return (f"{sum(map(replayable, rows))} actions ({made} calls) reproduce T{final['turn']} score "
            f"{final['score']['total']}" + (f"; wrote {', '.join(files)}" if files else ""))


def run_dirs(paths: list[Path]) -> list[Path]:
    """Run directories as given, or each seed-* directory of a batch."""
    out = []
    for p in paths:
        out += [p] if (p / "actions.jsonl").exists() else sorted(p.glob("seed-*"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", type=Path, nargs="+", help="run directories, or batch directories with seed-* runs")
    ap.add_argument("--server-cmd", default=DEFAULT_SERVER_CMD, help="as for run.py; {run_dir} is a temp dir")
    ap.add_argument("--env", action="append", default=[], metavar="KEY=VALUE", help="extra env var for the server")
    ap.add_argument("--scenario", type=json.loads, default={}, help="override new_game args, as JSON")
    ap.add_argument("--timeout", type=float, default=900, help="seconds allowed to render the recording")
    ap.add_argument("--no-recording", action="store_true", help="only check that the replay reproduces the game")
    args = ap.parse_args()
    status = 0
    for run in run_dirs(args.runs):
        try:
            print(f"{run}: {replay_run(run, args)}", flush=True)
        except Mismatch as e:
            print(f"{run}: REPLAY MISMATCH, nothing written: {e}", file=sys.stderr, flush=True)
            status = max(status, 1)
        except HarnessError as e:
            print(f"{run}: HARNESS ERROR: {e}", file=sys.stderr, flush=True)
            status = 2
    return status


if __name__ == "__main__":
    sys.exit(main())
