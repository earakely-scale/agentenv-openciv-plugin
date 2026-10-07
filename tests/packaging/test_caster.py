"""The casters (streamer/caster.py) against a fake match and a fake LiteLLM that speaks the tool-calling protocol:
what they say and when, a line at a time, how each caster looks the match up before it speaks and the analyst
researches in the background, how they pace themselves to their audio, what they serve the stream page, and that
failures neither stop them nor print the key."""

import array
import codecs
import importlib.util
import json
import math
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

CASTER = Path(__file__).resolve().parents[2] / "streamer" / "caster.py"
KEY = "sk-test-not-for-printing-123"
WAV_SECONDS = 2.0


def _load():
    spec = importlib.util.spec_from_file_location("caster", CASTER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module   # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


caster = _load()


def streamed_wav(seconds: float = WAV_SECONDS, rate: int = 8000) -> bytes:
    """Silence as a text-to-speech API streams it: 16-bit mono, the RIFF and data sizes left at 0xFFFFFFFF."""
    fmt = (1).to_bytes(2, "little") + (1).to_bytes(2, "little") + rate.to_bytes(4, "little") + \
        (rate * 2).to_bytes(4, "little") + (2).to_bytes(2, "little") + (16).to_bytes(2, "little")
    unknown = b"\xff\xff\xff\xff"
    return b"RIFF" + unknown + b"WAVE" + b"fmt " + (16).to_bytes(4, "little") + fmt + b"data" + unknown + \
        bytes(int(seconds * rate * 2))


def entry(turn: int, *, events=(), scores=None, at_war=(), **extra) -> dict:
    return {"turn": turn, "owners": [], "known": [],
            "cities": [[10, 12, "Roma", 1, 3, 1, "Warrior", 0], [30, 8, "Athens", 2, 2, 1, None, 1]],
            "units": [[0, 10, 12, 1, 0], [1, 11, 12, 1, 0], [2, 30, 8, 2, 1]],
            "scores": scores or {"1": [40, 1, 3, 9, 2, 0], "2": [30, 1, 2, 7, 1, 0]},
            "stats": {"1": {"gold": 1300, "government": "Despotism", "research": "Bronze Working",
                            "at_war": list(at_war)},
                      "2": {"gold": 12, "government": "Monarchy", "research": None, "at_war": []}},
            "events": list(events), "actions": {}, "calls": {"1": {"ok": 5, "failed": 1}, "2": {"ok": 4, "failed": 0}},
            **extra}


def event(kind: str, owner: int, text: str, **at) -> dict:
    return {"kind": kind, "owner": owner, "text": text, "source": "derived", **at}


def say(text: str, focus=None, ident: str = "call_say") -> dict:
    """A reply that speaks a line."""
    return calls(("say", {"text": text, "focus": focus}), ident=ident)


def calls(*made, ident: str = "call") -> dict:
    """A reply with tool calls: (name, arguments) each, the arguments an object or the raw JSON text."""
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"{ident}_{n}", "type": "function",
         "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}}
        for n, (name, args) in enumerate(made)]}


def tools_of(body: dict) -> set[str]:
    return {t["function"]["name"] for t in body.get("tools") or ()}


class Fake:
    """One server for both: the env's match data (GET /live/data.json) and LiteLLM (chat and speech)."""

    def __init__(self):
        self.players = [{"index": 0, "civ": "Barbarians", "label": None, "barbarian": True, "seat": None},
                        {"index": 1, "civ": "Rome", "label": "claude-opus", "barbarian": False, "seat": 0},
                        {"index": 2, "civ": "Greece", "label": "gpt-sol", "barbarian": False, "seat": 1}]
        self.meta = {"turn_limit": 50, "unit_types": ["Warrior", "Settler"], "civilian": ["Settler"],
                     "victory": None}
        self.game: str | None = None
        self.turns: list[dict] = []
        self.live: dict = {"turn": None, "game_over": False, "victory": None, "seats": []}
        self.data_down = False
        self.prompts: list[str] = []   # the user prompt of each line's first call (and each research session's)
        self.chats: list[dict] = []    # every chat request
        self.auth: list[str] = []
        self.speech: list[dict] = []
        self.chat_failures = self.speech_failures = 0
        self.reject_tools = False      # an endpoint that turns tool calls down
        self.reject_forced = False     # one that takes tools, but not a forced call
        self.bad_requests: list[bytes] = []   # 400s to give first, in order
        self.script: list = []         # replies (or functions of the request) to give first, in order
        self.content: str | None = None    # a plain-text reply instead of a say call
        self.said = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def start(self, turns: list[dict], turn: int, **live) -> None:
        self.game, self.turns = "g-1", turns
        self.live = {"turn": turn, "game_over": False, "victory": None, "seats": [
            {"civ": "Rome", "label": "claude-opus", "ended": False, "seconds": 41.2, "calls": {"ok": 3, "failed": 1},
             "actions": [{"text": "c1 builds Settler", "ok": True}, {"text": "u4 settle", "ok": False}]},
            {"civ": "Greece", "label": "gpt-sol", "ended": True, "seconds": 9.0, "calls": {"ok": 7, "failed": 0},
             "actions": []}], **live}

    def chat(self, body: dict) -> dict:
        self.chats.append(body)
        messages = body["messages"]
        if len(messages) == 2:
            self.prompts.append(messages[1]["content"])
        if self.script:
            step = self.script.pop(0)
            return step(body) if callable(step) else step
        if self.content is not None:
            return {"role": "assistant", "content": self.content}
        if "jot" in tools_of(body):
            return {"role": "assistant", "content": "Nothing worth jotting."}
        self.said += 1
        text, focus = f"Line {self.said}: Rome leads.", "claude-opus" if self.said % 2 else "Atlantis"
        if "tools" not in body:
            return {"role": "assistant", "content": json.dumps({"text": text, "focus": focus})}
        return say(text, focus)

    def handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if fake.data_down or not self.path.startswith("/live/data.json"):
                    return self.reply(503, b"down", "text/plain")
                since = int(self.path.split("since=")[1])
                doc = {"schema": 1, "game": fake.game, "players": fake.players if fake.game else [],
                       "meta": fake.meta, "turns": [t for t in fake.turns if t["turn"] > since], "live": fake.live}
                self.reply(200, json.dumps(doc).encode(), "application/json")

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.auth.append(self.headers["Authorization"])
                if self.path == "/v1/chat/completions":
                    if fake.chat_failures:
                        fake.chat_failures -= 1
                        return self.reply(500, f"bad key {KEY}".encode(), "text/plain")
                    if fake.bad_requests:
                        fake.chats.append(body)
                        return self.reply(400, fake.bad_requests.pop(0), "text/plain")
                    if fake.reject_tools and "tools" in body:
                        fake.chats.append(body)
                        return self.reply(400, b'{"error": "this model does not support tools"}', "text/plain")
                    if fake.reject_forced and "tool_choice" in body:
                        fake.chats.append(body)
                        return self.reply(400, b'{"error": "tool_choice is not supported with thinking"}',
                                          "text/plain")
                    reply = {"choices": [{"message": fake.chat(body), "finish_reason": "stop"}]}
                    return self.reply(200, json.dumps(reply).encode(), "application/json")
                fake.speech.append(body)
                if fake.speech_failures:
                    fake.speech_failures -= 1
                    return self.reply(429, b"slow down", "text/plain")
                self.reply(200, streamed_wav(), "audio/wav")

            def reply(self, status, body, content_type):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        return Handler


@pytest.fixture
def fake():
    f = Fake()
    thread = threading.Thread(target=f.server.serve_forever, daemon=True)
    thread.start()
    yield f
    f.server.shutdown()


SPEAKERS = (caster.PBP, caster.COLOR)

class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def new_caster(fake: Fake, clock: Clock, **kw):
    return caster.Caster(f"{fake.url}/live?stream", fake.url + "/v1/", KEY, model="anthropic/claude-haiku-4-5",
                         tts_model="openai/gpt-4o-mini-tts", clock=clock, **kw)


def until_quiet(c, clock: Clock) -> None:
    """Moves the clock to when the next line is due: less than LEAD_SECONDS of speech left."""
    clock.now = max(clock.now + caster.POLL_SECONDS, c.speaking_until - caster.LEAD_SECONDS + 0.1)


def beat(c, clock: Clock) -> list[dict]:
    """Runs the beat under way, or the next one, to its end; returns the lines it said."""
    said = len(c.lines)
    for _ in range(caster.MAX_LINES + 1):
        until_quiet(c, clock)
        c.tick()
        if c.beat is not None and c.beat.done and len(c.lines) > said:
            break
    return c.lines[said:]


def now_part(prompt: str) -> str:
    return prompt.split("NOW\n")[1]


