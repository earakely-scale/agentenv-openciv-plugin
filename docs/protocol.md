# CivBridge protocol

CivBridge is a .NET 8 console program that runs one OpenCiv3 game headlessly (no Godot, no
Civilization III files) and is driven over JSON lines. The Python env (`agentenv_openciv3`) is its
only client: it renders the facts the bridge returns into text for the agent.

## Transport

- One request per line on **stdin**, one response per line on **stdout**, in order. Nothing else is
  ever written to stdout; engine and bridge logs go to **stderr**.
- On start the bridge writes `{"id": 0, "ok": true, "result": {"ready": true, "version": "<v>"}}`.
- Request: `{"id": 7, "cmd": "state", "args": {...}}`. `args` may be omitted.
- Success: `{"id": 7, "ok": true, "result": {...}}`.
- Failure: `{"id": 7, "ok": false, "error": {"code": "cannot_found", "message": "...", "alternatives": [...], "suggest": "..."}}`.
  `message` is a complete sentence a player can act on. `alternatives` (optional) lists valid
  choices (order names, items, techs, or site objects). `suggest` (optional) is one concrete next
  call, written as the Python tool call, e.g. `unit_order(unit="u3", order="settle", x=17, y=7)`.
- Command line: `CivBridge [--lua-dir <dir>]`. The default Lua directory is `<bridge dir>/Lua`.
- One game per process. `new_game` succeeds once; a second call fails with `already_started`. The
  client restarts the process to start another game.

## Conventions

- **Ids.** The human player's units and cities get short ids, `u1`, `u2`, … and `c1`, `c2`, …,
  assigned in order of first sight and never reused within a game. Foreign units and cities are not
  addressable.
- **Coordinates** are the engine's own: integers `x, y` with `x + y` even (Civ3 diamond grid).
  North is `(x, y-2)`, east is `(x+2, y)`, northeast is `(x+1, y-1)`. Wherever a position is
  reported relative to something, the bridge adds `"dist"` (tiles, `Tile.DistanceTo`) and `"dir"`
  (one of `N NE E SE S SW W NW`, or `here`).
- **Turn limit.** `new_game` takes `turn_limit`. When `turn >= turn_limit` the game is over:
  `game_over` becomes true and `end_turn`/`unit_order` fail with `game_over`.
- **Score** = `10·cities + 3·pop + 1·tiles + 4·techs`, where `tiles` counts owned tiles that count
  for score and `techs` counts known techs (including starting techs). Every score object carries the
  components: `{"total", "cities", "pop", "tiles", "techs"}`.
- Observation commands never draw from the engine RNG.

## Commands

### `new_game`
Args (all optional except `seed`):

| Arg | Default | Values |
|---|---|---|
| `seed` | required | int ≥ 0 |
| `civ` | `"Rome"` | any playable civ name |
| `opponents` | `3` | 1 – 11 |
| `size` | `"Tiny"` | `Tiny`, `Small`, `Standard`, `Large`, `Huge` |
| `difficulty` | `"Regent"` | `Chieftain` … `Sid` |
| `barbarians` | `"Sedentary"` | `None`, `Sedentary`, `Roaming`, `Restless`, `Raging` |
| `landform` | `"Pangaea"` | `Pangaea`, `Continents`, `Archipelago` |
| `ocean` | `70` | `60`, `70`, `80` |
| `turn_limit` | `60` | int ≥ 1 |

Result: `{"turn", "turn_limit", "seed", "civ", "opponents": [civ names], "map": {"width", "height", "wrap_x"}}`.

### `state`
The human player's full situation. Result:

