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
  - There is no victory condition, and a game ends only at the turn limit or when the agent is defeated.
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
| City capture | yes | yes (patch 0011): the city loses a citizen, its palace and small wonders; one of size 1 is destroyed |
| Late eras | Industrial and Modern | nobody reaches them. Bank, University and Cathedral are never buildable, because of a prerequisite check (`Building.cs:145`) |

## What is missing

**Engine** (OpenCiv3 itself), in order of impact:

1. **No ending.**
   - The turn loop is `while (true)`, score is a TODO, and the ruleset's turn limit is never read.
   - Conquest, domination and score-at-the-limit checks can live in the bridge (M).
   - Space race and UN need the engine (L).
2. **Tech and economy stall: fixed** by patches 0005-0008.
   - The causes: 15 buildings were blocked by a prerequisite check, small wonders were blocked, the AI never
     changed government, the AI cut its own science to 0, and harder difficulties made AI research dearer.
   - Now, at Regent: mean AI techs at T540 went from 32 to 43-45, 3 to 7 civs reach the Industrial era, and
     most AIs end in Republic or Democracy.
   - Still slow: buildings have no economic effect in this engine (a Library adds no science), which is a
     rules change left for later (M).
3. **No city capture: fixed** by patch 0011.
   - A city taken changes hands as in Civ III: it loses a citizen, its palace, small wonders, stored food and
     shields; one of size 1 is destroyed. Great wonders and other buildings stay, and borders follow the new owner.
   - In 540-turn Standard games at Regent (seeds 1-6), 46 to 73 cities change hands per game, where 12 to 27
     were destroyed.
   - Still missing: a civ that loses its capital does not get a new palace.
4. **Diplomacy only went one way: fixed** by patch 0009.
   - An AI now asks a price for peace (`Player.PeacePriceFor`) and makes peace with other AIs.
   - It offers the player peace, and remembers a broken treaty.
   - At Regent, 6 to 15 wars per game end in peace, where none did.
5. **Military tops out around 1700.** The standalone ruleset keeps 27 of 124 units, and there are no
   upgrades, air units or armies (S to switch the ruleset, M to L for the rest).
6. **The AI ignores water**, so play on Pangaea only (L).

**Tools** (what the agent can do):

| Missing | Fix |
|---|---|
| Attack and bombard | **done:** `attack` and `bombard` orders through the engine's combat, with an estimated chance to win |
| Revolution | **done:** the `revolution` tool |
| Diplomacy | **done for war and peace:** the `diplomacy` tool, with peace at the AI's price and AI offers reported as events. Trading techs and gold is not done (M) |
| Acting at scale | at T300 all 13 cities waited on a production choice, and the brief cuts off at 6 cities; needs orders for many units, production queues and filtered views (M) |
| Seeing rivals | a rivals view and seen foreign cities (S) |
| The date and victory status | in the brief and `data/get` (S) |

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
| 4 | Continents, Huge with 16 civs, space or UN victory | naval transport, an AI that crosses water, space and UN in the engine | – |

Rung 3 is what "a full game like a human plays" means. In this engine, turns 300 to 540 change little
until the engine items under rung 3 land.
