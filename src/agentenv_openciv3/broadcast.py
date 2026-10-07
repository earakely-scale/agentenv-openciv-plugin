"""A match's broadcast settings: the openciv3_match step's `broadcast` option and the new-game argument of that name.
The task decides whether two AI casters talk over the stream, and which models and voices they use, and which banners
the stream's sponsor slot shows; `agent-env openciv3 stream` reads the settings from the running game (docs/tools.md,
Broadcast)."""

from __future__ import annotations

import base64
import binascii
import re
import urllib.request
from pathlib import Path

TITLE_LIMIT = 80
DISPLAY_LIMIT = 30      # a seat's name on screen: "Opus 5.5", "GPT-6 Sol"
CASTER_SEATS = ("play_by_play", "analyst")
CASTER_KEYS = ("name", "voice", "style", "model")   # model: that caster's own, else the casters' model
NAME = re.compile(r"[A-Za-z][A-Za-z .'-]{0,19}")
BANNER_LIMIT = 8
BANNER_TEXT = 100
THEMES = ("dark", "light")
LOGO = re.compile(r"\{([a-z][a-z0-9_-]{0,19})\}")
LOGO_NAME = re.compile(r"[a-z][a-z0-9_-]{0,19}")
LOGO_BYTES = 512 * 1024
LOGO_SECONDS = 20
DATA_URI = re.compile(r"data:(image/(?:png|jpeg|webp|gif|svg\+xml));base64,([A-Za-z0-9+/]+={0,2})")


