"""Report on one long game: how far the agent got, against same-seed baselines and the civs it played.

    python playtest/longgame.py <run dir> [--baseline engine_ai=<record dir> ...] [--every 50] [--out report.md]

Reads the run's meta.json, summary.json, metrics.json and actions.jsonl, and the env's per-turn snapshots
(--record, default: the newest <run dir>/tmp/openciv3-*/game-*/record, where the env keeps them when its TMPDIR
is in the run directory). Each --baseline is the record directory of a same-seed game the bridge played with
that policy in the agent's seat (`CivBridge --record <dir>` plus `autoplay`).
"""

import argparse
import gzip
import json
import sys
from collections import Counter
from pathlib import Path

from analyze import load_json

SCORE = ("total", "cities", "pop", "tiles", "techs")
NOTABLE = ("city_founded", "city_lost", "city_destroyed", "war_declared", "unit_lost", "disorder_started",
           "civ_destroyed", "city_starved", "gold_stolen")
WATER = {"coast", "sea", "ocean"}


def snapshots(record: Path) -> dict[int, dict]:
    return {int(p.name[5:9]): json.loads(gzip.open(p).read()) for p in sorted(record.glob("turn-*.json.gz"))}


def human(snap: dict) -> dict:
    return next(p for p in snap["players"] if p.get("is_human"))


def rivals(snap: dict) -> list[dict]:
    return [p for p in snap["players"] if not p.get("is_human") and "barbarian" not in p["civ"].lower()]


def rank(snap: dict) -> int:
    me = human(snap)["score"]["total"]
    return 1 + sum(p["score"]["total"] > me for p in rivals(snap))


def shares(snap: dict) -> tuple[float, float]:
    """The agent's share of the world's land tiles and of its population (Civ III's domination needs 2/3 of each)."""
    me = human(snap)
    land = [t for t in snap["tiles"] if t[2] not in WATER]
    pop = sum(p["score"]["pop"] for p in snap["players"])
    return sum(t[4] == me["index"] for t in land) / max(len(land), 1), me["score"]["pop"] / max(pop, 1)


def checkpoints(turns: list[int], every: int) -> list[int]:
    picked = [t for t in turns if t % every == 0]
    return picked + ([turns[-1]] if turns and turns[-1] not in picked else [])