```json
{
  "turn": 12, "turn_limit": 60, "game_over": false, "defeated": false,
  "civ": "Rome", "government": "Despotism", "anarchy_until": null,
  "gold": 34, "gold_per_turn": 3, "rates": {"tax": 4, "science": 6, "luxury": 0},
  "research": {"current": "Bronze Working", "turns_left": 3, "beakers": 8, "cost": 20, "queue": ["Bronze Working"]},
  "known_techs": ["Alphabet", "Pottery"],
  "score": {"total": 61, "cities": 2, "pop": 5, "tiles": 21, "techs": 3},
  "explored_pct": 18.5,
  "cities": [{
    "id": "c1", "name": "Rome", "x": 12, "y": 10, "size": 3, "capital": true,
    "food_stored": 4, "food_needed": 20, "food_per_turn": 2, "turns_to_grow": 8,
    "shields_per_turn": 3, "producing": "Settler", "production_stored": 12, "production_cost": 30,
    "turns_to_complete": 6, "disorder": false, "buildings": ["Palace"]
  }],
  "units": [{
    "id": "u3", "type": "Settler", "x": 14, "y": 10, "moves_left": 1.0, "moves_max": 1,
    "hp": 3, "hp_max": 3, "status": "idle", "target": null,
    "can_found_city": {"ok": false, "reason": "adjacent to Rome (12,10); cities need one empty tile between them"},
    "orders": ["settle", "found_city", "goto", "fortify", "hold", "disband"],
    "needs_orders": true
  }],
  "rivals": [{"civ": "Greece", "met": true, "at_war": false, "cities_seen": 1}],
  "blockers": [{"kind": "idle_unit", "id": "u3", "message": "u3 Settler has moves and no orders"}],
  "last_events": [{"turn": 11, "kind": "city_grew", "text": "Rome grew to size 3"}]
}
```

- `status` is one of `idle`, `fortified`, `exploring`, `auto_work`, `goto`, `settle`,
  `working:<job>` (e.g. `working:build_road`), `done` (no moves left this turn).
- `target` is `{"x", "y", "dist", "dir"}` for `goto`/`settle`, else null.
- `orders` lists the orders `unit_order` would accept for this unit right now.
- `needs_orders` is true when the unit can move, is not under a standing order, and is not fortified.
- `blockers` lists what stops `end_turn`: `no_research` (has a city, nothing being researched),
  `no_production` (a city producing nothing), `idle_unit` (one per unit with `needs_orders`).
- `last_events` are the events produced by the most recent `end_turn` (empty before the first).

### `map`
Args: `x`, `y` (center, required), `radius` (default 3, max 8). Only tiles the player has
explored are returned. Result:

```json
{"center": {"x": 12, "y": 10}, "radius": 3, "tiles": [{
  "x": 13, "y": 9, "dist": 1, "dir": "NE", "visible": true,
  "terrain": "Grassland", "overlay": null, "resource": "Wheat", "river": true,
  "improvements": ["road"], "owner": "Rome",
  "city": {"name": "Rome", "owner": "Rome", "size": 3, "id": "c1"},
  "units": [{"owner": "Rome", "type": "Warrior", "count": 1, "id": "u2"}],
  "yield": {"food": 3, "shields": 1, "commerce": 1},
  "city_site": {"ok": false, "reason": "adjacent to Rome"}
}]}
```

`overlay` is the overlay terrain (e.g. `Forest`, `Hills`, `Jungle`) or null. `units` lists only
units on visible tiles; `id` is present for the player's own units. `resource` is null unless the
player knows about it.

### `city_sites`
Args: `unit` (a Settler id; default: the first settler, else the capital), `top` (default 5).
Result: `{"origin": {"x", "y"}, "sites": [{"x", "y", "score", "dist", "dir", "turns", "terrain",
"river", "coastal", "yield": {"food", "shields", "commerce"}}], "note": "only sites on the unit's
continent are scored"}`. `turns` is the estimated travel time for the unit (null without a unit);
`yield` sums the 3x3 area around the site.

### `unit_order`
Args: `unit` (id), `order`, and `x`, `y` where the order needs a target.

| Order | Target | Effect |
|---|---|---|
| `settle` | x, y | Standing order: walk to (x,y), found a city on arrival. If already there and valid, founds now. |
| `found_city` | — | Found a city on the unit's tile now. Optional `name`. |
| `goto` | x, y | Standing order: walk to (x,y) over as many turns as needed. |
| `explore` | — | Standing order: auto-explore (scouts, warriors). |
| `auto_work` | — | Standing order: automated worker. |
| `fortify` | — | Fortify in place (until woken). |
| `wake` | — | Cancel fortify or any standing order. |
| `hold` | — | Skip this unit for this turn. |
| `disband` | — | Remove the unit. |
| `build_road`, `build_mine`, `irrigate`, `clear_forest` | — | Worker job on the current tile. |

