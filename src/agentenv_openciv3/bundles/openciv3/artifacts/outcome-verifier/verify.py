"""Grade an OpenCiv3 game from the env's data/get summary against the same-seed baselines."""

import asyncio
import time

from agentenv_protocol import client

BASELINE_WAIT_SECONDS = 300


async def _summary(base_url: str) -> dict:
    return (await client.get_data(base_url)).parts[0].data


def _pending(summary: dict) -> bool:
    """A baseline is still being played up to the game's turn."""
    return any(b["status"] == "running" and b.get("at_turn") is None for b in summary["baselines"].values())


def _baseline(summary: dict, policy: str) -> int | None:
    at_turn = summary["baselines"][policy].get("at_turn")
    return at_turn["total"] if at_turn else None


async def verify(mcp_url: str) -> list[dict]:
    base_url = mcp_url.removesuffix("/mcp")
    s = await _summary(base_url)
    deadline = time.monotonic() + BASELINE_WAIT_SECONDS
    while _pending(s) and time.monotonic() < deadline:
        await asyncio.sleep(1)
        s = await _summary(base_url)
    total, null, ai = s["score"]["total"], _baseline(s, "null"), _baseline(s, "engine_ai")
    return [
        {"criterion": "reached the turn limit", "result": s["turn"] >= s["turn_limit"],
         "turn": s["turn"], "turn_limit": s["turn_limit"]},
        {"criterion": "not defeated", "result": not s["defeated"]},
        {"criterion": "founded at least one city", "result": s["metrics"]["cities"] >= 1,
         "cities": s["metrics"]["cities"]},
        {"criterion": "beats the do-nothing baseline at the same turn", "result": null is not None and total > null,
         "game_score": total, "null_baseline": null},
        {"criterion": "score as a fraction of the built-in AI's at the same turn", "weight": 2,
         "result": ai is not None and total >= ai, "score": min(1.0, total / max(ai, 1)) if ai is not None else 0.0,
         "game_score": total, "engine_ai_baseline": ai, "metrics": s["metrics"], "actions": s["actions"]},
    ]
