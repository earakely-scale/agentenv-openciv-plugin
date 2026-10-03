"""The casters (streamer/caster.py) against a fake match and a fake LiteLLM: what they say and when, how they pace
themselves to their audio, what they serve the stream page, and that failures neither stop them nor print the key."""

import array
import codecs
import importlib.util
import json
import math
import re
import sys
import threading
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


class Fake:
    """One server for both: the env's match data (GET /live/data.json) and LiteLLM (chat and speech)."""

    def __init__(self):
        self.players = [{"index": 0, "civ": "Barbarians", "label": None, "barbarian": True, "seat": None},
                        {"index": 1, "civ": "Rome", "label": "claude-opus", "barbarian": False, "seat": 0},
                        {"index": 2, "civ": "Greece", "label": "gpt-sol", "barbarian": False, "seat": 1}]
        self.game: str | None = None
        self.turns: list[dict] = []
        self.live: dict = {"turn": None, "game_over": False, "victory": None, "seats": []}
        self.data_down = False
        self.prompts: list[str] = []
        self.chats: list[dict] = []
        self.analyst = "Ada"
        self.auth: list[str] = []
        self.speech: list[dict] = []
        self.chat_failures = self.speech_failures = 0
        self.content: str | None = None    # the chat reply's text; default: lines in the order asked for
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def start(self, turns: list[dict], turn: int, **live) -> None:
        self.game, self.turns = "g-1", turns
        self.live = {"turn": turn, "game_over": False, "victory": None, "seats": [
            {"civ": "Rome", "label": "claude-opus", "ended": False, "seconds": 41.2, "calls": {"ok": 3, "failed": 1},
             "actions": []},
            {"civ": "Greece", "label": "gpt-sol", "ended": True, "seconds": 9.0, "calls": {"ok": 7, "failed": 0},
             "actions": []}], **live}

    def chat(self, body: dict) -> str:
        prompt = body["messages"][1]["content"]
        self.prompts.append(prompt)
        self.chats.append(body)
        if self.content is not None:
            return self.content
        count = int(prompt.rsplit("Write ", 1)[1].split()[0])
        first = caster.COLOR if f"{self.analyst} first" in prompt else caster.PBP
        other = caster.PBP if first == caster.COLOR else caster.COLOR
        lines = [{"speaker": first if i % 2 == 0 else other, "text": f"Line {len(self.prompts)}.{i}: Rome leads.",
                  "focus": "claude-opus" if i == 0 else "Atlantis"} for i in range(count)]
        return f"```json\n{json.dumps({'lines': lines})}\n```"

    def handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if fake.data_down or not self.path.startswith("/live/data.json"):
                    return self.reply(503, b"down", "text/plain")
                since = int(self.path.split("since=")[1])
                doc = {"schema": 1, "game": fake.game, "players": fake.players if fake.game else [],
                       "meta": {"turn_limit": 50, "unit_types": ["Warrior", "Settler"], "civilian": ["Settler"],
                                "victory": None},
                       "turns": [t for t in fake.turns if t["turn"] > since], "live": fake.live}
                self.reply(200, json.dumps(doc).encode(), "application/json")

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.auth.append(self.headers["Authorization"])
                if self.path == "/v1/chat/completions":
                    if fake.chat_failures:
                        fake.chat_failures -= 1
                        return self.reply(500, f"bad key {KEY}".encode(), "text/plain")
                    reply = {"choices": [{"message": {"role": "assistant", "content": fake.chat(body)}}]}
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


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def new_caster(fake: Fake, clock: Clock, **kw):
    return caster.Caster(f"{fake.url}/live?stream", fake.url + "/v1/", KEY, model="anthropic/claude-haiku-4-5",
                         tts_model="openai/gpt-4o-mini-tts", clock=clock, **kw)


def until_quiet(c, clock: Clock) -> None:
    """Moves the clock to when the next beat is due: less than LEAD_SECONDS of speech left."""
    clock.now = max(clock.now + caster.POLL_SECONDS, c.speaking_until - caster.LEAD_SECONDS + 0.1)


