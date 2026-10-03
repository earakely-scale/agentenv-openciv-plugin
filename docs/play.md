# Humans playing alongside agents

A match can have human seats: a person plays a civilization in the browser, at the same time as the agents play
theirs, under the same rules. The human's moves go through the same env calls as an agent's tools, are logged the
same way, and show in the viewer and the recording like any seat's. This page is the contract between the bridge,
the env, the task steps and the play UI.

## 1. In a task

`openciv3_match` takes `humans` next to the agents:

```jsonc
{"id": "match", "type": "openciv3_match", "env_id": "openciv3", "turns": 100,
 "civs": {"opus": "Greece"},            // agents (deployed with deploy_agent) and their civs
 "humans": {"you": "Rome"},             // people: a name (the seat's label) and a civ; a list of names picks civs
 "human_turn_seconds": 900}             // a human seat idle this long while others wait has its turn ended; 0: never
```

- **The play link:** the step logs one per human, `PLAY Rome (you): http://127.0.0.1:<port>/play#token=<token>`, and
  stores them in `context.metadata["openciv3_match"]["play"]` as `{name: {"civ", "url"}}`. The token is the seat's:
  whoever has the link plays that civ. `agent-env openciv3 play --open` finds a running env's links and opens them.
- **Waiting for the game:** a human plays no `prompt_agent` step, so a match with humans adds `openciv3_await_game`
  (below) and makes grading and the recording depend on it.
- **Idle humans:** `human_turn_seconds: 0` lets a person take as long as they like, but an agent's `end_turn` then
  waits on them, past the agents' own tool timeout if need be; keep a limit in matches with agents.
- **Agents can't play a human's seat:** a tool call whose `X-OpenCiv3-Seat` names a human seat fails with
  `human_seat`.

`openciv3_await_game` waits until the game is over (`data/get` reports `game_over`), logging the turn as it goes:

| Field | Default | Meaning |
|---|---|---|
| `env_id` | required | The env the match runs in |
| `timeout_seconds` | `36000` | Fail the step if the game is not over by then |
| `poll_seconds` | `10` | How often it asks |

## 2. The new-game extension

`urn:openciv3:new-game/v1` takes two more args:

- `humans`: civs (among `civ` and `seats`) that people play.
- `human_turn_seconds` (default `900`): replaces `SEAT_STALL_SECONDS` (300) for human seats; `0` never ends a
  human's turn for them.

Its result adds `"play": {civ: "/play#token=<token>"}`, one per human seat (`{}` without humans); the token is 32
hex characters, new for every game. The env logs `NEW GAME <id>` when any game starts, then each link at INFO
(`PLAY <civ> (<label>) game <id> /play#token=...`), which is how `agent-env openciv3 play` finds the newest game's
links. `data/get`'s `seats[]` gains `"human": true|false` (a one-seat game a person plays has a top-level `"human"`
and lists its seat), and so do `/live`'s seats and the viewer's players. Leaving `humans` out of a new game means no
humans.

## 3. The bridge: `known_map`

