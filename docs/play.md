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
 "players": [{"index": 0, "civ": "Barbarians", "barbarian": true, "me": false, "color": "#f0f8ff"},
             {"index": 1, "civ": "Rome", "barbarian": false, "me": true, "color": "#e6194b"}],
 "tiles": [[x, y, "grassland", "forest" | null, river edges 0..255, owner index | -1, visible 0|1,
            "Wheat" | null, ["road", "mine", "barbarian_camp", ...], bonus 0|1]],   // every tile the seat knows, nothing else
 "cities": [{"x", "y", "name", "owner", "size", "capital",    // on known tiles
             "era": 0, "walls": false, "disorder": false,     // every known city
             "id": "c1", "producing": "Settler", "turns_to_complete": 6, "turns_to_grow": 4,
             "starving": false}],                              // the last five: own cities only
 "units": [{"x", "y", "owner", "type", "count", "id": "u3",   // on visible tiles; id on the seat's own units only
            "hp": 3, "hp_max": 3, "fortified": false, "combat": true}],
 "battles": [{"id": 7, "turn": 12, "kind": "attack" | "bombard",   // this turn's and the last's, that the seat saw
              "attacker": {"owner", "type", "x", "y", "id": "u3" | null, "hp_before", "hp_after", "hp_max"},
              "defender": {...},                                    // the same
              "rounds": ["a", "d", "a"],                            // who won each round, in order
              "winner": "attacker" | "defender" | "retreat",
              "city": {"x", "y", "name"} | null, "captured": false, "razed": false}]}
