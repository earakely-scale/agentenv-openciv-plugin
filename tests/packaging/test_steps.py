"""The plugin's task step against fake envs served over HTTP, with agent-env's local stores, and the bundle's tasks as
agent-env loads them."""

import asyncio
import base64
import json
import logging
import socket
from contextlib import asynccontextmanager
from importlib.resources import files

import pytest
import uvicorn
from agent_env.artifact import FileArtifact
from agent_env.env.env import DeployedEnv
from agent_env.task_step.context import DeployedAgent, TaskStepContext
from agent_env.task_step.registry import get_task_step_registry
from agentenv_protocol import AgentEnvEnvironment, client, environment_card, extension
from agentenv_protocol.types import WELL_KNOWN_PATH

from agentenv_openciv3 import broadcast
from agentenv_openciv3.steps import RECORDING_EXTENSION, SaveEnvRecordingTaskStep

pytestmark = pytest.mark.anyio

CONTENT_TYPES = {"mp4": "video/mp4", "html": "text/html", "gif": "image/gif", "png": "image/png"}


@environment_card(name="openciv3")
class FakeRecorder(AgentEnvEnvironment):
    def __init__(self):
        self.requests = []

    @extension(RECORDING_EXTENSION, description="Render the recording so far.")
    async def recording(self, formats: list | None = None, view: str = "spectator", fps: int = 4) -> dict:
        self.requests.append({"formats": formats, "view": view})
        out = [(f"openciv3-seed3.{fmt.replace('client_mp4', 'client.mp4')}", f"{fmt} of {view}".encode())
               for fmt in formats or ["mp4", "html"]]
        return {"turns": 3, "files": [{"name": name, "content_type": CONTENT_TYPES[name.rpartition(".")[2]],
                                       "bytes": len(data), "base64": base64.b64encode(data).decode()}
                                      for name, data in out]}


@environment_card(name="openciv3")
class Silent(AgentEnvEnvironment):
    pass


@asynccontextmanager
async def deployed(env: AgentEnvEnvironment, compose: bool = False):
    """The env served on a free port, as the record a deploy_env step leaves; behind a gateway when ``compose``."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(env.create_app().streamable_http_app(), host="127.0.0.1", port=port,
                                           log_level="warning"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    base = f"http://127.0.0.1:{port}"
    card = await client.get_card(base)
    if compose:
        card = {"name": "gateway", "children_environments": [card]}
    try:
        yield DeployedEnv(env_id="openciv3", env_version=1, environment_card_url=base + WELL_KNOWN_PATH,
                          environment_card=card)
    finally:
        server.should_exit = True
        await serving


def run_context(record: DeployedEnv, *agents: str) -> TaskStepContext:
    return TaskStepContext(deployed_envs=[record], metadata={"task_id": "smoke"}, instance_id="i1",
                           deployed_agents=[DeployedAgent(agent_name=a, api_url=f"http://{a}") for a in agents])


async def test_each_file_becomes_a_file_artifact(local_stores, caplog):
    env = FakeRecorder()
    step = SaveEnvRecordingTaskStep(id="recording", version=None, env_id="openciv3", view="agent")
    caplog.set_level(logging.INFO, logger="agentenv_openciv3.steps")
    async with deployed(env) as record:
        context = await step.execute(run_context(record))

    assert env.requests == [{"formats": None, "view": "agent"}]
    saved = context.metadata["recordings"]["recording"]
    assert [(f["name"], f["artifact_id"], f["version"], f["content_type"]) for f in saved] == [
        ("openciv3-seed3.mp4", "smoke-recording-i1.mp4", 1, "video/mp4"),
        ("openciv3-seed3.html", "smoke-recording-i1.html", 1, "text/html"),
    ]
    for f, ext in zip(saved, ["mp4", "html"], strict=True):
        artifact = FileArtifact.get(f["artifact_id"], f["version"])
        assert artifact.load() == f"{ext} of agent".encode()
        assert f["bytes"] == len(artifact.load())
        assert artifact.object_url.startswith(f"file://{local_stores.resolve()}")
        assert any(artifact.id in m and artifact.object_url in m for m in caplog.messages)


async def test_two_videos_keep_distinct_artifact_ids(local_stores):
    step = SaveEnvRecordingTaskStep(id="recording", version=None, env_id="openciv3", formats=["mp4", "client_mp4"])
    async with deployed(FakeRecorder()) as record:
        context = await step.execute(run_context(record))
    assert [f["artifact_id"] for f in context.metadata["recordings"]["recording"]] == [
        "smoke-recording-i1.mp4", "smoke-recording-i1.client.mp4"]


async def test_finds_the_extension_on_a_child_env_behind_a_gateway(local_stores):
    step = SaveEnvRecordingTaskStep(id="recording", version=None, env_id="openciv3", formats=["gif"])
    async with deployed(FakeRecorder(), compose=True) as record:
        context = await step.execute(run_context(record))
    assert [f["artifact_id"] for f in context.metadata["recordings"]["recording"]] == ["smoke-recording-i1.gif"]


async def test_an_env_without_the_extension_fails_the_step_but_not_the_task(local_stores):
    step = SaveEnvRecordingTaskStep(id="recording", version=None, env_id="openciv3")
    async with deployed(Silent()) as record:
        with pytest.raises(RuntimeError, match=f"does not advertise {RECORDING_EXTENSION}"):
            await step.execute(run_context(record))
    assert step.fail_task_on_error is False


def test_registered_under_its_type_and_round_trips(local_stores):
    cls = get_task_step_registry()["save_env_recording"]
    assert cls is SaveEnvRecordingTaskStep
    step = cls.from_dict({"id": "r", "type": "save_env_recording", "env_id": "openciv3", "formats": ["png"],
                          "timeout_seconds": 60, "depends_on": ["game"]})
    again = cls.from_dict(step.to_dict())
    assert again.to_dict() == step.to_dict()
    assert (again.formats, again.view, again.timeout_seconds, again.fail_task_on_error) == (["png"], "spectator", 60,
                                                                                            False)


DEFAULT_GAMES = ["smoke", "play", "full-game"]
LOBBY_GAMES = ["three-agents", "three-agents-quick", "frontier", "frontier-quick", "showmatch", "showmatch-quick",
               "livestream", "sol-vs-opus", "five-way-war", "human-vs-ai", "human-vs-agents"]


def task(name: str) -> list[dict]:
    steps = json.loads(files("agentenv_openciv3.bundles").joinpath(f"openciv3/tasks/{name}.json").read_text())
    registry = get_task_step_registry()
    for s in steps:
        assert registry[s["type"]].from_dict(s).to_dict()["id"] == s["id"]
    return steps


def lobby(steps: list[dict]) -> tuple[dict, dict[str, dict]]:
    """A task's game settings and its player slots, by player_id."""
    settings = next(s for s in steps if s["type"] == "open_lobby")["game_settings"]
    return settings, {s["player_id"]: s for s in steps if s["type"] == "add_player_slot"}


