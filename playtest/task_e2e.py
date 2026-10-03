"""End to end through the task steps: a person against the engine AI, as the human-vs-ai task runs it, without Docker.

    .venv/bin/python playtest/task_e2e.py --out /tmp/task-e2e --turns 8

Starts the env locally on a copy of the bridge (as bots.py does), then runs the real `openciv3_match` step with
`humans` (no agents) and the `openciv3_await_game` step, while playtest/play_e2e.mjs plays the human seat in a
headless browser from the link the match step produced. Checks that the match step stored the play link, that the
await step returns once the game is over, and that the browser's checks pass. Needs node and the global playwright.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from agent_env.env.env import DeployedEnv
from agent_env.task_step.context import TaskStepContext
from agentenv_protocol import client
from agentenv_protocol.types import WELL_KNOWN_PATH

from agentenv_openciv3.steps import OpenCiv3AwaitGameTaskStep, OpenCiv3MatchTaskStep

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def main(args: argparse.Namespace) -> int:
    out: Path = args.out.resolve()
    shutil.rmtree(out, ignore_errors=True)
    (out / "tmp").mkdir(parents=True)
    shutil.copytree(ROOT / "build" / "bridge", out / "bridge")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "CIVBRIDGE_CMD": shlex.quote(str(out / "bridge" / "CivBridge")), "MCP_HOST": "127.0.0.1",
           "MCP_PORT": str(port), "OPENCIV_ACTION_LOG": str(out / "actions.jsonl"), "OPENCIV_BASELINES": "0",
           "OPENCIV_RECORD": "1", "TMPDIR": str(out / "tmp")}
    log = open(out / "server.log", "wb")
    proc = subprocess.Popen([sys.executable, "-m", "agentenv_openciv3.server"], env=env, stdout=log,
                            stderr=subprocess.STDOUT, start_new_session=True)
    browser = None
    try:
        for _ in range(240):
            with contextlib.suppress(Exception):
                card = await client.get_card(base, 5)
                break
            await asyncio.sleep(0.5)
        else:
            raise SystemExit("the env did not come up")
        record = DeployedEnv(env_id="openciv3", env_version=1, environment_card_url=base + WELL_KNOWN_PATH,
                             environment_card=card)
        context = TaskStepContext(deployed_envs=[record], metadata={"task_id": "task-e2e"}, instance_id="i1")

        match = OpenCiv3MatchTaskStep(id="match", version=None, env_id="openciv3", turns=args.turns, size="Tiny",
                                      humans={"you": "Rome"}, ai_opponents=2, human_turn_seconds=600)
        context = await match.execute(context)
        play = context.metadata["openciv3_match"]["play"]
        print("match step:", json.dumps(play))
        assert play["you"]["civ"] == "Rome" and "/play#token=" in play["you"]["url"], play

        (out / "play.json").write_text(json.dumps({"base": base, "play": {"Rome": play["you"]["url"]}}))
        browser = subprocess.Popen(["node", str(ROOT / "playtest" / "play_e2e.mjs"), str(out), str(args.turns + 2)],
                                   env={**os.environ, "NODE_PATH": subprocess.run(
                                       ["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()})
        started = time.monotonic()
        await_step = OpenCiv3AwaitGameTaskStep(id="await", version=None, env_id="openciv3", timeout_seconds=900,
                                               poll_seconds=2)
        context = await await_step.execute(context)
        game = context.metadata.get("openciv3_game")
        print(f"await step returned after {time.monotonic() - started:.0f}s:", json.dumps(game)[:300])
        res = await client.get_data(base, 60)
        summary = res.parts[0].data if hasattr(res.parts[0], "data") else res.parts[0].root.data
        (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
        code = await asyncio.to_thread(browser.wait, 300)
        print("browser e2e exit:", code)
        ok = code == 0 and bool(summary.get("game_over"))
        print("TASK E2E", "PASSED" if ok else "FAILED")
        return 0 if ok else 1
    finally:
        if browser and browser.poll() is None:
            browser.kill()
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(15)
        log.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--out", type=Path, default=Path("/tmp/task-e2e"))
    p.add_argument("--turns", type=int, default=8)
    sys.exit(asyncio.run(main(p.parse_args())))
