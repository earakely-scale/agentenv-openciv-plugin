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
from agentenv_protocol import AgentEnvEnvironment, DataPart, client, environment_card, extension, get_data
from agentenv_protocol.types import WELL_KNOWN_PATH

from agentenv_openciv3 import broadcast
from agentenv_openciv3.steps import (
    NEW_GAME_EXTENSION,
    RECORDING_EXTENSION,
    OpenCiv3AwaitGameTaskStep,
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
        return {"turn": 1, **({"play": {civ: f"/play#token={civ.lower():x<32}" for civ in args["humans"]}}
                              if args.get("humans") else {})}


@environment_card(name="openciv3")
class FakeOldGame(AgentEnvEnvironment):
    """An env from before human seats: it takes the args but returns no play links."""

    @extension(NEW_GAME_EXTENSION, description="Start a new game.")
    async def new_game(self, **args) -> dict:
        return {"turn": 1}


@environment_card(name="openciv3")
class FakeSummary(AgentEnvEnvironment):
    """data/get answers from a script: a summary, or an exception to raise, per call; the last one repeats."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    @get_data
    async def summary(self) -> list[DataPart]:
        answer = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        if isinstance(answer, Exception):
            raise answer
        return [DataPart(data=answer)]


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


async def test_a_match_seats_humans_after_the_agents_and_logs_each_play_link(local_stores, caplog):
    env = FakeGame()
    step = OpenCiv3MatchTaskStep(id="match", version=None, env_id="openciv3", turns=100, civs={"opus": "Greece"},
                                 humans={"you": "Rome", "friend": None}, human_turn_seconds=600)
    caplog.set_level(logging.INFO, logger="agentenv_openciv3.steps")
    async with deployed(env) as record:
        context = await step.execute(run_context(record, "opus", "sol"))

    assert env.games == [{"seed": 1, "size": "Small", "difficulty": "Regent", "barbarians": "Roaming",
                          "turn_limit": 100, "civ": "Greece", "opponents": 3, "seats": ["Egypt", "Rome", "Babylon"],
                          "labels": {"Greece": "opus", "Egypt": "sol", "Rome": "you", "Babylon": "friend"},
                          "humans": ["Rome", "Babylon"], "human_turn_seconds": 600}]
    base = record.environment_url
    match = context.metadata["openciv3_match"]
    assert match["seats"] == [{"agent": "opus", "civ": "Greece"}, {"agent": "sol", "civ": "Egypt"},
                              {"agent": "you", "civ": "Rome", "human": True},
                              {"agent": "friend", "civ": "Babylon", "human": True}]
    assert match["play"] == {"you": {"civ": "Rome", "url": f"{base}/play#token={'rome':x<32}"},
                             "friend": {"civ": "Babylon", "url": f"{base}/play#token={'babylon':x<32}"}}
    plays = [r for r in caplog.records if r.getMessage().startswith("PLAY ")]
    assert [(r.levelno, r.getMessage()) for r in plays] == [
        (logging.WARNING, f"PLAY Rome (you): {base}/play#token={'rome':x<32}"),
        (logging.WARNING, f"PLAY Babylon (friend): {base}/play#token={'babylon':x<32}")]


async def test_a_match_may_seat_only_humans_and_names_them_from_a_list(local_stores):
    env = FakeGame()
    step = OpenCiv3MatchTaskStep(id="match", version=None, env_id="openciv3", turns=100, humans=["you"],
                                 ai_opponents=3)
    async with deployed(env) as record:
        context = await step.execute(run_context(record))
    assert [(g["civ"], g["opponents"], g["seats"], g["labels"], g["humans"], g["human_turn_seconds"])
            for g in env.games] == [("Rome", 3, [], {"Rome": "you"}, ["Rome"], 900)]
    assert context.metadata["openciv3_match"]["play"]["you"]["civ"] == "Rome"


async def test_a_human_is_not_an_agent(local_stores):
    env = FakeGame()
    async with deployed(env) as record:
        with pytest.raises(RuntimeError, match=r"\['opus'\] are named as both agents and humans"):
            await OpenCiv3MatchTaskStep(id="match", version=None, env_id="openciv3", turns=10,
                                        humans={"opus": "Rome"}, civs={"opus": "Greece"}).execute(
                run_context(record, "opus"))
        with pytest.raises(RuntimeError, match=r"\['Rome'\] are each named for more than one seat"):
            await OpenCiv3MatchTaskStep(id="match", version=None, env_id="openciv3", turns=10,
                                        humans={"you": "Rome"}, civs={"opus": "Rome"}).execute(
                run_context(record, "opus"))
        # A deployed agent that a human shares a name with is not seated as an agent.
        await OpenCiv3MatchTaskStep(id="match", version=None, env_id="openciv3", turns=10, humans=["opus"]).execute(
            run_context(record, "opus"))
    assert [(g["civ"], g["labels"], g["humans"]) for g in env.games] == [("Rome", {"Rome": "opus"}, ["Rome"])]


async def test_a_match_with_humans_fails_on_an_env_that_returns_no_play_links(local_stores):
    step = OpenCiv3MatchTaskStep(id="match", version=None, env_id="openciv3", turns=10, humans={"you": "Rome"})
    async with deployed(FakeOldGame()) as record:
        with pytest.raises(RuntimeError, match=r"returned no play link for \['Rome'\]"):
            await step.execute(run_context(record))


async def test_a_match_paced_for_a_broadcast_asks_the_env_for_min_turn_seconds(local_stores):
    env = FakeGame()
    async with deployed(env) as record:
        for pace in (15, 0):
            await OpenCiv3MatchTaskStep(id="match", version=None, env_id="openciv3", turns=10,
                                        min_turn_seconds=pace).execute(run_context(record, "opus", "sol"))
    assert [g.get("min_turn_seconds") for g in env.games] == [15, None]


def test_a_paced_match_round_trips(local_stores):
    cls = get_task_step_registry()["openciv3_match"]
    step = cls.from_dict({"id": "match", "type": "openciv3_match", "env_id": "openciv3", "turns": 10,
                          "min_turn_seconds": 20})
    again = cls.from_dict(step.to_dict())
    assert again.to_dict() == step.to_dict() and again.min_turn_seconds == 20
    assert cls.from_dict({"id": "m", "type": "openciv3_match", "env_id": "e", "turns": 1}).min_turn_seconds == 0


async def test_a_match_names_its_broadcast_for_the_env(local_stores, tmp_path):
    env = FakeGame()
    show = {"title": "Showmatch", "casters": {"model": "openai/gpt-5.6-luna", "analyst": {"name": "Iris"}}}
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 16
    (tmp_path / "modal.png").write_bytes(png)
    ad = {**show, "banners": [{"text": "Powered by {modal} Modal", "logos": {"modal": str(tmp_path / "modal.png")}}]}
    async with deployed(env) as record:
        for value in (show, None, ad):
            step = OpenCiv3MatchTaskStep(id="match", version=None, env_id="openciv3", turns=10, broadcast=value)
            await step.execute(run_context(record, "opus", "sol"))
    # the env gets each banner logo inlined, the step keeps the task's source
    uri = f"data:image/png;base64,{base64.b64encode(png).decode()}"
    inlined = {**ad, "banners": [{**ad["banners"][0], "logos": {"modal": uri}}]}
    assert [g.get("broadcast") for g in env.games] == [show, None, inlined]
    assert step.broadcast == ad


def test_a_broadcast_match_round_trips_and_a_bad_broadcast_fails_when_the_task_loads(local_stores):
    cls = get_task_step_registry()["openciv3_match"]
    data = {"id": "match", "type": "openciv3_match", "env_id": "openciv3", "turns": 10,
            "broadcast": {"title": "Showmatch", "casters": True}}
    again = cls.from_dict(cls.from_dict(data).to_dict())
    assert again.to_dict() == cls.from_dict(data).to_dict() and again.broadcast == data["broadcast"]
    assert cls.from_dict({"id": "m", "type": "openciv3_match", "env_id": "e", "turns": 1}).broadcast is None
    with pytest.raises(ValueError, match="broadcast casters must be true, false or an object"):
        cls.from_dict({**data, "broadcast": {"casters": "loud"}})


def test_a_match_with_humans_round_trips(local_stores):
    cls = get_task_step_registry()["openciv3_match"]
    for humans in ({"you": "Rome"}, ["you", "friend"]):
        step = cls.from_dict({"id": "match", "type": "openciv3_match", "env_id": "openciv3", "turns": 10,
                              "humans": humans, "human_turn_seconds": 0})
        again = cls.from_dict(step.to_dict())
        assert again.to_dict() == step.to_dict()
        assert (again.humans, again.human_turn_seconds) == (humans, 0)
    assert cls.from_dict({"id": "m", "type": "openciv3_match", "env_id": "e", "turns": 1}).human_turn_seconds == 900


def game(turn: int, game_over: bool = False, **more) -> dict:
    return {"turn": turn, "turn_limit": 3, "game_over": game_over, "defeated": False, "victory": None,
            "engine_failed": False, **more}


async def test_await_game_polls_until_the_game_is_over_through_failures(local_stores, caplog):
    env = FakeSummary(game(1), RuntimeError("busy"), RuntimeError("busy"), game(1), game(2), game(3, True))
    step = OpenCiv3AwaitGameTaskStep(id="await", version=None, env_id="openciv3", poll_seconds=0.01)
    caplog.set_level(logging.INFO, logger="agentenv_openciv3.steps")
    async with deployed(env) as record:
        context = await step.execute(run_context(record))
    assert env.calls == 6
    assert context.metadata["openciv3_game"] == {"turn": 3, "turn_limit": 3, "victory": None, "engine_failed": False}
    messages = [m for m in caplog.messages if m.startswith("openciv3_await_game")]
    assert [m for m in messages if "turn " in m and "over" not in m] == [
        "openciv3_await_game: turn 1 of 3", "openciv3_await_game: turn 2 of 3", "openciv3_await_game: turn 3 of 3"]
    assert sum("retrying" in m for m in messages) == 1


@pytest.mark.parametrize("summary", [
    game(2, victory={"kind": "conquest", "civ": "Rome", "label": "you", "turn": 2}),
    game(2, engine_failed=True),
    game(2, defeated=True),
    game(2, seats=[{"civ": "Rome", "defeated": True}, {"civ": "Greece", "defeated": True}]),
])
async def test_await_game_ends_on_a_victory_a_failed_engine_or_every_seat_defeated(local_stores, summary):
    env = FakeSummary(summary)
    step = OpenCiv3AwaitGameTaskStep(id="await", version=None, env_id="openciv3", poll_seconds=0.01)
    async with deployed(env) as record:
        await step.execute(run_context(record))
    assert env.calls == 1


async def test_await_game_times_out_and_keeps_waiting_while_one_seat_is_left(local_stores):
    env = FakeSummary(game(2, seats=[{"civ": "Rome", "defeated": True}, {"civ": "Greece", "defeated": False}]))
    step = OpenCiv3AwaitGameTaskStep(id="await", version=None, env_id="openciv3", timeout_seconds=0.2,
                                     poll_seconds=0.05)
    async with deployed(env) as record:
        with pytest.raises(TimeoutError, match=r"not over after 0.2s \(turn 2\)"):
            await step.execute(run_context(record))
    assert env.calls >= 2


async def test_await_game_times_out_on_an_env_that_never_answers(local_stores):
    step = OpenCiv3AwaitGameTaskStep(id="await", version=None, env_id="openciv3", timeout_seconds=0.2,
                                     poll_seconds=0.05)
    async with deployed(FakeSummary(RuntimeError("down"))) as record:
        with pytest.raises(TimeoutError, match=r"\(turn None\)"):
            await step.execute(run_context(record))


def test_await_game_is_registered_under_its_type_and_round_trips(local_stores):
    cls = get_task_step_registry()["openciv3_await_game"]
    assert cls is OpenCiv3AwaitGameTaskStep
    step = cls.from_dict({"id": "await", "type": "openciv3_await_game", "env_id": "openciv3", "poll_seconds": 5,
                          "fail_task_on_error": False, "depends_on": ["match"]})
    again = cls.from_dict(step.to_dict())
    assert again.to_dict() == step.to_dict()
    assert (again.env_id, again.timeout_seconds, again.poll_seconds, again.fail_task_on_error) == (
        "openciv3", 36000, 5, False)


@pytest.mark.parametrize("task", ["smoke", "play", "full-game", "three-agents", "three-agents-quick", "frontier",
                                  "frontier-quick", "showmatch", "showmatch-quick", "livestream", "sol-vs-opus",
                                  "five-way-war", "human-vs-ai",
                                  "human-vs-agents"])
def test_every_bundle_task_records_after_the_game_alongside_grading(local_stores, task):
    steps = json.loads(files("agentenv_openciv3.bundles").joinpath(f"openciv3/tasks/{task}.json").read_text())
    by_type = {s["type"]: s for s in steps}
    record = get_task_step_registry()["save_env_recording"].from_dict(by_type["save_env_recording"])
    assert record.fail_task_on_error is False
    assert by_type["save_env_recording"]["depends_on"] == by_type["env_outcome_verifier"]["depends_on"]
    players = [s["id"] for s in steps if s["type"] in ("openciv3_await_game", "prompt_agent")]
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


@pytest.mark.parametrize(("task", "turns", "size", "pace", "sessions"), [
    ("showmatch", 50, "Tiny", 15, None), ("livestream", 140, "Small", 45, {"OPENCIV3_SESSION_TURNS": "40"})])
def test_the_broadcast_tasks_seat_three_labs_paced_and_cast_for_a_stream(local_stores, task, turns, size, pace,
                                                                         sessions):
    steps = json.loads(files("agentenv_openciv3.bundles").joinpath(f"openciv3/tasks/{task}.json").read_text())
    registry = get_task_step_registry()
    for s in steps:
        assert registry[s["type"]].from_dict(s).to_dict()["id"] == s["id"]
    match = registry["openciv3_match"].from_dict(next(s for s in steps if s["type"] == "openciv3_match"))
    assert (match.turns, match.size, match.min_turn_seconds) == (turns, size, pace)
    assert match.civs == {"opus": "Rome", "sol": "America", "kimi": "China"}
    assert broadcast.settings(match.broadcast)["casters"] == {
        "model": "anthropic/claude-sonnet-5-5", "tts_model": "openai/gpt-4o-mini-tts",
        "play_by_play": {"name": "Max", "voice": "ash"}, "analyst": {"name": "Ada", "voice": "sage"}}
    assert match.broadcast["title"].endswith("Opus 5.5 vs GPT-6 Sol vs Kimi K3")
    agents = {s["agent_name"]: s for s in steps if s["type"] == "deploy_agent"}
    assert {n: a["a2a_agent_id"] for n, a in agents.items()} == {
        "opus": "openciv3-claude", "sol": "openciv3-codex", "kimi": "openciv3-claude"}
    assert all(a.get("env_vars") == sessions for a in agents.values())
    players = [s for s in steps if s["type"] == "prompt_agent"]
    assert {s["agent_name"]: s["model"] for s in players} == {
        "opus": "anthropic/claude-opus-5-5", "sol": "openai/gpt-6-sol", "kimi": "bedrock/global.moonshotai.kimi-k3"}
    assert all(s["prompt"] == players[0]["prompt"] and s["depends_on"] == ["match"] for s in players)
    assert all(w in players[0]["prompt"] for w in ("broadcast live", "message tool", "end_turn a note", "GAME OVER"))


def test_sol_vs_opus_is_won_by_conquest_among_ai_civilizations(local_stores):
    steps = json.loads(files("agentenv_openciv3.bundles").joinpath("openciv3/tasks/sol-vs-opus.json").read_text())
    registry = get_task_step_registry()
    for s in steps:
        assert registry[s["type"]].from_dict(s).to_dict()["id"] == s["id"]
    match = registry["openciv3_match"].from_dict(next(s for s in steps if s["type"] == "openciv3_match"))
    assert (match.turns, match.size, match.ai_opponents, match.min_turn_seconds) == (750, "Tiny", 3, 30)
    assert match.civs == {"opus": "Rome", "sol": "America"}
    assert broadcast.settings(match.broadcast)["title"] == (
        "GPT-6 Sol Battles Opus 5.5 in Civilization 3: which model is the best?")
    players = [s for s in steps if s["type"] == "prompt_agent"]
    assert {s["agent_name"]: s["model"] for s in players} == {
        "opus": "anthropic/claude-opus-5-5", "sol": "openai/gpt-6-sol"}
    assert all(s["prompt"] == players[0]["prompt"] for s in players)
    assert all(w in players[0]["prompt"] for w in ("conquer the other model's civilization", "only a tiebreaker",
                                                   "game's own AI", "GAME OVER"))


def test_five_way_war_seats_five_models_and_no_ai_until_one_conquers_the_rest(local_stores):
    steps = json.loads(files("agentenv_openciv3.bundles").joinpath("openciv3/tasks/five-way-war.json").read_text())
    registry = get_task_step_registry()
    for s in steps:
        assert registry[s["type"]].from_dict(s).to_dict()["id"] == s["id"]
    match = registry["openciv3_match"].from_dict(next(s for s in steps if s["type"] == "openciv3_match"))
    settings = (match.turns, match.size, match.ai_opponents, match.barbarians, match.min_turn_seconds)
    assert settings == (750, "Tiny", 0, "Restless", 55)
    assert match.civs == {"astra": "America", "sol": "England", "terra": "Persia", "opus": "Rome", "sonnet": "Greece"}
    shown = broadcast.settings(match.broadcast)
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


@pytest.mark.parametrize("task", ["human-vs-ai", "human-vs-agents"])
def test_a_human_task_seats_you_and_waits_for_the_game(local_stores, task):
    steps = json.loads(files("agentenv_openciv3.bundles").joinpath(f"openciv3/tasks/{task}.json").read_text())
    registry = get_task_step_registry()
    for s in steps:
        assert registry[s["type"]].from_dict(s).to_dict()["id"] == s["id"]
    by_id = {s["id"]: s for s in steps}
    match = registry["openciv3_match"].from_dict(by_id["match"])
    assert (match.humans, match.turns, match.size) == ({"you": "Rome"}, 100, "Small")
    wait = registry["openciv3_await_game"].from_dict(by_id["await"])
    assert (wait.depends_on[0].task_step_id, wait.fail_task_on_error) == ("match", False)
    agents = [s for s in steps if s["type"] == "deploy_agent"]
    players = [s for s in steps if s["type"] == "prompt_agent"]
    assert {s["agent_name"] for s in agents} == {s["agent_name"] for s in players} == set(match.civs or {})
    assert "you" not in (match.civs or {}) and all(s["depends_on"] == ["match"] for s in players)
    if task == "human-vs-ai":
        assert (match.ai_opponents, agents) == (3, [])
        assert by_id["grade"]["file_artifact_id"] == "full-game-verifier"
    else:
        assert {s["agent_name"]: s["a2a_agent_id"] for s in agents} == {"opus": "openciv3-claude",
                                                                       "sol": "openciv3-codex"}
        assert by_id["grade"]["file_artifact_id"] == "victor-verifier"
        assert all("GAME OVER" in s["prompt"] for s in players)
