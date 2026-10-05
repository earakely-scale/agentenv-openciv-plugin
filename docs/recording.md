# Game recordings

Every game is recorded as it is played, and every task run saves the recording as artifacts. The
game is headless, so the recording is rendered from the game state: a top-down map of the world each
turn (terrain, borders, cities, units), a scoreboard, and the actions the agent took that turn.

## 1. The bridge records every turn

`CivBridge --record <dir>` writes one snapshot after `new_game` and one after every turn that
`end_turn` or `autoplay` completes: `<dir>/turn-0000.json.gz`, `turn-0001.json.gz`, …. The `world`
command returns the same snapshot on demand.

```json
{
  "schema": 2, "turn": 12, "turn_limit": 60, "seed": 3, "date": "3450 BC",
  "map": {"width": 60, "height": 60, "wrap_x": true},
  "seats": [{"index": 1, "civ": "Rome", "label": "opus"}],
  "players": [{"index": 1, "civ": "Rome", "is_human": true, "label": "opus", "defeated": false, "color": [196, 52, 52],
               "score": {"total": 61, "cities": 2, "pop": 5, "tiles": 21, "techs": 3},
               "gold": 40, "government": "Despotism", "research": "Bronze Working", "at_war": [3], "contacts": [2, 3],
               "culture": 120, "city_culture": 80, "era": 0, "land": 0.0412, "pop": 0.1538}],
  "tiles": [[13, 9, "grassland", "forest", 0, 1, 1, null, ["road"], 0]],
  "cities": [{"id": "city-1", "x": 12, "y": 10, "name": "Rome", "owner": 1, "size": 3, "capital": true, "production": "Warrior",
              "era": 0, "walls": false, "wonders": ["The Pyramids"]}],
  "units": [{"id": "Settler-4", "x": 14, "y": 10, "owner": 1, "type": "Settler", "hp": 1, "hp_max": 1, "fortified": false,
             "aboard": null}],
  "moves": [{"seq": 57, "unit": "Settler-4", "owner": 1, "type": "Settler", "path": [[12, 10], [13, 11], [14, 10]], "seen": 1}],
  "battles": [{"id": 3, "seq": 58, "turn": 11, "kind": "attack",
               "attacker": {"owner": 1, "type": "Warrior", "x": 14, "y": 12, "hp_before": 3, "hp_after": 3, "hp_max": 3},
               "defender": {"owner": 0, "type": "Warrior", "x": 15, "y": 13, "hp_before": 2, "hp_after": 0, "hp_max": 3},
               "rounds": ["a", "a"], "winner": "attacker", "city": null, "captured": false, "razed": false, "seen": 1}],
  "trades": [{"seq": 59, "turn": 11, "a": 1, "b": 3, "a_gave": "60 gold", "b_gave": "Bronze Working"}],
  "events": [{"turn": 11, "kind": "city_grew", "text": "Rome grew to size 3"}]
}
```

- **`schema`:** `2`. Snapshots without it are schema 1: no `seats`, `known` is 0/1, and none of the fields below marked (2).
- **`seats`:** the agent seats in seat order, one in a single-seat game; `index` is the player's. (2)
- **`tiles`:** every tile of the map, regardless of fog. Each row is `[x, y, base_terrain, overlay or null, owner index or -1, river, known]`, where `known` is a bitmask over `seats`: bit `k` is set when `seats[k]` knows the tile, so non-zero means some seat does (schema 1: 0/1). Terrain names are lower-case engine keys. `river` is the tile's river edges as in `known_map` (`NE=1, SE=2, SW=4, NW=8`, ... in [protocol.md](protocol.md#known_map)), 0 for none; older recordings have 0/1, so read it as a flag unless you need the edges.
- **What the client's art draws** (columns 7-9 of a tile, a city's `era` and `walls`, a unit's hit points): a tile's
  `resource` (its name, once some civ knows of it: Iron only once a civ has Bronze Working; else null), its
  `improvements` (as in `known_map`, with `barbarian_camp`) and `bonus` (1 for bonus grassland, which shows its
  shield); a city's `era` (its owner's, 0-3) and `walls`; a unit's `hp`, `hp_max` and whether it is `fortified`.
  Snapshots from before these fields have none of them.
