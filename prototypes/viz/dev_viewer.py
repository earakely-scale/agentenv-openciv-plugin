"""Develop the viewer (src/agentenv_openciv3/viewer) without an env.

    python prototypes/viz/dev_viewer.py embed <record dir> <out.html> [--actions <action log>...]
    python prototypes/viz/dev_viewer.py serve <record dir> [--every 2] [--port 8765]

`embed` writes the recording's page. `serve` replays a recorded game as if it were live: GET /live and
/live/data.json, releasing one more turn every `--every` seconds, with seats that end the turn one by one.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from agentenv_openciv3 import matchdata  # noqa: E402

VIEWER = Path(__file__).resolve().parents[2] / "src/agentenv_openciv3/viewer"


def page(data: dict | None, videos: dict | None = None) -> str:
    html = (VIEWER / "index.html").read_text()
    blob = lambda v: json.dumps(v, separators=(",", ":")).replace("</", "<\\/")  # noqa: E731
    return (html.replace("/*__CSS__*/", (VIEWER / "app.css").read_text())
            .replace("/*__JS__*/", (VIEWER / "app.js").read_text())
            .replace("/*__DATA__*/null", blob(data)).replace("/*__VIDEOS__*/null", blob(videos)))


def load_actions(paths: list[Path], players: list[dict]) -> tuple[dict, dict]:
    """Action logs keyed by seat civ (or one log per seat named <civ>.jsonl) -> keyed by player index."""
    by_civ = {p["civ"]: p["index"] for p in players}
    by_label = {p["label"]: p["index"] for p in players if p.get("label")}
    actions: dict = {}
    calls: dict = {}
    for path in paths:
        a, c = matchdata.actions_from_log(path)
        for src, dst in ((a, actions), (c, calls)):
            for turn, per in src.items():
                for seat, v in per.items():
                    i = by_civ.get(seat, by_label.get(seat, by_civ.get(path.stem, by_label.get(path.stem))))
                    if i is not None:
                        dst.setdefault(turn, {})[i] = v
    return actions, calls


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=("embed", "serve"))
    ap.add_argument("record_dir", type=Path)
    ap.add_argument("out", nargs="?", type=Path)
    ap.add_argument("--actions", type=Path, nargs="*", default=[])
    ap.add_argument("--labels", default="")
    ap.add_argument("--videos", default="", help="json: {civ: {file, fps, turns}}")
    ap.add_argument("--every", type=float, default=2.0)
    ap.add_argument("--start", type=int, default=20)
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    labels = dict(kv.split("=", 1) for kv in args.labels.split(",") if kv)
    snaps = matchdata.load_snapshots(args.record_dir)
    m = matchdata.MatchData.from_snapshots(snaps, game="dev", labels=labels)
    actions, calls = load_actions(args.actions, m.players)
    if args.mode == "embed":
        videos = json.loads(args.videos) if args.videos else None
        args.out.write_text(page(m.document(actions=actions, calls=calls), videos))
        print(args.out, f"{args.out.stat().st_size:,} bytes")
        return

    began = time.time()
    seats = [p for p in m.players if p["seat"] is not None]
    rng = random.Random(1)
    finish = {p["civ"]: rng.uniform(0.2, 1.0) for p in seats}

    def shown() -> int:
        return min(len(snaps) - 1, args.start + int((time.time() - began) / args.every))

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in ("/live", "/live/"):
                body, kind = page(None).encode(), "text/html"
            elif url.path == "/live/data.json":
                n = shown()
                since = int(parse_qs(url.query).get("since", ["-1"])[0])
                part = matchdata.MatchData.from_snapshots(snaps[: n + 1], game="dev", labels=labels)
                doc = part.document(since, actions=actions, calls=calls)
                frac = ((time.time() - began) % args.every) / args.every
                cur = snaps[n]["turn"]
                doc["live"] = {"turn": cur, "game_over": n == len(snaps) - 1, "victory": None, "client": False,
                               "recording": True, "seats": [
                                   {"civ": p["civ"], "label": p["label"], "ended": frac > finish[p["civ"]],
                                    "seconds": round(frac * args.every * 20, 1),
                                    "calls": (calls.get(cur) or {}).get(
                                        p["index"], {"ok": int(frac * 30), "failed": 0}),
                                    "actions": (actions.get(cur) or {}).get(p["index"], [])} for p in seats]}
                body, kind = json.dumps(doc).encode(), "application/json"
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    print(f"http://127.0.0.1:{args.port}/live")
    ThreadingHTTPServer(("127.0.0.1", args.port), H).serve_forever()


if __name__ == "__main__":
    main()