def test_the_casters_open_call_the_events_and_sign_off(fake):
    clock = Clock()
    c = new_caster(fake, clock, title="Battle of the Labs")
    c.tick()
    assert c.lines == [] and fake.prompts == []   # no game yet

    fake.start([entry(1), entry(2)], turn=2)
    intro = beat(c, clock)
    assert [line["kind"] for line in intro] == ["intro"] * 3
    assert [(line["speaker"], line["name"]) for line in intro] == [("pbp", "Max"), ("color", "Ada"), ("pbp", "Max")]
    assert intro[0] == {"id": 1, "speaker": "pbp", "name": "Max", "text": "Line 1: Rome leads.",
                        "audio": "audio/1.wav", "seconds": WAV_SECONDS, "turn": 2, "focus": "Rome", "kind": "intro"}
    assert intro[1]["focus"] is None   # not a civ in this game
    first, second, third = fake.prompts[:3]
    assert '"Battle of the Labs"' in first and "claude-opus (Rome), gpt-sol (Greece)" in first
    assert "1. claude-opus (Rome): 40" in first and "1300 gold" in first and "2 military units" in first
    assert "still thinking, 41 s so far; 4 tool calls, 1 failed" in first
    assert "YOUR LINE: you are Max, line 1 of 3, 6 to 16 words. You open this exchange." in first
    # a line at a time, each caster answering the other's actual line
    assert 'YOUR LINE: you are Ada, line 2 of 3, at most 22 words. Max just said: "Line 1: Rome leads."' in second
    assert 'Ada just said: "Line 2: Rome leads."\nYours is the last line of this exchange: answer Ada' in third
    systems = [b["messages"][0]["content"] for b in fake.chats[:3]]
    assert systems[0].startswith("You are Max, the play-by-play caster") and systems[0] == systems[2]
    assert systems[1].startswith("You are Ada, the colour analyst caster")
    assert all(tools_of(b) == {"say"} for b in fake.chats[:3])   # the opening: nothing queued, it must speak
    assert [s["voice"] for s in fake.speech] == ["ash", "sage", "ash"]
    assert {s["response_format"] for s in fake.speech} == {"wav"}
    assert set(fake.auth) == {f"Bearer {KEY}"}

    fake.turns.append(entry(3, at_war=[2], events=[
        event("war_declared", 1, "claude-opus and gpt-sol are at war", **{"from": 2}),
        event("city_captured", 1, "claude-opus took Athens from gpt-sol", x=30, y=8, **{"from": 2}),
        event("tech_learned", 2, "gpt-sol learned Pottery")]))
    fake.live["turn"] = 3
    asked = len(fake.prompts)
    called = beat(c, clock)
    said = now_part(fake.prompts[asked])
    assert [line["kind"] for line in called] == ["event"] * 3
    assert said.index("claude-opus took Athens from gpt-sol (size 2)") < said.index("are at war")
    assert "Pottery" not in said   # not worth a call, but in the DATA
    assert "gpt-sol learned Pottery" in fake.prompts[asked]
    assert "at war with gpt-sol (Greece) since turn 3" in fake.prompts[asked]

    asked = len(fake.prompts)
    assert [line["kind"] for line in beat(c, clock)] == ["color"] * 2   # the events were called once
    assert "you are Ada, line 1 of 2, at most 22 words" in fake.prompts[asked]
    assert "The angle: the race at the top" in fake.prompts[asked]
    assert "Line 4: Rome leads." in fake.prompts[asked].split("RECENT LINES")[1]   # it remembers what it said

    fake.turns.append(entry(4, scores={"1": [61, 2, 5, 12, 3, 0], "2": [20, 0, 0, 0, 1, 1]}))
    fake.live.update(turn=4, game_over=True)
    clock.now += caster.POLL_SECONDS
    for _ in range(3):
        c.tick()   # at once, line after line: the stream ends a minute after GAME OVER
    assert [line["kind"] for line in c.lines[-3:]] == ["outro"] * 3 and c.finished
    assert c.speaking_until - clock.now > caster.LEAD_SECONDS + 2 * WAV_SECONDS   # however much was queued
    assert "The game is over: turn 4: claude-opus (Rome) wins on score with 61, ahead of gpt-sol (Greece) on 20" in (
        fake.prompts[-1])
    said = len(c.lines)
    for _ in range(3):
        until_quiet(c, clock)
        c.tick()
    assert len(c.lines) == said   # it stops talking


def test_the_task_names_the_casters_their_voices_and_models(fake):
    clock = Clock()
    config = {"model": "openai/gpt-5.6-luna", "tts_model": "openai/tts-2", "play_by_play": {"name": "Rex"},
              "analyst": {"name": "Iris", "voice": "coral", "style": "A dry, unhurried analyst.",
                          "model": "anthropic/claude-sonnet-5-5"}}
    casters = caster.casters_of(config)
    assert casters == {"pbp": ("Rex", "ash", caster.CASTERS["pbp"][2]),
                       "color": ("Iris", "coral", "A dry, unhurried analyst.")}
    assert caster.casters_of({}) == caster.CASTERS
    models = caster.models_of(config)
    assert models == {"pbp": "openai/gpt-5.6-luna", "color": "anthropic/claude-sonnet-5-5"}
    assert caster.models_of({}) == {"pbp": caster.MODEL, "color": caster.MODEL}
    c = caster.Caster(f"{fake.url}/live", fake.url, KEY, model="openai/gpt-5.6-luna", tts_model="openai/tts-2",
                      casters=casters, models=models, clock=clock)
    fake.start([entry(1)], turn=1)
    beat(c, clock)
    beat(c, clock)
    assert [(line["speaker"], line["name"]) for line in c.lines] == [
        ("pbp", "Rex"), ("color", "Iris"), ("pbp", "Rex"), ("color", "Iris"), ("pbp", "Rex")]
    pbp, analyst = (b["messages"][0]["content"] for b in fake.chats[:2])
    for system in (pbp, analyst):
        assert "- Rex, play-by-play" in system and "- Iris, colour analyst" in system
        assert 'Rex says "Iris", Iris says "Rex"' in system and not re.search(r"\b(Max|Ada)\b", system)
    assert pbp.startswith("You are Rex,") and analyst.startswith("You are Iris,")
    assert "Rex welcomes everyone" in fake.prompts[0] and "Iris opens with a sharp observation" in fake.prompts[3]
    assert [b["model"] for b in fake.chats] == ["openai/gpt-5.6-luna", "anthropic/claude-sonnet-5-5"] * 2 + [
        "openai/gpt-5.6-luna"]   # each writes its own lines
    assert not any("temperature" in b for b in fake.chats)   # some models take only their own
    assert {s["model"] for s in fake.speech} == {"openai/tts-2"}
    assert [(s["voice"], s["instructions"]) for s in fake.speech[:2]] == [
        ("ash", caster.CASTERS["pbp"][2]), ("coral", "A dry, unhurried analyst.")]
    reply = json.dumps({"lines": [{"speaker": "Iris", "text": "Iris: Rome leads, Iris.", "focus": None},
                                  {"speaker": "Max", "text": "Not a caster here."}]})
    assert [(line["speaker"], line["text"]) for line in caster.parse_lines(reply, c.match, casters)] == [
        ("color", "Rome leads, Rex.")]


def test_the_casters_pace_themselves_to_their_audio(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(1)], turn=1)
    c.tick()
    assert len(fake.prompts) == 1 and len(c.lines) == 1   # the page plays the first line as the next is written
    assert c.speaking_until == pytest.approx(clock.now + WAV_SECONDS + caster.GAP_SECONDS)
    c.tick()
    assert len(c.lines) == 2   # due at once: less than LEAD_SECONDS of speech left
    assert c.speaking_until == pytest.approx(clock.now + 2 * (WAV_SECONDS + caster.GAP_SECONDS))
    c.tick()
    assert len(c.lines) == 2   # plenty is queued
    clock.now = c.speaking_until - caster.LEAD_SECONDS - 0.5
    c.tick()
    assert len(fake.prompts) == 2
    clock.now += 1
    c.tick()
    assert len(fake.prompts) == 3
    assert c.speaking_until == pytest.approx(clock.now + caster.LEAD_SECONDS - 0.5 + WAV_SECONDS + caster.GAP_SECONDS)
    c.writing_seconds = dict.fromkeys(SPEAKERS, 3)   # a line that takes 3 s to write and voice starts that early
    clock.now = c.speaking_until - caster.LEAD_SECONDS - 2.5
    c.tick()
    assert len(fake.prompts) == 4 and c.lines[3]["kind"] == "color"


def test_the_page_gets_the_lines_and_their_audio_from_anywhere(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(1)], turn=1)
    beat(c, clock)
    server = ThreadingHTTPServer(("127.0.0.1", 0), caster.handler(c))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(f"{base}/cast.json?since=1") as r:
            assert r.headers["Access-Control-Allow-Origin"] == "*"
            body = json.load(r)
        assert [line["id"] for line in body["lines"]] == [2, 3]
        assert body["speaking_until"] == pytest.approx(c.speaking_until, abs=0.01)
        with urllib.request.urlopen(f"{base}/audio/2.wav") as r:
            wav = r.read()
            assert r.headers["Content-Type"] == "audio/wav" and r.headers["Access-Control-Allow-Origin"] == "*"
        assert int.from_bytes(wav[4:8], "little") == len(wav) - 8   # the streamed sizes are filled in
        assert int.from_bytes(wav[40:44], "little") == len(wav) - 44
        with urllib.request.urlopen(f"{base}/health") as r:
            assert r.read() == b"ok" and r.headers["Access-Control-Allow-Origin"] == "*"
        for path, status in (("/audio/9.wav", 404), ("/cast.json?since=x", 400)):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + path)
            assert e.value.code == status and e.value.headers["Access-Control-Allow-Origin"] == "*"
    finally:
        server.shutdown()


