# The match viewer

One web app shows a game live (`GET /live`) and as a recording (the `html` format). It draws everything from
data: the map, every seat's own view of it, the standings, the agents' actions, and the real OpenCiv3 client's view
of any seat. This page is the contract between the bridge, the env and the app.

## 1. The snapshot (bridge, `--record`)

`docs/recording.md` has the base schema. Schema 2 adds, all optional for readers so old recordings still load:

| Field | Meaning |
|---|---|
| `schema` | `2` |
| `seats` | `[{"index", "civ", "label"}]`, the agent seats in seat order: bit `k` of a tile's `known` is `seats[k]` |
| `tiles[i][6]` | `known`: a bitmask over `seats` (was 0/1 for any seat). Non-zero still means some seat knows it |
| `players[i].gold`, `.government`, `.research` | the treasury, the government's name, the tech being researched (null if none) |
| `players[i].at_war` | the player indices this player is at war with (barbarians left out) |
| `players[i].contacts` | the player indices this player has met |
| `cities[i].id`, `.production` | a stable city id, and the name of what the city builds (null if nothing) |
| `units[i].id` | a stable unit id, unique within the game, so a viewer can follow a unit from turn to turn |

## 2. The viewer's data (`agentenv_openciv3.matchdata`)

`MatchData` turns snapshots into one compact JSON document, appended to turn by turn. Live, the app asks only for
the turns it hasn't seen; a recording embeds the whole document.

```jsonc
{
  "schema": 1,
  "game": "g-3f2a",                     // changes when a new game starts: the app starts over
  "meta": {"seed": 1, "turn_limit": 200, "map": {"width": 100, "height": 100, "wrap_x": true},
           "seam": 94,                  // an even x: the column with the least land, drawn as the left edge
           "terrain": ["ocean", "sea", "coast", "grassland", ...],
           "unit_types": ["Settler", "Warrior", ...],   // grows; always sent whole
           "civilian": ["Settler", "Worker", ...],      // unit types that don't fight
           "victory": null},
  "players": [{"index": 1, "civ": "Rome", "label": "opus", "barbarian": false,
               "seat": 0,               // the seat number (bit in `known`), or null for an AI civ
               "color": "#3987e5", "engine_color": "#c43434"}],
  "static": {"tiles": [[x, y, terrain, overlay_or_-1, river]]},   // sent once (since=-1)
  "turns": [{
    "turn": 12,
    "owners": [[tile, owner]],          // changes since the turn before; tile indexes static.tiles
    "known":  [[tile, mask]],           // changes since the turn before
    "cities": [[x, y, name, owner, size, capital, production_or_null, id_or_null]],
    "units":  [[id, x, y, owner, type]],            // id: a small int for the engine id (-1 if none); type indexes meta.unit_types
    "scores": {"1": [total, cities, pop, tiles, techs, defeated]},
    "stats":  {"1": {"gold": 40, "government": "Despotism", "research": "Bronze Working", "at_war": [3]}},
    "events": [{"kind": "city_captured", "owner": 1, "from": 3, "x": 10, "y": 12, "text": "...",
                "source": "derived" | "bridge"}],
    "actions": {"1": [{"text": "u4 settle → (32,28)", "ok": true}]},   // made during turn - 1, by seat civs
    "calls":   {"1": {"ok": 31, "failed": 2}}                           // tool calls during turn - 1
  }]
}
```

- **Turn entries are final.** Entry `T` is the snapshot written when turn `T` began, plus what led to it: the events
  between `T - 1` and `T` and the actions the seats took in turn `T - 1`. Nothing in it changes later.
- **Derived events** cover every civ: `city_founded`, `city_captured`, `city_destroyed`, `civ_destroyed`,
  `tech_learned`, `government_changed`, `war_declared`, `peace_signed`, `lead_change`. Bridge events are kept for
  the kinds the snapshots can't show (`unit_lost`, `gold_stolen`, `disorder`, …); per-seat chatter (`city_grew`,
  `job_done`, `built`, `threat`) is dropped.
- **Lead changes** are every change of the top score (a tie keeps the old leader). The viewer and the video show one
  once the new leader has held the lead every turn since, for up to 5 turns, judged with nothing after the turn shown;
  while the top score is tied, no one leads.

## 3. Live routes (the env, next to `/mcp`)

| Route | What it returns |
|---|---|
| `GET /live` | The viewer app |
| `GET /live/data.json?since=N` | The match data with only turns after `N`; `since=-1` (default) adds `static`. Also `live` (below) |
| `GET /live/client.png?seat=CIV&turn=N` | The real client's view of the game from that seat (default: the first), for the newest turn it has drawn; 503 with `Retry-After` until the first frame, 404 without the client |
| `GET /live/state.json`, `GET /live/frame.png` | Kept for old pages and scripts |

`live` is the turn being played right now. A seat is `ended` once it has ended the turn, been defeated, or the game is
over; `end_turn` counts in `calls` once it returns. Before the first game, `game` is null and `turns` is empty.

```jsonc
{"turn": 57, "game_over": false, "victory": null, "client": true, "recording": true,
 "seats": [{"civ": "Rome", "label": "opus", "ended": false,      // has ended the turn
            "seconds": 41.2,                                      // playing: since the turn began; ended: how long it took
            "calls": {"ok": 12, "failed": 1},
            "actions": [{"text": "c3 builds Settler", "ok": true}]}]}   // so far this turn
```

## 4. In a recording

The `html` format is the same app with the data embedded, so it works offline as one file. With the client in the
image, `client_mp4` renders every seat's view (`<name>.client-<label or civ>.mp4`, a frame per turn); the app plays
those files when they sit next to the HTML, as `agent-env openciv3 recordings --out` leaves them.
