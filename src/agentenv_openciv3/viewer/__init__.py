"""The match viewer (docs/viewer.md): one page, live (`GET /live`) or with a whole game embedded (the `html` format);
and the play UI (docs/play.md, `GET /play`). Both draw the map in the client's art when the env has it (map.js,
art.js; the viewer through viewart.js)."""

from __future__ import annotations

import json
import re
from importlib import resources

PLACEHOLDER = re.compile(r"/\*__(CSS|JS)__\*/|/\*__(DATA|VIDEOS)__\*/null")


def _asset(name: str) -> str:
    return resources.files(__package__).joinpath(name).read_text(encoding="utf-8")


def _json(value) -> str:
    """JSON safe inside a <script>: with no `<` at all, no text an agent wrote (`</script>`, `<!--<script>`) can end
    the script early or keep it from ending."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def art_kit() -> str:
    """The play page's map drawing in the client's art (map.js, art.js) and the viewer's use of it (viewart.js), in a
    scope of their own (their helpers share names with app.js's): `ArtKit.Art`, `ArtKit.ViewArt`."""
    body = "\n".join(_asset(f) for f in ("map.js", "art.js", "viewart.js"))
    return f"const ArtKit = (() => {{\n{body}\nreturn {{Art, ViewArt}};\n}})();\n"


def page(data: dict | None = None, videos: dict[str, dict] | None = None) -> str:
    """The viewer as one HTML file. `data` is a `MatchData.document()`; None makes the live page, which fetches
    `live/data.json` itself. `videos` names the client's video of each seat (civ -> {"file", "fps", "turns"})."""
    js = (art_kit() + _asset("app.js")).replace("</script", "<\\/script")
    fills = {"CSS": _asset("app.css"), "JS": js, "DATA": _json(data), "VIDEOS": _json(videos)}
    return PLACEHOLDER.sub(lambda m: fills[m[1] or m[2]], _asset("index.html"))


def play_page() -> str:
    """The play UI as one HTML file; it reads its seat's token from the URL's `#token=`."""
    js = "\n".join(_asset(f) for f in ("map.js", "art.js", "screens.js", "play.js")).replace("</script", "<\\/script")
    fills = {"CSS": _asset("play.css"), "JS": js}
    return re.sub(r"/\*__(CSS|JS)__\*/", lambda m: fills[m[1]], _asset("play.html"))
