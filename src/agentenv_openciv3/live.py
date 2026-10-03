"""Watch a game while it plays (GET /live, docs/viewer.md): the viewer's data, kept up to date from the bridge's
per-turn snapshots, and the real client's view of each seat from its per-turn saves. The older page's routes
(state.json, frame.png) draw with the recording's renderer."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import TypeVar

from . import client, matchdata, recording

log = logging.getLogger(__name__)

VIEWS = ("spectator", "agent")
FRAMES_KEPT = 64
STUCK_SECONDS = 60
T = TypeVar("T")


def turn_of(path: Path) -> int:
    return int(path.name.split(".")[0].removeprefix("turn-"))


def read(path: Path) -> dict | None:
    """A snapshot, or None while the bridge is still writing it."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, EOFError, ValueError):
        return None


def latest(record: Path) -> dict | None:
    """The newest snapshot that reads whole: the bridge may be writing the next one."""
    for path in sorted(record.glob("turn-*.json.gz"), reverse=True):
        if (snap := read(path)) is not None:
            return snap
    return None


def state(snap: dict | None, timeline: dict[int, list[dict]], *, game: str | None, has_client: bool) -> dict:
    """GET /live/state.json: the newest turn's scoreboard and events, and the agents' actions of the turn before."""
    if snap is None:
        return {"game": game, "turn": None, "turn_limit": None, "game_over": False, "victory": None, "players": [],
                "events": [], "actions": [], "client": has_client}
    players = sorted((p for p in snap["players"] if not recording.is_barbarian(p)),
                     key=lambda p: (-p["score"]["total"], p["index"]))
    victory = snap.get("victory")
    over = victory is not None or snap["turn"] >= snap["turn_limit"] or all(
        p["defeated"] for p in players if p["is_human"])
    return {
        "game": game, "turn": snap["turn"], "turn_limit": snap["turn_limit"], "game_over": over, "victory": victory,
        "players": [{"civ": p["civ"], "label": p.get("label"), "is_agent": p["is_human"], "defeated": p["defeated"],
                     "score": p["score"], "color": recording.hex_color(p.get("color") or recording.DIM)}
                    for p in players],
        "events": snap.get("events", []),
        "actions": timeline.get(snap["turn"] - 1, []),
        "client": has_client,
    }


def render_frame(record: Path, turn: int, view: str) -> bytes | None:
    """The map and score chart of `turn`, as the recording draws them; None until its snapshot is written. The
    snapshots before it stream through, never all in memory: a long game's take hundreds of MB."""
    paths = [p for p in sorted(record.glob("turn-*.json.gz")) if turn_of(p) <= turn]
    if not paths or turn_of(paths[-1]) != turn or read(paths[-1]) is None:
        return None
    return recording.map_png((s for s in map(read, paths) if s is not None), view=view)


def by_player(per_civ: dict[str, dict[int, T]], players: list[dict]) -> dict[int, dict[int, T]]:
    """{civ: {turn: x}} as matchdata wants it, {turn: {player index: x}}; civs that are no player are left out."""
    index = {p["civ"]: p["index"] for p in players if not p.get("barbarian") and not recording.is_barbarian(p)}
    out: dict[int, dict[int, T]] = {}
    for civ, turns in per_civ.items():
        if civ in index:
            for t, x in turns.items():
                out.setdefault(t, {})[index[civ]] = x
    return out


