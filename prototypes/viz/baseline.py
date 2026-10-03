"""Render the current recording (src/agentenv_openciv3/recording.py) of the same game, as the 'before' reference."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from agentenv_openciv3 import recording  # noqa: E402

record, out = Path(sys.argv[1]), Path(sys.argv[2])
labels = dict(kv.split("=", 1) for kv in sys.argv[3].split(","))
snaps = recording.load_snapshots(record)
for s in snaps:
    for p in s["players"]:
        if p["civ"] in labels:
            p["is_human"], p["label"] = True, labels[p["civ"]]
r = recording.Renderer(snaps)
out.mkdir(parents=True, exist_ok=True)
for t in (60, 200):
    r.frame(snaps[t]).save(out / f"before-T{t:03d}.png")
print("ok", r.layout)
