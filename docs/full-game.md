# A full game

What it takes for an agent to play OpenCiv3 the way a person plays Civilization III: a Standard map, 7 AI
civs, Regent, roaming barbarians and the whole 540 turns (4000 BC to AD 2050), played to a result. This
summarises a research pass of 2026-10-01:
- full 540-turn engine games on Standard and Huge;
- a read of the engine, the bridge and the tools;
- the cost of 21 recorded agent games;
- a verifier that rechecked the engine claims.

## Bottom line

- **The settings work today.** The bridge accepts the human setup. The engine plays all 540 turns of a
  Standard game with 7 AIs in 62 to 111 s, and a seed replays identically. No game in about 4,700
  simulated turns produced an error.
- **The harness was the first thing to break, and it is fixed.** One `claude -p` session per game grows its
  context without bound: Sonnet would run out of budget around T190, or fill its context window around T276.
  `playtest/run.py --context-cap` plays a game in sessions that rotate at a context cap.
- **With that, Sonnet played a full 540-turn Standard game,** and won it on every measure the engine has
  (below).
- **The engine gives the game no shape after the ancient era.**
  - There is no victory condition, and a game ends only at the turn limit or when the agent is defeated (the
    bridge now decides victories).
  - The AI never changes government, and every civ ends in Despotism with 25 to 35 of 83 techs.
  - Entering an enemy city destroyed it instead of capturing it (fixed by patch 0011).
  - AI wars never end in peace.
- **The agent cannot attack, change government or negotiate.** A passive player gets razed late in the
  game: the scripted bot with science at 100% lost 10 cities between T315 and T334 to Longbowmen and
  Knights.

## A full game, played

2026-10-01, Sonnet 5.5, seed 1. The settings: Standard, 7 AIs, Regent, Roaming barbarians, 540 turns. Sessions
rotated at a 100K context cap.

| | Result |
|---|---|
| Score at T540 | 4,106: 120 cities, 549 pop, 1,123 tiles, 34 techs. The best AI civ had 1,149; the built-in AI in the same seat 667; `settler_bot` 453 |
| Rank | 1st of 8 on 483 of 541 turns; 4 AI civs destroyed by AI wars; 58% of the land and 62% of the population at the end |
| Play | 1,499 tool calls (2.8 a turn), 3.1% invalid, no autoplay. The agent chose 42% of production and 41% of research; the engine picked the rest |
| Cost | $22.31 and 37 minutes, in 4 sessions of $4.90 to $6.39 each |

**What it shows about the gaps above:**
- **The engine's ceilings:** the game ended in Despotism, in the ancient era, with no victory to claim.
- **The missing attack:** Arabia razed 3 of Rome's cities after T482, and the agent had no way to respond.
- **Rotation only at a segment's end:** sessions reached 251K to 326K of context before rotating.
  Rotating mid-segment, at the cap itself, would roughly halve the cost, towards the $12 projected for
  light play.
- **The late game is underplayed:** with 120 cities, the agent accepted most of the engine's production
  picks and cities starved 100 times. Batching tools is what would let it manage that empire.

## Human settings against what works today

| Setting | Human | Today |
|---|---|---|
| Map | Standard, 100×100 | Tiny to Huge (160×160) all start |
| Civs | 8 | up to 12 (11 opponents, a bridge cap) |
| Difficulty | Regent or harder | all 8 levels. Harder levels make the AI research slower, the opposite of Civ III (`GameData.cs:380`) |
| Barbarians | Roaming | all 5 levels. Only Warriors, Horsemen and Galleys; a barbarian in a city steals gold instead of razing it |
| Length | 540 turns | up to 1000; the env defaults to 60 |
| Victory | conquest, domination, space, UN, culture, score | none |
| Government | revolutions | Despotism only: no command for the agent, no code for the AI |
| War and peace | both | the AI declares war and never makes peace; the agent has neither |
| City capture | yes | yes (patch 0011): the city loses a citizen, its palace and small wonders; one of size 1 is destroyed; the loser gets a new capital (patch 0013) |
| Late eras | Industrial and Modern | with patches 0005-0008 and 0016 the AIs build Universities, Banks, Stock Exchanges and Factories and end a Standard game with 58 to 65 techs on average; the Industrial and Modern land and sea units are in the game and units upgrade (patches 0018, 0019); power plants, pollution and air units are missing |

## What is missing

