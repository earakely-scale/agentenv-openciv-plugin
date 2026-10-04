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
| `date`, `players[i].culture`, `.era`, `.land`, `.pop`, `cities[i].wonders`, `trades` | the race and the later game's stories ([recording.md](recording.md)) |

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
           "resources": ["Wheat", ...], "improvements": ["road", ...],   // grow; looks index them
           "victory": null},
  "players": [{"index": 1, "civ": "Rome", "label": "opus", "barbarian": false,
               "name": "Opus 5.5",      // only when the broadcast names the label (new-game broadcast.names)
               "human": true,           // only on a seat a person plays (docs/play.md)
               "seat": 0,               // the seat number (bit in `known`), or null for an AI civ
               "color": "#3987e5", "engine_color": "#c43434"}],
  "static": {"tiles": [[x, y, terrain, overlay_or_-1, river]]},   // sent once (since=-1); river: its edges (NE=1, SE=2, SW=4, NW=8), 0 none
  "turns": [{
    "turn": 12,
    "date": "3450 BC",                  // only when the snapshot has it
    "owners": [[tile, owner]],          // changes since the turn before; tile indexes static.tiles
    "known":  [[tile, mask]],           // changes since the turn before
    "looks":  [[tile, overlay, resource, improvements, bonus]],   // changes; only when there are some (below)
    "cities": [[x, y, name, owner, size, capital, production_or_null, id_or_null, era, walls]],
    "units":  [[id, x, y, owner, type, hp, hp_max, fortified]],   // id: a small int for the engine id (-1 if none);
                                        // type indexes meta.unit_types; the last three only when not a healthy 3-hp unit
    "moves":  [[seq, id, owner, type, seen, x0, y0, x1, y1, ...]],  // only when there are some (below)
    "battles": [[seq, kind, winner, rounds, city, seen, ...attacker, ...defender]],
    "scores": {"1": [total, cities, pop, tiles, techs, defeated]},
    "stats":  {"1": {"gold": 40, "government": "Despotism", "research": "Bronze Working", "at_war": [3],
                     "culture": 120, "era": 0, "land": 0.04, "pop": 0.15,   // the race, when the snapshot has it
                     "military": 6, "wonders": 1}},     // units that fight, and great wonders, counted here
    "events": [{"kind": "city_captured", "owner": 1, "from": 3, "x": 10, "y": 12, "text": "...",
                "source": "derived" | "bridge"}],
    "actions": {"1": [{"text": "u4 settle → (32,28)", "ok": true}]},   // made during turn - 1, by seat civs
    "calls":   {"1": {"ok": 31, "failed": 2}},                          // tool calls during turn - 1
    // The next three only when there are some (absent otherwise), all from turn - 1:
    "notes":   {"1": "Settling the river before Greece does."},        // each seat's end_turn note for turn - 1
    "plans":   {"1": "Expand to 6 cities by T40, …"},                   // the last plan each seat set in turn - 1
    "messages": [{"from": 1, "to": [3], "text": "Join me against Carthage."},   // sent in turn - 1, oldest first
                 {"from": 3, "to": "all", "text": "…"}]                 // "to": player indices, or "all"
  }]
}
```

- **Turn entries are final.** Entry `T` is the snapshot written when turn `T` began, plus what led to it: the events
  between `T - 1` and `T` and the actions the seats took in turn `T - 1`, with their notes, plans and messages.
  Nothing in it changes later.
- **What the agents say** is text written for spectators and cleaned for them ([tools.md](tools.md#what-spectators-read)):
  `notes` (at most 140 characters, the `end_turn` note of the turn the seat ended), `plans` (the first 300
  characters of a plan, cut with `…`) and `messages` (at most 280; `from` and `to` are player indices, `to` is
  `"all"` for every other leader). The `live` object has the turn being played's.
- **How tiles look** (`looks`): a row for each tile whose overlay, resource, improvements or bonus grassland changed
  since the turn before (the first turn: each with a resource, improvement or bonus), from the snapshot's art fields
  ([recording.md](recording.md)): `overlay` indexes `meta.terrain` (-1 for none: a forest cleared), `resource` indexes
  `meta.resources` (-1 for none), `improvements` is a bit mask over `meta.improvements`, `bonus` 1 for bonus
  grassland. Older recordings have none.
- **Moves and battles** (patches 0010 and 0012): what happened during turn - 1, in the order it happened (`seq`).
  A move is one unit's run of steps: `id` as in `units`, `seen` the seats that saw it (a mask as in `known`), then
  the tiles it walked, from where it stood. A battle: `kind` 0 an attack, 1 a bombardment; `winner` `"a"`, `"d"`
  or `"r"` (a retreat); `rounds` a letter a round, `"a"` won by the attacker; `city` 0 none, 1 it stood, 2 taken,
  3 destroyed; `seen` as for a move; then each side as `owner, type, x, y, hp_before, hp_after, hp_max`.
- **Derived events** cover every civ: `city_founded`, `city_captured`, `city_destroyed`, `civ_destroyed`,
  `tech_learned`, `government_changed`, `war_declared`, `peace_signed`, `lead_change`. From the race's fields, also:
  `wonder_built` (a great wonder appears in the world: `wonder`, `city`, at the city), `era_entered` (`era`),
  `contact` (two civs meet for the first time: `owner` and `from`), `trade` (a seat's trade: `gave`, `got`, `from` the
  other side), `units_upgraded` (a civ's units of one turn whose type changed, by engine id; at the first) and
  `landing` (a civ's units that came off a ship onto land not its own, at the first; `from` the land's owner). Bridge
  events are kept for the kinds the snapshots can't show (`unit_lost`, `gold_stolen`, `disorder`, …, and `contact`
  in snapshots without `contacts`); per-seat chatter (`city_grew`, `job_done`, `built`, `threat`) is dropped.
- **Names:** with new-game `broadcast.names` (`{"opus": "Opus 5.5"}`), a seat's player has its `name`, and the events'
  texts use it; the viewer shows a seat by `name`, else `label`, else civ.
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

`live` is the turn being played right now. A seat is `ended` once it has ended the turn (including while its
`end_turn` waits for the broadcast pace), been defeated, or the game is over; `end_turn` counts in `calls` once it
returns. Before the first game, `game` is null, `turns` is empty, and so are `seats` and `messages`.

```jsonc
{"turn": 57, "game_over": false, "victory": null, "client": true, "recording": true,
 "min_turn_seconds": 15,                                          // the broadcast pace: no turn ends sooner; 0: none
 "broadcast": {"title": "OpenCiv3 Showmatch", "casters": {}},     // the task's (tools.md, The broadcast); null: none
 "messages": [{"from": "Rome", "to": ["Greece"], "text": "Join me against Carthage.",
               "seconds": 12.4}],                                 // this turn's, oldest first; "to": "all" for everyone
 "seats": [{"civ": "Rome", "label": "opus", "ended": false,      // has ended the turn
            "seconds": 41.2,                                      // playing: since the turn began; ended: how long it took
            "calls": {"ok": 12, "failed": 1},
            "actions": [{"text": "c3 builds Settler", "ok": true}],    // so far this turn
            "note": "Settling the river before Greece does.",    // its end_turn note for this turn, or null
            "plan": "Expand to 6 cities by T40, then …",          // its plan as spectators see it, or null
            "plan_turn": 12}]}                                    // the turn it set that plan, or null
