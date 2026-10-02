"""`agent-env openciv3`: register the OpenCiv3 env, serve it locally without Docker, watch a game live, and fetch game
recordings."""

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import click
from agent_env.a2a_agent import A2AAgent
from agent_env.artifact import DockerImageArtifact, FileArtifact
from agent_env.env import MCPServerEnv

ENVIRONMENT_NAME = "openciv3"
PLAYER_ID = "openciv3-claude"
REPO = "https://github.com/earakely-scale/agentenv-openciv-plugin"
ENV_PORT = re.compile(r":(\d+)->18765/tcp")


@click.group()
def openciv3():
    """OpenCiv3: build and register the env, serve it locally, and fetch game recordings."""


def _checkout(source: Path | None) -> Path:
    """The checkout of this repo to build from: ``source``, else the one an editable install runs from, else the cwd."""
    for root in [source] if source else [Path(__file__).resolve().parents[2], Path.cwd()]:
        if (root / "Dockerfile").is_file() and (root / "bridge").is_dir():
            if not (root / "vendor/OpenCiv3/C7Engine").is_dir():
                raise click.ClickException(
                    f"{root / 'vendor/OpenCiv3'} is empty; run: git -C {root} submodule update --init vendor/OpenCiv3")
            return root
    if source:
        raise click.UsageError(f"{source} is not a checkout of agentenv-openciv-plugin (no Dockerfile and bridge/)")
    raise click.UsageError(f"no checkout of agentenv-openciv-plugin found; clone {REPO} and pass --source")


def _docker(*args: str) -> str:
    try:
        out = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise click.ClickException(f"docker is not available: {e}") from e
    if out.returncode:
        raise click.ClickException(f"docker is not running: {out.stderr.strip()}")
    return out.stdout.strip()


def _docker_platform() -> str:
    """The Docker host's own platform: the local `server` provider pulls images without emulation."""
    return _docker("version", "--format", "{{.Server.Os}}/{{.Server.Arch}}")


@openciv3.command()
@click.option("--id", "env_id", default="openciv3", show_default=True,
              help="Env id to register. The bundle's tasks deploy 'openciv3'.")