def test_failures_are_survived_and_the_key_is_never_printed(fake, capsys):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.data_down = True
    c.tick()
    clock.now += caster.POLL_SECONDS
    c.tick()
    fake.data_down = False
    fake.start([entry(1)], turn=1)
    fake.chat_failures = 1
    until_quiet(c, clock)
    c.tick()
    assert c.lines == [] and len(fake.prompts) == 0
    clock.now += 1
    c.tick()
    assert len(fake.auth) == 1   # it waits before retrying
    clock.now += caster.RETRY_SECONDS
    fake.speech_failures = 6   # a line's call and its twin
    for _ in range(3):
        c.tick()
        until_quiet(c, clock)
    assert [line["audio"] for line in c.lines] == [None, None, None]   # captions without a voice
    assert c.lines[0]["seconds"] == pytest.approx(4 / caster.WORDS_PER_SECOND)

    for junk in ("", '{"lines": [{"speaker": "narrator", "text": "Hi"}]}', '{"mood": "great"}', "Max: Not mine."):
        fake.content = junk   # nothing at all (all of it spent thinking), nobody's line, no line, the other's line
        until_quiet(c, clock)
        c.tick()
        clock.now += caster.RETRY_SECONDS
    assert len(c.lines) == 3
    assert c.beat.topic == "economy" and c.topics_used["race"]   # a beat that keeps failing gives way to the next
    fake.content = None
    fake.script = [say("   "), say("*nods*")]   # a say with nothing to say: the caster hears why and tries again
    until_quiet(c, clock)
    c.tick()
    assert len(c.lines) == 4 and c.lines[3]["audio"] == "audio/4.wav"
    assert [m["content"].split("\n")[0] for m in fake.chats[-1]["messages"] if m["role"] == "tool"] == [
        "error: an empty line; call say with your line as its text"] * 2
    fake.data_down = True   # the env is gone: nothing to talk about
    until_quiet(c, clock)
    c.tick()
    assert len(c.lines) == 4
    out = capsys.readouterr()
    printed = out.out + out.err
    assert printed.count("no match data") == 2   # once each time it goes away
    assert "HTTP 500 bad key <cast key>" in printed and "HTTP 429 slow down" in printed
    assert "the reply has no text (finish_reason stop)" in printed
    assert "no line in the reply" in printed
    assert KEY not in printed


def test_messages_and_notes_are_read_out_between_the_analysis(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(1)], turn=1)
    beat(c, clock)
    fake.turns.append(entry(2, messages=[{"from": 1, "to": [2], "text": "Join me against Carthage, or you're next."}],
                            notes={"1": "Settling the river before Greece does."},
                            plans={"2": "Six cities by turn 40, then Monarchy."}))
    fake.live.update(turn=2, messages=[{"from": "Greece", "to": "all", "text": "Rome is lying.", "seconds": 3.1}])
    fake.live["seats"][1].update(note="Walls first.", plan="Walls, then Monarchy.", plan_turn=2)
    kinds, firsts = [], []
    for _ in range(4):
        firsts.append(len(fake.prompts))
        kinds.append(beat(c, clock)[-1]["kind"])
    assert kinds == ["message", "color", "message", "color"]
    said = now_part(fake.prompts[firsts[0]])
    assert 'turn 1, claude-opus (Rome) to gpt-sol (Greece): "Join me against Carthage, or you\'re next."' in said
    assert 'turn 2, gpt-sol (Greece) to everyone: "Rome is lying."' in said
    notes = now_part(fake.prompts[firsts[2]])
    assert 'gpt-sol (Greece), ending turn 2: "Walls first."' in notes
    assert 'claude-opus (Rome), ending turn 1: "Settling the river before Greece does."' in notes
    assert 'plan (set turn 2): "Walls, then Monarchy."' in fake.prompts[firsts[2]]
    assert "The angle: the race at the top" in fake.prompts[firsts[1]]
    assert "The angle: the economy" in fake.prompts[firsts[3]]


def test_joining_a_game_under_way_calls_only_its_latest_moments(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(1), entry(2, events=[event("war_declared", 1, "an old war", **{"from": 2})]),
                *[entry(t) for t in range(3, 10)],
                entry(10, events=[event("peace_signed", 1, "claude-opus and gpt-sol made peace", **{"from": 2})])],
               turn=10)
    beat(c, clock)
    assert "We join at turn 10" in fake.prompts[0]
    beat(c, clock)
    said = now_part(fake.prompts[3])
    assert "made peace" in said and "an old war" not in said


def test_like_events_are_called_together_and_old_news_is_dropped(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(1)], turn=1)
    beat(c, clock)
    founded = [event("city_founded", 1 + i % 2, f"city number {i} founded", x=i, y=i) for i in range(4)]
    fake.turns.append(entry(2, events=[*founded, event("war_declared", 1, "a war", **{"from": 2})]))
    fake.live["turn"] = 2
    assert len(beat(c, clock)) == 3
    said = now_part(fake.prompts[3])
    assert said.index("a war") < said.index("city number 0") and all(f"city number {i}" in said for i in range(4))

    fake.turns.append(entry(3, events=[event("peace_signed", 1, "a quick peace", **{"from": 2})]))
    fake.live["turn"] = 3
    c.poll()   # seen, but not called before it is old news
    fake.turns += [entry(t) for t in range(4, 8)]
    fake.live["turn"] = 7
    asked = len(fake.prompts)
    assert beat(c, clock)[0]["kind"] == "color" and "a quick peace" not in now_part(fake.prompts[asked])


