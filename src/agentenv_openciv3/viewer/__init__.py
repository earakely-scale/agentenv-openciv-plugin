"""The match viewer (docs/viewer.md): one page, live (`GET /live`) or with a whole game embedded (the `html` format)."""

from __future__ import annotations

import json
import re
from importlib import resources

PLACEHOLDER = re.compile(r"/\*__(CSS|JS)__\*/|/\*__(DATA|VIDEOS)__\*/null")


def _asset(name: str) -> str:
    return resources.files(__package__).joinpath(name).read_text(encoding="utf-8")


def _json(value) -> str:
    """JSON safe inside a <script>: no `</` can close it."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def page(data: dict | None = None, videos: dict[str, dict] | None = None) -> str:
    """The viewer as one HTML file. `data` is a `MatchData.document()`; None makes the live page, which fetches
    `live/data.json` itself. `videos` names the client's video of each seat (civ -> {"file", "fps", "turns"})."""
    fills = {"CSS": _asset("app.css"), "JS": _asset("app.js").replace("</script", "<\\/script"),
             "DATA": _json(data), "VIDEOS": _json(videos)}
    return PLACEHOLDER.sub(lambda m: fills[m[1] or m[2]], _asset("index.html"))
