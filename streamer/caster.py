"""Two AI casters for a match's live stream: a play-by-play voice and a colour analyst talk over the game, reading
the same match data as the viewer (GET <live>/data.json, docs/viewer.md). Each caster is an agent: it writes its own
line in answer to the other's, and looks the match up with tools first (the standings at any turn, a civ in depth, a
stat's trend, events, battles, diplomacy, the turn being played, a city, what the desk has said), while the analyst
researches storylines in the background for both. A text-to-speech model voices the lines, all through an
OpenAI-compatible endpoint such as LiteLLM (CAST_BASE_URL, CAST_API_KEY), and the stream page plays them with
captions:

    GET /cast.json?since=<id>  {"lines": [{"id", "speaker", "name", "text", "audio", "seconds", "turn", "focus",
                                "kind"}], "speaking_until": <unix seconds>}
    GET /audio/<id>.wav, GET /health

It paces itself to the audio: the next line of a beat (the intro, the biggest new events, the players' messages and
notes, analysis, and the outro at GAME OVER) is written so that it lands when about LEAD_SECONDS of speech are left,
and a live line looks things up only while the audio already queued outlasts the lookup, the line and its voicing.
Stdlib only, as it runs in the streamer image's system Python; the key never appears in what it prints."""

from __future__ import annotations

import argparse
import array
import codecs
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
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PBP, COLOR = "pbp", "color"
SEATS = {PBP: "play_by_play", COLOR: "analyst"}   # the speakers as a task's broadcast names them
MODEL, TTS_MODEL = "anthropic/claude-haiku-4-5", "openai/gpt-4o-mini-tts"
CASTERS = {  # speaker: name, text-to-speech voice, how that voice sounds; the defaults a task's broadcast may change
    PBP: ("Max", "ash", "An esports play-by-play caster calling a live strategy match. High energy, quick and punchy, "
                        "smiling; build excitement on the big moments (wars, captured cities, lead changes) and land "
                        "the names and numbers crisply. Never shouting."),
    COLOR: ("Ada", "sage", "A calm, sharp esports analyst at the casters' desk. Measured and confident, with a dry, "
                           "amused wit; lean on the key number or word; steady and unhurried, warm but precise."),
}
POLL_SECONDS = 2
LEAD_SECONDS = 4        # speech left when the next line lands: writing and voicing it starts that much earlier
LEVEL_DBFS = -22        # every line's loudness (RMS): the voices come back from text-to-speech ~12 dB apart
GAP_SECONDS = 0.5       # between two lines
RETRY_SECONDS = 5       # after a failed LLM call
FRESH_SECONDS = 75      # an event or message not called by then is old news
NEWS_TURNS = 3          # nor is one from more turns ago, also when joining a game under way
MEMORY = 16             # lines the casters see in each call, so they don't repeat themselves (`said` has the rest)
AUDIO_KEPT = 200
LINES_KEPT = 2000       # the broadcast's lines kept for `said` and the page
MAX_WORDS = 30
MAX_TOKENS = 1500       # a call's reply: a line or a lookup, after whatever thinking the model does first
WORDS_PER_SECOND = 2.5  # a line's length when its audio failed
CHAT_SECONDS = 45       # a model call's timeout
SPEECH_SECONDS = 15     # a voicing's: a voice takes about 2.5 s, and one that hangs is dead air on the stream
LOOKUP_ROUNDS = 3       # a live line's rounds of lookups before it must speak
CALL_SECONDS = 2.0      # a model call's time and a line's voicing, until measured (a running average after that)
VOICE_SECONDS = 1.5
LANDING_SECONDS = 1.5   # a line lands this long before the audio runs out: the gap between lines and the page's poll
LOOKUPS_AT_ONCE = 4     # tool calls answered in one round
MAX_LINES = 4           # a beat's lines, counting the answers to questions it asks
LINE_FAILS = 3          # failed lines in a row that end a beat
OUTRO_FAILS = 6         # the outro's: the stream lingers a minute after GAME OVER for it
REFUSED = re.compile(r"(?=.*\b(?:tools?|tool_choice|function(?:s| call(?:ing)?)?)\b)(?=.*(?:not supported|unsupported|"
                     r"does ?n[o']t support|not (?:be )?enabled|not available|not allowed|isn't supported|"
                     r"requires --enable-auto-tool-choice))",
                     re.IGNORECASE | re.DOTALL)   # a 400 that turns tools (or a forced call) down, not a bad generation
ADAPTATIONS = ((re.compile(r"max_completion_tokens"), "max_completion_tokens"),   # what a 400 can ask a model's
               (re.compile(r"reasoning_effort.{0,80}'none'", re.DOTALL), "reasoning_effort"))   # calls for
TEXT_ALONE = "(Text alone doesn't go on air: call say with your line, or look something up first.)"
OUT_OF_TIME = "(You are live and out of time: call say with your line now.)"
RESEARCH_SECONDS = 45   # the analyst's research: at most this often, and only once something new happened
RESEARCH_ROUNDS = 6
NOTEBOOK = 8            # talking points kept
POINT_BEATS = 2         # a point is in this many beats' calls, then dropped
POINT_TURNS = 15        # or once it is this many turns old
POINT_CHARS = 300
TOOL_CHARS = 1500       # a tool's answer, at most
EVENTS = ("civ_destroyed", "city_captured", "city_destroyed", "war_declared", "wonder_built", "landing", "contact",
          "peace_signed", "trade", "lead_change", "era_entered", "government_changed", "units_upgraded",
          "city_founded")   # the events worth a call, the biggest first
BREAKING = EVENTS[:4]   # news that cuts into a beat under way
SEATS_ONLY = ("contact", "era_entered", "units_upgraded")   # news when a model's civ is in it, not between AIs
DIPLOMACY = ("war_declared", "peace_signed", "trade", "contact", "civ_destroyed")
DOMINATION, CULTURE_GOAL, CITY_CULTURE_GOAL = 2 / 3, 100000, 20000   # Civ III's victories (docs/protocol.md)
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
    "wars": "the wars: who is fighting whom, since when, the army sizes on each side and the battle record",
    "clock": "the clock: who is still thinking this turn and for how long, what they have done so far, who ended "
             "fast, and who keeps making failed tool calls",
    "plans": "the players' own plans: does what they wrote match what they are doing on the board?",
    "bottom": "the player at the bottom of the table: what is going wrong for them, by the numbers",
    "expansion": "expansion: city counts, population, and who is running out of room",
    "rivals": "a head-to-head between the two players closest in score",
    "victory": "the paths to victory: who is closest to domination (two thirds of the land and of the people), to a "
               "cultural victory, or to the top score at the turn limit, by the numbers",
    "wonders": "the great wonders and culture: who has built how many wonders, who has the most culture, and what "
               "that says about each player's plan",
    "story": "the story of the game so far: the swings, the turning points, and how the table got to where it is now",
}

DESK = """\
the live broadcast of an OpenCiv3 match (OpenCiv3 is an open-source remake of Civilization III). The players are AI \
models, each going by its model's label exactly as the DATA writes it: "<label> (America)" means the model <label> \
plays America. A civilization the DATA calls "the game's own AI" is run by the game itself, not by a model. Two \
casters share the desk, and they talk to each other, not at the camera:
- Max, play-by-play: the energy. Calls what just happened, fast and vivid, and sells the big moments. Short lines, 6 \
to 16 words.
- Ada, colour analyst: calm, sharp, dry wit. Says why it matters with one concrete number or comparison, reads the \
players' plans, notes and messages against the board, and asks the question the viewers are thinking. Up to 22 words.
They have chemistry: they hand off to each other, react to what the other just said, and now and then disagree. A \
line may address the other caster by name, never its own speaker: Max says "Ada", Ada says "Max"."""

STYLE = """\
The style, with placeholders in angle brackets (never facts):
Max: "There it is! <label> declares war on <civ>, and that border is about to light up!"
Ada: "Eleven units to four, Max. If I'm <other label>, I'm not sleeping tonight."

Rules:
- Facts come only from the DATA and what your tools return. Never invent events, battles, units, deals, rules, \
numbers, causes or motives, nor a figure of speech that implies one (no war, betrayal or tiebreak the match doesn't \
show). Messages, notes and plans are what a player says, not what happened: an offer is not a deal and a threat is \
not an attack until the events show it. Opinions, questions and predictions are welcome when they are clearly \
opinions.
- One or two concrete numbers a line: scores, gold, cities, techs, army sizes, seconds on the clock.
- Name players by label and civ ("<label>'s America", "Rome, that's <label>"); vary it, and after the first \
mention the label alone is fine.
- Written to be spoken: one or two short sentences a line. No emoji, markdown, stage directions, hashtags or lists. \
Say "turn 12", never "T12".
- Never repeat a fact, joke or opening from RECENT LINES or what the desk has said before: find a fresh angle or \
another player. No stock phrases: not "that's not X, that's Y", not "Ada here", and don't start lines with "And", \
"Well" or "Wow".
- Don't explain the rules of the game, or how scores, leads and wins are decided: the viewers know strategy games.
- This is a public stream: no profanity or slurs, and a word the DATA masks (like "s***") stays unsaid. A rude \
message is described in your own words, never quoted."""