def test_a_lead_change_is_called_once_the_new_leader_still_leads(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    behind, ahead = {"1": [40, 1, 3, 9, 2, 0], "2": [45, 1, 3, 9, 2, 0]}, {"1": [50, 1, 3, 9, 2, 0],
                                                                           "2": [45, 1, 3, 9, 2, 0]}
    fake.start([entry(6, scores=behind, events=[event("lead_change", 2, "gpt-sol takes the lead", **{"from": 1})]),
                entry(7, scores=ahead, events=[event("lead_change", 1, "claude-opus takes it back", **{"from": 2})])],
               turn=7)
    beat(c, clock)
    asked = len(fake.prompts)
    beat(c, clock)
    said = now_part(fake.prompts[asked])
    assert "turn 7: claude-opus (Rome) takes the lead from gpt-sol (Greece), 50 to 45" in said
    assert "gpt-sol (Greece) takes the lead" not in said
    assert "Leader: claude-opus (Rome), on top since turn 7" in fake.prompts[asked]

    level = {"1": [50, 1, 3, 9, 2, 0], "2": [50, 1, 3, 9, 2, 0]}
    fake.turns.append(entry(8, scores=level, events=[event("lead_change", 2, "gpt-sol takes the lead", **{"from": 1})]))
    fake.live["turn"] = 8
    asked = len(fake.prompts)
    assert beat(c, clock)[0]["kind"] == "color"   # a tie is no lead change, whatever the data calls it
    assert "No leader: claude-opus (Rome) and gpt-sol (Greece) are level on 50" in fake.prompts[asked]
    assert "takes the lead" not in fake.prompts[asked]

    fake.turns.append(entry(9, scores={"1": [50, 1, 3, 9, 2, 0], "2": [55, 1, 3, 9, 2, 0]},
                            events=[event("lead_change", 2, "gpt-sol takes the lead from claude-opus", **{"from": 1})]))
    fake.live["turn"] = 9
    asked = len(fake.prompts)
    beat(c, clock)
    said = now_part(fake.prompts[asked])
    assert ("turn 9: gpt-sol (Greece) takes the lead on 55, after claude-opus (Rome) and gpt-sol (Greece) were level "
            "at the top on 50") in said
    assert "from claude-opus" not in said   # it wasn't leading: it was level


def test_the_later_games_stories_and_the_race(fake):
    """A model's wonder, a landing on a model's land, the two models meeting and trading are called; an AI's new era,
    upgrades and contacts are not. The DATA has the year, each civ's share of land and people, culture and wonders,
    the race to each victory, and the broadcast's names for the seats."""
    clock = Clock()
    c = new_caster(fake, clock)
    fake.players[1]["name"] = "Opus 5.5"
    fake.players.append({"index": 3, "civ": "Egypt", "label": None, "barbarian": False, "seat": None})
    race = {"1": {"land": 0.31, "pop": 0.42, "culture": 12500, "city_culture": 4100, "wonders": 2, "era": 1},
            "2": {"land": 0.2, "pop": 0.25, "culture": 900}, "3": {"land": 0.1, "pop": 0.1, "culture": 300}}

    def later(turn: int, events=()) -> dict:
        e = entry(turn, events=events, date="AD 1250", scores={"1": [40, 1, 3, 9, 2, 0], "2": [30, 1, 2, 7, 1, 0],
                                                                 "3": [20, 1, 1, 3, 1, 0]})
        for i, more in race.items():
            e["stats"].setdefault(i, {"gold": 5, "government": "Despotism", "research": None, "at_war": []})
            e["stats"][i].update(more)
        return e

    fake.start([later(1)], turn=1)
    beat(c, clock)
    fake.turns.append(later(2, [
        event("wonder_built", 1, "Opus 5.5 completed The Pyramids in Roma", x=10, y=12, wonder="The Pyramids"),
        event("contact", 1, "First contact: Opus 5.5 meets gpt-sol", **{"from": 2}),
        event("contact", 2, "First contact: gpt-sol meets Egypt", **{"from": 3}),
        event("landing", 3, "Egypt landed 2 units from the sea in gpt-sol's land", x=30, y=9, **{"from": 2}),
        event("era_entered", 3, "Egypt enters the Middle Ages"),
        event("units_upgraded", 3, "Egypt upgraded 3 Warrior to Swordsman", x=1, y=1)]))
    fake.live["turn"] = 2
    asked = len(fake.prompts)
    beat(c, clock)
    said = now_part(fake.prompts[asked])
    for news in ("The Pyramids", "Opus 5.5 meets gpt-sol", "Egypt landed 2 units"):
        assert news in said, news
    for chatter in ("gpt-sol meets Egypt", "Egypt enters", "Egypt upgraded"):
        assert chatter not in said, chatter
    # an AI's era, upgrades and contacts are never news; a model's are
    news = {x.text: c.newsworthy(x) for x in c.moments_of(fake.turns[-1], clock.now)}
    assert [t for t, ok in news.items() if not ok] == [
        "turn 2: First contact: gpt-sol meets Egypt", "turn 2: Egypt enters the Middle Ages",
        "turn 2: Egypt upgraded 3 Warrior to Swordsman"], news
    seat_era = c.moments_of(later(3, [event("era_entered", 1, "Opus 5.5 enters the Middle Ages"),
                                      event("trade", 1, "Opus 5.5 traded Currency to gpt-sol for 40 gold",
                                            **{"from": 2})]), clock.now)
    assert all(c.newsworthy(x) for x in seat_era)
    data = fake.prompts[asked]
    assert "Turn 2 of 50, the year AD 1250" in data and "Opus 5.5 (Rome)" in data and "claude-opus" not in data
    assert "31% of the land and 42% of the people; 12,500 culture; 2 great wonders" in data
    assert ("The race: domination needs 67% of the land and of the people; nearest Opus 5.5 (Rome) with 31% and 42%; "
            "a cultural victory needs 100,000 culture and twice the next civ's, or 20,000 in one city: top "
            "Opus 5.5 (Rome) with 12,500, best city Opus 5.5 (Rome)'s with 4,100; 48 turns left") in data
    assert c.match.civ_named("opus 5.5") == "Rome"


def test_a_caster_looks_the_match_up_before_it_speaks(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(t) for t in range(1, 13)], turn=12)
    beat(c, clock)
    c.call_seconds, c.voice_seconds = dict.fromkeys(c.models.values(), 0.5), 0.5
    c.speaking_until += 5   # a long line queued: time for a round of lookups

    def answered(body: dict) -> dict:   # the lookups' answers come back under their ids, in order
        answers = [m for m in body["messages"] if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in answers] == ["look_0", "look_1"]
        assert body["messages"][2]["role"] == "assistant" and body["messages"][2]["tool_calls"][0]["id"] == "look_0"
        assert "claude-opus (Rome), turn 12: score 40, 1 of 2" in answers[0]["content"]
        assert "Before: turn 2: 40, 1 of 2" in answers[0]["content"]
        assert answers[1]["content"].startswith("score, turns 1 to 12:\nclaude-opus (Rome): 1: 40")
        assert "tool_choice" not in body
        return say("Forty points, Max, and it hasn't moved in ten turns.", "Rome")


    fake.script = [calls(("civ", {"civ": "claude-opus"}), ("trend", {"stat": "score", "civs": ["Rome"]}),
                         ident="look"), answered]
    lines = beat(c, clock)
    assert lines[0]["text"] == "Forty points, Max, and it hasn't moved in ten turns."
    assert lines[0]["focus"] == "Rome" and lines[0]["speaker"] == "color"
    assert len([b for b in fake.chats if b["messages"][1]["content"] == fake.prompts[3]]) == 2   # one line, 2 calls


def test_live_lookups_stop_when_the_audio_would_run_dry(fake, capsys):
    """A round of lookups only while the audio queued outlasts it, the line after it and its voicing: the casters'
    running estimates of a call's time and a voicing's."""
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(1)], turn=1)
    look = calls(("standings", {}))
    fake.script = [look]
    c.tick()   # nothing queued: it must speak at once
    assert fake.chats[0]["tool_choice"] == {"type": "function", "function": {"name": "say"}}
    assert len(c.lines) == 1 and len(fake.chats) == 2

    c.call_seconds, c.voice_seconds = dict.fromkeys(c.models.values(), 1.0), 1.0
    c.writing_seconds = dict.fromkeys(SPEAKERS, 40)   # so the next line is due at once
    c.speaking_until = clock.now + 30   # plenty queued: up to LOOKUP_ROUNDS rounds, then a forced say
    fake.script = [look] * caster.LOOKUP_ROUNDS
    chats = len(fake.chats)
    c.tick()
    asked = fake.chats[chats:]
    assert [("tool_choice" in b) for b in asked] == [False] * caster.LOOKUP_ROUNDS + [True]
    assert asked[-1]["messages"][-1]["content"].endswith(
        "(You are live and out of time: call say with your line now.)")
    assert len(c.lines) == 2

    def slow(body):   # a lookup that takes most of what was queued
        clock.now = c.speaking_until - 2.5
        return look

    c.speaking_until, c.writing_seconds = clock.now + 30, dict.fromkeys(SPEAKERS, 40)
    chats = len(fake.chats)
    fake.script = [slow]
    c.tick()
    assert [("tool_choice" in b) for b in fake.chats[chats:]] == [False, True]   # no time for another round
    assert len(c.lines) == 3

    c.speaking_until, c.writing_seconds = clock.now + 30, dict.fromkeys(SPEAKERS, 40)
    fake.script = [look] * (caster.LOOKUP_ROUNDS + 1)   # it won't speak even then: the line fails, and is retried
    c.tick()
    assert len(c.lines) == 3 and f"no line after {caster.LOOKUP_ROUNDS + 1} calls" in capsys.readouterr().out
    clock.now += caster.RETRY_SECONDS
    c.tick()
    assert len(c.lines) == 4


def test_a_model_without_a_forced_say_is_asked_instead(fake, capsys):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(1)], turn=1)
    fake.reject_forced = True
    c.tick()   # nothing queued, so the line is forced: refused, then asked without the force
    assert c.lines[0]["text"] == "Line 1: Rome leads." and c.no_force == {c.models["pbp"]}
    assert c.no_tools == set() and c.research_due()   # tools still work, research too
    assert "tool_choice" in fake.chats[0] and "tool_choice" not in fake.chats[1] and "tools" in fake.chats[1]
    until_quiet(c, clock)
    c.tick()
    assert len(c.lines) == 2 and not any("tool_choice" in b for b in fake.chats[2:])   # never forced again
    assert capsys.readouterr().out.count("turned a forced say down") == 1


def test_a_model_that_cant_be_forced_gets_only_say_on_its_last_round(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(1)], turn=1)
    fake.reject_forced = True
    c.tick()   # nothing queued: forced, refused, then asked with say alone, so a lookup can't cost the line
    assert "tool_choice" in fake.chats[0] and "tool_choice" not in fake.chats[1]
    assert tools_of(fake.chats[1]) == {"say"} and len(c.lines) == 1
    c.call_seconds, c.voice_seconds = dict.fromkeys(c.models.values(), 0.5), 0.5
    c.speaking_until, c.writing_seconds = clock.now + 30, dict.fromkeys(SPEAKERS, 40)
    fake.script = [calls(("standings", {}))] * caster.LOOKUP_ROUNDS
    chats = len(fake.chats)
    c.tick()   # time for every round of lookups, then say alone
    asked = fake.chats[chats:]
    assert [tools_of(b) == {"say"} for b in asked] == [False] * caster.LOOKUP_ROUNDS + [True]
    assert not any("tool_choice" in b for b in asked) and len(c.lines) == 2


