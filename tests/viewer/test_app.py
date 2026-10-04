"""The viewer app (app.js) in a real browser: headless Chrome or Chromium runs a page for a stretch of virtual time and
dumps its DOM, where a probe script has written what was on screen. Covers what the agents write (notes, plans and
messages, shown as text and never as markup) and the stream's broadcast layer (docs/viewer.md, section 5): the
director, its cards, the speech bubble, the chyron, captions with the casters' voices, and the notes ticker."""

import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import pairwise
from pathlib import Path

import pytest

from agentenv_openciv3 import recording, viewer
from agentenv_openciv3.matchdata import MatchData

FAKE = Path(__file__).resolve().parents[1] / "env" / "fake_bridge.py"
MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME = next((c for c in (os.environ.get("CHROME"), shutil.which("google-chrome"), shutil.which("chromium"),
                           shutil.which("chromium-browser"), MAC_CHROME) if c and Path(c).exists()), None)
pytestmark = pytest.mark.skipif(CHROME is None, reason="needs Chrome or Chromium")

NOTE = "Settling the river <b>before</b> Greece does."
PLAN = "Expand to 6 cities by T40, then <script>window.pwned = 1</script> take Athens."
MESSAGE = "Join me against Carthage, or you're next. <img src=x onerror=\"window.pwned = 1\">"
LIVE_MESSAGE = "Athens will be free again, Rome."


@pytest.fixture(scope="module")
def doc(tmp_path_factory) -> dict:
    """The fake bridge's game (T1 to T6) with two seats, opus (Rome) and sol (Greece), at war from T3, Rome taking
    Athens on T5; opus writes a note, a plan and a message during T2, both write notes during T4 and sol a message."""
    record = tmp_path_factory.mktemp("record")
    lines = [{"id": 1, "cmd": "new_game", "args": {"seed": 3, "turn_limit": 8}},
             {"id": 2, "cmd": "autoplay", "args": {"turns": 5, "policy": "settler_bot"}}]
    subprocess.run([sys.executable, str(FAKE), "--record", str(record)], check=True, capture_output=True, text=True,
                   timeout=60, input="".join(json.dumps(x) + "\n" for x in lines))
    snaps = recording.load_snapshots(record)
    for s in snaps:
        s["schema"] = 2
        s["seats"] = [{"index": 0, "civ": "Rome", "label": "opus"}, {"index": 1, "civ": "Greece", "label": "sol"}]
        for p in s["players"]:
            p["label"] = {"Rome": "opus", "Greece": "sol"}.get(p["civ"])
            p["at_war"] = [1 - p["index"]] if s["turn"] >= 3 and p["index"] < 2 else []
        for c in s["cities"]:
            if c["name"] == "Athens" and s["turn"] >= 5:
                c["owner"] = 0
    d = MatchData.from_snapshots(snaps).document()
    turns = {t["turn"]: t for t in d["turns"]}
    turns[3].update(notes={"0": NOTE}, plans={"0": PLAN}, messages=[{"from": 0, "to": [1], "text": MESSAGE}])
    turns[5].update(notes={"0": "Took Athens.", "1": "Lost Athens; walls everywhere now."},
                    messages=[{"from": 1, "to": "all", "text": "Rome broke the peace. Remember it."}])
    assert any(e["kind"] == "city_captured" for e in turns[5]["events"])
    return d


def probed(page: str, probe: str) -> str:
    return page.replace("</body>", f'<pre id="probe"></pre><script>{probe}</script></body>')