Result: `{"message": "<one line>", "unit": {<unit object as in state, or null if gone>},
"city": {<city object as in state>}|null, "path": {"length", "turns"}|null}`.

Error codes: `unknown_unit`, `invalid_order` (alternatives = the unit's valid orders),
`cannot_found` (alternatives = top city sites; suggest = a `settle` call), `bad_target` (off map,
`x+y` odd, or not explored), `no_path`, `no_moves`, `game_over`.

Standing orders are carried out at the start of each human turn (multi-turn `goto`/`settle`, explore,
auto_work, worker jobs). When one cannot make progress, `end_turn` reports an event (e.g.
`settle_failed`, `goto_blocked`, `explore_done`) and the unit becomes `idle`.

### `city`
Args: `city` (id). Result: the city object from `state` plus `"options": [{"name", "kind":
"unit"|"building"|"wealth", "cost", "turns"}]` (what it can produce now) and `"tiles_worked"`.

### `set_production`
Args: `city`, `item`. Result: `{"message", "city": {...}}`. Errors: `unknown_city`, `unknown_item`
(alternatives = option names).

### `techs`
Result: `{"current", "turns_left", "known": [...], "available": [{"name", "cost", "turns", "era",
"unlocks": ["Settler", "Granary", ...]}]}`.

### `set_research`
Args: `tech`. Any tech is accepted; missing prerequisites are queued first. Result: `{"message",
"current", "queue": [...]}`. Errors: `unknown_tech` (alternatives = available names), `already_known`.

### `end_turn`
Args: `skip_idle` (default false), `until_attention` (default false), `max_turns` (default 1, max 20).

- If `blockers` is non-empty and `skip_idle` is false, nothing happens: result
  `{"blocked": true, "blockers": [...]}`.
- Otherwise idle units hold, research is auto-picked if missing, and the turn advances (up to
  `max_turns` while `until_attention` is set and no blocker appears). Result:
  `{"blocked": false, "turns_advanced", "turn", "game_over", "defeated", "events": [...],
  "auto": [{"kind": "research_picked"|"government_picked"|"trade_declined", "text"}]}`.

Event kinds: `city_founded`, `city_grew`, `city_starved`, `built` (unit/building completed),
`tech_learned`, `unit_lost`, `unit_promoted`, `settle_failed`, `goto_blocked`, `explore_done`,
`job_done`, `contact` (met a civ), `war_declared`, `threat` (a foreign or barbarian unit within 3
tiles of a city or a settler), `city_destroyed`, `civ_destroyed`, `disorder`. Each event is
`{"turn", "kind", "text"}`, plus `"x", "y"` when it has a location.

### `autoplay`
Args: `turns` (required), `policy`: `null` (end turns holding everything, research auto-picked),
`found_capital` (found the capital with the first settler on turn 1, automate workers, then like
`null`), `engine_ai` (OpenCiv3's own AI plays the human seat each turn), and `record` (default
false). Result: `{"turn", "game_over", "defeated", "score": {...}, "trajectory": [{"turn",
"score": {...}}]}` (trajectory only when `record`).

### `score`
Result: `{"turn", "human": {score}, "players": [{"civ", "is_human", "defeated", "score": {...}}]}`.

## Engine patches

`patches/*.patch` apply to the pinned OpenCiv3 sources at build time (`scripts/prepare-engine.sh`
copies `C7Engine`, `QueryCiv3`, `Blast` and `C7/Lua` into `build/engine/` and applies them; the
submodule itself stays untouched):

1. `0001-defeated-controller-ends-turn-loop.patch`: `TurnHandling.PlayPlayerTurns` hands control
   back (sends `MsgStartTurn`, returns true) when the UI controller is defeated, instead of looping
   forever.
2. `0002-declare-war-uses-game-rng.patch`: `Player.DeclareWarOn` draws `refuseContactUntilTurn` from
   `GameData.rng`, so games stay deterministic.

## Round 2 additions (from the post-playtest audit)

Implemented. These extend the sections above; folding them in is still to do.

### Levers the agent was missing
- `set_rates {science, luxury}`:
  - tax is `10 - science - luxury`;
  - each rate is checked against the government's maximum; bad values give `bad_rates`, listing the allowed range;
  - result: `{"message", "rates", "gold_per_turn", "turns_left_research"}`.
- `hurry {city}`: buys the current production with gold (or population, where the government uses forced labour).
  - Before acting, it uses `GetHurryProductionDetails`; refusals give `cannot_hurry` with the engine's reason and the cost.
  - Result: `{"message", "gold_cost", "pop_cost", "city"}`.
- **Trades stay auto-declined.** Every declined offer is an `auto` entry, `trade_declined`, that says what was offered.
- **Defenders keep order.** Under Despotism, each military unit in a city suppresses one unhappy citizen (up to 2). `state` reports it and the disorder hints say so.

### Who decides
- **Engine picks are reported.** When the engine picks production after a completion, or the next research after a tech, the bridge reports it rather than hiding it:
  - `state.cities[].producing_source` and `state.research.source` are `agent` or `engine`;
  - a new blocker `choose_production` (per city) or `choose_research` asks the agent to confirm or change the pick;
  - `end_turn(skip_idle=true)` accepts the engine's picks.
- **`state.decisions`:** `{"production": {"agent": n, "engine": n}, "research": {"agent": n, "engine": n}}`. It counts completed items and learned techs by who chose them.

### Order, waste and threats
- **New city fields in `state`:**
  - `happy`, `content`, `unhappy`, `defenders` (military units in the city), `riot_risk` (true when one more citizen would put the city in disorder at the current luxury rate and garrison);
  - `capped` (true when production is full but waiting for population, e.g. a Settler at size 1 or 2), `shields_lost_last_turn`.
- **New blocker `disorder`** (per city in disorder). Its message names the fixes: raise luxury (`set_rates`), move a military unit into the city, or let it shrink.
- **New events:**
  - `disorder_started`, `disorder_ended`, `riot_risk` (the turn the risk first appears);
  - `production_capped` (once per item);
  - `gold_stolen` (barbarians or capture took gold; amount);
  - `defenseless` (a city with no defender while a hostile unit is within 3 tiles).
- **`threat` is narrower.** It only covers hostile units (at war, or barbarian), and only once per unit until it leaves and returns. Peaceful passers-by are never threats.
- **`until_attention` stops on more.** It stops on any blocker, and also on `disorder_started`, `riot_risk`, `unit_lost`, `city_destroyed`, `gold_stolen`, `war_declared`, `defenseless` and `threat`.

### Sites and messages
- **`city_sites`** also returns `nearby`: every legal site within 4 tiles of the unit, with its score, so agents never have to guess coordinates.
- **Settle suggestions only for settlers.** For a non-settler, `suggest` is never a `settle` call.
- **Precise movement errors.** A goto to a tile occupied by a foreign unit or city fails with `occupied`, naming the occupant, not `no_path`.

### A scripted baseline
- **`autoplay` gains `settler_bot`.** It follows the env's own suggestions, with no LLM:
  - found the capital on turn 0;
  - every settler: `settle` at `city_sites` top-1 from its position;
  - workers: `auto_work`. The first warrior explores; the others fortify in the nearest city without a defender.
  - each city builds a Warrior if it has no defender, else a Settler if size ≥ 2 and fewer than 8 cities, else a Worker if workers < cities, else Warrior;
  - research: the cheapest available tech.
- **It is the reference agents must beat.** It plays through the same standing orders and rules as the agent.

### Robustness
- **Engine patches:**
  - `0003-budget-never-throws.patch`: when the budget can't be balanced, clamp instead of throwing.
  - `0004-ai-turn-exceptions-are-contained.patch`: one AI player's exception is logged, and that player's turn ends, instead of aborting or stalling the turn.
  - The exact sites are in the robustness audit.
- **Autosave and `load`:** every human turn start writes `<autosave dir>/autosave.json`. The `--autosave <dir>` flag sets the directory; it defaults to a temp dir. A `load {path}` command restores a save (a new process plus `load`), so the env can recover from a bridge crash.
- **Recording:** `--record <dir>` and the `world` command, as specified in `docs/recording.md`.
