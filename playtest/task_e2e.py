"""End to end through the task steps: a person against the engine AI, as the human-vs-ai task runs it, without Docker.

    .venv/bin/python playtest/task_e2e.py --out /tmp/task-e2e --turns 8

Starts the env locally on a copy of the bridge (as bots.py does), then runs agentenv-game-env's real steps: open_lobby,
add_player_slot for a person and two of the game's AI, close_lobby and await_match, while playtest/play_e2e.mjs plays
the person's civilization in a headless browser from the page link add_player_slot gave (the slot's token in its
fragment). Checks the link, that await_match returns once the match is over, that the match reports the person's
outcome, scores and moves, and that the browser's checks pass. Needs node and the global playwright.
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
from urllib.parse import urlencode

from agent_env.env.env import DeployedEnv
from agent_env.task_step.context import TaskStepContext
from agentenv_game.steps import AddPlayerSlotTaskStep, AwaitMatchTaskStep, CloseLobbyTaskStep, OpenLobbyTaskStep
from agentenv_protocol import client
from agentenv_protocol.types import WELL_KNOWN_PATH

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

        await OpenLobbyTaskStep(id="lobby", version=None, env_id="openciv3", game_settings={
            "turn_limit": args.turns, "size": "Tiny", "human_turn_seconds": 600}).execute(context)
        for player_id, kind, settings in (("you", "human", {"civ": "Rome"}), ("ai1", "ai", {}), ("ai2", "ai", {})):
            await AddPlayerSlotTaskStep(id=f"slot-{player_id}", version=None, env_id="openciv3", player_id=player_id,
                                        player_kind=kind, player_name=player_id if kind == "human" else None,
                                        game_settings=settings).execute(context)
        await CloseLobbyTaskStep(id="match", version=None, env_id="openciv3").execute(context)
        slot = context.metadata["game_slots"]["you"]
        url = f"{slot['interfaces'][0]['url']}#{urlencode(slot['headers'])}"
        print("player slot:", url)
        assert slot["interfaces"][0]["transport"] == "http" and "#x-openciv3-token=" in url, slot

        (out / "play.json").write_text(json.dumps({"base": base, "play": {"Rome": url}}))
        browser = subprocess.Popen(["node", str(ROOT / "playtest" / "play_e2e.mjs"), str(out), str(args.turns + 2)],
                                   env={**os.environ, "NODE_PATH": subprocess.run(
                                       ["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()})
        started = time.monotonic()
        await_step = AwaitMatchTaskStep(id="await", version=None, env_id="openciv3", timeout_seconds=900,
                                        poll_seconds=2)
        context = await await_step.execute(context)
        match = context.metadata["game_match"]
        print(f"await_match returned after {time.monotonic() - started:.0f}s:", json.dumps(match)[:600])
        you = match["player_states"]["you"]
        assert match["status"] == "finished" and you["moves"]["made"] > 0 and you["scores"][0]["name"] == "score", match
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
