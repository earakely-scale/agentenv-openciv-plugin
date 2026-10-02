# Earlier results

The playtests that shaped the env, before the agent-env tasks existed: Claude Code drove the env's tools directly
through the playtest harness ([playtest/README.md](../playtest/README.md)). Current results are in the
[README](../README.md#results).

## A full game at Civilization III's own settings, before the engine fixes

On 2026-10-01 Sonnet 5.5 played a whole game: Standard map (100×100), 7 AI civs, Regent, roaming
barbarians, 540 turns (4000 BC to AD 2050), seed 1. The harness rotated sessions at a 100K context cap
(`playtest/run.py --context-cap`), and `playtest/longgame.py` wrote the report.

| At T540 | Agent (Rome) | Best AI civ (Arabia) | Built-in AI in Rome's seat | `settler_bot` in Rome's seat |
|---|---|---|---|---|
| Score | **4,106** | 1,149 | 667 | 453 |
| Cities | 120 | 24 | 12 | 9 |
| Population | 549 | 149 | 73 | 59 |
| Tiles | 1,123 | 330 | 188 | 86 |
| Techs (of 83) | 34 | 33 | 35 | 25 |

- **It led the game.** Rome ranked first of the 8 civs on 483 of 541 turns. It ended with 58% of the
  world's land and 62% of its population. Civ III's domination victory needs two-thirds of each; this
  engine has no victory conditions, so the game ran to the turn limit.
- **The AIs fought each other, and the agent filled the space.**
  - Four AI civs were destroyed in AI wars: Spain at T225, the Mongols at T372, Zululand at T385 and the
    Hittites at T490.
  - The agent founded 110 cities, many on their ruins.
  - Arabia declared war on Rome at T482 and razed 3 of its cities; the agent has no way to attack.
- **Weak spots:**
  - Techs are level with the AIs; no civ leaves the ancient era in this engine.
  - Cities starved 100 times, and 21 fell into disorder.
  - In a 120-city empire, the agent accepted the engine's pick for 58% of production items.
- **Cost:**
  - 37 minutes of play and $22.31 for 1,499 tool calls (2.8 a turn, 3.1% invalid), over 4 sessions.
  - No turn was autoplayed.
  - The real-client and map videos took another 17 minutes to render.

[docs/full-game.md](full-game.md) covers what a full game still lacks.

## Round 2: graded against `settler_bot`

Gate v2: beat `settler_bot` on the same seed in at least 2 of 3 runs, with fewer than 10% invalid calls
and no autoplay. Score per seed (agent / `settler_bot` / `engine_ai`):

| Batch | Seed 1 or 4 | Seed 2 or 5 | Seed 3 or 6 | Gate v2 | $ per game |
|---|---|---|---|---|---|
| Sonnet, Tiny, 3 rivals, 60 turns | 179 / 156 / 134 | 163 / 125 / 141 | 166 / 131 / 155 | pass, 3 of 3 | 0.29–0.35 |
| Haiku, Tiny, 3 rivals, 60 turns | 195 / 156 / 134 | 137 / 125 / 141 | 179 / 131 / 155 | pass, 3 of 3 | 0.25–0.27 |
| Sonnet, Small, 5 rivals, 100 turns | 295 / 275 / 240 | 318 / 271 / 201 | 371 / 291 / 294 | pass, 3 of 3 | 1.08–1.30 |

## Round 1

Claude Code through the playtest harness, one game per seed, same-seed baselines. Tiny map,
3 rivals, 60 turns (seeds 1-3); Small map, 5 rivals, roaming barbarians, 100 turns (seeds 4-6).
Score (cities):

| Seed | Sonnet | Haiku | `null` | `found_capital` | scripted settler bot | `engine_ai` |
|---|---|---|---|---|---|---|
| 1 | 168 (5) | 71 (1) | 8 | 58 | 164 (5) | 134 (3) |
| 2 | 161 (6) | 57 (1) | 8 | 54 | 171 (6) | 141 (4) |
| 3 | 176 (5) | 64 (1) | 8 | 62 | 159 (5) | 155 (4) |
| 4 | 341 (12) | – | 8 | 81 | 366 (12) | 240 (7) |
| 5 | 344 (11) | – | 8 | 74 | 375 (12) | 201 (5) |
| 6 | 255 (7) | – | 8 | 78 | 384 (13) | 294 (9) |

What the audit of these games found:
- **A scripted bot matched Sonnet.** The bot (from the audit, the forerunner of `settler_bot`) beat
  Sonnet on 4 of 6 seeds and `engine_ai` on 6 of 6. Sonnet's wins over `engine_ai` came mostly from
  the env's unescorted settling and the score formula, not from better play.
- **The engine made many of the agent's decisions.** It chose 55% of the production items Sonnet's
  cities completed, nearly all research after the first pick, and every worker and explorer move.
- **Haiku's one-city games were an env trap.** Every Haiku capital rioted at size 3, because an
  empty city riots at the size a Settler needs, and nothing told the agent how to stop it.
- Sonnet cost $0.15-0.16 per 60-turn game and $0.39-0.50 per 100-turn game, with 2-7% failed calls;
  10 of its 17 failures were harness artefacts (bare tool names copied from the prompt) or a
  since-fixed name mismatch.

Round 2 adds the levers, signals and baselines these findings called for (`set_rates`, `buy`, riot
warnings and fixes, reported engine picks, `settler_bot`), and grades against `settler_bot`.
