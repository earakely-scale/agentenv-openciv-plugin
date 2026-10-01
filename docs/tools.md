# Agent tools

The env (`agentenv_openciv3.server.OpenCiv3Env`, card name `openciv3`) exposes fourteen MCP tools. They
return compact text, not JSON: briefs are under about 600 tokens, a radius-3 map under about 700.
Every tool that changes the game ends with a one-line footer: `[T23/60 · needs orders: u7, c1]`.

Invalid actions **raise**, so the agent sees `isError: true`, with a message that gives the reason,
the valid alternatives and the exact call to make instead.

| Tool | Args | Returns |
|---|---|---|
| `get_turn_brief` | — | Turn and limit, gold, research and ETA, score, a pace line against the targets and both baselines (null and built-in AI on the same seed), what needs orders, standing orders, one line per city, the last turn's events, and the agent's plan. Self-contained: "lost context? call get_turn_brief". |
| `list_units` | `filter`: `needs_orders` (default) or `all` | One line per unit: id, type, `(x,y)`, moves, status or standing order, valid orders, and why `found_city` is or isn't possible here. |
| `view_map` | `x`, `y`, `radius` (default 3, max 6), or `around` (`"u7"`, `"c1"`) | Staggered ASCII of the explored tiles, a legend, then a "notable" list (resources, rivers, foreign units, cities, good sites) with distance and direction. Unexplored tiles are blank. |
| `find_city_sites` | `unit` (optional), `top` (default 5) | Ranked sites: `(x,y)`, score, distance and direction, travel turns, yields, river or coast. |
| `unit_order` | `unit`, `order`, `x`, `y` | Result line plus footer. `settle` walks to the site and founds the city on arrival. `attack` (an adjacent enemy unit or city of a civ at war) and `bombard` (in range) fight with the engine's own combat; a unit next to an enemy lists its targets with an estimated chance to win, and a city that falls is razed. |
| `city_info` | `city` (optional; all cities when omitted) | Size, food, growth ETA, production and ETA, and what it can build with cost and turns. |
| `set_production` | `city`, `item` | Result line plus footer. |
| `research` | `tech` (optional) | With no tech: researchable techs with turns and what each unlocks. With a tech: sets it, queuing any prerequisites. |
| `end_turn` | `skip_idle` (default false), `until_attention` (default false), `max_turns` (default 5) | Either END TURN BLOCKED with each blocker and the call that resolves it, or the turn report plus the next brief. At the turn limit: `GAME OVER` and final metrics. |
| `revolution` | `government` | Starts anarchy (no taxes or science for a few turns), then the chosen government. The brief lists the choices. |
| `diplomacy` | `action` (`status`, `declare_war`, `propose_peace`), `civ`, `gold` | Status: one line per civ you know (war or peace, score, government, military against yours, its wars, and at war the gold it asks for peace or the turn it talks again). `declare_war` starts a war; `propose_peace` pays the asked price. |
| `plan` | `text` (optional) | Reads, or replaces, the agent's plan (at most 1,000 characters), which every brief shows back. |

## War, peace and government

- **Brief:**
  - the head shows every war with the price of peace, e.g. `!! at war with Arabia (peace: 120 gold)`;
  - a `GOVERNMENT` line lists the governments a revolution can switch to, with the call;
  - during anarchy, the line names the government that follows.
- **Peace costs what the other civ asks:**
  - nothing when it is losing or tired of the war, gold when it is winning;
  - never while it refuses to talk, which happens for some turns after you declare war on it, and longer after
    breaking a peace.
  - An AI that wants peace offers it as a `peace_offered` event, which `end_turn(until_attention)` stops for.
    Accept it with `diplomacy(action="propose_peace", civ=...)`.
- **Attacks:** an attack on a civ you are at peace with fails with the call that declares war. The win chance
  comes from the engine's attack and defense strengths and both units' hit points. It ignores retreats, so it is
  an estimate.

## Anti-stuck rules (server side)

