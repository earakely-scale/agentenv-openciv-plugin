OpenCiv3: play a seeded Civilization III-style game through MCP tools, grade it against same-seed baselines, and save a recording.

Both tasks deploy the env registered as `openciv3` (run `agent-env openciv3 setup` once), start a
seeded game through the env's `urn:openciv3:new-game/v1` extension, and end with two steps that run
in parallel: the outcome verifier and `save_env_recording`.

- `smoke` needs no model and no agent. The scripted `settler_bot` plays your seat for 30 turns
  through `urn:openciv3:autoplay/v1`; the verifier grades the game (1.0, since it plays exactly like
  the reference baseline) and the recording is saved. It checks the image, the bridge, the
  extensions, the verifier and the recorder end to end.
- `play` has an LLM agent play 50 turns. Its `deploy_agent` step names no agent, so agent-env
  deploys your configured default: `[agents] default_a2a_agent_id` in `.agentenv/config.toml`
  (built-in default `a2a-default`). The agent must advertise the MCP config extension
  (`urn:agentenv:mcp-config/v1`) so it is handed the env's MCP server.
  - Pick the agent per run with `agent-env task run --id <task id> --a2a-agent-id <agent>`, or set
    the default in `config.toml`, e.g. `default_a2a_agent_id = "claude-code-cli"` where that agent
    is registered. `agent-env run` has no agent flag yet, so a bundle run uses the default.
  - A plain open-source install registers no agent, so the default `a2a-default` doesn't exist and
    the task stops at `deploy_agent` until you register one (`agent-env a2a-agent put ...`, with
    `--platform linux/arm64` to run it locally on Apple Silicon).
  - The agent needs a model endpoint: `LITELLM_BASE_URL` and `LITELLM_API_KEY` (any
    OpenAI-compatible endpoint), and `--model <model>`.

  To have an LLM play without an A2A agent, use the playtest harness in the repository
  (`playtest/run.py`, Claude Code).

`artifacts/outcome-verifier/verify.py` reads the env's `data/get` summary, waits for the baselines
to reach the game's turn, and scores, by weighted average:

| Criterion | Weight |
|---|---|
| reached the turn limit | 1 |
| not defeated | 1 |
| founded at least one city | 1 |
| score as a fraction of `settler_bot`'s at the same turn (it also reports `null` and `engine_ai`) | 2 |
| gate: the engine did not fail | a failure makes the grade 0 |
| gate: in a game the agent played, the harness autoplayed none of its turns | a failure makes the grade 0 |

An env that cannot report a game is a grade of 0, not a crashed step.

`save_env_recording` stores each file the env's `urn:openciv3:recording/v1` extension returns (an
mp4 and an HTML replay) as a `file` artifact named `<task id>-recording-<instance id>.<ext>`, records
them in `context.metadata["recordings"]`, and logs where each file is. A failed recording never
stops grading.

Run with `agent-env run openciv3 --task smoke` or `agent-env run openciv3 --task play --model <model>`.