def run(url: str, budget_ms: int, tmp_path: Path):
    """What the page's probe wrote into #probe after `budget_ms` of virtual time, as JSON."""
    chrome = subprocess.Popen(
        [CHROME, "--headless", "--no-sandbox", "--disable-gpu", "--no-first-run", "--mute-audio",
         "--window-size=1920,1080", "--autoplay-policy=no-user-gesture-required",
         f"--user-data-dir={tmp_path / 'chrome'}", f"--virtual-time-budget={budget_ms}", "--dump-dom", url],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    watchdog = threading.Timer(90, chrome.kill)
    watchdog.start()
    dom = ""
    try:
        for line in chrome.stdout:   # some builds keep running after the dump: read up to its end
            dom += line
            if "</html>" in line:
                break
    finally:
        watchdog.cancel()
        chrome.kill()
        chrome.wait()
    found = re.search(r'<pre id="probe">(.*?)</pre>', dom, re.S)
    assert found, dom[-2000:]
    return json.loads(html.unescape(found[1]))


TEXT = 'const text = e => e ? e.innerText.replace(/\\s+/g, " ").trim() : null;'


def test_a_recording_shows_what_the_agents_wrote_as_text(doc, tmp_path):
    probe = TEXT + """
      const out = {}, rome = M.players.find(p => p.civ === "Rome").index;
      setTurn(M.turnIndex(2)); setFocus(rome);
      out.t2 = {card: text($("#agentcard")), msgs: $$("#msgs li").map(text), diplo: !$("#diplo").hidden};
      setTurn(M.turnIndex(6));
      out.t6 = {card: text($("#agentcard")), msgs: $$("#msgs li").map(text)};
      setFocus(null); setView("agents");
      out.grid = $$(".acard").map(c => text($(".think", c)));
      out.markup = $$("#app img, #app script, .mind .note :not(.t), .mind .tx *, #msgs .mt *").length;
      out.pwned = window.pwned ?? null;
      $("#probe").textContent = JSON.stringify(out);"""
    page = tmp_path / "recording.html"
    page.write_text(probed(viewer.page(doc), probe), encoding="utf-8")
    out = run(page.as_uri(), 1000, tmp_path)

    # turn 2: what opus wrote during it, and nothing from later turns
    assert f"“{NOTE}”" in out["t2"]["card"] and PLAN in out["t2"]["card"]
    assert out["t2"]["diplo"] and out["t2"]["msgs"] == [f"T2 opus → sol “{MESSAGE}”"]
    # turn 6: newest first, and the newest note
    assert out["t6"]["msgs"] == ["T4 sol → all “Rome broke the peace. Remember it.”", f"T2 opus → sol “{MESSAGE}”"]
    assert "“Took Athens.”" in out["t6"]["card"] and PLAN in out["t6"]["card"]
    assert "“Took Athens.”" in out["grid"][0] and PLAN in out["grid"][0]
    assert "“Lost Athens; walls everywhere now.”" in out["grid"][1] and "Took" not in out["grid"][1]
    assert out["markup"] == 0 and out["pwned"] is None


def wav(seconds: float) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0" * int(8000 * seconds))
    return buf.getvalue()


@pytest.fixture
def broadcast(doc):
    """A live env and a caster on one local server. The data shows T1 to T4 for its first three answers, then T5 and
    T6 (Athens taken, sol's live message, opus's note), and the game over from the 30th."""
    asked, lines = [], [
        {"id": 1, "speaker": "pbp", "name": "Max", "text": "<i>Opus</i> takes the field!", "audio": None,
         "seconds": 2.0, "turn": None, "focus": None, "kind": "intro"},
        {"id": 2, "speaker": "color", "name": "Ada", "text": "Greece is in trouble.", "audio": "audio/2.wav",
         "seconds": 1.5, "turn": 5, "focus": "Greece", "kind": "color"}]

    def data(since: int) -> dict:
        n = sum(p == "/live/data.json" for p in asked)
        last = 4 if n <= 3 else 6
        live = {"turn": last, "game_over": n >= 30, "victory": None, "client": False, "recording": True,
                "min_turn_seconds": 15, "messages": [], "seats": [
                    {"civ": "Rome", "label": "opus", "human": False, "ended": True, "seconds": 12.5,
                     "calls": {"ok": 9, "failed": 0}, "actions": [], "note": None, "plan": PLAN, "plan_turn": 2},
                    {"civ": "Greece", "label": "sol", "human": False, "ended": False, "seconds": 20.0,
                     "calls": {"ok": 4, "failed": 1}, "actions": [], "note": None, "plan": None, "plan_turn": None}]}
        if last == 6:
            live["seats"][0]["note"] = "Marching on Sparta."
            live["messages"] = [{"from": "Greece", "to": ["Rome"], "text": LIVE_MESSAGE, "seconds": 3.2}]
        out = {k: doc[k] for k in ("schema", "game", "meta", "players")}
        out["turns"] = [t for t in doc["turns"] if since < t["turn"] <= last]
        if since < 0:
            out["static"] = doc["static"]
        return {**out, "live": live}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, body: bytes, ctype: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            since = int(dict(urllib.parse.parse_qsl(url.query)).get("since", -1))
            asked.append(url.path)
            if url.path == "/live":
                self.reply(probed(viewer.page(), PROBE_STREAM).encode(), "text/html; charset=utf-8")
            elif url.path == "/live/data.json":
                self.reply(json.dumps(data(since)).encode(), "application/json")
            elif url.path == "/cast/cast.json":
                body = {"lines": [ln for ln in lines if ln["id"] > since], "speaking_until": time.time() + 60}
                self.reply(json.dumps(body).encode(), "application/json")
            elif url.path == "/cast/audio/2.wav":
                self.reply(wav(1.5), "audio/wav")
            else:
                self.send_error(404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", asked
    server.shutdown()


PROBE_STREAM = TEXT + """
  const frames = [], shown = sel => { const e = $(sel); return e && !e.hidden ? text(e) : null; };
  setInterval(() => {
    if (!$("#moment")) return;
    frames.push({t: Math.round(performance.now()), view: S.view, card: shown("#moment"), bubble: shown("#bubble"),
      chyron: shown("#chyron"), caption: shown("#caption"), ticker: shown("#ticker"),
      markup: $$(".titlecard h1 *, .caption p *, .bubble p *, .ticker .q *").length});
    $("#probe").textContent = JSON.stringify(frames);
  }, 250);"""


def test_the_stream_casts_a_live_game(broadcast, tmp_path):
    base, asked = broadcast
    query = urllib.parse.urlencode({"title": "<b>Battle</b> of the Labs", "cast": f"{base}/cast"})
    frames = run(f"{base}/live?stream&{query}", 60000, tmp_path)

    def first(key, *words):   # as shown: CSS may change the case
        return next((f for f in frames if f[key] and all(w.lower() in f[key].lower() for w in words)), None)

    title = first("card", "<b>Battle</b> of the Labs", "opus", "Rome", "sol", "Greece")
    assert title and title["t"] < 2000
    captured = first("chyron", "captured", "Athens")
    assert captured and captured["t"] > max(f["t"] for f in frames if f["card"] and "Battle" in f["card"])
    assert first("bubble", "sol", "→", "opus", LIVE_MESSAGE)
    assert first("caption", "Max", "play-by-play", "<i>Opus</i> takes the field!")
    assert first("caption", "Ada", "analyst", "Greece is in trouble.")
    assert "/cast/audio/2.wav" in asked
    assert first("ticker", "Agent notes", "opus:", "“Marching on Sparta.”")
    over = first("card", "game over")
    assert over and over["t"] > captured["t"] and frames[-1]["view"] == "summary"
    assert all(f["markup"] == 0 for f in frames)


@pytest.fixture(scope="module")
def later(tmp_path_factory):
    """The two-seat game with the later game's stories and the broadcast's names (Opus 5.5 for opus) on a local live
    server: T1-T3 first, then T4 (opus and sol meet; Rome enters the Middle Ages), T5 (the Pyramids in Veii), T6 (a
    trade between the two seats)."""
    record = tmp_path_factory.mktemp("record")
    lines = [{"id": 1, "cmd": "new_game", "args": {"seed": 3, "turn_limit": 8}},
             {"id": 2, "cmd": "autoplay", "args": {"turns": 5, "policy": "settler_bot"}}]
    subprocess.run([sys.executable, str(FAKE), "--record", str(record)], check=True, capture_output=True, text=True,
                   timeout=60, input="".join(json.dumps(x) + "\n" for x in lines))
    snaps = recording.load_snapshots(record)
    for s in snaps:
        t = s["turn"]
        s.update(schema=2, date=f"{4000 - 50 * t} BC", seats=[{"index": 0, "civ": "Rome", "label": "opus"},
                                                               {"index": 1, "civ": "Greece", "label": "sol"}])
        for p in s["players"]:
            p["label"] = {"Rome": "opus", "Greece": "sol"}.get(p["civ"])
            if p["index"] < 2:
                p["contacts"] = [1 - p["index"]] if t >= 4 else []
            p.update(culture=100 * t, era=int(p["index"] == 0 and t >= 4), land=0.1 + 0.02 * t, pop=0.2)
        for c in s["cities"]:
            c["wonders"] = ["The Pyramids"] if c["name"] == "Veii" and t >= 5 else []
        s["trades"] = [{"seq": 9, "turn": 6, "a": 0, "b": 1, "a_gave": "Bronze Working", "b_gave": "60 gold"}] \
            if t == 6 else []
    d = MatchData.from_snapshots(snaps, names={"opus": "Opus 5.5"}).document()
    asked = []

    def data(since: int) -> dict:
        n = sum(p == "/live/data.json" for p in asked)
        last = 3 if n <= 3 else 4 if n <= 8 else 5 if n <= 16 else 6
        live = {"turn": last, "game_over": False, "victory": None, "client": False, "recording": True,
                "min_turn_seconds": 15, "messages": [], "broadcast": {"title": "Sol <i>vs</i> Opus", "casters": None,
                                                                      "names": {"opus": "Opus 5.5"}},
                "seats": [{"civ": "Rome", "label": "opus", "human": False, "ended": False, "seconds": 42.0,
                           "calls": {"ok": 7, "failed": 1}, "note": None, "plan": None, "plan_turn": None,
                           "actions": [{"text": "c1 builds <b>Settler</b>", "ok": True}]},
                          {"civ": "Greece", "label": "sol", "human": False, "ended": True, "seconds": 31.0,
                           "calls": {"ok": 4, "failed": 0}, "actions": [], "note": None, "plan": None,
                           "plan_turn": None}]}
        out = {k: d[k] for k in ("schema", "game", "meta", "players")}
        out["turns"] = [t for t in d["turns"] if since < t["turn"] <= last]
        if since < 0:
            out["static"] = d["static"]
        return {**out, "live": live}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            asked.append(url.path)
            if url.path == "/live":
                body, ctype = probed(viewer.page(), PROBE_LATER).encode(), "text/html; charset=utf-8"
            elif url.path == "/live/data.json":
                since = int(dict(urllib.parse.parse_qsl(url.query)).get("since", -1))
                body, ctype = json.dumps(data(since)).encode(), "application/json"
            else:
                return self.send_error(404)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


PROBE_LATER = TEXT + """
  const frames = [], shown = sel => { const e = $(sel); return e && !e.hidden ? text(e) : null; };
  setInterval(() => {
    if (!$("#moment") || !M.ready) return;
    const recap = loopShot(6), div = document.createElement("div");   // the loop's recap slot, as the director asks
    div.innerHTML = raceCard();
    const race = text(div);
    div.innerHTML = recap.key === "recap" ? recapCard(recapEvents()) : "";
    frames.push({t: Math.round(performance.now()), card: shown("#moment"), chyron: shown("#chyron"),
      duel: shown("#duel"), brand: text($(".brand")), turnbig: text($("#turnbig")), recap: text(div),
      recapKey: recap.key, race,
      markup: $$("#duel .act *, .brand i, .moment .big *").length});
    $("#probe").textContent = JSON.stringify(frames);
  }, 250);"""


def test_the_stream_tells_the_later_games_stories(later, tmp_path):
    frames = run(f"{later}/live?stream", 100000, tmp_path)

    def first(key, *words):
        return next((f for f in frames if f[key] and all(w.lower() in f[key].lower() for w in words)), None)

    # the broadcast's title and the seats' names; the year by the turn
    assert first("brand", "Sol <i>vs</i> Opus") and first("turnbig", "3800 BC", "T4")
    # the score bug: both seats head to head, with the race and their turns as they go
    bug = first("duel", "Opus 5.5", "Rome", "sol", "Greece", "3800 BC", "turn 4 of 8", "Land", "People")
    assert bug and "thinking · 0:42 · 8 calls" in bug["duel"] and "c1 builds <b>Settler</b>" in bug["duel"]
    assert "turn ended · 0:31" in bug["duel"]
    # the stories: the two seats meet, Rome's new era, its wonder, and their trade, each a card
    meet = first("card", "Opus 5.5", "Meet", "sol", "First contact: Opus 5.5 meets sol")
    era = first("card", "Opus 5.5 enters", "Middle Ages")
    wonder = first("card", "Opus 5.5 completes a wonder", "The Pyramids", "Opus 5.5 completed The Pyramids in Veii")
    trade = first("card", "Trade", "gives Bronze Working", "gives 60 gold")
    assert meet and era and wonder and trade, [f["card"] for f in frames if f["card"]]
    assert meet["t"] < wonder["t"] < trade["t"]
    assert first("chyron", "wonder", "The Pyramids in Veii")   # then the camera on the city
    # the race card (each seat against each victory); and the loop's recap of the story so far, once three stories
    # have happened (not before)
    assert first("race", "The race", "Opus 5.5", "Rome", "sol", "Greece", "Land", "People", "two thirds")
    assert frames[0]["recapKey"] != "recap"
    assert first("recap", "The story so far", "Middle Ages", "The Pyramids", "trade")
    assert all(f["markup"] == 0 for f in frames)


def test_the_director_holds_shots_by_priority_and_skips_the_backlog(doc, tmp_path):
    probe = """
      const loop = () => ({key: "loop", prio: 9, ms: 15000});
      const shot = (key, prio, ms, more = {}) => ({key, prio, ms, ...more});
      const key = s => s ? s.key : null, out = {};
      let d = new Director(loop);
      d.add(shot("msg", 5, 7000), 0); d.add(shot("war", 2, 10000, {card: true}), 0);
      out.order = [d.next(0), d.next(5000), d.next(10000), d.next(17000)].map(key);
      d = new Director(loop); d.next(0); d.add(shot("war", 2, 10000, {card: true}), 1000);
      out.dwell = [d.next(5999), d.next(6000)].map(key);
      d = new Director(loop); d.add(shot("a", 2, 4000, {card: true}), 0); d.add(shot("b", 2, 4000, {card: true}), 0);
      out.gap = [d.next(0), d.next(4000), d.next(7999), d.next(8000)].map(key);
      d = new Director(loop); d.add(shot("over", 0, 60000), 0); d.add(shot("msg", 5, 7000), 0);
      out.stale = [d.next(0), d.next(60000)].map(key);
      d = new Director(loop);
      out.dedupe = [d.add(shot("x", 5, 1000), 0), d.add(shot("x", 5, 1000), 0)];
      d = new Director(loop); d.add(shot("msg", 5, 7000), 0); d.next(0); d.add(shot("cast", 6, 4000), 100);
      out.lower = [d.next(6500), d.next(7000)].map(key);
      d = new Director(loop); d.add(shot("war", 2, 10000), 0); d.next(0);
      d.add(shot("city", 7, 6000, {expires: 4000}), 0);
      out.expired = key(d.next(10000));
      d = new Director(loop); d.add(shot("a1", 5, 7000, {group: "a"}), 0); d.add(shot("b1", 5, 7000, {group: "b"}), 0);
      d.add(shot("a2", 5, 7000, {group: "a"}), 1000);
      out.grouped = [d.next(1000), d.next(8000), d.next(15000)].map(key);
      let still = true;
      d = new Director(loop); d.add(shot("lead", 3, 10000, {valid: () => still}), 0); still = false;
      out.invalid = key(d.next(0));
      Object.assign(bc, {leader: 0, leadAt: -Infinity});
      M.leaders[M.last - 1] = 0; M.leaders[M.last] = 1;
      out.lead = [newLeader(0)];
      M.leaders[M.last - 1] = 1;
      out.lead.push(newLeader(0));
      bc.leadAt = -10000;
      out.lead.push(newLeader(0));
      const lines = [1, 2, 3].map(id => ({id, seconds: 4}));
      const now = Date.now() / 1000;
      out.backlog = [stillOn(lines, now + 6), stillOn(lines, now - 1)].map(l => l.map(x => x.id));
      $("#probe").textContent = JSON.stringify(out);"""
    page = tmp_path / "director.html"
    page.write_text(probed(viewer.page(doc), probe), encoding="utf-8")
    out = run(page.as_uri(), 500, tmp_path)
    assert out["order"] == ["war", None, "msg", "loop"]          # most important first; each holds its length
    assert out["dwell"] == [None, "war"]                          # a more important shot cuts in after the dwell
    assert out["gap"] == ["a", None, None, "b"]                   # full-screen cards at least 8 s apart
    assert out["stale"] == ["over", "loop"]                       # a shot queued over 45 s ago is dropped
    assert out["dedupe"] == [True, False]
    assert out["lower"] == [None, "cast"]                         # a less important shot waits its turn
    assert out["expired"] == "loop"                               # a shot can go stale sooner (a new city)
    assert out["grouped"] == ["a2", "b1", "loop"]                 # a sender's newer message takes the older's place
    assert out["invalid"] == "loop"                               # a lead lost while its card waited is not shown
    assert out["lead"] == [None, 1, None]                         # a lead held two turns, one card every 45 s
    assert out["backlog"] == [[2, 3], []]                         # a page opened mid-cast skips the said lines


def test_the_director_shares_a_busy_broadcast(doc, tmp_path):
    """Three minutes with a caster's line about a civ every 6 s and, from 20 s to 140 s, a message every 2 s from one
    of three senders: the loop keeps getting the screen (while the bubbles flood in, its wide shots: the whole map and
    every agent's panel), its spotlights go where the casters look, each bubble shows its sender's newest message, and
    no shot is cut short."""
    probe = """
      const civs = M.civs.slice(0, 3).map(p => p.index), d = new Director(loopShot), cuts = [], newest = {};
      for (let t = 0; t <= 180000; t += 250) {
        if (t % 6000 === 0) d.steer(civs[t / 6000 % 2], t + 6000);
        if (t >= 20000 && t < 140000 && t % 2000 === 0) {
          const from = civs[t / 2000 % 3];
          newest[from] = t;
          d.add({key: "m" + t, group: "msg:" + from, prio: MINOR, ms: 7000, focus: from, sent: t}, t);
        }
        const s = d.next(t);
        if (s) cuts.push({t, key: s.key, focus: s.focus, wish: d.wish.focus,
                          newest: s.sent == null || s.sent === newest[s.focus]});
      }
      $("#probe").textContent = JSON.stringify(cuts);"""
    page = tmp_path / "busy.html"
    page.write_text(probed(viewer.page(doc), probe), encoding="utf-8")
    cuts = run(page.as_uri(), 500, tmp_path)
    loop = [c for c in cuts if c["key"] in ("overview", "spotlight", "agents")]
    gone = {a["t"]: b["t"] for a, b in pairwise(cuts)}   # when each shot left the screen
    assert {c["key"] for c in loop if 20000 <= c["t"] < 140000} == {"overview", "agents"}, cuts   # the wide shots
    assert all(b["t"] - gone[a["t"]] <= 37000 for a, b in pairwise(loop)), cuts   # off the screen 37 s at most
    assert all(c["focus"] == c["wish"] for c in cuts if c["key"] == "spotlight"), cuts
    assert sum(c["key"].startswith("m") for c in cuts) >= 8 and all(c["newest"] for c in cuts), cuts
    assert all(b["t"] - a["t"] >= 6000 for a, b in pairwise(cuts)), cuts


def test_the_ticker_goes_round_every_seat(doc, tmp_path):
    """Nine seats writing a note every turn, a 15 s turn and the ticker moving every 5 s: every seat in nine ticks."""
    probe = """
      const seats = Array.from({length: 9}, (_, k) => ({index: 20 + k, seat: k})), shown = new Map(), out = {};
      const pick = (turn, fresh = -1) => nextNote(seats.map(p => ({p, n: {turn: p.seat === fresh ? turn + 1 : turn,
        text: "note"}})), shown).p.seat;
      out.busy = Array.from({length: 36}, (_, tick) => pick(Math.floor(tick / 3)));
      out.quiet = Array.from({length: 12}, () => pick(99));
      out.fresh = [pick(99, 7), pick(99, 7)];
      $("#probe").textContent = JSON.stringify(out);"""
    page = tmp_path / "ticker.html"
    page.write_text(probed(viewer.page(doc), probe), encoding="utf-8")
    out = run(page.as_uri(), 500, tmp_path)
    assert all(sorted(out["busy"][k:k + 9]) == list(range(9)) for k in range(0, 28)), out["busy"]
    assert all(sorted(out["quiet"][k:k + 9]) == list(range(9)) for k in range(0, 4)), out["quiet"]
    assert out["fresh"][0] == 7 and out["fresh"][1] != 7                # a new note goes first, then the round goes on


def fake_art(root: Path, monkeypatch) -> Path:
    """The client's art converted for the browser (webart.py), from test_webart's small stand-in for the C7 tree: one
    unit, the Warrior."""
    import importlib.util

    from agentenv_openciv3 import webart
    where = Path(__file__).resolve().parents[1] / "env" / "test_webart.py"
    spec = importlib.util.spec_from_file_location("test_webart", where)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(webart, "UNIT_ART", {"Warrior": "Tribal Mediterranean Warrior"})
    out = root / "webart"
    webart.convert(mod.client_tree(root), out)
    return out


PROBE_ART = """
  const errors = [], samples = [];
  addEventListener("error", e => errors.push(String(e.message)));
  setInterval(() => {
    if (M.ready) paint();   // a frame now: headless virtual time draws few of its own
    samples.push({t: Math.round(performance.now()), art: !!VA, on: artOn(), ti: S.ti, last: M.ready ? M.last : -1,
      walking: VA ? VA.drawn.walking : 0, fights: VA ? VA.drawn.fights : 0, units: VA ? VA.drawn.units : 0,
      chips: $$("#layers .chip").map(b => b.textContent + (b.classList.contains("on") ? "+" : ""))});
    if (samples.length === 150) setArt(false);
    $("#probe").textContent = JSON.stringify({samples, errors});
  }, 100);"""


def test_the_live_view_draws_the_client_art_and_plays_a_turn_out(doc, tmp_path, monkeypatch):
    """With the env's art (GET /play/art/), the live view draws the map in the client's art; a turn that arrives plays
    out on it: a unit walks the path it took (patches/0012) and the turn's battle plays (patches/0010). T (here
    setArt) goes back to the plain map. A recording never loads the art."""
    import copy

    d = copy.deepcopy(doc)
    last = d["turns"][-1]
    warrior = d["meta"]["unit_types"].index("Warrior")
    uid, x, y = next((u[0], u[1], u[2]) for u in last["units"] if u[4] == warrior)
    last["moves"] = [[90, uid, 0, warrior, 1, x + 2, y, x + 1, y + 1, x, y]]
    side = [0, warrior, x, y, 3, 2, 3]
    last["battles"] = [[91, 0, "a", "adaa", 0, 1, *side, 1, warrior, x + 2, y, 3, 0, 3]]
    art = fake_art(tmp_path, monkeypatch)
    asked, polls = [], [0]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, body: bytes, ctype: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            asked.append(url.path)
            if url.path == "/live":
                self.reply(probed(viewer.page(), PROBE_ART).encode(), "text/html; charset=utf-8")
            elif url.path == "/live/data.json":
                since = int(dict(urllib.parse.parse_qsl(url.query)).get("since", -1))
                polls[0] += 1
                upto = d["turns"][-1]["turn"] if polls[0] > 3 else d["turns"][-2]["turn"]
                out = {k: d[k] for k in ("schema", "game", "meta", "players")}
                out["turns"] = [t for t in d["turns"] if since < t["turn"] <= upto]
                if since < 0:
                    out["static"] = d["static"]
                out["live"] = {"turn": upto, "game_over": False, "client": False, "recording": True,
                               "min_turn_seconds": 0, "messages": [], "seats": []}
                self.reply(json.dumps(out).encode(), "application/json")
            elif url.path.startswith("/play/art/") and (art / url.path[len("/play/art/"):]).is_file():
                path = art / url.path[len("/play/art/"):]
                self.reply(path.read_bytes(), "font/ttf" if path.suffix == ".ttf" else
                           "application/json" if path.suffix == ".json" else "image/png")
            else:
                self.send_error(404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        out = run(f"http://127.0.0.1:{server.server_port}/live", 30000, tmp_path)
    finally:
        server.shutdown()
    samples = out["samples"]
    assert not out["errors"], out["errors"]
    assert "/play/art/manifest.json" in asked
    on = [s for s in samples if s["on"]]
    assert on and on[0]["chips"][0] == "Art+" and "Territory" not in on[0]["chips"] and any(s["units"] for s in on)
    arrived = next(k for k, s in enumerate(samples) if s["ti"] == s["last"] and s["last"] == len(d["turns"]) - 1)
    after = samples[arrived:150]
    assert any(s["walking"] for s in after), "the warrior walked its path"
    assert any(s["fights"] for s in after), "the battle played"
    assert not samples[-1]["on"] and samples[-1]["chips"][0] == "Art" and "Territory+" in samples[-1]["chips"]

    # A recording carries no art: it never asks for it, and draws the plain map.
    page = tmp_path / "recording.html"
    page.write_text(probed(viewer.page(d), "$('#probe').textContent = JSON.stringify({va: VA, kit: typeof ArtKit});"),
                    encoding="utf-8")
    assert run(page.as_uri(), 3000, tmp_path) == {"va": None, "kit": "object"}