@pytest.mark.parametrize("name", DEFAULT_GAMES)
def test_a_default_game_task_records_after_the_game_alongside_grading(local_stores, name):
    steps = task(name)
    by_type = {s["type"]: s for s in steps}
    assert get_task_step_registry()["save_env_recording"].from_dict(by_type["save_env_recording"]).fail_task_on_error \
        is False
    assert by_type["save_env_recording"]["depends_on"] == by_type["env_outcome_verifier"]["depends_on"] == [
        steps[-3]["id"]]


@pytest.mark.parametrize("name", LOBBY_GAMES)
def test_a_lobby_task_seats_each_player_plays_it_out_grades_it_and_keeps_its_files(local_stores, name):
    steps = task(name)
    by_id = {s["id"]: s for s in steps}
    _, slots = lobby(steps)
    agents = {s["agent_name"]: s for s in steps if s["type"] == "deploy_agent"}
    assert all(a["env_ids"] == [] for a in agents.values())   # each plays at its player slot's address
    for name_, a in agents.items():
        slot = slots[name_]
        assert (slot["player_kind"], slot["player_name"], slot["depends_on"]) == ("agent", name_, ["lobby", a["id"]])
    assert by_id["lobby"]["depends_on"] == ["deploy"] and by_id["match"]["type"] == "close_lobby"
    assert by_id["match"]["depends_on"] == [s["id"] for s in steps if s["type"] == "add_player_slot"]
    players = [s for s in steps if s["type"] == "prompt_agent"]
    assert {s["agent_name"] for s in players} == set(agents) and all(s["depends_on"] == ["match"] for s in players)
    waits = [s["id"] for s in steps if s["type"] == "await_match"]
    assert by_id["finish"]["type"] == "finish_match"
    assert sorted(by_id["finish"]["depends_on"]) == sorted([*waits, *(s["id"] for s in players)])
    assert (by_id["files"]["type"], by_id["files"]["depends_on"], by_id["files"].get("fail_task_on_error")) == (
        "save_match_files", ["finish"], None)
    assert by_id["grade"]["depends_on"] == ["finish"]
    if by_id["grade"]["type"] == "grade_match":
        assert by_id["grade"]["max_missed"] == 0.1    # a player whose turns the env ended past a tenth scores 0


