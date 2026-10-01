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
  "civ": "Rome", "government": "Despotism", "anarchy_until": null, "governments": ["Monarchy"],
  "revolution_target": null, "gold": 34, "gold_per_turn": 3, "rates": {"tax": 4, "science": 6, "luxury": 0},
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
  "rivals": [{"civ": "Greece", "met": true, "at_war": false, "peace_price": null, "cities_seen": 1}],
  "blockers": [{"kind": "idle_unit", "id": "u3", "message": "u3 Settler has moves and no orders"}],
  "last_events": [{"turn": 11, "kind": "city_grew", "text": "Rome grew to size 3"}]
}
```

- `status` is one of `idle`, `fortified`, `exploring`, `auto_work`, `goto`, `settle`,
  `working:<job>` (e.g. `working:build_road`), `done` (no moves left this turn).
- `target` is `{"x", "y", "dist", "dir"}` for `goto`/`settle`, else null.
- `orders` lists the orders `unit_order` would accept for this unit right now. A unit with moves next to an
  enemy also has `attack_targets` (see `unit_order`).
- `governments` lists the governments a revolution can change to now; `revolution_target` is the one a revolution
  under way ends in. `rivals[].peace_price` is the gold that civ asks for peace while at war (null at peace, or
  while it refuses to talk).
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
| `attack` | x, y | Attack the adjacent tile: the top defender of a civ at war with you (barbarians always are), or move into an undefended enemy city, which the engine razes. |
| `bombard` | x, y | Bombard a tile in range (`bombard` units): an enemy unit, city or improvement, at war. |

`attack_targets` on a unit: `[{"x", "y", "dir", "owner", "defender": "Spearman 3/3 hp"|null, "city": name|null,
"win_chance": 0.62}]`. The chance comes from the engine's own strengths: each combat round the attacker wins
with a/(a+d) and the loser loses a hit point; retreats and defensive bombard are left out. An attack reports the
chance, who died, the attacker's remaining hit points, and any city that fell.

Result: `{"message": "<one line>", "unit": {<unit object as in state, or null if gone>},
"city": {<city object as in state>}|null, "path": {"length", "turns"}|null}`.

Error codes: `unknown_unit`, `invalid_order` (alternatives = the unit's valid orders),
`cannot_found` (alternatives = top city sites; suggest = a `settle` call), `bad_target` (off map,
`x+y` odd, or not explored; for `attack` and `bombard`, alternatives = the unit's targets), `no_path`, `no_moves`,
`at_peace` (attacking a civ you are at peace with; suggest = declare war), `game_over`.

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

### `revolution`
Args: `government` (a name from `state.governments`). Starts anarchy (the engine's own transition: 2 to 6 turns,
longer for big empires, 2 for religious civs; no taxes and no science), after which the bridge sets the chosen
government at the start of the turn anarchy ends (event `government_picked`). Called during anarchy, it changes
the target. Result: `{"message", "government": {"current", "anarchy_until", "revolution_target", "available":
[{"name", "corruption", "hurry": "population"|"gold"|"none", "tile_penalty", "trade_bonus", "unit_cost",
"free_units_per_city"}]}}`. Errors: `unknown_government` (alternatives = the choices), `same_government`.

### `diplomacy`
No args. Result: `{"civs": [<civ>], "unmet": n}`, where a civ (one you have met and that is alive) is `{"civ",
"at_war", "talks", "refuses_talks_until", "peace_price", "score": {...}, "government", "military_vs_yours" (sum of
each combat unit's best strength, theirs over yours), "at_war_with": [known civs]}`.

### `declare_war`
Args: `civ`. The engine's `DeclareWarOn`; the civ refuses to talk for 5 to 16 turns (longer after a sneak attack
from inside its borders). Result: `{"message", "civ": <civ>}`. Errors: `unknown_civ` (alternatives = the civs you
have met), `already_at_war`.

### `propose_peace`
Args: `civ`, `gold` (default 0, paid to them). Peace is signed through the engine's deal path when `gold` is at
least the civ's `Player.PeacePriceFor` (patch 0009): 0 when it is losing or tired of the war, more when it is
winning, and never inside its refuse-contact window or a war younger than its minimum. Result: `{"message",
"civ": <civ>}`. Errors: `unknown_civ`, `not_at_war`, `no_talks`, `refused`, `price` (suggest = the call at the
asked price, when you can pay), `not_enough_gold`.

An AI that wants peace offers it during its turn; the bridge cannot hold the AI's turn open, so it reports a
`peace_offered` event (which stops `end_turn(until_attention)`) and the agent accepts with `propose_peace`. Peace
between any two civs the human knows is reported as `peace_signed`.

## Engine patches

`patches/*.patch` apply to the pinned OpenCiv3 sources at build time (`scripts/prepare-engine.sh`
copies `C7Engine`, `QueryCiv3`, `Blast` and `C7/Lua` into `build/engine/` and applies them; the
submodule itself stays untouched):

1. `0001-defeated-controller-ends-turn-loop.patch`: `TurnHandling.PlayPlayerTurns` hands control
   back (sends `MsgStartTurn`, returns true) when the UI controller is defeated, instead of looping
   forever.
2. `0002-declare-war-uses-game-rng.patch`: `Player.DeclareWarOn` draws `refuseContactUntilTurn` from
   `GameData.rng`, so games stay deterministic.
3. `0003-budget-never-throws.patch`: when the budget cannot be balanced, gold stays at 0 instead of
   throwing mid-turn.
4. `0004-ai-turn-exceptions-are-contained.patch`: an exception in one AI or barbarian turn ends that
   player's turn instead of aborting or stalling the game.
5. `0005-building-prerequisites-check-the-city.patch`: a building that needs another (Bank, University,
   Cathedral and 12 more) becomes buildable once the city has the prerequisite; the check compared the
   building with itself, so none could ever be built.
6. `0006-small-wonders-are-buildable.patch`: small wonders can be built, once per civ and by one city at
   a time (Heroic Epic and The Pentagon stay blocked: they need armies, which the engine lacks). The AI
   values a Forbidden Palace by the corruption it removes.
7. `0007-ai-keeps-its-science-funded.patch`: the AI runs a deficit only while its treasury covers it,
   builds no units it cannot support, gives up science before units when broke and then disbands its
   least useful unit, and leaves Despotism for the best government it knows (at most once per 50 turns).
   The human seat's budget is unchanged.
8. `0008-research-cost-follows-the-difficulty.patch`: the difficulty's AI cost factor scales the AI's
   research the way it scales production (the AI pays 200% at Chieftain and 40% at Sid; it paid 50% and
   250%). Regent and the human's costs are unchanged.
9. `0009-ai-makes-peace.patch`: `Player.PeacePriceFor(gameData, other)`, the gold an AI asks for peace
   (`propose_peace` above); `WouldAcceptDealFrom` charges it for a peace offer; AIs sign peace with each
   other (the loser paying the winner's price) and offer a human peace every 5 turns when they want
   nothing; a war declared within 50 turns of a treaty counts as breaking it, which makes the victim refuse
   peace for longer. The new war records are saved with the game.

Measured over full 540-turn Standard games with 7 AIs at Regent (seeds 1-3), patches 0005-0009 take:
- mean AI techs at T540 from 32 to 43-45, and civs with an Industrial-era tech from 0 to 3-7;
- Banks, Universities and Cathedrals from 0 to 6-8, 25-45 and 81-110;
- wars ending in peace from 0 to 6-15 per game;
- AI governments at T540 from all Despotism to mostly Republic and Democracy.

Wall time per game is unchanged at 74-78 s, and a seed replays byte-identically.

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
- **Precise movement errors.** A goto or settle to a tile occupied by a foreign unit or city fails with `occupied`, naming the occupant, not `no_path`. For a settler it also lists the best free sites and suggests settling the first.

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
- **Per-turn saves:** `--saves <dir>` also keeps every turn's autosave as `<dir>/turn-NNNN.json.gz`, for
  renderers that load engine saves (the client's view in `docs/recording.md`). Each one loads with `load`
  once decompressed.
