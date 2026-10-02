# agentenv-openciv3

[OpenCiv3](https://github.com/C7-Game/OpenCiv3), the open-source Civilization III remake, as an
[AgentEnv](https://github.com/scaleapi/agentenv-framework) environment. An LLM agent plays a seeded
game through twelve MCP tools. The game is graded against same-seed baselines, one of which is a
scripted bot that plays through the same tools, and every game can be saved as a video and an HTML
replay.

The game runs headless: no Godot, no display and no Civilization III files. One env serves one game
at a time, 

https://github.com/user-attachments/assets/3c59d7b2-831a-40ff-9db7-920af600b0a0

and a game is deterministic: the same seed and the same actions give the same game.



## Scope

This is a 4X game short of its endgame. The agent:
- founds and places cities, and chooses what they build and what to research;
- sets the science and luxury rates (tax is what they leave) and buys production with gold;
- moves, automates and fortifies units, and attacks and bombards enemies;
- changes government;
- declares war and makes peace, at the price the other civ asks.

It cannot trade techs or gold outside a peace treaty. The env still declines every AI trade offer, and
the brief says so.

The rivals are OpenCiv3's own AI. Cities that fall are razed, not captured, and the engine has no victory
conditions, so a game ends at its turn limit (docs/full-game.md). The score rewards cities, citizens,
territory and techs.

## Quickstart

You need Docker, [uv](https://docs.astral.sh/uv/) and git. One command clones the repository, installs
`agent-env` with the plugin, builds and registers the env, and plays the smoke game:

```bash
curl -fsSL https://raw.githubusercontent.com/earakely-scale/agentenv-openciv-plugin/main/scripts/install.sh | bash
```

`bash -s -- --agent` (after the pipe) also sets up the Claude player agent, so Claude can play: it asks for a
model key, without echoing it. `--client` adds recordings of the real game's view. `--help` lists the
options; [scripts/install.sh](scripts/install.sh) is short.

By hand, the same steps, with no model needed:

```bash
git clone https://github.com/earakely-scale/agentenv-openciv-plugin
git -C agentenv-openciv-plugin submodule update --init vendor/OpenCiv3   # not --recursive: its art isn't needed
uv tool install agentenv-framework --with-editable ./agentenv-openciv-plugin

agent-env openciv3 setup              # build the env image for this machine, register it as the env "openciv3"
agent-env run openciv3 --task smoke   # the scripted bot plays 30 turns; the game is graded and recorded
agent-env openciv3 recordings --out recordings   # copy the game's video and HTML replay here
```

`setup` builds the image `mcp-server-openciv3` for the Docker host's own platform (`linux/arm64` on
Apple Silicon) and registers it as the MCP server env `openciv3`. Pass `--platform linux/amd64` for
remote sandboxes, `--source <checkout>` when the plugin isn't installed from a checkout, and
`--image <tag>` to register an image you already have. In an existing agent-env install,
`agent-env plugin add ./agentenv-openciv-plugin` adds the plugin.

The `smoke` task checks the image, the bridge, the env's extensions, the verifier and the recorder
end to end. It ends with `passed (grade: 1)`: the scripted bot playing your seat matches the
`settler_bot` baseline exactly.

## Have an LLM play

**With Claude Code, through the playtest harness.** This works out of the box with a logged-in
`claude` CLI (a 60-turn Sonnet game cost about $0.15 in round 1). It needs a local bridge, which
needs the .NET 8 SDK:

```bash
cd agentenv-openciv-plugin
uv venv && uv pip install -e '.[dev]'
scripts/build-bridge.sh                 # build/bridge/CivBridge
export DOTNET_ROOT=<your .NET 8 dir>    # only when .NET isn't installed system-wide
CIVBRIDGE_CMD=$PWD/build/bridge/CivBridge .venv/bin/python playtest/run.py --seed 1 --turns 60 --model sonnet
```

The run directory gets the transcript, the env's action log, the final summary against the
baselines, and the recording. See [playtest/README.md](playtest/README.md) for batches and gates.

**Through agent-env, with the Claude player agent.** The repository ships an A2A agent for the bundle's
tasks, `agents/claude-player`. It runs Claude Code against the env's tools, one session per prompt.

- **Setup:** `scripts/install.sh --agent` does all of it:
  - `agent-env openciv3 setup --agent` builds and registers the agent as `openciv3-claude`;
  - `.agentenv/config.toml` makes it the default agent and names the model endpoint;
  - the key lives in `~/.config/agentenv/secrets.yaml`, behind a `secret:` reference.
- **Credentials:** the key may be an Anthropic API key, a `claude setup-token` token or a LiteLLM key.
- **Running:** run agent-env from inside the checkout, so it finds that config:

```bash
agent-env run openciv3 --task play        # 50 turns on a Tiny map
agent-env run openciv3 --task full-game   # 540 turns at Civilization III's own settings, as six 90-turn sessions
agent-env run openciv3 --task three-agents  # Opus, Sonnet and Haiku play one 300-turn game against each other
```

In `three-agents` each agent plays its own civilization (the `X-OpenCiv3-Seat` header names it), all three play each
turn at the same time, and the turn advances once every agent has ended it.

The full game took 46 minutes and about $13 on Sonnet 5.5, and scored 0.86 with the full-game verifier. Any
other A2A agent that advertises `urn:agentenv:mcp-config/v1` works too: set it as the default, or pick it
per run with `agent-env task run ... --a2a-agent-id <agent>`.
[The bundle's README](src/agentenv_openciv3/bundles/openciv3/README.md) has the details.

**By hand.** Serve the env and connect Claude Code to it:

```bash
docker run --rm -p 127.0.0.1:18765:18765 mcp-server-openciv3
# or, with a local bridge: agent-env openciv3 serve

claude mcp add --transport http openciv3 http://127.0.0.1:18765/mcp
claude --allowedTools "mcp__openciv3__*" "Play OpenCiv3 with the openciv3 tools. Start with the get_turn_brief tool and keep going until GAME OVER."
```

## Tools

Fourteen tools. They return compact text, end every game action with a status footer such as
`[T23/60 · needs orders: u7, c1]`, and fail with the reason, the valid alternatives and, when one
would succeed, the call to make instead. Full contract: [docs/tools.md](docs/tools.md).

| Tool | What it does |
|---|---|
| `get_turn_brief` | Turn, gold, rates, research, score and pace, what needs orders, cities in disorder or at risk, undefended cities, capped production, the engine's pending picks, last turn's events, the plan |
| `list_units` | One line per unit: position, moves, status, valid orders, whether it can found a city here |
| `view_map` | ASCII map of explored tiles around a point, a unit or a city, plus notable things with distance and direction |
| `find_city_sites` | Ranked city sites with travel time and yields, and every legal site nearby |
| `unit_order` | `settle`, `found_city`, `goto`, `explore`, `auto_work`, `fortify`, worker jobs, `attack` (with the estimated chance to win) and `bombard` |
| `city_info` | Growth, production, mood and what each city can build |
| `set_production` | Choose what a city builds |
| `research` | List researchable techs, or set one (prerequisites are queued) |
| `set_rates` | Set the science and luxury rates; luxury is the main fix for disorder |
| `buy` | Rush a city's current production with gold |
| `revolution` | Change government, after a few turns of anarchy |
| `diplomacy` | The civilizations you know: war or peace, score, government, military against yours, the price of peace; declare war or propose peace |
| `end_turn` | End the turn, or several quiet ones until something needs attention; lists blockers instead when something needs orders |
| `plan` | Read or replace the agent's plan, which every brief shows back |

The engine still makes some choices itself: what a city builds next after finishing something, and
the next tech after one is learned. Those picks are reported and block the turn until the agent
changes or accepts them (`end_turn` with `skip_idle` accepts them), and the summary's `decisions`
counts who chose each completed item and learned tech.

## Scoring and baselines

Score = 10 × cities + 3 × citizens + 1 × owned tiles + 4 × known techs.

For the same seed and scenario the env plays three baselines in the background
(`OPENCIV_BASELINES=1`), and the playtest harness adds a fourth:

| Baseline | Plays | What it tells you |
|---|---|---|
| `null` | ends every turn with every unit holding | nothing: it never founds a city, so it is always 8 (the two starting techs) |
| `found_capital` (playtest only) | founds the capital, automates workers, holds the rest | the floor for one city |
| `settler_bot` | founds the capital; settles every settler at the top site `find_city_sites` gives; Warrior if a city has no defender, else Settler from size 2 (up to 8 cities), else Worker, else Warrior; cheapest tech | **the bar**: a no-LLM script that follows the env's own suggestions through the same tools and rules as the agent |
| `engine_ai` | OpenCiv3's AI plays the agent's seat | a weak reference: its settlers wait for escorts and it builds an army; it also trades techs, which the agent cannot |

A scripted settler bot is the meaningful bar. In round 1 a similar bot matched or beat Sonnet (see
Results), so beating `engine_ai` says little; an agent is doing something only when it beats
`settler_bot`.

The env's `data/get` summary carries the score, its components, the baselines at the same turn,
who made each production and research decision, the agent's valid and invalid calls, and what the
harness did (`autoplay_turns`, `new_games`, `extension_calls`, `engine_restarts`). The bundle's
verifier scores it by weighted average:

| Criterion | Weight |
|---|---|
| Reached the turn limit | 1 |
| Not defeated | 1 |
| Founded at least one city | 1 |
| Score as a fraction of `settler_bot`'s at the same turn | 2 |
| Gate: the engine did not fail | failing makes the grade 0 |
| Gate: in a game the agent played, the harness autoplayed none of its turns | failing makes the grade 0 |

A run reports `passed` only at 1.0: every check passes and the agent matches or beats `settler_bot`.

## Results

### Three agents in one game

`agent-env run openciv3 --task three-agents`: Opus 5.5 as Rome, Sonnet 5.5 as Greece and Haiku 4.5 as Egypt, each
the Claude player agent on its own model, in one game with no AI civilizations.
- **Setup:** seed 1, a Small map, Regent, roaming barbarians, 300 turns, four 75-turn sessions per agent.
- **Run:** passed with grade 1. The game reached T300 in 68 minutes, the engine never restarted, and every agent
  played every one of its turns (the env ended none for them).

| At T300 | Opus (Rome) | Haiku (Egypt) | Sonnet (Greece) |
|---|---|---|---|
| Rank and score | **1st, 1,546** | 2nd, 1,233 | 3rd, 1,082 |
| Cities, population | 45, 212 | 28, 163 | 29, 144 |
| Techs | 22 | 21 | **26** |
| Share of the land, of the population | 27.7%, 40.9% | **32.3%**, 31.4% | 20.5%, 27.8% |
| Government | Monarchy | Despotism | Monarchy |
| Production picks (agent / engine) | 285 / 1 | 51 / 248 | 146 / 36 |
| Tool calls (failed) | 1,282 (3.0%) | 545 (1.8%) | 805 (4.0%) |
| Cost | $20.95 | $4.64 | $6.58 |

- **Expansion decided it.** Opus reached 45 cities by T150, then stopped founding them; its score rose from 1,261
  to 1,546 over the last 150 turns. Haiku left most production picks to the engine but kept settling to the end,
  and passed Sonnet's score around T250.
- **No war between the agents.** All three stayed at peace with each other all game. Their nine attacks were all
  on barbarians.
- **The engine's upkeep rule bites.** When a civ's gold would go negative, the engine disbands random units to pay
  their support: Egypt lost 12 at once on T277. The env reports these as lost units without the reason yet.
- **Total cost** $32.16. The recording names each seat (`Rome (Opus) vs Greece (Sonnet) vs Egypt (Haiku)`).

### The full game as an agent-env task

`agent-env run openciv3 --task full-game`, on the patched engine (patches 0005-0009: AI science, governments
and peace).
- **Setup:** seed 1, a Standard map, 7 AIs, Regent, roaming barbarians, 540 turns.
- **Agent:** Sonnet 5.5 as the Claude player agent, in six 90-turn sessions.
- **Grade:** 0.86 with the full-game verifier.

| At T540 | Agent (Rome) | Zululand (2nd) | Arabia (3rd) | Built-in AI in Rome's seat | `settler_bot` in Rome's seat |
|---|---|---|---|---|---|
| Score | **1,221** | 1,148 | 1,122 | 1,172 | 227 |
| Cities | 33 | 19 | 23 | 22 | 5 |
| Techs (of 83) | 45 | 49 | 42 | 41 | 26 |

- **Rank:** first of 8, and every civ survived. With the engine fixes the AIs research, change government and
  make peace, so the margin is narrow.
- **World share:** 11.6% of the land and 17.6% of the population. Civ III's domination victory needs two
  thirds of each.
- **Government and combat:** the agent changed government three times (Monarchy, Republic, Democracy) and
  attacked once.
- **Play:** of its production, it chose 231 items and the engine 62. It made 1,353 tool calls, 3.8% of them
  invalid.
- **Cost:** about $13 and 46 minutes, including the recordings.

### A full game at Civilization III's own settings, before the engine fixes

On 2026-10-01 Sonnet 5.5 played a whole game: Standard map (100×100), 7 AI civs, Regent, roaming
barbarians, 540 turns (4000 BC to AD 2050), seed 1. The harness rotated sessions at a 100K context cap
(`playtest/run.py --context-cap`), and `playtest/longgame.py` wrote the report.

| At T540 | Agent (Rome) | Best AI civ (Arabia) | Built-in AI in Rome's seat | `settler_bot` in Rome's seat |
|---|---|---|---|---|
| Score | **4,106** | 1,149 | 667 | 453 |
| Cities | 120 | 24 | 12 | 9 |
| Population | 549 | 149 | 73 | 59 |
| Tiles | 1,123 | 330 | 188 | 86 |
| Techs (of 83) | 34 | 33 | 35 | 25 |

- **It led the game.** Rome ranked first of the 8 civs on 483 of 541 turns. It ended with 58% of the
  world's land and 62% of its population. Civ III's domination victory needs two-thirds of each; this
  engine has no victory conditions, so the game ran to the turn limit.
- **The AIs fought each other, and the agent filled the space.**
  - Four AI civs were destroyed in AI wars: Spain at T225, the Mongols at T372, Zululand at T385 and the
    Hittites at T490.
  - The agent founded 110 cities, many on their ruins.
  - Arabia declared war on Rome at T482 and razed 3 of its cities; the agent has no way to attack.
- **Weak spots:**
  - Techs are level with the AIs; no civ leaves the ancient era in this engine.
  - Cities starved 100 times, and 21 fell into disorder.
  - In a 120-city empire, the agent accepted the engine's pick for 58% of production items.
- **Cost:**
  - 37 minutes of play and $22.31 for 1,499 tool calls (2.8 a turn, 3.1% invalid), over 4 sessions.
  - No turn was autoplayed.
  - The real-client and map videos took another 17 minutes to render.

[docs/full-game.md](docs/full-game.md) covers what a full game still lacks.

### Round 2: graded against `settler_bot`

Gate v2: beat `settler_bot` on the same seed in at least 2 of 3 runs, with fewer than 10% invalid calls
and no autoplay. Score per seed (agent / `settler_bot` / `engine_ai`):

| Batch | Seed 1 or 4 | Seed 2 or 5 | Seed 3 or 6 | Gate v2 | $ per game |
|---|---|---|---|---|---|
| Sonnet, Tiny, 3 rivals, 60 turns | 179 / 156 / 134 | 163 / 125 / 141 | 166 / 131 / 155 | pass, 3 of 3 | 0.29–0.35 |
| Haiku, Tiny, 3 rivals, 60 turns | 195 / 156 / 134 | 137 / 125 / 141 | 179 / 131 / 155 | pass, 3 of 3 | 0.25–0.27 |
| Sonnet, Small, 5 rivals, 100 turns | 295 / 275 / 240 | 318 / 271 / 201 | 371 / 291 / 294 | pass, 3 of 3 | 1.08–1.30 |

### Round 1

Claude Code through the playtest harness, one game per seed, same-seed baselines. Tiny map,
3 rivals, 60 turns (seeds 1-3); Small map, 5 rivals, roaming barbarians, 100 turns (seeds 4-6).
Score (cities):

| Seed | Sonnet | Haiku | `null` | `found_capital` | scripted settler bot | `engine_ai` |
|---|---|---|---|---|---|---|
| 1 | 168 (5) | 71 (1) | 8 | 58 | 164 (5) | 134 (3) |
| 2 | 161 (6) | 57 (1) | 8 | 54 | 171 (6) | 141 (4) |
| 3 | 176 (5) | 64 (1) | 8 | 62 | 159 (5) | 155 (4) |
| 4 | 341 (12) | – | 8 | 81 | 366 (12) | 240 (7) |
| 5 | 344 (11) | – | 8 | 74 | 375 (12) | 201 (5) |
| 6 | 255 (7) | – | 8 | 78 | 384 (13) | 294 (9) |

What the audit of these games found:
- **A scripted bot matched Sonnet.** The bot (from the audit, the forerunner of `settler_bot`) beat
  Sonnet on 4 of 6 seeds and `engine_ai` on 6 of 6. Sonnet's wins over `engine_ai` came mostly from
  the env's unescorted settling and the score formula, not from better play.
- **The engine made many of the agent's decisions.** It chose 55% of the production items Sonnet's
  cities completed, nearly all research after the first pick, and every worker and explorer move.
- **Haiku's one-city games were an env trap.** Every Haiku capital rioted at size 3, because an
  empty city riots at the size a Settler needs, and nothing told the agent how to stop it.
- Sonnet cost $0.15-0.16 per 60-turn game and $0.39-0.50 per 100-turn game, with 2-7% failed calls;
  10 of its 17 failures were harness artefacts (bare tool names copied from the prompt) or a
  since-fixed name mismatch.

Round 2 adds the levers, signals and baselines these findings called for (`set_rates`, `buy`, riot
warnings and fixes, reported engine picks, `settler_bot`), and grades against `settler_bot`.

## Recordings

The bridge writes a snapshot of the whole world after every turn. The env renders the game so far
on request through its `urn:openciv3:recording/v1` extension: an mp4 (a gif when `ffmpeg` is
missing; the image has it) with the map, the scoreboard and the agent's actions for each turn, and a
self-contained HTML replay with a turn slider. See [docs/recording.md](docs/recording.md).

- **In agent-env:** every task in the bundle ends with the `save_env_recording` step, in parallel
  with grading. It stores each file as a `file` artifact named
  `<task id>-recording-<instance id>.<suffix>`. `agent-env openciv3 recordings` lists them, and
  `agent-env openciv3 recordings <instance id> --out <dir>` copies one run's files out (`agent-env
  run` prints the instance id). The step works with any env that advertises an extension of that
  shape, and a failed recording never stops grading.
- **In the playtest harness:** every run directory gets `recording.mp4` and `replay.html`, and
  `playtest/replay.py` rebuilds them for older runs.
- **The real game's view:** `agent-env openciv3 setup --client` builds the image with the OpenCiv3 client
  (Godot, rendering on the CPU, no GPU or display). Recordings then also include `client.mp4`, one frame per
  turn of the real client with its art. It adds about a minute per 60 turns and about 350 MB. The art carries
  no licence, so keep that image and its videos private; see [docs/recording.md](docs/recording.md#5-the-real-clients-view-client_mp4).
- `OPENCIV_RECORD=0` turns recording off.

## How it works

```
LLM agent ──MCP (streamable HTTP, :18765/mcp)──▶ agentenv_openciv3.server   (Python, AgentEnv SDK)
harness ───data plane /agentenv, extensions──────▶   │  JSON lines on stdin/stdout
                                                      ▼
                                                   CivBridge                 (.NET 8)
                                                      │
                                                      ▼
                                                   C7Engine                  (OpenCiv3, patched copy)
```

- **CivBridge** (`bridge/`) runs one OpenCiv3 game headlessly and answers JSON-lines commands
  (`new_game`, `state`, `unit_order`, `end_turn`, `autoplay`, `world`, `load`, ...). It autosaves
  every turn, so the env restarts a crashed bridge from the start of the current turn. Protocol:
  [docs/protocol.md](docs/protocol.md).
- **The env** (`src/agentenv_openciv3/`) is an `AgentEnvEnvironment`. It renders the bridge's facts
  into text for the agent, keeps the plan and the action log, plays the baselines, renders
  recordings, and exposes the data plane (`data/reset`, `data/add`, `data/get`) and three harness
  extensions: `urn:openciv3:new-game/v1`, `urn:openciv3:autoplay/v1` and
  `urn:openciv3:recording/v1`. It is configured through environment variables (`OPENCIV_SEED`,
  `OPENCIV_TURN_LIMIT`, `OPENCIV_SIZE`, `OPENCIV_ACTION_LOG`, ...); see
  [docs/tools.md](docs/tools.md).
- **The plugin** adds `agent-env openciv3 setup`, `serve` and `recordings`, the task step
  `save_env_recording` (`src/agentenv_openciv3/steps.py`), and the bundle `openciv3`
  (`src/agentenv_openciv3/bundles/openciv3/`) with the `smoke` and `play` tasks and the outcome
  verifier.
- **The image** holds the bridge published self-contained for `linux/amd64` or `linux/arm64`
  (cross-compiled on the build host), the env, and a static `ffmpeg`.

## Development

```bash
git submodule update --init vendor/OpenCiv3   # pinned; not --recursive
uv venv && uv pip install -e '.[dev]'
scripts/build-bridge.sh                       # build/engine (patched copy), then build/bridge/CivBridge
export DOTNET_ROOT=<your .NET 8 dir>          # only when .NET isn't installed system-wide
export CIVBRIDGE_CMD=$PWD/build/bridge/CivBridge
.venv/bin/pytest                              # tests/ (bridge, env, packaging) and the playtest harness
.venv/bin/ruff check .
docker build -t mcp-server-openciv3 .         # the env image, for this machine's platform
```

- The local bridge is framework-dependent, so it needs a .NET 8 runtime; `agent-env openciv3 serve`
  checks that it starts and says when `DOTNET_ROOT` is missing.
- `tests/env` drives the env against a fake bridge, `tests/bridge` the real one, and
  `tests/packaging` the CLI, the `save_env_recording` step (against a fake env and agent-env's
  local stores) and the verifier.
- CI (`.github/workflows/ci.yml`) lints, runs every test on Python 3.11 and 3.12 against a freshly
  built bridge, then runs the quickstart: `agent-env openciv3 setup`, the `smoke` task, and a check
  that the recording was saved (uploaded as a workflow artifact).

## Licence and credits

This repository is licensed under the Apache License 2.0 ([LICENSE](LICENSE), [NOTICE](NOTICE)).

[OpenCiv3](https://github.com/C7-Game/OpenCiv3) is MIT-licensed, by the OpenCiv3 (C7) contributors.
It is referenced as a git submodule and never modified there. The engine in the bridge and the image
is built from a copy of it with these patches applied (`patches/`):

1. `0001-defeated-controller-ends-turn-loop`: hand control back once the human player is defeated,
   instead of looping forever.
2. `0002-declare-war-uses-game-rng`: draw war declarations from the game's seeded RNG, so games stay
   deterministic.
3. `0003-budget-never-throws`: when the budget can't be balanced, keep gold at 0 instead of throwing
   mid-turn.
4. `0004-ai-turn-exceptions-are-contained`: an exception in one AI player's turn ends that player's
   turn instead of aborting or stalling the game.
5. `0005-building-prerequisites-check-the-city`: buildings that need another building (Bank,
   University, Cathedral, ...) become buildable.
6. `0006-small-wonders-are-buildable`: small wonders can be built, once per civ.
7. `0007-ai-keeps-its-science-funded`: the AI keeps its science funded, stops building units it cannot
   support, and changes government.
8. `0008-research-cost-follows-the-difficulty`: harder difficulties make AI research cheaper, as they
   make its production cheaper, instead of dearer.
9. `0009-ai-makes-peace`: an AI asks a price for peace, makes peace with other AIs and offers it to the
   player, and remembers broken treaties.

The image also contains Blast (Apache-2.0), Serilog (Apache-2.0), MoonSharp (BSD-3-Clause),
ini-parser (MIT), the .NET runtime (MIT) and a static FFmpeg build (GPL-3.0-or-later); the image
carries their licences in `/opt/civbridge/licenses/`. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

The optional client image (`--target client`) adds the OpenCiv3 client (MIT), Godot (MIT), the .NET
runtime, Xvfb and Mesa, and OpenCiv3's community art from
[C7-Game/Assets](https://github.com/C7-Game/Assets). That art carries no licence: it is fetched when the
image is built, never stored in this repository, and the image and its videos are for private use.

Civilization and Civilization III are trademarks of Take-Two Interactive Software. This project is
not affiliated with or endorsed by Take-Two, Firaxis Games or the OpenCiv3 project, and it uses no
Civilization III game files.
