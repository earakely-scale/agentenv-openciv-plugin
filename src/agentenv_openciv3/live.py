"""Watch a game while it plays (GET /live): a page that follows the newest turn, drawn by the recording's renderer
from the bridge's per-turn snapshots, or by the real client from its per-turn saves."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import subprocess
from pathlib import Path

from . import client, recording

log = logging.getLogger(__name__)

VIEWS = ("spectator", "agent")
FRAMES_KEPT = 64


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
    """The map and score chart of `turn`, as the HTML replay shows them; None until its snapshot is written. Of the
    turns before, only the players are kept for the chart: a long game's whole snapshots take hundreds of MB."""
    paths = [p for p in sorted(record.glob("turn-*.json.gz")) if turn_of(p) <= turn]
    if not paths or turn_of(paths[-1]) != turn or (last := read(paths[-1])) is None:
        return None
    before = [{"turn": s["turn"], "turn_limit": s["turn_limit"], "players": s["players"]}
              for s in map(read, paths[:-1]) if s is not None]
    r = recording.Renderer([*before, last], view=view)
    return recording._png(r.frame(last).crop(r.map_box), colors=128)


class Live:
    """The live view's images: map frames per (game, turn, view), and the client's newest frame."""

    def __init__(self) -> None:
        self.frames: dict[tuple[Path, int, str], bytes] = {}
        self.client_frame: tuple[Path, int, bytes] | None = None
        self.client_tried: tuple[Path, int] | None = None
        self.client_task: asyncio.Task | None = None

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

    def client_view(self, saves: Path) -> tuple[int, bytes] | None:
        """The turn and PNG of the client's newest frame of this game. When a newer save is waiting and no render
        runs, renders the newest save in the background, skipping any turns in between."""
        newest = max(saves.glob("turn-*.json.gz"), key=turn_of, default=None)
        idle = self.client_task is None or self.client_task.done()
        if newest is not None and idle and self.client_tried != (saves, turn_of(newest)):
            self.client_tried = (saves, turn_of(newest))
            self.client_task = asyncio.create_task(self._render_client(saves, newest))
        if self.client_frame is None or self.client_frame[0] != saves:
            return None
        return self.client_frame[1], self.client_frame[2]

    async def _render_client(self, saves: Path, save: Path) -> None:
        try:
            png = await asyncio.to_thread(client.frame, save)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            log.warning("the client could not render %s: %s", save, e)
            return
        self.client_frame = (saves, turn_of(save), png)


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>OpenCiv3 live</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
[hidden]{display:none!important}
body{margin:0;background:#12161c;color:#e6e9ee;font:14px/1.4 system-ui,-apple-system,Segoe UI,sans-serif}
main{display:flex;gap:16px;padding:16px;align-items:flex-start}
#stage{flex:1;min-width:0} #stage img{display:block;max-width:100%;height:auto;border-radius:6px}
#waiting{padding:160px 0;text-align:center;color:#8c94a2;font-size:18px}
aside{width:360px;flex:none}
h1{font-size:24px;margin:0 0 4px} h2{font-size:12px;letter-spacing:.06em;color:#8c94a2;margin:16px 0 6px}
.controls{display:flex;gap:8px;align-items:center;margin:12px 0 4px}
button{background:#2e3746;color:#e6e9ee;border:0;border-radius:4px;padding:6px 12px;cursor:pointer}
button.on{background:#4b5a72}
table{width:100%;border-collapse:collapse} td{padding:2px 4px} td.n{text-align:right;color:#8c94a2}
tr.me{background:#2e3746;font-weight:600} tr.out td{color:#8c94a2}
.sw{display:inline-block;width:11px;height:11px;margin-right:6px;vertical-align:-1px}
ul{list-style:none;padding:0;margin:0} li{padding:1px 0} .bad,.urgent{color:#f08060} .none{color:#8c94a2}
#banner{margin:12px 0 0;padding:10px 12px;border-radius:6px;background:#2e3746;font-weight:600}
#banner.win{background:#4d4120;color:#ffe08a}
</style></head><body><main>
<div id="stage"><div id="waiting">Waiting for the game to start…</div>
<img id="map" alt="the map" hidden><img id="client" alt="the OpenCiv3 client's view" hidden></div>
<aside><h1 id="turn">OpenCiv3</h1><div class="none" id="sub">live</div><div id="banner" hidden></div>
<div class="controls"><button id="v-spectator" class="on">Map</button><button id="v-agent">Agents' view</button>
<button id="v-client" hidden>Client</button></div><div class="none" id="note"></div>
<h2>SCORE</h2><table id="score"></table>
<h2 id="acts-h">AGENT ACTIONS</h2><ul id="acts"></ul><h2 id="evts-h">EVENTS</h2><ul id="evts"></ul>
</aside></main>
<script>
const URGENT = new Set(__URGENT__), $ = id => document.getElementById(id);
const COLS = [["score", "total"], ["cities", "cities"], ["pop", "pop"], ["techs", "techs"]];
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"})[c]);
const list = (items, cls, text) => items.length ? items.map(i => `<li class="${cls(i)}">${esc(text(i))}</li>`).join("")
  : '<li class="none">none</li>';
const named = (civ, label) => label ? `${civ} (${label})` : civ;
const VIEWS = ["spectator", "agent", "client"];
let s = null, view = VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "spectator";
let mapUrl = null, clientAt = null, clientBusy = false;

function show() {
  const started = s.turn !== null;
  $("waiting").hidden = started;
  $("waiting").textContent = s.recording === false
    ? "Recording is off (OPENCIV_RECORD=0), so there is nothing to show." : "Waiting for the game to start…";
  $("v-client").hidden = !s.client;
  $("turn").textContent = started ? `Turn ${s.turn} / ${s.turn_limit}` : "OpenCiv3";
  document.title = started ? `T${s.turn} · OpenCiv3 live` : "OpenCiv3 live";
  const agents = s.players.filter(p => p.is_agent).map(p => named(p.civ, p.label));
  $("sub").textContent = (agents.length ? agents.join(" vs ") + " · " : "") + "live, every 2 s";
  const v = s.victory, top = s.players[0];
  $("banner").hidden = !s.game_over && !v;
  $("banner").className = v ? "win" : "";
  $("banner").textContent = v ? `${named(v.civ, v.label)} wins by ${v.kind} on turn ${v.turn}` : top ?
    `Game over at turn ${s.turn}: ${named(top.civ, top.label)} has the top score, ${top.score.total}` : "";
  $("score").innerHTML = (started ? `<tr><td></td>${COLS.map(([h]) => `<td class="n">${h}</td>`).join("")}</tr>` : "")
    + s.players.map(p => `<tr class="${p.is_agent ? "me" : ""}${p.defeated ? " out" : ""}"><td><span class="sw" ` +
    `style="background:${p.color}"></span>${esc(named(p.civ, p.label))}${p.defeated ? " (out)" : ""}</td>` +
    COLS.map(([, k]) => `<td class="n">${p.score[k]}</td>`).join("") + "</tr>").join("");
  const at = started && s.turn > 0 ? ` T${s.turn - 1}` : "";
  const seat = civ => (s.players.find(p => p.civ === civ) || {}).label || civ;
  $("acts-h").textContent = "AGENT ACTIONS" + at; $("evts-h").textContent = "EVENTS" + at;
  $("acts").innerHTML = list(s.actions, a => a.ok === false ? "bad" : "", a => a.text);
  $("evts").innerHTML = list(s.events, e => URGENT.has(e.kind) ? "urgent" : "",
    e => (e.civ ? seat(e.civ) + ": " : "") + (e.text || e.kind));
  for (const w of VIEWS) $("v-" + w).classList.toggle("on", w === view);
  $("map").hidden = !started || view === "client";
  $("client").hidden = !started || view !== "client" || clientAt === null;
  if (!started) return;
  if (view === "client") return clientFrame();
  $("note").textContent = "";
  const url = `live/frame.png?turn=${s.turn}&view=${view}&game=${s.game}`;
  if (url !== mapUrl) $("map").src = mapUrl = url;
}

async function clientFrame() {
  if (clientBusy || clientAt === `${s.game}/${s.turn}`) return;
  clientBusy = true;
  try {
    const r = await fetch(`live/client.png?turn=${s.turn}&game=${s.game}`, {cache: "no-store"});
    if (!r.ok) {
      clientAt = null;
      $("client").hidden = true;
      $("note").textContent = await r.text();
      return;
    }
    const turn = +r.headers.get("X-OpenCiv3-Turn"), img = $("client"), old = img.src;
    img.src = URL.createObjectURL(await r.blob());
    if (old) URL.revokeObjectURL(old);
    clientAt = `${s.game}/${turn}`;
    img.hidden = view !== "client";
    $("note").textContent = turn < s.turn ? `The client shows turn ${turn}; it is drawing turn ${s.turn}.` : "";
  } finally {
    clientBusy = false;
  }
}

for (const w of VIEWS) {
  $("v-" + w).onclick = () => {
    view = w;
    history.replaceState(null, "", "#" + w);
    if (s) show();
  };
}

async function poll() {
  try {
    s = await (await fetch("live/state.json", {cache: "no-store"})).json();
    show();
  } catch (e) {
    $("sub").textContent = "Lost the env; retrying every 2 s";
  }
  setTimeout(poll, 2000);
}
poll();
</script></body></html>
""".replace("__URGENT__", json.dumps(sorted(recording.URGENT)))