- When the same call fails 3 times in a turn, the error adds: "same error 3x — try one of: …".
- After 25 calls in one turn, every response adds: "consider end_turn(skip_idle=true)".
- When a standing order can't make progress, it becomes an event and the unit goes back to idle.
- At the turn limit, `end_turn` returns `GAME OVER` with final metrics, and further game actions fail
  with "the game is over".

## Action log

When `OPENCIV_ACTION_LOG` is set, the env appends one JSON line per tool call to that file:
`{"ts", "turn", "tool", "args", "ok", "error_code", "ms"}`. For `end_turn` the line also has
`"idle_units"` and `"turns_advanced"`. The playtest harness reads this log; it is the
authoritative record of what the agent did.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `CIVBRIDGE_CMD` | `/opt/civbridge/CivBridge` | Command that starts the bridge (split on spaces) |
| `OPENCIV_SEED` | `1` | Seed for the first game |
| `OPENCIV_TURN_LIMIT` | `60` | Turn limit |
| `OPENCIV_SIZE`, `OPENCIV_OPPONENTS`, `OPENCIV_DIFFICULTY`, `OPENCIV_BARBARIANS` | as in `new_game` | Scenario |
| `OPENCIV_BASELINES` | `1` | Compute the null and built-in-AI baselines for the same seed in the background |
| `OPENCIV_ACTION_LOG` | unset | Path of the action log |
| `OPENCIV_RECORD` | `1` | Record every turn for the recording extension |
| `OPENCIV_CLIENT`, `GODOT`, `GODOT_ARGS` | set by the client image | The OpenCiv3 client install, the Godot binary and its arguments, for `client_mp4` (`docs/recording.md`) |
| `MCP_HOST`, `MCP_PORT` | `0.0.0.0`, `18765` | HTTP bind (AgentEnv SDK) |

## Data plane and extensions

- `data/reset`: start a new game from the current scenario.
- `data/add`: a `DataPart` with `{"scenario": {...}}` updates the scenario and starts a new game.
- `data/get`: one `DataPart` with the summary: `turn`, `turn_limit`, `game_over`, `defeated`, `seed`,
  `civ`, `score`, `metrics` (cities, pop, techs, tiles, units, gold, explored_pct), `baselines`
  (`null` and `engine_ai`, each with the score at the same turn and at the turn limit when known), and
  `actions` (`ok`, `invalid`, `max_consecutive_errors`).
- Extensions (REST, for harnesses and `apply_server_config`):
  - `urn:openciv3:new-game/v1`: scenario args.
  - `urn:openciv3:autoplay/v1`: `turns`, `policy` (`null`, `found_capital`, `engine_ai`).

## Round 2 additions (from the post-playtest audit)

Implemented. These extend the sections above; folding them in is still to do.

**New tools (12 in total):**
- `set_rates(science, luxury)`: the luxury rate is the main fix for disorder, and science is how gold turns into techs.
- `buy(city)`: rush the current production with gold.

**The brief shows:**
- every city in disorder or at riot risk, with the fix;
- cities without a defender;
- capped production, with the shields being lost;
- unspent gold, with a hint to buy or raise science;
- the engine's pending picks (`choose_production`, `choose_research`), with the call to change them.

**`data/get` adds:**
- `decisions`, as in `state.decisions`;
- `harness`: `{"autoplay_turns", "new_games", "extension_calls"}`. A graded agent game must have `autoplay_turns == 0`.
- `baselines` gains `settler_bot`.

**Robustness:**
- If the bridge dies, the env restarts it from the last autosave and reports an event: "the engine restarted from the start of turn N".
- Read-only tools never return stale state as ok.
- A failed `data/add` or new game leaves the running game untouched.
- Plans are sanitised (lone surrogates are dropped).

**Text fixes:**
- No more "Nonet": a missing ETA renders as "no progress" with the reason.
- Fix suggestions are only given when they would succeed.

**Recording:** per `docs/recording.md`. The extension is `urn:openciv3:recording/v1`, and rendering lives in `agentenv_openciv3.recording`.

**Prompt hygiene for harnesses:** refer to tools by name in prose (e.g. "the end_turn tool"), never with call syntax that a model might copy as a bare tool name.
