"""One playtest: start the env, let one long-lived `claude -p` play it over MCP, record, analyze.

    python playtest/run.py --seed 1 --turns 60 --model sonnet --budget-usd 25

The run directory gets transcript.jsonl (claude's stream-json), actions.jsonl (the env's action
log), summary.json (final data/get), meta.json, server.log, claude.stderr, metrics.json and the env's
recording of the game (recording.mp4, or .gif without ffmpeg, and replay.html).
`--server-cmd` may use the placeholders {port}, {seed}, {turns} and {run_dir}, e.g. for Docker:
    --server-cmd "docker run --rm -p 127.0.0.1:{port}:18765 -e OPENCIV_SEED={seed}
                  -e OPENCIV_TURN_LIMIT={turns} -e OPENCIV_ACTION_LOG=/run/actions.jsonl -v {run_dir}:/run openciv3"
Exit status: 0 gate passed, 1 gate failed, 2 harness error.
"""
import argparse
import json
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import analyze
from envctl import (
    DEFAULT_SERVER_CMD,
    RECORDING_FILES,
    Env,
    HarnessError,
    clean_claude_project,
    free_ports,
    kill_group,
    save_recording,
)

HERE = Path(__file__).resolve().parent
MCP_NAME = "openciv3"
NUDGE = "Game not over (turn {turn}/{turn_limit}). Continue playing."