**Engine** (OpenCiv3 itself), in order of impact:

1. **No ending: fixed in the bridge.**
   - The engine's turn loop is `while (true)`, score is a TODO, and the ruleset's turn limit is never read.
   - The bridge now decides Civ III's conquest, domination and score-at-the-limit victories for every civilization,
     the AI's included, and the game ends at the first.
   - Still missing: the space race and the UN, which need the engine (L).
2. **Tech and economy stall: fixed** by patches 0005-0008.
   - The causes: 15 buildings were blocked by a prerequisite check, small wonders were blocked, the AI never
     changed government, the AI cut its own science to 0, and harder difficulties made AI research dearer.
   - Now, at Regent: mean AI techs at T540 went from 32 to 43-45, 3 to 7 civs reach the Industrial era, and
     most AIs end in Republic or Democracy.
   - Buildings now have their economic effects (patch 0016): Library, University and Research Lab +50%
     science, Copernicus' Observatory and Newton's University +100%, Marketplace, Bank and Stock Exchange +50%
     tax and luxury, Factory and Manufacturing Plant +25% shields, and Wall Street pays 5% interest (at most 50
     gold). Before, they cost upkeep and returned nothing, and the AI valued a Library only for its culture; now
     it values one by what it would add in that city, in order (a Marketplace's luxury counts in a city that riots).
   - Measured with engine_ai in every seat at Regent, before and after patch 0016:

     | | Small, 5 AIs, T200 (seeds 1-4) | Standard, 7 AIs, T540 (seeds 1-3) |
     |---|---|---|
     | Mean techs of the AIs left | 22.4-24.0 → 23.4-25.6 | 36.7-42.4 → 57.8-65.0 (best 43-49 → 60-66) |
     | The seat (engine_ai) | 22-24 → 23-25 techs | 32-38 → 41-50 techs |
     | Libraries, Marketplaces | 22-67, 3-7 → 44-73, 5-12 | 114-124, 39-54 → 122-139, 87-107 |
     | Universities, Banks, Stock Exchanges | none | 32-43, 1-6, 0 → 70-104, 58-78, 19-36 |
     | Factories, Wall Street | none | 0, 0 → 3-10, 2-4 |
     | Cities captured, civs destroyed | 0-2, 0 → 0-1, 0 | 39-85, 0-1 → 100-117, 2-3 |
     | Wall time per game | 17-21 s → 18-25 s | 167-246 s → 197-227 s |

     The richer AIs reach the Industrial era and fight more decisive wars: more cities change hands and 2 to 3
     civs are destroyed per Standard game, where at most one was.
   - Left out of patch 0016:
     - power plants (Coal, Hydro, Nuclear, Solar, Hoover Dam): +25% with a Factory and one per city, which needs
       a power-plant rule;
     - pollution and everything tied to it (Mass Transit, Recycling Center, meltdowns): the engine has none;
     - Coastal Fortress, SAM sites and naval or bombard defence, which need naval bombard and air units;
     - Smith's Trading Company paying upkeep, the Sistine Chapel's empire-wide cathedral, a Temple doubled by a
       tech, the Courthouse's content face, and the Police Station against war weariness (the engine has none);
     - Great Library techs, Leonardo's upgrades and other wonder specials;
     - the BIQ's integer `Production` field and its `DoublesResearchOutput` flag, which cannot be checked without
       a BIQ (the ruleset here is already converted).
3. **No city capture: fixed** by patch 0011.
   - A city taken changes hands as in Civ III: it loses a citizen, its palace, small wonders, stored food and
     shields; one of size 1 is destroyed. Great wonders and other buildings stay, and borders follow the new owner.
   - In 540-turn Standard games at Regent (seeds 1-6), 46 to 73 cities change hands per game, where 12 to 27
     were destroyed.
   - A civ that loses its capital gets a new one at once, in its largest city (patch 0013).
4. **Diplomacy only went one way: fixed** by patch 0009.
   - An AI now asks a price for peace (`Player.PeacePriceFor`) and makes peace with other AIs.
   - It offers the player peace, and remembers a broken treaty.
   - At Regent, 6 to 15 wars per game end in peace, where none did.
5. **Military topped out around 1700: land and sea fixed** by patches 0018 and 0019.
   - The standalone ruleset kept 27 of 124 units and, by a bug, none of their upgrades, so no unit ever went
     obsolete and 25 civs had lost a unit class to their missing unique units. It now keeps 76: the Industrial and
     Modern land and sea units (Rifleman to Modern Armor, Ironclad to AEGIS Cruiser, the Transport) and the 30
     unique units, drawn with the art of the unit each replaces. A unit leaves a city's options once it could upgrade
     there (the engine's own rule also counted "sibling" lines, so the Archer went at Feudalism with no upgrade).
   - Units upgrade in their own cities for gold (3 per shield of difference), the agent's with the `upgrade` order
     and the AI's garrisons on their own (never into a weaker defender: no Chasqui Scout becomes an Explorer);
     while a city's best defender could upgrade, the AI keeps a tenth of its commerce from science to pay for it.
   - Measured in 540-turn Standard games at Regent (seeds 1-2, engine AI in every seat): 153 and 49 AI units
     upgraded by T540 (Spearmen to Pikemen, Knights to Cavalry, Archers to Longbowmen, Swordsmen to Medieval
     Infantry, Musketmen and Impis to Riflemen, Persia's Warriors to Immortals; none into a weaker unit), Riflemen,
     Musketmen and Cavalry in the field where there were none, mean techs 41.1 and 40.6 (41.9 and 36.1 before:
     within the games' spread), and a similar wall time (197 and 212 s, against 220 and 175).
   - Left out: air units, missiles, nukes, the Carrier, Nuclear Submarine, Flak and Mobile SAM (the engine has no
     air movement, air missions, interception or nuclear attack: L), armies and Great Leaders (no rule spawns or
     uses them: L), Leonardo's Workshop's free upgrade (wonders have no effects yet: M), paradrop, amphibious
     assault and submarine stealth (these units keep their stats only: M each), and the naval AI (item 6).
   - Most later units still need strategic resources few civs have (seed 1 places one Saltpeter and no Coal),
     which is the map generator's to fix.
6. **The AI ignored water: fixed for exploring and settling** by patches 0020-0022.
   - It built no boat (a Curragh never won its city's production choice), settled only its own landmass and moved
     nothing across water; a ship lost at sea left its passengers stranded on the water.
   - Now the AI builds a few ships to explore the ocean (at most min(4, 1 + cities / 6) at a time), ferries a
     settler and its escort to sites on other landmasses, and a ship lost at sea takes its passengers with it (a
     broke AI disbands a loaded ship at sea only after its settlers and workers).
     The agent boards ships (`board`, `unload`) and sees who is aboard what.
   - Measured, 300 turns, Small, 3 AIs, Regent, the engine AI in the seat, seeds 1-3, before and after: on
     Archipelago, 0 → 14-17 boats built per game, 0 → 6-36 cities on another landmass, the seat's known tiles
     390-700 → 2,882-3,027; on Continents, 0 → 16-20 boats, 0 → 1-23 overseas cities, 823-916 → 2,886-2,970 known
     tiles. Wall time went from 22-30 s to 28-42 s a game (1.05-1.43×); no turn hung and no unit was stranded.
   - Still missing (L each): naval warfare (ships attacking ships, bombarding coasts, escorting ferries);
     amphibious invasion and war across water, so on Continents AIs on different landmasses still never fight
     (`WarPriority` skips cities on other continents); AI troop transport beyond one settler and its escort; Civ
     III's coast-only Galley and sinking (here a Galley sails the open ocean safely), transport chaining, carriers
     and submarines; barbarian sea raids (barbarian galleys spawn and never move); the AI's use of Harbors, the
     Great Lighthouse and Magellan's; trade by sea.

**Tools** (what the agent can do):

| Missing | Fix |
|---|---|
| Attack and bombard | **done:** `attack` and `bombard` orders through the engine's combat, with an estimated chance to win |
| Revolution | **done:** the `revolution` tool |
| Diplomacy | **done:** the `diplomacy` tool, with peace at the AI's price and AI offers reported as events, and trades of techs and gold: quoted and judged by the AI's own values, AI offers that stand for a turn, and trades between agents when both agree; a traded tech keeps the progress on other research (patch 0017). Left out: gold per turn (the engine's `TradeOffer` has no form for it and the AI does not value it: an engine change across valuation, bookkeeping and saves, L), maps, luxuries, resources and embassies (no tradeable form in the engine), attitude in the AI's judgement (an engine TODO), tech prerequisites in trades (a tech can be got without the ones it needs: the engine's AIs trade so among themselves and the client's deal screen lists such techs, so the seat follows them), AI offers stopping `end_turn(until_attention)`, and fairer AI-to-AI trades (an AI buys from another without asking it, and `PlayerAI.AttemptTrading` drops the wrong tech when trimming; left as the engine's tech-diffusion knob). Autoplay baselines still decline every AI offer |
| Acting at scale | **done:** `unit_orders` orders many units at once (by id, or every idle unit of a type), `set_production` sets many cities at once (`"all"`, `"pending"`, a list) and takes a queue (`then`) the bridge follows after each completion, `list_units` filters by type, and with many cities the brief folds the engine's picks into one line and `city_info` gives one line per city |
| Seeing rivals | a rivals view and seen foreign cities (S) |
| Crossing water | **done:** `board` (a ship in port, or one on an adjacent water tile) and `unload` (in a city) orders; a passenger lands with `goto` or `settle`; units show `aboard`, ships `capacity` and `cargo`, and `city_sites` for a unit aboard ranks the islands along the ship's waters |
| The date and victory status | **done:** the brief dates each turn, ranks the seat by score and gives its share of the land and population against the civ nearest domination; `data/get` carries the victory |

**Harness** (what breaks at 540 turns):

- **One session per game:** fixed by `--context-cap`.
- **Budget and clock:** set per game. The defaults ($25, 240 minutes) still fit only short games.
- **Resuming:** the env's game lives in a temp dir. Resuming needs the game dir under the run dir,
  `data/add {"load": ...}` and a checkpoint every 50 turns (M).
- **Hand-off between sessions:** the plan is capped at 1,000 characters (S to raise).
- **Gates:** the stall gate and the env's nudge fire at 25 calls a turn, and human-like play needs more.
  The brief's pace targets stop at T100 (S).

## Cost and time

**Engine and bridge, measured in full 540-turn games:**

| | Standard, 8 civs | Huge, 12 civs |
|---|---|---|
| Wall time per game | 62 to 111 s | about 400 s |
| Seconds per turn, early to late | 0.06 to 0.13–0.28 | 0.11 to about 1.0 |
| Memory | flat, about 150 MB | 166 MB |
| Autosave | 1 to 2 MB | 2.5 to 4.7 MB |
| Per-turn record | 27 to 31 KB a turn | about 70 KB a turn |

**Agent, projected for Standard 540 with sessions rotated at 100K context:**

| Play | Calls (per turn) | Sonnet | Opus | Haiku |
|---|---|---|---|---|
| Light, as measured to T100 | 1,202 (2.2) | $12, 0.7 h | $18, 1.0 h | $9, 1.4 h |
| Engaged | 3,487 (6.5) | $34, 1.9 h | $52, 2.8 h | $26, 4.1 h |
| Human-like | 8,865 (16.4) | $88, 4.7 h | $134, 7.0 h | $67, 10.4 h |

- **Context size drives cost, not model choice.** With one session, the same engaged Sonnet game would cost
  about $159.
- **Calls per turn drive the rest.** Cost per call is roughly fixed, so batching tools is the next lever.
- **Projections.** These are calculations from the measured runs (up to T100 and 13 cities); play beyond
  that has not been measured yet.

## A ladder to a full game

| Rung | Settings | Needs | Done when |
|---|---|---|---|
| 1 | Small, 5 AIs, Regent, Roaming, 200 turns | resumable env, a larger plan, `revolution`, a rivals view, the date, the building patch | 3 Sonnet seeds reach T200 over several sessions with no harness error; a run resumes from its autosave; the agent leaves Despotism |
| 2 | Standard, 7 AIs, 300 turns | `attack` and `bombard`, diplomacy with the peace guard, batching, end checks in the bridge | 3 seeds finish or win, none defeated; the brief stays under 2.5K tokens at 20+ cities |
| 3 | Standard, 7 AIs, 540 turns | AI governments and science, AI peace, small wonders, the full ruleset, checkpoints | an engine-only game reaches the Industrial era and ends a war in peace; 3 Sonnet seeds finish T540 for $34 to $88 each |
| 4 | Continents, Huge with 16 civs, space or UN victory | naval transport and an AI that explores and settles across water (done: patches 0020-0022), an AI that fights across water, space and UN in the engine | – |

Rung 3 is what "a full game like a human plays" means. In this engine, turns 300 to 540 change little
until the engine items under rung 3 land.