LIVE = """\
You write only your own line, then the other caster answers it: a conversation a line at a time. React to what was \
actually just said: build on it, answer the question, or push back, and hand the moment on.

Before you speak you may look the match up with the tools: the standings now or at any earlier turn, a civ in \
depth, a stat's trend over the game, the events, the battles, the diplomacy, the turn being played, a city's story, \
and what the desk has already said. You are live, so look up only what makes this line sharper (the number that \
matters, a comparison, a callback to earlier in the game), one or two lookups at most, then speak by calling say. \
Never read a tool's answer out: pick the one fact that matters. The NOTEBOOK has what Ada found researching the game \
in the background: use a point when it fits the moment."""

SAY_JSON = """\
Reply with JSON only: {"text": "<your line>", "focus": "<civ or null>"}. "focus" is the civ the line is mostly about, \
else null. Inside the text, quote with single quotes, never double quotes."""

RESEARCH = """\
The broadcast is running, and you are preparing for what comes next: dig through the match with the tools and jot \
down talking points the desk can use in the next few minutes. A good point is a fact with its numbers that the \
standings alone don't show: a streak or a swing, a comparison over time, a player's plan or promise against what it \
actually did, a battle record, a callback to an earlier moment, the player nobody has talked about. Each point is one \
or two sentences of facts, for the casters to use, not a line to read out. Don't jot what the NOTEBOOK or the RECENT \
LINES already have. Look up what you need, jot one to three points with jot, then stop: reply with no tool call.

Rules: facts come only from the DATA and what your tools return; never invent events, numbers, causes or motives. \
Messages, notes and plans are what a player says, not what happened."""


def tool(name: str, description: str, properties: dict, required: tuple = ()) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": list(required)}}}


CIV = {"type": "string", "description": "a civ, or a player's label or name"}
STATS = ("score", "cities", "pop", "techs", "gold", "military", "culture", "land", "people")
LOOKUPS = [
    tool("standings", "The table at a turn, best score first: score, cities, population, techs, gold, government, "
                      "military units, shares of the land and people, wars.",
         {"turn": {"type": "integer", "description": "an earlier turn; leave out for now"}}),
    tool("civ", "One civ in depth: its score and rank now and 10, 25 and 50 turns ago, its cities, its army by unit "
                "type, gold, research, culture and wonders, wars, its record of cities founded, taken and lost, its "
                "plan, notes and messages, and its tool calls.", {"civ": CIV}, ("civ",)),
    tool("trend", "A stat over the game (or its last turns), at most 12 points a civ.",
         {"stat": {"type": "string", "enum": list(STATS)},
          "civs": {"type": "array", "items": CIV, "description": "leave out for every civ"},
          "turns": {"type": "integer", "description": "only the last this many turns"}}, ("stat",)),
    tool("events", "The game's events, newest last: city_founded, city_captured, city_destroyed, civ_destroyed, "
                   "war_declared, peace_signed, lead_change, tech_learned, government_changed, wonder_built, "
                   "era_entered, contact, trade, units_upgraded, landing and more.",
         {"kind": {"type": "string"}, "civ": CIV, "since_turn": {"type": "integer"},
          "until_turn": {"type": "integer"}}),
    tool("battles", "The fights: wins and losses attacking and defending, the unit types that won, cities taken, and "
                    "the latest battles; with civ and other, only theirs with each other.",
         {"civ": CIV, "other": CIV, "since_turn": {"type": "integer"}}),
    tool("diplomacy", "Wars, peace, trades, first contacts and the players' messages, in order.",
         {"civ": CIV, "other": CIV}),
    tool("turn_now", "The turn being played: who is still thinking and for how long, tool calls, what each seat has "
                     "done so far, notes and plans; with a civ, also what it did the turn before.", {"civ": CIV}),
    tool("city", "A city's story: who founded it, its size over time, who took it, what it builds, its wonders.",
         {"name": {"type": "string"}}, ("name",)),
    tool("said", "What the desk has already said in this broadcast that has these words, newest last: for callbacks, "
                 "and to keep from repeating.", {"query": {"type": "string"}}),
]
LOOKUP_ARGS = {t["function"]["name"]: set(t["function"]["parameters"]["properties"]) for t in LOOKUPS}
REQUIRED = {t["function"]["name"]: t["function"]["parameters"]["required"] for t in LOOKUPS}
SAY = tool("say", "Speak your line, live: one or two short sentences, written to be spoken. This ends your turn.",
           {"text": {"type": "string"}, "focus": {"type": "string", "description": "the civ the line is mostly "
                                                                                  "about; empty for none"}},
           ("text",))
JOT = tool("jot", "Jot a talking point in the desk's notebook: one or two sentences of facts with their numbers.",
           {"point": {"type": "string"}, "civ": CIV}, ("point",))


class Unknown(Exception):
    """A lookup of something the match doesn't have: the model reads why."""


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
    """A subject the desk talks about, a line at a time: the speakers alternate, `first` first."""
    kind: str             # intro, event, message, color or outro
    task: str             # what the casters do now
    first: str            # who speaks first
    count: int            # how many lines (a question to the other caster earns an answer, up to MAX_LINES)
    moments: list[Moment] = field(default_factory=list)
    topic: str | None = None
    lines: list[dict] = field(default_factory=list)   # written so far
    cut_in: bool = False  # it breaks off a beat under way
    begun: bool = False
    fails: int = 0
    points: list[dict] = field(default_factory=list)   # the notebook as it was when the beat began

    @property
    def speaker(self) -> str:
        """Who speaks the next line."""
        return self.first if len(self.lines) % 2 == 0 else (PBP if self.first == COLOR else COLOR)

    @property
    def done(self) -> bool:
        return len(self.lines) >= self.count


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
        if p.get("barbarian"):
            return p["civ"]
        name = p.get("name") or p.get("label")   # the broadcast's name for the seat ("Opus 5.5"), else its label
        return f"{name} ({p['civ']})" if name else f"{p['civ']} (the game's own AI)"

    def who_civ(self, civ: str) -> str:
        return next((self.who(p["index"]) for p in self.civs if p["civ"] == civ), civ)

    def civ_named(self, name, players: list[dict] | None = None) -> str | None:
        """The civ a line's `focus` names, by civ, label or name; None for anything else ("none" too)."""
        p = self.named(name, self.civs if players is None else players)
        return p["civ"] if p else None

    @staticmethod
    def named(name, players: list[dict]) -> dict | None:
        name = name.strip().lower() if isinstance(name, str) else ""
        if name in ("", "none", "null"):
            return None
        return next((p for p in players if name in {str(p[k]).lower() for k in ("civ", "label", "name") if p.get(k)}),
                    None)

    def leader(self, entry: dict) -> int | None:
        """The player with the top score in a turn entry; None while the top score is tied."""
        scores = {int(i): s[0] for i, s in (entry.get("scores") or {}).items()}
        top = max(scores.values(), default=None)
        leaders = [i for i, s in scores.items() if s == top]
        return leaders[0] if len(leaders) == 1 else None

    def entry(self, turn: int) -> dict:
        return next((t for t in self.turns if t["turn"] == turn), {})

    def at(self, turn: int) -> dict:
        """The newest entry at or before `turn`; {} before the first."""
        return next((t for t in reversed(self.turns) if t["turn"] <= turn), {})

    def military(self, entry: dict, index: int | str) -> int:
        st = (entry.get("stats") or {}).get(str(index)) or {}
        if "military" in st:
            return st["military"]
        civilian, types = set(self.meta.get("civilian") or ()), self.meta.get("unit_types") or []
        return sum(1 for u in entry.get("units") or ()
                   if str(u[3]) == str(index) and 0 <= u[4] < len(types) and types[u[4]] not in civilian)

    def unit_type(self, index: int) -> str:
        types = self.meta.get("unit_types") or []
        return types[index] if 0 <= index < len(types) else "unit"


