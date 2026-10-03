"""Two AI casters for a match's live stream: a play-by-play voice and a colour analyst talk over the game, reading
the same match data as the viewer (GET <live>/data.json, docs/viewer.md). An LLM writes their lines and a
text-to-speech model voices them, both through an OpenAI-compatible endpoint such as LiteLLM (CAST_BASE_URL,
CAST_API_KEY), and the stream page plays them with captions:

    GET /cast.json?since=<id>  {"lines": [{"id", "speaker", "name", "text", "audio", "seconds", "turn", "focus",
                                "kind"}], "speaking_until": <unix seconds>}
    GET /audio/<id>.wav, GET /health

It paces itself to the audio: the next beat (the intro, the biggest new events, the players' messages and notes,
analysis, and the outro at GAME OVER) is written so that it lands when about LEAD_SECONDS of speech are left. Stdlib
only, as it runs in the streamer image's system Python; the key never appears in what it prints."""

from __future__ import annotations

import argparse
import array
import codecs
import concurrent.futures
import http.client
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PBP, COLOR = "pbp", "color"
CASTERS = {  # speaker: name, text-to-speech voice, how that voice sounds
    PBP: ("Max", "ash", "An esports play-by-play caster calling a live strategy match. High energy, quick and punchy, "
                        "smiling; build excitement on the big moments (wars, captured cities, lead changes) and land "
                        "the names and numbers crisply. Never shouting."),
    COLOR: ("Ada", "sage", "A calm, sharp esports analyst at the casters' desk. Measured and confident, with a dry, "
                           "amused wit; lean on the key number or word; steady and unhurried, warm but precise."),
}
POLL_SECONDS = 2
LEAD_SECONDS = 4        # speech left when the next beat lands: writing and voicing it starts that much earlier
LEVEL_DBFS = -22        # every line's loudness (RMS): the voices come back from text-to-speech ~12 dB apart
GAP_SECONDS = 0.5       # between two lines
RETRY_SECONDS = 5       # after a failed LLM call
FRESH_SECONDS = 75      # an event or message not called by then is old news
NEWS_TURNS = 3          # nor is one from more turns ago, also when joining a game under way
MEMORY = 12             # lines the casters remember, so they don't repeat themselves
AUDIO_KEPT = 200
MAX_WORDS = 30
WORDS_PER_SECOND = 2.5  # a line's length when its audio failed
EVENTS = ("civ_destroyed", "city_captured", "city_destroyed", "war_declared", "peace_signed", "lead_change",
          "government_changed", "city_founded")   # the events worth a call, the biggest first
EARLY_CITIES = 3        # a civ's first cities are news, later ones are not
# Slurs and strong profanity, whole words: the env's moderation.BLOCKLIST (ROT13, so the repository holds no plaintext
# slurs), copied because this runs without the env's package. A line that says one gets "bleep" instead.
BLOCKLIST = codecs.decode(
    "shpx shpxre shpxref shpxvat shpxrq shpxva zbgureshpxre zbgureshpxref zbgureshpxvat fuvg fuvgf fuvggl "
    "fuvgurnq ohyyfuvg phag phagf gjng gjngf ovgpu ovgpurf juber juberf fyhg fyhgf jnaxre jnaxref nffubyr "
    "nffubyrf pbpxfhpxre avttre avttref avttn avttnf snttbg snttbgf snt sntf genaal genaavrf "
    "ergneq ergneqf ergneqrq xvxr xvxrf fcvp fcvpf puvax puvaxf tbbx tbbxf jrgonpx jrgonpxf ornare "
    "ornaref pbba pbbaf cnxv cnxvf enturnq enturnqf gbjryurnq gbjryurnqf fnaqavttre", "rot13").split()
BLOCKED = re.compile(r"\b(?:" + "|".join(sorted(BLOCKLIST, key=len, reverse=True)) + r")\b", re.IGNORECASE)
MASKED = re.compile(r"(?<![*\w])[A-Za-z]+(?:\*{2,}[A-Za-z]*|\*[A-Za-z]+)")   # "s***", as the data masks it, or "f**k"
TOPICS = {  # what the analysis is about when nothing new happened, each in turn
    "race": "the race at the top: the leader, the gap to second place, and who has the momentum over the last five "
            "turns",
    "economy": "the economy: gold in the bank, governments and research. Who is sitting on gold, and why isn't it "
               "spending it?",
    "wars": "the wars: who is fighting whom, since when, and the army sizes on each side",
    "clock": "the clock: who is still thinking this turn and for how long, who ended fast, and who keeps making "
             "failed tool calls",
    "plans": "the players' own plans: does what they wrote match what they are doing on the board?",
    "bottom": "the player at the bottom of the table: what is going wrong for them, by the numbers",
    "expansion": "expansion: city counts, population, and who is running out of room",
    "rivals": "a head-to-head between the two players closest in score",
}

