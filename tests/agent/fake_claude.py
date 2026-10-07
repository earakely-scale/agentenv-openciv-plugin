"""A stand-in for `claude -p` in stream-json mode: one process is one session, each user message plays fake_game on.
FAKE_HANG=model hangs once after a tool result, as a model request that never returns; FAKE_HANG=tool makes one
end_turn take FAKE_HANG_SECONDS, as one that waits for the other seats."""

import json
import os
import sys
import time
from pathlib import Path

from fake_game import emit, load, play, save

state = load()
state["mcp"] = json.loads(Path(sys.argv[sys.argv.index("--mcp-config") + 1]).read_text())
session = state["sessions"].setdefault(str(len(state["sessions"]) + 1), [])
for n, line in enumerate(iter(sys.stdin.readline, ""), 1):
    session.append(json.loads(line)["message"]["content"])
    for tool, text in play(state):
        use = {"type": "tool_use", "id": tool, "name": f"mcp__openciv3__{tool}", "input": {}}
        emit({"type": "assistant", "message": {"content": [use]}})
        hang = os.environ.get("FAKE_HANG") if not state.get("hung") else None
        if hang == "tool" and tool == "end_turn":
            state["hung"] = True
            time.sleep(float(os.environ["FAKE_HANG_SECONDS"]))
        result = {"type": "tool_result", "tool_use_id": tool, "content": [{"type": "text", "text": text}]}
        emit({"type": "user", "message": {"content": [result]}})
        if hang == "model":
            state["hung"] = True
            save(state)
            time.sleep(3600)
    save(state)
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": f"Ended turn {state['turn']}."}]}})
    emit({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.25 * n,
          "usage": {"input_tokens": 100, "output_tokens": 10},
          "modelUsage": {"claude-fake": {"inputTokens": 100 * n, "outputTokens": 10 * n}}})