```

Terrain and overlay names are the engine's lower-case keys, as in the world snapshot; a resource shows only once the
seat knows about it, as in `map`. Reading it never draws from the engine's RNG. Rows only ever grow at the end, so a
reader of the old nine columns keeps working. What the OpenCiv3 client's art needs (details in
[protocol.md](protocol.md#known_map)):

- `river` is the tile's river edges, `NE=1, SE=2, SW=4, NW=8` (then `N=16, E=32, S=64, W=128`, which only Civ III
  maps set); 0 means no river, so it still reads as a flag. An edge shows on both of its tiles, with the opposite
  bit. The client's river sprite at a tile's east corner is cell `(N&4||W&1) + 2(E&8||N&2) + 4(W&2||S&8) +
  8(S&1||E&4)` of `mtnRivers.png`, with W the tile and N, E, S its NE, E and SE neighbours.
- `bonus` marks bonus grassland, where the client draws the shield from `tnt.png`; `barbarian_camp` in the
  improvements marks a camp.
- `players[].color` is the client's colour for the player, after its primary/secondary pick.
- A city's `era` (0-3, its owner's) is the sprite row; `walls` picks the walled sprite for a town (size 6 or less);
  `disorder` adds the fire; `starving` (own cities) turns the label's population red.
- A unit's `hp`/`hp_max` fill the hit point bar, drawn only when `combat`; `fortified` frames it. A foreign group
  shows the unit the client would draw of it, its best defender.
- `battles` are what the client animates in combat: per round both units play ATTACK1 facing each other (the
  defender facing back), the round's loser losing a hit point, and the loser of the battle plays DEATH. A battle
  shows while its turn is this one or the last, if the seat fought it or had either tile in sight when it began,
  whoever's turn it was (an AI attacking the seat, AI against AI in sight, the seat's own attacks); `id` grows
  with each battle of the game, so a page plays each once. `rounds` are as the engine drew them: `"a"` the
  attacker won the round (the defender loses a hit point), `"d"` the defender did; `hp_before - hp_after` is the
  rounds the other side won, but for a `retreat`, whose last round's loser withdrew (the defender when it is
  `"a"`, the attacker when `"d"`). `id` is the seat's own unit's; `x`, `y` are where the units stood. `city` is
  the city on the defender's tile; `captured` says the winner took it and `razed` that it destroyed it (a city of
  size 1). A `bombard` is the bombarder's
  shots at a unit: `"a"` a hit, `"d"` a miss. The seat's own `attack` or `bombard` order answers with the same
  record as the act's `result.battle`.

The city screen and the advisors read two more commands' fields:

- `city` (`GET /play/api/city`) has `worked`, `[[x, y, food, shields, commerce], ...]`, the centre first and then
  every worked tile with the yields the client's city screen draws on it (`TileAssignmentLayer`), and `workable`,
  `[[x, y], ...]`, the tiles in the city's radius it could work, around which the client draws the border.
- `state` has `era`, the seat's era 0-3, by which the client picks the advisors' heads and the science advisor's
  background.
- `state` has `finance`, the domestic advisor's income and expenses as it computes them (`Player.AggregateFlows()`):
  `income` `{cities, taxmen, other_civs, interest, total}` ("From cities: +n", ..., "Income: n") and `expenses`
  `{science, entertainment, corruption, maintenance, unit_costs, other_civs, total}` ("-n: Science", ...,
  "Expenses: n"); `income.total - expenses.total` is `gold_per_turn`, the "Net gain/Net loss/Neutral" line.
- Each of `state`'s cities (and `city`) has the domestic advisor's row: `food_eaten` (the food column's
  `eaten.surplus` with `food_per_turn`), `shields` `{total, useful, corrupt}` (`corrupt.useful`), `commerce`
  `{total, taxes, science, luxury, corrupt, wealth}` (the commerce column is `corrupt.(taxes + science + luxury)`,
  the science and tax columns its `science` and `taxes`; the city screen's three commerce lines are its `taxes`,
  `science` with `corrupt`, and `luxury`), and `maintenance` (its buildings' upkeep, which the client leaves at 0).
- `city` has the rest of the city screen: `culture` `{per_turn, total, next_border}` ("n/turn", "Total:
  total/next_border"); `strategic` and `luxuries`, `[{name, icon, count}]` with `icon` the resource's index in
  `resources.png`; `citizens`, one per resident in the order the client draws the heads (laborers happy, content,
  unhappy, then specialists), `{"mood", "works": "tile", "tile": [x, y]}` or `{"mood": null, "works": "specialist",
  "specialist": "Entertainer"}`; and `specialists`, `[{type, index, count, taxes, research, luxuries, corruption,
  construction}]`, `index` picking the head's row in `popHeads.png` (`16 + index - 1`). Details in
  [protocol.md](protocol.md#state) and [`city`](protocol.md#city).

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
                                                            // (a turn the env ended for you is stamped with that turn;
                                                            // a message from an agent adds "from", "label", "to_all")
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
to confirm while units still have moves); the start of a turn shows its report (the events, the env's notices and
the agents' messages to you, `✉ Greece (sonnet) to you: "…"`), and the science advisor when nothing is being
researched. While the others play, a banner names who the turn waits for.

`playtest/bots.py --humans Rome=you` starts a local match with a human seat against scripted bots, and
`playtest/play_e2e.mjs` plays that seat in a headless browser through this UI, end to end.

## 6. The game's art

With the client in the image (`docker build --target client`, or `agent-env openciv3 setup --client`), `/play` draws
the map with the OpenCiv3 client's own art, the way the client draws it. **T** switches between it and the plain map
(the browser remembers the choice); without the client the page has only the plain map.

- **What it draws:** the client's terrain (its corner sprites, so coasts and terrain edges blend as in the game),
  forests, jungle, marsh, hills, mountains and volcanoes, rivers along tile edges, roads, railroads, irrigation, mines,
  fortresses, pollution and ruins, resources, bonus grassland, barbarian camps, borders in each civ's colour, the fog
  of war's soft edge, cities (town, city or metropolis by size, by their owner's era, walls on a walled town) with
  their labels, and units: each unit's sprite tinted in its civ's colour, facing where it last moved, sliding to its
  new tile with its run animation; under it its HP bar (framed when fortified), its movement light and its stack
  marks, and the turning cursor under the active unit. The HUD takes the client's art too: the status scroll with
  the next-turn dome (Enter or Space ends the turn when no unit waits), the minimap's frame and colours, and the unit
  order buttons. Colours are the client's (`known_map` `players[].color`).
- **Battles:** every battle the seat saw (`known_map` `battles`, and its own attack's `result.battle` at once) plays
  on the map as the client plays it: both units face each other and play their attack each round while their HP bars
  drop, then the loser plays its death; one battle at a time, in order, the camera following, each once (the page
  remembers the ones it showed, so a reload doesn't replay them). A long fight shows its last eight rounds.
- **The advisors and the city screen** (`screens.js`) are the client's too, with the art on: its 1024x768 screens,
  scaled to the window, every label and button where the client puts it (UIElements/Advisors, UIElements/CityScreen),
  in its Noto Sans. The advisors' heads are the seat's era's (`state.era`). Esc or the exit button closes a screen;
  F1, F4 and F6 switch between the advisors; the client's own popups (an advisor's head over the parchment, the
  orb buttons) confirm what changes the game.
  - **Domestic (F1):** the income and expenses, line by line as the client lists them (`state.finance`), the
    treasury and the net gold per turn; the science and luxury sliders (drag the beaker or the
    smiley, or press their plus and minus), each step moving a tenth as the client does (more science takes it from
    taxes, else from luxury; less gives it to taxes), sent as `set_rates`; the research and its turns; the
    government button, which starts a revolution ("You say you want a revolution?") and then asks for the new
    government (`revolution`; the client asks that when the anarchy ends); and a row per city in the client's
    columns: food eaten.surplus, shields wasted.useful, commerce corrupt.(taxes + science + luxury), the buildings'
    upkeep (which the client leaves at 0), happy.content, science, taxes, the citizens' heads by mood, and what it
    builds. A city's name opens
    its screen.
  - **Foreign (F4):** the client's screen, whose tab panel (Treaties, Trades, Details) is otherwise empty, with the
    seat's `diplomacy` on its parchment: a row per civ met (war or peace, the gold asked for peace, score,
    government) with Declare war or Propose peace, and the treaties, the peace offers and the civs met in the panel.
  - **Science (F6):** the client's tech tree, an era a page (Previous Era, Next Era), each tech's box where the
    ruleset places it, sized by what it brings, coloured by its state: known, being researched or queued (with its
    place in the queue), researchable now (with its turns), or blocked; a tech not needed for the next era is in
    italics, marked. Clicking a tech of this era or an earlier one researches it, its missing prerequisites first
    (`research`). This art draws only the ancient era's small boxes, so every box is that one stretched to its size.
  - **City screen** (click your city, or its name in the domestic advisor): the map shows through the screen's
    window with the camera on the tile south of the city, as the client puts it, the tiles the city can work
    outlined and each worked tile's food, shields and commerce on it (`city` `worked`, `workable`); its culture
    (per turn, and the total against the next border growth), strategic resources (with their counts) and luxuries;
    the citizens' heads, laborers by mood and specialists with what each adds (`city` `citizens`, `specialists`); the
    improvements; the production button (the unit or building being built) and its list
    (`set_production`), the shield box and row (useful shields, and the waste from the left); the food box (with
    the granary's half), the food line and row (the food eaten and the surplus); the growth and completion; where the
    commerce goes (gold to taxes, to science with the corruption, to happiness); and Hurry, which buys the production (`buy`). The arrow
    buttons, or ← and →, go to the previous and next city.
  - **What they leave out:** the client's moving of citizens between tiles and cycling of specialists on the city
    screen (the bridge has no command for them), the tiles other cities work, and the specialists in the domestic
    advisor's rows (`state`'s cities have mood counts, not `citizens`). Without the bridge's newer fields (an older
    bridge) the screens show what its tiles' yields and mood counts give.
- **Where it comes from:** `python -m agentenv_openciv3.webart <C7 dir> <out dir>` converts the client's art
  (C7-Game/Assets) for the browser when the client image is built: the PNG sheets as they are (losslessly smaller),
  and each unit's FLC animations decoded (`civ3flc.py`) into a sheet per unit with a row per action and direction, and
  a mask of its civ-colour pixels; the screens' art (`screens` in the manifest: backgrounds, heads, buttons, boxes and
  icons, which the page fetches when a screen first opens), with the tech boxes stretched to their sizes; and, from
  the client's ruleset (`Lua/civ3/ruleset.json`, the standalone mode's units), the science advisor's tech tree as it
  lays it out (`techs`), the units' stats and icons (`unit_info`) and the buildings' icons (`building_icons`).
  `manifest.json` names them. Out of the image, `OPENCIV_WEB_ART` points the env at a converted directory.
- **Served:** `GET /play/art/<path>` (no token: it is the same for every seat), the manifest revalidated, the rest
  cached for good under the art's id. 404 when the env has no art; the view's `game.art` says whether it has.
- **Rules followed:** the client's draw order (each layer over every tile before the next; fog over cities, under
  units), offsets and sprite choices (vendor/OpenCiv3/C7: MapView.cs, Map/*.cs, CityScene.cs, CityLabelScene.cs,
  UnitLayer.cs). Where the client draws a random variant per session (deep water, ruins), a hash of the position
  stands in. Units hold their last idle frame, as OpenCiv3 draws them; the civ colour keeps the artist's shading,
  where OpenCiv3 paints it flat.
- **Licence:** the art carries no licence (THIRD_PARTY_NOTICES.md): it stays in the client image, and the env sends
  it only to the play page's browser, like the client's view in the live viewer.
