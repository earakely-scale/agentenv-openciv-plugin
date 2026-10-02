"""The plugin's task steps: `openciv3_match` starts a game for the deployed agents, `save_env_recording` stores a
deployed env's game recording as file artifacts (docs/recording.md)."""

from __future__ import annotations

import asyncio
import base64
import logging
import uuid
from typing import ClassVar

from agent_env.artifact import FileArtifact
from agent_env.entity_refs import EntityRef
from agent_env.env.env import DeployedEnv
from agent_env.task_step.context import TaskStepContext
from agent_env.task_step.task_step import TaskStep
from agentenv_protocol import client

log = logging.getLogger(__name__)

RECORDING_EXTENSION = "urn:openciv3:recording/v1"
NEW_GAME_EXTENSION = "urn:openciv3:new-game/v1"
DEFAULT_CIVS = ["Rome", "Greece", "Egypt", "Babylon", "Germany", "Russia", "China", "America", "Japan", "France",
                "India", "Persia"]
MATCH_OPTIONS = ("civs", "agents", "seed", "size", "difficulty", "barbarians", "ai_opponents", "landform", "ocean",
                 "timeout_seconds")


def _deployed_env(context: TaskStepContext, env_id: str) -> DeployedEnv:
    deployed = next((d for d in context.deployed_envs if d.env_id == env_id), None)
    if deployed is None:
        raise RuntimeError(f"env {env_id!r} is not deployed in this run")
    return deployed


def _extension_card(deployed: DeployedEnv, uri: str) -> dict:
    """The card that advertises ``uri``: the env's own, else one of its children (an env behind a gateway)."""
    own = deployed.environment_card or {}
    card = next((c for c in [own, *(own.get("children_environments") or [])] if client.find_extension(c, uri)), None)
    if card is None:
        raise RuntimeError(f"env {deployed.env_id!r} does not advertise {uri}")
    return card


class OpenCiv3MatchTaskStep(TaskStep):
    """Start a game with one seat per deployed agent; the env knows each agent by its name, the seat's label."""

    type: ClassVar[str] = "openciv3_match"
    entity_refs = (EntityRef.env("env_id"),)

    def __init__(self, id: str, version: int | None, env_id: str, turns: int, civs: dict[str, str] | None = None,
                 agents: list[str] | None = None, seed: int = 1, size: str = "Small", difficulty: str = "Regent",
                 barbarians: str = "Roaming", ai_opponents: int = 0, landform: str | None = None,
                 ocean: int | None = None, timeout_seconds: int = 120, depends_on: list | None = None,
                 fail_task_on_error: bool = True):
        super().__init__(id, version, depends_on=depends_on, fail_task_on_error=fail_task_on_error)
        self.env_id = env_id
        self.turns = turns
        self.civs = dict(civs) if civs is not None else None
        self.agents = list(agents) if agents is not None else None
        self.seed = seed
        self.size = size
        self.difficulty = difficulty
        self.barbarians = barbarians
        self.ai_opponents = ai_opponents
        self.landform = landform
        self.ocean = ocean
        self.timeout_seconds = timeout_seconds

    def to_dict(self) -> dict:
        return {**super().to_dict(), "env_id": self.env_id, "turns": self.turns,
                **{k: getattr(self, k) for k in MATCH_OPTIONS}}

    @classmethod
    def from_dict(cls, data: dict) -> OpenCiv3MatchTaskStep:
        return cls(**cls._base_from_dict(data), env_id=data["env_id"], turns=data["turns"],
                   **{k: data[k] for k in MATCH_OPTIONS if k in data})

    async def execute(self, context: TaskStepContext) -> TaskStepContext:
        deployed = _deployed_env(context, self.env_id)
        card = _extension_card(deployed, NEW_GAME_EXTENSION)
        civs = self.civs or {}
        present = [a.agent_name for a in context.deployed_agents]
        names = self.agents if self.agents is not None else present
        missing = [n for n in [*civs, *names] if n not in present]
        if missing:
            raise RuntimeError(f"agents {missing} are not deployed in this run (deployed: {present})")
        order = [*civs, *sorted(set(names) - set(civs))]
        if not order:
            raise RuntimeError("openciv3_match needs at least one deployed agent")
        free = (c for c in DEFAULT_CIVS if c not in civs.values())
        seats = [{"agent": n, "civ": civs.get(n) or next(free)} for n in order]
        first, *others = seats
        args = {"seed": self.seed, "size": self.size, "difficulty": self.difficulty, "barbarians": self.barbarians,
                "turn_limit": self.turns, "civ": first["civ"], "opponents": len(others) + self.ai_opponents,
                "seats": [s["civ"] for s in others], "labels": {s["civ"]: s["agent"] for s in seats},
                **{k: v for k, v in [("landform", self.landform), ("ocean", self.ocean)] if v is not None}}
        await client.invoke_extension(deployed.environment_url, card, NEW_GAME_EXTENSION, args,
                                      timeout=self.timeout_seconds)
        live_url = f"{deployed.mcp_url.removesuffix('/mcp')}/live"
        context.metadata["openciv3_match"] = {"seats": seats, "turn_limit": self.turns, "live_url": live_url}
        log.info("openciv3_match: %s for %d turns; watch it live at %s",
                 ", ".join(f"{s['agent']} plays {s['civ']}" for s in seats), self.turns, live_url)
        return context


