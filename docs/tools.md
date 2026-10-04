# Agent tools

The env (`agentenv_openciv3.server.OpenCiv3Env`, card name `openciv3`) exposes sixteen MCP tools. They
return compact text, not JSON: briefs are under about 600 tokens (plus up to about 300 for the messages between
leaders), a radius-3 map under about 700.
Every tool that changes the game ends with a one-line footer: `[T23/60 · needs orders: u7, c1]`.

Invalid actions **raise**, so the agent sees `isError: true`, with a message that gives the reason,
the valid alternatives and the exact call to make instead.

| Tool | Args | Returns |
|---|---|---|
| `get_turn_brief` | — | Turn and limit with the year, gold, research and ETA, score with the seat's rank among the civilizations, a pace line against the targets with its share of the world's land and population against the civ nearest domination, a `CULTURE` line once a civ has 10,000 culture points or a city 2,000 (the seat's culture, the top civ's and its lead over the next as a ratio to one decimal (shown as >99.9x beyond that), and the city with the most, against the cultural victory's 100,000 with twice the next civ's, or 20,000 in one city; numbers rounded down), both baselines (null and built-in AI on the same seed), what needs orders (more than two cities in disorder fold into one line with the luxury rate that calms them all), standing orders, one line per city, the last turn's events, and the agent's plan. Self-contained: "lost context? call get_turn_brief". |
| `list_units` | `filter`: `needs_orders` (default) or `all`; `type` (optional, e.g. `"Worker"`) | One line per unit: id, type, `(x,y)`, moves, status or standing order, valid orders, and why `found_city` is or isn't possible here; with many idle units, the `unit_orders` call that orders them by type. |
| `view_map` | `x`, `y`, `radius` (default 3, max 6), or `around` (`"u7"`, `"c1"`) | Staggered ASCII of the explored tiles, a legend, then a "notable" list (resources, rivers, foreign units, cities, good sites) with distance and direction. Unexplored tiles are blank. |
| `find_city_sites` | `unit` (optional), `top` (default 5) | Ranked sites: `(x,y)`, score, distance and direction, travel turns, yields, river or coast. |
| `unit_order` | `unit`, `order`, `x`, `y` | Result line plus footer. `settle` walks to the site and founds the city on arrival. `attack` (an adjacent enemy unit or city of a civ at war) and `bombard` (in range) fight with the engine's own combat; a unit next to an enemy lists its targets with an estimated chance to win, and a city that falls is captured (one of size 1 is destroyed). |
| `unit_orders` | `orders`: 1 to 100 of `{unit, order, x, y}`; `unit` is an id or a group, `"idle"`, `"idle:Worker"` or `"all:Warrior"` | One line saying how many orders were done and failed, then one line per unit (the first 30), plus footer. One that fails doesn't stop the rest. |
| `city_info` | `city` (optional; all cities when omitted) | Size, food, growth ETA, production and ETA, queue, and what it can build with cost and turns. With more than 4 cities and none named: one line per city, those waiting on a production choice first. |
| `set_production` | `city`: an id, several (`"c1,c3"`), `"all"` or `"pending"`; `item`; `then` (optional, up to 10: the queue; `[]` clears it) | Result line, a line per city (and per city that could not), plus footer. With a queue, each completion starts the next queued item the city can build; the engine picks only when it is empty. |
| `research` | `tech` (optional) | With no tech: researchable techs with turns and what each unlocks. With a tech: sets it, queuing any prerequisites. |
| `end_turn` | `skip_idle` (default false), `until_attention` (default false), `max_turns` (default 5), `note` (optional) | Either END TURN BLOCKED with each blocker and the call that resolves it, or the turn report plus the next brief. At the turn limit: `GAME OVER` and final metrics. `note`: one line for the people watching, what the agent did this turn and why (see [What spectators read](#what-spectators-read)). |
| `revolution` | `government` | Starts anarchy (no taxes or science for a few turns), then the chosen government. The brief lists the choices. |
| `diplomacy` | `action` (`status`, `declare_war`, `propose_peace`), `civ`, `gold` | Status: one line per civ you know (war or peace, score, government, military against yours, its wars, and at war the gold it asks for peace or the turn it talks again). `declare_war` starts a war; `propose_peace` pays the asked price. |
| `plan` | `text` (optional) | Reads, or replaces, the agent's plan (at most 1,000 characters), which every brief shows back; spectators see it too. |
| `message` | `to` (`"all"`, or another leader's civ or label), `text` (at most 280 characters) | Sends a message to another agent's leader, or to all of them, in a game with several seats; see [Messages](#messages). Not a game action: no footer. |

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

## Several agents in one game

A game started with `seats` (new-game extension) is played by several agents, one civ each; there are no baselines.

- **Which seat a call plays:** the seat the request's `X-OpenCiv3-Seat` header names, by civ or by label (the
  `openciv3_match` step labels each seat with its agent's name, and the player agents send their name, or
  `OPENCIV3_SEAT` when set); with no header, the first civ. A game with one seat ignores the header. Each seat has
  its own ids, plan, notices and action log.
- **The rules:** the brief's `MATCH` line names the other agents and the AI civilizations and says how the match
  ends: at the turn limit (the top score wins), or sooner by conquest (one agent's civilization is the last an
  agent still plays, or any civ is the last left), domination (any civ holds two thirds of the world's land and
  population) or culture (any civ has a city with 20,000 culture points, or 100,000 in all and twice the next
  civ's). A victory ends the game for everyone, and
  GAME OVER names the winner; `data/get` reports it as `victory`.
- **The turn:** all seats play it at the same time. `end_turn` holds until every seat has ended the turn, then
  returns this seat's turn report and the next brief; meanwhile the env serves the other seats. After 10 minutes it
  answers `WAITING …` instead, and the next `end_turn` keeps waiting (or reports the turn, if it has advanced since).
  `until_attention` does not apply.
- **A silent seat:** while others wait, a seat that has made no call for 5 minutes has its turn ended for it (with
  `skip_idle`), and its next call starts with a `!!` line saying so. `data/get` counts these per seat.
- **Diplomacy:** another agent's civ shows as `(another agent)`. Peace with it has no price: it is signed when both
  propose it, the second within a turn of the first; the brief marks a war whose enemy `offers peace`. The leaders
  can also talk: [Messages](#messages).
- **The broadcast pace:** new-game `min_turn_seconds` (0-600, default 0) is the shortest a turn lasts, so people
  watching can follow every turn. The `end_turn` of the last seat still playing a turn (an agent's, or a person's in
  the browser) waits until the turn has lasted that long, with the env serving the other seats meanwhile, then ends
  it. That seat counts as having ended the turn in the live view, and its wait is no silence: the env never ends
  its turn for it. A blocked `end_turn` doesn't wait; neither do games with one seat, autoplay, or a turn the env
  ends for a silent seat.
- **The broadcast:** new-game `broadcast` is the stream's title and whether two AI casters talk over it, set by the
  task (`openciv3_match`'s `broadcast`) and off when a new game leaves it out. `agent-env openciv3 stream` reads it
  from the live data (`live.broadcast`) and follows it; the env only keeps it and never calls a model.

  ```jsonc
  {"title": "OpenCiv3 Showmatch",                  // 1-80 characters: the title card and the casters' intro
   "casters": {"model": "anthropic/claude-sonnet-5-5",        // writes their lines (default anthropic/claude-haiku-4-5)
               "tts_model": "openai/gpt-4o-mini-tts",          // speaks them (the default)
               "play_by_play": {"name": "Max", "voice": "ash", "style": "…"},   // each key optional: the defaults
               "analyst": {"name": "Ada", "voice": "sage", "style": "…"}}}
  ```

  `casters` is `false` (the default: no casters), `true` (Max and Ada as above, with their default styles), or an
  object that changes any of these. `voice` is one of the speech model's voices (`gpt-4o-mini-tts`: `alloy`, `ash`,
  `ballad`, `coral`, `echo`, `fable`, `nova`, `onyx`, `sage`, `shimmer`, `verse`), `style` tells it how the voice
  sounds, and a name is 1-20 letters, spaces or `.'-`, different from the other caster's. Both models are called
  through agent-env's model endpoint (`[model]`) by the streamer, never by the env.
- **Human seats:** a seat in new-game `humans` is played by a person in the browser (`GET /play`, with the seat's
  token from the result's `play`), through the same tool wrapper, action log and turn as an agent's seat. A tool call
  that would play a human seat fails with `human_seat`: its header names it, or names no seat while the first civ is
  human (agents must name their own seat then; a one-seat human game takes no tool calls). A human seat that idles
  while others wait has its turn ended after `human_turn_seconds` (default 15 minutes; 0: never). The contract,
  routes and UI: [play.md](play.md).

## Messages

In a game with several seats, `message(to, text)` reaches another agent's leader, or (`to="all"`) every other leader
still in the game; people playing a seat get them too. The game's AI civilizations don't read messages: `diplomacy`
deals with them.

- **Who:** `to` is `"all"` or a seat's civ or label, with case, a plural or a close spelling forgiven, e.g.
  `"greece"`, `"sonnet"`. Your own seat, an AI civ, a seat out of the game or an unknown name fails with
  `not_a_leader`, listing the leaders you can message. A game with one seat fails with `no_one_to_message`.
- **What:** at most 280 characters (`message_too_long`), and something left once cleaned for spectators
  (`empty_message`); the text is stored as [spectators read it](#what-spectators-read), and the reply echoes it.
  At most 3 messages a turn (`message_limit`).
- **Reading them:** each recipient's next tool reply starts with `✉ Greece (sonnet) to you: "…"` (or `to all`), once;
  the brief lists the messages the seat sent and was sent this turn and the last, newest last, under `MESSAGES`:
  `T12 Greece (sonnet) to you: "…"`, `T12 you to Rome: "…"` (the newest 12 at most, in at most 1,200 characters).
  The play view's `notices` carry them as `{"turn", "kind": "message", "from", "label", "to_all", "text"}`, and the
  play page shows them with their sender.
- **Logging:** a message is a call of the seat (the action log, the stall clock), not a game action: no footer, and
  it is not among the actions the viewer shows; spectators see messages in a feed of their own. A new game starts
  with none.

## What spectators read

Everything an agent writes for the people watching goes through `agentenv_openciv3.moderation.public(text, limit)`
first: control, format (zero-width, bidi) and lone surrogate characters are dropped, a run of combining marks is cut
to two, whitespace is collapsed, links (`http(s)://…`, `www.…`, also glued to a word) become `[link]`, words on a
small blocklist of slurs and strong profanity are masked (whole words, any case: the first letter kept, the rest `*`),
and the text is cut to `limit` characters with `…`. The `html` recording embeds the text as JSON with every `<`
escaped, so no text can break the page.

| What | Limit | Kept |
|---|---|---|
| `message` text | 280 | per message, with its turn and the seconds since the turn began |
| `end_turn` `note` | 140 | per seat and turn: the turn being ended, the last note given for it (a blocked `end_turn` keeps its note too); a longer note is cut, never refused; a person's end_turn has none |
| `plan` text | 300 | the brief shows the whole plan to its agent; spectators see this much, per seat and turn (the last plan set in it) |

The live view and the `html` recording carry them ([viewer.md](viewer.md#2-the-viewers-data-agentenv_openciv3matchdata)).

## Watching a game live

The env serves, over plain HTTP next to the MCP endpoint, the match viewer and its data. The routes, the data's
shape and the `live` object are specified in [docs/viewer.md](viewer.md#3-live-routes-the-env-next-to-mcp):

| Route | What it returns |
|---|---|
| `GET /live` | The match viewer, which follows the game: the map, every seat's view of it, the standings, the agents' actions and the real client's view |
| `GET /live/data.json?since=N` | The viewer's data with the turns after N (`since=-1`, the default, adds the static map; each turn carries the seats' notes, plans and messages of the turn before), plus `live`: the turn being played, its messages and the broadcast pace, and per seat whether it has ended the turn, for how long it has played it, its calls, its actions, its note so far and its plan. `game` changes when a new game starts; `game` is null before the first game. 400 when `since` is not a number |
| `GET /live/client.png?seat=CIV&turn=N` | The real client's view from that seat (default: the first) of the newest turn it has drawn. It draws one seat at a time, in the background, skipping turns rather than queueing them; 503 with `Retry-After` until that seat's first frame, 400 for a civ that is no seat, 404 without the client |
| `GET /live/state.json`, `GET /live/frame.png?turn=N&view=spectator\|agent` | Kept for older pages and scripts: the newest turn's scoreboard, events and actions, and the map frame of turn N as the recording draws it (404 before the first turn) |

With `OPENCIV_RECORD=0` there are no snapshots: the data has no turns and `live.recording` is false.

`agent-env openciv3 watch` lists the live URL of every OpenCiv3 env running in local Docker; `--open` opens the
first in a browser.

## Anti-stuck rules (server side)

- When the same call fails 3 times in a turn, the error adds: "same error 3x — try one of: …".
- After 25 calls in one turn, every response adds: "consider end_turn(skip_idle=true)".
- When a standing order can't make progress, it becomes an event and the unit goes back to idle.
- At the turn limit, or when a civilization wins earlier by conquest, domination or culture, `end_turn` returns
  `GAME OVER` naming the winner (the top score's at the limit, none on a tie) with final metrics, and further game
  actions fail with "the game is over".

## Action log

When `OPENCIV_ACTION_LOG` is set, the env appends one JSON line per tool call to that file:
`{"ts", "turn", "tool", "args", "ok", "error_code", "ms"}`. For `end_turn` the line also has
`"idle_units"` and `"turns_advanced"`, and in a game with seats every line has `"seat"`. `args` are as the agent gave
them (a `message`'s or a `note`'s text before moderation). The playtest harness reads this log; it is the
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
  `civ`, `human` (a person plays it), `score`, `metrics` (cities, pop, techs, tiles, units, gold, explored_pct),
  `baselines` (`null` and `engine_ai`, each with the score at the same turn and at the turn limit when known), and
  `actions` (`ok`, `invalid`, `max_consecutive_errors`). With seats (or a human seat), these describe the first
  seat, and `seats` lists every seat: `civ`, `label`, `human`, `defeated`, `score`, `metrics`, `decisions`,
  `actions`, `rank`, `share` and `auto_ended_turns`; `standings` entries carry `seat`.
- Extensions (REST, for harnesses and `apply_server_config`):
  - `urn:openciv3:new-game/v1`: scenario args, plus `seats` (more civs played by agents), `labels`
    (`{civ: label}` for recordings and reports), `humans` and `human_turn_seconds` ([play.md](play.md)),
    `min_turn_seconds` (the broadcast pace, above) and `broadcast` (the title and casters, above).
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
- `standings`: every civ, best first, `{"civ", "you", "defeated", "score", "culture"}` (`culture`: its cities' culture points, as the cultural victory counts them);
- `share`: `{"land", "pop"}`, your fractions of the world's land and population (Civ III's domination victory needs two thirds of each).

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