Args: none (plays the request's seat, like every command). Everything the seat knows of the world, for drawing it.

```jsonc
{"turn": 12, "width": 60, "height": 60, "wrap_x": true,
 "players": [{"index": 0, "civ": "Barbarians", "barbarian": true, "me": false},
             {"index": 1, "civ": "Rome", "barbarian": false, "me": true}],
 "tiles": [[x, y, "grassland", "forest" | null, river 0|1, owner index | -1, visible 0|1,
            "Wheat" | null, ["road", "mine", ...]]],          // every tile the seat knows, nothing else
 "cities": [{"x", "y", "name", "owner", "size", "capital",    // on known tiles
             "id": "c1", "producing": "Settler", "turns_to_complete": 6, "turns_to_grow": 4}],   // the last four: own cities only
 "units": [{"x", "y", "owner", "type", "count", "id": "u3"}]}  // on visible tiles; id on the seat's own units only
```

Terrain and overlay names are the engine's lower-case keys, as in the world snapshot; a resource shows only once the
seat knows about it, as in `map`. Reading it never draws from the engine's RNG.

## 4. The play API (the env, next to `/mcp`)

Every route but the page needs the seat's token, as the header `X-OpenCiv3-Token` or `?token=`; a wrong or missing
token gets 401. Errors are JSON: `{"ok": false, "error": {"code", "message", "alternatives", "suggest"}}`, with 400 for
a malformed request (`bad_request`, `unknown_tool`, `bad_args`) and 200 for what the game refuses. The GET routes but
`view` and `status` answer with the bridge's result as it is.

| Route | What it does |
|---|---|
| `GET /play` | The play UI. It reads the token from the URL's `#token=` |
| `GET /play/api/view` | Everything the UI draws (below) |
| `GET /play/api/status` | `{"game", "turn", "game_over", "ended", "waiting_for": [civ], "seats": [...]}`, cheap, for polling |
| `GET /play/api/city?city=c1` | The bridge's `city` (with `options`) |
| `GET /play/api/techs` | The bridge's `techs` |
| `GET /play/api/diplomacy` | The bridge's `diplomacy` |
| `GET /play/api/tile?x=&y=` | The bridge's `map` at radius 0: the tile's yield, resource, improvements and whether a city can be founded |
| `GET /play/api/sites?unit=u3&top=5` | The bridge's `city_sites` for that settler |
| `POST /play/api/act` | `{"tool", "args"}`: one action, as the agents' tool of the same name (below) |

`/play/api/view`:

```jsonc
{"game": {"id": "g-3f2a", "turn": 12, "turn_limit": 100, "game_over": false, "victory": null,
          "me": {"civ": "Rome", "label": "you", "index": 1, "color": "#3987e5"},
          "ended": false,                                   // this seat has ended the turn
          "waiting_for": ["Greece"],                        // seats that haven't ended it yet (when this one has)
          "seats": [{"civ", "label", "human", "ended", "defeated", "color"}],
          "human_turn_seconds": 900, "seconds_left": 840},  // null when the turn can't be ended for you
 "colors": {"1": "#3987e5", "2": "#d95926"},                // every player, as the viewer colours them
 "state": {...},                                            // the bridge's `state` for this seat
 "map": {...},                                              // the bridge's `known_map` for this seat
 "notices": [{"turn", "kind", "text"}]}                     // the env's notices to this seat, this turn and the last
                                                            // (a turn the env ended for you is stamped with that turn)
```

`POST /play/api/act` tools and args are the MCP tools': `unit_order` (`unit`, `order`, `x`, `y`), `set_production`
(`city`, `item`), `research` (`tech`), `set_rates` (`science`, `luxury`), `buy` (`city`), `revolution`
(`government`), `diplomacy` (`action`: `declare_war`|`propose_peace`, `civ`, `gold`) and `end_turn` (no args).
Each runs through the env's tool wrapper: the action log (the viewer's actions and calls), notices, the stall clock,
and advancing the turn once every seat has ended it. The answer is `{"ok": true, "message", "result"}` with the
bridge's result. `end_turn` never waits: it ends the turn with `skip_idle` (as pressing Enter does in the game) and
answers at once, `{"ok": true, "advanced": bool, "turn", "waiting_for": [civ]}`; the UI polls `status` until the
turn moves on.

Only explicit requests (an action, opening a city, techs, diplomacy, a tile) count as the human being there for the
stall clock; the UI's polling of `view` and `status` does not. The explicit GETs count as calls under the agents'
tool names (`city_info`, `research`, `diplomacy`, `view_map`, `find_city_sites`), so a person's call counts compare
with an agent's.

## 5. The UI

`/play` is the game's own flow, drawn from the seat's `known_map`: the active unit blinks and the command bar lists
its orders with the game's keys (B build city, G go to, X explore, A automate, R road, M mine, I irrigate, ⇧C clear
forest, F fortify, ⇧W wake, Space skip, ⇧B bombard, ⇧D disband, W wait, C centre, Tab next unit); the arrow keys and
the number pad move it a tile, attacking what stands there; right-click goes to a tile (or settles a suggested
site, or attacks). Clicking a city opens the city screen (food, production, buy, what to build, its units); F1, F4
and F6 open the domestic (rates, government), foreign (war, peace) and science advisors. Enter ends the turn (again
to confirm while units still have moves); the start of a turn shows its report, and the science advisor when
nothing is being researched. While the others play, a banner names who the turn waits for.

`playtest/bots.py --humans Rome=you` starts a local match with a human seat against scripted bots, and
`playtest/play_e2e.mjs` plays that seat in a headless browser through this UI, end to end.
