"""A stand-in for `codex exec --json`: one process is one message of a thread (`resume <thread> -` goes on with it),
which plays fake_game on. FAKE_CODEX_FAIL fails the turn with that message."""

import os
import sys
from pathlib import Path

from fake_game import emit, load, play, save

args = sys.argv[1:]
state = load()
state["config"] = (Path(os.environ["CODEX_HOME"]) / "config.toml").read_text()
state["args"] = args
thread = args[args.index("resume") + 1] if "resume" in args else f"thread-{len(state['sessions']) + 1}"
messages = state["sessions"].setdefault(thread, [])
messages.append(sys.stdin.read())
emit({"type": "thread.started", "thread_id": thread})
emit({"type": "turn.started"})
if failure := os.environ.get("FAKE_CODEX_FAIL"):
    save(state)
    emit({"type": "error", "message": "Reconnecting... 1/2"})
    emit({"type": "turn.failed", "error": {"message": failure}})
    sys.exit(1)
for k, (tool, text) in enumerate(play(state)):
    call = {"id": f"item_{k}", "type": "mcp_tool_call", "server": "openciv3", "tool": tool, "arguments": {}}
    emit({"type": "item.started", "item": {**call, "result": None, "error": None, "status": "in_progress"}})
    result = {"content": [{"type": "text", "text": text}], "structured_content": None}
    emit({"type": "item.completed", "item": {**call, "result": result, "error": None, "status": "completed"}})
save(state)
n = len(messages)
message = {"id": "item_9", "type": "agent_message", "text": f"Ended turn {state['turn']}."}
emit({"type": "item.completed", "item": message})
emit({"type": "turn.completed", "usage": {"input_tokens": 100 * n, "cached_input_tokens": 50 * n,
                                          "output_tokens": 10 * n}})
