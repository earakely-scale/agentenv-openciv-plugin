OpenCiv3: play a seeded Civilization III-style game through MCP tools and grade it against same-seed baselines.

Both tasks deploy the env registered as `openciv3` (run `agent-env openciv3 setup` once) and start
a seeded game through the env's `urn:openciv3:new-game/v1` extension.

- `smoke` needs no model: OpenCiv3's own AI plays your seat for 30 turns through
  `urn:openciv3:autoplay/v1`, then the outcome verifier grades the game. It checks the image, the
  bridge, the extensions and the verifier end to end.
- `play` deploys the A2A agent registered as `openciv3-player` and prompts it to play 50 turns.
  Register any agent that advertises the MCP config extension under that id
  (`agent-env a2a-agent put --id openciv3-player ...`) and configure a model endpoint
  (`LITELLM_BASE_URL`, `LITELLM_API_KEY`) before running it.

`artifacts/outcome-verifier/verify.py` reads the env's `data/get` summary and scores: the turn
limit was reached, the civ was not defeated, at least one city was founded, the score beats the
do-nothing baseline at the same turn, and the score as a fraction of the built-in AI's score at the
same turn (graded, weight 2).

Run with `agent-env run openciv3 --task smoke` or `agent-env run openciv3 --task play --model <model>`.