def report(run: Path, record: Path, baselines: dict[str, dict[int, dict]], every: int) -> str:
    meta, summary = load_json(run / "meta.json") or {}, load_json(run / "summary.json") or {}
    metrics = load_json(run / "metrics.json") or {}
    snaps = snapshots(record)
    if not snaps:
        raise SystemExit(f"no snapshots in {record}")
    turns = sorted(snaps)
    last = snaps[turns[-1]]
    me = human(last)
    scenario = {**(meta.get("scenario") or {}), "seed": meta.get("seed"), "turn_limit": meta.get("turn_limit")}
    out = [f"# {me['civ']}: turn {last['turn']} of {last.get('turn_limit')}", ""]

    leader = max(rivals(last), key=lambda p: p["score"]["total"])
    alive = [p for p in rivals(last) if not p.get("defeated")]
    s, land, pop = me["score"], *shares(last)
    out += [f"- **Setup:** {json.dumps(scenario)}; model {meta.get('model')}; stop: {meta.get('stop')}.",
            f"- **Final:** score {s['total']} ({s['cities']} cities, {s['pop']} pop, {s['tiles']} tiles, "
            f"{s['techs']} techs); rank {rank(last)} of {len(rivals(last)) + 1} (best rival {leader['civ']} "
            f"{leader['score']['total']}); {len(alive)} of {len(rivals(last))} rivals alive.",
            f"- **Share of the world:** {land:.1%} of the land, {pop:.1%} of the population."]
    for name, base in baselines.items():
        if last["turn"] in base:
            b = human(base[last["turn"]])["score"]
            out.append(f"- **vs {name} in this seat at T{last['turn']}:** {b['total']} ({b['cities']} cities, "
                       f"{b['techs']} techs): {me['score']['total'] - b['total']:+d}.")
    first_lead = next((t for t in turns if rank(snaps[t]) == 1 and t > 0), None)
    held = sum(rank(snaps[t]) == 1 for t in turns)
    out += [f"- **Lead:** first ranked 1st at T{first_lead}; 1st on {held} of {len(turns)} turns." if first_lead
            else "- **Lead:** never ranked 1st.", ""]

    out += ["## Score over the game", ""]
    head = ["Turn", "Score", "Cities", "Pop", "Tiles", "Techs", "Rank", "Best rival"]
    head += [f"{n} seat" for n in baselines]
    out += ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for t in checkpoints(turns, every):
        s, snap = human(snaps[t])["score"], snaps[t]
        best = max(rivals(snap), key=lambda p: p["score"]["total"])
        row = [t, *(s[k] for k in SCORE), rank(snap), f"{best['civ']} {best['score']['total']}"]
        row += [human(b[t])["score"]["total"] if t in b else "–" for b in baselines.values()]
        out.append("| " + " | ".join(map(str, row)) + " |")

    out += ["", f"## Standings at T{last['turn']}", "", "| Civ | Score | Cities | Pop | Tiles | Techs | Status |",
            "|---|---|---|---|---|---|---|"]
    for p in sorted([me, *rivals(last)], key=lambda p: -p["score"]["total"]):
        status = "defeated" if p.get("defeated") else "agent" if p.get("is_human") else "AI"
        out.append(f"| {p['civ']} | " + " | ".join(str(p["score"][k]) for k in SCORE) + f" | {status} |")

    kinds = Counter(e["kind"] for s in snaps.values() for e in s.get("events") or [])
    counts = ", ".join(f"{k} {n}" for k, n in sorted(kinds.items(), key=lambda kv: -kv[1]))
    out += ["", "## Events", "", counts or "none"]
    for e in [e for s in snaps.values() for e in s.get("events") or [] if e["kind"] in NOTABLE][:40]:
        out.append(f"- T{e['turn']} {e['kind']}: {e['text']}")

    agent, sessions = metrics.get("agent") or {}, meta.get("sessions") or []
    played = max(last["turn"] - (meta.get("start_turn") or 0), 1)
    cost, calls = meta.get("cost_usd") or metrics.get("cost_usd") or 0, agent.get("tool_calls") or 0
    out += ["", "## How the agent played", "",
            f"- **Time and cost:** {meta.get('wall_seconds')} s wall; ${cost} ({cost / played:.3f} per turn); "
            f"{len(sessions)} sessions.",
            f"- **Calls:** {calls} tool calls ({calls / played:.2f} per turn); agent error rate "
            f"{agent.get('agent_error_rate')}; nudges {meta.get('nudges')}.",
            f"- **Who decided:** {json.dumps(summary.get('decisions'))}.",
            f"- **Harness:** {json.dumps(summary.get('harness'))}.", ""]
    if sessions:
        out += ["| Session | Turns | Calls | Cost | Max context | Ended |", "|---|---|---|---|---|---|"]
        for i, s in enumerate(sessions, 1):
            out.append(f"| {i} | T{s.get('start_turn')}–T{s.get('end_turn')} | {s.get('calls')} | "
                       f"${s.get('cost_usd', 0):.2f} | {(s.get('max_context') or 0) // 1000}K | {s.get('end')} |")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", type=Path)
    ap.add_argument("--record", type=Path)
    ap.add_argument("--baseline", action="append", default=[], metavar="NAME=RECORD_DIR")
    ap.add_argument("--every", type=int, default=50)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    record = args.record or max(args.run.glob("tmp/openciv3-*/game-*/record"), key=lambda p: p.stat().st_mtime,
                                default=None)
    if record is None:
        raise SystemExit(f"no record directory under {args.run}/tmp; pass --record")
    baselines = {name: snapshots(Path(path)) for name, path in (b.split("=", 1) for b in args.baseline)}
    text = report(args.run, record, baselines, args.every)
    if args.out:
        args.out.write_text(text)
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
