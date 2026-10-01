"""One playtest: start the env, let `claude -p` play it over MCP, record, analyze.

    python playtest/run.py --seed 1 --turns 60 --model sonnet --budget-usd 25

The run directory gets transcript.jsonl (claude's stream-json), actions.jsonl (the env's action
log), summary.json (final data/get), meta.json, server.log, claude.stderr, metrics.json and the env's
recording of the game (recording.mp4, or .gif without ffmpeg, and replay.html).
`--server-cmd` may use the placeholders {port}, {seed}, {turns} and {run_dir}, e.g. for Docker:
    --server-cmd "docker run --rm -p 127.0.0.1:{port}:18765 -e OPENCIV_SEED={seed}
                  -e OPENCIV_TURN_LIMIT={turns} -e OPENCIV_ACTION_LOG=/run/actions.jsonl -v {run_dir}:/run openciv3"
For long games, --context-cap plays the game in several sessions (see play()).
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


def load_prompts(path: Path) -> dict[str, str]:
    """prompt.md's sections by lower-case title: `system prompt` and `game prompt`, plus `resume prompt` and
    `handoff` for games played in several sessions (--context-cap)."""
    sections, current = {}, None
    for line in path.read_text().splitlines():
        if line.startswith("## "):
            current = sections.setdefault(line[3:].strip().lower(), [])
        elif current is not None:
            current.append(line)
    for required in ("system prompt", "game prompt"):
        if required not in sections:
            raise SystemExit(f"{path}: missing section ## {required.capitalize()}")
    return {title: "\n".join(lines).strip() for title, lines in sections.items()}


class _Fields(dict):
    def __missing__(self, key):
        return "?"


def claude_cmd(args, mcp_json: Path, system: str, session_id: str, budget: float) -> list[str]:
    return [args.claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--mcp-config", str(mcp_json), "--strict-mcp-config", "--tools", "", "--setting-sources", "",
            "--system-prompt", system, "--allowedTools", f"mcp__{MCP_NAME}__*", "--permission-mode", "dontAsk",
            "--model", args.model, "--max-turns", str(args.max_agent_turns),
            "--max-budget-usd", f"{budget:.2f}", "--no-session-persistence", "--session-id", session_id,
            *args.claude_arg]


def context_of(usage: dict) -> int:
    """The tokens one request sent: its whole conversation so far."""
    return sum(usage.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


def play(args, env: Env, run_dir: Path, meta: dict) -> None:
    """Play the game in `claude -p` sessions: a prompt, then a nudge after each result while the game goes on.

    Without --context-cap one session plays the whole game. With it, a session ends at the first result after its
    context passes the cap, and the next one starts afresh: the env keeps the game, and the brief and the plan
    carry what the agent needs, so a long game costs and waits like a short one.
    """
    prompts = load_prompts(args.prompt)
    if args.context_cap and not {"resume prompt", "handoff"} <= set(prompts):
        raise SystemExit(f"{args.prompt}: --context-cap needs the sections ## Resume prompt and ## Handoff")
    start = env.summary()
    meta.update(start_turn=start.get("turn"), turn_limit=start.get("turn_limit"), sessions=[])
    mcp_json = run_dir / "mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": {MCP_NAME: {"type": "http", "url": env.mcp_url}}}, indent=2) + "\n")
    game = {"deadline": time.monotonic() + args.timeout_min * 60, "nudges": 0, "stalled": 0, "spent": 0.0,
            "last_turn": start.get("turn"), "api_failed_sessions": 0}
    try:
        with open(run_dir / "transcript.jsonl", "w") as out:
            while "stop" not in meta:
                s = env.summary()
                text = prompts["game prompt"] + (f"\n\n{prompts['resume prompt']}" if meta["sessions"] else "")
                text += f"\n\n{prompts['handoff']}" if args.context_cap else ""
                session(args, env, run_dir, out, mcp_json, prompts["system prompt"],
                        text.format_map(_Fields(s, seed=s.get("seed", args.seed))), game, meta)
    finally:
        meta["nudges"] = game["nudges"]
        meta["cost_usd"] = round(game["spent"], 4)
        if meta["sessions"]:
            meta["session_id"] = meta["sessions"][0]["session_id"]


def session(args, env: Env, run_dir: Path, out, mcp_json: Path, system: str, prompt: str, game: dict,
            meta: dict) -> None:
    """One claude process. Sets meta["stop"] when the game, the budget, the clock, the nudges or a stall end the
    run, and returns without it when the next session should take over."""
    budget, remaining = args.budget_usd - game["spent"], game["deadline"] - time.monotonic()
    if budget < 0.05 or remaining <= 0:
        meta["stop"] = "budget" if budget < 0.05 else "timeout"
        return
    rec = {"session_id": str(uuid.uuid4()), "start_turn": env.summary().get("turn"), "calls": 0, "cost_usd": 0.0,
           "context": 0, "max_context": 0}
    meta["sessions"].append(rec)
    with open(run_dir / "claude.stderr", "a") as err:
        proc = subprocess.Popen(claude_cmd(args, mcp_json, system, rec["session_id"], budget), cwd=run_dir, text=True,
                                bufsize=1, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                                start_new_session=True)

    def send(text: str) -> None:
        try:
            proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
            proc.stdin.flush()
        except BrokenPipeError:
            pass   # claude already exited; the stdout loop ends and records it

    def timeout() -> None:
        meta["stop"] = "timeout"
        kill_group(proc, grace=5)

    timer = threading.Timer(remaining, timeout)
    timer.start()
    api_errors = 0
    try:
        send(prompt)
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
                if not rec["calls"]:
                    log(f"session {len(meta['sessions'])} connected at T{rec['start_turn']}: model {ev.get('model')}, "
                        f"{len(tools)} tools")
            elif kind == "assistant":
                rec["calls"] += sum(c.get("type") == "tool_use" for c in ev["message"].get("content") or [])
                if usage := ev["message"].get("usage"):
                    rec["context"] = context_of(usage)
                    rec["max_context"] = max(rec["max_context"], rec["context"])
            elif kind == "result":
                rec["cost_usd"] = ev.get("total_cost_usd") or 0.0
                s = env.summary()
                turn, limit = s.get("turn"), s.get("turn_limit")
                reason = ev.get("terminal_reason") or ev.get("subtype")
                # API failures are infrastructure, not the agent: retry them, but never count them as a stall.
                api_errors = api_errors + 1 if reason == "api_error" else 0
                if not api_errors:
                    game["stalled"] = game["stalled"] + 1 if turn == game["last_turn"] else 0
                game["last_turn"] = turn
                spent = game["spent"] + rec["cost_usd"]
                log(f"session {len(meta['sessions'])} segment ended ({reason}): T{turn}/{limit}, {rec['calls']} "
                    f"calls, context {rec['context'] // 1000}K, ${spent:.2f} so far")
                if s.get("game_over") or s.get("defeated"):
                    meta["stop"] = "defeated" if s.get("defeated") else "game_over"
                elif reason == "budget_exhausted" or ev.get("subtype") == "error_max_budget_usd":
                    meta["stop"] = "budget"
                elif game["nudges"] >= args.max_nudges:
                    meta["stop"] = "nudges_exhausted"
                elif game["stalled"] >= args.max_stalled_nudges:
                    meta["stop"] = "stalled"
                elif api_errors >= 3:
                    rec["end"] = "api_errors"
                elif args.context_cap and rec["context"] >= args.context_cap:
                    rec["end"] = "context"
                else:
                    game["nudges"] += 1
                    time.sleep(30 * api_errors)
                    send(NUDGE.format(turn=turn, turn_limit=limit))
                    continue
                break
        else:
            meta.setdefault("stop", "claude_exited")
        rec.setdefault("end", meta.get("stop"))
        game["api_failed_sessions"] = game["api_failed_sessions"] + 1 if rec["end"] == "api_errors" else 0
        if game["api_failed_sessions"] >= 2:
            raise HarnessError("claude API errors ended two sessions in a row")
    finally:
        timer.cancel()
        try:
            proc.stdin.close()
            proc.wait(30)
        except (OSError, subprocess.TimeoutExpired):
            pass
        kill_group(proc, grace=5)
        meta["claude_exit"] = proc.returncode
        game["spent"] += rec["cost_usd"]
        try:
            rec["end_turn"] = env.summary().get("turn")
        except HarnessError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--turns", type=int, default=60, help="game turn limit")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--budget-usd", type=float, default=25.0, help="claude --max-budget-usd for the whole run")
    ap.add_argument("--max-agent-turns", type=int, default=300, help="claude --max-turns, per segment")
    ap.add_argument("--context-cap", type=int,
                    help="start a new session once a session's context passes this many tokens (long games)")
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
    ap.add_argument("--recording-formats", default="mp4,html",
                    help="formats to ask the recording extension for; client_mp4 needs the client image")
    ap.add_argument("--recording-timeout", type=float, default=900, help="seconds allowed for the recording")
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
                    meta["recording"] = save_recording(env, run_dir, args.recording_formats.split(","),
                                                       args.recording_timeout)
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
