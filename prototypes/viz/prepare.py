"""Turn a directory of bridge snapshots (turn-NNNN.json.gz) into one compact match.json for the viz prototypes.

    python prototypes/viz/prepare.py <record dir> --out prototypes/viz/out/match.json [--labels Rome=opus,...]

The static map (terrain, rivers) is written once; per turn only what changes: tile owners as deltas, cities, unit
stacks, scores. Events are the bridge's own (the seats') plus events derived from the snapshot diffs for every civ:
cities founded, captured and lost, techs learned, civs eliminated, lead changes.
"""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

# Nine civ colours for a dark surface, validated as a categorical set (adjacent pairs; CVD dE >= 8.4).
PALETTE = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767", "#2b9db5"]
BARBARIAN = "#6b6f78"
TERRAIN = ["ocean", "sea", "coast", "grassland", "plains", "desert", "tundra", "floodplain", "hills", "mountains",
           "forest", "jungle", "marsh", "volcano"]
MILITARY_FREE = {"Worker", "Settler", "Explorer", "Galley", "Caravel", "Curragh"}


def load(directory: Path) -> list[dict]:
    snaps = {}
    for path in sorted(directory.glob("turn-*.json.gz")):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                s = json.load(f)
        except (OSError, EOFError, ValueError):
            continue
        snaps[s["turn"]] = s
    return [snaps[t] for t in sorted(snaps)]


def seam(snap: dict) -> int:
    """The x column with the least land near it: the map is cut there so no continent is split across the edges."""
    w = snap["map"]["width"]
    land = [0] * w
    for x, _y, base, *_ in snap["tiles"]:
        if base not in ("ocean", "sea", "coast"):
            land[x] += 1
    window = [sum(land[(x + d) % w] for d in range(-3, 4)) for x in range(w)]
    return min(range(w), key=window.__getitem__)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("record_dir", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--labels", default="", help="Civ=label,... ; every labelled civ is drawn as an agent seat")
    args = ap.parse_args()
    labels = dict(kv.split("=", 1) for kv in args.labels.split(",") if kv)
    snaps = load(args.record_dir)
    first, last = snaps[0], snaps[-1]

    players = []
    slot = 0
    for p in last["players"]:
        barb = "barbarian" in p["civ"].lower()
        label = labels.get(p["civ"]) or p.get("label")
        players.append({"index": p["index"], "civ": p["civ"], "label": label, "barbarian": barb,
                        "seat": bool(p.get("is_human") or p["civ"] in labels),
                        "color": BARBARIAN if barb else PALETTE[slot % len(PALETTE)],
                        "engine_color": "#" + "".join(f"{v:02x}" for v in p.get("color") or (128, 128, 128))})
        slot += not barb
    name = {p["index"]: p["label"] or p["civ"] for p in players}

    tiles = first["tiles"]
    index = {(r[0], r[1]): i for i, r in enumerate(tiles)}
    static = {"tiles": [[r[0], r[1], TERRAIN.index(r[2]) if r[2] in TERRAIN else 0,
                         TERRAIN.index(r[3]) if r[3] in TERRAIN else -1, r[5]] for r in tiles]}

    turns, owners = [], [-1] * len(tiles)
    prev_cities: dict[str, dict] = {}
    prev_scores: dict[int, dict] = {}
    leader = None
    for s in snaps:
        t = s["turn"]
        delta = []
        for r in s["tiles"]:
            i = index[(r[0], r[1])]
            if owners[i] != r[4]:
                owners[i] = r[4]
                delta.append([i, r[4]])
        cities = {c["name"]: c for c in s["cities"]}
        events = []
        for c in s["cities"].__iter__():
            was = prev_cities.get(c["name"])
            if was is None and t > 0:
                events.append({"kind": "city_founded", "owner": c["owner"], "x": c["x"], "y": c["y"],
                               "text": f"{name[c['owner']]} founded {c['name']}"})
            elif was is not None and was["owner"] != c["owner"]:
                events.append({"kind": "city_captured", "owner": c["owner"], "from": was["owner"], "x": c["x"],
                               "y": c["y"], "text": f"{name[c['owner']]} took {c['name']} from {name[was['owner']]}"})
        for n, c in prev_cities.items():
            if n not in cities:
                events.append({"kind": "city_destroyed", "owner": c["owner"], "x": c["x"], "y": c["y"],
                               "text": f"{name[c['owner']]} lost {n}; it was razed"})
        for p in s["players"]:
            before = prev_scores.get(p["index"])
            if before and p["score"]["techs"] > before["techs"] and "barbarian" not in p["civ"].lower():
                events.append({"kind": "tech_learned", "owner": p["index"],
                               "text": f"{name[p['index']]} learned a tech ({p['score']['techs']} known)"})
            if p.get("defeated") and before is not None and not before.get("defeated"):
                events.append({"kind": "civ_destroyed", "owner": p["index"],
                               "text": f"{name[p['index']]} was eliminated"})
        live = [p for p in s["players"] if "barbarian" not in p["civ"].lower()]
        top = max(live, key=lambda p: (p["score"]["total"], -p["index"]))
        if t > 5 and top["index"] != leader and leader is not None:
            events.append({"kind": "lead_change", "owner": top["index"], "from": leader,
                           "text": f"{name[top['index']]} takes the lead from {name[leader]}"})
        leader = top["index"] if t > 5 or leader is None else leader
        for e in s.get("events", []):
            if e.get("kind") in ("city_grew", "job_done", "built", "riot_risk", "threat"):
                continue    # per-seat chatter; the derived events above cover every civ
            owner = next((p["index"] for p in s["players"] if p["civ"] == e.get("civ")), 1)
            events.append({"kind": e.get("kind"), "owner": owner, "text": f"{name[owner]}: {e.get('text', '')}",
                           **({"x": e["x"], "y": e["y"]} if "x" in e else {})})
        stacks: dict[tuple, list] = {}
        for u in s["units"]:
            k = (u["x"], u["y"], u["owner"])
            st = stacks.setdefault(k, [0, 0])
            st[0 if u["type"] in MILITARY_FREE else 1] += 1
        turns.append({
            "turn": t,
            "owners": delta,
            "cities": [[c["x"], c["y"], c["name"], c["owner"], c["size"], int(bool(c.get("capital")))]
                       for c in s["cities"]],
            "units": [[x, y, o, civil, mil] for (x, y, o), (civil, mil) in stacks.items()],
            "scores": {p["index"]: [p["score"][k] for k in ("total", "cities", "pop", "tiles", "techs")]
                       + [int(bool(p.get("defeated")))] for p in s["players"]},
            "events": events,
        })
        prev_cities = {n: dict(c) for n, c in cities.items()}
        prev_scores = {p["index"]: {**p["score"], "defeated": p.get("defeated")} for p in s["players"]}

    out = {"meta": {"seed": first.get("seed"), "turn_limit": last.get("turn_limit"), "map": first["map"],
                    "seam": seam(first), "terrain": TERRAIN, "victory": last.get("victory"),
                    "score_keys": ["total", "cities", "pop", "tiles", "techs", "defeated"]},
           "players": players, "static": static, "turns": turns}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, separators=(",", ":")))
    print(f"{args.out}: {len(turns)} turns, {args.out.stat().st_size:,} bytes, seam x={out['meta']['seam']}")


if __name__ == "__main__":
    main()
