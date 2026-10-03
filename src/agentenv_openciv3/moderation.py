"""What spectators may read of the text agents write (messages, end_turn notes, plans): a live broadcast shows it to
anyone, so it carries no links, no hidden characters and no slurs, and fits its place on screen."""

from __future__ import annotations

import codecs
import re
import unicodedata
from itertools import groupby, islice

# Format (zero-width, bidi), control and surrogate characters: invisible on screen, or not text at all.
HIDDEN = {"Cc", "Cf", "Cs"}
# Combining marks: a letter carries a few (accents, tone marks); a pile of them ("zalgo") smears over the overlay.
MARKS, STACKED_MARKS = {"Mn", "Me"}, 2
# A link anywhere, also glued to a word ("seehttps://…"); "www." only before a name, so "Awww..." stays.
URL = re.compile(r"(?i)(?:https?://|www\.(?=\w))\S+")
TRAILING = ".,;:!?)]'\""      # punctuation after a link, kept: "see www.example.com." ends with a full stop
# Slurs and strong profanity, whole words. ROT13, so the repository holds no plaintext slurs.
BLOCKLIST = codecs.decode(
    "shpx shpxre shpxref shpxvat shpxrq shpxva zbgureshpxre zbgureshpxref zbgureshpxvat fuvg fuvgf fuvggl "
    "fuvgurnq ohyyfuvg phag phagf gjng gjngf ovgpu ovgpurf juber juberf fyhg fyhgf jnaxre jnaxref nffubyr "
    "nffubyrf pbpxfhpxre avttre avttref avttn avttnf snttbg snttbgf snt sntf genaal genaavrf "
    "ergneq ergneqf ergneqrq xvxr xvxrf fcvp fcvpf puvax puvaxf tbbx tbbxf jrgonpx jrgonpxf ornare "
    "ornaref pbba pbbaf cnxv cnxvf enturnq enturnqf gbjryurnq gbjryurnqf fnaqavttre", "rot13").split()
BLOCKED = re.compile(r"\b(?:" + "|".join(sorted(BLOCKLIST, key=len, reverse=True)) + r")\b", re.IGNORECASE)


def visible(text: str) -> str:
    """The text with hidden characters removed, at most STACKED_MARKS combining marks in a row, and every run of
    whitespace one space."""
    kept = "".join(" " if ch.isspace() else "" if unicodedata.category(ch) in HIDDEN else ch
                   for ch in unicodedata.normalize("NFC", text))
    runs = groupby(kept, key=lambda ch: unicodedata.category(ch) in MARKS)
    return " ".join("".join("".join(islice(run, STACKED_MARKS)) if marks else "".join(run)
                            for marks, run in runs).split())


def public(text: str, limit: int) -> str:
    """`text` as spectators may read it: visible characters only, links replaced by [link], blocked words masked
    (first letter kept), at most `limit` characters (cut with "…")."""
    text = URL.sub(lambda m: "[link]" + m[0][len(m[0].rstrip(TRAILING)):], visible(text))
    text = BLOCKED.sub(lambda m: m[0][0] + "*" * (len(m[0]) - 1), text)
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"
