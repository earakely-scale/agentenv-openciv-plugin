"""`agent-env openciv3`: register the OpenCiv3 env, serve it locally without Docker, watch a game live, play in one,
and fetch game recordings."""

import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import click
from agent_env.a2a_agent import A2AAgent
from agent_env.artifact import DockerImageArtifact, FileArtifact
from agent_env.config import get_config
from agent_env.env import MCPServerEnv

ENVIRONMENT_NAME = "openciv3"
PLAYER_ID = "openciv3-claude"
PLAYERS = {  # A2A agent id: its directory under agents/, the CLI it plays with, its model when a task names none
    PLAYER_ID: ("claude-player", "Claude Code", "sonnet"),
    "openciv3-codex": ("codex-player", "Codex", "gpt-5.6-sol"),
    "openciv3-gemini": ("gemini-player", "Gemini CLI", "gemini-3.1-pro-preview"),
}
REPO = "https://github.com/earakely-scale/agentenv-openciv-plugin"
ENV_PORT = re.compile(r":(\d+)->18765/tcp")
# The env logs `NEW GAME <id>` when a game starts, then one line per human seat (docs/play.md):
# `PLAY <civ> (<label>) game <id> /play#token=<token>`.
PLAY_LINK = re.compile(r"\bPLAY (?P<civ>.+?) \((?P<label>.*)\):?(?: game \S+)? \S*?(?P<path>/play#token=\w+)")
NEW_GAME_LINE = re.compile(r"\bNEW GAME \S+")
STREAMER_IMAGE = "openciv3-streamer"
STREAM_KEY = "OPENCIV3_STREAM_KEY"


@click.group()
def openciv3():
    """OpenCiv3: build and register the env, serve it locally, watch a game live, and fetch game recordings."""


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
              help="Also build and register the player agents, which play the bundle's tasks: "
                   + ", ".join(f"{id} ({cli}, agents/{path})" for id, (path, cli, _) in PLAYERS.items()) + ".")
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
        root, build_platform = _checkout(source), build_platform or _docker_platform()
        for player_id in PLAYERS:
            _register_player(root, build_platform, player_id)
        click.echo(f"The bundle's agent tasks deploy the default agent: set [agents] default_a2a_agent_id = "
                   f'"{PLAYER_ID}" in .agentenv/config.toml, and the model endpoint in [model] (base_url, api_key). '
                   "The frontier tasks name each player's agent.")


def _register_player(root: Path, build_platform: str, player_id: str) -> None:
    """Build agents/<player> (with agents/common, the game loop the players share) and register it as `player_id`."""
    path, cli, model = PLAYERS[player_id]
    context, dockerfile = root / "agents", root / "agents" / path / "Dockerfile"
    image = f"a2a-agent-{player_id}"
    click.echo(f"Building {image} for {build_platform} from {dockerfile.parent}")
    build = ["docker", "build", "--platform", build_platform, "-t", image, "-f", str(dockerfile), str(context)]
    if subprocess.run(build).returncode:
        raise click.ClickException(f"docker build of the {cli} player agent failed")
    artifact = DockerImageArtifact.put(id=image, image_name=image, description=f"OpenCiv3 {cli} player agent",
                                       build_context_path=str(context), dockerfile_path=str(dockerfile))
    player = A2AAgent.put(id=player_id, docker_image_artifact=artifact, metadata={"default_model": model})
    click.echo(f"Registered A2A agent {player.id!r} version {player.version} ({cli})")


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

    INSTANCE keeps one run's: the instance id `agent-env run` prints. Files keep the names the env gave them, so the
    HTML viewer finds the client videos next to it; with several runs, each run's files go into a directory of its own.
    """
    saved = [a for a in FileArtifact.query().type("file").execute()
             if "-recording-" in a.id and a.id.rpartition("-recording-")[2].startswith(instance or "")]
    if not saved:
        raise click.ClickException("no recordings" + (f" for instance {instance}" if instance else ""))
    runs = {_run_of(a.id) for a in saved}
    for a in saved:
        line = f"{a.id} v{a.version}  {a.content_type}  {a.object_url}"
        if out:
            folder = out / _run_of(a.id) if len(runs) > 1 else out
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / Path(a.filename or a.id.rpartition("/")[2]).name
            path.write_bytes(a.load())
            line += f"  -> {path}"
        click.echo(line)


def _run_of(artifact_id: str) -> str:
    """`smoke-recording-<instance>`: the artifact id without the file's suffix."""
    task, _, rest = artifact_id.rpartition("/")[2].rpartition("-recording-")
    return f"{task}-recording-{rest.partition('.')[0]}"


