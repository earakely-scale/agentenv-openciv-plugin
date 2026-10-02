"""A stand-in for `claude -p` in stream-json mode. Each user message plays the 30-turn game kept in the file
FAKE_CLAUDE_GAME names on by four turns (a get_turn_brief and an end_turn call); the file also keeps the MCP config and
the messages each process got."""

import json
import os
import sys
from pathlib import Path

LIMIT, STEP = 30, 4


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


def call(tool: str, turn: int) -> None:
    text = (f"GAME OVER at T{turn}.\n[GAME OVER T{turn}/{LIMIT}]" if turn >= LIMIT
            else f"{tool} done.\n[T{turn}/{LIMIT} · nothing needs orders]")
    use = {"type": "tool_use", "id": tool, "name": f"mcp__openciv3__{tool}", "input": {}}
    emit({"type": "assistant", "message": {"content": [use]}})
    emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tool,
                                                    "content": [{"type": "text", "text": text}]}]}})


game = Path(os.environ["FAKE_CLAUDE_GAME"])
state = json.loads(game.read_text()) if game.exists() else {"turn": 0, "sessions": []}
state["mcp"] = json.loads(Path(sys.argv[sys.argv.index("--mcp-config") + 1]).read_text())
state["sessions"].append([])
for n, line in enumerate(iter(sys.stdin.readline, ""), 1):
    state["sessions"][-1].append(json.loads(line)["message"]["content"])
    call("get_turn_brief", state["turn"])
    state["turn"] = min(state["turn"] + STEP, LIMIT)
    call("end_turn", state["turn"])
    game.write_text(json.dumps(state))
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": f"Ended turn {state['turn']}."}]}})
    emit({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.25 * n,
          "usage": {"input_tokens": 100, "output_tokens": 10},
          "modelUsage": {"claude-fake": {"inputTokens": 100 * n, "outputTokens": 10 * n}}})