```

## 4. In a recording

The `html` format is the same app with the data embedded, so it works offline as one file. With the client in the
image, `client_mp4` renders every seat's view (`<name>.client-<label or civ>.mp4`, a frame per turn); the app plays
those files when they sit next to the HTML, as `agent-env openciv3 recordings --out` leaves them.

## 5. Broadcast mode (`?stream`)

`GET /live?stream` is the layout `agent-env openciv3 stream` puts on Twitch: 1920×1080, nothing to click, a director
choosing what to show. Parameters, after `stream`:

| Parameter | Effect |
|---|---|
| `client` | Spotlights show the agent's real-client view full size, not in the corner |
| `cast=URL` | The casters' service (`streamer/caster.py`, e.g. `http://127.0.0.1:8790`): their lines are voiced and captioned |
| `title=TEXT` | The broadcast's name, in the top bar and on the title card; without it the title card shows the task's (`live.broadcast.title`) |

On screen: the map and the side panel (standings, the spotlit agent's card with its newest note, plan and turn, the
diplomacy feed, the score chart while no agent is spotlit, and the events), a lower-third caption while a caster
speaks, a ticker under the map with the newest note of each seat still in the game (`label: "note"`, every 5 s, seat
by seat, a note not shown yet before the others), and the timeline. The top bar has the broadcast's title and the
turn's year.

**The score bug** sits over the top of the map while the map is shown: each seat (up to six; two face each other
across the year and the turn) with its name and civ, rank and score, cities, people, techs, army, gold and great
wonders, its shares of the land and of the people against domination's two thirds (a mark on each bar), its culture
once a civ has a tenth of the cultural victory's 100,000, and its turn as it goes: thinking (or playing) for how long
with how many tool calls, or ended and after how long, and its newest action. In the last 25 turns it counts them down.