def test_the_casters_open_call_the_events_and_sign_off(fake):
    clock = Clock()
    c = new_caster(fake, clock, title="Battle of the Labs")
    c.tick()
    assert c.lines == [] and fake.prompts == []   # no game yet

    fake.start([entry(1), entry(2)], turn=2)
    until_quiet(c, clock)
    c.tick()
    intro = c.lines
    assert [line["kind"] for line in intro] == ["intro"] * 3
    assert [(line["speaker"], line["name"]) for line in intro] == [("pbp", "Max"), ("color", "Ada"), ("pbp", "Max")]
    assert intro[0] == {"id": 1, "speaker": "pbp", "name": "Max", "text": "Line 1.0: Rome leads.",
                        "audio": "audio/1.wav", "seconds": WAV_SECONDS, "turn": 2, "focus": "Rome", "kind": "intro"}
    assert intro[1]["focus"] is None   # not a civ in this game
    prompt = fake.prompts[0]
    assert '"Battle of the Labs"' in prompt and "claude-opus (Rome), gpt-sol (Greece)" in prompt
    assert "1. claude-opus (Rome): 40" in prompt and "1300 gold" in prompt and "2 military units" in prompt
    assert "still thinking, 41 s so far; 4 tool calls, 1 failed" in prompt
    assert sorted(s["voice"] for s in fake.speech) == ["ash", "ash", "sage"]   # voiced in parallel
    assert {s["response_format"] for s in fake.speech} == {"wav"}
    assert set(fake.auth) == {f"Bearer {KEY}"}

    fake.turns.append(entry(3, at_war=[2], events=[
        event("war_declared", 1, "claude-opus and gpt-sol are at war", **{"from": 2}),
        event("city_captured", 1, "claude-opus took Athens from gpt-sol", x=30, y=8, **{"from": 2}),
        event("tech_learned", 2, "gpt-sol learned Pottery")]))
    fake.live["turn"] = 3
    until_quiet(c, clock)
    c.tick()
    calls = fake.prompts[-1].split("NOW\n")[1]
    assert [line["kind"] for line in c.lines[3:]] == ["event"] * 3
    assert calls.index("claude-opus took Athens from gpt-sol (size 2)") < calls.index("are at war")
    assert "Pottery" not in calls   # not worth a call, but in the DATA
    assert "gpt-sol learned Pottery" in fake.prompts[-1]
    assert "at war with gpt-sol (Greece) since turn 3" in fake.prompts[-1]

    until_quiet(c, clock)
    c.tick()
    assert c.lines[-1]["kind"] == "color"   # the events were called once
    assert "Ada first" in fake.prompts[-1] and "The angle: the race at the top" in fake.prompts[-1]
    assert "Line 2.0: Rome leads." in fake.prompts[-1].split("RECENT LINES")[1]   # it remembers what it said

    fake.turns.append(entry(4, scores={"1": [61, 2, 5, 12, 3, 0], "2": [20, 0, 0, 0, 1, 1]}))
    fake.live.update(turn=4, game_over=True)
    clock.now += caster.POLL_SECONDS
    assert c.speaking_until - clock.now > caster.LEAD_SECONDS
    c.tick()
    assert c.lines[-1]["kind"] == "outro"   # at once: the stream ends a minute after GAME OVER
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
              "analyst": {"name": "Iris", "voice": "coral", "style": "A dry, unhurried analyst."}}
    casters = caster.casters_of(config)
    assert casters == {"pbp": ("Rex", "ash", caster.CASTERS["pbp"][2]),
                       "color": ("Iris", "coral", "A dry, unhurried analyst.")}
    assert caster.casters_of({}) == caster.CASTERS
    fake.analyst = "Iris"
    c = caster.Caster(f"{fake.url}/live", fake.url, KEY, model="openai/gpt-5.6-luna", tts_model="openai/tts-2",
                      casters=casters, clock=clock)
    fake.start([entry(1)], turn=1)
    c.tick()
    until_quiet(c, clock)
    c.tick()
    assert [(line["speaker"], line["name"]) for line in c.lines] == [
        ("pbp", "Rex"), ("color", "Iris"), ("pbp", "Rex"), ("color", "Iris"), ("pbp", "Rex")]
    system = fake.chats[0]["messages"][0]["content"]
    assert "- Rex, play-by-play" in system and "- Iris, colour analyst" in system
    assert 'Rex says "Iris", Iris says "Rex"' in system and not re.search(r"\b(Max|Ada)\b", system)
    assert "Rex welcomes everyone" in fake.prompts[0] and "Iris opens with a sharp observation" in fake.prompts[1]
    assert {b["model"] for b in fake.chats} == {"openai/gpt-5.6-luna"}
    assert not any("temperature" in b for b in fake.chats)   # some models take only their own
    assert {s["model"] for s in fake.speech} == {"openai/tts-2"}
    assert sorted((s["voice"], s["instructions"]) for s in fake.speech)[0] == ("ash", caster.CASTERS["pbp"][2])
    assert {s["instructions"] for s in fake.speech if s["voice"] == "coral"} == {"A dry, unhurried analyst."}
    reply = json.dumps({"lines": [{"speaker": "Iris", "text": "Iris: Rome leads, Iris.", "focus": None},
                                  {"speaker": "Max", "text": "Not a caster here."}]})
    assert [(line["speaker"], line["text"]) for line in caster.parse_lines(reply, c.match, casters)] == [
        ("color", "Rome leads, Rex.")]


