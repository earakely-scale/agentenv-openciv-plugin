"""Grade an OpenCiv3 game from the env's data/get summary against the same-seed baselines.

The graded bar is the scripted settler bot, which plays through the same standing orders and rules as
the agent.
"""

import asyncio
import time

from agentenv_protocol import client

BASELINE_WAIT_SECONDS = 300
REFERENCE = "settler_bot"
# A failed gate outweighs every other criterion, so the weighted average is 0.
GATE_WEIGHT = -100


async def _summary(base_url: str) -> dict:
    return (await client.get_data(base_url)).parts[0].data


def _pending(summary: dict) -> bool:
    """A baseline is still being played up to the game's turn."""
    return any(b["status"] == "running" and b.get("at_turn") is None for b in summary["baselines"].values())


def grade(s: dict) -> list[dict]:
    total = s["score"]["total"]
    baselines = {policy: (b.get("at_turn") or {}).get("total") for policy, b in s["baselines"].items()}
    reference = baselines.get(REFERENCE)
    agent_calls = s["actions"]["ok"] + s["actions"]["invalid"]
    victory = s.get("victory")
    beaten = victory is not None and victory["civ"] != s["civ"] and victory["kind"] != "score"
    return [
        {"criterion": "played to its end: the turn limit, or a victory",
         "result": s["turn"] >= s["turn_limit"] or victory is not None,
         "turn": s["turn"], "turn_limit": s["turn_limit"], "victory": victory},
        {"criterion": "not defeated, and no other civilization won by conquest or domination",
         "result": not s["defeated"] and not beaten},
        {"criterion": "founded at least one city", "result": s["metrics"]["cities"] >= 1,
         "cities": s["metrics"]["cities"]},
        {"criterion": f"score as a fraction of the {REFERENCE} baseline's at the same turn", "weight": 2,
         "result": reference is not None and total >= reference,
         "score": min(1.0, total / max(reference, 1)) if reference is not None else 0.0,
         "game_score": total, "baselines": baselines, "metrics": s["metrics"], "decisions": s["decisions"],
         "actions": s["actions"]},
        {"criterion": "the engine kept running", "weight": GATE_WEIGHT, "result": not s["engine_failed"],
         "engine_restarts": s["harness"]["engine_restarts"]},
        {"criterion": "the harness played none of an agent's turns", "weight": GATE_WEIGHT,
         "result": agent_calls == 0 or s["harness"]["autoplay_turns"] == 0,
         "agent_calls": agent_calls, "harness": s["harness"]},
    ]


async def verify(mcp_url: str) -> list[dict]:
    base_url = mcp_url.removesuffix("/mcp")
    try:
        s = await _summary(base_url)
        deadline = time.monotonic() + BASELINE_WAIT_SECONDS
        while _pending(s) and time.monotonic() < deadline:
            await asyncio.sleep(1)
            s = await _summary(base_url)
        return grade(s)
    except Exception as e:  # an env that can't report a whole game is a failed grade, not a crashed step
        return [{"criterion": "the env reported a gradable game", "result": False, "error": f"{type(e).__name__}: {e}"}]
