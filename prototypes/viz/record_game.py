"""Record a game to try the viz prototypes on, with no LLM: OpenCiv3's own AI plays every civ.

    scripts/build-bridge.sh
    python prototypes/viz/record_game.py <record dir> [--turns 200] [--seed 1]

Nine civs on a Standard map at Regent with roaming barbarians, like the `frontier` task; the bridge writes
turn-NNNN.json.gz into <record dir> (docs/recording.md). Takes about half a minute for 200 turns.
"""

import argparse
import json
import subprocess
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("record_dir", type=Path)
ap.add_argument("--turns", type=int, default=200)
ap.add_argument("--seed", type=int, default=1)
ap.add_argument("--bridge", default=str(Path(__file__).resolve().parents[2] / "build/bridge/CivBridge"))
args = ap.parse_args()

proc = subprocess.Popen([args.bridge, "--record", str(args.record_dir)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, text=True)
ids = iter(range(1, 1 << 30))


def call(cmd: str, **kw) -> dict:
    proc.stdin.write(json.dumps({"id": next(ids), "cmd": cmd, "args": kw}) + "\n")
    proc.stdin.flush()
    r = json.loads(proc.stdout.readline())
    if not r["ok"]:
        raise SystemExit(f"{cmd}: {r['error']}")
    return r["result"]


proc.stdout.readline()    # ready
game = call("new_game", seed=args.seed, civ="Rome", opponents=8, size="Standard", difficulty="Regent",
            barbarians="Roaming", landform="Continents", turn_limit=args.turns)
print("civs:", ", ".join([game["civ"], *game["opponents"]]))
while True:
    r = call("autoplay", turns=10, policy="engine_ai")
    print(f"T{r['turn']}", flush=True)
    if r["game_over"] or r["turn"] >= args.turns:
        break
proc.stdin.close()
proc.wait(timeout=30)
