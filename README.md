# agentenv-openciv3

[OpenCiv3](https://github.com/C7-Game/OpenCiv3), the open-source Civilization III remake, as an
[AgentEnv](https://github.com/scaleapi/agentenv-framework) environment. An LLM agent plays a seeded
4X game through ten MCP tools, and the game is graded against the same seed played by OpenCiv3's
own AI and by a do-nothing player.

<!-- Recording of an agent's game: docs/demo.gif (not recorded yet). -->

The game runs headless: no Godot, no display and no Civilization III files. One env serves one game
at a time, and a game is deterministic: the same seed gives the same map, rivals and AI moves.

## Quickstart

You need Docker, [uv](https://docs.astral.sh/uv/) and git.

```bash
git clone https://github.com/earakely-scale/agentenv-openciv-plugin
git -C agentenv-openciv-plugin submodule update --init    # vendor/OpenCiv3 (not recursive: its art isn't needed)
uv tool install agentenv-framework --with-editable ./agentenv-openciv-plugin

agent-env openciv3 setup              # build the image for this machine, register the env "openciv3"
agent-env run openciv3 --task smoke   # OpenCiv3's AI plays 30 turns, the verifier grades the game
```

`setup` builds the Docker image for the Docker host's own platform (`linux/arm64` on Apple Silicon)
and registers it as the MCP server env `openciv3`. Pass `--platform linux/amd64` for remote
sandboxes, `--source <checkout>` when the plugin is not installed from a checkout, and
`--image <tag>` to register an image you already have. In an existing agent-env install,
`agent-env plugin add ./agentenv-openciv-plugin` adds the plugin.

The `smoke` task needs no model. The `play` task has an LLM agent play 50 turns:

```bash
export LITELLM_BASE_URL=... LITELLM_API_KEY=...     # an OpenAI-compatible endpoint
agent-env run openciv3 --task play --model <model>
```

It deploys the A2A agent registered as `openciv3-player`. This repo does not ship one yet: register
any agent built with the AgentEnv A2A framework that advertises the MCP config extension, for
example `agent-env a2a-agent put --id openciv3-player --dockerfile <agent>/Dockerfile --platform linux/arm64`.

### Play it yourself with Claude Code

Serve the env on your machine, from the image that `setup` built or from a local bridge build:

```bash
docker run --rm -p 127.0.0.1:18765:18765 mcp-server-openciv3
# or, with the .NET 8 SDK: scripts/build-bridge.sh && agent-env openciv3 serve

claude mcp add --transport http openciv3 http://127.0.0.1:18765/mcp
claude "Play OpenCiv3 with the openciv3 tools. Start with get_turn_brief and keep going until GAME OVER."
```

## Tools

The agent gets ten tools. They return compact text, end every game action with a status footer
such as `[T23/60 · needs orders: u7, c1]`, and fail with the reason, the valid alternatives and the
exact call to make instead. Full contract: [docs/tools.md](docs/tools.md).

| Tool | What it does |
|---|---|
| `get_turn_brief` | Turn, gold, research, score, pace against both baselines, what needs orders, cities, last turn's events, the agent's plan |
| `list_units` | One line per unit: position, moves, status, valid orders, whether it can found a city here |
| `view_map` | ASCII map of explored tiles around a point, a unit or a city, plus notable things with distance and direction |
| `find_city_sites` | Ranked city sites with travel time and yields |
| `unit_order` | `settle`, `found_city`, `goto`, `explore`, `auto_work`, `fortify`, worker jobs, and more |
| `city_info` | Growth, production and what each city can build |
| `set_production` | Choose what a city builds |
| `research` | List researchable techs, or set one (prerequisites are queued) |
| `end_turn` | End the turn, or several quiet ones; lists blockers instead when something needs orders |
| `plan` | Read or replace the agent's plan, which every brief shows back |

The server also guards against getting stuck: a call that fails three times in a turn gets a list of
alternatives, and after 25 calls in one turn every response suggests ending it.

## Scoring and baselines

Score = 10 × cities + 3 × citizens + 1 × owned tiles + 4 × known techs.

For the same seed and scenario the env plays two baselines in the background (`OPENCIV_BASELINES=1`):

- `null`: every turn is ended with every unit holding; research is picked automatically.
- `engine_ai`: OpenCiv3's own AI plays the agent's seat.

The env's `data/get` summary carries the score, its components, the process metrics (valid and
invalid calls, the longest error streak) and both baselines at the same turn. The bundle's verifier
reads it and scores five criteria, averaged by weight:

| Criterion | Weight | Score |
|---|---|---|
| Reached the turn limit | 1 | pass or fail |
| Not defeated | 1 | pass or fail |
| Founded at least one city | 1 | pass or fail |
| Beats the `null` baseline at the same turn | 1 | pass or fail |
| Score as a fraction of the `engine_ai` baseline at the same turn | 2 | 0 to 1 |

A run reports PASSED only at 1.0, that is when the agent passes every check and matches or beats
OpenCiv3's AI.

## How it works

```
LLM agent ──MCP (streamable HTTP, :18765/mcp)──▶ agentenv_openciv3.server   (Python, AgentEnv SDK)
harness ───data plane /agentenv, extensions──────▶   │  JSON lines on stdin/stdout
                                                      ▼
                                                   CivBridge                 (.NET 8, self-contained)
                                                      │
                                                      ▼
                                                   C7Engine                  (OpenCiv3, patched copy)
```

- **CivBridge** (`bridge/`) runs one OpenCiv3 game headlessly and answers JSON-lines commands
  (`new_game`, `state`, `map`, `unit_order`, `end_turn`, `autoplay`, `score`, ...). Protocol:
  [docs/protocol.md](docs/protocol.md).
- **The env** (`src/agentenv_openciv3/`) is an `AgentEnvEnvironment`. It renders the bridge's facts
  into text for the agent, keeps the plan and the action log, computes the baselines, and exposes
  the data plane (`data/reset`, `data/add`, `data/get`) and two harness extensions:
  `urn:openciv3:new-game/v1` and `urn:openciv3:autoplay/v1`.
- **The plugin** adds `agent-env openciv3 setup` and `agent-env openciv3 serve`, and the bundle
  `openciv3` (`src/agentenv_openciv3/bundles/openciv3/`) with the `smoke` and `play` tasks and the
  outcome verifier. Tasks set up games through `apply_server_config`, so no custom task steps are
  needed.

The env is configured through environment variables (`OPENCIV_SEED`, `OPENCIV_TURN_LIMIT`,
`OPENCIV_SIZE`, `OPENCIV_ACTION_LOG`, ...); see [docs/tools.md](docs/tools.md#configuration-environment-variables).

## Development

```bash
git submodule update --init                          # vendor/OpenCiv3, pinned; not --recursive
scripts/build-bridge.sh                              # build/engine (patched copy), then build/bridge/CivBridge
uv run --extra dev pytest tests/bridge tests/env     # tests/env drives the env against a fake bridge
docker build -t openciv3 .                           # the env image (host platform)
CIVBRIDGE_CMD=$PWD/build/bridge/CivBridge uv run playtest/run.py --seed 1   # claude -p plays a game
```

- `vendor/OpenCiv3` is the upstream submodule and is never edited. `patches/*.patch` are applied
  to a copy under `build/engine/` at build time: one stops the engine looping forever once the
  human player is defeated, the other keeps war declarations on the game's seeded RNG.
- The Dockerfile cross-compiles the bridge on the build host for the target architecture
  (`linux/amd64` or `linux/arm64`), then installs it with the env into `python:3.12-slim`.
- `playtest/` has Claude Code play real games and measures score, baselines and stuck signals;
  see [playtest/README.md](playtest/README.md).
- CI (`.github/workflows/ci.yml`) builds the bridge, runs both test suites and starts a game in the
  Docker image.

## License and credits

This repository is licensed under the Apache License 2.0 ([LICENSE](LICENSE)).

[OpenCiv3](https://github.com/C7-Game/OpenCiv3) is MIT-licensed, by the OpenCiv3 (C7) contributors.
It is included unmodified as a git submodule; the Docker image contains the engine built from it.

Civilization and Civilization III are trademarks of Take-Two Interactive Software. This project is
not affiliated with or endorsed by Take-Two, Firaxis Games or the OpenCiv3 project, and it uses no
Civilization III game files.
