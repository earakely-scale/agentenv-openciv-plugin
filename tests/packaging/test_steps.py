"""The task steps against fake envs served over HTTP, with agent-env's local stores."""

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

from agentenv_openciv3.steps import (
    NEW_GAME_EXTENSION,
    RECORDING_EXTENSION,
    OpenCiv3MatchTaskStep,
    SaveEnvRecordingTaskStep,
)

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
class FakeGame(AgentEnvEnvironment):
    def __init__(self):
        self.games = []

    @extension(NEW_GAME_EXTENSION, description="Start a new game.")
    async def new_game(self, **args) -> dict:
        self.games.append(args)
        return {"turn": 1}


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


async def test_a_match_seats_every_deployed_agent_under_its_name(local_stores, caplog):
    env = FakeGame()
    step = OpenCiv3MatchTaskStep(id="match", version=None, env_id="openciv3", turns=10, civs={"sonnet": "Greece"},
                                 ai_opponents=2, landform="Pangaea")
    caplog.set_level(logging.INFO, logger="agentenv_openciv3.steps")
    async with deployed(env) as record:
        context = await step.execute(run_context(record, "opus", "sonnet", "haiku"))

    assert env.games == [{"seed": 1, "size": "Small", "difficulty": "Regent", "barbarians": "Roaming", "turn_limit": 10,
                          "civ": "Greece", "opponents": 4, "seats": ["Rome", "Egypt"],
                          "labels": {"Greece": "sonnet", "Rome": "haiku", "Egypt": "opus"}, "landform": "Pangaea"}]
    live_url = f"{record.environment_url}/live"
    assert context.metadata["openciv3_match"] == {
        "seats": [{"agent": "sonnet", "civ": "Greece"}, {"agent": "haiku", "civ": "Rome"},
                  {"agent": "opus", "civ": "Egypt"}],
        "turn_limit": 10, "live_url": live_url}
    assert any("sonnet plays Greece" in m and live_url in m for m in caplog.messages)


async def test_a_match_seats_only_the_agents_it_names(local_stores):
    env = FakeGame()
    step = OpenCiv3MatchTaskStep(id="match", version=None, env_id="openciv3", turns=5, agents=["opus"],
                                 ai_opponents=1, ocean=70)
    async with deployed(env) as record:
        await step.execute(run_context(record, "opus", "judge"))
    assert [(g["civ"], g["opponents"], g["seats"], g["labels"], g["ocean"]) for g in env.games] == [
        ("Rome", 1, [], {"Rome": "opus"}, 70)]


async def test_a_match_fails_for_an_agent_that_is_not_deployed(local_stores):
    env = FakeGame()
    step = OpenCiv3MatchTaskStep(id="match", version=None, env_id="openciv3", turns=10, civs={"gpt": "Rome"})
    async with deployed(env) as record:
        with pytest.raises(RuntimeError, match=r"agents \['gpt'\] are not deployed"):
            await step.execute(run_context(record, "opus"))
        with pytest.raises(RuntimeError, match="at least one deployed agent"):
            await OpenCiv3MatchTaskStep(id="match", version=None, env_id="openciv3", turns=10).execute(
                run_context(record))
    assert env.games == []


def test_the_match_is_registered_under_its_type_and_round_trips(local_stores):
    cls = get_task_step_registry()["openciv3_match"]
    assert cls is OpenCiv3MatchTaskStep
    step = cls.from_dict({"id": "match", "type": "openciv3_match", "env_id": "openciv3", "turns": 10,
                          "civs": {"opus": "Rome"}, "ocean": 70, "depends_on": ["agent-opus"]})
    again = cls.from_dict(step.to_dict())
    assert again.to_dict() == step.to_dict()
    assert (again.turns, again.civs, again.agents, again.seed, again.size, again.ai_opponents, again.landform,
            again.ocean, again.timeout_seconds, again.fail_task_on_error) == (
        10, {"opus": "Rome"}, None, 1, "Small", 0, None, 70, 120, True)


@pytest.mark.parametrize("task", ["smoke", "play", "full-game", "three-agents", "three-agents-quick", "frontier",
                                  "frontier-quick"])