class Caster:
    """Writes and voices the casters' lines a line at a time, and keeps them for the stream page."""

    def __init__(self, data_url: str, base_url: str, api_key: str, *, model: str = MODEL, tts_model: str = TTS_MODEL,
                 casters: dict[str, tuple[str, str, str]] = CASTERS, models: dict[str, str] | None = None,
                 title: str | None = None, clock: Callable[[], float] = time.time):
        self.data_url = data_url.split("?")[0].rstrip("/")
        self.base_url = base_url.rstrip("/").removesuffix("/v1")
        self.api_key = api_key
        self.tts_model, self.title = tts_model, title
        self.models = {s: (models or {}).get(s) or model for s in CASTERS}   # each caster's own, else the shared one
        self.casters = casters
        self.clock = clock
        self.lock = threading.Lock()     # the lines, the audio and the notebook: the page and the research read them
        self.data = threading.RLock()    # the match: the poll writes it, the lookups read it
        self.lines: list[dict] = []
        self.line_count = 0
        self.audio: dict[int, bytes] = {}
        self.speaking_until = 0.0
        self.polled_at = float("-inf")
        self.retry_at = 0.0
        self.writing_seconds: dict[str, float] = {}   # how long each caster's last line took to write and voice
        self.data_down = False
        self.no_tools: set[str] = set()   # models the endpoint turned tools down for: they write from the DATA
        self.no_force: set[str] = set()   # models that take tools but not a forced say
        self.call_seconds: dict[str, float] = {}   # each model's live calls, a running average
        self.adaptations: dict[str, set[str]] = {}   # what the endpoint asked each model's calls for (ADAPTATIONS)
        self.voice_seconds = VOICE_SECONDS
        self.game_line = 0           # the last line before this game's first: RECENT LINES and `said` start after it
        self.researched_at = float("-inf")
        self.researched_turn: int | None = None
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
        self.beat: Beat | None = None
        with self.lock:
            self.notebook: list[dict] = []
            self.game_line = getattr(self, "line_count", 0)

    @property
    def names(self) -> tuple[str, str]:
        """The play-by-play caster's name and the analyst's."""
        return self.casters[PBP][0], self.casters[COLOR][0]

    def log(self, message: str) -> None:
        if self.api_key:
            for form in (self.api_key, repr(self.api_key)[1:-1]):
                message = message.replace(form, "<cast key>")
        print(message, flush=True)

    # ---- the loop ----

    def run(self) -> None:
        threading.Thread(target=self.research_loop, daemon=True).start()
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
        upcoming = self.beat.speaker if self.beat is not None and not self.beat.done else None   # else not chosen yet
        writing = self.writing_seconds.get(upcoming, max(self.writing_seconds.values(), default=0.0))
        due = self.speaking_until - now <= LEAD_SECONDS + writing
        if self.finished or self.data_down or now < self.retry_at or not (due or self.match.over):
            return   # the outro waits for no one; with the match data gone, there is nothing to talk about
        beat = self.beat
        if beat is None or beat.done or self.breaking(beat) or (self.match.over and beat.kind != "outro"):
            under_way = beat is not None and not beat.done
            beat = self.beat = self.next_beat()
            if beat is None:
                return
            beat.cut_in = under_way and beat.kind == "event"
        self.speak(beat)

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
        with self.data:
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

    def news(self) -> list[Moment]:
        """The fresh events worth a call, the biggest first."""
        m, now = self.match, self.clock()
        self.pending = [x for x in self.pending if now - x.seen <= FRESH_SECONDS and x.turn >= m.turn - NEWS_TURNS]
        return sorted((x for x in self.pending if x.kind in EVENTS and self.newsworthy(x)),
                      key=lambda x: (EVENTS.index(x.kind), -x.turn))

    def breaking(self, beat: Beat) -> bool:
        """Whether news has come in that is bigger than the beat under way, which then gives way to it."""
        if not beat.lines or beat.done or beat.kind in ("intro", "outro"):
            return False
        rank = min((EVENTS.index(x.kind) for x in beat.moments if x.kind in EVENTS), default=len(EVENTS))
        return any(x.kind in BREAKING and EVENTS.index(x.kind) < rank for x in self.news())

    def next_beat(self) -> Beat | None:
        m = self.match
        if not m.started or self.finished:
            return None
        if m.over:
            return self.outro()
        if not self.introduced:
            return self.intro()
        news = self.news()
        events = news[:3] + [x for x in news[3:] if any((x.kind, x.turn) == (e.kind, e.turn) for e in news[:3])][:3]
        pbp, color = self.names
        if events:
            return Beat("event", "Call these moments from the board, the biggest first:\n"
                        + "\n".join(f"- {x.text}" for x in events)
                        + f"\n{pbp} calls it with energy; {color} answers with what it means, one number from the "
                          "match. Between your lines, call every moment above.",
                        PBP, 2 if len(events) == 1 else 3, events)
        if messages := [x for x in self.pending if x.kind == "message"][-3:]:
            return Beat("message", "New diplomacy between the players, sent where everyone can see it:\n"
                        + "\n".join(f"- {x.text}" for x in messages)
                        + f"\n{pbp} reads it out, quoting it unless it is rude; {color} reads between the lines: do "
                          "the sender's wars, army or plan back it up?",
                        PBP, 2 if len(messages) == 1 else 3, messages)
        notes = self.notes_to_read()
        if notes and self.last_kind != "message":
            return Beat("message", "The players' own notes on the turn they just played:\n"
                        + "\n".join(f"- {x.text}" for x in notes)
                        + f"\n{pbp} relays one, paraphrased; {color} checks it against the board: do the numbers "
                          "back it up?", PBP, 2, notes)
        return self.color()

    def newsworthy(self, moment: Moment) -> bool:
        e, m = moment.event, self.match
        if moment.kind == "lead_change":   # as the viewer shows it: the new leader has held the lead since
            return e["owner"] != self.called_leader and all(
                m.leader(t) == e["owner"] for t in m.turns if t["turn"] >= moment.turn)
        if moment.kind == "city_founded":
            cities = m.entry(moment.turn).get("cities") or ()
            return sum(c[3] == e["owner"] for c in cities) <= EARLY_CITIES
        seat = lambda i: bool((m.player(i) or {}).get("label")) if i is not None else False   # noqa: E731
        if moment.kind in SEATS_ONLY:
            return seat(e.get("owner")) and (moment.kind != "contact" or seat(e.get("from")))
        if moment.kind == "landing":
            return seat(e.get("owner")) or seat(e.get("from"))
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
        pbp, color = self.names
        return Beat("intro", f"Open the broadcast. {pbp} welcomes everyone{title}; between them, {pbp} and {color} "
                             f"name every player with its civ: {roster}. {color} sets the stakes: {models} AI models"
                             + (f", {limit} turns" if limit else "")
                             + ", and they can message each other, so expect alliances, threats and betrayals. The "
                             f"last line throws to the action.{joined} Make it big: this is the opening.", PBP, 3)

    def outro(self) -> Beat:
        pbp, color = self.names
        return Beat("outro", f"The game is over: {self.result()}. {pbp} calls the result with the final score; {color} "
                             f"says why, with one number, and names one standout from the rest of the table; {pbp} "
                             "thanks the viewers and signs off for both of you. These are the last lines of the "
                             "broadcast.",
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
                "rivals": len(m.civs) >= 2, "victory": bool(self.race()),
                "wonders": any(s.get("wonders") for s in stats.values()), "story": len(m.turns) >= 20}
        topic = min((t for t in TOPICS if fits.get(t, True)), key=lambda t: self.topics_used.get(t, -1))
        pbp, color = self.names
        return Beat("color", f"Nothing new to call this moment, so the desk fills with analysis. The angle: "
                             f"{TOPICS[topic]}; or a storyline from the NOTEBOOK, if one is fresher. {color} opens "
                             f"with a sharp observation and a number; {pbp} reacts.",
                    COLOR, 2,
                    topic=topic)

    # ---- writing and voicing a line ----

    def begin(self, beat: Beat) -> None:
        """A beat's first line is being written: it reads the notebook as it is now."""
        beat.begun = True
        with self.lock:
            turn = self.match.turn
            self.notebook = [p for p in self.notebook if p["beats"] < POINT_BEATS
                             and turn - POINT_TURNS <= p["turn"] <= turn]
            beat.points = list(self.notebook)

    def called(self, beat: Beat) -> None:
        """A beat's first line is out: what it calls is called, whether or not it is cut short. A beat that fails
        before it says anything calls nothing, so its news, its topic and the intro are there for the next one."""
        self.beats += 1
        self.last_kind = beat.kind
        self.introduced = True
        called = {x.key for x in beat.moments}
        self.pending = [x for x in self.pending if x.key not in called]
        for x in beat.moments:
            if x.kind == "lead_change":
                self.called_leader = x.event["owner"]
        if beat.topic:
            self.topics_used[beat.topic] = self.beats
        with self.lock:
            for p in beat.points:
                p["beats"] += 1

    def speak(self, beat: Beat) -> None:
        """Writes the beat's next line, voices it and publishes it."""
        began, speaker = self.clock(), beat.speaker
        if not beat.begun:
            self.begin(beat)
        line = self.write(beat)
        if line is None:
            self.retry_at = self.clock() + RETRY_SECONDS
            beat.fails += 1
            if beat.fails >= (OUTRO_FAILS if beat.kind == "outro" else LINE_FAILS):
                beat.count = len(beat.lines)   # it ends where it got to, and the desk moves on
                if not beat.lines and beat.topic:   # analysis that keeps failing gives way to another; news waits
                    self.topics_used[beat.topic] = self.beats
                self.finished = beat.kind == "outro"   # the broadcast's last lines are never started again
            return
        beat.fails = 0
        if not beat.lines:
            self.called(beat)
        wav = self.voice(line)
        beat.lines.append(line)
        if (len(beat.lines) == beat.count < MAX_LINES and beat.kind not in ("intro", "outro")
                and line["text"].rstrip().endswith("?")):
            beat.count += 1   # a question to the other caster: it answers
        self.publish(beat, line, wav)
        self.finished = beat.kind == "outro" and beat.done
        self.writing_seconds[speaker] = self.clock() - began

    def prompt(self, beat: Beat) -> str:
        """What the speaker of the beat's next line reads: the DATA, the notebook, the show so far and its task."""
        speaker = beat.speaker
        me = self.casters[speaker][0]
        other = self.casters[COLOR if speaker == PBP else PBP][0]
        recent = self.recent()
        with self.data:
            data = self.summary()
        points = beat.points
        notebook = "\n".join(f"- turn {p['turn']}{', ' + self.match.who_civ(p['civ']) if p['civ'] else ''}: "
                             f"{p['point']}" for p in points)
        n, i = beat.count, len(beat.lines)
        if i == 0:
            where = ("You cut in: the desk was on something else, and this news can't wait." if beat.cut_in
                     else "You open this exchange.")
        else:
            where = f'{beat.lines[-1]["name"]} just said: "{beat.lines[-1]["text"]}"\n' + (
                f"Yours is the last line of this exchange: answer {other}, and land it." if i == n - 1 else
                f"Answer {other}, and keep it moving.")
        return (f"DATA\n{data}\n\n"
                + (f"NOTEBOOK (what {self.casters[COLOR][0]}'s research found; use a point when it fits)\n{notebook}"
                   "\n\n" if notebook else "")
                + f"RECENT LINES (oldest first)\n{recent or '(none: this is the opening)'}"
                f"\n\nNOW\n{beat.task}\n\nYOUR LINE: you are {me}, line {i + 1} of {n}, "
                f"{'6 to 16' if speaker == PBP else 'at most 22'} words. {where}")

    def game_lines(self) -> list[dict]:
        """This game's lines so far."""
        with self.lock:
            return [line for line in self.lines if line["id"] > self.game_line]

    def recent(self) -> str:
        return "\n".join(f"{line['name']}: {line['text']}" for line in self.game_lines()[-MEMORY:])

    def system(self, speaker: str, tools: bool = True) -> str:
        role = "play-by-play" if speaker == PBP else "colour analyst"
        me = CASTERS[speaker][0]
        text = f"You are {me}, the {role} caster on {DESK}\n\n{STYLE}\n\n" + (LIVE if tools else SAY_JSON)
        return renamed(text, self.casters)

    def time_to_look(self, model: str) -> bool:
        """Whether the audio queued outlasts one more of the model's lookups, the line after it, its voicing and its
        landing before the audio runs out."""
        call = self.call_seconds.get(model, CALL_SECONDS)
        return self.clock() + 2 * call + self.voice_seconds + LANDING_SECONDS <= self.speaking_until

    def write(self, beat: Beat) -> dict | None:
        """The beat's next line, from its speaker's agent: a round of lookups at a time while the audio queued
        outlasts it (at most LOOKUP_ROUNDS), then its line. None if the calls failed."""
        speaker = beat.speaker
        model = self.models[speaker]
        tools = model not in self.no_tools
        messages = [{"role": "system", "content": self.system(speaker, tools)},
                    {"role": "user", "content": self.prompt(beat)}]
        if tools and not self.time_to_look(model):
            messages[1]["content"] += f"\n{OUT_OF_TIME}"   # forced or not, it knows
        round_ = 0
        try:
            while round_ <= LOOKUP_ROUNDS:
                body = {"model": model, "max_tokens": MAX_TOKENS, "messages": messages}
                last = round_ == LOOKUP_ROUNDS or not self.time_to_look(model)
                if tools:   # the last round has only say, for a model that can't be forced to it
                    body["tools"] = [SAY] if last else [*LOOKUPS, SAY]
                    if last and model not in self.no_force:
                        body["tool_choice"] = {"type": "function", "function": {"name": "say"}}
                try:
                    message = self.chat(body)
                except urllib.error.HTTPError as e:
                    why = failure(e)
                    if not (tools and e.code == 400 and REFUSED.search(why)):
                        raise ValueError(why) from None
                    if "tool_choice" in body:   # tools, but not a forced say: it is asked to speak instead
                        self.no_force.add(model)
                        self.log(f"caster: {model} turned a forced say down ({why}); its casters are asked instead")
                        continue
                    if round_:
                        raise ValueError(why) from None
                    self.no_tools.add(model)
                    self.log(f"caster: the endpoint turned tools down for {model} ({why}); its caster writes from "
                             "the DATA alone")
                    return self.write(beat)
                calls = message.get("tool_calls") or []
                said = next((c for c in calls if (c.get("function") or {}).get("name") == "say"), None)
                if said is not None:
                    args = arguments(said)
                    try:
                        return self.line(speaker, args.get("text"), args.get("focus"))
                    except ValueError as e:   # nothing to say: it says why, and the caster tries again
                        if round_ == LOOKUP_ROUNDS:
                            raise
                        calls, answers = [said], [{"role": "tool", "tool_call_id": said.get("id") or "call_say",
                                                   "content": f"error: {e}; call say with your line as its text"}]
                else:
                    if not calls:
                        text = str(message.get("content") or "").strip()
                        if not tools or last or not text:
                            return self.line_of(speaker, message.get("content"), message)
                        round_ += 1   # "Let me check the standings first" is not a line: it is told so
                        more = round_ == LOOKUP_ROUNDS or not self.time_to_look(model)
                        messages += [{"role": "assistant", "content": text},
                                     {"role": "user", "content": TEXT_ALONE + (f"\n{OUT_OF_TIME}" if more else "")}]
                        continue
                    answers = self.answers(calls)
                round_ += 1
                if tools and (round_ == LOOKUP_ROUNDS or not self.time_to_look(model)):
                    answers[-1]["content"] += f"\n{OUT_OF_TIME}"
                messages += [assistant(message, calls), *answers]
            raise ValueError(f"no line after {LOOKUP_ROUNDS + 1} calls")
        except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError, http.client.HTTPException) as e:
            self.log(f"caster: the {beat.kind} beat's line failed, retrying in {RETRY_SECONDS} s: {failure(e)}")
        return None

    def chat(self, body: dict) -> dict:
        while True:
            began = self.clock()
            try:
                reply = json.loads(self.post("/v1/chat/completions", self.adapted(body), CHAT_SECONDS))
                break
            except urllib.error.HTTPError as e:
                if e.code != 400 or not self.adapt(body["model"], failure(e)):
                    raise
        if "jot" not in {t["function"]["name"] for t in body.get("tools") or ()}:   # a live call
            seconds = self.call_seconds.get(body["model"], CALL_SECONDS)
            self.call_seconds[body["model"]] = seconds + 0.3 * (self.clock() - began - seconds)
        message = reply["choices"][0]["message"]
        if not isinstance(message, dict):
            raise ValueError("the reply has no message")
        message["finish_reason"] = reply["choices"][0].get("finish_reason")
        for n, call in enumerate(message.get("tool_calls") or ()):
            if isinstance(call, dict) and not call.get("id"):   # one id for the call and its answer, both sent back
                call["id"] = f"call_{n}"
        return message

    def adapt(self, model: str, why: str) -> bool:
        """Takes up what a 400 says the model's calls need (max_completion_tokens for max_tokens, tools without
        reasoning: OpenAI's reasoning models); False when it asks for nothing new."""
        have = self.adaptations.setdefault(model, set())
        new = {name for pattern, name in ADAPTATIONS if pattern.search(why)} - have
        if new:
            have |= new
            self.log(f"caster: {model}'s calls take {', '.join(sorted(new))} from now on ({why[:160]})")
        return bool(new)

    def adapted(self, body: dict) -> dict:
        have = self.adaptations.get(body["model"], set())
        out = dict(body)
        if "max_completion_tokens" in have and "max_tokens" in out:
            out["max_completion_tokens"] = out.pop("max_tokens")
        if "reasoning_effort" in have and out.get("tools"):
            out["reasoning_effort"] = "none"
        return out

    def answers(self, calls: list) -> list[dict]:
        """The tool messages answering a round of lookups, one for each call, in order."""
        out = []
        for n, call in enumerate(calls):
            fn = call.get("function") or {}
            name = fn.get("name")
            answer = (self.lookup(name, fn.get("arguments")) if n < LOOKUPS_AT_ONCE
                      else f"error: at most {LOOKUPS_AT_ONCE} lookups at a time")
            out.append({"role": "tool", "tool_call_id": call.get("id") or f"call_{n}", "content": answer})
        return out

    def line_of(self, speaker: str, content, message: dict) -> dict:
        """A line from a reply without a say call: its JSON ({"text"}, or the old {"lines"}), or its text."""
        content = str(content or "").strip()
        if not content:
            raise ValueError(f"the reply has no text (finish_reason {message.get('finish_reason')})")
        start = content.find("{")
        if start >= 0:
            try:
                reply, _ = json.JSONDecoder().raw_decode(content, start)
            except ValueError:
                reply = None
            if isinstance(reply, dict) and isinstance(reply.get("lines"), list):
                lines = parse_lines(content, self.match, self.casters)
                mine = next((x for x in lines if x["speaker"] == speaker), lines[0])
                return self.line(speaker, mine["text"], mine["focus"])
            if isinstance(reply, dict) and "text" in reply:
                return self.line(speaker, reply.get("text"), reply.get("focus"))
            if reply is not None:
                raise ValueError(f"no line in the reply: {content[:120]!r}")
        other = re.escape(self.casters[COLOR if speaker == PBP else PBP][0])
        said = [x for x in content.splitlines() if x.strip() and not re.match(rf"^\W*{other}\W*:", x)]
        if not said:
            raise ValueError(f"no line in the reply: {content[:120]!r}")
        return self.line(speaker, said[0].strip().strip('"'), None)

    def line(self, speaker: str, text, focus) -> dict:
        """A line as it is said, cleaned up for speech; ValueError when nothing is left."""
        names = tuple(name for name, _, _ in self.casters.values())
        text = spoken(text, names)
        if not text:
            raise ValueError("an empty line")
        own, other = (self.casters[s][0] for s in (speaker, PBP if speaker == COLOR else COLOR))
        # "A fine move, Ada" from Ada was meant for Max; "I'm Ada" is not addressed to anyone.
        text = re.sub(rf"(?:(?<=, )|^){re.escape(own)}(?=\s*(?:[,.!?]|$))", other, text)
        with self.data:
            return {"speaker": speaker, "text": text, "focus": self.match.civ_named(focus)}

    def voice(self, line: dict) -> bytes | None:
        """The line spoken, as a WAV file; None if the speech call failed."""
        _, voice, style = self.casters[line["speaker"]]
        began = self.clock()
        try:
            wav = leveled(fixed_wav(self.post("/v1/audio/speech", {
                "model": self.tts_model, "voice": voice, "input": line["text"], "instructions": style,
                "response_format": "wav"}, SPEECH_SECONDS)))
        except (OSError, ValueError, http.client.HTTPException) as e:
            self.log(f"caster: no voice for a line, captions only: {failure(e)}")
            return None
        self.voice_seconds += 0.3 * (self.clock() - began - self.voice_seconds)
        return wav

    def post(self, path: str, body: dict, timeout: float) -> bytes:
        request = urllib.request.Request(f"{self.base_url}{path}", data=json.dumps(body).encode(), headers={
            "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as r:
            return r.read()

    def publish(self, beat: Beat, line: dict, wav: bytes | None) -> None:
        with self.lock:
            seconds = wav_seconds(wav) if wav else len(line["text"].split()) / WORDS_PER_SECOND
            self.speaking_until = max(self.clock(), self.speaking_until) + seconds + GAP_SECONDS
            self.line_count += 1
            ident = self.line_count
            if wav:
                self.audio[ident] = wav
            for old in [k for k in self.audio if k <= ident - AUDIO_KEPT]:
                del self.audio[old]
            line.update(id=ident, name=self.casters[line["speaker"]][0], audio=f"audio/{ident}.wav" if wav else None,
                        seconds=round(seconds, 2), turn=self.match.turn, kind=beat.kind)
            self.lines.append({k: line[k] for k in ("id", "speaker", "name", "text", "audio", "seconds", "turn",
                                                    "focus", "kind")})
            del self.lines[:-LINES_KEPT]
        self.log(f"[{beat.kind} T{self.match.turn}] {line['name']}: {line['text']}")

    # ---- the analyst's research, in the background ----

    def research_loop(self) -> None:
        while True:
            time.sleep(1)
            try:
                if self.research_due():
                    self.research_once()
            except Exception as e:  # research is a bonus: the show never waits on it
                self.log(f"caster: research: {e!r}")
                self.researched_at = self.clock()

    def research_due(self) -> bool:
        m = self.match
        return (self.models[COLOR] not in self.no_tools and m.started and not m.over and self.introduced
                and not self.data_down and self.clock() - self.researched_at >= RESEARCH_SECONDS
                and m.turn != self.researched_turn)

    def research_once(self) -> int:
        """One session of the analyst digging through the match; returns the points it jotted."""
        self.researched_at = self.clock()
        with self.data:
            game, turn, data = self.match.game, self.match.turn, self.summary()
        self.researched_turn = turn
        recent = self.recent()
        with self.lock:
            notebook = "\n".join(f"- turn {p['turn']}: {p['point']}" for p in self.notebook)
        analyst = CASTERS[COLOR][0]
        messages = [{"role": "system", "content": renamed(f"You are {analyst}, the colour analyst on {DESK}\n\n"
                                                          f"{RESEARCH}", self.casters)},
                    {"role": "user", "content": f"DATA\n{data}\n\nNOTEBOOK\n{notebook or '(empty)'}\n\n"
                                                f"RECENT LINES (oldest first)\n{recent or '(none yet)'}"}]
        jotted = 0
        for _ in range(RESEARCH_ROUNDS):
            try:
                message = self.chat({"model": self.models[COLOR], "max_tokens": MAX_TOKENS, "messages": messages,
                                     "tools": [*LOOKUPS, JOT]})
            except (OSError, ValueError, KeyError, IndexError, TypeError, http.client.HTTPException) as e:
                why = failure(e)
                if isinstance(e, urllib.error.HTTPError) and e.code == 400 and REFUSED.search(why) and not jotted \
                        and len(messages) == 2:
                    self.no_tools.add(self.models[COLOR])   # no research without tools
                self.log(f"caster: research call failed: {why}")
                break
            calls = message.get("tool_calls") or []
            if not calls:
                break
            messages.append(assistant(message, calls))
            looked = 0
            for n, call in enumerate(calls):
                fn = call.get("function") or {}
                if fn.get("name") == "jot":
                    answer = self.jot(game, turn, arguments(call))
                    jotted += answer.startswith("jotted")
                else:
                    looked += 1
                    answer = (self.lookup(fn.get("name"), fn.get("arguments"), "jot") if looked <= LOOKUPS_AT_ONCE
                              else f"error: at most {LOOKUPS_AT_ONCE} lookups at a time")
                messages.append({"role": "tool", "tool_call_id": call.get("id") or f"call_{n}", "content": answer})
        return jotted

    def jot(self, game: str | None, turn: int, args: dict) -> str:
        point = " ".join(str(args.get("point") or "").split())[:POINT_CHARS]
        if not point:
            return "error: jot needs a point"
        with self.data, self.lock:   # a new game can't begin between the check and the jot
            if self.match.game != game:
                return "error: that game is over; a new one has begun"
            civ = self.match.civ_named(args.get("civ"))
            self.notebook.append({"turn": turn, "civ": civ, "point": point, "beats": 0})
            del self.notebook[:-NOTEBOOK]
            return f"jotted ({len(self.notebook)} in the notebook)"

    # ---- the lookups: the match as short text ----

    def lookup(self, name, args, then: str = "say") -> str:
        """A lookup's answer, as the model reads it: an error is text too, never an exception. `then` is the tool the
        caller ends with besides the lookups (say, or jot in research)."""
        if name not in LOOKUP_ARGS:
            return f"error: there is no tool {name!r}; the tools: {', '.join(LOOKUP_ARGS)}, {then}"
        try:
            args = json.loads(args) if isinstance(args, str) and args.strip() else args or {}
        except ValueError:
            return f"error: the arguments are not JSON: {str(args)[:80]!r}"
        if not isinstance(args, dict):
            return "error: the arguments must be an object"
        takes = LOOKUP_ARGS[name]
        needs = [k for k in REQUIRED[name] if args.get(k) in (None, "")]
        if needs:
            return (f"error: {name} needs {', '.join(needs) or 'other arguments'}; it takes "
                    f"{', '.join(sorted(takes)) or 'none'}, got {', '.join(sorted(args)) or 'none'}")
        ignored = sorted(set(args) - takes)
        try:
            with self.data:
                if not self.match.turns:
                    return "error: the game hasn't started"
                out = getattr(self, f"look_{name}")(**{k: v for k, v in args.items()
                                                       if k in takes and v not in (None, "")})
            if ignored:
                out = f"(ignored {', '.join(ignored)}: {name} takes {', '.join(sorted(takes))})\n{out}"
        except Unknown as e:
            return f"error: {e}"
        except Exception as e:  # a model's odd arguments (or a bug) answer with an error, never stop the line
            self.log(f"caster: the {name} lookup failed on {json.dumps(args)[:200]}: {e!r}")
            return f"error: {name} failed on those arguments ({type(e).__name__})"
        return out if len(out) <= TOOL_CHARS else out[:TOOL_CHARS - 1].rsplit("\n", 1)[0] + "\n…"

    def civ_of(self, name) -> dict:
        """A player by civ, label or name, the barbarians too."""
        found = self.match.named(name, self.match.players)
        if found is None:
            civs = ", ".join(self.match.who(p["index"]) for p in self.match.civs)
            raise Unknown(f"no civ {name!r}; the civs: {civs}")
        return found

    def turn_of(self, turn) -> int:
        turn = int(turn)
        first, last = self.match.turns[0]["turn"], self.match.turns[-1]["turn"]
        if not first <= turn <= last:
            raise Unknown(f"no turn {turn} in the data: turns {first} to {last}")
        return turn

    def rank(self, entry: dict, index: str) -> str:
        ranked = sorted((entry.get("scores") or {}).items(), key=lambda kv: -kv[1][0])
        at = next((n for n, (i, _) in enumerate(ranked, 1) if i == index), None)
        return f"{at} of {len(ranked)}" if at else "-"

    def look_standings(self, turn=None) -> str:
        m = self.match
        e = m.at(self.turn_of(turn)) if turn is not None else m.turns[-1]
        out = [f"Turn {e['turn']}" + (f", {e['date']}" if e.get("date") else "") + ":"]
        for rank, (i, s) in enumerate(sorted((e.get("scores") or {}).items(), key=lambda kv: -kv[1][0]), 1):
            st = (e.get("stats") or {}).get(i) or {}
            parts = [f"{rank}. {m.who(i)}: {s[0]}",
                     f"{s[1]} {'city' if s[1] == 1 else 'cities'}, pop {s[2]}, {s[4]} techs"]
            parts += [f"{st['gold']} gold"] if "gold" in st else []
            parts += [st["government"]] if st.get("government") else []
            parts.append(f"{m.military(e, i)} military")
            if "land" in st:
                parts.append(f"{st['land']:.0%} of the land, {st['pop']:.0%} of the people")
            parts += [f"at war with {', '.join(m.who(x) for x in st['at_war'])}"] if st.get("at_war") else []
            parts += ["ELIMINATED"] if s[5] else []
            out.append("; ".join(parts))
        return "\n".join(out)

    def look_civ(self, civ) -> str:
        m = self.match
        p = self.civ_of(civ)
        i, index = str(p["index"]), p["index"]
        last = m.turns[-1]
        score = (last.get("scores") or {}).get(i)
        if score is None:
            return f"{m.who(i)} has no score in the data yet"
        st = (last.get("stats") or {}).get(i) or {}
        out = [f"{m.who(i)}, turn {last['turn']}: score {score[0]}, {self.rank(last, i)}"
               + ("; ELIMINATED" if score[5] else "")]
        back = []
        for turns in (10, 25, 50):
            e = m.at(last["turn"] - turns)
            if e and i in (e.get("scores") or {}) and e["turn"] < last["turn"]:
                back.append(f"turn {e['turn']}: {e['scores'][i][0]}, {self.rank(e, i)}")
        if back:
            out.append("Before: " + "; ".join(dict.fromkeys(back)))
        cities = sorted((c for c in last.get("cities") or () if c[3] == index), key=lambda c: -c[4])
        out.append(f"{len(cities)} cities, pop {score[2]}: " + ", ".join(
            f"{c[2]} {c[4]}" + (" (capital)" if c[5] else "") + (f" building {c[6]}" if c[6] else "")
            for c in cities[:8]) + (f", and {len(cities) - 8} more" if len(cities) > 8 else ""))
        civilian = set(m.meta.get("civilian") or ())
        units = Counter(m.unit_type(u[4]) for u in last.get("units") or () if u[3] == index)
        army = [f"{n} {t}" for t, n in units.most_common() if t not in civilian]
        others = sum(n for t, n in units.items() if t in civilian)
        out.append(f"Army: {', '.join(army[:8]) or 'none'}" + (f"; {others} civilian units" if others else ""))
        gold = m.at(last["turn"] - 10)
        was = ((gold.get("stats") or {}).get(i) or {}).get("gold") if gold and gold is not last else None
        parts = [f"{score[4]} techs"]
        parts += [f"{st['gold']} gold" + (f" ({st['gold'] - was:+d} in 10 turns)" if was is not None else "")] \
            if "gold" in st else []
        parts += [st["government"]] if st.get("government") else []
        parts += [f"researching {st['research']}"] if st.get("research") else []
        parts += [f"{st['culture']:,} culture"] if st.get("culture") else []
        if "land" in st:
            parts.append(f"{st['land']:.0%} of the land, {st['pop']:.0%} of the people")
        out.append("; ".join(parts))
        events = [(t["turn"], e) for t in m.turns for e in t.get("events") or ()]
        wonders = [e.get("wonder") or e["text"] for _, e in events
                   if e["kind"] == "wonder_built" and e.get("owner") == index]
        if wonders:
            out.append(f"Great wonders: {', '.join(wonders)}")
        record = Counter()
        for _, e in events:
            if e["kind"] == "city_founded" and e.get("owner") == index:
                record["founded"] += 1
            elif e["kind"] == "city_captured":
                record["taken"] += e.get("owner") == index
                record["lost"] += e.get("from") == index
            elif e["kind"] == "city_destroyed":   # its owner is the civ that lost it
                record["lost"] += e.get("owner") == index
        record["razed"] = sum(1 for t in m.turns for b in t.get("battles") or () if b[4] == 3 and b[6] == index)
        out.append(f"Record: {record['founded']} cities founded, {record['taken']} taken, {record['lost']} lost"
                   + (f", {record['razed']} razed" if record["razed"] else ""))
        wars = self.war_starts()
        if st.get("at_war"):
            out.append("At war with " + ", ".join(
                m.who(x) + (f" since turn {wars[frozenset((index, x))]}" if frozenset((index, x)) in wars else "")
                for x in st["at_war"]))
        if plan := self.plans().get(p["civ"]):
            out.append(f'Plan (set turn {plan[0]}): "{plan[1]}"' if plan[0] is not None else f'Plan: "{plan[1]}"')
        notes = [(t["turn"] - 1, t["notes"][i]) for t in m.turns if i in (t.get("notes") or {})][-3:]
        out += [f'Note, turn {turn}: "{note}"' for turn, note in notes]
        sent = [x for x in self.all_messages() if x[1] == index]
        got = sum(1 for x in self.all_messages() if (x[1] != index if x[2] == "all" else index in x[2]))
        if sent or got:
            out.append(f"Messages: {len(sent)} sent, {got} received"
                       + "".join(f'; turn {t}: "{text[:120]}"' for t, _, _, text in sent[-2:]))
        if total := self.call_totals().get(i):
            out.append(f"Tool calls: {total['ok'] + total['failed']}, {total['failed']} failed")
        return "\n".join(out)

    def look_trend(self, stat, civs=None, turns=None) -> str:
        m = self.match
        if stat not in STATS:
            raise Unknown(f"no stat {stat!r}; the stats: {', '.join(STATS)}")
        players = [self.civ_of(c) for c in (civs if isinstance(civs, list) else [civs])] if civs else m.civs
        span = m.turns[-max(2, int(turns)):] if turns else m.turns
        picks = sorted({round(k * (len(span) - 1) / 11) for k in range(12)}) if len(span) > 12 else range(len(span))
        sample = [span[k] for k in picks]

        def value(e: dict, i: str):
            s, st = (e.get("scores") or {}).get(i), (e.get("stats") or {}).get(i) or {}
            if s is None:
                return None
            if stat in ("score", "cities", "pop", "techs"):
                return s[("score", "cities", "pop", None, "techs").index(stat)]
            if stat == "military":
                return m.military(e, i)
            if stat in ("land", "people"):
                share = st.get("land" if stat == "land" else "pop")
                return None if share is None else f"{share:.0%}"
            return st.get(stat)

        out = [f"{stat}, turns {span[0]['turn']} to {span[-1]['turn']}:"]
        for p in players[:8]:
            points = [(e["turn"], value(e, str(p["index"]))) for e in sample]
            known = [(t, v) for t, v in points if v is not None]
            change = (f" ({known[-1][1] - known[0][1]:+,} over these turns)"
                      if len(known) > 1 and all(isinstance(v, int) for _, v in known) else "")
            out.append(f"{m.who(p['index'])}: " + (", ".join(f"{t}: {v:,}" if isinstance(v, int) else f"{t}: {v}"
                                                             for t, v in known) or "no data") + change)
        return "\n".join(out)

    def look_events(self, kind=None, civ=None, since_turn=None, until_turn=None) -> str:
        m = self.match
        index = self.civ_of(civ)["index"] if civ else None
        since = int(since_turn) if since_turn is not None else None
        until = int(until_turn) if until_turn is not None else None
        found = [(t["turn"], e) for t in m.turns for e in t.get("events") or ()
                 if (kind is None or e["kind"] == kind) and (since is None or t["turn"] >= since)
                 and (until is None or t["turn"] <= until)
                 and (index is None or index in (e.get("owner"), e.get("from")))]
        if not found:
            kinds = sorted({e["kind"] for t in m.turns for e in t.get("events") or ()})
            return "No such events" + (f"; the kinds in this game: {', '.join(kinds)}" if kind else "")
        head = f"{len(found)} events" + (", the latest 15:" if len(found) > 15 else ":")
        return "\n".join([head, *(f"- turn {t}: {e['text']}" for t, e in found[-15:])])

    def look_battles(self, civ=None, other=None, since_turn=None) -> str:
        m = self.match
        index = self.civ_of(civ)["index"] if civ else None
        versus = self.civ_of(other)["index"] if other else None
        since = int(since_turn) if since_turn is not None else None
        fights = [(t["turn"] - 1, b) for t in m.turns for b in t.get("battles") or ()   # fought during the turn
                  if (since is None or t["turn"] - 1 >= since) and (index is None or index in (b[6], b[13]))
                  and (versus is None or versus in (b[6], b[13]))]
        if not fights:
            return "No battles" + (f" for {m.who(index)}" if index is not None else "") + (
                f" with {m.who(versus)}" if versus is not None else "") + (
                f" since turn {since}" if since is not None else "") + " in the data"
        tally: dict[int, Counter] = {}
        winners: dict[int, Counter] = {}
        for _, b in fights:
            a, d = b[6], b[13]
            won = b[2] == "a"
            tally.setdefault(a, Counter())["attacks won" if won else "attacks lost"] += 1
            tally.setdefault(d, Counter())["defences lost" if won else "defences held"] += 1
            if b[4] == 2:
                tally[a]["cities taken"] += 1
            elif b[4] == 3:
                tally[a]["cities razed"] += 1
            if b[2] in "ad":
                side = 7 if won else 14
                winners.setdefault(a if won else d, Counter())[m.unit_type(b[side])] += 1
        out = [f"{len(fights)} battles, turns {fights[0][0]} to {fights[-1][0]}:"]
        for who in ([index] if index is not None else sorted(tally, key=lambda x: -sum(tally[x].values()))[:6]):
            c = tally.get(who, Counter())
            best = ", ".join(f"{t} {n}" for t, n in (winners.get(who) or Counter()).most_common(3))
            out.append(f"{m.who(who)}: " + ", ".join(f"{n} {k}" for k, n in c.items())
                       + (f"; wins by {best}" if best else ""))
        out.append("Latest:")
        for turn, b in fights[-6:]:
            result = {"a": "the attacker won", "d": "the defender held", "r": "the attacker retreated"}[b[2]]
            city = {2: ", city taken", 3: ", city razed"}.get(b[4], "")
            verb = "bombarded" if b[1] else "attacked"
            out.append(f"- turn {turn}: {m.who(b[6])}'s {m.unit_type(b[7])} {verb} {m.who(b[13])}'s "
                       f"{m.unit_type(b[14])} at ({b[15]},{b[16]}): {result}{city}")
        return "\n".join(out)

    def all_messages(self) -> list[tuple[int, int, list | str, str]]:
        """(turn sent, from, to, text) of every message of the game: the entries', then the turn being played's."""
        m = self.match
        out = [(t["turn"] - 1, x["from"], x["to"], x["text"]) for t in m.turns for x in t.get("messages") or ()]
        index = {p["civ"]: p["index"] for p in m.players}
        for x in m.live.get("messages") or ():
            to = x["to"] if x["to"] == "all" else [index.get(c) for c in x["to"]]
            out.append((m.turn, index.get(x["from"]), to, x["text"]))
        return out

    def look_diplomacy(self, civ=None, other=None) -> str:
        m = self.match
        a = self.civ_of(civ)["index"] if civ else None
        b = self.civ_of(other)["index"] if other else None

        def involves(*sides) -> bool:
            return all(x is None or x in sides for x in (a, b))

        items = [(t["turn"], 0, e["text"]) for t in m.turns for e in t.get("events") or ()
                 if e["kind"] in DIPLOMACY and involves(e.get("owner"), e.get("from"))]
        for turn, sender, to, text in self.all_messages():
            sides = [sender, *(to if isinstance(to, list) else [x["index"] for x in m.civs if x["index"] != sender])]
            if involves(*sides):
                recipients = "everyone" if to == "all" else ", ".join(m.who(x) for x in to)
                items.append((turn, 1, f'{m.who(sender)} to {recipients}: "{text[:160]}"'))
        if not items:
            return "No diplomacy in the data" + (" between them" if b is not None else "")
        items.sort(key=lambda x: (x[0], x[1]))
        head = f"{len(items)} moments" + (", the latest 15:" if len(items) > 15 else ":")
        return "\n".join([head, *(f"- turn {t}: {text}" for t, _, text in items[-15:])])

    def look_turn_now(self, civ=None) -> str:
        m = self.match
        p = self.civ_of(civ) if civ else None
        seats = [s for s in m.live.get("seats") or () if p is None or s["civ"] == p["civ"]]
        out = [f"Turn {m.turn}" + (" (GAME OVER)" if m.over else "") + ":"]
        if not seats:
            out.append("No seat of that civ is playing" if p else "No seat is playing")
        for s in seats:
            calls = s.get("calls") or {}
            clock = (f"ended after {s.get('seconds', 0):.0f} s" if s.get("ended")
                     else f"still thinking, {s.get('seconds', 0):.0f} s so far")
            out.append(f"{m.who_civ(s['civ'])}: {clock}; {calls.get('ok', 0) + calls.get('failed', 0)} tool calls, "
                       f"{calls.get('failed', 0)} failed")
            actions = s.get("actions") or []
            if actions:
                shown = actions[-(12 if p else 5):]
                out.append(f"   {len(actions)} actions so far" + (f", the latest {len(shown)}" if len(shown) <
                                                                   len(actions) else "") + ": "
                           + "; ".join(a["text"] + ("" if a.get("ok", True) else " (failed)") for a in shown))
            if s.get("note"):
                out.append(f'   note: "{s["note"]}"')
            if s.get("plan"):
                out.append(f'   plan: "{s["plan"]}"')
        if p is not None and (done := (m.turns[-1].get("actions") or {}).get(str(p["index"]))):
            out.append(f"Turn {m.turns[-1]['turn'] - 1}, {len(done)} actions: "
                       + "; ".join(a["text"] + ("" if a.get("ok", True) else " (failed)") for a in done[-12:]))
        return "\n".join(out)

    def look_city(self, name) -> str:
        m = self.match
        key = str(name).strip().lower()
        seen = [(t["turn"], c) for t in m.turns for c in t.get("cities") or () if str(c[2]).lower() == key]
        if not seen:
            biggest = sorted(m.turns[-1].get("cities") or (), key=lambda c: -c[4])[:8]
            raise Unknown(f"no city {name!r}; the biggest: {', '.join(c[2] for c in biggest)}")
        cities: dict = {}   # by engine id: two civs may have cities of one name, and a lost city may be refounded
        for t, c in seen:
            cities.setdefault(c[7] if len(c) > 7 and c[7] is not None else "name", []).append((t, c))
        stories = sorted(cities.values(), key=lambda story: -story[-1][0])
        out = [self.city_story(story) for story in stories[:3]]
        if len(stories) > 3:
            out.append(f"…and {len(stories) - 3} more of that name")
        return "\n\n".join(out)

    def city_story(self, seen: list[tuple[int, list]]) -> str:
        m = self.match
        turn, c = seen[-1]
        gone = next((t["turn"] for t in m.turns if t["turn"] > turn), None)
        out = [f"{c[2]}: " + (f"gone since turn {gone}: last {m.who(c[3])}'s, size {c[4]}" if gone is not None else
                              f"{m.who(c[3])}'s, size {c[4]}" + (", the capital" if c[5] else "")
                              + (f", building {c[6]}" if c[6] else ""))]
        out.append(f"First seen turn {seen[0][0]}, {m.who(seen[0][1][3])}'s")
        owner = seen[0][1][3]
        for t, x in seen[1:]:
            if x[3] != owner:
                out.append(f"Turn {t}: now {m.who(x[3])}'s, from {m.who(owner)}")
                owner = x[3]
        picks = sorted({round(k * (len(seen) - 1) / 7) for k in range(8)})
        out.append("Size: " + ", ".join(f"turn {seen[k][0]}: {seen[k][1][4]}" for k in picks))
        at = {(x[0], x[1]) for _, x in seen}
        wonders = [f"{e.get('wonder')} (turn {t['turn']})" for t in m.turns for e in t.get("events") or ()
                   if e["kind"] == "wonder_built" and str(e.get("city", "")).lower() == c[2].lower()
                   and (e.get("x") is None or (e.get("x"), e.get("y")) in at)]
        if wonders:
            out.append(f"Great wonders: {', '.join(wonders)}")
        return "\n".join(out)

    def look_said(self, query="") -> str:
        words = str(query).lower().split()
        lines = [x for x in self.game_lines() if all(w in f"{x['name']} {x['text']}".lower() for w in words)]
        if not lines:
            return "The desk hasn't said that yet" if words else "The desk hasn't said anything yet"
        head = f"{len(lines)} lines" + (", the latest 8:" if len(lines) > 8 else ":")
        return "\n".join([head, *(f"- turn {x['turn']}, {x['name']}: {x['text']}" for x in lines[-8:])])

    # ---- the DATA the casters read ----

    def summary(self) -> str:
        m = self.match
        limit = m.meta.get("turn_limit")
        out = [f'Broadcast: "{self.title}"'] if self.title else []
        date = (m.turns[-1].get("date") if m.turns else None)
        out.append(f"Turn {m.turn}" + (f" of {limit}" if limit else "") + (f", the year {date}" if date else "")
                   + (" (GAME OVER)" if m.over else ""))
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
        if race := self.race():
            out.append(race)
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
            parts.append(f"{m.military(last, index)} military units")
            if "land" in st:
                parts.append(f"{st['land']:.0%} of the land and {st['pop']:.0%} of the people")
            if st.get("culture"):
                parts.append(f"{st['culture']:,} culture")
            if st.get("wonders"):
                parts.append(f"{st['wonders']} great wonder{'s' if st['wonders'] > 1 else ''}")
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

    def race(self) -> str:
        """Who is nearest each victory: domination (two thirds of the land and of the people) and culture."""
        m = self.match
        stats = {i: st for i, st in (m.turns[-1].get("stats") or {}).items() if "land" in st} if m.turns else {}
        if not stats:
            return ""
        near = max(stats, key=lambda i: min(stats[i]["land"], stats[i]["pop"]))
        out = (f"The race: domination needs {DOMINATION:.0%} of the land and of the people; nearest {m.who(near)} with "
               f"{stats[near]['land']:.0%} and {stats[near]['pop']:.0%}")
        top = max(stats, key=lambda i: stats[i].get("culture") or 0)
        city = max(stats, key=lambda i: stats[i].get("city_culture") or 0)
        if (stats[top].get("culture") or 0) >= CULTURE_GOAL / 10 or \
                (stats[city].get("city_culture") or 0) >= CITY_CULTURE_GOAL / 10:
            out += (f"; a cultural victory needs {CULTURE_GOAL:,} culture and twice the next civ's, or "
                    f"{CITY_CULTURE_GOAL:,} in one city: top {m.who(top)} with {stats[top].get('culture') or 0:,}")
            if stats[city].get("city_culture"):
                out += f", best city {m.who(city)}'s with {stats[city]['city_culture']:,}"
        if (limit := m.meta.get("turn_limit")) and m.turn:
            out += f"; {limit - m.turn} turns left"
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


def assistant(message: dict, calls: list) -> dict:
    """A reply's turn as it goes back to the model with the answers to its calls: with its thinking, which a model that
    thinks (Anthropic's, through LiteLLM) wants back before its tool calls."""
    out = {"role": "assistant", "content": message.get("content"), "tool_calls": calls}
    out |= {k: message[k] for k in ("thinking_blocks", "reasoning_content") if message.get(k)}
    return out


def arguments(call: dict) -> dict:
    """A tool call's arguments as an object: {} when they aren't one."""
    args = (call.get("function") or {}).get("arguments")
    try:
        args = json.loads(args) if isinstance(args, str) else args
    except ValueError:
        return {}
    return args if isinstance(args, dict) else {}


def casters_of(config: dict) -> dict[str, tuple[str, str, str]]:
    """The two casters as a task's broadcast sets them up (`play_by_play` and `analyst`, each {name, voice, style}),
    with the default for whatever it leaves out."""
    return {speaker: tuple((config.get(seat) or {}).get(key, default)
                           for key, default in zip(("name", "voice", "style"), CASTERS[speaker], strict=True))
            for speaker, seat in SEATS.items()}


def models_of(config: dict) -> dict[str, str]:
    """Each caster's model as a task's broadcast sets it: its own `model`, else the casters' `model`, else MODEL."""
    return {speaker: (config.get(seat) or {}).get("model") or config.get("model") or MODEL
            for speaker, seat in SEATS.items()}


def renamed(text: str, casters: dict[str, tuple[str, str, str]]) -> str:
    """`text`, written for Max and Ada, with the casters' own names."""
    names = {CASTERS[s][0]: casters[s][0] for s in CASTERS}
    return re.sub(r"\b(Max|Ada)\b", lambda m: names[m[1]], text)


def parse_lines(content: str, match: Match, casters: dict[str, tuple[str, str, str]] = CASTERS) -> list[dict]:
    """The lines in an LLM reply (its first JSON object, maybe in a code fence or followed by more; or, from a model
    that forgot the JSON, its `Name: text` lines), cleaned up for speech."""
    names = tuple(name for name, _, _ in casters.values())
    start = content.find("{")
    if start >= 0:
        reply, _ = json.JSONDecoder().raw_decode(content, start)
        items = (reply.get("lines") if isinstance(reply, dict) else None) or ()
    else:
        said = "|".join(re.escape(name) for name in names)
        items = [{"speaker": m[1], "text": m[2]} for m in re.finditer(rf"(?m)^\W*({said})\W*:\s*(.+)$", content)]
        if not items:
            raise ValueError(f"no JSON in the reply: {content[:120]!r}")
    speakers = {name.lower(): speaker for speaker, (name, _, _) in casters.items()} | {s: s for s in casters}
    lines = []
    for item in items:
        speaker = speakers.get(str(item.get("speaker")).lower()) if isinstance(item, dict) else None
        if speaker and (text := spoken(item.get("text"), names)):
            own, other = (casters[s][0] for s in (speaker, PBP if speaker == COLOR else COLOR))
            # "A fine move, Ada" from Ada was meant for Max; "I'm Ada" is not addressed to anyone.
            text = re.sub(rf"(?:(?<=, )|^){re.escape(own)}(?=\s*(?:[,.!?]|$))", other, text)
            lines.append({"speaker": speaker, "text": text, "focus": match.civ_named(item.get("focus"))})
    if not lines:
        raise ValueError(f"no lines in the reply: {content[:120]!r}")
    return lines


def spoken(text, names: tuple[str, ...] = ("Max", "Ada")) -> str:
    """A line as it is said: no stage directions or speaker prefix, "bleep" for a blocked or masked word (a voice
    could read "s***" as the word), at most MAX_WORDS words (whole sentences)."""
    text = MASKED.sub("bleep", str(text or ""))   # before its stars read as a stage direction
    text = BLOCKED.sub("bleep", re.sub(r"\*[^*]*\*|\[[^\]]*\]", "", text))
    prefix = "|".join(re.escape(name) for name in names)
    text = " ".join(re.sub(rf"^(?:\s*(?:{prefix})\s*:)+\s*", "", text).split())   # "Max: Ada: …" too
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
    """What went wrong, with an HTTP error's reply, which says why (read once, then kept on the error)."""
    if isinstance(e, urllib.error.HTTPError):
        if not hasattr(e, "why"):
            try:
                body = e.read(300).decode(errors="replace")
            except OSError:
                body = ""
            e.why = f"HTTP {e.code} {' '.join(body.split())}"
        return e.why
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
    p.add_argument("--config", type=json.loads, default={},
                   help='the casters as a task\'s broadcast sets them up, JSON: {"model": the model that writes the '
                        'lines, "tts_model": the one that speaks them, "play_by_play" and "analyst": {"name", "voice", '
                        '"style", "model": that caster\'s own}}; what it leaves out keeps its default')
    p.add_argument("--title", help="the broadcast's title, for the intro")
    args = p.parse_args()
    base_url, api_key = os.environ.get("CAST_BASE_URL", "").strip(), os.environ.get("CAST_API_KEY", "").strip()
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in api_key):
        print("caster: CAST_API_KEY has a control character in it", file=sys.stderr)
        return 2
    if not base_url or not api_key:
        print("caster: set CAST_BASE_URL (the endpoint, e.g. https://your-litellm-proxy) and CAST_API_KEY",
              file=sys.stderr)
        return 2
    config = args.config
    caster = Caster(args.data, base_url, api_key, model=config.get("model", MODEL), models=models_of(config),
                    tts_model=config.get("tts_model", TTS_MODEL), casters=casters_of(config), title=args.title)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(caster))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    caster.log(f"Casting {caster.data_url} on http://127.0.0.1:{args.port}/cast.json: "
               f"{' and '.join(name for name, _, _ in caster.casters.values())}, written by "
               f"{' and '.join(dict.fromkeys(caster.models.values()))}, "
               f"voiced by {caster.tts_model}")
    caster.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
