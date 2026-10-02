# Contributing

Thanks for helping. New tasks, tools, player agents, engine fixes, docs and bug reports are all welcome.

## How a change gets in

1. **Talk first about anything large.** Open an issue for a new tool, task, agent or engine change before you write
   it, so we agree on the shape. Small fixes can go straight to a pull request.
2. **Fork, branch and open a pull request against `main`.** Fill in the template: what changes, why, and how you
   tested it.
3. **CI must pass:** lint, the tests on Python 3.11 and 3.12, and the image builds that play the `smoke` task with and
   without the real client.
4. **The maintainer reviews and approves every pull request.** `main` is protected: a pull request merges only
   with an approving review from the code owner ([.github/CODEOWNERS](.github/CODEOWNERS)), green checks and its
   conversations resolved. Pull requests are squash-merged, so the title becomes the commit message.

First-time contributors' CI runs wait for the maintainer's approval, as GitHub does for public repositories.

## Setup

You need git, [uv](https://docs.astral.sh/uv/), the [.NET 8 SDK](https://dotnet.microsoft.com/download/dotnet/8.0)
(for the bridge) and Docker with buildx (for the images).

```bash
git clone https://github.com/<you>/agentenv-openciv-plugin && cd agentenv-openciv-plugin
git submodule update --init vendor/OpenCiv3   # pinned; not --recursive
uv venv && uv pip install -e '.[dev]'
scripts/build-bridge.sh                       # build/engine (patched copy), then build/bridge/CivBridge
export DOTNET_ROOT=<your .NET 8 dir>          # only when .NET isn't installed system-wide
export CIVBRIDGE_CMD=$PWD/build/bridge/CivBridge
.venv/bin/pytest && .venv/bin/ruff check .
```

To try a change end to end, run `scripts/install.sh --run three-agents-quick` from your checkout: it installs the
checkout into the `agent-env` tool, registers the env and the player agents built from it, and plays the match (see
the [README](README.md#run-it-yourself) for the model key). After that, `agent-env openciv3 setup --agent` rebuilds
them from your changes.

## Where things go

| Change | Code | Tests and docs |
|---|---|---|
| A tool, or what a tool says | `src/agentenv_openciv3/server.py`, `render.py` | `tests/env/test_tools.py`, `test_render.py` (against the fake bridge); [docs/tools.md](docs/tools.md) and the README's tool table |
| A bridge command or game rule | `bridge/CivBridge/` | `tests/bridge/test_protocol.py` (against the real bridge); [docs/protocol.md](docs/protocol.md), and `tests/env/fake_bridge.py` if the env uses it |
| An engine fix | a new numbered patch in `patches/`: a description of what it fixes and why, then the diff. `scripts/prepare-engine.sh` applies the patches in order to a copy of the engine; never edit `vendor/OpenCiv3` | a bridge test that fails without it; the patch list in the README's credits |
| A task | `src/agentenv_openciv3/bundles/openciv3/tasks/` | `tests/packaging/test_steps.py`; the bundle's README and the README's task table |
| A task step or verifier | `src/agentenv_openciv3/steps.py`, `bundles/openciv3/artifacts/` | `tests/packaging/test_steps.py`, `test_verifier.py` |
| A player agent | `agents/<cli>-player/` (an `agent.py` on `agents/common/openciv3_player.py`, and a Dockerfile built from `agents/`), registered in `PLAYERS` in `src/agentenv_openciv3/cli.py` | `tests/agent/`, with a fake of the CLI like `fake_codex.py` |
| Recordings or the live view | `src/agentenv_openciv3/recording.py`, `live.py`, `client.py` | `tests/env/test_http.py`, `test_recording.py`; [docs/recording.md](docs/recording.md) |

## Conventions

- **Code:** match the code around it. `ruff check .` must pass (line length 120). Name things so the code reads
  without comments; write a short docstring or comment only for why something is the way it is.
- **Tools speak to models.** Keep their text compact, put facts before advice, and make every failure say why, list
  the valid choices and, when one would work, the exact call to make instead.
- **Tests:** a fix comes with a test that fails without it. `tests/env` runs against the fake bridge, so a tool test
  needs no .NET; `tests/bridge` runs the real bridge.
- **Determinism:** the same seed and the same actions must give the same game. Draw randomness from the game's
  seeded RNG.
- **No secrets or private endpoints** in code, tests, docs or task files: model keys come from agent-env's secret
  store, and examples use placeholders such as `https://your-litellm-proxy`.
- **Licensing:** contributions are licensed under the Apache License 2.0, like the rest of the repository. Don't add
  code or assets whose licence doesn't allow that; OpenCiv3's community art stays out of the repository and out of
  public images.

## Reporting bugs and security issues

Open an issue with the task or command, what you expected, what happened, and the run's instance id or log. For a
security issue, use GitHub's private vulnerability reporting instead ([SECURITY.md](SECURITY.md)).