def test_the_casters_pace_themselves_to_their_audio(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(1)], turn=1)
    c.tick()
    assert len(fake.prompts) == 1
    assert c.speaking_until == pytest.approx(clock.now + 3 * (WAV_SECONDS + caster.GAP_SECONDS))
    clock.now = c.speaking_until - caster.LEAD_SECONDS - 1
    c.tick()
    assert len(fake.prompts) == 1   # plenty is queued
    clock.now = c.speaking_until - caster.LEAD_SECONDS + 0.5
    c.tick()
    assert len(fake.prompts) == 2
    assert c.lines[3]["kind"] == "color"
    assert c.speaking_until == pytest.approx(clock.now + caster.LEAD_SECONDS - 0.5
                                             + 2 * (WAV_SECONDS + caster.GAP_SECONDS))


def test_the_page_gets_the_lines_and_their_audio_from_anywhere(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(1)], turn=1)
    c.tick()
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
    fake.speech_failures = 3
    c.tick()
    assert [line["audio"] for line in c.lines] == [None, None, None]   # captions without a voice
    assert c.lines[0]["seconds"] == pytest.approx(4 / caster.WORDS_PER_SECOND)

    fake.content = "Sorry, I can't help with that."
    until_quiet(c, clock)
    c.tick()
    fake.content = '{"lines": [{"speaker": "narrator", "text": "Hi"}]}'
    clock.now += caster.RETRY_SECONDS
    c.tick()
    assert len(c.lines) == 3
    fake.content = ""   # all of it spent thinking
    clock.now += caster.RETRY_SECONDS
    c.tick()
    assert len(c.lines) == 3
    fake.content = None
    clock.now += caster.RETRY_SECONDS
    c.tick()
    assert len(c.lines) == 5 and c.lines[3]["audio"] == "audio/4.wav"
    fake.data_down = True   # the env is gone: nothing to talk about
    until_quiet(c, clock)
    c.tick()
    assert len(c.lines) == 5
    out = capsys.readouterr()
    printed = out.out + out.err
    assert printed.count("no match data") == 2   # once each time it goes away
    assert "HTTP 500 bad key <cast key>" in printed and "HTTP 429 slow down" in printed
    assert "no JSON in the reply" in printed and "no lines in the reply" in printed
    assert "the reply has no text (finish_reason None)" in printed
    assert KEY not in printed


def test_messages_and_notes_are_read_out_between_the_analysis(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(1)], turn=1)
    c.tick()
    fake.turns.append(entry(2, messages=[{"from": 1, "to": [2], "text": "Join me against Carthage, or you're next."}],
                            notes={"1": "Settling the river before Greece does."},
                            plans={"2": "Six cities by turn 40, then Monarchy."}))
    fake.live.update(turn=2, messages=[{"from": "Greece", "to": "all", "text": "Rome is lying.", "seconds": 3.1}])
    fake.live["seats"][1].update(note="Walls first.", plan="Walls, then Monarchy.", plan_turn=2)
    kinds = []
    for _ in range(4):
        until_quiet(c, clock)
        c.tick()
        kinds.append(c.lines[-1]["kind"])
    assert kinds == ["message", "color", "message", "color"]
    said = fake.prompts[1].split("NOW\n")[1]
    assert 'turn 1, claude-opus (Rome) to gpt-sol (Greece): "Join me against Carthage, or you\'re next."' in said
    assert 'turn 2, gpt-sol (Greece) to everyone: "Rome is lying."' in said
    notes = fake.prompts[3].split("NOW\n")[1]
    assert 'gpt-sol (Greece), ending turn 2: "Walls first."' in notes
    assert 'claude-opus (Rome), ending turn 1: "Settling the river before Greece does."' in notes
    assert 'plan (set turn 2): "Walls, then Monarchy."' in fake.prompts[3]
    assert "The angle: the race at the top" in fake.prompts[2] and "The angle: the economy" in fake.prompts[4]


def test_joining_a_game_under_way_calls_only_its_latest_moments(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(1), entry(2, events=[event("war_declared", 1, "an old war", **{"from": 2})]),
                *[entry(t) for t in range(3, 10)],
                entry(10, events=[event("peace_signed", 1, "claude-opus and gpt-sol made peace", **{"from": 2})])],
               turn=10)
    c.tick()
    assert "We join at turn 10" in fake.prompts[0]
    until_quiet(c, clock)
    c.tick()
    assert "made peace" in fake.prompts[1].split("NOW\n")[1] and "an old war" not in fake.prompts[1].split("NOW\n")[1]


