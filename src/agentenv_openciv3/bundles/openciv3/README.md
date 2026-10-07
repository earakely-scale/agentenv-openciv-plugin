OpenCiv3: play a seeded Civilization III-style game through MCP tools, alone against the AI or against other agents, grade it, and save a recording.

Every task deploys the env registered as `openciv3` (run `agent-env openciv3 setup` once), start a
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
- `full-game` is the full game at Civilization III's own settings: seed 1, a Standard map, 7 AIs, Regent,
  roaming barbarians and 540 turns.
  - **Sessions:** it is played as six `prompt_agent` sessions of 90 turns. Each is a fresh conversation
    that starts from the brief and the plan the env keeps, so no session's context outgrows the game.
  - **Order:** a session that stops early or times out does not fail the task; the next one takes the
    game from wherever it is. Grading and the recording follow the last session.
  - **Time:** allow an hour or two.

- `three-agents` is a match: Opus as Rome, Sonnet as Greece and Haiku as Egypt play one game against each other,
  on a Small map at Regent with roaming barbarians and no AI civilizations, for 300 turns (seed 1).
  `three-agents-quick` is the same match in 10 turns (a few minutes, well under a dollar), to check a setup first.
  Both are written so you can change who plays and how:
  - **Agents:** each `deploy_agent` step is a player. Add one or remove one to change how many agents play; the
    step's `agent_name` is how the game knows the agent.
  - **The game:** the `openciv3_match` step starts it once the agents are deployed, with one seat per deployed
    agent. `turns` is the turn limit; `civs` says which civilization each agent plays (the others get the next free
    one); `ai_opponents` adds AI civilizations; `seed`, `size`, `difficulty` and `barbarians` set the map.
  - **Prompts:** each agent has one `prompt_agent` step with its own prompt and model. The tasks give all three the
    same prompt; edit one to give that agent its own strategy. The brief states the match's rules, so a prompt
    need not.
  - **Victor:** `artifacts/victor-verifier/verify.py` names the winner. A conquest (one agent's civilization is
    the last an agent still plays), a domination (one holds two thirds of the world's land and population) or a
    cultural victory (a city with 20,000 culture points, or 100,000 in all and twice the next civilization's) ends
    the game early and wins it; otherwise the top score at the turn limit wins, and a tie has no victor. The grade
    is 1 for a valid match with one victor, 0.5 for a tie, and 0 when the match was not played to its end, the
    engine failed, or the env ended more than a tenth of any agent's turns. Each agent's rank, score, metrics and
    share are reported in rows of weight 0.
  - **Watch it live:** while it plays, `agent-env openciv3 watch` prints the URL of the env's live view (`--open`
    opens it): the map, the scores, each agent's actions and the turn's events, and the real client's view when the
    image has it.
  - **Models:** the ids are LiteLLM-style (`anthropic/claude-opus-5-5`, …); the player agents also take them on
    their providers' own APIs.
- `frontier` is the same kind of match with nine models, each its own agent and civilization, on a Standard map for
  200 turns: Opus 5.5 (Rome), Sonnet 5.5 (Greece), Haiku 4.5 (Egypt), GPT-5.6 Sol (America), Luna (Babylon) and
  Terra (Persia), Gemini 3.1 Pro (England), Grok 4.7 (Carthage) and Kimi K3 (China). Each `deploy_agent` step
  names the player agent for its model with `a2a_agent_id`: Codex plays the GPT models, Gemini CLI plays Gemini,
  and Claude Code plays the rest (Grok and Kimi through a LiteLLM proxy). Each agent starts a fresh session every
  40 turns. `frontier-quick` is the same match in 10 turns.
- `showmatch` and `livestream` are matches made to be streamed: Opus 5.5 (Claude Code, Rome), GPT-6 Sol (Codex,
  America) and Kimi K3 (Claude Code, China), told that the game is broadcast, to talk to each other with the
  `message` tool and to leave a note for the audience with every `end_turn`. `showmatch` plays 50 turns on a Tiny map
  in about 20 minutes; `livestream` plays 140 turns on a Small map in about 2 hours, each agent starting a fresh
  session every 40 turns. The `openciv3_match` step paces them (`min_turn_seconds`: 15 and 45, so viewers can follow
  every turn) and sets the `broadcast`: the stream's title and two AI casters, Max and Ada, whose lines Sonnet 5.5
  writes and `gpt-4o-mini-tts` voices. `agent-env openciv3 stream` follows that setting; a task without `broadcast`
  streams with no casters. `showmatch-quick` is the showmatch in 10 turns.
- `sol-vs-opus` is a duel for a broadcast: GPT-6 Sol (Codex, America) against Opus 5.5 (Claude Code, Rome) on a Tiny
  map with 3 AI civilizations, for at most 750 turns at a 30 s pace (several hours, unless a conquest ends it). Their prompt says that conquering the other
  model wins (the game ends when one model's civilization is the last a model plays) and that the higher score at
  the turn limit only breaks a tie.
- `astra-opus-kimi` is a three-way war for a long broadcast: GPT-6 Astra (Codex, America), Opus 5.5 (Claude Code,
  Rome) and Kimi K3 (Claude Code, China) on a Tiny map with 3 AI civilizations at Monarch and restless barbarians,
  for 300 turns at a 55 s pace (4 h 35 min, unless a conquest ends it sooner), each agent starting a fresh session
  every 30 turns. Its prompt is `sol-vs-opus`'s for three: conquering the other two models wins, and the score at
  the turn limit only breaks a tie. On a Small map three agents kept the peace for 75 turns; on a Tiny one, Opus
  went to war at turn 56.
- `human-vs-ai` and `human-vs-agents` seat you: you play Rome in the browser, on a Small map for 100 turns, against
  3 AI civilizations (`human-vs-ai`) or against Opus (Claude Code, Greece) and GPT-5.6 Sol (Codex, America)
  (`human-vs-agents`). The `openciv3_match` step's `humans` names each person and their civ, and the step logs one
  play link per person, `PLAY Rome (you): http://127.0.0.1:<port>/play#token=...`; `agent-env openciv3 play --open`
  finds it again. `human_turn_seconds` (900) is how long a human seat may sit idle while the agents wait before the
  env ends its turn. Since no `prompt_agent` step plays your seat, `openciv3_await_game` waits for the game to end
  (polling `data/get`, logging the turn) and grading and the recording depend on it. `human-vs-ai` is graded by the
  full-game verifier (your score against the engine's own AI in your seat, your rank, your share of the world);
  `human-vs-agents` by the victor verifier, which ranks you like an agent but doesn't hold your idle turns against
  the match. See [docs/play.md](https://github.com/earakely-scale/agentenv-openciv-plugin/blob/main/docs/play.md).

**The player agents.** The repository ships three A2A agents for these tasks, one per coding-agent CLI, over one
game loop (`agents/common`):

| Agent | Directory | CLI | Models |
|---|---|---|---|
| `openciv3-claude` | `agents/claude-player` | Claude Code | Anthropic's, and others' behind a LiteLLM proxy |
| `openciv3-codex` | `agents/codex-player` | Codex | OpenAI's, through LiteLLM's OpenAI route or OpenAI's API |
| `openciv3-gemini` | `agents/gemini-player` | Gemini CLI | Google's, through LiteLLM's Gemini route or Google's API |

- Each plays through the env's MCP tools only: the CLI's own tools (shell, files, web, subagents) are off.
- A prompt that names a stop turn ("until turn 180", as in `full-game`) is one session that plays to that turn or
  GAME OVER, nudging as the playtest harness does.
- Any other prompt plays the whole game: the agent starts a fresh session every 75 turns
  (`OPENCIV3_SESSION_TURNS` in the deploy step's `env_vars`; 0 for one session), and each new session resumes from
  the brief and the plan.
- In a game with seats it names its seat by its `agent_name` (or `OPENCIV3_SEAT` from its `env_vars`).
- `agent-env openciv3 setup --agent` builds and registers all three. A `deploy_agent` step picks one with
  `a2a_agent_id`; a step without one deploys the default agent.
- Then set, in `.agentenv/config.toml` (a LiteLLM key serves all three agents; Anthropic's own key only the first):

  ```toml
  [agents]
  default_a2a_agent_id = "openciv3-claude"

  [model]
  base_url = "https://api.anthropic.com"   # or a LiteLLM endpoint
  api_key = "secret:OPENCIV3_MODEL_KEY"     # an Anthropic API key, a LiteLLM key, or a `claude setup-token` token
  ```

`artifacts/outcome-verifier/verify.py` reads the env's `data/get` summary, waits for the baselines
to reach the game's turn, and scores `smoke` and `play`, by weighted average:

| Criterion | Weight |
|---|---|
| reached the turn limit | 1 |
| not defeated | 1 |
| founded at least one city | 1 |
| score as a fraction of `settler_bot`'s at the same turn (it also reports `null` and `engine_ai`) | 2 |
| gate: the engine did not fail | a failure makes the grade 0 |
| gate: in a game the agent played, the harness autoplayed none of its turns | a failure makes the grade 0 |

An env that cannot report a game is a grade of 0, not a crashed step.

`artifacts/full-game-verifier/verify.py` scores `full-game` against the engine's own AI in the agent's seat:

| Criterion | Weight |
|---|---|
| reached the turn limit | 1 |
| not defeated | 1 |
| score as a fraction of `engine_ai`'s at the same turn | 2 |
| rank among the civilizations, as a fraction from last (0) to first (1) | 1 |
| the smaller of the land and population shares, as a fraction of Civ III's domination bar (two thirds of each) | 1 |
| the same two gates | a failure makes the grade 0 |

`save_env_recording` stores each file the env's `urn:openciv3:recording/v1` extension returns (an
mp4 and an HTML replay) as a `file` artifact named `<task id>-recording-<instance id>.<ext>`, records
them in `context.metadata["recordings"]`, and logs where each file is. A failed recording never
stops grading.

Run with `agent-env run openciv3 --task smoke` or `agent-env run openciv3 --task play --model <model>`.