def log(msg: str) -> None:
    print(f"[run {datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def load_prompts(path: Path) -> tuple[str, str]:
    """prompt.md: the text under `## System prompt` and under `## Game prompt`."""
    sections, current = {}, None
    for line in path.read_text().splitlines():
        if line.startswith("## "):
            current = sections.setdefault(line[3:].strip().lower(), [])
        elif current is not None:
            current.append(line)
    try:
        return "\n".join(sections["system prompt"]).strip(), "\n".join(sections["game prompt"]).strip()
    except KeyError as e:
        raise SystemExit(f"{path}: missing section ## {e.args[0].capitalize()}") from None


class _Fields(dict):
    def __missing__(self, key):
        return "?"


def claude_cmd(args, mcp_json: Path, system: str, session_id: str) -> list[str]:
    return [args.claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--mcp-config", str(mcp_json), "--strict-mcp-config", "--tools", "", "--setting-sources", "",
            "--system-prompt", system, "--allowedTools", f"mcp__{MCP_NAME}__*", "--permission-mode", "dontAsk",
            "--model", args.model, "--max-turns", str(args.max_agent_turns),
            "--max-budget-usd", str(args.budget_usd), "--no-session-persistence", "--session-id", session_id,
            *args.claude_arg]


def play(args, env: Env, run_dir: Path, meta: dict) -> None:
    """Drive one claude process: the game prompt, then a nudge after each result while the game goes on."""
    system, game = load_prompts(args.prompt)
    start = env.summary()
    meta.update(start_turn=start.get("turn"), turn_limit=start.get("turn_limit"))
    fields = _Fields(start, seed=start.get("seed", args.seed))
    mcp_json = run_dir / "mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": {MCP_NAME: {"type": "http", "url": env.mcp_url}}}, indent=2) + "\n")

    session_id = meta["session_id"] = str(uuid.uuid4())
    with open(run_dir / "claude.stderr", "w") as err:
        proc = subprocess.Popen(claude_cmd(args, mcp_json, system, session_id), cwd=run_dir, text=True, bufsize=1,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, start_new_session=True)

    def send(text: str) -> None:
        try:
            proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
            proc.stdin.flush()
        except BrokenPipeError:
            pass   # claude already exited; the stdout loop ends and records it

    def timeout() -> None:
        meta["stop"] = "timeout"
        kill_group(proc, grace=5)

    timer = threading.Timer(args.timeout_min * 60, timeout)
    timer.start()
    nudges = stalled = calls = api_errors = 0
    last_turn = start.get("turn")
    finished = False
    try:
        send(game.format_map(fields))
        with open(run_dir / "transcript.jsonl", "w") as out:
            for line in proc.stdout:
                out.write(line)
                out.flush()
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                kind = ev.get("type")
                if kind == "system" and ev.get("subtype") == "init":
                    servers = ev.get("mcp_servers") or []
                    tools = [t for t in ev.get("tools") or [] if t.startswith(f"mcp__{MCP_NAME}__")]
                    if not servers or any(s.get("status") != "connected" for s in servers) or not tools:
                        raise HarnessError(f"MCP server not connected: {servers}, tools {ev.get('tools')}")
                    if nudges == 0:
                        log(f"claude connected: model {ev.get('model')}, {len(tools)} tools")
                elif kind == "assistant":
                    calls += sum(c.get("type") == "tool_use" for c in ev["message"].get("content") or [])
                elif kind == "result":
                    s = env.summary()
                    turn, limit = s.get("turn"), s.get("turn_limit")
                    reason = ev.get("terminal_reason") or ev.get("subtype")
                    # API failures are infrastructure, not the agent: retry them, but never count them as a stall.
                    api_errors = api_errors + 1 if reason == "api_error" else 0
                    if api_errors >= 3:
                        raise HarnessError(f"claude API errors: {ev.get('errors')}")
                    if not api_errors:
                        stalled = stalled + 1 if turn == last_turn else 0
                    last_turn = turn
                    log(f"segment {nudges + 1} ended ({reason}): T{turn}/{limit}, {calls} tool calls, "
                        f"${ev.get('total_cost_usd') or 0:.3f}")
                    if s.get("game_over") or s.get("defeated"):
                        meta["stop"] = "defeated" if s.get("defeated") else "game_over"
                    elif reason == "budget_exhausted" or ev.get("subtype") == "error_max_budget_usd":
                        meta["stop"] = "budget"
                    elif nudges >= args.max_nudges:
                        meta["stop"] = "nudges_exhausted"
                    elif stalled >= args.max_stalled_nudges:
                        meta["stop"] = "stalled"
                    else:
                        nudges += 1
                        time.sleep(30 * api_errors)
                        send(NUDGE.format(turn=turn, turn_limit=limit))
                        continue
                    proc.stdin.close()
        meta.setdefault("stop", "claude_exited")
        finished = True
    finally:
        timer.cancel()
        meta["nudges"] = nudges
        if finished:
            try:
                proc.wait(30)
            except subprocess.TimeoutExpired:
                pass
        kill_group(proc, grace=5)
        meta["claude_exit"] = proc.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--turns", type=int, default=60, help="game turn limit")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--budget-usd", type=float, default=25.0, help="claude --max-budget-usd for the whole run")
    ap.add_argument("--max-agent-turns", type=int, default=300, help="claude --max-turns, per segment")
    ap.add_argument("--max-nudges", type=int, default=30)
    ap.add_argument("--max-stalled-nudges", type=int, default=3,
                    help="stop after this many consecutive segments without a game turn advancing")
    ap.add_argument("--timeout-min", type=float, default=240, help="wall-clock limit for the agent")
    ap.add_argument("--server-cmd", default=DEFAULT_SERVER_CMD, help="command that serves the env (see above)")
    ap.add_argument("--url", help="attach to a running env instead of starting one; the game is reset over data/add")
    ap.add_argument("--actions", type=Path, help="with --url: the env's OPENCIV_ACTION_LOG, to copy this run's rows")
    ap.add_argument("--port", type=int, help="port for the env (default: a free one)")
    ap.add_argument("--env", action="append", default=[], metavar="KEY=VALUE", help="extra env var for the server")
    ap.add_argument("--scenario", type=json.loads, default={},
                    help='extra new_game args as JSON, e.g. \'{"size": "Small"}\'')
    ap.add_argument("--run-dir", type=Path)
    ap.add_argument("--prompt", type=Path, default=HERE / "prompt.md")
    ap.add_argument("--baselines-file", type=Path, help="baselines JSON from baseline.py, for the metrics")
    ap.add_argument("--claude", default="claude", help="claude executable")
    ap.add_argument("--claude-arg", action="append", default=[],
                    help="extra claude flag, e.g. --claude-arg=--effort=low")
    ap.add_argument("--no-recording", action="store_true", help="skip the env's recording at the end")
    args = ap.parse_args()

    run_dir = (args.run_dir or HERE / "runs" / f"{datetime.now():%Y%m%d-%H%M%S}-seed{args.seed}").resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    for stale in ("transcript.jsonl", "actions.jsonl", "summary.json", "metrics.json", "server.log",
                  *RECORDING_FILES.values()):
        (run_dir / stale).unlink(missing_ok=True)
    meta = {"seed": args.seed, "turn_limit": args.turns, "scenario": args.scenario, "model": args.model,
            "budget_usd": args.budget_usd, "max_agent_turns": args.max_agent_turns,
            "started": datetime.now(UTC).isoformat(timespec="seconds"),
            "baselines_file": str(args.baselines_file.resolve()) if args.baselines_file else None}
    t0 = time.monotonic()
    env = None
    actions_offset = args.actions.stat().st_size if args.actions and args.actions.exists() else 0
    try:
        if args.url:
            env = Env(args.url)
            meta["env"] = {"url": args.url}
        else:
            port = args.port or free_ports()[0]
            env_vars = {"OPENCIV_SEED": args.seed, "OPENCIV_TURN_LIMIT": args.turns,
                        "OPENCIV_ACTION_LOG": run_dir / "actions.jsonl", **dict(kv.split("=", 1) for kv in args.env)}
            cmd = args.server_cmd.format(port=port, seed=args.seed, turns=args.turns, run_dir=run_dir)
            env = Env.start(cmd, port, env_vars, run_dir / "server.log")
            meta["env"] = {"cmd": cmd, "port": port}
        card = env.wait_ready()
        scenario = {"seed": args.seed, "turn_limit": args.turns, **args.scenario} if args.url or args.scenario else None
        env.new_game(scenario)
        log(f"env {card['name']} ready at {env.base} (mcp {env.mcp_url}), seed {args.seed}; run dir {run_dir}")
        play(args, env, run_dir, meta)
    except HarnessError as e:
        meta["harness_error"] = str(e)
        log(f"HARNESS ERROR: {e}")
    except KeyboardInterrupt:
        meta["stop"] = "interrupted"
        meta["harness_error"] = "interrupted"
    finally:
        if env is not None:
            try:
                (run_dir / "summary.json").write_text(json.dumps(env.summary(), indent=2) + "\n")
            except HarnessError as e:
                meta.setdefault("harness_error", f"final data/get: {e}")
            if not args.no_recording:
                try:
                    meta["recording"] = save_recording(env, run_dir)
                    log(f"recording: {', '.join(meta['recording']['files'])}")
                except HarnessError as e:
                    meta["recording"] = {"error": str(e)}
                    log(f"no recording: {e}")
            env.stop()
        if args.actions and args.actions.exists():
            with open(args.actions, "rb") as src:
                src.seek(actions_offset)
                (run_dir / "actions.jsonl").write_bytes(src.read())
        meta["claude_project_removed"] = [str(p) for p in clean_claude_project(run_dir)]
        meta["wall_seconds"] = round(time.monotonic() - t0, 1)
        (run_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    metrics = analyze.analyze(run_dir)
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    print(analyze.summary_line(metrics), flush=True)
    if meta.get("harness_error"):
        return 2
    return 0 if metrics["gate"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