def settings(value: object, *, inlined: bool = False) -> dict | None:
    """The settings as the env keeps them, `{"title": str | None, "casters": dict | None}`, and `names` and `banners`
    when given: casters None are off, a dict (empty for the casters' defaults) is on; names maps a seat's label to the
    name the broadcast shows ("opus": "Opus 5.5"); banners are the sponsor slot's, each `{"text", "logos", "theme"}`.
    `inlined`: the env's check, where every logo must already be a data: URI (inline() makes it one). None stays None;
    anything malformed raises ValueError."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"broadcast must be an object with title and casters, got {value!r}")
    if unknown := sorted(set(value) - {"title", "casters", "names", "banners"}):
        raise ValueError(f"broadcast has unknown keys {unknown}; valid: title, casters, names, banners")
    title = value.get("title")
    if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > TITLE_LIMIT):
        raise ValueError(f"broadcast title must be text of 1-{TITLE_LIMIT} characters, got {title!r}")
    names = _names(value.get("names") or {})
    banners = _banners(value.get("banners"), inlined)
    return {"title": title.strip() if title else None, "casters": _casters(value.get("casters", False)),
            **({"names": names} if names else {}), **({"banners": banners} if banners else {})}


def inline(value: dict | None) -> dict | None:
    """`value` with each banner logo a data: URI: a URL fetched and a file read, each checked to be a PNG, JPEG, WebP,
    GIF or SVG image of at most LOGO_BYTES. The game's settings then hold all the stream shows, and a broadcast never
    waits on a third-party host. Raises ValueError for a logo that is missing, too big or not an image."""
    if not value or not value.get("banners"):
        return value
    return {**value, "banners": [{**b, "logos": {n: _data_uri(n, s) for n, s in (b.get("logos") or {}).items()}}
                                 for b in value["banners"]]}


def public(value: dict | None) -> dict | None:
    """The settings as the live view sends them: each banner logo as the env's link to it (live/logo/<banner>/<name>),
    not its bytes, which every poll would carry."""
    if not value or not value.get("banners"):
        return value
    return {**value, "banners": [{**b, "logos": {n: f"live/logo/{i}/{n}" for n in b["logos"]}}
                                 for i, b in enumerate(value["banners"])]}


def logo(value: dict | None, banner: int, name: str) -> tuple[bytes, str] | None:
    """A banner logo's image and its media type, from the env's settings; None when there is no such logo."""
    banners = (value or {}).get("banners") or []
    if not 0 <= banner < len(banners) or name not in banners[banner]["logos"]:
        return None
    m = DATA_URI.fullmatch(banners[banner]["logos"][name])
    return base64.b64decode(m[2]), m[1]


def _banners(value: object, inlined: bool) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list) or not 1 <= len(value) <= BANNER_LIMIT:
        raise ValueError(f"broadcast banners must be a list of 1-{BANNER_LIMIT} banners, got {value!r}")
    return [_banner(b, inlined) for b in value]


def _banner(value: object, inlined: bool) -> dict:
    if not isinstance(value, dict) or set(value) - {"text", "logos", "theme"}:
        raise ValueError(f"a broadcast banner is an object of text, logos and theme, got {value!r}")
    text, logos, theme = value.get("text"), value.get("logos") or {}, value.get("theme", "dark")
    if not isinstance(text, str) or not text.strip() or len(text) > BANNER_TEXT:
        raise ValueError(f"a broadcast banner's text must be 1-{BANNER_TEXT} characters, got {text!r}")
    if not isinstance(logos, dict) or not all(isinstance(k, str) and LOGO_NAME.fullmatch(k) and isinstance(v, str)
                                              for k, v in logos.items()):
        raise ValueError(f"a broadcast banner's logos map names (a-z, 0-9, _ and -) to image sources, got {logos!r}")
    placed = set(LOGO.findall(text))
    if missing := sorted(placed - set(logos)):
        raise ValueError(f"banner {text!r} places logos it has no source for: {missing}")
    if unused := sorted(set(logos) - placed):
        raise ValueError(f"banner {text!r} has logos it never places ({{name}} in its text): {unused}")
    if theme not in THEMES:
        raise ValueError(f"a broadcast banner's theme is one of {', '.join(THEMES)}, got {theme!r}")
    for name, source in logos.items():
        _check_source(name, source, inlined)
    return {"text": text.strip(), "logos": dict(logos), "theme": theme}


def _check_source(name: str, source: str, inlined: bool) -> None:
    if source.startswith("data:"):
        _decoded(name, source)
    elif inlined:
        raise ValueError(f"banner logo {name!r} reaches the env as a data: URI (the openciv3_match step inlines URLs "
                         f"and files), got {source[:80]!r}")
    elif not (source.startswith("https://") and len(source) > 8 or source.startswith(("/", "~/"))):
        raise ValueError(f"banner logo {name!r} is an https:// URL, a data:image/...;base64, URI or an image file's "
                         f"path (absolute or ~/...), got {source[:80]!r}")


def _decoded(name: str, uri: str) -> bytes:
    m = DATA_URI.fullmatch(uri)
    try:
        body = base64.b64decode(m[2], validate=True) if m else None
    except binascii.Error:
        body = None
    if body is None:
        raise ValueError(f"banner logo {name!r} is not a base64 data: URI of a PNG, JPEG, WebP, GIF or SVG image")
    if len(body) > LOGO_BYTES:
        raise ValueError(f"banner logo {name!r} is {len(body):,} bytes; the limit is {LOGO_BYTES:,}")
    return body


def _data_uri(name: str, source: str) -> str:
    if source.startswith("data:"):
        _decoded(name, source)
        return source
    try:
        if source.startswith("https://"):
            request = urllib.request.Request(source, headers={"User-Agent": "agentenv-openciv3"})
            with urllib.request.urlopen(request, timeout=LOGO_SECONDS) as r:
                body = r.read(LOGO_BYTES + 1)
        else:
            with Path(source).expanduser().open("rb") as f:
                body = f.read(LOGO_BYTES + 1)
    except OSError as e:
        raise ValueError(f"banner logo {name!r} could not be read from {source}: {e}") from e
    if len(body) > LOGO_BYTES:
        raise ValueError(f"banner logo {name!r} at {source} is over {LOGO_BYTES:,} bytes")
    kind = _image_type(body)
    if kind is None:
        raise ValueError(f"banner logo {name!r} at {source} is not a PNG, JPEG, WebP, GIF or SVG image")
    return f"data:{kind};base64,{base64.b64encode(body).decode()}"


def _image_type(body: bytes) -> str | None:
    """The image's media type from its first bytes, as a browser would sniff it."""
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    return "image/svg+xml" if b"<svg" in body[:4096].lower() else None


def _names(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
            isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip() and len(v.strip()) <= DISPLAY_LIMIT
            for k, v in value.items()):
        raise ValueError(f"broadcast names must map seat labels to names of 1-{DISPLAY_LIMIT} characters, "
                         f"got {value!r}")
    return {k.strip(): v.strip() for k, v in value.items()}


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