@openciv3.command()
@click.option("--open", "open_page", is_flag=True, help="Open the newest env's live view in the browser.")
def watch(open_page: bool):
    """Print the live view of every OpenCiv3 env running in Docker, newest first: a page that follows the game while
    the agents play it."""
    views = _live_views()
    for url, name in views:
        click.echo(f"{url}  ({name})")
    if not views:
        raise click.ClickException("no OpenCiv3 env is running; agent-env run starts one, e.g. "
                                   "agent-env run openciv3 --task three-agents-quick")
    if open_page:
        click.launch(views[0][0])


def _live_views() -> list[tuple[str, str]]:
    """The live view of each OpenCiv3 env running in Docker, newest first, with its container's name."""
    return [(f"http://127.0.0.1:{port}/live", name) for port, name in _running_envs()]


def _running_envs() -> list[tuple[str, str]]:
    """The host port and container name of each OpenCiv3 env running in Docker, newest first."""
    envs = []
    for line in _docker("ps", "--format", "{{.Image}}\t{{.Ports}}\t{{.Names}}").splitlines():
        image, ports, name = line.split("\t")
        if "mcp-server-openciv3" in image and (port := ENV_PORT.search(ports)):
            envs.append((port[1], name))
    return envs


@openciv3.command()
@click.option("--open", "open_page", is_flag=True, help="Open the first human seat's play page in the browser.")
def play(open_page: bool):
    """Print the play link of every human seat in the OpenCiv3 games running in Docker, newest env first: whoever opens
    a link plays that civilization in the browser (docs/play.md). A task seats people with openciv3_match's `humans`,
    e.g. agent-env run openciv3 --task human-vs-ai."""
    envs = _running_envs()
    if not envs:
        raise click.ClickException("no OpenCiv3 env is running; agent-env run starts one, e.g. "
                                   "agent-env run openciv3 --task human-vs-ai")
    links = [(f"http://127.0.0.1:{port}{path}", civ, label, name)
             for port, name in envs for civ, label, path in _play_links(_container_log(name))]
    for url, civ, label, name in links:
        click.echo(f"{url}  ({civ}, {label}; {name})")
    if not links:
        raise click.ClickException(
            f"no human seats: the game in {', '.join(name for _, name in envs)} has no human players. A task seats "
            "you with openciv3_match's humans, e.g. agent-env run openciv3 --task human-vs-ai")
    if open_page:
        click.launch(links[0][0])


