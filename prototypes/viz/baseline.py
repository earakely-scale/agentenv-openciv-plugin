"""Render the current recording (src/agentenv_openciv3/recording.py) of the same game, as the 'before' reference."""
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

# The renderer as it was before the viewer work (commit e277b61), loaded from git.
root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / "src"))
old = subprocess.run(["git", "-C", str(root), "show", "e277b61:src/agentenv_openciv3/recording.py"],
                     capture_output=True, text=True, check=True).stdout
path = Path(tempfile.mkdtemp()) / "recording_before.py"
path.write_text(old.replace("from .actionlog import", "from agentenv_openciv3.actionlog import"))
spec = importlib.util.spec_from_file_location("recording_before", path)
recording = importlib.util.module_from_spec(spec)
sys.modules["recording_before"] = recording
spec.loader.exec_module(recording)

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
