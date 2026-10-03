import importlib.util
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import click
from agent_env.artifact import FileArtifact
from click.testing import CliRunner

from agentenv_openciv3 import cli
from agentenv_openciv3.cli import _check_bridge, openciv3

STREAMER = Path(__file__).resolve().parents[2] / "streamer" / "stream.py"

FAKE_BRIDGE = Path(__file__).resolve().parents[1] / "env" / "fake_bridge.py"


def test_serve_checks_that_the_bridge_starts():
    _check_bridge(f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE_BRIDGE))}")


def test_serve_explains_a_bridge_that_needs_dotnet(tmp_path):
    bridge = tmp_path / "CivBridge"
    bridge.write_text("#!/bin/sh\necho 'You must install .NET to run this application.' >&2\nexit 131\n")
    bridge.chmod(0o755)
    result = CliRunner().invoke(openciv3, ["serve", "--bridge", str(bridge)])
    assert result.exit_code == 1
    assert "You must install .NET" in result.output
    assert "set DOTNET_ROOT" in result.output


def test_recordings_lists_and_copies_out_one_runs_files(local_stores, tmp_path):
    for name in ["smoke-recording-aaa.mp4", "smoke-recording-aaa.html", "play-recording-bbb.mp4", "other.mp4"]:
        FileArtifact.put_bytes(name, description=name, filename=name, content=name.encode())
    result = CliRunner().invoke(openciv3, ["recordings", "aaa", "--out", str(tmp_path / "out")])
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["smoke-recording-aaa.html",
                                                                    "smoke-recording-aaa.mp4"]
    assert (tmp_path / "out" / "smoke-recording-aaa.mp4").read_bytes() == b"smoke-recording-aaa.mp4"
    listed = CliRunner().invoke(openciv3, ["recordings"]).output
    assert "play-recording-bbb.mp4 v1" in listed and "other.mp4" not in listed
    assert CliRunner().invoke(openciv3, ["recordings", "zzz"]).exit_code == 1


def fake_docker(tmp_path, monkeypatch, cases: str) -> None:
    """A docker on PATH that runs the shell `case` arms in `cases` on its first two arguments."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(f'#!/bin/sh\ncase "$1 $2" in\n{cases}  *) echo "unexpected: docker $*" >&2; exit 2 ;;\nesac\n')
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")


def test_setup_asks_for_buildx_before_building(tmp_path, monkeypatch):
    fake_docker(tmp_path, monkeypatch, '  "version --format") echo linux/amd64 ;;\n  "buildx version") exit 1 ;;\n')
    result = CliRunner().invoke(openciv3, ["setup", "--source", str(Path(__file__).resolve().parents[2])])
    assert result.exit_code == 1
    assert "sudo apt-get install docker-buildx" in result.output and "unexpected" not in result.output


def test_watch_prints_the_live_view_of_each_running_env(tmp_path, monkeypatch):
    ps = ("localhost:5000/mcp-server-openciv3:v2\t127.0.0.1:49293->18765/tcp\tagent-local-new\n"
          "registry:2\t127.0.0.1:5000->5000/tcp\tregistry\n"
          "mcp-server-openciv3\t0.0.0.0:41000->18765/tcp, [::]:41000->18765/tcp\tagent-local-old\n"
          "a2a-agent-openciv3-claude\t\tplayer\n")
    (tmp_path / "ps.txt").write_text(ps)
    fake_docker(tmp_path, monkeypatch, f'  "ps --format") cat {tmp_path / "ps.txt"} ;;\n')
    opened = []
    monkeypatch.setattr(click, "launch", opened.append)
    result = CliRunner().invoke(openciv3, ["watch", "--open"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == ["http://127.0.0.1:49293/live  (agent-local-new)",
                                          "http://127.0.0.1:41000/live  (agent-local-old)"]
    assert opened == ["http://127.0.0.1:49293/live"]

    (tmp_path / "ps.txt").write_text("registry:2\t127.0.0.1:5000->5000/tcp\tregistry\n")
    result = CliRunner().invoke(openciv3, ["watch"])
    assert result.exit_code == 1
    assert "no OpenCiv3 env is running; agent-env run starts one" in result.output


def _secrets(monkeypatch, **secrets):
    store = SimpleNamespace(get=secrets.get)
    monkeypatch.setattr(cli, "get_config", lambda: SimpleNamespace(get_secret_store=lambda: store))


def test_stream_needs_a_stream_key(monkeypatch):
    _secrets(monkeypatch)
    result = CliRunner().invoke(openciv3, ["stream"])
    assert result.exit_code == 1 and "store your Twitch stream key as the secret OPENCIV3_STREAM_KEY" in result.output


def test_stream_sends_the_newest_game_under_way_without_showing_the_key(tmp_path, monkeypatch):
    _secrets(monkeypatch, OPENCIV3_STREAM_KEY="live_123_secret")
    (tmp_path / "ps.txt").write_text("mcp-server-openciv3\t127.0.0.1:41000->18765/tcp\tagent-local-old\n"
                                     "mcp-server-openciv3\t127.0.0.1:42000->18765/tcp\tagent-local-new\n")
    log = tmp_path / "docker.log"
    fake_docker(tmp_path, monkeypatch, f'  "ps --format") cat {tmp_path / "ps.txt"} ;;\n'
                                       f'  "image inspect") ;;\n'
                                       f'  "run --rm") echo "$@" > {log}; echo "$STREAM_URL" >> {log} ;;\n')
    monkeypatch.setattr(cli, "_playing", lambda url: "42000" in url)
    monkeypatch.setattr(cli.sys, "platform", "linux")
    result = CliRunner().invoke(openciv3, ["stream", "--linger", "30"])
    assert result.exit_code == 0, result.output
    assert "live_123_secret" not in result.output
    args, url = log.read_text().splitlines()
    assert "--network host -e STREAM_URL openciv3-streamer --url http://127.0.0.1:42000/live" in args
    assert "live_123_secret" not in args and args.endswith("--linger 30")
    assert url == "rtmp://live.twitch.tv/app/live_123_secret"


def test_the_streamer_hides_the_key_and_ends_after_the_game():
    spec = importlib.util.spec_from_file_location("streamer", STREAMER)
    streamer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(streamer)
    assert streamer.redacted("Error writing to rtmp://x/app/live_9: broken pipe", "live_9") == (
        "Error writing to rtmp://x/app/<stream key>: broken pipe")
    assert not streamer.stop_at(None, None, 60, 1000)
    assert not streamer.stop_at(950, None, 60, 1000) and streamer.stop_at(940, None, 60, 1000)
    assert not streamer.stop_at(None, 945, 60, 1000) and streamer.stop_at(None, 940, 60, 1000)