def test_three_agents_each_play_their_civilization_for_the_whole_game(local_stores):
    steps = task("three-agents")
    settings, slots = lobby(steps)
    assert settings == {"turn_limit": 300}
    assert {p: s["game_settings"]["civ"] for p, s in slots.items()} == {"opus": "Rome", "sonnet": "Greece",
                                                                      "haiku": "Egypt"}
    players = [s for s in steps if s["type"] == "prompt_agent"]
    assert all(s["agent_name"] in s["model"] and s["prompt"] == players[0]["prompt"] for s in players)
    assert "GAME OVER" in players[0]["prompt"]
    assert "player_ids" not in next(s for s in steps if s["id"] == "grade")   # every agent


def test_frontier_seats_nine_models_on_a_standard_map(local_stores):
    steps = task("frontier")
    settings, slots = lobby(steps)
    players = [s for s in steps if s["type"] == "prompt_agent"]
    agents = [s for s in steps if s["type"] == "deploy_agent"]
    assert (settings["turn_limit"], settings["size"], len(players)) == (200, "Standard", 9)
    assert set(slots) == {s["agent_name"] for s in players} == {s["agent_name"] for s in agents}
    assert len({s["game_settings"]["civ"] for s in slots.values()}) == 9 and len({s["model"] for s in players}) == 9
    assert all(s["env_vars"] == {"OPENCIV3_SESSION_TURNS": "40"} for s in agents)
    harness = {s["agent_name"]: s["a2a_agent_id"] for s in agents}
    assert {s["agent_name"]: harness[s["agent_name"]] for s in players if s["model"].startswith("openai/")} == {
        "sol": "openciv3-codex", "luna": "openciv3-codex", "terra": "openciv3-codex"}
    assert harness["gemini"] == "openciv3-gemini" and {harness[n] for n in ("opus", "grok", "kimi")} == {
        "openciv3-claude"}
    assert all(s["prompt"] == players[0]["prompt"] for s in players)


@pytest.mark.parametrize(("name", "turns", "size", "pace", "sessions"), [
    ("showmatch", 50, "Tiny", 15, None), ("livestream", 140, "Small", 45, {"OPENCIV3_SESSION_TURNS": "40"})])
def test_the_broadcast_tasks_seat_three_labs_paced_and_cast_for_a_stream(local_stores, name, turns, size, pace,
                                                                         sessions):
    steps = task(name)
    settings, slots = lobby(steps)
    assert (settings["turn_limit"], settings["size"], settings["min_turn_seconds"]) == (turns, size, pace)
    assert {p: s["game_settings"]["civ"] for p, s in slots.items()} == {"opus": "Rome", "sol": "America",
                                                                      "kimi": "China"}
    assert broadcast.settings(settings["broadcast"])["casters"] == {
        "model": "anthropic/claude-sonnet-5-5", "tts_model": "openai/gpt-4o-mini-tts",
        "play_by_play": {"name": "Max", "voice": "ash"}, "analyst": {"name": "Ada", "voice": "sage"}}
    assert settings["broadcast"]["title"].endswith("Opus 5.5 vs GPT-6 Sol vs Kimi K3")
    agents = {s["agent_name"]: s for s in steps if s["type"] == "deploy_agent"}
    assert {n: a["a2a_agent_id"] for n, a in agents.items()} == {
        "opus": "openciv3-claude", "sol": "openciv3-codex", "kimi": "openciv3-claude"}
    assert all(a.get("env_vars") == sessions for a in agents.values())
    players = [s for s in steps if s["type"] == "prompt_agent"]
    assert {s["agent_name"]: s["model"] for s in players} == {
        "opus": "anthropic/claude-opus-5-5", "sol": "openai/gpt-6-sol", "kimi": "bedrock/global.moonshotai.kimi-k3"}
    assert all(s["prompt"] == players[0]["prompt"] for s in players)
    assert all(w in players[0]["prompt"] for w in ("broadcast live", "message tool", "end_turn a note", "GAME OVER"))


def test_sol_vs_opus_is_won_by_conquest_among_ai_civilizations(local_stores):
    steps = task("sol-vs-opus")
    settings, slots = lobby(steps)
    assert (settings["turn_limit"], settings["size"], settings["min_turn_seconds"]) == (750, "Tiny", 30)
    assert {p: (s["player_kind"], s.get("game_settings", {}).get("civ")) for p, s in slots.items()} == {
        "opus": ("agent", "Rome"), "sol": ("agent", "America"), "ai1": ("ai", None), "ai2": ("ai", None),
        "ai3": ("ai", None)}
    assert broadcast.settings(settings["broadcast"])["title"] == (
        "GPT-6 Sol Battles Opus 5.5 in Civilization 3: which model is the best?")
    players = [s for s in steps if s["type"] == "prompt_agent"]
    assert {s["agent_name"]: s["model"] for s in players} == {
        "opus": "anthropic/claude-opus-5-5", "sol": "openai/gpt-6-sol"}
    assert all(s["prompt"] == players[0]["prompt"] for s in players)
    assert all(w in players[0]["prompt"] for w in ("conquer the other model's civilization", "only a tiebreaker",
                                                   "game's own AI", "GAME OVER"))


