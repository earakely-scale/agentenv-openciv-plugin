"""Per-call record of what the agent did: the JSON-lines action log, the counters data/get reports, and the
per-turn list of game actions the recording shows."""

from __future__ import annotations

import collections
import datetime
import json
from pathlib import Path
from typing import Any


def describe(tool: str, args: dict) -> str | None:
    """One line for a call that changes the game, e.g. `u4 settle → (32,28)`; None for observations."""
    if tool == "unit_order":
        target = f" → ({args['x']},{args['y']})" if args.get("x") is not None and args.get("y") is not None else ""
        return f"{args.get('unit')} {args.get('order')}{target}"
    if tool == "set_production":
        return f"{args.get('city')} builds {args.get('item')}"
    if tool == "research" and args.get("tech"):
        return f"research {args['tech']}"
    if tool == "set_rates":
        return "rates " + ", ".join(f"{k} {args[k]}" for k in ("science", "luxury") if args.get(k) is not None)
    if tool == "buy":
        return f"{args.get('city')} buys its production"
    return None


class ActionLog:
    def __init__(self, path: str | None):
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.reset()

    def reset(self) -> None:
        self.ok = self.invalid = self.streak = self.max_streak = 0
        self.turn: int | None = None
        self.calls_this_turn = 0
        self.failures: collections.Counter[str] = collections.Counter()
        self.timeline: dict[int, list[dict]] = {}

    def begin(self, turn: int | None) -> int:
        """Count a call in `turn`; returns how many calls this turn so far, this one included."""
        if turn != self.turn:
            self.turn, self.calls_this_turn = turn, 0
            self.failures.clear()
        self.calls_this_turn += 1
        return self.calls_this_turn

    def failed(self, key: str) -> int:
        """Count a failure of the exact call `key` this turn; returns how many times it has failed."""
        self.failures[key] += 1
        return self.failures[key]

    def note(self, turn: int | None, text: str, ok: bool = True) -> None:
        """Add a line to the turn's actions in the recording."""
        if turn is not None:
            self.timeline.setdefault(turn, []).append({"text": text, "ok": ok})

    def record(self, *, turn: int | None, tool: str, args: dict, ok: bool, error_code: str | None, ms: float,
               **extra: Any) -> None:
        if ok:
            self.ok += 1
            self.streak = 0
        else:
            self.invalid += 1
            self.streak += 1
            self.max_streak = max(self.max_streak, self.streak)
        if text := describe(tool, args):
            self.note(turn, text if ok else f"{text} ✗ {error_code}", ok)
        if self.path is None:
            return
        row = {"ts": datetime.datetime.now(datetime.UTC).isoformat(timespec="milliseconds"), "turn": turn,
               "tool": tool, "args": args, "ok": ok, "error_code": error_code, "ms": round(ms, 1), **extra}
        with self.path.open("a") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")

    def summary(self) -> dict:
        return {"ok": self.ok, "invalid": self.invalid, "max_consecutive_errors": self.max_streak}
