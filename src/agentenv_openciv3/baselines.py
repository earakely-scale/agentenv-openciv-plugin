"""Same-seed reference games, played in the background: the built-in AI (`engine_ai`), the scripted `settler_bot`
that follows the env's own suggestions, and do-nothing (`null`)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .bridge import Bridge

log = logging.getLogger(__name__)

POLICIES = ("engine_ai", "settler_bot", "null")
CHUNK = 10
ATTEMPTS = 2


@dataclass
class Run:
    status: str = "running"
    scores: dict[int, dict] = field(default_factory=dict)
    turn: int = 0
    defeated: bool = False
    error: str | None = None


class Baselines:
    """Plays each policy to the turn limit in its own bridge, in chunks so partial trajectories are usable."""

    def __init__(self, cmd: list[str], workdir: Path):
        self.cmd = cmd
        self.workdir = workdir
        self.runs: dict[str, Run] = {}
        self._tasks: list[asyncio.Task] = []

    def start(self, scenario: dict) -> None:
        self._cancel()
        self.runs = {p: Run() for p in POLICIES}
        self._tasks = [asyncio.create_task(self._play(p, scenario)) for p in POLICIES]

    async def stop(self) -> None:
        tasks = self._cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t

    def _cancel(self) -> list[asyncio.Task]:
        tasks, self._tasks = self._tasks, []
        for t in tasks:
            t.cancel()
        return tasks

    async def _play(self, policy: str, scenario: dict) -> None:
        run = self.runs[policy]
        for attempt in range(1, ATTEMPTS + 1):
            bridge = Bridge([*self.cmd, "--autosave", str(self.workdir / policy)])
            try:
                await self._play_once(bridge, run, policy, scenario)
                run.status, run.error = "done", None
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("baseline %s failed (attempt %d of %d): %s", policy, attempt, ATTEMPTS, e)
                run.status, run.error = ("running" if attempt < ATTEMPTS else "failed"), str(e)
                run.scores.clear()
            finally:
                await bridge.close()

    @staticmethod
    async def _play_once(bridge: Bridge, run: Run, policy: str, scenario: dict) -> None:
        game = await bridge.new_game(**scenario)
        run.turn, limit = game["turn"], game["turn_limit"]
        run.scores[run.turn] = (await bridge.call("score"))["human"]
        while run.turn < limit:
            res = await bridge.call("autoplay", turns=min(CHUNK, limit - run.turn), policy=policy, record=True)
            for point in res.get("trajectory", []):
                run.scores[point["turn"]] = point["score"]
            run.scores[res["turn"]] = res["score"]
            run.turn, run.defeated = res["turn"], res.get("defeated", False)
            if res.get("game_over") or run.defeated:
                break

    def score_at(self, policy: str, turn: int) -> dict | None:
        """The policy's score at `turn`, once its game has got that far (or ended before it)."""
        run = self.runs.get(policy)
        if run is None or (run.status != "done" and run.turn < turn):
            return None
        known = [t for t in run.scores if t <= turn]
        return run.scores[max(known)] if known else None

    def scores_at(self, turn: int) -> dict[str, dict | str | None]:
        """Each policy's score at `turn`: a score, None while still computing, or "unavailable" if it failed."""
        return {p: "unavailable" if self.runs[p].status == "failed" else self.score_at(p, turn)
                for p in POLICIES if p in self.runs}

    def trajectories(self) -> dict[str, dict[int, dict]]:
        """Every score known so far for each policy, by turn."""
        return {p: dict(run.scores) for p, run in self.runs.items() if run.status != "failed"}

    def summary(self, turn: int) -> dict:
        out = {}
        for p in POLICIES:
            run = self.runs.get(p)
            if run is None:
                out[p] = {"status": "disabled"}
                continue
            final = run.scores[max(run.scores)] if run.status == "done" and run.scores else None
            out[p] = {"status": run.status, "at_turn": self.score_at(p, turn), "final": final,
                      "final_turn": run.turn if final else None, "defeated": run.defeated}
            if run.error:
                out[p]["error"] = run.error
        return out