def test_five_way_war_seats_five_models_and_no_ai_until_one_conquers_the_rest(local_stores):
    steps = task("five-way-war")
    settings, slots = lobby(steps)
    assert settings == {"turn_limit": 750, "size": "Tiny", "barbarians": "Restless", "min_turn_seconds": 55,
                        "broadcast": settings["broadcast"]}
    assert {p: s["game_settings"]["civ"] for p, s in slots.items()} == {
        "astra": "America", "sol": "England", "terra": "Persia", "opus": "Rome", "sonnet": "Greece"}
    shown = broadcast.settings(settings["broadcast"])
    assert shown["title"].startswith("Five AI models at war in Civilization 3")
    assert [(b["text"], b["theme"], sorted(b["logos"])) for b in shown["banners"]] == [
        ("Powered by {modal} Modal Sandboxes with {agentenv} AgentEnv Framework", "light", ["agentenv", "modal"])]
    agents = {s["agent_name"]: s for s in steps if s["type"] == "deploy_agent"}
    assert {n: a["a2a_agent_id"] for n, a in agents.items()} == {
        "astra": "openciv3-codex", "sol": "openciv3-codex", "terra": "openciv3-codex", "opus": "openciv3-claude",
        "sonnet": "openciv3-claude"}
    assert {n: a["env_vars"]["OPENCIV3_SESSION_TURNS"] for n, a in agents.items()} == {
        "astra": "20", "sol": "30", "terra": "30", "opus": "30", "sonnet": "30"}
    assert all(a["ttl_seconds"] == 36000 for a in agents.values())   # an 8-hour broadcast, with room to spare
    players = [s for s in steps if s["type"] == "prompt_agent"]
    assert {s["agent_name"]: s["model"] for s in players} == {
        "astra": "openai/gpt-6-astra", "sol": "openai/gpt-6-sol", "terra": "openai/gpt-5.6-terra",
        "opus": "anthropic/claude-opus-5-5", "sonnet": "anthropic/claude-sonnet-5-5"}
    assert all(s["prompt"] == players[0]["prompt"] and s["timeout_seconds"] == 36000 for s in players)
    assert all(w in players[0]["prompt"] for w in ("four other AI models", "conquering the other civilizations your "
                                                   "ultimate goal", "the game ends only when you have done so",
                                                   "GAME OVER"))
    assert "tiebreaker" not in players[0]["prompt"]


@pytest.mark.parametrize("full_name", ["three-agents", "frontier", "showmatch"])
def test_a_quick_task_is_the_same_match_in_ten_turns(local_stores, full_name):
    full, quick = task(full_name), task(f"{full_name}-quick")
    limits = ["ttl_seconds", "timeout_seconds"]
    for q, f in zip(quick, full, strict=True):
        if q["type"] == "open_lobby":
            assert q["game_settings"] == {**f["game_settings"], "turn_limit": 10}
            assert f["game_settings"]["turn_limit"] > 10
            continue
        assert q == {**f, **{k: q[k] for k in limits if k in f}}
        assert all(q[k] < f[k] for k in limits if k in f)


@pytest.mark.parametrize("name", ["human-vs-ai", "human-vs-agents"])
def test_a_human_task_seats_you_and_waits_for_the_game(local_stores, name):
    steps = task(name)
    by_id = {s["id"]: s for s in steps}
    settings, slots = lobby(steps)
    assert (settings["turn_limit"], settings["size"]) == (100, "Small")
    assert (slots["you"]["player_kind"], slots["you"]["game_settings"]) == ("human", {"civ": "Rome"})
    wait = get_task_step_registry()["await_match"].from_dict(by_id["await"])
    assert (wait.depends_on[0].task_step_id, wait.fail_task_on_error) == ("match", False)
    agents = [s for s in steps if s["type"] == "deploy_agent"]
    if name == "human-vs-ai":
        assert agents == [] and [p for p, s in slots.items() if s["player_kind"] == "ai"] == ["ai1", "ai2", "ai3"]
        assert by_id["grade"]["file_artifact_id"] == "full-game-verifier"
    else:
        assert {s["agent_name"]: s["a2a_agent_id"] for s in agents} == {"opus": "openciv3-claude",
                                                                       "sol": "openciv3-codex"}
        assert "player_ids" not in by_id["grade"]    # every agent, a tenth of its turns missed at most
        assert (by_id["grade-you"]["player_ids"], by_id["grade-you"].get("max_missed")) == (["you"], None)
        assert all("GAME OVER" in s["prompt"] for s in steps if s["type"] == "prompt_agent")
