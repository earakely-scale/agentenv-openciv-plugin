import http.server
import importlib.util
import json
import os
import shlex
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import click
from agent_env.artifact import FileArtifact
from agent_env.config import ConfigError
from click.testing import CliRunner

from agentenv_openciv3 import cli
from agentenv_openciv3.cli import _check_bridge, openciv3

STREAMER = Path(__file__).resolve().parents[2] / "streamer" / "stream.py"
STREAMER_IMAGE = cli._streamer_image(None)[0]

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


def test_recordings_copy_out_the_env_s_own_file_names(local_stores, tmp_path):
    """The viewer's HTML links the client videos by the names the env gave them, so they keep those names."""
    for run in ("aaa", "bbb"):
        for name in ["openciv3-seed1-seats.html", "openciv3-seed1-seats.client-opus.mp4"]:
            FileArtifact.put_bytes(f"match-recording-{run}.{name.partition('.')[2]}", description=name, filename=name,
                                   content=f"{run} {name}".encode())
    one = CliRunner().invoke(openciv3, ["recordings", "aaa", "--out", str(tmp_path / "one")])
    assert one.exit_code == 0, one.output
    assert sorted(p.name for p in (tmp_path / "one").iterdir()) == [
        "openciv3-seed1-seats.client-opus.mp4", "openciv3-seed1-seats.html"]
    assert (tmp_path / "one" / "openciv3-seed1-seats.html").read_bytes() == b"aaa openciv3-seed1-seats.html"
    both = CliRunner().invoke(openciv3, ["recordings", "--out", str(tmp_path / "both")])
    assert both.exit_code == 0, both.output
    assert sorted(str(p.relative_to(tmp_path / "both")) for p in (tmp_path / "both").rglob("*.*")) == [
        f"match-recording-{run}/{name}" for run in ("aaa", "bbb")
        for name in ["openciv3-seed1-seats.client-opus.mp4", "openciv3-seed1-seats.html"]]


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


