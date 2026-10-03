# Playtest harness

Claude Code plays the OpenCiv3 env through its MCP tools, and the harness measures whether it
operates the game well. Run it with the Python environment the env is installed in (the default
server command is `python -m agentenv_openciv3.server` with that interpreter, and `replay.py` uses
the env's MCP client) and a logged-in `claude` CLI. A locally built bridge needs `CIVBRIDGE_CMD`
pointing at it and, with a .NET outside the system location, `DOTNET_ROOT`.

```bash
python playtest/run.py --seed 1 --turns 60 --model sonnet                  # one game
python playtest/batch.py --seeds 1,2,3 --turns 60 --model sonnet --parallel 3   # the acceptance batch
python playtest/baseline.py --seeds 1,2,3 --turns 60 --out baselines.json  # baselines only, no LLM
python playtest/analyze.py playtest/runs/<batch>/seed-1                    # re-analyze one run
python playtest/batch.py --name <batch> --report-only                      # re-analyze a batch
python playtest/replay.py playtest/runs/<batch>                            # rebuild recordings
python playtest/longgame.py <run dir> --baseline engine_ai=<record dir>    # report on one long game
```

`--server-cmd` takes the placeholders `{port}`, `{seed}`, `{turns}` and `{run_dir}`, so a Docker image
works too:
`--server-cmd "docker run --rm -p 127.0.0.1:{port}:18765 -e OPENCIV_SEED={seed} -e OPENCIV_TURN_LIMIT={turns} -e OPENCIV_ACTION_LOG=/run/actions.jsonl -v {run_dir}:/run openciv3"`.
`--url` attaches to an env that is already running (`--actions` points at its action log).

## One run (`run.py`)

1. Start the env on a free port with `MCP_HOST=127.0.0.1`, `MCP_PORT`, `OPENCIV_SEED`,
   `OPENCIV_TURN_LIMIT` and `OPENCIV_ACTION_LOG`; wait for `/.well-known/agent-env.json`; `data/reset`
   (or `data/add` with `--scenario`).
2. Write `mcp.json` (HTTP, the card's MCP interface) and start one `claude -p` with stream-json in
   and out, `--tools ""` and `--allowedTools "mcp__openciv3__*"` (the env's tools are the only tools),
   `--strict-mcp-config`, `--setting-sources ""`, `--permission-mode dontAsk`, `--max-turns` per
   segment, `--max-budget-usd` and `--no-session-persistence`. The prompts come from `prompt.md`:
   the goal, how the score is computed, the tools by name and the rules the env does not show itself,
   with no advice on how to play.
3. The run is a harness error (exit 2) unless claude's init event shows the MCP server connected.
4. After each result, `data/get`: while the game is not over, send
   `Game not over (turn X/Y). Continue playing.` Stop on game over, an exhausted budget,
   `--max-nudges`, `--max-stalled-nudges` segments in a row without the turn advancing, or
   `--timeout-min`.
4a. **Long games:** with `--context-cap N`, a session whose context has passed N tokens at a result
   ends, and a new `claude -p` takes over. It gets the game prompt, `prompt.md`'s resume prompt and,
   in every session, its handoff, which asks the agent to keep the plan current. The env keeps the
   game, the brief and the plan, so nothing else is carried over. Budget and timeout are for the whole
   game. `meta.json` lists the sessions (turns, calls, cost, peak context, why each ended), and the
   cost in `meta.json` and `metrics.json` is the sum. The cap acts only at a segment's end, so a session
   can run up to `--max-agent-turns` requests past it.
5. Final `data/get`, then the env's `urn:openciv3:recording/v1` extension (skip with
   `--no-recording`; `--recording-formats mp4,html,client_mp4` adds the real client's view in the
   client image), stop the env's process group, remove claude's `~/.claude/projects/<slug>`.

Run directory: `transcript.jsonl` (claude's stream), `actions.jsonl` (the env's action log),
`summary.json` (final `data/get`), `meta.json` (including the scenario), `metrics.json`,
`server.log`, `claude.stderr`, and the recording: `recording.mp4` (`recording.gif` when the env has
no ffmpeg), `replay.html` and, when asked for, `client.mp4`. The batch report links them.

**Long-game report (`longgame.py`):** where the agent got to, from the env's per-turn snapshots. It
includes:
- score, cities, pop, tiles and techs at checkpoints;
- the agent's rank among the civs;
- the same-seed baselines in its seat;
- the final standings and notable events;
- a table of sessions.

For the snapshots to outlive the env, give the env a `TMPDIR` inside the run directory. A baseline is
the record directory of a game the bridge played in the agent's seat: `CivBridge --record <dir>`, then
`new_game` and `autoplay` with that policy.

## Metrics and gates (`analyze.py`)

| Metric | Definition |
|---|---|
| agent error rate | failed tool calls / tool calls, from the transcript (includes argument errors that never reach the env), leaving out harness artifacts |
| harness artifacts | calls to a tool name the client doesn't have (e.g. a bare `list_units`) and permission denials; counted on their own |
| error rate | every failed call / every call (gate v1); `env_log.error_rate` is the env-side rate |
| max consecutive errors | longest run of failing agent calls; flag `error_streak` at 5 |
| repeated failures | most times one identical failing call (tool and args) was made in one game turn; flag `repeat` at 3 |
| stall turns | game turns with 25 or more calls (from the action log); flag `stall`, also when the run stopped as stalled |
| no-op end_turns | `end_turn` with units needing orders and no game change since the last one; flag `passive` above half the turns |
| agent picks | from `data/get` `decisions`: the share of completed production items and learned techs the agent chose rather than the engine |
| harness counters | `data/get` `harness`: `autoplay_turns` (must be 0 in a graded game; flag `autoplay`), `new_games`, `extension_calls` |
| disorder, waste | `disorder_city_turns` (flag `disorder` at 10) and `shields_lost`, when `data/get` metrics report them |
| engine failures | action-log errors `engine_error`, `turn_failed`, `bridge_failed`, `bridge_down`, `timeout`, `internal_error`; flag `env_failure` |
| scores | the score, every baseline at the turn limit (`null`, `found_capital`, `settler_bot`, `engine_ai`) and the margin over each |
| also | blocked end_turns, nudges, cost, wall and API time, tokens, calls and cost per turn |

**Gate v2** (the gate): a run passes when it is valid (no harness error, no engine failure, no
autoplay on the agent's game), reaches the turn limit undefeated, has an agent error rate below 10%,
no `stall` or `repeat` flag, and scores strictly above `settler_bot`, the scripted bot that follows
the env's own suggestions. The batch passes when at least 2/3 of its runs pass (acceptance: 3 seeds
x 60 turns).

**Gate v1** (round 1, reported for comparison): valid, limit reached, every failed call below 15%,
no `stall` or `repeat`, above `null`; every run must pass, and the stretch goal is a score at or
above the built-in AI on 2/3 of the seeds.

Baselines come from `baseline.py` (the env's autoplay extension; all four policies by default, same
seed, scenario and turn limit), else from the baselines the env reports in `data/get`.
`baseline.py --add --out <batch>/baselines.json` runs only the policies the file lacks.

## Replays (`replay.py`)

`replay.py` rebuilds the recording of a run played before recordings existed: it replays the run's
successful game-changing calls from `actions.jsonl`, in order, on a fresh env with the run's seed
and scenario, checks the turn before every call and the final turn and score against
`summary.json`, and only then writes `recording.mp4` and `replay.html`. `end_turn` is replayed as
single turns with `skip_idle=true`, as many as the original advanced, so blockers and stop
conditions added since cannot change what happened. On any difference it writes nothing and exits 1.
`--no-recording` only checks.

## Scripted matches without an LLM (`bots.py`)

`bots.py` plays a whole multi-seat match with scripted players, for testing the live viewer and the
recordings on realistic games at no LLM cost. It starts the env locally (no Docker) on a copy of
`build/bridge`, starts the match the way the `frontier` task does (the `urn:openciv3:new-game/v1`
extension with civs, seats, labels, size and turn limit) and runs one MCP client per seat, each
sending its `X-OpenCiv3-Seat` header and playing every turn with real tool calls, so the action log,
per-seat turns and live routes all get exercised.

```bash
.venv/bin/python playtest/bots.py --out /tmp/match-9                       # 9 seats, Standard, Regent, 200 turns, seed 1
.venv/bin/python playtest/bots.py --seats 3 --turns 30 --out /tmp/match-3 --fast   # a quick one
```

It prints the live view (`http://127.0.0.1:<port>/live`) at the start. The defaults match the
`frontier` task: Rome=opus, Greece=sonnet, Egypt=haiku, America=sol, Babylon=luna, Persia=terra,
England=gemini, Carthage=grok and China=kimi on a Standard map at Regent with roaming barbarians.
Options:
- `--seats N` keeps the first N of them; `--civs Rome=a,Greece=b` sets your own.
- `--turns`, `--seed`, `--size`, `--difficulty`, `--barbarians` and `--ai-opponents` (engine-AI civs
  besides the seats) set the game.
- `--personalities label=kind,...` changes who plays how.
- `--think MIN MAX` is the random delay before each call (default 0.05-0.3 s, so seats finish their
  turns at different times); `--fast` sets it to 0.
- `--error-rate` is the share of calls preceded by a deliberately wrong one (default 0.015; with the
  game's own refusals about 2% of calls fail).
- `--linger S` keeps the env up after the game, for the viewer.
- `--recording html` also renders the env's recording.

**Personalities:**
- **expansionist** (haiku, sol, kimi): settlers from every city, many cities.
- **builder** (sonnet, luna, gemini): a few cities, buildings, a wonder, Republic. Accepts every peace
  offer.
- **aggressive** (opus, terra, grok): 6-7 cities and barracks, then an army. A third of the way in it
  declares war on its nearest neighbour, whose cities it found with `view_map`. It marches to them
  and attacks them, then offers peace after 30-50 turns, and may pick a new target later.

Every seat researches, chooses its government, fortifies a garrison, explores, puts workers on
`auto_work`, sometimes buys, fixes disorder with `set_rates` and writes a `plan`. A failing call never
stops a seat, and a turn always ends. The engine razes the cities it takes, so conquests show up as
cities razed, not captured.

**Output directory:**
- `record/`: the bridge's `turn-*.json.gz` snapshots.
- `saves/`: the engine's per-turn saves. The env passes `--saves` only when the client is installed, so
  `bots.py` adds it to `CIVBRIDGE_CMD`; `--no-saves` turns it off.
- `autosave/`
- `actions.jsonl`: the env's action log, every seat, each row with `seat`. `actions/<label>.jsonl` is
  the same, split by seat.
- `summary.json`: `data/get` at the end.
- `bots.json`: each bot's calls, failures and notable moves.
- `match.json`: seats, labels, personalities, seed, the live URL, timings, failure rate, standings and
  `history` (wars, peace, cities captured or razed, civs eliminated, from the snapshots).
- `server.log` and `bots.log`: every call.

## Testing the harness without the engine

`stub_env.py` serves the same tool names, card, data plane (decisions, harness counters,
baselines), autoplay and recording extensions and action log over a toy game:

```bash
python playtest/run.py --seed 1 --turns 5 --model haiku --budget-usd 0.15 --server-cmd "python playtest/stub_env.py"
pytest playtest      # offline, with a fake claude: no LLM cost
```
