"""Grade a full OpenCiv3 game from the env's data/get summary.

The bar is OpenCiv3's own AI playing the agent's seat on the same seed (the engine_ai baseline), and the agent's
standing among the civilizations it played: its rank, and its share of the world's land and population against
Civ III's domination victory, which needs two thirds of each. The game ends early when a civilization wins by
conquest or domination; another civilization's victory counts as a defeat.
"""

import asyncio
import time

from agentenv_protocol import client

BASELINE_WAIT_SECONDS = 600
REFERENCE = "engine_ai"
DOMINATION = 2 / 3
# A failed gate outweighs every other criterion, so the weighted average is 0.
GATE_WEIGHT = -100


async def _summary(base_url: str) -> dict:
    return (await client.get_data(base_url)).parts[0].data


def _pending(summary: dict) -> bool:
    """A baseline is still being played up to the game's turn."""
    return any(b["status"] == "running" and b.get("at_turn") is None for b in summary["baselines"].values())


def grade(s: dict) -> list[dict]:
    total = s["score"]["total"]
    reference = ((s["baselines"].get(REFERENCE) or {}).get("at_turn") or {}).get("total")
    standings = s.get("standings") or []
    rank = next((i for i, p in enumerate(standings, 1) if p["you"]), None)
    share = s.get("share") or {}
    land, pop = share.get("land", 0.0), share.get("pop", 0.0)
    agent_calls = s["actions"]["ok"] + s["actions"]["invalid"]
    victory = s.get("victory")
    beaten = victory is not None and victory["civ"] != s["civ"] and victory["kind"] != "score"
    return [
        {"criterion": "played to its end: the turn limit, or a victory",
         "result": s["turn"] >= s["turn_limit"] or victory is not None,
         "turn": s["turn"], "turn_limit": s["turn_limit"], "victory": victory},
        {"criterion": "not defeated, and no other civilization won by conquest or domination",
         "result": not s["defeated"] and not beaten},
        {"criterion": f"score as a fraction of the {REFERENCE} baseline's at the same turn", "weight": 2,
         "result": reference is not None and total >= reference,
         "score": min(1.0, total / max(reference, 1)) if reference is not None else 0.0,
         "game_score": total,
         "baselines": {p: (b.get("at_turn") or {}).get("total") for p, b in s["baselines"].items()},
         "metrics": s["metrics"], "decisions": s["decisions"], "actions": s["actions"]},
        {"criterion": "rank among the civilizations by score", "result": rank == 1,
         "score": (len(standings) - rank) / max(len(standings) - 1, 1) if rank else 0.0,
         "rank": rank, "standings": standings},
        {"criterion": "share of the world's land and population against domination (two thirds of each)",
         "result": land >= DOMINATION and pop >= DOMINATION, "score": min(1.0, min(land, pop) / DOMINATION),
         "land": land, "pop": pop},
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
