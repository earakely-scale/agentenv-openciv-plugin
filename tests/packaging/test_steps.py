"""save_env_recording against a fake env served over HTTP, with agent-env's local stores."""

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
from agent_env.task_step.context import TaskStepContext
from agent_env.task_step.registry import get_task_step_registry
from agentenv_protocol import AgentEnvEnvironment, client, environment_card, extension
from agentenv_protocol.types import WELL_KNOWN_PATH

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


def run_context(record: DeployedEnv) -> TaskStepContext:
    return TaskStepContext(deployed_envs=[record], metadata={"task_id": "smoke"}, instance_id="i1")


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


@pytest.mark.parametrize("task", ["smoke", "play", "full-game", "three-agents"])
def test_every_bundle_task_records_after_the_game_alongside_grading(local_stores, task):
    steps = json.loads(files("agentenv_openciv3.bundles").joinpath(f"openciv3/tasks/{task}.json").read_text())
    by_type = {s["type"]: s for s in steps}
    record = get_task_step_registry()["save_env_recording"].from_dict(by_type["save_env_recording"])
    assert record.fail_task_on_error is False
    assert by_type["save_env_recording"]["depends_on"] == by_type["env_outcome_verifier"]["depends_on"]
    last = ["opus-4", "sonnet-4", "haiku-4"] if task == "three-agents" else [steps[-3]["id"]]
    assert by_type["save_env_recording"]["depends_on"] == last


def test_three_agents_each_play_their_seat_in_their_own_sessions(local_stores):
    steps = json.loads(files("agentenv_openciv3.bundles").joinpath("openciv3/tasks/three-agents.json").read_text())
    registry = get_task_step_registry()
    for s in steps:
        assert registry[s["type"]].from_dict(s).to_dict()["id"] == s["id"]
    game = steps[1]["directives"][0]["args"]
    assert (game["civ"], game["opponents"], game["seats"], game["turn_limit"]) == ("Rome", 2, ["Greece", "Egypt"], 300)
    agents = {s["agent_name"]: s["env_vars"]["OPENCIV3_SEAT"] for s in steps if s["type"] == "deploy_agent"}
    assert agents == {"opus": "Rome", "sonnet": "Greece", "haiku": "Egypt"}
    for name, civ in agents.items():
        sessions = [s for s in steps if s["type"] == "prompt_agent" and s["agent_name"] == name]
        assert [s["depends_on"] for s in sessions] == [[f"agent-{name}"]] + [[s["id"]] for s in sessions[:-1]]
        assert all(s["prompt"].startswith(f"You lead {civ} ") and name in s["model"] for s in sessions)
        assert [s["prompt"].rsplit("until turn ", 1)[1][:3] for s in sessions] == ["75;", "150", "225", "300"]
