"""What spectators may read of what agents write (moderation.public)."""

import codecs

from agentenv_openciv3 import moderation
from agentenv_openciv3.moderation import public

# Blocked words, decoded here so the tests hold none in plain text either.
PROFANITY, SLUR = codecs.decode("shpx", "rot13"), codecs.decode("phag", "rot13")


def test_hidden_characters_go_and_whitespace_collapses():
    assert public("a\x00b\x07c\x1b[31m", 50) == "abc[31m"
    assert public("zero\u200bwidth\ufeff \u202eflipped\u2066 \U000e0041tag", 50) == "zerowidth flipped tag"
    assert public("lone \ud800surrogate\udfff", 50) == "lone surrogate"
    assert public("  two\n\n\tlines\u00a0and  spaces ", 50) == "two lines and spaces"
    assert public(" \u200b\n\u202e ", 50) == ""
    assert public("emoji 🏛️ and accents: Ελλάδα, Égypte", 50) == "emoji 🏛️ and accents: Ελλάδα, Égypte"


def test_a_pile_of_combining_marks_is_cut_to_two():
    assert public("z" + "\u0336" * 16 + "algo", 50) == "z\u0336\u0336algo"
    assert public("o" + "\u200b\u0336" * 5, 50) == "o\u0336\u0336"
    assert public("Vie\u0323\u0302t Nam, ภาษาไทย น้ำ, tiếng", 50) == "Vi\u1ec7t Nam, ภาษาไทย น้ำ, tiếng"


def test_links_become_link():
    assert public("see https://evil.example/x?y=1, now", 80) == "see [link], now"
    assert public("(HTTP://A.B/c) and www.example.com.", 80) == "([link]) and [link]."
    assert public("plain words: http and www are fine", 80) == "plain words: http and www are fine"
    assert public("glued: seehttps://evil.example/x and _www.evil.example", 80) == "glued: see[link] and _[link]"
    assert public("Awww. Fine. Awww... www.", 80) == "Awww. Fine. Awww... www."
    assert public("http://" + "." * 50_000 + " ok", 12) == "[link]....." + "…"    # in linear time


def test_blocked_words_are_masked_as_whole_words_in_any_case():
    assert public(f"What the {PROFANITY}, Rome!", 80) == f"What the {PROFANITY[0]}{'*' * (len(PROFANITY) - 1)}, Rome!"
    shouted = PROFANITY.upper()
    assert public(shouted, 80) == shouted[0] + "*" * (len(shouted) - 1)
    assert public(f"S{SLUR}horpe", 80) == f"S{SLUR}horpe"
    assert public(f"{SLUR}s", 80) == SLUR[0] + "*" * len(SLUR)
    assert all(w == w.lower() and w.isalpha() for w in moderation.BLOCKLIST)


def test_long_text_is_cut_with_an_ellipsis():
    assert public("x" * 300, 280) == "x" * 279 + "…"
    assert public("word " * 60, 140) == ("word " * 28).rstrip() + "…"
    assert public("x" * 280, 280) == "x" * 280