def test_every_bundle_task_records_after_the_game_alongside_grading(local_stores, task):
    steps = json.loads(files("agentenv_openciv3.bundles").joinpath(f"openciv3/tasks/{task}.json").read_text())
    by_type = {s["type"]: s for s in steps}
    record = get_task_step_registry()["save_env_recording"].from_dict(by_type["save_env_recording"])
    assert record.fail_task_on_error is False
    assert by_type["save_env_recording"]["depends_on"] == by_type["env_outcome_verifier"]["depends_on"]
    players = [s["id"] for s in steps if s["type"] == "prompt_agent"]
    last = players if "match" in {s["id"] for s in steps} else [steps[-3]["id"]]
    assert by_type["save_env_recording"]["depends_on"] == last


def test_three_agents_each_play_their_seat_for_the_whole_game(local_stores):
    steps = json.loads(files("agentenv_openciv3.bundles").joinpath("openciv3/tasks/three-agents.json").read_text())
    registry = get_task_step_registry()
    for s in steps:
        assert registry[s["type"]].from_dict(s).to_dict()["id"] == s["id"]
    by_id = {s["id"]: s for s in steps}
    agents = [s for s in steps if s["type"] == "deploy_agent"]
    assert [(s["agent_name"], s["depends_on"], "env_vars" in s) for s in agents] == [
        (n, ["deploy"], False) for n in ["opus", "sonnet", "haiku"]]
    match = registry["openciv3_match"].from_dict(by_id["match"])
    assert (match.turns, match.agents) == (300, None)
    assert match.civs == {"opus": "Rome", "sonnet": "Greece", "haiku": "Egypt"}
    assert by_id["match"]["depends_on"] == [s["id"] for s in agents]
    players = [s for s in steps if s["type"] == "prompt_agent"]
    assert [(s["id"], s["agent_name"], s["depends_on"]) for s in players] == [
        (n, n, ["match"]) for n in ["opus", "sonnet", "haiku"]]
    assert all(s["agent_name"] in s["model"] and s["prompt"] == players[0]["prompt"] for s in players)
    assert "GAME OVER" in players[0]["prompt"]
    assert (by_id["grade"]["file_artifact_id"], by_id["grade"]["verifier_id"]) == ("victor-verifier", "victor")


def test_frontier_seats_nine_models_on_a_standard_map(local_stores):
    steps = json.loads(files("agentenv_openciv3.bundles").joinpath("openciv3/tasks/frontier.json").read_text())
    registry = get_task_step_registry()
    for s in steps:
        assert registry[s["type"]].from_dict(s).to_dict()["id"] == s["id"]
    match = next(s for s in steps if s["type"] == "openciv3_match")
    players = [s for s in steps if s["type"] == "prompt_agent"]
    agents = [s for s in steps if s["type"] == "deploy_agent"]
    assert (match["turns"], match["size"], len(players)) == (200, "Standard", 9)
    assert set(match["civs"]) == {s["agent_name"] for s in players} == {s["agent_name"] for s in agents}
    assert len(set(match["civs"].values())) == 9 and len({s["model"] for s in players}) == 9
    assert all(s["env_vars"] == {"OPENCIV3_SESSION_TURNS": "40"} for s in agents)
    harness = {s["agent_name"]: s["a2a_agent_id"] for s in agents}
    assert {s["agent_name"]: harness[s["agent_name"]] for s in players if s["model"].startswith("openai/")} == {
        "sol": "openciv3-codex", "luna": "openciv3-codex", "terra": "openciv3-codex"}
    assert harness["gemini"] == "openciv3-gemini" and {harness[n] for n in ("opus", "grok", "kimi")} == {
        "openciv3-claude"}
    assert all(s["prompt"] == players[0]["prompt"] and s["depends_on"] == ["match"] for s in players)


@pytest.mark.parametrize("full_name", ["three-agents", "frontier"])
def test_a_quick_task_is_the_same_match_in_ten_turns(local_stores, full_name):
    tasks = files("agentenv_openciv3.bundles").joinpath("openciv3/tasks")
    full = json.loads(tasks.joinpath(f"{full_name}.json").read_text())
    quick = json.loads(tasks.joinpath(f"{full_name}-quick.json").read_text())
    registry = get_task_step_registry()
    for s in quick:
        assert registry[s["type"]].from_dict(s).to_dict()["id"] == s["id"]
    limits = ["turns", "ttl_seconds", "timeout_seconds"]
    for q, f in zip(quick, full, strict=True):
        assert q == {**f, **{k: q[k] for k in limits if k in f}}
        assert all(q[k] < f[k] for k in limits if k in f)
    assert next(s for s in quick if s["type"] == "openciv3_match")["turns"] == 10
