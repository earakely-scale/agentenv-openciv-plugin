# Visualization prototypes

Four candidate redesigns of how an OpenCiv3 match is shown, for review. They are prototypes: nothing under `src/`
changes, and each reads the same per-turn snapshots the env already records (`docs/recording.md`).

| Variant | What it is | Replaces | Tech |
|---|---|---|---|
| **A. Broadcast frame** | A 1920×1080 frame per turn: the map, standings with rank changes and sparklines, a territory bar, key moments and the score chart | The MP4, GIF and the live view's `frame.png` | Pillow, ffmpeg (as today) |
| **B. Interactive replay** | One HTML page that draws the map from data: pan and zoom, tooltips on every tile, focus on a civ, layers, a timeline with lead changes, a chart with five metrics, a filterable event feed | The HTML replay and the live page | Canvas and SVG, no libraries |
| **C. Match report** | A post-game page: the headline, final standings, who led when, rank by turn, score small multiples, the expansion race, the final map, key moments | The results tables written by hand in the README | SVG, no libraries |
| **D. Seat grid** | Nine panels, one per seat, each following its own civ with its stats, sparkline and latest events | A new view for many-agent matches (live or replay) | Canvas, no libraries |

Previews are in [previews/](previews/); the `before-*.png` frames are today's renderer on the same game.

## What each variant fixes

Today's frame (`src/agentenv_openciv3/recording.py`) has these problems, all visible in the `frontier` video:

- **City labels pile up.** By T100 of a nine-civ game every city's name is drawn, and most collide. A, B and D
  place labels greedily by priority (capitals, then the largest cities) and skip any that would collide; B and D
  show more as you zoom in.
- **The map is split at the wrap edge.** The map wraps east to west and is drawn from x = 0, so a continent can be
  cut in two. All four variants cut the map at the column with the least land, then crop to the land.
- **The colours repeat.** The engine's palette gives Rome and the Mongols two near-identical reds, and Persia's
  tan and Zululand's peach measure ΔE 1.6 under colour-blind simulation (6.6 for normal vision). The variants use
  nine colours validated as a set for a dark surface (worst adjacent colour-blind ΔE 8.4), always next to a
  name, never alone.
- **Nine tangled lines.** The score chart draws every civ at once. B lets you focus one civ and greys the rest,
  and switches between score, cities, population, land and techs. C uses small multiples (each civ against the
  others in grey) and a rank chart.
- **The panel is a raw dump.** The side panel lists the last turn's events, mostly workers finishing mines. The
  variants show derived events for every civ instead: cities founded, captured or razed, civs eliminated, and
  lead changes, ranked by how much they matter.
- **The title overflows.** "Rome (opus) vs Greece (sonnet) vs …" runs into the panel with nine seats. The variants
  name the seats in the standings and keep the header short.
- **The HTML replay is 200 PNGs.** B draws from data (about 1 MB for 200 turns), so it can zoom, hover and filter.

## Run it

```bash
scripts/build-bridge.sh                                       # .NET 8 and the vendor/OpenCiv3 submodule
python prototypes/viz/record_game.py /tmp/rec                 # the engine's AI plays nine civs, 200 turns (~30 s)
python prototypes/viz/prepare.py /tmp/rec --out prototypes/viz/out/match.json \
  --labels Rome=opus,Spain=sonnet,Egypt=haiku,Zululand=grok,Arabia=kimi,Netherlands=gemini,Hittites=terra,Mongols=luna,Carthage=sol
python prototypes/viz/build_html.py                           # B, C and D: out/*.html, self-contained
python prototypes/viz/broadcast.py prototypes/viz/out/match.json --out prototypes/viz/out   # A: PNGs and MP4
python prototypes/viz/baseline.py /tmp/rec prototypes/viz/out <same labels>                 # today's renderer
```

`prepare.py` works on any recording, including a real match's `turn-*.json.gz`; for one, leave out `--labels`
and the seats are the civs the snapshots mark `is_human`.

## About the sample data

The previews use a real 200-turn OpenCiv3 game on a Standard map (seed 1, nine civs, Regent, roaming
barbarians), played by the engine's own AI so no LLM was needed. The model names are labels put on its civs to
look like the `frontier` match; they did not play. Because no agent played:

- There are no agent actions (`u4 settle → (32,28)`). Real matches have them in the action log, and B's event
  feed and A's panel have room for them.
- The game is peaceful: no city changed hands. A, B and C give captures and eliminations top priority and
  draw them in red on the timeline, but this game only has one razed city.
- Bridge events (`events` in a snapshot) are only the seats'. The variants derive events for every civ from
  consecutive snapshots instead.

## What it would take to ship one

- **A** is the cheapest: it is a new `Renderer` for `recording.py` with the same inputs, so the MP4, GIF and live
  frame change and nothing else does.
- **B** replaces the HTML replay in `recording.py` (its `_page`/`HTML`) and can also back `/live`: the live page
  would fetch the newest turn's compact data from `state.json` instead of a PNG.
- **C** needs only the snapshots and the verifier's summary; it could be a fourth recording format (`report`).
- **D** fits `/live` for matches with many seats, as a tab next to the map.