def test_play_prints_the_newest_game_s_play_links_and_opens_the_first(tmp_path, monkeypatch):
    (tmp_path / "ps.txt").write_text(
        "localhost:5000/mcp-server-openciv3:v2\t127.0.0.1:49293->18765/tcp\tagent-local-new\n"
        "registry:2\t127.0.0.1:5000->5000/tcp\tregistry\n"
        "mcp-server-openciv3\t127.0.0.1:41000->18765/tcp\tagent-local-old\n")
    old, new, friend = "a" * 32, "b" * 32, "c" * 32
    (tmp_path / "new.log").write_text(
        f"2026-10-03 10:00:00 INFO agentenv_openciv3.server: PLAY Rome (you) /play#token={old}\n"
        "INFO:     127.0.0.1:5000 - \"POST /agentenv HTTP/1.1\" 200 OK\n"
        f"2026-10-03 11:00:00 INFO agentenv_openciv3.server: PLAY Rome (you) /play#token={new}\n"
        f"2026-10-03 11:00:00 INFO agentenv_openciv3.server: PLAY Greece (a friend) /play#token={friend}\n"
        "2026-10-03 11:00:01 INFO agentenv_openciv3.server: turn 2\n")
    (tmp_path / "old.log").write_text("2026-10-03 09:00:00 INFO agentenv_openciv3.server: turn 40\n")
    fake_docker(tmp_path, monkeypatch, f'  "ps --format") cat {tmp_path / "ps.txt"} ;;\n'
                                       f'  "logs agent-local-new") cat {tmp_path / "new.log"} >&2 ;;\n'
                                       f'  "logs agent-local-old") cat {tmp_path / "old.log"} ;;\n')
    opened = []
    monkeypatch.setattr(click, "launch", opened.append)
    result = CliRunner().invoke(openciv3, ["play", "--open"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        f"http://127.0.0.1:49293/play#token={new}  (Rome, you; agent-local-new)",
        f"http://127.0.0.1:49293/play#token={friend}  (Greece, a friend; agent-local-new)"]
    assert opened == [f"http://127.0.0.1:49293/play#token={new}"]


def test_play_takes_the_links_after_the_last_new_game_line(tmp_path, monkeypatch):
    (tmp_path / "ps.txt").write_text("mcp-server-openciv3\t127.0.0.1:41000->18765/tcp\tagent-local\n")
    old, new = "a" * 32, "b" * 32
    log = tmp_path / "env.log"
    log.write_text(f"INFO server: NEW GAME g-1\nINFO server: PLAY Rome (you) game g-1 /play#token={old}\n"
                   f"INFO server: NEW GAME g-2\nINFO server: PLAY Rome (you) game g-2 /play#token={new}\n")
    fake_docker(tmp_path, monkeypatch, f'  "ps --format") cat {tmp_path / "ps.txt"} ;;\n'
                                       f'  "logs agent-local") cat {log} ;;\n')
    result = CliRunner().invoke(openciv3, ["play"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [f"http://127.0.0.1:41000/play#token={new}  (Rome, you; agent-local)"]
    # a later game without humans: the old links are dead
    log.write_text(log.read_text() + "INFO server: NEW GAME g-3\n")
    result = CliRunner().invoke(openciv3, ["play"])
    assert result.exit_code == 1
    assert "no human seats" in result.output


def test_play_explains_no_env_and_no_human_seats(tmp_path, monkeypatch):
    (tmp_path / "ps.txt").write_text("registry:2\t127.0.0.1:5000->5000/tcp\tregistry\n")
    fake_docker(tmp_path, monkeypatch, f'  "ps --format") cat {tmp_path / "ps.txt"} ;;\n'
                                       '  "logs agent-local") echo "INFO turn 3" ;;\n')
    result = CliRunner().invoke(openciv3, ["play"])
    assert result.exit_code == 1
    assert "no OpenCiv3 env is running; agent-env run starts one" in result.output
    (tmp_path / "ps.txt").write_text("mcp-server-openciv3\t127.0.0.1:41000->18765/tcp\tagent-local\n")
    result = CliRunner().invoke(openciv3, ["play", "--open"])
    assert result.exit_code == 1
    assert "no human seats: the game in agent-local has no human players" in result.output


def _config(monkeypatch, *, model: bool = True, **secrets):
    """agent-env's config as the stream command reads it: the secret store and, with `model`, the model endpoint."""
    def endpoint(value):
        def get():
            if not model:
                raise ConfigError("No model endpoint configured: set [model] base_url in .agentenv/config.toml")
            return value
        return get
    config = SimpleNamespace(get_secret_store=lambda: SimpleNamespace(get=secrets.get),
                             get_litellm_base_url=endpoint("http://localhost:4000"),
                             get_litellm_api_key=endpoint("sk-model-key-456"))
    monkeypatch.setattr(cli, "get_config", lambda: config)


def test_stream_needs_a_stream_key(monkeypatch):
    _config(monkeypatch)
    result = CliRunner().invoke(openciv3, ["stream"])
    assert result.exit_code == 1 and "store your Twitch stream key as the secret OPENCIV3_STREAM_KEY" in result.output


def test_stream_sends_the_newest_game_under_way_without_showing_the_key(tmp_path, monkeypatch):
    _config(monkeypatch, OPENCIV3_STREAM_KEY="live_123_secret")
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
    assert f"--network host -e STREAM_URL {STREAMER_IMAGE} --url http://127.0.0.1:42000/live" in args
    assert "live_123_secret" not in args and args.endswith("--linger 30")
    assert url == "rtmp://live.twitch.tv/app/live_123_secret"
    assert CliRunner().invoke(openciv3, ["stream", "--test", "--client-view"]).exit_code == 0
    assert log.read_text().splitlines()[0].endswith("--linger 60 --client-view")
    assert log.read_text().splitlines()[1] == "rtmp://live.twitch.tv/app/live_123_secret?bandwidthtest=true"


def test_stream_sends_one_stream_to_twitch_and_x_at_once(tmp_path, monkeypatch):
    _config(monkeypatch, OPENCIV3_STREAM_KEY="live_123_secret", OPENCIV3_X_SERVER="rtmps://or.pscp.tv:443/x/",
            OPENCIV3_X_STREAM_KEY="x_456_secret")
    log = tmp_path / "docker.log"
    fake_docker(tmp_path, monkeypatch, '  "image inspect") ;;\n'
                                       f'  "run --rm") echo "$@" > {log}; echo "$STREAM_URL" >> {log} ;;\n')
    monkeypatch.setattr(cli, "_playing", lambda url: True)
    monkeypatch.setattr(cli, "_broadcast", lambda url: {})
    stream = ["stream", "--url", "http://127.0.0.1:41589/live", "--to", "twitch", "--to", "x"]
    result = CliRunner().invoke(openciv3, stream)
    assert result.exit_code == 0, result.output
    args, *urls = log.read_text().splitlines()
    assert urls == ["rtmp://live.twitch.tv/app/live_123_secret", "rtmps://or.pscp.tv:443/x/x_456_secret"]
    assert "_secret" not in result.output + args and "-e STREAM_URL" in args
    assert ("to rtmp://live.twitch.tv/app/<stream key> and to rtmps://or.pscp.tv:443/x/<stream key> (press Go Live "
            "in X's Live Studio once it starts)") in result.output
    assert CliRunner().invoke(openciv3, [*stream[:3], "--to", "x"]).exit_code == 0
    assert log.read_text().splitlines()[1:] == ["rtmps://or.pscp.tv:443/x/x_456_secret"]
    _config(monkeypatch, OPENCIV3_X_SERVER="rtmps://ca.pscp.tv:443/x", MY_X_KEY="x_789_secret")
    assert CliRunner().invoke(openciv3, [*stream[:3], "--to", "x", "--x-key-secret", "MY_X_KEY"]).exit_code == 0
    assert log.read_text().splitlines()[1:] == ["rtmps://ca.pscp.tv:443/x/x_789_secret"]

    _config(monkeypatch, OPENCIV3_STREAM_KEY="live_123_secret", OPENCIV3_X_SERVER="rtmps://or.pscp.tv:443/x")
    result = CliRunner().invoke(openciv3, stream)
    assert result.exit_code == 1
    assert "store its server URL and stream key as the secrets OPENCIV3_X_SERVER and OPENCIV3_X_STREAM_KEY" in (
        result.output)


def test_stream_records_offline_with_the_tasks_casters_without_showing_any_key(tmp_path, monkeypatch):
    _config(monkeypatch)   # no stream key: offline needs none
    log = tmp_path / "docker.log"
    fake_docker(tmp_path, monkeypatch, '  "image inspect") ;;\n'
                                       f'  "run --rm") echo "$@" > {log}; '
                                       f'echo "[$STREAM_URL] $CAST_BASE_URL $CAST_API_KEY" >> {log} ;;\n')
    casters = {"model": "openai/gpt-5.6-luna", "analyst": {"name": "Iris", "voice": "coral"}}
    monkeypatch.setattr(cli, "_broadcast", lambda url: {"title": "Battle of the Labs", "casters": casters})
    monkeypatch.setattr(cli, "_playing", lambda url: True)
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    rec = tmp_path / "rec" / "show"
    stream = ["stream", "--url", "http://127.0.0.1:41589/live", "--offline", "--record", str(rec)]
    result = CliRunner().invoke(openciv3, stream)
    assert result.exit_code == 0, result.output
    assert rec.is_dir() and f"into {rec}, with the casters" in result.output
    args, env = log.read_text().splitlines()
    assert args.startswith(f"run --rm --shm-size 1g -v {rec.resolve()}:/rec -e STREAM_URL -e CAST_BASE_URL "
                           f"-e CAST_API_KEY {STREAMER_IMAGE} --url http://host.docker.internal:41589/live")
    assert args.endswith(f"--linger 60 --cast-config {json.dumps(casters)} --title Battle of the Labs --record /rec")
    assert env == "[] http://host.docker.internal:4000 sk-model-key-456"   # values in the environment only
    assert "sk-model-key-456" not in result.output + args

    assert CliRunner().invoke(openciv3, [*stream, "--no-cast", "--title", "Mine"]).exit_code == 0
    args, env = log.read_text().splitlines()
    assert args.endswith("--linger 60 --title Mine --record /rec") and env == "[]  "

    monkeypatch.setattr(cli, "_broadcast", lambda url: {})   # a task without a broadcast: no casters unless asked
    monkeypatch.setattr(cli.sys, "platform", "linux")   # the container writes the recording as you
    result = CliRunner().invoke(openciv3, stream)
    assert result.exit_code == 0, result.output
    args, env = log.read_text().splitlines()
    assert (f"--network host -v {rec.resolve()}:/rec --user {os.getuid()}:{os.getgid()} -e HOME=/tmp -e STREAM_URL "
            f"{STREAMER_IMAGE} --url http://127.0.0.1:41589/live") in args
    assert env == "[]  " and "--cast" not in args and "--title" not in args
    assert CliRunner().invoke(openciv3, [*stream, "--cast"]).exit_code == 0
    assert log.read_text().splitlines()[0].endswith("--linger 60 --cast-config {} --record /rec")


def test_stream_waits_for_the_tasks_game_and_reads_its_broadcast(monkeypatch):
    live = {"turn": 0, "game_over": False, "broadcast": None}   # the env's own game, before the match replaces it
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _serving({"/live/data.json": {"live": live}}))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/live"
        assert not cli._playing(url) and cli._broadcast(url) == {}
        assert server.paths[0] == f"/live/data.json?since={cli.NO_TURNS}"
        live["broadcast"] = {"title": "Showmatch", "casters": {}}   # the match's game, at its start
        assert cli._playing(url) and cli._broadcast(url) == {"title": "Showmatch", "casters": {}}
        live.update(broadcast={"title": None, "casters": None}, game_over=True)
        assert not cli._playing(url)
        live.update(broadcast=None, game_over=False, turn=2)   # a game no match set up, or an older env's: under way
        assert cli._playing(url) and cli._broadcast(url) == {}
    finally:
        server.shutdown()
    assert not cli._playing(url) and cli._broadcast(url) == {}   # gone


def _serving(docs: dict[str, dict]) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.paths = [*getattr(self.server, "paths", []), self.path]
            body = json.dumps(docs[self.path.split("?")[0]]).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    return Handler


def test_stream_builds_a_new_streamer_image_when_streamer_changes(tmp_path, monkeypatch):
    _config(monkeypatch)
    checkout = tmp_path / "checkout"
    (checkout / "streamer").mkdir(parents=True)
    for name in ("Dockerfile", "stream.py", "caster.py"):
        (checkout / "streamer" / name).write_text(f"{name} v1\n")
    log = tmp_path / "docker.log"
    fake_docker(tmp_path, monkeypatch, '  "image inspect") exit 1 ;;\n'
                                       f'  "build -t") echo "$@" >> {log} ;;\n'
                                       f'  "run --rm") echo "$@" >> {log} ;;\n')
    monkeypatch.setattr(cli, "_playing", lambda url: True)
    stream = ["stream", "--url", "http://127.0.0.1:41589/live", "--offline", "--record", str(tmp_path / "rec"),
              "--source", str(checkout)]
    first = CliRunner().invoke(openciv3, stream)
    (checkout / "streamer" / "caster.py").write_text("caster.py v2\n")
    second = CliRunner().invoke(openciv3, stream)
    assert first.exit_code == second.exit_code == 0, first.output + second.output
    built, ran, built_again, ran_again = log.read_text().splitlines()
    image = built.split()[2]
    assert image.startswith("openciv3-streamer:") and built == f"build -t {image} {checkout / 'streamer'}"
    assert f" {image} --url " in ran and built_again.split()[2] != image
    assert f" {built_again.split()[2]} --url " in ran_again
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "__file__", str(tmp_path / "site-packages" / "agentenv_openciv3" / "cli.py"))
    result = CliRunner().invoke(openciv3, stream[:-2])
    assert result.exit_code == 2 and "no checkout of agentenv-openciv-plugin with streamer/ found" in result.output


def test_stream_explains_what_offline_and_the_casters_need(tmp_path, monkeypatch):
    _config(monkeypatch, model=False)
    fake_docker(tmp_path, monkeypatch, '  "image inspect") ;;\n')
    monkeypatch.setattr(cli, "_broadcast", lambda url: {"casters": {}})
    monkeypatch.setattr(cli, "_playing", lambda url: True)
    result = CliRunner().invoke(openciv3, ["stream", "--offline"])
    assert result.exit_code == 2 and "--offline only records: add --record DIR" in result.output
    result = CliRunner().invoke(openciv3, ["stream", "--url", "http://127.0.0.1:41589/live", "--offline",
                                           "--record", str(tmp_path)])
    assert result.exit_code == 1
    assert "the casters need agent-env's model endpoint: No model endpoint configured" in result.output
    assert result.output.rstrip().endswith("or stream without them: --no-cast")


def _streamer():
    spec = importlib.util.spec_from_file_location("streamer", STREAMER)
    streamer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(streamer)
    return streamer


def test_the_streamer_hides_the_keys_and_ends_after_the_game():
    streamer = _streamer()
    secrets = {"live_9": "stream key", "x_7": "stream key", "sk-cast-1": "cast key", "": "nothing"}
    assert streamer.redacted("Error writing to rtmp://x/app/live_9?bandwidthtest=true: broken pipe", secrets) == (
        "Error writing to rtmp://x/app/<stream key>?bandwidthtest=true: broken pipe")
    assert streamer.redacted("caster: HTTP 401 bad key sk-cast-1", secrets) == "caster: HTTP 401 bad key <cast key>"
    assert streamer.redacted("[tee] Slave rtmps://or.pscp.tv:443/x/x_7 failed", secrets) == (
        "[tee] Slave rtmps://or.pscp.tv:443/x/<stream key> failed")
    assert not streamer.stop_at(None, None, 60, 1000)
    assert not streamer.stop_at(950, None, 60, 1000) and streamer.stop_at(940, None, 60, 1000)
    assert not streamer.stop_at(None, 945, 60, 1000) and streamer.stop_at(None, 940, 60, 1000)


def test_the_streamer_page_asks_for_the_casters_and_the_title():
    streamer = _streamer()
    assert streamer.page_url("http://h:1/live") == "http://h:1/live?stream"
    assert streamer.page_url("http://h:1/live?seat=Rome", client_view=True, cast_port=41000,
                             title="Battle of the Labs") == (
        "http://h:1/live?seat=Rome&stream&client&cast=http://127.0.0.1:41000&title=Battle%20of%20the%20Labs")


def test_the_streamer_encodes_once_for_the_stream_and_the_recording():
    streamer = _streamer()
    both = streamer.ffmpeg_command("1280x720", 30, "3000k", ["rtmp://x/app/key"], "/rec/stream.mkv")
    assert ["-f", "pulse", "-i", "broadcast.monitor"] == both[both.index("pulse") - 1:both.index("pulse") + 3]
    assert both[-5:] == ["-flags", "+global_header", "-f", "tee",
                         "[f=flv:onfail=ignore]rtmp://x/app/key|[f=matroska]/rec/stream.mkv"]
    assert ["-c:a", "aac", "-b:a", "128k"] == both[both.index("-c:a"):both.index("-c:a") + 4]
    assert streamer.ffmpeg_command("1280x720", 30, "3000k", ["rtmp://x/app/key"], None)[-3:] == [
        "-f", "flv", "rtmp://x/app/key"]
    assert streamer.ffmpeg_command("1280x720", 30, "3000k", [], "/rec/s.mkv")[-3:] == ["-f", "matroska", "/rec/s.mkv"]
    twice = streamer.ffmpeg_command("1280x720", 30, "3000k", ["rtmp://t/app/a", "rtmps://x:443/x/b"], None)
    assert twice[-3:] == ["-f", "tee", "[f=flv:onfail=ignore]rtmp://t/app/a|[f=flv:onfail=ignore]rtmps://x:443/x/b"]
