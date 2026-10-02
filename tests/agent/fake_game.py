"""The 30-turn game the fake CLIs play, kept in the file FAKE_GAME names: each message plays four turns, a
get_turn_brief and an end_turn call. The file also keeps what the fakes were given."""

import json
import os
from pathlib import Path

LIMIT, STEP = 30, 4


def load() -> dict:
    path = Path(os.environ["FAKE_GAME"])
    return json.loads(path.read_text()) if path.exists() else {"turn": 0, "sessions": {}}


def save(state: dict) -> None:
    Path(os.environ["FAKE_GAME"]).write_text(json.dumps(state))


def text(tool: str, turn: int) -> str:
    return (f"GAME OVER at T{turn}.\n[GAME OVER T{turn}/{LIMIT}]" if turn >= LIMIT
            else f"{tool} done.\n[T{turn}/{LIMIT} · nothing needs orders]")


def play(state: dict) -> list[tuple[str, str]]:
    """The message's two calls, each with its result."""
    brief = ("get_turn_brief", text("get_turn_brief", state["turn"]))
    state["turn"] = min(state["turn"] + STEP, LIMIT)
    return [brief, ("end_turn", text("end_turn", state["turn"]))]


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)