- **`moves`, `battles`:** what happened since the last snapshot, in the order it happened (patches 0010 and 0012):
  every unit's runs of steps, whoever saw them, and every battle. A move is `known_map`'s entry for every seat at
  once: `unit` is the engine's id, `path` the tiles from where it stood, and `seen` the seats that saw it, a mask
  over `seats` as a tile's `known`; a run is cut where who saw it changes, and at a turn's end. A battle is `known_map`'s without the
  sides' ids, plus `seen`. With every snapshot written, each unit's runs lead from where the last snapshot had it
  to where this one has it (units carried aboard ships aside). The `world` command has the same as the last
  snapshot, and what happened since.
- **`players`:** includes the barbarians. `color` is RGB, derived from the civ's primary colour index. `is_human` marks every civ an agent plays (one, or one per seat), and `label` the seat's label from `new_game`. Every player, seat or AI, also has (2) its `gold`, its `government`'s name, the tech it is `research`ing (null if none), the player indices it is `at_war` with and the ones it has met (`contacts`), both ascending and without the barbarians.
- **The race** (the spectators' stories): the turn's `date` as the client shows it (`state.date`; null for a ruleset
  that doesn't count years); each player's `culture` (and its best city's, `city_culture`), `era` (0-3) and shares of
  the world's `land` and people (`pop`),
  as `state.race` has them (0 for the barbarians); each city's great `wonders`; and `trades`, the trades seats made
  since the last snapshot (in `seq` order with moves and battles): `a` gave `a_gave` to `b` for `b_gave`, each
  described as in the brief ("60 gold", "Bronze Working, Currency"). The engine's AIs' trades among themselves are
  not recorded. Snapshots from before these fields have none of them.
- **`cities`, `units`:** (2) `id` is the engine's own id as a string (`"city-1"`, `"Settler-4"`): unique in the game, kept for the city's or unit's life and through saves. It is not the `c1`/`u1` id a seat's commands use. A city's `production` is the name of what it builds, or null. A unit's `aboard` is the engine id of the ship carrying it, or null.
- **`events`:** the human player's events from the turn that just ended. In a game with seats, every seat's, each with `"civ"`.
- **Several agents:** the renderers outline and bold every seat, name each as `civ (label)` in the scoreboard and on the chart, title the frame `Rome (Opus) vs Greece (Sonnet) vs …`, and prefix each action and event with its seat.
- **Cost:** reading a snapshot never draws from the engine RNG.

## 2. The env renders on demand

The env starts the bridge with `--record` unless `OPENCIV_RECORD=0`, and keeps each seat's tool calls
in memory, per turn: its game actions and how many calls succeeded and failed. The extension
`urn:openciv3:recording/v1` renders the recording so far:

- **Args:**
  - `formats`: list, default `["mp4", "html"]`, plus `client_mp4` in an image with the client (section 5); any of
    `mp4`, `gif`, `html`, `png` (the last turn) and `client_mp4`.
  - `view`: `spectator` (default, everything) or `agent` (only tiles the player has explored).
  - `fps`: default 4.
  - `client_seats`: the seats (civ or label) whose client view `client_mp4` renders; default: every seat.
- **Result:** `{"turns": n, "notes": [...], "files": [{"name": "openciv3-seed3.mp4", "content_type": "video/mp4", "bytes": n, "base64": "..."}]}`.
  A game with several seats names its files `openciv3-seed3-seats.*`; the `agent` view adds `-agent`.

**Formats:**
- **`mp4`:** needs `ffmpeg` on `PATH` (the image ships it). Without it the env falls back to an animated `gif` and says so.
- **`html`:** the match viewer ([docs/viewer.md](viewer.md)) as one self-contained file that works offline: the
  live view's app with the whole game's data embedded (`MatchData.document()`, every seat's actions and call
  counts per turn). With `client_mp4` in the same call, it also knows each seat's client video (file name, fps,
  and the turn of each frame) and plays the files that sit next to it, as `agent-env openciv3 recordings --out`
  leaves them.
- **`client_mp4`:** the real client's view (section 5). One seat: `<name>.client.mp4`. Several seats: one video
  per seat, `<name>.client-<label or civ>.mp4` (e.g. `openciv3-seed1-seats.client-opus.mp4`), each a frame per
  kept turn from that seat's point of view; `client_seats` limits it to some seats.

**What a frame shows** (mp4, gif, png and the live view's map): one 1920x1080 frame per turn, drawn from the
viewer's data (`matchdata.MatchData`, [viewer.md](viewer.md)). A frame for turn T shows nothing after T.
- **The map:** cut at the column with the least land and cropped to the land. Borders and a tint in each civ's
  colour; cities are discs sized by population, capitals starred; military units are dots. Labels go to capitals,
  then the largest cities, and never overlap. A city captured or razed in the last few turns is ringed in red.
  `view=agent` draws only the tiles some seat has explored.
- **Standings:** rank and its change over 10 turns, score bar, cities, pop, techs and a 40-turn sparkline. Under
  each civ, in games with agents or wars: the seat's tool calls and failures in the turn just played and its
  latest game action (an AI civ's government instead), and whom it is at war with.
- **Territory:** each civ's share of the claimed tiles.
- **Key moments:** eliminations, captures, razings, wars, great wonders, landings, peace, lead changes, trades, new
  eras, governments, first contacts, foundings and techs,
  ranked by weight and age; eliminations, captures, razings and wars stand out in red. A lead change counts once the
  new leader has held the lead every turn since, for up to 5 turns, so a close race's flips don't crowd it out.
- **The score chart:** every civ over the turns so far (both scales follow them), the lead changes that held on the
  axis, and the baselines' scores when the env plays them.

Rendering lives in `agentenv_openciv3.recording` and uses Pillow only, so the playtest harness and
tests can call it directly on a directory of snapshots.

## 3. A task step saves it

The plugin registers the step type `save_env_recording`, through the `agent_env.task_steps` entry
point. Its fields:

| Field | Default | Meaning |
|---|---|---|
| `env_id` | required | The deployed env to record |
| `extension_uri` | `urn:openciv3:recording/v1` | The env extension that returns `{"files": [...]}` |
| `formats` | the env's default | Passed to the extension when set |
| `view` | `spectator` | Passed to the extension |
| `timeout_seconds` | `300` | For the extension call |
| `fail_task_on_error` | `false` | A failed recording never fails the game |

**What it does:**
- Each returned file is stored as a `file` artifact, `<task id>-recording-<instance id>.<suffix>`, so bulk output goes to the object store rather than the context. The suffix is everything after the first dot of the file's name (`mp4`, `html`, `client.mp4`), so two videos keep separate artifacts.
- It logs the extension's notes (a format that was skipped or fell back) as warnings.
- It records `context.metadata["recordings"][<step id>] = [{"name", "artifact_id", "version", "bytes", "content_type"}]`.
- It logs one line per file, saying where the file is.
- It works with any env that advertises an extension returning that shape, so it can later move to agent-env core unchanged.

Every task in the `openciv3` bundle ends with this step, after the game and in parallel with grading.

## 4. Playtests and replays

- **Playtests:** `playtest/run.py` calls the extension at the end of every game and writes `recording.mp4` and `replay.html` into the run directory. The batch report links them.
- **Replays:** `playtest/replay.py <run dir>` rebuilds the recording of a game played before recording existed. It replays the run's `actions.jsonl` against a fresh bridge with the same seed and scenario; the engine is deterministic, so the replay matches. It checks that the final score equals `summary.json` and fails if not.

## 5. The real client's view (`client_mp4`)

The image's `client` target (`docker build --target client`, or `agent-env openciv3 setup --client`) adds
the OpenCiv3 client itself: Godot 4.4.1 .NET on Xvfb with Mesa's CPU renderer, so it needs no GPU or
display. In that image:

- **Saves:** the env starts the bridge with `--saves <dir>`, which keeps every turn's autosave as
  `turn-NNNN.json.gz`.
- **Rendering:** `client_mp4` loads each save into the client, captures the map as the player sees it at
  the start of that turn, and joins the frames into `<name>.client.mp4`. A frame shows the real art, fog of
  war, borders, units, the minimap and the status box. The capture is `client/FrameCapture.cs`, an
  autoload added to a copy of the client when the image is built; `vendor/OpenCiv3` is not modified.
- **Defaults:** `client_mp4` is in the env's default formats there, so the bundle's tasks record it with
  no change.
- **Cost:** about 3 s to start plus 0.8 to 1.3 s per turn on 4 CPUs, so a 60-turn game renders in about a
  minute. Raise the step's `timeout_seconds` (300) for games over about 200 turns. The image is about
  350 MB larger.
- **Failures:** when the client is missing or fails, the other formats are still returned and the reason
  is a note.

**Art licensing.** The client draws OpenCiv3's community art from
[C7-Game/Assets](https://github.com/C7-Game/Assets), which carries no licence. The image fetches it at
build time, and it is never committed here. Because a client image contains the art, don't push it to a
public registry.

[client/README.md](../client/README.md) has the capture's details, measurements and how to run it on a
Mac for debugging.

## 6. Watching a game live

The same per-turn snapshots (and, with the client, the per-turn saves) feed the match viewer while the game plays:
`GET /live` on the env, which `agent-env openciv3 watch` finds for envs running locally. It is the app the `html`
format embeds; live, it asks `GET /live/data.json?since=N` for the turns it has not seen, plus the turn being
played: which seats have ended it, for how long each has played it, and their calls and actions so far. The env
keeps one viewer document per game and adds each snapshot once, when the bridge has written it whole, so a request
never reads the game again. The real client draws the seat the viewer asks for, one render at a time and the
newest save only, so a slow client skips turns instead of falling behind. With `OPENCIV_RECORD=0` there are no
snapshots, and the viewer says so. The older page's routes (`/live/state.json`, `/live/frame.png`) still work.
Routes: [docs/tools.md](tools.md#watching-a-game-live); the data and the `live` object: [docs/viewer.md](viewer.md).

`/live?stream` lays the viewer out for a 1920×1080 screen with nothing to click: larger text, no tabs, buttons or
hints, and the newest turn always shown. It alternates the map with the agents' panels, shows the summary once the
game is over, and, with the client, keeps one agent's client view in a corner, a different agent every half minute.
`agent-env openciv3 stream` shows that layout in Chromium on a virtual display
(`streamer/`) and sends it with ffmpeg (H.264 at 4.5 Mbit/s, a keyframe every 2 s) to Twitch, X or any RTMP server,
or with `--to twitch --to x` to both from one encode. The
sound is whatever the page plays, captured from a PulseAudio null sink as 128 kbit/s AAC: silence, unless the casters
talk. It streams the newest env whose game is under way, waiting for one to start, and ends the stream `--linger`
seconds after GAME OVER, or a minute after the env is gone. `--record DIR` also writes the stream to
`DIR/stream-<UTC time>.mkv`, from the same encode (ffmpeg's tee muxer: the file and the other servers keep going if
one RTMP server fails), and `--offline` only records. The stream keys come from agent-env's secret store (X's server
URL too, `OPENCIV3_X_SERVER`), and the streamer replaces them in everything ffmpeg prints.

When the task's `openciv3_match` turns on the casters (its `broadcast`, [tools.md](tools.md)), or with `--cast`,
`streamer/caster.py` runs next to the browser: two AI casters, by default Max (play-by-play) and Ada (the analyst);
the task may rename them and pick their voices, styles and models, and `--no-cast` keeps them off. It reads `GET /live/data.json` every 2 s, as the viewer does, and talks only about what that data shows:
the intro (the players, the turns, that they can message each other), the biggest new events first (an elimination,
a city taken or razed, war, a great wonder, a landing by a model or on a model's land, two models meeting, peace, a
model's trade, a lead change the new leader holds, a model's new era, then a change of government, a model's
upgrades or a civ's first cities), the agents' messages and notes, analysis when nothing happens (standings, gold,
wars, plans, who is still thinking, the race to each victory, wonders and culture), and the outro at GAME OVER, which
fits in the default minute of `--linger`. Their data has the year, each civ's shares of the land and of the people,
culture and great wonders, and who is nearest domination and a cultural victory; seats go by the broadcast's `names`
when it has them.

Each caster is an agent: a beat's subject is picked as above, and its lines are written one at a time,
alternating, each by its own caster's call that sees the line before it, so the two talk to each other rather than
taking turns at a script. Before it speaks, a caster may look the match up with tools that answer from the same data:
`standings` (now or at an earlier turn), `civ` (one civ in depth: its score and rank now and 10, 25 and 50 turns ago,
cities, army by unit type, wars, record, plan, notes, messages and tool calls), `trend` (a stat over the game and
its change), `events` (by kind, civ and turns), `battles` (wins and losses, the unit types that won, cities taken,
of one civ or two with each other), `diplomacy` (wars, peace, trades,
contacts and messages, in order), `turn_now` (who is still thinking and what each seat has done so far), `city` (a
city's story) and `said` (what the desk has already said, for callbacks); it speaks with `say`. A lookup it gets
wrong answers with an error it can read (a civ or city the game hasn't, the arguments the lookup takes), never stops
the line. A live line looks
things up a round at a time while the audio already queued outlasts one more round, the line and its voicing (by the
running times of its calls; at most three rounds), then must speak: it is told it is out of time, and its last call
forces `say` (or, for a model that turns a forced call down, asks for it). Breaking
news (an elimination, a city taken or razed, a war) cuts into a beat under way, and a line that asks the other caster
a question gets its answer, up to four lines a beat. In the background, every 45 s at most and once a turn, the analyst
researches the game with the same tools and jots talking points into a notebook both casters see, each for two beats
or 15 turns. The casters' calls use the task's caster `model` (default `anthropic/claude-haiku-4-5`), or a caster's
own (`play_by_play.model`, `analyst.model`: say, a fast model for the play-by-play and a stronger one for the
analysis); a model the endpoint turns tool calls down for writes its lines from the data alone, as before, and does
no research. Each line is
voiced by the `tts_model` (default `openai/gpt-4o-mini-tts`) in its caster's voice, levelled to the same loudness, and
published at once, so the page plays it while the next is written. It paces itself to the audio: the next line is
written so that it lands as the last one ends, so the talk runs on without piling up. The endpoint is agent-env's
`[model]`; a failed call is retried after 5 s (a beat whose lines fail three times in a row ends where it got to; one that said nothing leaves its news and the intro for the next, and its subject, if analysis, gives way to another),
and a line whose voice failed is still captioned. The stream is public, so the casters are told to keep it
clean and to paraphrase a rude message rather than quote it, and a word on the env's blocklist, or one it masked
(`s***`), is said and captioned as "bleep" if a line has one anyway. The page (`?stream&cast=<caster URL>`) polls
`/cast.json?since=<id>` for new lines (`speaker`, `name`, `text`, `audio`, `seconds`, `turn`, `focus`, `kind`) and
plays `/audio/<id>.wav` in order with a caption; Chromium runs with autoplay allowed. The task's `title`, or
`--title`, puts the broadcast's name on screen and in the intro.