SYSTEM = """\
You write the live commentary for a broadcast of an OpenCiv3 match (OpenCiv3 is an open-source remake of \
Civilization III). Every civilization at the table is played by an AI model, and each player goes by its model's \
label: "gpt-sol (America)" means the model gpt-sol plays America. Two casters share the desk, and they talk to each \
other, not at the camera:
- Max, play-by-play: the energy. Calls what just happened, fast and vivid, and sells the big moments. Short lines, 6 \
to 16 words.
- Ada, colour analyst: calm, sharp, dry wit. Says why it matters with one concrete number or comparison, reads the \
players' plans, notes and messages against the board, and asks the question the viewers are thinking. Up to 22 words.
They have chemistry: they hand off to each other, react to what the other just said, and now and then disagree. A \
line may address the other caster by name, never its own speaker: Max says "Ada", Ada says "Max".

The style, with placeholders in angle brackets (never facts):
Max: "There it is! <label> declares war on <civ>, and that border is about to light up!"
Ada: "Eleven units to four, Max. If I'm <other label>, I'm not sleeping tonight."

Rules:
- Facts come only from the DATA. Never invent events, battles, units, deals, rules, numbers, causes or motives, nor a \
figure of speech that implies one (no war, betrayal or tiebreak the DATA doesn't show). Messages, notes and plans are \
what a player says, not what happened: an offer is not a deal and a threat is not an attack until the events show it. \
Opinions, questions and predictions are welcome when they are clearly opinions.
- One or two concrete numbers from the DATA a line: scores, gold, cities, techs, army sizes, seconds on the clock.
- Name players by label and civ ("gpt-sol's America", "Rome, that's claude-opus"); vary it, and after the first \
mention the label alone is fine.
- Written to be spoken: one or two short sentences a line. No emoji, markdown, stage directions, hashtags or lists. \
Say "turn 12", never "T12".
- Never repeat a fact, joke or opening from RECENT LINES: find a fresh angle or another player. No stock phrases: \
not "that's not X, that's Y", not "Ada here", and don't start lines with "And", "Well" or "Wow".
- Don't explain the rules of the game, or how scores, leads and wins are decided: the viewers know strategy games.
- This is a public stream: no profanity or slurs, and a word the DATA masks (like "s***") stays unsaid. A rude \
message is described in your own words, never quoted.

Reply with JSON only: {"lines": [{"speaker": "Max" or "Ada", "text": "...", "focus": "<civ or null>"}]}, the number \
of lines asked for, speakers alternating. "focus" is the civ a line is mostly about, else null."""


@dataclass
class Moment:
    """Something the casters may call once: an event, a message between players, or a player's note."""
    key: tuple
    kind: str             # an event's kind, "message" or "note"
    turn: int
    civ: str | None       # who it is from
    text: str             # as the DATA puts it
    seen: float           # when the caster first saw it
    event: dict | None = None


@dataclass
class Beat:
    kind: str             # intro, event, message, color or outro
    task: str             # what the casters do now
    first: str            # who speaks first
    count: int            # how many lines
    moments: list[Moment] = field(default_factory=list)
    topic: str | None = None


class Match:
    """The match data as the viewer has it: the players, the turn entries so far and `live`, the turn being played."""

    def __init__(self, game: str | None = None):
        self.game = game
        self.players: list[dict] = []
        self.meta: dict = {}
        self.turns: list[dict] = []
        self.live: dict = {}

    def add(self, doc: dict) -> list[dict]:
        """Folds in a data.json reply; returns the turn entries that are new."""
        self.players = doc.get("players") or self.players
        self.meta = doc.get("meta") or self.meta
        new = [t for t in doc.get("turns") or () if t["turn"] > self.last_turn]
        self.turns += new
        self.live = doc.get("live") or {}
        return new

    @property
    def last_turn(self) -> int:
        return self.turns[-1]["turn"] if self.turns else -1

    @property
    def turn(self) -> int:
        """The turn being played."""
        return self.live["turn"] if self.live.get("turn") is not None else self.last_turn

    @property
    def civs(self) -> list[dict]:
        return [p for p in self.players if not p.get("barbarian")]

    @property
    def started(self) -> bool:
        return bool(self.turns and self.civs)

    @property
    def over(self) -> bool:
        return bool(self.live.get("game_over") or self.meta.get("victory"))

    @property
    def victory(self):
        return self.meta.get("victory") or self.live.get("victory")

    def player(self, index: int | str) -> dict | None:
        return next((p for p in self.players if str(p["index"]) == str(index)), None)

    def who(self, index: int | str) -> str:
        p = self.player(index)
        if p is None:
            return "?"
        return f"{p['label']} ({p['civ']})" if p.get("label") else f"{p['civ']} (the game's own AI)"

    def who_civ(self, civ: str) -> str:
        return next((self.who(p["index"]) for p in self.civs if p["civ"] == civ), civ)

    def civ_named(self, name) -> str | None:
        """The civ a line's `focus` names, by civ or label; None for anything else."""
        name = str(name or "").strip().lower()
        return next((p["civ"] for p in self.civs if name in (p["civ"].lower(), str(p.get("label")).lower())), None)

    def leader(self, entry: dict) -> int | None:
        """The player with the top score in a turn entry; None while the top score is tied."""
        scores = {int(i): s[0] for i, s in (entry.get("scores") or {}).items()}
        top = max(scores.values(), default=None)
        leaders = [i for i, s in scores.items() if s == top]
        return leaders[0] if len(leaders) == 1 else None

    def entry(self, turn: int) -> dict:
        return next((t for t in self.turns if t["turn"] == turn), {})


