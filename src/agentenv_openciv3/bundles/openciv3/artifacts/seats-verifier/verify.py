"""Grade a game several agents played, one seat each, from the env's data/get summary.

The grade is whether the match counts: it reached the turn limit, the engine kept running, and every agent played
its own turns (the env ends a silent seat's turn so the others can go on; past a tenth of a seat's turns the match
does not count). How each agent did is reported, not graded: one row per seat with its rank among the seats by
score, its score and metrics, and its share of the world's land and population.
"""

from agentenv_protocol import client

GATE_WEIGHT = -100
REPORTED = 0
IDLE_SHARE = 0.1


def grade(s: dict) -> list[dict]:
    seats = s.get("seats") or []
    ranked = sorted(seats, key=lambda seat: -seat["score"]["total"])
    rows = [{"criterion": "reached the turn limit", "result": s["turn"] >= s["turn_limit"],
             "turn": s["turn"], "turn_limit": s["turn_limit"]}]
    for seat in seats:
        rank = ranked.index(seat) + 1
        rows.append({
            "criterion": f"{seat.get('label') or seat['civ']} ({seat['civ']}): rank among the {len(seats)} agents",
            "weight": REPORTED, "result": rank == 1, "score": (len(seats) - rank) / max(len(seats) - 1, 1),
            "rank": rank, "game_score": seat["score"]["total"], "defeated": seat["defeated"],
            "metrics": seat["metrics"], "share": seat.get("share"), "decisions": seat["decisions"],
            "actions": seat["actions"], "auto_ended_turns": seat["auto_ended_turns"]})
    idle = {seat["civ"]: seat["auto_ended_turns"] for seat in seats}
    rows += [
        {"criterion": "the engine kept running", "weight": GATE_WEIGHT, "result": not s["engine_failed"],
         "engine_restarts": s["harness"]["engine_restarts"]},
        {"criterion": f"every agent played: the env ended at most {IDLE_SHARE:.0%} of any seat's turns",
         "weight": GATE_WEIGHT, "result": len(seats) > 1 and max(idle.values()) <= IDLE_SHARE * max(s["turn"], 1),
         "auto_ended_turns": idle},
    ]
    return rows


async def verify(mcp_url: str) -> list[dict]:
    try:
        return grade((await client.get_data(mcp_url.removesuffix("/mcp"))).parts[0].data)
    except Exception as e:  # an env that can't report the game is a failed grade, not a crashed step
        return [{"criterion": "the env reported a gradable game", "result": False, "error": f"{type(e).__name__}: {e}"}]