def test_each_casters_model_keeps_its_own_timing(fake):
    """A fast play-by-play model and a slow analyst: a line's lookups and its start go by its own caster's times."""
    clock = Clock()
    c = new_caster(fake, clock, models={"pbp": "fast/model", "color": "slow/model"})
    fake.start([entry(1)], turn=1)

    def taking(seconds):
        def line(body):
            clock.now += seconds
            return say(f"A line from {body['model']}.")
        return line

    fake.script = [taking(1), taking(6), taking(1)]   # the intro: Max, Ada, Max
    beat(c, clock)
    assert c.call_seconds["fast/model"] < caster.CALL_SECONDS < c.call_seconds["slow/model"]
    assert c.writing_seconds["pbp"] < 2 < 6 <= c.writing_seconds["color"]
    c.call_seconds, c.voice_seconds = {"fast/model": 1.0, "slow/model": 6.0}, 1.0
    c.speaking_until = clock.now + 10
    assert c.time_to_look("fast/model") and not c.time_to_look("slow/model")
    c.speaking_until = clock.now + 2 * 1.0 + 1.0 + caster.LANDING_SECONDS - 0.1
    assert not c.time_to_look("fast/model")   # a line that looked something up still lands before the audio ends

    c.writing_seconds = {"pbp": 1.0, "color": 6.0}
    c.beat = caster.Beat("color", "Analysis.", caster.COLOR, 2)
    c.speaking_until = clock.now + caster.LEAD_SECONDS + 3
    chats = len(fake.chats)
    c.tick()   # Ada's line takes 6 s, so it starts with 7 s queued
    assert len(fake.chats) > chats
    c.beat = caster.Beat("color", "Analysis.", caster.PBP, 2)
    c.speaking_until = clock.now + caster.LEAD_SECONDS + 3
    chats = len(fake.chats)
    c.tick()   # Max's takes 1 s: not yet
    assert len(fake.chats) == chats


def test_a_reasoning_model_gets_what_the_endpoint_asks_for(fake, capsys):
    """OpenAI's reasoning models on chat completions want max_completion_tokens, and tools without reasoning: the
    first 400s say so, and the model's calls carry it from then on, tools and all."""
    clock = Clock()
    c = new_caster(fake, clock, models=dict.fromkeys(SPEAKERS, "openai/gpt-6-luna"))
    fake.start([entry(1)], turn=1)
    fake.bad_requests = [
        b'{"error": {"message": "Unsupported parameter: \'max_tokens\' is not supported with this model. Use '
        b'\'max_completion_tokens\' instead."}}',
        b'{"error": {"message": "Function tools with reasoning_effort are not supported for gpt-6-luna in '
        b'/v1/chat/completions. To use function tools, use /v1/responses or set reasoning_effort to \'none\'."}}']
    c.tick()
    first, second, third = fake.chats[:3]
    assert "max_tokens" in first and "max_completion_tokens" not in first
    assert second["max_completion_tokens"] == caster.MAX_TOKENS and "reasoning_effort" not in second
    assert third["reasoning_effort"] == "none" and "max_tokens" not in third and tools_of(third) == {"say"}
    assert len(c.lines) == 1 and c.no_tools == set() and c.no_force == set()
    until_quiet(c, clock)
    c.tick()
    assert len(fake.chats) == 4 and fake.chats[3]["reasoning_effort"] == "none"   # straight away from now on
    assert capsys.readouterr().out.count("calls take") == 2


def test_a_tool_refusal_in_vllms_words_turns_tools_off(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(1)], turn=1)
    vllm = b'{"error": {"message": "\\"auto\\" tool choice requires --enable-auto-tool-choice and --tool-call-parser"}}'
    fake.bad_requests = [vllm, vllm]   # forced, then not: neither works on this endpoint
    c.tick()
    assert c.no_tools == {c.models["pbp"]} and len(c.lines) == 1 and "tools" not in fake.chats[-1]


def test_text_alone_is_not_a_line_while_there_is_time_to_look(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(t) for t in range(1, 4)], turn=3)
    c.call_seconds, c.voice_seconds = dict.fromkeys(c.models.values(), 0.5), 0.5
    c.speaking_until, c.writing_seconds = clock.now + 30, dict.fromkeys(SPEAKERS, 40)
    no_id = {"type": "function", "function": {"name": "standings", "arguments": "{}"}}
    fake.script = [{"role": "assistant", "content": "Let me check the standings first."},
                   {"role": "assistant", "content": None, "tool_calls": [no_id]},
                   say("Rome leads on forty.")]
    chats = len(fake.chats)
    c.tick()
    assert c.lines[0]["text"] == "Rome leads on forty."
    nudged, answered = fake.chats[chats + 1], fake.chats[chats + 2]
    assert nudged["messages"][-2:] == [{"role": "assistant", "content": "Let me check the standings first."},
                                       {"role": "user", "content": caster.TEXT_ALONE}]
    back, reply = answered["messages"][-2:]   # a call without an id goes back with the id its answer has
    assert back["tool_calls"][0]["id"] == reply["tool_call_id"] == "call_0"


def test_a_voice_that_hangs_is_given_up_on_in_seconds(fake, monkeypatch):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(1)], turn=1)
    asked = []
    post = c.post
    monkeypatch.setattr(c, "post", lambda path, body, timeout: asked.append((path, timeout))
                        or post(path, body, timeout))
    c.tick()
    assert ("/v1/audio/speech", caster.SPEECH_SECONDS) in asked and caster.SPEECH_SECONDS <= 15


def test_a_slow_voice_gets_a_twin_request_and_the_first_answer_is_the_voice(fake, monkeypatch):
    c = new_caster(fake, Clock())
    monkeypatch.setattr(caster, "SPEECH_TWIN_SECONDS", 0.2)
    monkeypatch.setattr(caster, "SPEECH_SECONDS", 1.0)
    asked = []

    def answers(*replies):
        def post(path, body, timeout):
            asked.append(timeout)
            reply = replies[len(asked) - 1]
            if isinstance(reply, float):
                time.sleep(reply)
                raise TimeoutError("The read operation timed out")
            if isinstance(reply, Exception):
                raise reply
            return reply
        return post

    def timed(*replies):
        asked.clear()
        monkeypatch.setattr(c, "post", answers(*replies))
        began = time.monotonic()
        try:
            return c.voiced({"input": "Sol takes the lead!"}), round(time.monotonic() - began, 1)
        except OSError as e:
            return e, round(time.monotonic() - began, 1)

    assert timed(b"wav") == (b"wav", 0.0) and asked == [1.0]
    # a call still out at SPEECH_TWIN_SECONDS gets a twin, which has the rest of SPEECH_SECONDS; the first answer wins
    assert timed(1.0, b"twin") == (b"twin", 0.2) and asked == [1.0, 0.8]
    # a call that fails gets its twin at once
    assert timed(urllib.error.URLError("refused"), b"again") == (b"again", 0.0)
    # both hung: given up on at SPEECH_SECONDS, and the line goes out as a caption
    error, seconds = timed(1.0, 0.8)
    assert isinstance(error, TimeoutError) and seconds <= 1.1


def test_bad_lookups_get_errors_back_not_exceptions(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(1), entry(2)], turn=2)
    bad = [calls(("teleport", {}), ("civ", "{not json"), ("civ", {"civ": "Atlantis"}), ("standings", {"turn": "soon"}),
                 ident="bad"),
           calls(("standings", {"turn": 99}), ("trend", {"stat": "mood"}), ("city", {"name": "Atlantis"}),
                 ("events", ["kind"]), ident="worse"),
           calls(*[("standings", {})] * (caster.LOOKUPS_AT_ONCE + 1), ident="many")]
    replies: list[list[dict]] = []

    def answered(body):
        replies.append([m for m in body["messages"] if m["role"] == "tool"])
        return say("Still standing.")

    fake.script = [*bad, answered]
    c.speaking_until, c.call_seconds, c.voice_seconds, c.writing_seconds = (
        clock.now + 60, dict.fromkeys(c.models.values(), 0.5), 0.5, dict.fromkeys(SPEAKERS, 60))
    c.tick()
    assert [m["tool_call_id"] for m in replies[0]] == [  # every call is answered, in order
        *(f"bad_{n}" for n in range(4)), *(f"worse_{n}" for n in range(4)), *(f"many_{n}" for n in range(5))]
    answers = [m["content"] for m in replies[0]]
    assert answers[0].startswith("error: there is no tool 'teleport'; the tools: standings, civ")
    assert answers[1].startswith("error: the arguments are not JSON")
    assert answers[2] == "error: no civ 'Atlantis'; the civs: claude-opus (Rome), gpt-sol (Greece)"
    assert answers[3] == "error: standings failed on those arguments (ValueError)"
    assert answers[4] == "error: no turn 99 in the data: turns 1 to 2"
    assert answers[5].startswith("error: no stat 'mood'; the stats: score, cities")
    assert answers[6] == "error: no city 'Atlantis'; the biggest: Roma, Athens"
    assert answers[7] == "error: the arguments must be an object"
    assert answers[8].startswith("Turn 2:\n1. claude-opus (Rome): 40")
    assert answers[12].startswith(f"error: at most {caster.LOOKUPS_AT_ONCE} lookups at a time")
    assert c.lines[-1]["text"] == "Still standing."


