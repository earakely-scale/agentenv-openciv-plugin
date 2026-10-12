"""The match a game from the lobby reports (agentenv-game-env's `urn:game:match/v1`): its turns, each player's outcome
by the game's own victory rules, scores and moves, the play-out, the spectator view and the match's files; with the
fake bridge, whose states the tests set where a game would have to be played to reach them."""

import pytest
from agentenv_protocol import DataPart
from seated import lobby_game, playing

from agentenv_openciv3 import server

pytestmark = pytest.mark.anyio

PLAYERS = {"seats": ["Greece"], "labels": {"Rome": "opus", "Greece": "sonnet"}, "opponents": 2}


def as_seen(env, turn: int, victory: dict | None = None, defeated: tuple = ()) -> None:
    """The game as its seats last saw it: at `turn`, with `victory`, and `defeated` civilizations knocked out."""
    for seat in env.seats:
        lost = seat.civ in defeated
        seat.last_state = {**seat.last_state, "turn": turn, "victory": victory, "defeated": lost,
                           "game_over": victory is not None or lost or turn >= seat.last_state["turn_limit"]}
    env.turn = turn


def outcomes(env) -> dict:
    return {p: s.status.value for p, s in env.match.player_states.items()}


@pytest.fixture(autouse=True)
def short_waits(monkeypatch):
    """A seat that ends its turn waits for the others a moment, not minutes."""
    monkeypatch.setattr(server, "SEAT_WAIT_SECONDS", 0.2)


async def test_a_game_from_the_lobby_has_a_match_of_turns_scores_and_moves(env):
    await lobby_game(env, **PLAYERS)
    assert [(s.civ, s.player_id, s.label) for s in env.seats] == [("Rome", "opus", "opus"), ("Greece", "sonnet",
                                                                                            "sonnet")]
    assert list(env.ai_civs) == ["ai1"]
    match = env.match
    assert (match.status.value, match.progress[0].unit, match.progress[0].value, match.progress[0].limit) == (
        "started", "turns", 1, 8)
    assert outcomes(env) == {"opus": "undecided", "sonnet": "undecided", "ai1": "undecided"}
    assert [s.name for s in match.player_states["opus"].scores] == ["score", "cities", "population", "techs"]
    assert match.player_states["ai1"].scores is None and match.player_states["ai1"].moves is None
    with playing(env, "opus"):
        await env.mcp.call_tool("end_turn", {"skip_idle": True})
    assert env.match.player_states["opus"].moves.model_dump() == {"made": 1, "missed": 0}
    assert env.match.player_states["sonnet"].moves.model_dump() == {"made": 0, "missed": 0}


async def test_a_victory_is_its_civilizations_and_every_other_player_has_lost(env):
    await lobby_game(env, **PLAYERS)
    as_seen(env, 5, {"kind": "conquest", "civ": "Greece", "label": "sonnet", "turn": 5}, defeated=("Rome",))
    match = env.match
    assert (match.status.value, match.status_detail) == ("finished", "sonnet won by conquest on turn 5")
    assert outcomes(env) == {"opus": "lost", "sonnet": "won", "ai1": "lost"}


async def test_an_ai_civilizations_victory_is_its_player_slots_and_the_agents_lost(env):
    await lobby_game(env, **PLAYERS)
    as_seen(env, 8, {"kind": "score", "civ": env.ai_civs["ai1"], "label": None, "turn": 8})
    assert outcomes(env) == {"opus": "lost", "sonnet": "lost", "ai1": "won"}


async def test_at_the_turn_limit_a_tie_on_top_is_no_ones_victory(env):
    await lobby_game(env, **PLAYERS)
    as_seen(env, 8)
    assert (env.match.status.value, env.match.status_detail) == (
        "finished", "the turn limit, turn 8: a tie on top, no one's victory")
    assert outcomes(env) == {"opus": "undecided", "sonnet": "undecided", "ai1": "undecided"}


async def test_a_defeated_civilization_has_lost_while_the_others_play_on(env):
    await lobby_game(env, **PLAYERS)
    as_seen(env, 3, defeated=("Greece",))
    assert env.match.status.value == "started" and outcomes(env)["sonnet"] == "lost"
    assert outcomes(env)["opus"] == "undecided"


async def test_finish_plays_the_match_out_and_each_turn_the_env_ends_is_a_missed_one(env):
    await lobby_game(env, **PLAYERS, turn_limit=4)
    with playing(env, "sonnet"):
        await env.mcp.call_tool("end_turn", {"skip_idle": True})
    match = await env.finish_match()
    assert (match.status.value, match.progress[0].value) == ("finished", 4)
    moves = {p: s.moves.model_dump() for p, s in match.player_states.items() if s.moves}
    assert moves == {"opus": {"made": 0, "missed": 3}, "sonnet": {"made": 1, "missed": 2}}


async def test_the_spectator_view_is_the_live_page_and_the_files_are_the_recording(env):
    await lobby_game(env, **PLAYERS, turn_limit=2)
    assert env.spectators_card()["additionalInterfaces"] == [{"url": "/live", "transport": "http"}]
    assert env.slot_card("opus")["additionalInterfaces"] == [{"url": "/mcp", "transport": "mcp"}]
    await env.finish_match()
    listed = await env._match_files({"kinds": ["html_replay", "last_turn"]})
    assert sorted(f["kind"] for f in listed["files"]) == ["html_replay", "last_turn"]
    assert all(f["bytes"] > 0 and f["path"].startswith("/agentenv/ext/match/files/") for f in listed["files"])
    with pytest.raises(Exception, match="OpenCiv3 keeps a match's map_video, html_replay, last_turn, client_video"):
        await env._match_files({"kinds": ["replay"]})


async def test_the_data_plane_starts_a_default_game_after_a_lobby_game(env):
    await lobby_game(env, **PLAYERS)
    await env.data_reset()
    assert [(s.civ, s.player_id) for s in env.seats] == [("Rome", None)] and "seats" not in env.scenario
    await env.data_add([DataPart(data={"scenario": {"turn_limit": 5}})])
    assert [(s.civ, s.player_id) for s in env.seats] == [("Rome", None)] and env.scenario["turn_limit"] == 5
