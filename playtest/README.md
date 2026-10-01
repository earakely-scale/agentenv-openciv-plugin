# Playtest harness

Claude Code plays the OpenCiv3 env through its MCP tools, and the harness measures whether it
operates the game well. The harness uses only the Python standard library (3.11+) and a logged-in
`claude` CLI; the env runs as its own process (by default `python -m agentenv_openciv3.server`).

```bash
python playtest/run.py --seed 1 --turns 60 --model sonnet                  # one game
python playtest/batch.py --seeds 1,2,3 --turns 60 --model sonnet --parallel 3   # the acceptance batch
python playtest/baseline.py --seeds 1,2,3 --turns 60 --out baselines.json  # baselines only, no LLM
python playtest/analyze.py playtest/runs/<batch>/seed-1                    # re-analyze one run
python playtest/batch.py --name <batch> --seeds 1,2,3 --turns 60 --report-only
```

`--server-cmd` takes the placeholders `{port}`, `{seed}`, `{turns}` and `{run_dir}`, so a Docker image
works too:
`--server-cmd "docker run --rm -p 127.0.0.1:{port}:18765 -e OPENCIV_SEED={seed} -e OPENCIV_TURN_LIMIT={turns} -e OPENCIV_ACTION_LOG=/run/actions.jsonl -v {run_dir}:/run openciv3"`.
`--url` attaches to an env that is already running (`--actions` points at its action log).

## One run (`run.py`)

1. Start the env on a free port with `MCP_HOST=127.0.0.1`, `MCP_PORT`, `OPENCIV_SEED`,
   `OPENCIV_TURN_LIMIT` and `OPENCIV_ACTION_LOG`; wait for `/.well-known/agent-env.json`; `data/reset`.
2. Write `mcp.json` (HTTP, the card's MCP interface) and start one `claude -p` with stream-json in
   and out, `--tools ""` and `--allowedTools "mcp__openciv3__*"` (the env's tools are the only tools),
   `--strict-mcp-config`, `--setting-sources ""`, `--permission-mode dontAsk`, `--max-turns` per
   segment, `--max-budget-usd` and `--no-session-persistence`. The prompts come from `prompt.md`.
3. The run is a harness error (exit 2) unless claude's init event shows the MCP server connected.
4. After each result, `data/get`: while the game is not over, send
   `Game not over (turn X/Y). Continue playing.` Stop on game over, an exhausted budget,
   `--max-nudges`, `--max-stalled-nudges` segments in a row without the turn advancing, or
   `--timeout-min`.
5. Final `data/get`, stop the env's process group, remove claude's `~/.claude/projects/<slug>`.

Run directory: `transcript.jsonl` (claude's stream), `actions.jsonl` (the env's action log),
`summary.json` (final `data/get`), `meta.json`, `metrics.json`, `server.log`, `claude.stderr`.

## Metrics and gate (`analyze.py`)

| Metric | Definition |
|---|---|
| error rate | tool results with `isError` / tool calls, from the transcript (includes argument errors that never reach the env; `env_log.error_rate` is the env-side rate) |
| max consecutive errors | longest run of failing calls; flag `error_streak` at 5 |
| repeated failures | most times one identical failing call (tool and args) was made in one game turn; flag `repeat` at 3 |
| stall turns | game turns with 25 or more calls (from the action log); flag `stall`, also when the run stopped as stalled |
| no-op end_turns | `end_turn` with units needing orders and no `unit_order`/`set_production`/`research` since the last one; flag `passive` above half the turns |
| also | blocked end_turns, nudges, cost (`total_cost_usd`), wall and API time, tokens, calls and cost per turn, cities/pop/techs, score against the baselines |

A run passes when it is valid, reaches the turn limit undefeated, has an error rate below 15%, no
`stall` or `repeat` flag, and scores above the null baseline. The batch gate needs every run to pass
(acceptance: 3 seeds x 60 turns); the stretch goal is a score at or above the built-in AI on 2/3 of
the seeds. Baselines come from `baseline.py` (the env's autoplay extension, `null` and `engine_ai`,
same seed and turn limit), else from the baselines the env reports in `data/get`.

## Testing the harness without the engine

`stub_env.py` serves the same tool names, card, data plane, autoplay extension and action log over
a toy game (needs `mcp>=1.25,<2`):

```bash
python playtest/run.py --seed 1 --turns 5 --model haiku --budget-usd 0.15 --server-cmd "python playtest/stub_env.py"
pytest playtest      # offline, with a fake claude: no LLM cost
```