def _container_log(name: str) -> str:
    """A container's log, its stdout and stderr together, in order."""
    try:
        out = subprocess.run(["docker", "logs", name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise click.ClickException(f"docker logs {name} failed: {e}") from e
    if out.returncode:
        raise click.ClickException(f"docker logs {name} failed: {out.stdout.strip()}")
    return out.stdout


def _play_links(log: str) -> list[tuple[str, str, str]]:
    """The newest game's play links in an env's log, as (civ, label, path): the PLAY lines after the last NEW GAME line
    (none if the newest game has no humans). An older env logs no NEW GAME: then the last run of PLAY lines, up to a
    civ named twice (an earlier game's)."""
    lines = log.splitlines()
    starts = [i for i, line in enumerate(lines) if NEW_GAME_LINE.search(line)]
    if starts:
        return [(m["civ"], m["label"], m["path"]) for line in lines[starts[-1]:] if (m := PLAY_LINK.search(line))]
    ends = [i for i, line in enumerate(lines) if PLAY_LINK.search(line)]
    links: list[tuple[str, str, str]] = []
    for line in reversed(lines[:ends[-1] + 1] if ends else []):
        m = PLAY_LINK.search(line)
        if m is None or m["civ"] in {civ for civ, _, _ in links}:
            break
        links.append((m["civ"], m["label"], m["path"]))
    return links[::-1]


def _playing(url: str) -> bool:
    """Whether the env behind a live view answers and its game is under way."""
    try:
        with urllib.request.urlopen(f"{url}/state.json", timeout=5) as r:
            return not json.load(r).get("game_over")
    except (OSError, ValueError):
        return False


@openciv3.command()
@click.option("--url", help="The live view to stream. Default: the newest OpenCiv3 env in Docker whose game is under "
                            "way; the command waits for one to start.")
@click.option("--server", default="rtmp://live.twitch.tv/app", show_default=True,
              help="The RTMP ingest server; the stream key is appended to it.")
@click.option("--key-secret", default=STREAM_KEY, show_default=True,
              help="The secret that holds the stream key, from agent-env's secret store ([stores.secret] in "
                   ".agentenv/config.toml, or an environment variable of that name).")
@click.option("--size", default="1920x1080", show_default=True, help="The stream's resolution.")
@click.option("--fps", default=30, show_default=True)
@click.option("--bitrate", default="4500k", show_default=True)
@click.option("--linger", default=60, show_default=True, help="Seconds to keep streaming the final standings.")
@click.option("--test", "bandwidth_test", is_flag=True,
              help="Send to Twitch without going live (its bandwidth test): the stream shows only in Twitch Inspector.")
@click.option("--source", type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Checkout to build the streamer image from when it isn't built yet.")
def stream(url: str | None, server: str, key_secret: str, size: str, fps: int, bitrate: str, linger: int,
           bandwidth_test: bool, source: Path | None):
    """Stream a game's live view to Twitch, or any RTMP server, while the agents play it. A headless browser in Docker
    shows the page and ffmpeg sends it; the stream starts with the game and ends after GAME OVER."""
    key = get_config().get_secret_store().get(key_secret)
    if not key:
        raise click.ClickException(f"no stream key: store your Twitch stream key as the secret {key_secret}, e.g. a "
                                   f"line '{key_secret}: <key>' in the secrets file .agentenv/config.toml names, or "
                                   f"export {key_secret}")
    if subprocess.run(["docker", "image", "inspect", STREAMER_IMAGE], capture_output=True).returncode:
        context = _checkout(source) / "streamer"
        click.echo(f"Building {STREAMER_IMAGE} from {context}")
        if subprocess.run(["docker", "build", "-t", STREAMER_IMAGE, str(context)]).returncode:
            raise click.ClickException("docker build of the streamer failed")
    if url is None:
        click.echo("Waiting for an OpenCiv3 game to start (agent-env run openciv3 --task ...)")
        while (url := next((u for u, _ in _live_views() if _playing(u)), None)) is None:
            time.sleep(5)
    if sys.platform == "darwin":
        network, page = [], url.replace("127.0.0.1", "host.docker.internal")
    else:
        network, page = ["--network", "host"], url
    target = f"{server.rstrip('/')}/{key}" + ("?bandwidthtest=true" if bandwidth_test else "")
    test = " as a bandwidth test (not live; see Twitch Inspector)" if bandwidth_test else ""
    click.echo(f"Streaming {url} to {server.rstrip('/')}/<stream key>{test}; Ctrl-C ends the stream")
    cmd = ["docker", "run", "--rm", "--shm-size", "1g", *network, "-e", "STREAM_URL", STREAMER_IMAGE,
           "--url", page, "--size", size, "--fps", str(fps), "--bitrate", bitrate, "--linger", str(linger)]
    code = subprocess.run(cmd, env={**os.environ, "STREAM_URL": target}).returncode
    if code:
        raise click.ClickException(f"the stream ended with an error ({code})")