def test_like_events_are_called_together_and_old_news_is_dropped(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(1)], turn=1)
    c.tick()
    founded = [event("city_founded", 1 + i % 2, f"city number {i} founded", x=i, y=i) for i in range(4)]
    fake.turns.append(entry(2, events=[*founded, event("war_declared", 1, "a war", **{"from": 2})]))
    fake.live["turn"] = 2
    until_quiet(c, clock)
    c.tick()
    said = fake.prompts[-1].split("NOW\n")[1]
    assert said.index("a war") < said.index("city number 0") and all(f"city number {i}" in said for i in range(4))
    assert len(c.lines) == 3 + 3

    fake.turns.append(entry(3, events=[event("peace_signed", 1, "a quick peace", **{"from": 2})]))
    fake.live["turn"] = 3
    clock.now += caster.POLL_SECONDS
    c.tick()   # seen, but too much is queued to call it
    fake.turns += [entry(t) for t in range(4, 8)]
    fake.live["turn"] = 7
    until_quiet(c, clock)
    c.tick()
    assert c.lines[-1]["kind"] == "color" and "a quick peace" not in fake.prompts[-1].split("NOW\n")[1]


def test_a_lead_change_is_called_once_the_new_leader_still_leads(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    behind, ahead = {"1": [40, 1, 3, 9, 2, 0], "2": [45, 1, 3, 9, 2, 0]}, {"1": [50, 1, 3, 9, 2, 0],
                                                                           "2": [45, 1, 3, 9, 2, 0]}
    fake.start([entry(6, scores=behind, events=[event("lead_change", 2, "gpt-sol takes the lead", **{"from": 1})]),
                entry(7, scores=ahead, events=[event("lead_change", 1, "claude-opus takes it back", **{"from": 2})])],
               turn=7)
    c.tick()
    until_quiet(c, clock)
    c.tick()
    said = fake.prompts[1].split("NOW\n")[1]
    assert "turn 7: claude-opus (Rome) takes the lead from gpt-sol (Greece), 50 to 45" in said
    assert "gpt-sol (Greece) takes the lead" not in said
    assert "Leader: claude-opus (Rome), on top since turn 7" in fake.prompts[1]

    level = {"1": [50, 1, 3, 9, 2, 0], "2": [50, 1, 3, 9, 2, 0]}
    fake.turns.append(entry(8, scores=level, events=[event("lead_change", 2, "gpt-sol takes the lead", **{"from": 1})]))
    fake.live["turn"] = 8
    until_quiet(c, clock)
    c.tick()
    assert c.lines[-1]["kind"] == "color"   # a tie is no lead change, whatever the data calls it
    assert "No leader: claude-opus (Rome) and gpt-sol (Greece) are level on 50" in fake.prompts[2]
    assert "takes the lead" not in fake.prompts[2]

    fake.turns.append(entry(9, scores={"1": [50, 1, 3, 9, 2, 0], "2": [55, 1, 3, 9, 2, 0]},
                            events=[event("lead_change", 2, "gpt-sol takes the lead from claude-opus", **{"from": 1})]))
    fake.live["turn"] = 9
    until_quiet(c, clock)
    c.tick()
    said = fake.prompts[3].split("NOW\n")[1]
    assert ("turn 9: gpt-sol (Greece) takes the lead on 55, after claude-opus (Rome) and gpt-sol (Greece) were level "
            "at the top on 50") in said
    assert "from claude-opus" not in said   # it wasn't leading: it was level


def test_the_casters_never_say_what_the_env_masks(fake):
    clock = Clock()
    c = new_caster(fake, clock)
    fake.start([entry(1)], turn=1)
    c.tick()
    rude = codecs.decode("Ebzr, lbh fuvg shpxvat pbjneq.", "rot13")
    fake.turns.append(entry(2, messages=[{"from": 2, "to": [1], "text": "Rome, you s*** f****** coward."}]))
    fake.live["turn"] = 2
    fake.content = json.dumps({"lines": [{"speaker": "Max", "text": f'Greece goes nuclear: "{rude}"'},
                                         {"speaker": "Ada", "text": "Rome, you s*** f****** coward. Bold words."}]})
    until_quiet(c, clock)
    c.tick()
    assert "quoting it unless it is rude" in fake.prompts[-1] and "stays unsaid" in caster.SYSTEM
    said = [line["text"] for line in c.lines[3:]]
    assert said == ['Greece goes nuclear: "Rome, you bleep bleep coward."', "Rome, you bleep bleep coward. Bold words."]
    assert sorted(s["input"] for s in fake.speech[3:]) == sorted(said)   # voiced in parallel


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
