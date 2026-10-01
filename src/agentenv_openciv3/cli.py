"""`agent-env openciv3`: register the OpenCiv3 env in the store, or serve it locally without Docker."""

import os
import subprocess
import sys
from pathlib import Path

import click
from agent_env.artifact import DockerImageArtifact
from agent_env.env import MCPServerEnv

ENVIRONMENT_NAME = "openciv3"
REPO = "https://github.com/earakely-scale/agentenv-openciv-plugin"


@click.group()
def openciv3():
    """OpenCiv3: build and register the env, or serve it locally."""


def _checkout(source: Path | None) -> Path:
    """The checkout of this repo to build from: ``source``, else the one an editable install runs from, else the cwd."""
    for root in [source] if source else [Path(__file__).resolve().parents[2], Path.cwd()]:
        if (root / "Dockerfile").is_file() and (root / "bridge").is_dir():
            if not (root / "vendor/OpenCiv3/C7Engine").is_dir():
                raise click.ClickException(f"{root / 'vendor/OpenCiv3'} is empty; run: git -C {root} submodule update --init")
            return root
    if source:
        raise click.UsageError(f"{source} is not a checkout of agentenv-openciv-plugin (no Dockerfile and bridge/)")
    raise click.UsageError(f"no checkout of agentenv-openciv-plugin found; clone {REPO} and pass --source")


def _docker_platform() -> str:
    """The Docker host's own platform: the local `server` provider pulls images without emulation."""
    try:
        out = subprocess.run(["docker", "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"],
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise click.ClickException(f"docker is not available: {e}") from e
    if out.returncode:
        raise click.ClickException(f"docker is not running: {out.stderr.strip()}")
    return out.stdout.strip()


@openciv3.command()
@click.option("--id", "env_id", default="openciv3", show_default=True,
              help="Env id to register. The bundle's tasks deploy 'openciv3'.")
@click.option("--source", type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Checkout of this repo to build. Default: the one an editable install runs from, or the cwd.")
@click.option("--platform", "build_platform",
              help="Platform to build for. Default: the Docker host's (linux/arm64 on Apple Silicon); "
                   "remote sandboxes need linux/amd64.")
@click.option("--image", help="Register this existing local image instead of building one.")
def setup(env_id: str, source: Path | None, build_platform: str | None, image: str | None):
    """Build the env image and register it as an MCP server env on the `server` provider."""
    if image is None:
        root = _checkout(source)
        image = f"mcp-server-{env_id}"
        build_platform = build_platform or _docker_platform()
        click.echo(f"Building {image} for {build_platform} from {root}")
        if subprocess.run(["docker", "build", "--platform", build_platform, "-t", image, str(root)]).returncode:
            raise click.ClickException("docker build failed")
    click.echo("Storing the image (docker save, can take a minute)")
    artifact = DockerImageArtifact.put(id=f"mcp-server-{env_id}", description="OpenCiv3 env: CivBridge and its MCP server",
                                       image_name=image)
    env = MCPServerEnv.put(id=env_id, docker_image_artifact=artifact, environment_name=ENVIRONMENT_NAME,
                           env_provider_type="server")
    click.echo(f"Registered env {env.id!r} version {env.version} (image {artifact.image_name})")
    click.echo("Next: agent-env run openciv3 --task smoke")


@openciv3.command()
@click.option("--bridge", envvar="CIVBRIDGE_CMD",
              help="Command that starts CivBridge. Default: $CIVBRIDGE_CMD, else build/bridge/CivBridge in the checkout.")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=18765, show_default=True)
@click.option("--seed", type=int, help="Seed for the first game (OPENCIV_SEED).")
@click.option("--turn-limit", type=int, help="Turn limit (OPENCIV_TURN_LIMIT).")
@click.option("--action-log", type=click.Path(dir_okay=False), help="Append one JSON line per tool call here.")
def serve(bridge: str | None, host: str, port: int, seed: int | None, turn_limit: int | None, action_log: str | None):
    """Run the env on this machine without Docker, for development and for playing it from an MCP client."""
    if not bridge:
        built = Path(__file__).resolve().parents[2] / "build/bridge/CivBridge"
        if not built.is_file():
            raise click.UsageError("no bridge: run scripts/build-bridge.sh in the checkout, or set CIVBRIDGE_CMD / --bridge")
        bridge = str(built)
    settings = {"CIVBRIDGE_CMD": bridge, "MCP_HOST": host, "MCP_PORT": port, "OPENCIV_SEED": seed,
                "OPENCIV_TURN_LIMIT": turn_limit, "OPENCIV_ACTION_LOG": action_log}
    os.environ.update({k: str(v) for k, v in settings.items() if v is not None})
    click.echo(f"OpenCiv3 MCP on http://{host}:{port}/mcp  (claude mcp add --transport http openciv3 "
               f"http://{host}:{port}/mcp)", err=True)
    os.execv(sys.executable, [sys.executable, "-m", "agentenv_openciv3.server"])
