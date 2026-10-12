"""A game from the env's lobby in process, and an agent's MCP requests at its player slot's address."""

from contextlib import contextmanager

from agentenv_game import SlotRequest
from agentenv_game.routing import PLAYER


async def lobby_game(env, civ: str = "Rome", seats=(), labels=None, humans=(), opponents=None, **settings) -> dict:
    """A game from the env's lobby, set up as new-game set one up before the lobby: `civ` and each of `seats` a player
    slot, a person's for a civ in `humans` and an agent's otherwise, its player_id the civ's label (else the civ),
    and the rest of `opponents` the game's AI (one, for a game of one civ). The answer is new-game's then: the game,
    its scenario and each person's play link, by civ."""
    labels = labels or {}
    env.new_lobby({"seed": env.scenario["seed"], "turn_limit": env.scenario["turn_limit"], **settings})
    for c in [civ, *seats]:
        env.fill_slot(SlotRequest(player_id=labels.get(c, c), player_kind="human" if c in humans else "agent",
                                  player_name=labels.get(c), game_settings={"civ": c}))
    for i in range(opponents - len(seats) if opponents is not None else 0 if seats else 1):
        env.fill_slot(SlotRequest(player_id=f"ai{i + 1}", player_kind="ai"))
    await env.close_lobby()
    return {**env.game, "scenario": env.scenario,
            "play": {s.civ: f"/play#token={s.token}" for s in env.seats if s.human}}


@contextmanager
def playing(env, name: str | None):
    """MCP requests at the player slot of the seat `name` names (its label, civ or player_id), as at
    /players/<player_id>/mcp, or at a player slot of that id when no seat has it; at the env's own address for
    None."""
    seat = next((s for s in env.seats if name in (s.label, s.civ, s.player_id)), None) if name else None
    token = PLAYER.set(seat.player_id if seat else name)
    try:
        yield
    finally:
        PLAYER.reset(token)
