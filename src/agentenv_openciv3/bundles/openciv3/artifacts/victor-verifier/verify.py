"""Decide which agent won a game several agents played, one seat each, from the env's data/get summary.

The victor is the seat that won by conquest or domination, else, at the turn limit, the seat with the highest score;
a tie on top is no victor and half the grade. The match counts only if it was played to its end, the engine kept
running and every agent played its own turns (the env ends a silent seat's turn so the others can go on; past a tenth
of a seat's turns the match does not count). Each seat's rank among the seats is reported, not graded: the victor
first, then undefeated seats before defeated ones, then by score.
"""

from agentenv_protocol import client

GATE_WEIGHT = -100
REPORTED = 0
IDLE_SHARE = 0.1
TIE = 0.5


def _name(seat: dict) -> str:
    return f"{seat.get('label') or seat['civ']} ({seat['civ']})"


def grade(s: dict) -> list[dict]:
    seats = s.get("seats") or []
    victory = s.get("victory")
    ended = victory is not None or s["turn"] >= s["turn_limit"]
    standing = {seat["civ"]: (seat["civ"] != (victory or {}).get("civ"), seat["defeated"], -seat["score"]["total"])
                for seat in seats}
    order = sorted(standing.values())
    rank = {civ: order.index(key) + 1 for civ, key in standing.items()}
    ranked = sorted(seats, key=lambda seat: rank[seat["civ"]])
    leaders = [seat for seat in ranked if rank[seat["civ"]] == 1]
    match = {"criterion": "the match has a victor", "result": ended and len(leaders) == 1}
    if match["result"]:
        victor, runner_up = ranked[:2]
        how, turn, margin = (victory["kind"], victory["turn"], None) if victory else (
            "score", s["turn"], victor["score"]["total"] - runner_up["score"]["total"])
        match |= {"victor": victor.get("label") or victor["civ"], "civ": victor["civ"], "how": how, "turn": turn,
                  "margin": margin}
    elif ended and leaders:
        match |= {"score": TIE, "tied": [_name(seat) for seat in leaders]}
    match["standings"] = [{"civ": seat["civ"], "label": seat.get("label"), "rank": rank[seat["civ"]],
                           "score": seat["score"]["total"], "defeated": seat["defeated"]} for seat in ranked]
    rows = [match]
    for seat in seats:
        rows.append({
            "criterion": f"{_name(seat)}: rank {rank[seat['civ']]} of {len(seats)}", "weight": REPORTED,
            "result": seat["civ"] == match.get("civ"), "score": (len(seats) - rank[seat["civ"]]) / (len(seats) - 1),
            "rank": rank[seat["civ"]], "game_score": seat["score"]["total"], "defeated": seat["defeated"],
            "metrics": seat["metrics"], "share": seat.get("share"), "decisions": seat["decisions"],
            "actions": seat["actions"], "auto_ended_turns": seat["auto_ended_turns"]})
    idle = {seat["civ"]: seat["auto_ended_turns"] for seat in seats}
    rows += [
        {"criterion": "the match was played to its end", "weight": GATE_WEIGHT, "result": ended,
         "turn": s["turn"], "turn_limit": s["turn_limit"], "game_over": s["game_over"]},
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
