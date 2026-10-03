"""A match's broadcast settings: the openciv3_match step's `broadcast` option and the new-game argument of that name.
The task decides whether two AI casters talk over the stream, and which models and voices they use; `agent-env
openciv3 stream` reads the settings from the running game (docs/tools.md, Broadcast)."""

from __future__ import annotations

import re

TITLE_LIMIT = 80
CASTER_SEATS = ("play_by_play", "analyst")
CASTER_KEYS = ("name", "voice", "style")
NAME = re.compile(r"[A-Za-z][A-Za-z .'-]{0,19}")


def settings(value: object) -> dict | None:
    """The settings as the env keeps them, `{"title": str | None, "casters": dict | None}`: casters None are off, a dict
    (empty for the casters' defaults) is on. None stays None; anything malformed raises ValueError."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"broadcast must be an object with title and casters, got {value!r}")
    if unknown := sorted(set(value) - {"title", "casters"}):
        raise ValueError(f"broadcast has unknown keys {unknown}; valid: title, casters")
    title = value.get("title")
    if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > TITLE_LIMIT):
        raise ValueError(f"broadcast title must be text of 1-{TITLE_LIMIT} characters, got {title!r}")
    return {"title": title.strip() if title else None, "casters": _casters(value.get("casters", False))}


def _casters(value: object) -> dict | None:
    if value is True:
        return {}
    if value is False or value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"broadcast casters must be true, false or an object, got {value!r}")
    if unknown := sorted(set(value) - {"model", "tts_model", *CASTER_SEATS}):
        raise ValueError(f"broadcast casters have unknown keys {unknown}; valid: model, tts_model, "
                         f"{', '.join(CASTER_SEATS)}")
    out: dict = {}
    for key in ("model", "tts_model"):
        if key in value:
            if not isinstance(value[key], str) or not value[key].strip():
                raise ValueError(f"broadcast casters {key} must be a model name, got {value[key]!r}")
            out[key] = value[key].strip()
    for seat in CASTER_SEATS:
        if seat not in value:
            continue
        caster = value[seat]
        if not isinstance(caster, dict) or set(caster) - set(CASTER_KEYS) or not all(
                isinstance(v, str) and v.strip() for v in caster.values()):
            raise ValueError(f"broadcast casters {seat} must be an object of {', '.join(CASTER_KEYS)} (text), "
                             f"got {caster!r}")
        if "name" in caster and not NAME.fullmatch(caster["name"].strip()):
            raise ValueError(f"broadcast casters {seat} name must be 1-20 letters, spaces or .'- starting with a "
                             f"letter, got {caster['name']!r}")
        out[seat] = {k: v.strip() for k, v in caster.items()}
    names = [out[s]["name"].lower() for s in CASTER_SEATS if "name" in out.get(s, {})]
    if len(names) != len(set(names)):
        raise ValueError("broadcast casters need two different names")
    return out
