"""Per-call record of what the agent did: the JSON-lines action log plus the counters data/get reports."""

from __future__ import annotations

import collections
import datetime
import json
from pathlib import Path
from typing import Any


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

    def record(self, *, turn: int | None, tool: str, args: dict, ok: bool, error_code: str | None, ms: float,
               **extra: Any) -> None:
        if ok:
            self.ok += 1
            self.streak = 0
        else:
            self.invalid += 1
            self.streak += 1
            self.max_streak = max(self.max_streak, self.streak)
        if self.path is None:
            return
        row = {"ts": datetime.datetime.now(datetime.UTC).isoformat(timespec="milliseconds"), "turn": turn,
               "tool": tool, "args": args, "ok": ok, "error_code": error_code, "ms": round(ms, 1), **extra}
        with self.path.open("a") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")

    def summary(self) -> dict:
        return {"ok": self.ok, "invalid": self.invalid, "max_consecutive_errors": self.max_streak}