def test_breaking_news_cuts_into_the_talk(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(1)], turn=1)
    beat(c, clock)
    until_quiet(c, clock)
    c.tick()
    assert c.beat.kind == "color" and len(c.beat.lines) == 1   # the analysis is under way
    fake.turns.append(entry(2, events=[event("city_captured", 1, "claude-opus took Athens from gpt-sol", x=30, y=8,
                                             **{"from": 2})]))
    fake.live["turn"] = 2
    asked = len(fake.prompts)
    lines = beat(c, clock)
    assert [(line["kind"], line["speaker"]) for line in lines] == [("event", "pbp"), ("event", "color")]
    assert "You cut in: the desk was on something else, and this news can't wait." in fake.prompts[asked]
    assert "took Athens" in now_part(fake.prompts[asked])
    fake.turns.append(entry(3, events=[event("peace_signed", 1, "a quiet peace", **{"from": 2})]))
    fake.live["turn"] = 3
    until_quiet(c, clock)
    c.tick()
    until_quiet(c, clock)
    c.tick()   # a peace is news, but not news that cuts in
    assert c.beat.kind == "event" and c.beat.lines and not c.beat.cut_in


def test_a_question_to_the_other_caster_gets_an_answer(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(1)], turn=1)
    beat(c, clock)
    fake.script = [say("Rome sits on 1300 gold."), say("Why isn't Rome spending it, Max?"),
                   say("Saving for walls, I'd guess?"), say("Walls, or a war chest?")]
    lines = beat(c, clock)
    assert [line["speaker"] for line in lines] == ["color", "pbp", "color", "pbp"]   # up to MAX_LINES
    assert len(lines) == caster.MAX_LINES
    assert "line 3 of 3" in fake.prompts[5] and "line 4 of 4" in fake.prompts[6]


def test_the_analyst_researches_storylines_for_the_desk(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(t) for t in range(1, 6)], turn=5)
    assert not c.research_due()   # not before the intro
    beat(c, clock)
    assert c.research_due()

    def jots(body):
        assert tools_of(body) == {*caster.LOOKUP_ARGS, "jot"} and "tool_choice" not in body
        assert body["model"] == c.models["color"] and body["messages"][0]["content"].startswith("You are Ada,")
        assert "jot down talking points" in body["messages"][0]["content"]
        assert "NOTEBOOK\n(empty)" in body["messages"][1]["content"]
        return calls(("jot", {"point": "Rome has sat on 40 points for five turns.", "civ": "claude-opus"}),
                     ("jot", {"point": " "}), ("jot", {"point": "Greece trails by 10.", "civ": "nobody"}))

    fake.script = [calls(("standings", {"turn": 1})), jots]
    assert c.research_once() == 2 and not c.research_due()   # once a turn at most
    assert [(p["turn"], p["civ"], p["point"]) for p in c.notebook] == [
        (5, "Rome", "Rome has sat on 40 points for five turns."), (5, None, "Greece trails by 10.")]
    answers = [m for m in fake.chats[-1]["messages"] if m["role"] == "tool"][-3:]
    assert [m["content"] for m in answers] == ["jotted (1 in the notebook)", "error: jot needs a point",
                                               "jotted (2 in the notebook)"]

    asked = len(fake.prompts)
    beat(c, clock)
    assert ("NOTEBOOK (what Ada's research found; use a point when it fits)\n"
            "- turn 5, claude-opus (Rome): Rome has sat on 40 points for five turns.\n"
            "- turn 5: Greece trails by 10.") in fake.prompts[asked]
    beat(c, clock)
    assert "NOTEBOOK (" in fake.prompts[-1]
    beat(c, clock)
    assert "NOTEBOOK (" not in fake.prompts[-1]   # a point is in POINT_BEATS beats, then dropped

    c.jot("g-1", 5, {"point": "An old point."})
    fake.turns += [entry(t) for t in range(6, 6 + caster.POINT_TURNS + 1)]
    fake.live["turn"] = 5 + caster.POINT_TURNS + 1
    beat(c, clock)
    assert "An old point" not in fake.prompts[-1]   # nor once it is old news
    assert c.jot("g-0", 5, {"point": "From another game."}).startswith("error: that game is over")


def test_an_endpoint_without_tools_gets_lines_from_the_data(fake, capsys):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.reject_tools = True
    fake.start([entry(1)], turn=1)
    beat(c, clock)
    assert [line["text"] for line in c.lines] == ["Line 1: Rome leads.", "Line 2: Rome leads.", "Line 3: Rome leads."]
    assert c.no_tools == {c.models["pbp"], c.models["color"]} and not c.research_due()
    assert sum("tools" in b for b in fake.chats) == 2   # asked once with a forced say, once without
    assert fake.chats[2]["messages"][0]["content"].endswith(caster.SAY_JSON) and "tools" not in fake.chats[2]
    printed = capsys.readouterr().out
    assert printed.count("the endpoint turned tools down") == 1 and "does not support tools" in printed


def test_the_lookups_tell_the_story_of_the_game(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.meta["unit_types"] = ["Warrior", "Settler", "Legion", "Hoplite"]
    fight = lambda a, d, win, city=0: [1, 0, win, "aad", city, 3, a, 2 if a == 1 else 3, 30, 8, 3, 2, 3,   # noqa
                                       d, 3 if d == 2 else 2, 30, 8, 3, 0, 3]
    turns = [entry(t, scores={"1": [40 + 2 * t, 1, 3, 9, 2, 0], "2": [30 + t, 1, 2, 7, 1, 0]}) for t in range(1, 9)]
    turns[2]["events"] = [event("war_declared", 1, "claude-opus and gpt-sol are at war", **{"from": 2})]
    for t in turns[2:]:
        t["stats"]["1"]["at_war"], t["stats"]["2"]["at_war"] = [2], [1]
    turns[2]["messages"] = [{"from": 2, "to": [1], "text": "Peace, or you lose Roma."}]
    turns[3]["notes"] = {"1": "Legions to Athens."}
    turns[3]["plans"] = {"1": "Take Athens by turn 10."}
    turns[4]["battles"] = [fight(1, 2, "d"), fight(2, 1, "d")]
    turns[5]["battles"] = [fight(1, 2, "a", city=2)]
    turns[5]["events"] = [event("city_captured", 1, "claude-opus took Athens from gpt-sol", x=30, y=8,
                                **{"from": 2})]
    for t in turns[5:]:
        t["cities"] = [[10, 12, "Roma", 1, 4, 1, "Legion", 0], [30, 8, "Athens", 1, 1, 0, None, 1]]
    turns[7]["events"] = [event("wonder_built", 1, "claude-opus completed The Pyramids in Roma", city="Roma",
                                wonder="The Pyramids")]
    turns[6]["battles"] = [fight(1, 2, "a", city=3)]
    turns[6]["events"] = [event("city_destroyed", 2, "gpt-sol lost Sparta; it was razed", x=40, y=4)]   # the loser
    turns[7]["actions"] = {"1": [{"text": "c1 builds Legion", "ok": True}]}
    fake.start(turns, turn=8, messages=[{"from": "Greece", "to": "all", "text": "Rome is lying.", "seconds": 3.0}])
    beat(c, clock)

    rome = c.lookup("civ", {"civ": "Rome"})
    for fact in ("claude-opus (Rome), turn 8: score 56, 1 of 2", "2 cities, pop 3: Roma 4 (capital) building Legion, "
                 "Athens 1", "Army: 2 Warrior", "1300 gold", "Great wonders: The Pyramids",
                 "Record: 0 cities founded, 1 taken, 0 lost, 1 razed", "At war with gpt-sol (Greece) since turn 3",
                 'Plan (set turn 3): "Take Athens by turn 10."', 'Note, turn 3: "Legions to Athens."',
                 "Messages: 0 sent, 2 received", "Tool calls: 48, 8 failed"):
        assert fact in rome, (fact, rome)
    greece = c.lookup("civ", {"civ": "gpt-sol"})
    assert 'Messages: 2 sent, 0 received; turn 2: "Peace, or you lose Roma."; turn 8: "Rome is lying."' in greece
    assert "Record: 0 cities founded, 0 taken, 2 lost" in greece   # one taken from it, one razed
    battles = c.lookup("battles", {"civ": "Rome"})
    assert battles.startswith("4 battles, turns 4 to 6:\nclaude-opus (Rome): 1 attacks lost, 1 defences held, "
                              "2 attacks won, 1 cities taken, 1 cities razed; wins by Legion 3")
    assert "- turn 5: claude-opus (Rome)'s Legion attacked gpt-sol (Greece)'s Hoplite at (30,8): the attacker won, " \
           "city taken" in battles   # fought during the turn before the entry, as its messages were sent
    assert c.lookup("battles", {"since_turn": 8}) == "No battles since turn 8 in the data"
    assert c.lookup("battles", {"civ": "Rome", "other": "Greece"}).startswith("4 battles")   # theirs with each other
    assert c.lookup("battles", {"civ": "Greece", "other": "Barbarians"}) == (
        "No battles for gpt-sol (Greece) with Barbarians in the data")
    assert c.lookup("city", {"city": "Roma"}) == "error: city needs name; it takes name, got city"   # its arguments
    assert c.lookup("standings", {"civ": "Rome"}).startswith(   # a harmless extra argument costs no round
        "(ignored civ: standings takes turn)\nTurn 8:\n1. claude-opus (Rome): 56")
    assert c.lookup("plan", {}, "jot").endswith(", jot")   # research ends with jot, not say
    assert c.lookup("events", {"kind": "city_captured", "turns": 3}).startswith(
        "(ignored turns: events takes civ, kind, since_turn, until_turn)\n1 events:")
    assert c.lookup("events", {"since_turn": 4, "until_turn": 6}) == (
        "1 events:\n- turn 6: claude-opus took Athens from gpt-sol")
    assert c.lookup("city", {"name": "athens"}) == (
        "Athens: claude-opus (Rome)'s, size 1\nFirst seen turn 1, gpt-sol (Greece)'s\n"
        "Turn 6: now claude-opus (Rome)'s, from gpt-sol (Greece)\n"
        "Size: turn 1: 2, turn 2: 2, turn 3: 2, turn 4: 2, turn 5: 2, turn 6: 1, turn 7: 1, turn 8: 1")
    assert "Great wonders: The Pyramids (turn 8)" in c.lookup("city", {"name": "Roma"})
    assert c.lookup("diplomacy", {"civ": "Greece", "other": "Rome"}) == (
        "3 moments:\n- turn 2: gpt-sol (Greece) to claude-opus (Rome): \"Peace, or you lose Roma.\"\n"
        "- turn 3: claude-opus and gpt-sol are at war\n- turn 8: gpt-sol (Greece) to everyone: \"Rome is lying.\"")
    assert c.lookup("events", {"kind": "city_captured"}) == (
        "1 events:\n- turn 6: claude-opus took Athens from gpt-sol")
    assert c.lookup("events", {"kind": "landing"}).startswith("No such events; the kinds in this game: city_captured")
    assert c.lookup("trend", {"stat": "score", "turns": 3}) == (
        "score, turns 6 to 8:\nclaude-opus (Rome): 6: 52, 7: 54, 8: 56 (+4 over these turns)\n"
        "gpt-sol (Greece): 6: 36, 7: 37, 8: 38 (+2 over these turns)")
    assert c.lookup("standings", {"turn": 2}).startswith(
        "Turn 2:\n1. claude-opus (Rome): 44; 1 city, pop 3, 2 techs; 1300 gold; Despotism; 2 military")
    now = c.lookup("turn_now", {"civ": "Rome"})
    assert now == ("Turn 8:\nclaude-opus (Rome): still thinking, 41 s so far; 4 tool calls, 1 failed\n"
                   "   2 actions so far: c1 builds Settler; u4 settle (failed)\nTurn 7, 1 actions: c1 builds Legion")
    assert c.lookup("said", {"query": "rome LEADS"}).startswith("3 lines:\n- turn 8, Max: Line 1: Rome leads.")
    assert c.lookup("said", {"query": "Carthage"}) == "The desk hasn't said that yet"
    fake.meta["unit_types"] = ["Warrior"] * 2000
    assert len(c.lookup("trend", {"stat": "pop"})) <= caster.TOOL_CHARS


def test_a_beat_that_fails_before_it_says_anything_calls_nothing(fake):
    """A beat that fails three times ends; one that never got a line out leaves its news and the intro for the next."""
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(1)], turn=1)

    def blip():
        fake.chat_failures = caster.LINE_FAILS
        for _ in range(caster.LINE_FAILS):
            until_quiet(c, clock)
            c.tick()
            clock.now += caster.RETRY_SECONDS

    blip()   # before the opening
    assert c.lines == [] and not c.introduced
    assert [line["kind"] for line in beat(c, clock)] == ["intro"] * 3   # the show still opens
    fake.turns.append(entry(2, events=[event("civ_destroyed", 1, "claude-opus destroyed gpt-sol", **{"from": 2})]))
    fake.live["turn"] = 2
    blip()   # before the elimination is called
    assert len(c.lines) == 3
    asked = len(fake.prompts)
    lines = beat(c, clock)
    assert lines[0]["kind"] == "event" and "destroyed gpt-sol" in now_part(fake.prompts[asked])


