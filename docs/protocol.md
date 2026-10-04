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
- **Victory.** After every turn the bridge checks Civ III's victories, for every civilization, an agent's or the
  AI's (the engine has none of its own): conquest, when it is the last civilization left (with several seats, also
  when its seat is the last one an agent still plays); domination, when it holds two thirds of the world's land
  tiles and two thirds of its population; and, at the turn limit, score, when it has the highest score (a tie on
  top is no one's victory). A one-seat game whose civilization is defeated in its own turn (its last units
  disbanded or lost) is checked at once, since no turn ends after that. The game is then over: every seat gets a `victory` event, `game_over` turns true, and
  `state`, `score` and the world snapshot carry `"victory": {"kind": "conquest"|"domination"|"score", "civ",
  "label", "turn"}` (`label` is the seat's, null for an AI civ; null until someone wins). `autoplay` plays on
  after a victory, as after a defeat, so baselines cover every turn they were asked for.
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
| `seats` | `[]` | more civs played by agents, each taking an opponent slot (see [Seats](#seats-several-agents-in-one-game)) |
| `labels` | none | `{civ: label}`, a name per seat for recordings and reports, e.g. the agent's model |

Result: `{"turn", "turn_limit", "seed", "civ", "opponents": [civ names], "seats": [{"civ", "label"}], "map":
{"width", "height", "wrap_x"}}`.

### `state`
The human player's full situation. Result:

```json
{
  "turn": 12, "turn_limit": 60, "game_over": false, "defeated": false, "victory": null, "date": "3400 BC",
  "race": {"civs_left": 5, "rank": 2, "domination": 0.667,
           "you": {"civ": "Rome", "you": true, "score": 61, "land": 0.04, "pop": 0.05},
           "leader": {"civ": "Greece", "you": false, "score": 66, "land": 0.05, "pop": 0.06},
           "nearest_domination": {"civ": null, "you": false, "score": 58, "land": 0.06, "pop": 0.05}},
  "civ": "Rome", "era": 0, "government": "Despotism", "anarchy_until": null, "tile_penalty": true,
  "governments": [{"name": "Monarchy", "corruption": "problematic", "hurry": "gold", "tile_penalty": false,
                   "trade_bonus": false, "unit_cost": 1, "free_units_per_city": 3}],
  "revolution_target": null, "gold": 34, "gold_per_turn": 3,
  "finance": {"income": {"cities": 12, "taxmen": 0, "other_civs": 0, "interest": 0, "total": 12},
              "expenses": {"science": 7, "entertainment": 0, "corruption": 1, "maintenance": 1, "unit_costs": 0,
                           "other_civs": 0, "total": 9}},
  "rates": {"tax": 4, "science": 6, "luxury": 0},
  "research": {"current": "Bronze Working", "turns_left": 3, "beakers": 8, "cost": 20, "queue": ["Bronze Working"]},
  "known_techs": ["Alphabet", "Pottery"],
  "score": {"total": 61, "cities": 2, "pop": 5, "tiles": 21, "techs": 3},
  "explored_pct": 18.5,
  "cities": [{
    "id": "c1", "name": "Rome", "x": 12, "y": 10, "size": 3, "capital": true,
    "food_stored": 4, "food_needed": 20, "food_per_turn": 2, "turns_to_grow": 8,
    "shields_per_turn": 3, "producing": "Settler", "production_stored": 12, "production_cost": 30,
    "turns_to_complete": 6, "disorder": false, "buildings": ["Palace"], "queue": ["Warrior", "Granary"],
    "food_eaten": 6, "commerce": {"total": 5, "taxes": 2, "science": 3, "luxury": 0, "corrupt": 0, "wealth": 0},
    "shields": {"total": 3, "useful": 3, "corrupt": 0}, "maintenance": 0
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

- `status` is one of `idle`, `fortified`, `aboard` (carried by a ship, see `board`), `exploring`, `auto_work`,
  `goto`, `settle`, `working:<job>` (e.g. `working:build_road`), `done` (no moves left this turn).
- A unit aboard a ship has `aboard`, the ship's id. A ship that can carry units has `capacity` (Galley 2,
  Caravel 3, Galleon 4) and `cargo`, the ids of the units aboard. Neither key is there otherwise.
- `target` is `{"x", "y", "dist", "dir"}` for `goto`/`settle`, else null.
- `orders` lists the orders `unit_order` would accept for this unit right now. A unit with moves next to an
  enemy also has `attack_targets` (see `unit_order`).
- `era` is the civ's era (`Player.EraIndex()`): 0 Ancient Times, 1 Middle Ages, 2 Industrial Age, 3 Modern Era.
  The client picks its advisors' heads and the science advisor's background by it.
- `governments` lists the governments a revolution can change to now, as in `revolution`'s result;
  `tile_penalty` is true when the current government takes 1 from any tile yield above 2 (Despotism);
  `revolution_target` is the one a revolution under way ends in. `rivals[].peace_price` is the gold that civ asks for peace while at war (null at peace, or
  while it refuses to talk).
- `needs_orders` is true when the unit can move, is not under a standing order, is not fortified and is not aboard
  a ship.
- `blockers` lists what stops `end_turn`: `no_research` (has a city, nothing being researched),
  `no_production` (a city producing nothing), `idle_unit` (one per unit with `needs_orders`). A `disorder` blocker
  (a city in civil disorder, or that riots when the turn ends) also has `now` (in disorder already), `unhappy` and
  `happy` (its citizens, by the engine's riot rule) and `luxury`, the lowest luxury rate (tenths) that calms it, or
  null when raising luxury cannot.
- `date` is the turn's year as the client's turn box shows it (`TimeOptions.GetRawNumber`): `"4000 BC"`,
  `"AD 1250"`; null for a ruleset that counts months or weeks.
- `race` is how each victory stands: `civs_left` (undefeated civilizations), the seat's `rank` by score (1 + the
  civs with a higher score), and `you`, the score `leader` and the civ `nearest_domination` (the highest of its land
  and population shares' minimum), each with its score and shares; a civ the seat has not met has `civ` null.
  `rank` and `you` are null once the seat is defeated; `race` is null when every civ is.
- `last_events` are the events produced by the most recent `end_turn` (empty before the first).
- `finance` is the domestic advisor's income and expenses, `Player.AggregateFlows()` as the client's
  `DomesticAdvisor.ShowAdvisor` shows them. Income: `cities` "From cities" (`CityInflows()`: the cities'
  commerce, corrupt and science and luxury included, less the tax collectors' share, plus Wealth's), `taxmen` "From
  taxmen", `other_civs` "From other civs" (gold-per-turn deals), `interest` "From interest"; `total` "Income"
  (`Inflows()`). Expenses: `science`, `entertainment` (the luxury spending), `corruption`, `maintenance` (buildings),
  `unit_costs` (support beyond the free units), `other_civs` "To other civs"; `total` "Expenses" (`Outflows()`).
  Science, entertainment and corruption are on both sides, as the engine counts them, so `income.total -
  expenses.total` is `gold_per_turn` (`CalculateGoldPerTurn()` is `AggregateFlows().Netflows()`). All zero
  without cities.
- Each city also has the domestic advisor's row and the city screen's lines: `food_eaten`
  (`City.FoodConsumedPerTurn()`, two per citizen; `food_per_turn` is what is left); `commerce`, from
  `City.CurrentCommerceYield()`, its `taxes`, `science` (beakers), `luxury`, `corrupt` and `wealth` (what
  building Wealth adds), with `total` their sum: the city's tiles' commerce, corrupt part included, plus what its
  specialists add (taxes counts the tax collectors'). The cities' `total`s add up to `finance.income.cities +
  taxmen`, their science, luxury and corrupt to the expenses' science, entertainment and corruption. `shields`
  is `City.CurrentProductionYield()`: `useful` (= `shields_per_turn`), `corrupt` (waste; everything in disorder
  or anarchy) and `total`, the city screen's "PRODUCTION: n per turn". `maintenance` is
  `City.MaintenanceCosts()`, its buildings' upkeep in gold (the client's column reads 0, a TODO there); they add
  up to `finance.expenses.maintenance`.

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
units on visible tiles; `id` is present for the player's own units, and `aboard` (the ship's id) for those of them
aboard a ship. `resource` is null unless the player knows about it.

### `known_map`
Args: none. Everything the seat knows of the world, for drawing it (the play page polls it after every action, so
it is one cheap pass over the map: no yields or city-site checks). Result:

```json
{"turn": 12, "width": 60, "height": 60, "wrap_x": true,
 "players": [{"index": 0, "civ": "Barbarians", "barbarian": true, "me": false, "color": "#f0f8ff"},
             {"index": 1, "civ": "Rome", "barbarian": false, "me": true, "color": "#e6194b"}],
 "tiles": [[13, 9, "grassland", null, 6, 1, 1, "Wheat", ["road"], 1]],
 "cities": [{"x": 12, "y": 10, "name": "Rome", "owner": 1, "size": 3, "capital": true,
             "era": 0, "walls": false, "disorder": false,
             "id": "c1", "producing": "Settler", "turns_to_complete": 6, "turns_to_grow": 4, "starving": false}],
 "units": [{"x": 12, "y": 10, "owner": 1, "type": "Warrior", "count": 1, "id": "u2",
            "hp": 3, "hp_max": 3, "fortified": true, "combat": true},
           {"x": 14, "y": 10, "owner": 0, "type": "Horseman", "count": 2,
            "hp": 2, "hp_max": 3, "fortified": false, "combat": true}],
 "battles": [{"id": 7, "seq": 412, "turn": 12, "kind": "attack",
              "attacker": {"owner": 0, "type": "Horseman", "x": 14, "y": 10, "id": null,
                           "hp_before": 2, "hp_after": 0, "hp_max": 2},
              "defender": {"owner": 1, "type": "Warrior", "x": 12, "y": 10, "id": "u2",
                           "hp_before": 3, "hp_after": 2, "hp_max": 3},
              "rounds": ["a", "d", "d"], "winner": "defender",
              "city": {"x": 12, "y": 10, "name": "Rome"}, "captured": false, "razed": false}],
 "moves": [{"seq": 409, "turn": 12, "owner": 0, "type": "Horseman", "id": null,
            "path": [[18, 10], [17, 11], [16, 10]]},
           {"seq": 413, "turn": 12, "owner": 1, "type": "Warrior", "id": "u3", "path": [[12, 12], [12, 10]]}]}
```

- `tiles` has one row per tile the seat knows, and none for the rest: `[x, y, terrain, overlay, river, owner,
  visible, resource, improvements, bonus]`. `terrain` and `overlay` are the engine's lower-case keys, as in the
  world snapshot (`overlay` null when it is the base terrain); `visible` is 0 or 1; `owner` is the index of the
  player whose borders hold the tile, or -1; `resource` is null unless the seat knows about it (as in `map`);
  `improvements` are as in `map` (`road`, `mine`, ...), plus `barbarian_camp` on a tile with a camp (the engine
  keeps camps as a tile flag, not an improvement).
- `river` is a bitmask of the tile's river edges, 0 when no river touches it (so it reads as the old 0/1 flag):
  `NE=1, SE=2, SW=4, NW=8`, the four edges rivers run along, then `N=16, E=32, S=64, W=128`, the corner flags
  (only Civ III maps set them; the map generator never does). These are the engine's `Tile.river*` flags, which
  the OpenCiv3 client's `RiverLayer` reads to pick a river sprite (`C7/MapView.cs`). An edge is flagged on both
  of its tiles: a tile's NE bit is its NE neighbour's SW bit, SE pairs with NW, N with S, E with W. The client
  draws one sprite per vertex, on each known tile's east corner: with N, E, S the tile's NE, E and SE neighbours,
  its 4x4 cell in `mtnRivers.png` is `(N&4 || W&1 ? 1 : 0) + (E&8 || N&2 ? 2 : 0) + (W&2 || S&8 ? 4 : 0) +
  (S&1 || E&4 ? 8 : 0)` (nothing for 0).
- `bonus` is 1 on bonus grassland where the client draws its shield marker (`C7/Map/TntLayer.cs`) and the tile
  yields the extra shield: the engine's `isBonusShield` on a grassland overlay (a forest hides it until cleared).
- `players` lists every player, barbarians included; `owner` everywhere is an index into it, the same index as
  the world snapshot's, and `me` marks the seat. `color` is the player's colour in the OpenCiv3 client, as
  `#rrggbb`: the client's pick of the civ's primary or secondary colour index, so that players differ where they
  can (`C7/Textures/PlayerTextureUtil.cs`), from its standalone colour table (`C7/Lua/standalone/textures.lua`).
  It is not the world snapshot's `color`.
- `cities` are the cities on known tiles. Each carries what the client draws a city from (`C7/Map/CityScene.cs`):
  `era`, its owner's era (0 Ancient, 1 Middle Ages, 2 Industrial, 3 Modern), the row of the city sprite;
  `walls`, whether it has a building that provides walls (the client draws the walled sprite only for towns,
  size 6 or less); and `disorder`, civil disorder (the fire). The seat's own also carry its `id`, `producing`,
  `turns_to_complete` and `turns_to_grow`, as in `state`, and `starving`, true when its food per turn is negative
  (`turns_to_grow` is null both then and when the city stagnates).
- `units` are on visible tiles only: the seat's own one per unit with its `id` and `count` 1, everyone else's
  counted per tile, owner and type. Each carries what the client's `UnitLayer` draws beside a unit: `hp` and
  `hp_max` (the hit point bar), `fortified` (a white frame around the bar) and `combat` (attack or defence above
  0; the client draws no bar for a unit that cannot fight). A group's values are those of the unit the client
  would draw of it, its best defender (`selectUnitToDisplay`): the healthiest not aboard a transport, fortified
  first.
- `battles` are the battles of this turn and the last that the seat saw, oldest first, so the play page can play
  them the way the client does (each round both units play ATTACK1 facing each other, then the loser DEATH). A
  battle shows if either unit is the seat's, or if the seat had the attacker's or the defender's tile in sight
  when it began; that includes battles fought in other players' turns (an AI's attack on the seat's units, AI
  against AI in sight) and the seat's own. A battle keeps its record and `id` (a count of the game's battles,
  from 1, never reused) while it shows; it goes once its turn is two turns old. Each:
  - `seq`: its place in the order things happened: battles and `moves` take their numbers from one count (each
    as it begins), which an autosave keeps, so a page plays both in the order they were made;
  - `turn`: the game turn it was fought in (an AI's attacks after the turn advanced carry the new turn);
  - `kind`: `attack` (`MapUnit.Fight`: a unit moving into an enemy's tile) or `bombard` (`MapUnit.Bombard` on a
    unit; a bombardment of a city, walls or an improvement fights no unit and is not listed);
  - `attacker`, `defender`: `owner` (player index), `type`, `x`, `y` (where it stood when the rounds began), `id`
    (the seat's id of the unit when it is the seat's, else null; also null for a unit of the seat's killed before
    the seat ever had it in a `state`), `hp_before` and `hp_after` (hit points when the rounds began and ended)
    and `hp_max`. A defensive bombard before the rounds is already in `hp_before`, and a promotion after them
    (one more hit point) is not in `hp_after`;
  - `rounds`: who won each round, in the order the engine drew them: `"a"` the attacker (the defender loses a
    hit point), `"d"` the defender (the attacker loses one). For `bombard` each round is a shot: `"a"` a hit,
    `"d"` a miss (the bombarder never loses hit points);
  - `winner`: `attacker` (the defender died), `defender` (the attacker died; for `bombard`, the target lived) or
    `retreat` (the last round's loser withdrew instead of losing its last hit point: the defender, to the tile
    behind it, when that round is `"a"`; the attacker when it is `"d"`). So for an attack each side's
    `hp_before - hp_after` is the number of rounds the other side won, less the retreat round;
  - `city`: `{"x", "y", "name"}` of the city on the defender's tile, or null; `captured`: the winning attacker
    moved in and took the city (patch 0011); `razed`: it moved in and the city, of size 1, was destroyed
    (barbarians take gold instead and do neither).

  The engine fights with animations off, so it sends no animation messages; patch 0010 has `MapUnit.Fight` and
  `MapUnit.BombardUnits` tell the bridge each round as they draw it. Recording a battle changes nothing in it.
- `moves` are the steps of this turn and the last that the seat saw, oldest first, so the play page can show units
  walking where they went instead of jumping there. A step shows if the unit is the seat's, or if the seat had the
  tile it left or the one it entered in sight; that includes other players' turns (AI and barbarian units in
  sight) and the seat's own moves, orders and standing orders alike. A unit's steps in one turn with nothing else
  in between (no other step or battle) make one entry, which stays the same while it is listed:
  - `seq`: its first step's place in the order things happened, as a battle's `seq`; its later steps follow on;
  - `turn`, `owner`, `type`: as for a battle's sides;
  - `id`: the seat's id of the unit, while it lives, when it is the seat's; else null;
  - `path`: `[x, y]` where it stood, then each tile it stepped to, each a neighbour of the last. A move into a
    tile won in an attack is its own step after the battle; a retreat is the loser's step during it. Units aboard
    a ship move with it and have no steps of their own.

  Patch 0012 has `MapUnit.Move` tell the bridge of every step; that changes nothing in the game. An engine restored
  from an autosave has no steps from before, and its count goes on 100000 past the save's: numbers given out after
  the save, before the engine went down, may already have been played.

### `city_sites`
Args: `unit` (a Settler id; default: the first settler, else the capital), `top` (default 5).
Result: `{"origin": {"x", "y"}, "sites": [{"x", "y", "score", "dist", "dir", "turns", "terrain",
"river", "coastal", "yield": {"food", "shields", "commerce"}}], "note": "only sites on the unit's
continent are scored"}`. `turns` is the estimated travel time for the unit (null without a unit);
`yield` sums the 3x3 area around the site. For a unit aboard a ship, `sites` ranks the sites on every landmass
along the ship's body of water instead.

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
| `attack` | x, y | Attack the adjacent tile: the top defender of a civ at war with you (barbarians always are), or move into an undefended enemy city, which is captured: it loses a citizen, its palace, small wonders and what it was building, and one of size 1 is destroyed (patch 0011). |
| `bombard` | x, y | Bombard a tile in range (`bombard` units): an enemy unit, city or improvement, at war. |
| `board` | x, y optional | A land unit goes aboard one of your ships with room: on its own tile (in port; free), or on the adjacent water tile (x,y), which it steps onto at the cost of its moves. The ship then carries it, and it is `aboard`. |
| `unload` | — | In a city only: a ship's passengers go ashore (or, ordered to a passenger, that one). At sea a passenger lands with `goto` or `settle` to a land tile next to its ship, which takes its moves. |

Ships: a Galley (Map Making) carries 2 land units, a Caravel 3, a Galleon 4; a Curragh carries none. `goto` sails
a ship (it may enter your own cities), and its passengers go with it. A ship lost at sea, sunk or disbanded, takes
its passengers with it (patch 0020); in a city they go ashore. `disband`'s message names them. The engine has no
coast-only rule for the Galley: it sails the open ocean safely.

`attack_targets` on a unit: `[{"x", "y", "dir", "owner", "defender": "Spearman 3/3 hp"|null, "city": name|null,
"win_chance": 0.62}]`. The chance comes from the engine's own strengths: each combat round the attacker wins
with a/(a+d) and the loser loses a hit point; retreats and defensive bombard are left out. An attack reports the
chance, who died, the attacker's remaining hit points, and any city that fell.

Result: `{"message": "<one line>", "unit": {<unit object as in state, or null if gone>},
"city": {<city object as in state>}|null, "path": {"length", "turns"}|null, "battle": {...}|null}`. `battle` is
the battle an `attack` or `bombard` fought, as `known_map` lists it (null for any other order, an undefended
city entered, or a bombardment of no unit).

Error codes: `unknown_unit`, `invalid_order` (alternatives = the unit's valid orders),
`cannot_found` (alternatives = top city sites; suggest = a `settle` call), `bad_target` (off map,
`x+y` odd, or not explored; for `attack` and `bombard`, alternatives = the unit's targets; for `board`, a tile
not next to the unit or land; a land unit's `goto` to a water tile with your ship on it suggests `board`), `no_path`
(a land unit's route across water says to board a ship), `no_moves`, `no_transport` (`board`: no ship of yours with
room there; suggest = boarding one nearby, if any), `at_peace` (attacking a civ you are at peace with; suggest =
declare war), `game_over`.

Standing orders are carried out at the start of each human turn (multi-turn `goto`/`settle`, explore,
auto_work, worker jobs). When one cannot make progress, `end_turn` reports an event (e.g.
`settle_failed`, `goto_blocked`, `explore_done`) and the unit becomes `idle`.

### `city`
Args: `city` (id). Result: the city object from `state` plus `"options": [{"name", "kind":
"unit"|"building"|"wealth", "cost", "turns"}]` (what it can produce now), `"tiles_worked"`, and for the city
screen's map (the client's `C7/Map/TileAssignmentLayer.cs`):

- `"worked": [[x, y, food, shields, commerce], ...]`: the city centre first, then each tile a citizen works, with
  the yields the client draws on it, the engine's `Tile.FoodYield/ProductionYield/CommerceYield(city)`. They
  sum to the city's totals before corruption: food minus two per citizen is `food_per_turn`, and shields are
  `shields_per_turn` plus what corruption (or disorder, or anarchy) takes.
- `"workable": [[x, y], ...]`: the tiles in the city's radius it could work, `City.GetWorkableTiles` (inside the
  civ's borders, no city on them), around which the client draws its border; this includes tiles another of the
  civ's cities works, and not the centre.

And the rest of the client's city screen (`C7/UIElements/CityScreen/CityScreen.cs`):

- `"culture": {"per_turn": 1, "total": 7, "next_border": 10}` (`RenderCulture`): `City.GetCulturePerTurn()`
  (the buildings' culture, the Palace's included), `GetCulture()` (gathered so far) and `10^GetBorderExpansionLevel()`,
  the culture at which the borders next grow; the client shows "1/turn" and "Total: 7/10".
- `"strategic"` and `"luxuries"`: `[{"name": "Horses", "icon": 0, "count": 1}]`, `City.GetStrategicResources` and
  `GetLuxuries` in the engine's order: the resources on the civ's own tiles that the city's road network (the
  engine's `TradeNetwork`, the city tile included) reaches, known to the civ, with how many such tiles. `icon` is
  `Resource.Icon`, the resource's index in `resources.png` (the client draws the strategic ones large with the
  count under them, the luxuries small after "(count)").
- `"citizens"`: one per resident, `size` in all, in the order the client draws the heads (`RenderPopHeads`): the
  laborers happy, then content, then unhappy, then the specialists. A laborer is `{"mood": "happy" | "content" |
  "unhappy", "works": "tile", "tile": [x, y]}` (its tile, one of `tiles_worked`); a specialist is `{"mood": null,
  "works": "specialist", "specialist": "Entertainer"}`. Moods are the engine's `City.RecalculateCitizenMoods`,
  as the client runs it before drawing; the engine leaves a specialist's mood as it was, which the
  `happy`/`content`/`unhappy` counts still include, so they can exceed the laborers'.
- `"specialists"`: `[{"type": "Entertainer", "index": 1, "count": 1, "taxes": 0, "research": 0, "luxuries": 1,
  "corruption": 0, "construction": 0}]`, the residents that are specialists (`CitizenType`, not `IsDefaultCitizen`)
  by type, in the order they first appear, with what each one adds (`CitizenType.Taxes`, `Research`, `Luxuries`,
  and the `Corruption` and `Construction` the client draws as icons and the engine does not use yet). `index` is
  `CitizenType.SpecialistIndex`, which picks the head: row `16 + index - 1` of `popHeads.png`, column the era
  (the client's `textures/popheads.lua`). The engine makes a new citizen a specialist when the city has no tile
  left for it, and an entertainer when working a tile would put the city into disorder
  (`CityTileAssignmentAI`).

### `set_production`
Args: `city`, `item`, `then` (optional list of up to 10 names). Result: `{"message", "city": {...}}`. Errors:
`unknown_city`, `unknown_item` (alternatives = option names), `no_cities`, `bad_args`.

- `then` is the city's queue (the city object's `queue`): each time the city completes something, or the engine
  changes what it builds, the bridge sets the first queued item it can build now and takes it off the queue; an item
  it cannot build any more leaves the queue, and the `built` event says so. Only when the queue is empty does the
  engine pick, and the `choose_production` blocker asks the agent to keep or change that pick. `then: []` clears the
  queue; leaving `then` out keeps it. Queues are saved with the game.
- `city` may name several cities: ids or names separated by commas, `"all"`, or `"pending"` (the cities whose
  next item the engine picked and nobody has kept or changed, and those building nothing). The result is then
  `{"message": "3 cities now build Warrior, then Granary; 1 could not.", "ok", "failed", "results": [{"city",
  "ok", "message", "code"?}], "cities": [...]}`; when none of them can, the first city's error is raised.
  `no_cities`: `"pending"` names no city now.

### `unit_orders`
Args: `orders`, 1 to 100 of `{"unit", "order", "x"?, "y"?}`, each as `unit_order` takes it, in order. `unit` is a
unit id or a group: `"idle"` (every unit waiting for orders), `"idle:Worker"` (those of a type) or `"all:Warrior"`
(every unit of a type). One that fails doesn't stop the rest. Result: `{"message": "5 orders done, 1 failed.", "ok",
"failed", "results": [{"unit", "ok", "message", "code"?}]}`, one result per unit (a group gives one per unit it
names, or one `no_units` failure when it names none now); a failure's `code` is the error `unit_order` would give,
or `unknown_unit` for a type the civ has none of, `bad_args` for `"all"` without a type. Errors: `bad_args` (no
orders, or more than 100), `game_over`.

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
tiles of a city or a settler), `city_destroyed`, `city_captured` (the seat took a city, or a civ it knows
took one in sight), `city_lost` (one of the seat's cities was taken), `civ_destroyed`, `disorder`. Each event is
`{"turn", "kind", "text"}`, plus `"x", "y"` when it has a location.

### `autoplay`
Args: `turns` (required), `policy`: `null` (end turns holding everything, research auto-picked),
`found_capital` (found the capital with the first settler on turn 1, automate workers, then like
`null`), `engine_ai` (OpenCiv3's own AI plays the human seat each turn), and `record` (default
false). Result: `{"turn", "game_over", "defeated", "score": {...}, "trajectory": [{"turn",
"score": {...}}]}` (trajectory only when `record`).

### `score`
Result: `{"turn", "human": {score}, "players": [{"civ", "is_human", "seat", "defeated", "score": {...}, "share":
{"land", "pop"}}], "human_share": {"land", "pop"}}`: each player's fractions of the world's land tiles and of its
population (Civ III's domination victory needs two thirds of each). `seat` is the seat's label (or civ) for a civ an
agent plays, else null; `is_human` marks the seat the command plays. `victory` as in `state`.

### `revolution`
Args: `government` (a name from `state.governments`). Starts anarchy (the engine's own transition: 2 to 6 turns,
longer for big empires, 2 for religious civs; no taxes and no science), after which the bridge sets the chosen
government at the start of the turn anarchy ends (event `government_picked`). Called during anarchy, it changes
the target. Result: `{"message", "government": {"current", "anarchy_until", "revolution_target", "available":
[{"name", "corruption", "hurry": "population"|"gold"|"none", "tile_penalty", "trade_bonus", "unit_cost",
"free_units_per_city"}]}}`. Errors: `unknown_government` (alternatives = the choices), `same_government`.

### `diplomacy`
No args. Result: `{"civs": [<civ>], "unmet": n}`, where a civ (one you have met and that is alive) is `{"civ",
"agent" (another seat), "at_war", "talks", "refuses_talks_until", "peace_price", "peace_offered" and "you_offered"
(a standing peace offer between seats: `{"gold", "until_turn"}`), "score": {...}, "government", "military_vs_yours"
(sum of each combat unit's best strength, theirs over yours), "at_war_with": [known civs]}`.

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

## Seats: several agents in one game

`new_game` with `seats` makes a game several agents play, one civ each. The first civ (`civ`) stays the engine's UI
controller; each seat civ takes an opponent slot and is a human player, so the engine gives it no AI turn and the
human's costs. Opponent slots beyond the seats are the engine's AI. Every command but `new_game` and `load` takes
`seat` (a seat's civ; default the first) and plays that seat: its ids (`u1`, `c1`, … per seat), standing orders,
events, decisions and plan state are its own. An unknown seat fails with `unknown_seat`.

- **The turn.** `end_turn` marks the seat ready; until every live seat is, it answers `{"blocked": false,
  "turns_advanced": 0, "turn", "waiting_for": [civs]}` and the game does not move. The seat that completes the set
  advances the game one turn (`until_attention` and `max_turns` do not apply) and gets `{"turn", "seats": {civ:
  <end_turn result>}}`, every seat's own events. The seats play the turn at the same time; standing orders run at
  the start of the next turn, seat by seat in seat order. A seat can keep giving orders after it ends the turn.
- **What another seat did.** Attacks and bombards reach the target seat as `unit_lost`, `attacked` or `bombarded`
  events; war declarations as `war_declared` (to every seat that knows both); contacts as `contact` the turn after,
  whoever's move made them.
- **Peace between seats** has no price: `propose_peace` from one seat stands until the end of the next turn and
  reaches the other as a `peace_offered` event; a `propose_peace` from the other meanwhile signs it, each side paying
  the gold it offered. Talks are never refused between seats.
- **Victory** (see Conventions): conquest also when one seat's civilization is the last an agent still plays
  (the other seats are defeated), whatever AI civs are left. The game is then over for every seat. A victory on
  score at the turn limit may be an AI civ's; the victor verifier then still ranks the seats by score, while an AI
  civ's conquest or domination leaves the match without a victor.
- `autoplay` fails with `multi_seat`. The autosave (format 2) keeps every seat, the victory and the battles
  `known_map` lists; `load` restores them.

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
10. `0010-combat-is-observable.patch`: `MapUnit.combatObserver` (an `ICombatObserver`) is told when a
   battle's rounds begin (in `MapUnit.Fight`, after any defensive bombard, and in `MapUnit.BombardUnits`), who
   won each round or shot as the engine draws it, and when the rounds end. The bridge records `known_map`'s
   `battles` with it; the observer draws nothing from `GameData.rng` and changes nothing, so games replay as before.
11. `0011-cities-are-captured.patch`: a combat unit that enters an enemy city takes it
   (`CityInteractions.CaptureCity`) instead of destroying it, as in Civ III. The city loses a citizen (one of size 1
   is destroyed), its palace, small wonders, stored food and shields; great wonders and other buildings stay, the
   loser's units in it are lost, its citizens keep their nationality and are put back to work, its borders follow
   the new owner's culture, and the new owner's AI picks what it builds. `MsgCityCaptured` tells the UI. The loser
   gets a new capital from patch 0013. Barbarians still take gold.
12. `0012-moves-are-observable.patch`: `MapUnit.moveObserver` (an `IMoveObserver`) is told of every step a unit
   takes in `MapUnit.Move` (a move, the last step of a won attack, or a retreat), once the unit is on its new tile
   and before it enters it (`OnEnterTile`). The bridge records `known_map`'s `moves` and the world snapshot's
   `moves` with it; like 0010's observer it draws nothing from `GameData.rng` and changes nothing.
13. `0013-the-palace-moves-when-the-capital-falls.patch`: a civ whose capital is taken (`CaptureCity`) or
   destroyed (`DestroyCity`) gets a new one at once and free, as in Civ III (`CityInteractions.RelocatePalace`):
   its largest city (the oldest of those), preferring one without a Forbidden Palace; a Forbidden Palace where the
   palace lands is lost. Corruption is measured again from there.
14. `0014-units-disbanded-in-a-city-add-their-shields.patch`: the ruleset's disband script
   (`C7/Lua/civ3/behaviors/gameplay.lua`) failed for every unit disbanded inside its civ's borders (`HasCity`
   read as a method, then `GetType().Name` on a MoonSharp static userdata), so none gave shields. A unit disbanded
   in one of its civ's cities now adds `ShieldRateForDisbanding` (a quarter) of its cost to what the city builds,
   unless that is a great wonder or wealth (`City.TakesDisbandShields`); `unit_order`'s `disband` message says how
   many.
15. `0015-a-tech-got-out-of-order-leaves-the-research-queue.patch`: `Player.CompleteResearchingTech` dropped the
   research queue's head whatever tech was completed, so a tech got in a trade dropped the one being researched,
   and a traded tech further down the queue stayed in it; at the head, `PlayerAI.MaybePickTechToResearch` picked it
   again forever and the turn hung (a seat with a `set_research` queue, played by `autoplay`'s `engine_ai`). The
   completed tech now leaves the queue wherever it is, and a known tech at the head is skipped.
20. `0020-cargo-goes-down-with-its-ship.patch`: `GameData.RemoveUnit` left a lost ship's passengers on the water,
   still aboard a ship that no longer existed. A ship lost at sea (sunk, disbanded, or disbanded by patch 0007's
   broke AI) now takes its passengers with it, as in Civ III; one lost in a city puts them ashore, awake. As one
   removal can now take several units, a destroyed civ's units are removed from a copy of its list (by index, the
   engine threw once a passenger listed before its ship had gone with it, and a captured last city stayed on its
   tile), a broke human seat stops disbanding when it has no units left, and the broke AI spares a loaded ship at sea
   as it spares settlers and workers.
21. `0021-the-ai-explores-by-sea.patch`: the AI builds a few ships to explore the ocean. A coastal city builds one
   while unknown tiles border what its civ knows of the ocean next to it (`ExplorerAI.HasOceanToExplore`, which,
   unlike the check it replaces, changes nothing), the civ is at peace and its ships, built or being built, number
   fewer than min(4, 1 + cities / 6); the ship gets +15, which the cheap Curragh wins. `ExplorerAI` skips
   unreachable targets (no route is an empty path, length 0, not null) and a ship explores only its own body of
   water, so a boat on a lake refuses `explore`.
22. `0022-the-ai-ferries-settlers-overseas.patch`: the AI settles other landmasses. A settler weighs the best known site
   overseas (reachable by sea from one of its civ's ports on its landmass, outside other civs' borders) against the best
   one at home and takes it when it scores more than twice as much; it waits in that port, a free ship is sent or the
   port builds one, and a new `FerryAI` carries it and its escort to a landing next to the site. A ship found with cargo
   and no plan (after a load) finishes the crossing or puts its cargo ashore in port (with no port it can reach, on the
   nearest shore it can reach), and passengers stay aboard, fortified, until landed. A unit told to hold where it stood
   that is carried off its post before it fortifies (an escort whose ship sails, or a seat unit moved by `goto` before
   `autoplay`) plans again; it used to throw on its missing path and end its player's turn. Wars across water,
   amphibious attack and naval combat are not done (`docs/full-game.md`).

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
- **`until_attention` stops on more.** It stops on any blocker, and also on `disorder_started`, `riot_risk`, `unit_lost`, `city_destroyed`, `city_lost`, `city_captured`, `gold_stolen`, `war_declared`, `defenseless` and `threat`.

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
