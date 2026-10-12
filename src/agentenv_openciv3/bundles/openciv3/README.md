OpenCiv3: play a seeded Civilization III-style game through MCP tools, alone against the AI or against other agents, grade it, and save a recording.

Every task deploys the env registered as `openciv3` (run `agent-env openciv3 setup` once) and starts a seeded game.
A game of one agent (`smoke`, `play`, `full-game`) is the env's default game, started through its
`urn:openciv3:new-game/v1` extension, and ends with two steps that run in parallel: its verifier and
`save_env_recording`. A match of several players, or a person's, comes from the env's lobby (agentenv-game-env's
`open_lobby`, an `add_player_slot` per player and `close_lobby`), is played out by `finish_match`, and ends with
`grade_match` and `save_match_files`, in parallel.

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
  - **Agents:** each agent is a `deploy_agent` step (with `"env_ids": []`) and an `add_player_slot` step whose
    `player_id` and `player_name` are its `agent_name`. Add or remove both to change how many agents play, and list
    the slots in `close_lobby`'s `depends_on`; `add_player_slot` hands each agent its slot's address,
    `/players/<player_id>/mcp`, so its calls play its civilization.
  - **The game:** `open_lobby`'s `game_settings` set it: `turn_limit`, and `seed`, `size`, `difficulty` and
    `barbarians` for the map (seed 1, Small, Regent and Roaming when left out). Each slot's `game_settings.civ` says
    which civilization it plays (the next free one when it names none); a slot `{"player_kind": "ai"}` adds an AI
    civilization, whose civ the engine picks. `close_lobby` starts the game.
  - **Prompts:** each agent has one `prompt_agent` step with its own prompt and model. The tasks give all three the
    same prompt; edit one to give that agent its own strategy. The brief states the match's rules, so a prompt
    need not.
  - **Grading:** `finish_match` plays the match out once the agents have stopped (the env ends each remaining
    player's turn), and `grade_match` (agentenv-game-env) grades each agent from the match the env reports: a win 1,
    a draw 0.5, a loss 0. A conquest (one agent's civilization is the last an agent still plays), a domination (one
    holds two thirds of the world's land and population) or a cultural victory (a city with 20,000 culture points,
    or 100,000 in all and twice the next civilization's) ends the game early and is its civilization's win; otherwise
    the top score at the turn limit wins (an AI civilization's too, a loss for every agent), and a tie on top is no
    one's, a draw for every player left. A defeated civilization has lost. `max_missed: 0.1` makes the grade 0 for
    an agent whose turns the env ended more than a tenth of, and so does making no move; a match whose engine
    failed has no grade. Each agent's score, cities, population and techs against the best opponent's are reported
    in rows of weight 0.
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
  session every 40 turns. `open_lobby`'s settings pace them (`min_turn_seconds`: 15 and 45, so viewers can follow
  every turn) and set the `broadcast`: the stream's title and two AI casters, Max and Ada, whose lines Sonnet 5.5
  writes and `gpt-4o-mini-tts` voices. `agent-env openciv3 stream` follows that setting; a task without `broadcast`
  streams with no casters. `showmatch-quick` is the showmatch in 10 turns.
- `sol-vs-opus` is a duel for a broadcast: GPT-6 Sol (Codex, America) against Opus 5.5 (Claude Code, Rome) on a Tiny
  map with 3 AI civilizations, for at most 750 turns at a 30 s pace (several hours, unless a conquest ends it). Their prompt says that conquering the other
  model wins (the game ends when one model's civilization is the last a model plays) and that the higher score at
  the turn limit only breaks a tie. The three AI civilizations are player slots of the game's AI, so an AI
  civilization's victory, on score at the turn limit too, is a loss for both models.
- `five-way-war` is a war for a long broadcast: GPT-6 Astra (America), GPT-6 Sol (England) and GPT-5.6 Terra
  (Persia) on Codex, Opus 5.5 (Rome) and Sonnet 5.5 (Greece) on Claude Code, alone on a Tiny map with restless
  barbarians, at a 55 s pace, each agent starting a fresh session every 30 turns (Astra, at 5× Sol's price a
  token, every 20). Their prompt makes conquering the others the ultimate goal, as the game ends only when one
  has: the turn limit, 750, is about 11 hours away, so the broadcast runs until a conquest or until you stop
  it (about 65 turns an hour). Its sponsor slot reads "Powered by Modal Sandboxes with AgentEnv Framework", with both logos. On a Small map three agents kept the peace for 75 turns; on a Tiny one, Opus
  went to war at turn 56.
- `human-vs-ai` and `human-vs-agents` seat you: you play Rome in the browser, on a Small map for 100 turns, against
  3 AI civilizations (`human-vs-ai`) or against Opus (Claude Code, Greece) and GPT-5.6 Sol (Codex, America)
  (`human-vs-agents`). Your player slot, `"player_kind": "human"`, gets a play token, and `add_player_slot` logs
  your link, `PLAY player slot you (you): http://127.0.0.1:<port>/players/you/play#x-openciv3-token=...`;
  `agent-env openciv3 play --open` finds it again. `human_turn_seconds` (900, in `open_lobby`'s settings) is how long
  a human seat may sit idle while the agents wait before the env ends its turn. Since no `prompt_agent` step plays
  your seat, `await_match` waits for the match to end (reading it, logging the turn) and `finish_match`, grading and
  the recording depend on it. `human-vs-ai` is graded by the full-game verifier (your score against the engine's own
  AI in your seat, your rank, your share of the world); `human-vs-agents` grades the agents with `grade_match`
  (`max_missed: 0.1`) and you with a second `grade_match` step, `grade-you`, which doesn't hold your idle turns
  against you. See [docs/play.md](https://github.com/earakely-scale/agentenv-openciv-plugin/blob/main/docs/play.md).

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
- In a match, the MCP server it is handed is its player slot's (`add_player_slot`), so its calls play its
  civilization.
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
stops grading. In a match, agentenv-game-env's `save_match_files` keeps the match's files the same way: the
recording by kind (`map_video`, `html_replay`, `client_video`; `last_turn` on request), listed in
`context.metadata["match_files"]`.

Run with `agent-env run openciv3 --task smoke` or `agent-env run openciv3 --task play --model <model>`.