def test_the_outro_rides_out_a_blip_but_is_never_started_again(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(1)], turn=1)
    beat(c, clock)
    fake.live["game_over"] = True
    clock.now += caster.POLL_SECONDS
    c.tick()
    assert c.lines[-1]["kind"] == "outro"
    fake.chat_failures = caster.LINE_FAILS   # a 15 s blip: the outro carries on where it was
    for _ in range(caster.LINE_FAILS + 2):
        clock.now += caster.RETRY_SECONDS
        c.tick()
    assert [line["kind"] for line in c.lines].count("outro") == 3 and c.finished
    assert [line["speaker"] for line in c.lines[-3:]] == ["pbp", "color", "pbp"]

    c = new_caster(fake, clock)
    until_quiet(c, clock)
    c.tick()
    assert [line["kind"] for line in c.lines] == ["outro"]
    fake.chat_failures = caster.OUTRO_FAILS   # the endpoint is gone: the broadcast ends, the result called once
    for _ in range(caster.OUTRO_FAILS + 3):
        clock.now += caster.RETRY_SECONDS
        c.tick()
    assert c.finished and [line["kind"] for line in c.lines].count("outro") == 1


def test_a_thinking_model_gets_its_thinking_back_and_a_bad_generation_is_no_refusal(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(1)], turn=1)
    thought = {**calls(("standings", {})), "reasoning_content": "Hmm.",
               "thinking_blocks": [{"type": "thinking", "thinking": "Hmm.", "signature": "sig"}]}
    fake.script = [thought]
    c.speaking_until, c.call_seconds, c.voice_seconds, c.writing_seconds = (
        clock.now + 30, dict.fromkeys(c.models.values(), 0.5), 0.5, dict.fromkeys(SPEAKERS, 40))
    c.tick()
    echoed = fake.chats[-1]["messages"][2]
    assert echoed["thinking_blocks"] == thought["thinking_blocks"] and echoed["reasoning_content"] == "Hmm."
    assert "finish_reason" not in echoed and len(c.lines) == 1

    fake.bad_requests = [b'{"error": "Failed to call a function. Please adjust your prompt. tool_use_failed"}']
    clock.now += caster.RETRY_SECONDS
    until_quiet(c, clock)
    c.tick()   # one bad generation: the line is retried, the tools stay
    clock.now += caster.RETRY_SECONDS
    c.tick()
    assert c.no_tools == set() and c.no_force == set() and len(c.lines) == 2
    fake.bad_requests = [b'{"error": "Failed to call a function. tool_use_failed"}']
    fake.script = [calls(("standings", {}))]
    c.research_once()
    assert c.no_tools == set()   # nor in the research


def test_a_line_with_no_time_left_is_told_so_up_front(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(1)], turn=1)
    c.tick()   # nothing queued
    assert fake.chats[0]["messages"][1]["content"].endswith(f"\n{caster.OUT_OF_TIME}")
    c.call_seconds, c.voice_seconds = dict.fromkeys(c.models.values(), 0.5), 0.5
    c.speaking_until, c.writing_seconds = clock.now + 30, dict.fromkeys(SPEAKERS, 40)
    c.tick()
    assert caster.OUT_OF_TIME not in fake.chats[-1]["messages"][1]["content"]


def test_a_new_game_starts_the_desk_afresh(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(1)], turn=1)
    beat(c, clock)
    assert c.lookup("said", {"query": "rome"}).startswith("3 lines")
    fake.game, fake.turns = "g-2", [entry(1)]
    clock.now += caster.POLL_SECONDS
    c.tick()
    assert "RECENT LINES (oldest first)\n(none: this is the opening)" in fake.prompts[-1]   # not the old game's
    assert c.lookup("said", {"query": "Line 2"}) == "The desk hasn't said that yet"
    assert c.jot("g-2", 9, {"point": "From a turn not played yet."}).startswith("jotted")
    c.begin(caster.Beat("color", "", caster.COLOR, 2))
    assert c.notebook == []   # a point from a turn after this one is another game's


