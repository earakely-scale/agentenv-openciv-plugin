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
  "turn": 12, "turn_limit": 60, "seed": 3,
  "map": {"width": 60, "height": 60, "wrap_x": true},
  "players": [{"index": 0, "civ": "Rome", "is_human": true, "defeated": false, "color": [196, 52, 52],
               "score": {"total": 61, "cities": 2, "pop": 5, "tiles": 21, "techs": 3}}],
  "tiles": [[13, 9, "grassland", "forest", 0, 1, 1]],
  "cities": [{"x": 12, "y": 10, "name": "Rome", "owner": 0, "size": 3, "capital": true}],
  "units": [{"x": 14, "y": 10, "owner": 0, "type": "Settler"}],
  "events": [{"turn": 11, "kind": "city_grew", "text": "Rome grew to size 3"}]
}
```

- **`tiles`:** every tile of the map, regardless of fog. Each row is `[x, y, base_terrain, overlay or null, owner index or -1, river 0|1, known_by_human 0|1]`. Terrain names are lower-case engine keys.
- **`players`:** includes the barbarians. `color` is RGB, derived from the civ's primary colour index.
- **`events`:** the human player's events from the turn that just ended.
- **Cost:** reading a snapshot never draws from the engine RNG.

## 2. The env renders on demand

The env starts the bridge with `--record` unless `OPENCIV_RECORD=0`, and keeps the agent's tool calls
in memory, per turn. The extension `urn:openciv3:recording/v1` renders the recording so far:

- **Args:**
  - `formats`: list, default `["mp4", "html"]`; any of `mp4`, `gif`, `html`, `png` (the last turn).
  - `view`: `spectator` (default, everything) or `agent` (only tiles the player has explored).
  - `fps`: default 4.
- **Result:** `{"turns": n, "files": [{"name": "openciv3-seed3.mp4", "content_type": "video/mp4", "bytes": n, "base64": "..."}]}`.

**Formats:**
- **`mp4`:** needs `ffmpeg` on `PATH` (the image ships it). Without it the env falls back to an animated `gif` and says so.
- **`html`:** a single self-contained file. It has a turn slider, play and pause, the map frame, the scoreboard, and that turn's agent actions and events.

**What a frame shows:**
- **The map:** the diamond grid drawn as diamonds. Terrain is coloured; borders are tinted with the owner's colour; rivers are blue lines. Cities are discs in the owner's colour with name and size. Units are small dots.
- **A side panel:**
  - the turn and limit;
  - the scoreboard (civ, score, cities, pop, techs), with the human highlighted;
  - the agent's actions that turn, one per line, for example `u4 settle → (32,28)` and `c1 builds Settler`;
  - the turn's events.

Rendering lives in `agentenv_openciv3.recording` and uses Pillow only, so the playtest harness and
tests can call it directly on a directory of snapshots.

## 3. A task step saves it

The plugin registers the step type `save_env_recording`, through the `agent_env.task_steps` entry
point. Its fields:

| Field | Default | Meaning |
|---|---|---|
| `env_id` | required | The deployed env to record |
| `extension_uri` | `urn:openciv3:recording/v1` | The env extension that returns `{"files": [...]}` |
| `formats` | `["mp4", "html"]` | Passed to the extension |
| `view` | `spectator` | Passed to the extension |
| `timeout_seconds` | `300` | For the extension call |
| `fail_task_on_error` | `false` | A failed recording never fails the game |

**What it does:**
- Each returned file is stored as a `file` artifact, `<task id>-recording-<instance id>.<ext>`, so bulk output goes to the object store rather than the context.
- It records `context.metadata["recordings"][<step id>] = [{"name", "artifact_id", "version", "bytes", "content_type"}]`.
- It logs one line per file, saying where the file is.
- It works with any env that advertises an extension returning that shape, so it can later move to agent-env core unchanged.

Every task in the `openciv3` bundle ends with this step, after the game and in parallel with grading.

## 4. Playtests and replays

- **Playtests:** `playtest/run.py` calls the extension at the end of every game and writes `recording.mp4` and `replay.html` into the run directory. The batch report links them.
- **Replays:** `playtest/replay.py <run dir>` rebuilds the recording of a game played before recording existed. It replays the run's `actions.jsonl` against a fresh bridge with the same seed and scenario; the engine is deterministic, so the replay matches. It checks that the final score equals `summary.json` and fails if not.
