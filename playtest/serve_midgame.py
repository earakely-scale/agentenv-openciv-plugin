"""A game well under way for a person (or a browser test) to play in /play: one human seat, Rome, which the engine's AI
plays for --turns turns before the page is served, so it has cities, units, contacts and techs to trade
(playtest/play_features_e2e.mjs). With --gold-turns the seat then plays that many more turns at full tax, through the
play API as a person would, so it has gold to buy and upgrade with. Writes <out>/play.json with the play link, and
serves the env until stopped.

    .venv/bin/python playtest/serve_midgame.py /tmp/mid --turns 130 --gold-turns 12 --seed 4 --landform Archipelago
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("out", type=Path)
    p.add_argument("--port", type=int, default=8811)
    p.add_argument("--seed", type=int, default=4)
    p.add_argument("--turns", type=int, default=130, help="turns the engine's AI plays the seat first (default 130)")
    p.add_argument("--gold-turns", type=int, default=0, help="then turns at full tax, to save gold (default 0)")
    p.add_argument("--size", default="Small")
    p.add_argument("--landform", default="Archipelago")
    p.add_argument("--opponents", type=int, default=4)
    p.add_argument("--turn-limit", type=int, default=400)
    p.add_argument("--bridge", default=os.environ.get("CIVBRIDGE_CMD") or str(ROOT / "build/bridge/CivBridge"))
    a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    os.environ.update({"CIVBRIDGE_CMD": a.bridge, "OPENCIV_BASELINES": "0",
                       "OPENCIV_ACTION_LOG": str(a.out / "actions.jsonl")})
    asyncio.run(serve(a))


async def serve(a) -> None:
    import uvicorn
    from starlette.requests import Request

    from agentenv_openciv3.server import OpenCiv3Env

    env = OpenCiv3Env()
    app = env.create_app()
    game = await env.new_game(seed=a.seed, size=a.size, landform=a.landform, opponents=a.opponents,
                              turn_limit=a.turn_limit, humans=["Rome"], human_turn_seconds=0)
    link = game["play"]["Rome"]
    token = link.split("#token=")[1]

    async def act(tool: str, **args) -> dict:
        body = json.dumps({"tool": tool, "args": args}).encode()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        r = await env._play_act(Request({"type": "http", "method": "POST", "path": "/play/api/act", "query_string": b"",
                                         "headers": [(b"x-openciv3-token", token.encode())]}, receive))
        return json.loads(r.body)

    if a.turns:
        await env.autoplay(turns=a.turns, policy="engine_ai")
    for _ in range(a.gold_turns):
        await act("set_rates", science=0, luxury=0)
        await act("end_turn")
    (a.out / "play.json").write_text(json.dumps({"play": {"Rome": f"http://127.0.0.1:{a.port}{link}"}}))
    print(f"PLAY http://127.0.0.1:{a.port}{link}", flush=True)
    await uvicorn.Server(uvicorn.Config(app.streamable_http_app(), host="127.0.0.1", port=a.port,
                                        log_level="warning")).serve()


if __name__ == "__main__":
    main()