**The director** cuts between shots. The data's new turns and new messages queue them; a shot holds the screen for at
least 6 s before a more important one cuts in, a queued shot is dropped after 45 s, and full-screen cards are at least
8 s apart. Events in the turns already played when the page opens are not replayed. Messages can come faster than
their bubbles, so once the loop (last row) has been off the screen for 30 s, messages and new cities wait while it shows
its next wide shot (the whole map or every agent's panel) whole. The casters steer the loop rather than cut away from
it: a line about a civ sends the next spotlight to that civ, and turns a spotlight already on screen for 6 s that has
at least 6 s left.

| Shot (most important first) | What it shows |
|---|---|
| Title (on opening), game over | A card with the title, each seat's label, civ and colour (8 s); at the end a winner card (8 s), then the summary, which stays |
| `civ_destroyed` | An "eliminated" card, then the whole map |
| `city_captured`, `city_destroyed` | The camera flies in on the city, with a caption naming the event; with the client, a capture cuts to the taker's client view |
| `war_declared` | A card splitting the screen between the two sides' colours, then the camera on their border (or their closest cities) |
| A new leader | "X TAKES THE LEAD" (4 s), then a spotlight on it, once it has led two turns running (and if it still leads when the card's turn comes); at most one every 45 s, after turn 5 |
| `peace_signed` | Like a war, in peace colours |
| Two seats meet (`contact`) | Like a war, "Meet", then their border |
| A seat's great wonder (`wonder_built`) | A card in its colour ("Opus 5.5 completes a wonder: The Pyramids"), then the camera on the city with a caption; an AI's wonder only the camera and the caption |
| A seat's trade (`trade`) | Both sides' colours, "Trade", and what each gave, then their border |
| A landing by a seat or on a seat's land (`landing`) | The camera on the beach, with a caption |
| A seat's new era (`era_entered`) | A card ("Opus 5.5 enters the Middle Ages"), then a spotlight on it |
| A civ near a victory | Once each: half the land and half the people ("closes in on domination"), or half the cultural victory's culture: a card, then a spotlight; with 25, 10 and 3 turns left, the race card |
| A seat's upgrades (`units_upgraded`) | A short look, with a caption |
| A new message | A speech bubble over the map (6.6 s) while the camera flies to the sender; one waits per sender, a newer message taking the older one's place, stale after 30 s (the diplomacy feed has them all) |
| A civ's second to fourth city | A short look at the new city (6 s), with a caption; stale after 20 s |
| Nothing queued: the loop | The whole map (20 s), three agents in the spotlight (15 s each), every agent's panel (25 s), the race card (13 s, after turn 10: each seat, and an AI leading the table, with its score over the game and its shares against each victory), and the story so far (13 s: the biggest events since the last one, or the last 40 turns, when there are at least three besides lead changes; else a spotlight); with `client` and the client, the whole map and then each agent's client view (15 s each). While messages flood in, the loop's wide shots are the map and the panels, not its cards |

**The casters** (`cast`): the page asks `<cast>/cast.json?since=<last id>` every second and plays the lines in order:
`new Audio(<cast>/<line.audio>)` with the caption up while it plays, or the caption alone for `seconds` when a line has
no audio, the audio fails or autoplay is refused (the streamer's Chromium allows it). A page opened mid-broadcast starts
with the lines still under way by the caster's `speaking_until`, not its backlog.

Agent-written text (notes, plans, messages) and the casters' lines are shown as text, never as markup. The normal
viewer and recordings show the same notes, plans and messages: the agent card and each agent's panel carry its newest
note and its plan, and a diplomacy section lists the messages up to the turn shown, newest first, those to or from the
followed agent when one is followed.

## 6. The client's art

When the env has the client's art (the client image; `GET /play/art/`, [play.md](play.md#6-the-games-art)), the live
view draws the map with it, as the play page does, for every civ at once or for the seat it sees as. **T**, or the
Art chip, goes back to the plain map, and the browser remembers. Zoomed out until a tile is under 12 px wide, the
plain map shows instead, the art being noise there.

- **The picture** (terrain, rivers, forests and hills, improvements, resources, borders in each civ's colour,
  cities by size and era) is ArtPainter's (art.js), drawn in chunks at a quarter, half and full scale and kept from
  turn to turn wherever nothing in them changed (a tile's owner or look, a city); a chunk out of date is redrawn a
  few a frame, the old one standing in meanwhile. Zoomed in far enough to fit them (a tile 58 px wide), the cities'
  labels are the client's; further out they are the viewer's, which make room for each other.
- **Units** are the client's sprites in their civ's colour, one per tile (a fighting one first) with a mark per unit
  stacked there, hit point bars and the fortify pose; further out than a tile 16 px wide, the viewer's dots.
- **A turn arriving** plays out: the units walk the paths they took (`moves`), a step at a time with their run
  animation, facing the way they go; a unit that moved with no path (aboard a ship) slides there. Stepping a turn or
  following live, the turn plays in full, in order: steps and battles by `seq`, the battles as the client plays
  them (both units attack each round, the loser dies), several at once, a few seconds in all. Played (Play), the
  units only walk, within the turn's time.
- **Recordings** carry no art: the art has no licence to travel with them (THIRD_PARTY_NOTICES.md), so the `html`
  format and its videos draw the plain map. The streamer (`agent-env openciv3 stream`, with `--record` for a video
  file) shows the live view, art and all.
