"""A stand-in for `gemini -p ... --output-format stream-json`: one process is one message of a session (`--resume`
goes on with it), which plays fake_game on. FAKE_GEMINI_FAIL ends the message with that error."""

import json
import os
import sys
from pathlib import Path

from fake_game import emit, load, play, save

args = sys.argv[1:]
state = load()
state["settings"] = json.loads((Path(os.environ["HOME"]) / ".gemini" / "settings.json").read_text())
state["gemini_md"] = Path("GEMINI.md").read_text()
state["args"] = [a for a in args if a != args[args.index("-p") + 1]]
state["endpoint"] = [os.environ.get("GOOGLE_GEMINI_BASE_URL"), os.environ.get("GEMINI_API_KEY")]
session = args[args.index("--resume") + 1] if "--resume" in args else f"session-{len(state['sessions']) + 1}"
state["sessions"].setdefault(session, []).append(args[args.index("-p") + 1])
emit({"type": "init", "session_id": session, "model": args[args.index("--model") + 1]})
if failure := os.environ.get("FAKE_GEMINI_FAIL"):
    save(state)
    emit({"type": "result", "status": "error", "error": {"type": "Error", "message": failure}, "stats": {}})
    sys.exit(1)
for k, (tool, text) in enumerate(play(state)):
    emit({"type": "tool_use", "tool_name": f"mcp_openciv3_{tool}", "tool_id": f"call_{k}", "parameters": {}})
    if tool == "end_turn":
        emit({"type": "tool_result", "tool_id": f"call_{k}", "status": "error", "output": "Error: MCP tool failed.",
              "error": {"type": "mcp_tool_error", "message": json.dumps([{"text": text}])}})
    else:
        emit({"type": "tool_result", "tool_id": f"call_{k}", "status": "success", "output": text})
save(state)
for chunk in ("Ended ", f"turn {state['turn']}."):
    emit({"type": "message", "role": "assistant", "content": chunk, "delta": True})
emit({"type": "result", "status": "success", "stats": {"input_tokens": 100, "output_tokens": 10, "tool_calls": 2}})