class LiveMatch:
    """The viewer's data of the game being played, added to as the bridge writes snapshots: each is read once,
    when it is whole, so a request never reads the whole game again."""

    def __init__(self, record: Path, game: str, labels: dict[str, str] | None = None):
        self.record = record
        self.data = matchdata.MatchData(game=game, labels=labels)
        self.lock = asyncio.Lock()

    def update(self) -> None:
        """Add the snapshots written since the last call, in turn order. One still being written stops the update
        until a later call; one that stays cut short (a crash) while later ones read is skipped after STUCK_SECONDS."""
        last = self.data.last_turn()
        paths = sorted((p for p in self.record.glob("turn-*.json.gz") if turn_of(p) > last), key=turn_of)
        for i, path in enumerate(paths):
            if (snap := matchdata.read_snapshot(path)) is not None:
                self.data.add(snap)
                continue
            try:
                stuck = time.time() - path.stat().st_mtime > STUCK_SECONDS
            except OSError:
                stuck = False
            if not (stuck and i + 1 < len(paths)):
                return

    async def document(self, since: int, actions: dict[str, matchdata.Actions], calls: dict[str, matchdata.Calls],
                       live: dict) -> bytes:
        """GET /live/data.json: the turns after `since` with `live`, as JSON. `actions` and `calls` are keyed by civ,
        then turn: the players' indices come from the snapshots."""
        def build() -> bytes:
            self.update()
            players = self.data.players
            doc = self.data.document(since, actions=by_player(actions, players), calls=by_player(calls, players))
            return json.dumps({**doc, "live": live}, ensure_ascii=False, separators=(",", ":")).encode()
        async with self.lock:
            return await asyncio.to_thread(build)


class Live:
    """The live view's images: map frames per (game, turn, view), and the client's newest frame of each seat."""

    def __init__(self) -> None:
        self.frames: dict[tuple[Path, int, str], bytes] = {}
        self.client_frames: dict[str | None, tuple[Path, int, bytes]] = {}
        self.client_tried: dict[str | None, tuple[Path, int]] = {}
        self.client_task: asyncio.Task | None = None
        self.match: LiveMatch | None = None

    def match_of(self, record: Path, game: str, labels: dict[str, str] | None = None) -> LiveMatch:
        """The viewer's data of `game`; a new game starts it over."""
        if self.match is None or self.match.data.game != game or self.match.record != record:
            self.match = LiveMatch(record, game, labels)
        return self.match

    async def frame(self, record: Path, turn: int | None, view: str) -> bytes | None:
        """The frame of `turn` (default: the newest), or None when the game has not reached it."""
        if turn is None:
            if not (paths := sorted(record.glob("turn-*.json.gz"))):
                return None
            turn = turn_of(paths[-1])
        key = (record, turn, view)
        if (png := self.frames.get(key)) is None:
            if (png := await asyncio.to_thread(render_frame, record, turn, view)) is None:
                return None
            self.frames[key] = png
            while len(self.frames) > FRAMES_KEPT:
                del self.frames[next(iter(self.frames))]
        return png

    def client_view(self, saves: Path, seat: str | None = None) -> tuple[int, bytes] | None:
        """The turn and PNG of the client's newest frame of this game from `seat` (a civ; None in a game with one
        seat). When that seat's newest save is not drawn yet and no render runs, draws it in the background: one
        render at a time, for the seat asked for, skipping any turns in between."""
        newest = max(saves.glob("turn-*.json.gz"), key=turn_of, default=None)
        idle = self.client_task is None or self.client_task.done()
        if newest is not None and idle and self.client_tried.get(seat) != (saves, turn_of(newest)):
            self.client_tried[seat] = (saves, turn_of(newest))
            self.client_task = asyncio.create_task(self._render_client(saves, newest, seat))
        shown = self.client_frames.get(seat)
        if shown is None or shown[0] != saves:
            return None
        return shown[1], shown[2]

    async def _render_client(self, saves: Path, save: Path, seat: str | None) -> None:
        try:
            png = await asyncio.to_thread(client.frame, save, **({"seat": seat} if seat else {}))
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            log.warning("the client could not render %s%s: %s", save, f" for {seat}" if seat else "", e)
            return
        for key in [k for k, v in self.client_frames.items() if v[0] != saves]:
            del self.client_frames[key]
        self.client_frames[seat] = (saves, turn_of(save), png)