def test_the_lookups_tell_cities_of_one_name_apart(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    turns = [entry(t) for t in range(1, 6)]
    for t in turns:   # Greece's Roma (id 7) beside Rome's; Rome's is lost after turn 3
        t["cities"].append([40, 4, "Roma", 2, 1, 0, None, 7])
    for t in turns[3:]:
        t["cities"] = [x for x in t["cities"] if x[7] != 0]
    fake.start(turns, turn=5)
    beat(c, clock)
    said = c.lookup("city", {"name": "Roma"})
    assert said.startswith("Roma: gpt-sol (Greece)'s, size 1\nFirst seen turn 1, gpt-sol (Greece)'s")
    assert "\n\nRoma: gone since turn 4: last claude-opus (Rome)'s, size 3" in said


def test_none_is_no_civ(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.players.append({"index": 3, "civ": "Egypt", "label": None, "barbarian": False, "seat": None})
    fake.start([entry(1)], turn=1)
    c.tick()
    for nothing in ("none", "None", "null", "", None, 3, ["Rome"]):
        assert c.match.civ_named(nothing) is None, nothing   # an AI civ without a label is no match for "none"
    assert c.lookup("civ", {"civ": "None"}).startswith("error: no civ 'None'; the civs: claude-opus (Rome)")
    assert c.lookup("civ", {"civ": "Barbarians"}).startswith("Barbarians has no score")


def test_the_voices_kept_stay_bounded(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    beat_ = caster.Beat("color", "", caster.COLOR, 2)
    for n in range(caster.AUDIO_KEPT * 3):
        c.publish(beat_, {"speaker": "pbp", "text": "Hi.", "focus": None}, streamed_wav(0.1) if n % 7 else None)
    assert len(c.audio) <= caster.AUDIO_KEPT and min(c.audio) > c.line_count - caster.AUDIO_KEPT


def test_a_key_with_a_control_character_is_refused_not_printed(tmp_path):
    import os
    import subprocess
    env = {**os.environ, "CAST_BASE_URL": "http://127.0.0.1:9", "CAST_API_KEY": "sk-se\ncret-123"}
    out = subprocess.run([sys.executable, str(CASTER), "--data", "http://127.0.0.1:9/live"], env=env,
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 2 and "control character" in out.stderr and "cret-123" not in out.stdout + out.stderr


def test_research_jots_are_not_lookups(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(1), entry(2)], turn=2)
    beat(c, clock)
    fake.script = [calls(*[("jot", {"point": f"Point {n}."}) for n in range(caster.LOOKUPS_AT_ONCE)],
                         ("standings", {}))]
    c.research_once()
    answers = [m["content"] for m in fake.chats[-1]["messages"] if m["role"] == "tool"]
    assert answers[-1].startswith("Turn 2:")   # the lookup is answered, whatever the jots before it


def test_the_research_and_the_poll_share_the_match_safely(fake):
    """The research thread looks the match up while the main loop polls it, and a new game replaces it."""
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(t) for t in range(1, 30)], turn=29)
    beat(c, clock)
    errors: list[str] = []
    stop = threading.Event()

    def look():
        while not stop.is_set():
            for name, args in (("civ", {"civ": "Rome"}), ("trend", {"stat": "score"}), ("said", {}),
                               ("diplomacy", {}), ("city", {"name": "Roma"}), ("standings", {})):
                out = c.lookup(name, args)
                if "failed on those arguments" in out:
                    errors.append(out)

    thread = threading.Thread(target=look)
    thread.start()
    try:
        for n in range(30, 80):
            fake.turns.append(entry(n))
            fake.live["turn"] = n
            if n == 60:
                fake.game, fake.turns = "g-2", [entry(1)]
            clock.now += caster.POLL_SECONDS
            c.poll()
    finally:
        stop.set()
        thread.join()
    assert errors == [] and c.match.game == "g-2"


def test_the_casters_never_say_what_the_env_masks(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(1)], turn=1)
    beat(c, clock)
    rude = codecs.decode("Ebzr, lbh fuvg shpxvat pbjneq.", "rot13")
    fake.turns.append(entry(2, messages=[{"from": 2, "to": [1], "text": "Rome, you s*** f****** coward."}]))
    fake.live["turn"] = 2
    fake.script = [say(f'Greece goes nuclear: "{rude}"'),
                   {"role": "assistant", "content": '{"text": "Rome, you s*** f****** coward. Bold words."}'}]
    beat(c, clock)
    assert "quoting it unless it is rude" in fake.prompts[3] and "stays unsaid" in caster.STYLE
    said = [line["text"] for line in c.lines[3:]]
    assert said == ['Greece goes nuclear: "Rome, you bleep bleep coward."', "Rome, you bleep bleep coward. Bold words."]
    assert [s["input"] for s in fake.speech[3:]] == said


def test_the_casters_block_the_words_the_env_blocks():
    moderation = pytest.importorskip("agentenv_openciv3.moderation")
    assert caster.BLOCKLIST == moderation.BLOCKLIST


def test_lines_are_cleaned_up_for_speech():
    match = caster.Match()
    match.players = [{"index": 1, "civ": "Rome", "label": "claude-opus"}]
    long = " ".join(["word"] * 20) + ". " + " ".join(["more"] * 20) + "."
    reply = ('Here you go:\n```json\n{"lines": [{"speaker": "pbp", "text": "Max: *leans in* Rome [cheering] strikes!",'
             ' "focus": "CLAUDE-OPUS"}, {"speaker": "color", "text": "' + long + '", "focus": null},'
             ' {"speaker": "pbp", "text": "  "}]}\n```')
    lines = caster.parse_lines(reply, match)
    assert lines == [{"speaker": "pbp", "text": "Rome strikes!", "focus": "Rome"},
                     {"speaker": "color", "text": " ".join(["word"] * 20) + ".", "focus": None}]
    assert caster.spoken(" ".join(["x"] * 40)) == " ".join(["x"] * 30) + "…"
    assert caster.spoken("Max: Ada: Max, look at that army.") == "Max, look at that army."
    mixed_up = ('{"lines": [{"speaker": "Ada", "text": "Rome takes the lead, Ada."},'
                ' {"speaker": "pbp", "text": "Max!"}]}')
    assert [line["text"] for line in caster.parse_lines(mixed_up, match)] == ["Rome takes the lead, Max.", "Ada!"]
    intro = ('{"lines": [{"speaker": "pbp", "text": "I\'m Max, and Max has the call: Rome first, Max?"},'
             ' {"speaker": "color", "text": "Ada, at your service. Ada thinks Rome is ahead, Ada!"}]}')
    assert [line["text"] for line in caster.parse_lines(intro, match)] == [
        "I'm Max, and Max has the call: Rome first, Ada?", "Max, at your service. Ada thinks Rome is ahead, Max!"]
    twice = '{"lines": [{"speaker": "pbp", "text": "Go!"}]}\n{"lines": [{"speaker": "pbp", "text": "Again"}]}'
    assert [line["text"] for line in caster.parse_lines(twice, match)] == ["Go!"]   # the first object only
    plain = "Max: Turn 4, and Rome says 'peace'!\n\n**Ada:** Rome has 2 units, Max.\nSomething else."
    assert [(line["speaker"], line["text"]) for line in caster.parse_lines(plain, match)] == [
        ("pbp", "Turn 4, and Rome says 'peace'!"), ("color", "Rome has 2 units, Max.")]
    blocked = codecs.decode("Fuvg, gung'f n OHYYFUVG zbir", "rot13")
    assert caster.spoken(f"*grins* {blocked}; what the f**k, sh*t, s*** **Rome** 5*3") == (
        "bleep, that's a bleep move; what the bleep, bleep, bleep Rome 5*3")
    assert caster.spoken("Scunthorpe, shiitake and a classic assassin") == "Scunthorpe, shiitake and a classic assassin"
    assert caster.wav_seconds(caster.fixed_wav(streamed_wav(1.5))) == 1.5
    with pytest.raises(ValueError):
        caster.fixed_wav(b"ID3 not a wav")


def test_a_line_without_a_say_call_is_still_a_line(fake):
    """Models that answer in text: the line's JSON, the old shape with every caster's lines, or plain text."""
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(1)], turn=1)
    old = json.dumps({"lines": [{"speaker": "Ada", "text": "Not yours."}, {"speaker": "Max", "text": "Mine, Max!"}]})
    fake.script = [{"role": "assistant", "content": old},
                   {"role": "assistant", "content": 'Sure! {"text": "Rome, forty points.", "focus": "Rome"}'},
                   {"role": "assistant", "content": '"Max: Greece has twelve gold."\nAda: Not mine either.'}]
    for _ in range(3):
        until_quiet(c, clock)
        c.tick()
    assert [(line["speaker"], line["text"], line["focus"]) for line in c.lines] == [
        ("pbp", "Mine, Ada!", None), ("color", "Rome, forty points.", "Rome"), ("pbp", "Greece has twelve gold.", None)]


def tone(amplitude: int, samples: int = 8000) -> bytes:
    pcm = array.array("h", (round(amplitude * math.sin(i / 5)) for i in range(samples)))
    if sys.byteorder == "big":
        pcm.byteswap()
    return caster.fixed_wav(streamed_wav(0) + pcm.tobytes())


def levels(wav: bytes) -> tuple[float, int]:
    pcm = array.array("h", wav[44:])
    if sys.byteorder == "big":
        pcm.byteswap()
    return 20 * math.log10(math.sqrt(sum(x * x for x in pcm) / len(pcm)) / 32767), max(map(abs, pcm))


def test_the_two_voices_are_levelled_to_the_same_loudness():
    quiet, loud = caster.leveled(tone(800)), caster.leveled(tone(20000))
    assert levels(quiet)[0] == pytest.approx(caster.LEVEL_DBFS, abs=0.1)
    assert levels(loud)[0] == pytest.approx(caster.LEVEL_DBFS, abs=0.1)
    spiky = caster.leveled(tone(500)[:-2] + (32000).to_bytes(2, "little", signed=True))
    assert levels(spiky)[1] <= 32767   # a peak caps the gain rather than clipping
    assert caster.leveled(streamed_wav(0.5)) == streamed_wav(0.5)   # silence stays silence