class SaveEnvRecordingTaskStep(TaskStep):
    """Ask a deployed env for its recording and store each returned file as a ``file`` artifact."""

    type: ClassVar[str] = "save_env_recording"
    entity_refs = (EntityRef.env("env_id"),)

    def __init__(self, id: str, version: int | None, env_id: str, extension_uri: str = RECORDING_EXTENSION,
                 formats: list[str] | None = None, view: str = "spectator", timeout_seconds: int = 300,
                 depends_on: list | None = None, fail_task_on_error: bool = False):
        super().__init__(id, version, depends_on=depends_on, fail_task_on_error=fail_task_on_error)
        self.env_id = env_id
        self.extension_uri = extension_uri
        self.formats = list(formats) if formats is not None else None
        self.view = view
        self.timeout_seconds = timeout_seconds

    def to_dict(self) -> dict:
        return {**super().to_dict(), "env_id": self.env_id, "extension_uri": self.extension_uri,
                "formats": self.formats, "view": self.view, "timeout_seconds": self.timeout_seconds}

    @classmethod
    def from_dict(cls, data: dict) -> SaveEnvRecordingTaskStep:
        return cls(**{**cls._base_from_dict(data), "fail_task_on_error": data.get("fail_task_on_error", False)},
                   env_id=data["env_id"], extension_uri=data.get("extension_uri", RECORDING_EXTENSION),
                   formats=data.get("formats"), view=data.get("view", "spectator"),
                   timeout_seconds=data.get("timeout_seconds", 300))

    async def execute(self, context: TaskStepContext) -> TaskStepContext:
        deployed = _deployed_env(context, self.env_id)
        card = _extension_card(deployed, self.extension_uri)
        args = {"view": self.view, **({"formats": self.formats} if self.formats is not None else {})}
        result = await client.invoke_extension(deployed.environment_url, card, self.extension_uri, args,
                                               timeout=self.timeout_seconds)
        for note in result.get("notes") or []:
            log.warning("save_env_recording: %s", note)
        if not result.get("files"):
            raise RuntimeError(f"{self.extension_uri} on env {self.env_id!r} returned no files")
        stem = f"{context.metadata.get('task_id', self.env_id)}-recording-{context.instance_id or uuid.uuid4().hex}"
        saved = []
        for f in result["files"]:
            content = base64.b64decode(f["base64"])
            artifact = await asyncio.to_thread(
                FileArtifact.put_bytes, f"{stem}.{f['name'].partition('.')[2]}",
                description=f"Recording of env {self.env_id!r}: {f['name']}", filename=f["name"],
                content=content, content_type=f["content_type"])
            saved.append({"name": f["name"], "artifact_id": artifact.id, "version": artifact.version,
                          "bytes": len(content), "content_type": f["content_type"]})
            log.info("save_env_recording: %s (%d bytes) is file artifact %s v%d at %s",
                     f["name"], len(content), artifact.id, artifact.version, artifact.object_url)
        context.metadata.setdefault("recordings", {})[self.id] = saved
        return context
