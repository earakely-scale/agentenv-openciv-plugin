import shlex
import sys
from pathlib import Path

from agent_env.artifact import FileArtifact
from click.testing import CliRunner

from agentenv_openciv3.cli import _check_bridge, openciv3

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


def test_setup_asks_for_buildx_before_building(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text('#!/bin/sh\ncase "$1 $2" in\n  "version --format") echo linux/amd64 ;;\n'
                      '  "buildx version") exit 1 ;;\n  *) echo "unexpected: docker $*" >&2; exit 2 ;;\nesac\n')
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    result = CliRunner().invoke(openciv3, ["setup", "--source", str(Path(__file__).resolve().parents[2])])
    assert result.exit_code == 1
    assert "sudo apt-get install docker-buildx" in result.output and "unexpected" not in result.output