@click.option("--source", type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Checkout of this repo to build. Default: the one an editable install runs from, or the cwd.")
@click.option("--platform", "build_platform",
              help="Platform to build for. Default: the Docker host's (linux/arm64 on Apple Silicon); "
                   "remote sandboxes need linux/amd64.")
@click.option("--image", help="Register this existing local image instead of building one.")
@click.option("--client", is_flag=True,
              help="Also install the real OpenCiv3 client, so recordings include its view (client_mp4). Adds about "
                   "350 MB and fetches OpenCiv3's community art, which carries no license: don't push the image to a "
                   "public registry.")
@click.option("--agent", is_flag=True,
              help=f"Also build and register the Claude player agent ({PLAYER_ID}, agents/claude-player), which plays "
                   "the bundle's tasks with Claude Code.")
def setup(env_id: str, source: Path | None, build_platform: str | None, image: str | None, client: bool, agent: bool):
    """Build the env image and register it as an MCP server env on the `server` provider."""
    if image is None:
        root = _checkout(source)
        image = f"mcp-server-{env_id}"
        build_platform = build_platform or _docker_platform()
        with_client = " with the OpenCiv3 client" if client else ""
        click.echo(f"Building {image} for {build_platform} from {root}{with_client}")
        if subprocess.run(["docker", "buildx", "version"], capture_output=True).returncode:
            raise click.ClickException("Docker's buildx plugin is required for the image builds. Docker Desktop has "
                                       "it; on Debian or Ubuntu: sudo apt-get install docker-buildx")
        target = ["--target", "client"] if client else []
        build = ["docker", "build", "--platform", build_platform, *target, "-t", image, str(root)]
        if subprocess.run(build).returncode:
            raise click.ClickException("docker build failed")
    click.echo("Storing the image (docker save, can take a minute)")
    artifact = DockerImageArtifact.put(id=f"mcp-server-{env_id}", image_name=image,
                                       description="OpenCiv3 env: CivBridge and its MCP server")
    env = MCPServerEnv.put(id=env_id, docker_image_artifact=artifact, environment_name=ENVIRONMENT_NAME,
                           env_provider_type="server")
    click.echo(f"Registered env {env.id!r} version {env.version} (image {artifact.image_name})")
    if env_id == ENVIRONMENT_NAME:
        click.echo("Next: agent-env run openciv3 --task smoke")
    else:
        click.echo(f"The openciv3 bundle's tasks deploy the env 'openciv3'; your own tasks can deploy {env_id!r}.")
    if agent:
        _register_player(_checkout(source), build_platform or _docker_platform())


def _register_player(root: Path, build_platform: str) -> None:
    """Build agents/claude-player and register it as the A2A agent PLAYER_ID."""
    context = root / "agents" / "claude-player"
    image = f"a2a-agent-{PLAYER_ID}"
    click.echo(f"Building {image} for {build_platform} from {context}")
    if subprocess.run(["docker", "build", "--platform", build_platform, "-t", image, str(context)]).returncode:
        raise click.ClickException("docker build of the player agent failed")
    artifact = DockerImageArtifact.put(id=image, image_name=image, description="OpenCiv3 Claude player agent",
                                       build_context_path=str(context), dockerfile_path=str(context / "Dockerfile"))
    player = A2AAgent.put(id=PLAYER_ID, docker_image_artifact=artifact, metadata={"default_model": "sonnet"})
    click.echo(f"Registered A2A agent {player.id!r} version {player.version}. The bundle's agent tasks deploy the "
               f'default agent: set [agents] default_a2a_agent_id = "{PLAYER_ID}" in .agentenv/config.toml, and the '
               "model endpoint in [model] (base_url, api_key).")


def _check_bridge(bridge: str) -> None:
    """Start the bridge once, so a missing .NET runtime is reported here rather than on the agent's first call."""
    try:
        out = subprocess.run(shlex.split(bridge), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        ready = json.loads(out.stdout.splitlines()[0])["ok"] if out.stdout else False
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        raise click.ClickException(f"the bridge {bridge} did not start: {e}") from e
    if not ready:
        hint = ("\nA bridge built by scripts/build-bridge.sh needs the .NET 8 runtime: set DOTNET_ROOT to the .NET "
                "install it was built with." if "install .NET" in out.stderr else "")
        raise click.ClickException(f"the bridge {bridge} did not start:\n{(out.stdout + out.stderr).strip()}{hint}")


@openciv3.command()
@click.option("--bridge", envvar="CIVBRIDGE_CMD",
              help="Command that starts CivBridge. Default: $CIVBRIDGE_CMD, else build/bridge/CivBridge in the "
                   "checkout.")
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
            raise click.UsageError("no bridge: run scripts/build-bridge.sh in the checkout, or set CIVBRIDGE_CMD or "
                                   "--bridge")
        bridge = shlex.quote(str(built))
    _check_bridge(bridge)
    settings = {"CIVBRIDGE_CMD": bridge, "MCP_HOST": host, "MCP_PORT": port, "OPENCIV_SEED": seed,
                "OPENCIV_TURN_LIMIT": turn_limit, "OPENCIV_ACTION_LOG": action_log}
    os.environ.update({k: str(v) for k, v in settings.items() if v is not None})
    click.echo(f"OpenCiv3 MCP on http://{host}:{port}/mcp  (claude mcp add --transport http openciv3 "
               f"http://{host}:{port}/mcp)", err=True)
    os.execv(sys.executable, [sys.executable, "-m", "agentenv_openciv3.server"])


@openciv3.command()
@click.argument("instance", required=False)
@click.option("--out", type=click.Path(file_okay=False, path_type=Path), help="Copy the files into this directory.")
def recordings(instance: str | None, out: Path | None):
    """List the game recordings the save_env_recording step stored, or copy them out with --out.

    INSTANCE keeps one run's: the instance id `agent-env run` prints.
    """
    saved = [a for a in FileArtifact.query().type("file").execute()
             if "-recording-" in a.id and a.id.rpartition("-recording-")[2].startswith(instance or "")]
    if not saved:
        raise click.ClickException("no recordings" + (f" for instance {instance}" if instance else ""))
    for a in saved:
        line = f"{a.id} v{a.version}  {a.content_type}  {a.object_url}"
        if out:
            out.mkdir(parents=True, exist_ok=True)
            path = out / a.id.rpartition("/")[2]
            path.write_bytes(a.load())
            line += f"  -> {path}"
        click.echo(line)


@openciv3.command()
@click.option("--open", "open_page", is_flag=True, help="Open the newest env's live view in the browser.")
def watch(open_page: bool):
    """Print the live view of every OpenCiv3 env running in Docker, newest first: a page that follows the game while
    the agents play it."""
    urls = []
    for line in _docker("ps", "--format", "{{.Image}}\t{{.Ports}}\t{{.Names}}").splitlines():
        image, ports, name = line.split("\t")
        if "mcp-server-openciv3" in image and (port := ENV_PORT.search(ports)):
            urls.append(f"http://127.0.0.1:{port[1]}/live")
            click.echo(f"{urls[-1]}  ({name})")
    if not urls:
        raise click.ClickException("no OpenCiv3 env is running; agent-env run starts one, e.g. "
                                   "agent-env run openciv3 --task three-agents-quick")
    if open_page:
        click.launch(urls[0])