class Caster:
    """Writes and voices the casters' lines a beat at a time, and keeps them for the stream page."""

    def __init__(self, data_url: str, base_url: str, api_key: str, *, model: str, tts_model: str,
                 title: str | None = None, clock: Callable[[], float] = time.time):
        self.data_url = data_url.split("?")[0].rstrip("/")
        self.base_url = base_url.rstrip("/").removesuffix("/v1")
        self.api_key = api_key
        self.model, self.tts_model, self.title = model, tts_model, title
        self.clock = clock
        self.lock = threading.Lock()
        self.lines: list[dict] = []
        self.audio: dict[int, bytes] = {}
        self.speaking_until = 0.0
        self.polled_at = float("-inf")
        self.retry_at = 0.0
        self.writing_seconds = 0.0   # how long the last beat took to write and voice
        self.data_down = False
        self.start(None)

    def start(self, game: str | None) -> None:
        """A new game: the casters start over, with an intro."""
        self.match = Match(game)
        self.known: set[tuple] = set()
        self.pending: list[Moment] = []
        self.messages: list[Moment] = []
        self.notes: dict[str, Moment] = {}
        self.introduced = self.finished = False
        self.called_leader: int | None = None
        self.topics_used: dict[str, int] = {}
        self.beats = 0
        self.last_kind: str | None = None

    def log(self, message: str) -> None:
        print(message.replace(self.api_key, "<cast key>") if self.api_key else message, flush=True)

    # ---- the loop ----

    def run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as e:  # the show goes on: no glitch in the data or in a reply ends the casting
                self.log(f"caster: {e!r}")
                self.retry_at = self.clock() + RETRY_SECONDS
            time.sleep(0.25)

    def tick(self) -> None:
        now = self.clock()
        if now - self.polled_at >= POLL_SECONDS:
            self.polled_at = now
            self.poll()
        due = self.speaking_until - now <= LEAD_SECONDS + self.writing_seconds
        if self.finished or self.data_down or now < self.retry_at or not (due or self.match.over):
            return   # the outro waits for no one; with the match data gone, there is nothing to talk about
        if beat := self.next_beat():
            self.cast(beat)

    def lines_since(self, since: int) -> dict:
        with self.lock:
            return {"lines": [line for line in self.lines if line["id"] > since],
                    "speaking_until": round(self.speaking_until, 2)}

    # ---- reading the match ----

    def poll(self) -> None:
        try:
            with urllib.request.urlopen(f"{self.data_url}/data.json?since={self.match.last_turn}", timeout=10) as r:
                doc = json.load(r)
        except (OSError, ValueError, http.client.HTTPException) as e:
            if not self.data_down:
                self.log(f"caster: no match data from {self.data_url} ({e}); retrying")
            self.data_down = True
            return
        self.data_down = False
        if doc.get("game") != self.match.game:
            cut = self.match.last_turn >= 0   # that reply has only the turns after the old game's last
            self.start(doc.get("game"))
            if cut:
                return self.poll()
        joining = not self.match.turns
        new = self.match.add(doc)
        if joining:
            new = [t for t in new if t["turn"] >= self.match.last_turn - NEWS_TURNS]
        now = self.clock()
        for entry in new:
            for moment in self.moments_of(entry, now):
                self.remember(moment)
        for moment in self.live_moments(now):
            self.remember(moment)

    def remember(self, moment: Moment) -> None:
        if moment.key in self.known:
            return
        self.known.add(moment.key)
        self.pending.append(moment)
        if moment.kind == "message":
            self.messages.append(moment)
        elif moment.kind == "note" and moment.turn >= getattr(self.notes.get(moment.civ), "turn", -1):
            self.notes[moment.civ] = moment

    def moments_of(self, entry: dict, now: float) -> list[Moment]:
        """A turn entry's events, and the messages and notes of the turn before it (docs/viewer.md)."""
        t, m = entry["turn"], self.match
        out = [Moment(("event", t, e["kind"], e.get("owner"), e.get("from"), e.get("x"), e.get("y")), e["kind"], t,
                      (m.player(e.get("owner", -1)) or {}).get("civ"), self.event_text(e, entry), now, e)
               for e in entry.get("events") or () if e.get("kind") in EVENTS]
        civ = {str(p["index"]): p["civ"] for p in m.players}
        for msg in entry.get("messages") or ():
            to = msg["to"] if msg["to"] == "all" else [civ.get(str(i), "?") for i in msg["to"]]
            out.append(self.message(t - 1, civ.get(str(msg["from"]), "?"), to, msg["text"], now))
        out += [self.note(t - 1, civ[i], note, now) for i, note in (entry.get("notes") or {}).items() if i in civ]
        return out

    def live_moments(self, now: float) -> list[Moment]:
        turn, live = self.match.turn, self.match.live
        out = [self.message(turn, msg["from"], msg["to"], msg["text"], now) for msg in live.get("messages") or ()]
        return out + [self.note(turn, s["civ"], s["note"], now) for s in live.get("seats") or () if s.get("note")]

    def message(self, turn: int, sender: str, to: list[str] | str, text: str, now: float) -> Moment:
        m = self.match
        recipients = "everyone" if to == "all" else ", ".join(m.who_civ(c) for c in to)
        return Moment(("message", turn, sender, text), "message", turn, sender,
                      f'turn {turn}, {m.who_civ(sender)} to {recipients}: "{text}"', now)

    def note(self, turn: int, civ: str, text: str, now: float) -> Moment:
        return Moment(("note", turn, civ), "note", turn, civ,
                      f'{self.match.who_civ(civ)}, ending turn {turn}: "{text}"', now)

    def event_text(self, e: dict, entry: dict) -> str:
        if e["kind"] == "lead_change":
            return f"turn {entry['turn']}: {self.lead_text(e['owner'], entry)}"
        city = next((c for c in entry.get("cities") or () if (c[0], c[1]) == (e.get("x"), e.get("y"))), None)
        size = f" (size {city[4]})" if city and e["kind"] in ("city_captured", "city_founded") else ""
        return f"turn {entry['turn']}: {e['text']}{size}"

    def lead_text(self, leader: int, entry: dict) -> str:
        """A lead change as the scores show it: the data's own text names a previous leader also when the top score
        was tied."""
        m = self.match
        now = {int(i): s[0] for i, s in (entry.get("scores") or {}).items()}
        before = {int(i): s[0] for i, s in (m.entry(entry["turn"] - 1).get("scores") or {}).items()}
        top = max(before.values(), default=None)
        was = [i for i, s in before.items() if s == top]
        if len(was) > 1:
            return (f"{m.who(leader)} takes the lead on {now.get(leader)}, after "
                    f"{' and '.join(m.who(i) for i in was)} were level at the top on {top}")
        if was and was[0] != leader:
            return f"{m.who(leader)} takes the lead from {m.who(was[0])}, {now.get(leader)} to {now.get(was[0])}"
        return f"{m.who(leader)} takes the lead on {now.get(leader)}"

    # ---- choosing the beat ----

    def next_beat(self) -> Beat | None:
        m = self.match
        if not m.started or self.finished:
            return None
        if m.over:
            return self.outro()
        if not self.introduced:
            return self.intro()
        now = self.clock()
        self.pending = [x for x in self.pending if now - x.seen <= FRESH_SECONDS and x.turn >= m.turn - NEWS_TURNS]
        news = sorted((x for x in self.pending if x.kind in EVENTS and self.newsworthy(x)),
                      key=lambda x: (EVENTS.index(x.kind), -x.turn))
        events = news[:3] + [x for x in news[3:] if any((x.kind, x.turn) == (e.kind, e.turn) for e in news[:3])][:3]
        if events:
            return Beat("event", "Call these moments from the board, the biggest first:\n"
                        + "\n".join(f"- {x.text}" for x in events)
                        + "\nMax calls it with energy; Ada answers with what it means, one number from the DATA.",
                        PBP, 2 if len(events) == 1 else 3, events)
        if messages := [x for x in self.pending if x.kind == "message"][-3:]:
            return Beat("message", "New diplomacy between the players, sent where everyone can see it:\n"
                        + "\n".join(f"- {x.text}" for x in messages)
                        + "\nMax reads it out, quoting it unless it is rude; Ada reads between the lines: do the "
                          "sender's wars, army or plan in the DATA back it up?",
                        PBP, 2 if len(messages) == 1 else 3, messages)
        notes = self.notes_to_read()
        if notes and self.last_kind != "message":
            return Beat("message", "The players' own notes on the turn they just played:\n"
                        + "\n".join(f"- {x.text}" for x in notes)
                        + "\nMax relays one in his own words; Ada checks it against the board: do the numbers back "
                          "it up?", PBP, 2, notes)
        return self.color()

    def newsworthy(self, moment: Moment) -> bool:
        e, m = moment.event, self.match
        if moment.kind == "lead_change":   # as the viewer shows it: the new leader has held the lead since
            return e["owner"] != self.called_leader and all(
                m.leader(t) == e["owner"] for t in m.turns if t["turn"] >= moment.turn)
        if moment.kind == "city_founded":
            cities = m.entry(moment.turn).get("cities") or ()
            return sum(c[3] == e["owner"] for c in cities) <= EARLY_CITIES
        return True

    def notes_to_read(self) -> list[Moment]:
        """The two newest notes, from different players."""
        out: list[Moment] = []
        for x in reversed(self.pending):
            if x.kind == "note" and x.civ not in {o.civ for o in out}:
                out.append(x)
        return out[:2]

    def intro(self) -> Beat:
        m = self.match
        title = f' to "{self.title}"' if self.title else ""
        roster = ", ".join(m.who(p["index"]) for p in m.civs)
        models = sum(bool(p.get("label")) for p in m.civs)
        limit = m.meta.get("turn_limit")
        joined = f" We join at turn {m.turn}, with the game under way." if m.turn > 3 else ""
        return Beat("intro", f"Open the broadcast. Max welcomes everyone{title}; between them, Max and Ada name every "
                             f"player with its civ: {roster}. Ada sets the stakes: {models} AI models"
                             + (f", {limit} turns" if limit else "")
                             + ", and they can message each other, so expect alliances, threats and betrayals. The "
                             f"last line throws to the action.{joined} Make it big: this is the opening.", PBP, 3)

    def outro(self) -> Beat:
        return Beat("outro", f"The game is over: {self.result()}. Max calls the result with the final score; Ada says "
                             "why, with one number, and names one standout from the rest of the table; Max thanks "
                             "the viewers and signs off for both of you. These are the last lines of the broadcast.",
                    PBP, 3)

    def result(self) -> str:
        m = self.match
        if v := m.victory:
            return f"{m.who_civ(v.get('civ'))} won by {v.get('kind')} on turn {v.get('turn', m.turn)}"
        limit = m.meta.get("turn_limit")
        end = f"turn {m.turn}" + (", the turn limit" if limit and m.turn >= limit else "")
        ranked = sorted(((s[0], i) for i, s in (m.turns[-1].get("scores") or {}).items()), reverse=True)
        top = [i for s, i in ranked if s == ranked[0][0]]
        if len(top) > 1:
            return f"{end}, with {' and '.join(m.who(i) for i in top)} level at the top on {ranked[0][0]} points"
        runner_up = f", ahead of {m.who(ranked[1][1])} on {ranked[1][0]}" if len(ranked) > 1 else ""
        return f"{end}: {m.who(top[0])} wins on score with {ranked[0][0]}{runner_up}"

    def color(self) -> Beat:
        m = self.match
        stats = m.turns[-1].get("stats") or {}
        fits = {"wars": any(s.get("at_war") for s in stats.values()), "clock": bool(m.live.get("seats")),
                "plans": bool(self.plans()), "bottom": len(m.civs) >= 3, "race": len(m.civs) >= 2,
                "rivals": len(m.civs) >= 2}
        topic = min((t for t in TOPICS if fits.get(t, True)), key=lambda t: self.topics_used.get(t, -1))
        return Beat("color", f"Nothing new to call this moment, so the desk fills with analysis. The angle: "
                             f"{TOPICS[topic]}. Ada opens with a sharp observation and a number; Max reacts.", COLOR, 2,
                    topic=topic)

    # ---- writing and voicing a beat ----

    def cast(self, beat: Beat) -> None:
        began = self.clock()
        written = self.write(beat)
        if written is None:
            self.retry_at = self.clock() + RETRY_SECONDS
            return
        with concurrent.futures.ThreadPoolExecutor(len(written)) as pool:
            voiced = list(pool.map(self.voice, written))
        self.beats += 1
        self.last_kind = beat.kind
        self.introduced = True
        self.finished = beat.kind == "outro"
        called = {x.key for x in beat.moments}
        self.pending = [x for x in self.pending if x.key not in called]
        for x in beat.moments:
            if x.kind == "lead_change":
                self.called_leader = x.event["owner"]
        if beat.topic:
            self.topics_used[beat.topic] = self.beats
        self.publish(beat, written, voiced)
        self.writing_seconds = self.clock() - began

    def write(self, beat: Beat) -> list[dict] | None:
        """The beat's lines, from the LLM; None if the call failed."""
        with self.lock:
            recent = "\n".join(f"{line['name']}: {line['text']}" for line in self.lines[-MEMORY:])
        first = CASTERS[beat.first][0]
        prompt = (f"DATA\n{self.summary()}\n\nRECENT LINES (oldest first)\n{recent or '(none: this is the opening)'}"
                  f"\n\nNOW\n{beat.task}\nWrite {beat.count} lines, {first} first, speakers alternating.")
        try:
            reply = json.loads(self.post("/v1/chat/completions", {
                "model": self.model, "max_tokens": 400, "temperature": 0.9,
                "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]}, 45))
            lines = parse_lines(reply["choices"][0]["message"]["content"], self.match)
        except (OSError, ValueError, KeyError, IndexError, TypeError, http.client.HTTPException) as e:
            self.log(f"caster: the {beat.kind} beat failed, retrying in {RETRY_SECONDS} s: {failure(e)}")
            return None
        return lines[:3]

    def voice(self, line: dict) -> bytes | None:
        """The line spoken, as a WAV file; None if the speech call failed."""
        _, voice, style = CASTERS[line["speaker"]]
        try:
            wav = leveled(fixed_wav(self.post("/v1/audio/speech", {
                "model": self.tts_model, "voice": voice, "input": line["text"], "instructions": style,
                "response_format": "wav"}, 45)))
        except (OSError, ValueError, http.client.HTTPException) as e:
            self.log(f"caster: no voice for a line, captions only: {failure(e)}")
            return None
        return wav

    def post(self, path: str, body: dict, timeout: float) -> bytes:
        request = urllib.request.Request(f"{self.base_url}{path}", data=json.dumps(body).encode(), headers={
            "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as r:
            return r.read()

    def publish(self, beat: Beat, written: list[dict], voiced: list[bytes | None]) -> None:
        with self.lock:
            for line, wav in zip(written, voiced, strict=True):
                seconds = wav_seconds(wav) if wav else len(line["text"].split()) / WORDS_PER_SECOND
                self.speaking_until = max(self.clock(), self.speaking_until) + seconds + GAP_SECONDS
                ident = len(self.lines) + 1
                if wav:
                    self.audio[ident] = wav
                    self.audio.pop(ident - AUDIO_KEPT, None)
                self.lines.append({"id": ident, "speaker": line["speaker"], "name": CASTERS[line["speaker"]][0],
                                   "text": line["text"], "audio": f"audio/{ident}.wav" if wav else None,
                                   "seconds": round(seconds, 2), "turn": self.match.turn, "focus": line["focus"],
                                   "kind": beat.kind})
                self.log(f"[{beat.kind} T{self.match.turn}] {CASTERS[line['speaker']][0]}: {line['text']}")

    # ---- the DATA the casters read ----

    def summary(self) -> str:
        m = self.match
        limit = m.meta.get("turn_limit")
        out = [f'Broadcast: "{self.title}"'] if self.title else []
        out.append(f"Turn {m.turn}" + (f" of {limit}" if limit else "") + (" (GAME OVER)" if m.over else ""))
        if m.over:
            out.append(f"Result: {self.result()}")
        out.append("Standings (score; cities, population, techs; treasury; government; research; military units):")
        out += self.standings()
        if leader := self.leader_since():
            out.append(f"Leader: {m.who(leader[0])}, on top since turn {leader[1]}")
        else:
            scores = {i: s[0] for i, s in (m.turns[-1].get("scores") or {}).items()}
            top = max(scores.values(), default=0)
            out.append(f"No leader: {' and '.join(m.who(i) for i, s in scores.items() if s == top)} are level on {top}")
        events = [f"- turn {t['turn']}: {e['text']}" for t in m.turns[-5:] for e in t.get("events") or ()
                  if e["kind"] != "lead_change"]   # the data calls a tie a lead; the Leader line has it right
        if events:
            out.append("Recent events:\n" + "\n".join(events[-12:]))
        if self.messages:
            out.append("Recent messages between the players:\n" + "\n".join(f"- {x.text}" for x in self.messages[-6:]))
        return "\n".join(out)

    def standings(self) -> list[str]:
        """A line per civ, best score first, then its plan, its last note and its clock."""
        m = self.match
        last = m.turns[-1]
        before = next((t for t in reversed(m.turns) if t["turn"] <= last["turn"] - 5), {})
        civilian, types = set(m.meta.get("civilian") or ()), m.meta.get("unit_types") or []
        army: dict[int, int] = {}
        for u in last.get("units") or ():
            if 0 <= u[4] < len(types) and types[u[4]] not in civilian:
                army[u[3]] = army.get(u[3], 0) + 1
        wars, plans, totals = self.war_starts(), self.plans(), self.call_totals()
        seats = {s["civ"]: s for s in m.live.get("seats") or ()}
        out = []
        scores = sorted((last.get("scores") or {}).items(), key=lambda kv: -kv[1][0])
        for rank, (index, score) in enumerate(scores, 1):
            if (p := m.player(index)) is None:
                continue
            st = (last.get("stats") or {}).get(index, {})
            was = (before.get("scores") or {}).get(index)
            parts = [f"{rank}. {m.who(index)}: {score[0]}" + (f" ({score[0] - was[0]:+d} in 5 turns)" if was else ""),
                     f"{score[1]} {'city' if score[1] == 1 else 'cities'}, pop {score[2]}, {score[4]} techs"]
            gold_was = (before.get("stats") or {}).get(index, {}).get("gold")
            parts += [f"{st['gold']} gold" + (f" ({st['gold'] - gold_was:+d} in 5 turns)" if gold_was is not None
                                              else "")] if "gold" in st else []
            parts += [st["government"]] if st.get("government") else []
            parts += [f"researching {st['research']}"] if st.get("research") else []
            parts.append(f"{army.get(int(index), 0)} military units")
            parts += ["ELIMINATED"] if score[5] else []
            for enemy in st.get("at_war") or ():
                since = wars.get(frozenset((int(index), enemy)))
                parts.append(f"at war with {m.who(enemy)}" + (f" since turn {since}" if since else ""))
            out.append("; ".join(parts))
            if plan := plans.get(p["civ"]):
                out.append(f'   plan (set turn {plan[0]}): "{plan[1]}"' if plan[0] is not None else
                           f'   plan: "{plan[1]}"')
            if note := self.notes.get(p["civ"]):
                out.append(f"   last note: {note.text.split(': ', 1)[-1]}")
            if seat := seats.get(p["civ"]):
                calls = seat.get("calls") or {}
                clock = (f"ended it after {seat.get('seconds', 0):.0f} s" if seat.get("ended")
                         else f"still thinking, {seat.get('seconds', 0):.0f} s so far")
                out.append(f"   this turn: {clock}; {calls.get('ok', 0) + calls.get('failed', 0)} tool calls, "
                           f"{calls.get('failed', 0)} failed")
            if total := totals.get(index):
                out.append(f"   whole game: {total['ok'] + total['failed']} tool calls, {total['failed']} failed")
        return out

    def plans(self) -> dict[str, tuple[int | None, str]]:
        """Each civ's newest plan, with the turn it was set."""
        out: dict[str, tuple[int | None, str]] = {}
        for entry in self.match.turns:
            for index, plan in (entry.get("plans") or {}).items():
                if p := self.match.player(index):
                    out[p["civ"]] = (entry["turn"] - 1, plan)
        for s in self.match.live.get("seats") or ():
            if s.get("plan"):
                out[s["civ"]] = (s.get("plan_turn"), s["plan"])
        return out

    def war_starts(self) -> dict[frozenset, int]:
        return {frozenset((e["owner"], e["from"])): t["turn"] for t in self.match.turns for e in t.get("events") or ()
                if e.get("kind") == "war_declared" and e.get("from") is not None}

    def call_totals(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for t in self.match.turns:
            for index, c in (t.get("calls") or {}).items():
                total = out.setdefault(index, {"ok": 0, "failed": 0})
                total["ok"] += c.get("ok", 0)
                total["failed"] += c.get("failed", 0)
        return out

    def leader_since(self) -> tuple[int, int] | None:
        m = self.match
        if (leader := m.leader(m.turns[-1])) is None:
            return None
        since = m.turns[-1]["turn"]
        for t in reversed(m.turns):
            if m.leader(t) != leader:
                break
            since = t["turn"]
        return leader, since


def parse_lines(content: str, match: Match) -> list[dict]:
    """The lines in an LLM reply (JSON, maybe in a code fence), cleaned up for speech."""
    found = re.search(r"\{.*\}", content, re.S)
    if found is None:
        raise ValueError(f"no JSON in the reply: {content[:120]!r}")
    speakers = {name.lower(): speaker for speaker, (name, _, _) in CASTERS.items()} | {s: s for s in CASTERS}
    lines = []
    for item in json.loads(found[0]).get("lines") or ():
        speaker = speakers.get(str(item.get("speaker")).lower()) if isinstance(item, dict) else None
        if speaker and (text := spoken(item.get("text"))):
            own, other = (CASTERS[s][0] for s in (speaker, PBP if speaker == COLOR else COLOR))
            text = re.sub(rf"\b{own}\b", other, text)   # "a fine move, Ada" from Ada herself was meant for Max
            lines.append({"speaker": speaker, "text": text, "focus": match.civ_named(item.get("focus"))})
    if not lines:
        raise ValueError(f"no lines in the reply: {content[:120]!r}")
    return lines


def spoken(text) -> str:
    """A line as it is said: no stage directions or speaker prefix, "bleep" for a blocked or masked word (a voice
    could read "s***" as the word), at most MAX_WORDS words (whole sentences)."""
    text = MASKED.sub("bleep", str(text or ""))   # before its stars read as a stage direction
    text = BLOCKED.sub("bleep", re.sub(r"\*[^*]*\*|\[[^\]]*\]", "", text))
    text = " ".join(re.sub(r"^\s*(Max|Ada)\s*:\s*", "", text).split())
    if len(text.split()) <= MAX_WORDS:
        return text
    kept: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if len(" ".join([*kept, sentence]).split()) > MAX_WORDS:
            break
        kept.append(sentence)
    return " ".join(kept) or " ".join(text.split()[:MAX_WORDS]) + "…"


def wav_chunks(wav: bytes):
    """(id, offset of its data, declared size) of each chunk of a WAV file."""
    if wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    pos = 12
    while pos + 8 <= len(wav):
        cid, size = wav[pos:pos + 4], int.from_bytes(wav[pos + 4:pos + 8], "little")
        yield cid, pos + 8, size
        if cid == b"data":
            return
        pos += 8 + size + (size & 1)


def fixed_wav(wav: bytes) -> bytes:
    """A streamed WAV (sizes left at 0xFFFFFFFF, as text-to-speech APIs send it) with its real sizes, so a browser
    knows its length."""
    data = next((at for cid, at, _ in wav_chunks(wav) if cid == b"data"), None)
    if data is None:
        raise ValueError("a WAV file without audio")
    out = bytearray(wav)
    out[4:8] = (len(wav) - 8).to_bytes(4, "little")
    out[data - 4:data] = (len(wav) - data).to_bytes(4, "little")
    return bytes(out)


def leveled(wav: bytes) -> bytes:
    """A 16-bit PCM WAV scaled to LEVEL_DBFS, or as close as it gets without clipping; other WAVs as they are."""
    chunks = {cid: at for cid, at, _ in wav_chunks(wav)}
    fmt, data = chunks.get(b"fmt "), chunks.get(b"data")
    if fmt is None or data is None or wav[fmt:fmt + 2] != b"\x01\x00" or wav[fmt + 14:fmt + 16] != b"\x10\x00":
        return wav
    end = data + (len(wav) - data) // 2 * 2
    samples = array.array("h", wav[data:end])
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples or not (peak := max(max(samples), -min(samples))):
        return wav
    rms = math.sqrt(math.fsum(x * x for x in samples) / len(samples))
    gain = min(32767 * 10 ** (LEVEL_DBFS / 20) / rms, 32767 / peak)
    out = array.array("h", (max(-32768, min(32767, round(x * gain))) for x in samples))
    if sys.byteorder == "big":
        out.byteswap()
    return wav[:data] + out.tobytes() + wav[end:]


def wav_seconds(wav: bytes) -> float:
    byte_rate = None
    for cid, at, size in wav_chunks(wav):
        if cid == b"fmt ":
            byte_rate = int.from_bytes(wav[at + 8:at + 12], "little")
        elif cid == b"data" and byte_rate:
            return min(size, len(wav) - at) / byte_rate
    raise ValueError("a WAV file without a format or audio")


def failure(e: Exception) -> str:
    """What went wrong, with an HTTP error's reply, which says why."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read(300).decode(errors="replace")
        except OSError:
            body = ""
        return f"HTTP {e.code} {' '.join(body.split())}"
    return str(e) or repr(e)


def handler(caster: Caster) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/health":
                return self.reply(200, b"ok", "text/plain")
            if url.path == "/cast.json":
                since = urllib.parse.parse_qs(url.query).get("since", ["0"])[0]
                if not re.fullmatch(r"-?\d+", since):
                    return self.reply(400, b"since is a line id: the last one you have, or 0", "text/plain")
                return self.reply(200, json.dumps(caster.lines_since(int(since))).encode(), "application/json")
            found = re.fullmatch(r"/audio/(\d+)\.wav", url.path)
            if found and (wav := caster.audio.get(int(found[1]))) is not None:
                return self.reply(200, wav, "audio/wav")
            self.reply(404, b"not found", "text/plain")

        def reply(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args) -> None:
            pass

    return Handler


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--data", required=True, help="the env's live view, e.g. http://127.0.0.1:41589/live")
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--model", default="anthropic/claude-haiku-4-5", help="the model that writes the lines")
    p.add_argument("--tts-model", default="openai/gpt-4o-mini-tts", help="the model that speaks them")
    p.add_argument("--title", help="the broadcast's title, for the intro")
    args = p.parse_args()
    base_url, api_key = os.environ.get("CAST_BASE_URL", ""), os.environ.get("CAST_API_KEY", "")
    if not base_url or not api_key:
        print("caster: set CAST_BASE_URL (the endpoint, e.g. https://your-litellm-proxy) and CAST_API_KEY",
              file=sys.stderr)
        return 2
    caster = Caster(args.data, base_url, api_key, model=args.model, tts_model=args.tts_model, title=args.title)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(caster))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    caster.log(f"Casting {caster.data_url} on http://127.0.0.1:{args.port}/cast.json: "
               f"{' and '.join(name for name, _, _ in CASTERS.values())}, written by {args.model}, "
               f"voiced by {args.tts_model}")
    caster.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
