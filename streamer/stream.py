"""Streams an OpenCiv3 env's live view to an RTMP server such as Twitch, or records it, or both: Chromium shows the
page's stream layout (`?stream`) full screen on a virtual display and plays its sound into a PulseAudio null sink, and
ffmpeg encodes the display and the sink to STREAM_URL and, with --record, to a Matroska file. With --cast-config, two
AI casters (caster.py) talk over the game: the page plays their voices and shows them as captions. It waits for the env
to answer, and stops `--linger` seconds after the game ends, or once the env has been gone for a minute. STREAM_URL
holds the stream key and CAST_API_KEY the model endpoint's key: nothing it prints shows either."""

import argparse
import datetime
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

DISPLAY = ":99"
SINK = "broadcast"
CASTER = Path(__file__).with_name("caster.py")
POLL_SECONDS = 5
GONE_SECONDS = 60


def state(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/state.json", timeout=10) as r:
            return json.load(r)
    except (OSError, ValueError):
        return None


def redacted(line: str, secrets: dict[str, str]) -> str:
    """`line` with each secret replaced by its name, e.g. <stream key>."""
    for secret, name in secrets.items():
        if secret:
            line = line.replace(secret, f"<{name}>")
    return line


def relay(stream, secrets: dict[str, str]) -> None:
    for line in iter(stream.readline, b""):
        print(redacted(line.decode(errors="replace").rstrip(), secrets), flush=True)


def stop_at(game_over_since: float | None, gone_since: float | None, linger: float, now: float) -> bool:
    """Whether to end the stream: the game ended `linger` seconds ago, or the env has been gone for a minute."""
    if game_over_since is not None and now - game_over_since >= linger:
        return True
    return gone_since is not None and now - gone_since >= GONE_SECONDS


def page_url(url: str, *, client_view: bool = False, cast_port: int | None = None, title: str | None = None) -> str:
    params = ["stream", *(["client"] if client_view else []),
              *([f"cast=http://127.0.0.1:{cast_port}"] if cast_port else []),
              *([f"title={urllib.parse.quote(title)}"] if title else [])]
    return f"{url}{'&' if '?' in url else '?'}{'&'.join(params)}"


def free_port() -> int:
    """A loopback port nothing listens on, for the casters: with --network host, two streams on one machine share the
    host's ports."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ffmpeg_command(size: str, fps: int, bitrate: str, target: str, record: str | None) -> list[str]:
    """ffmpeg encoding the display and the sink's sound once, for the RTMP target, the recording, or both: the tee
    muxer then keeps recording when the RTMP leg fails."""
    rate = int(bitrate.rstrip("k"))
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats_period", "60",
           "-thread_queue_size", "512", "-f", "x11grab", "-video_size", size, "-framerate", str(fps),
           "-draw_mouse", "0", "-i", DISPLAY,
           "-thread_queue_size", "512", "-f", "pulse", "-i", f"{SINK}.monitor", "-map", "0:v", "-map", "1:a",
           "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-pix_fmt", "yuv420p",
           "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", f"{2 * rate}k", "-g", str(2 * fps),
           "-c:a", "aac", "-b:a", "128k", "-ar", "44100"]
    if target and record:
        return [*cmd, "-flags", "+global_header", "-f", "tee", f"[f=flv:onfail=ignore]{target}|[f=matroska]{record}"]
    return [*cmd, "-f", "flv", target] if target else [*cmd, "-f", "matroska", record]


def start_pulseaudio(env: dict) -> subprocess.Popen:
    """PulseAudio with one null sink as the default output: Chromium plays into it and ffmpeg records its monitor,
    which is silence while nothing plays."""
    Path(env["XDG_RUNTIME_DIR"]).mkdir(mode=0o700, exist_ok=True)
    proc = subprocess.Popen(
        ["pulseaudio", "--daemonize=no", "--exit-idle-time=-1", "--disallow-exit", "-n",
         "--load=module-native-protocol-unix",
         f"--load=module-null-sink sink_name={SINK} sink_properties=device.description=Broadcast"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if not subprocess.run(["pactl", "set-default-sink", SINK], env=env, capture_output=True).returncode:
            return proc
        time.sleep(0.2)
    proc.kill()
    raise RuntimeError("PulseAudio did not start")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", required=True, help="the env's live view, e.g. http://127.0.0.1:41589/live")
    p.add_argument("--size", default="1920x1080")
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--bitrate", default="4500k")
    p.add_argument("--linger", type=float, default=60)
    p.add_argument("--client-view", action="store_true", help="show the spotlit agent's real-client view full size")
    p.add_argument("--cast-config", metavar="JSON",
                   help="two AI casters talk over the game, set up as the JSON says ({} for their defaults; caster.py "
                        "--config); CAST_BASE_URL and CAST_API_KEY name an OpenAI-compatible endpoint")
    p.add_argument("--title", help="the broadcast's title, on screen and in the casters' intro")
    p.add_argument("--record", metavar="DIR", help="also write the stream to DIR/stream-<UTC time>.mkv")
    args = p.parse_args()
    target = os.environ.get("STREAM_URL", "")
    if not target and not args.record:
        print("Nothing to do: set STREAM_URL to stream, or --record DIR to record", flush=True)
        return 2
    if args.record and not os.access(args.record, os.W_OK):
        print(f"Cannot record: {args.record} is not writable by this container's user", flush=True)
        return 2
    secrets = {target.rsplit("/", 1)[-1].split("?")[0]: "stream key", os.environ.get("CAST_API_KEY", ""): "cast key"}
    width, height = args.size.split("x")

    print(f"Waiting for {args.url}", flush=True)
    while state(args.url) is None:
        time.sleep(POLL_SECONDS)

    env = {**os.environ, "DISPLAY": DISPLAY, "XDG_RUNTIME_DIR": "/tmp/pulse-runtime"}
    procs = [start_pulseaudio(env),
             subprocess.Popen(["Xvfb", DISPLAY, "-screen", "0", f"{width}x{height}x24", "-nolisten", "tcp"])]
    time.sleep(2)
    cast_port = free_port() if args.cast_config is not None else None
    page = page_url(args.url, client_view=args.client_view, cast_port=cast_port, title=args.title)
    procs.append(subprocess.Popen(
        ["chromium", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--no-first-run", "--noerrdialogs",
         "--disable-infobars", "--hide-scrollbars", "--kiosk", "--window-position=0,0",
         "--autoplay-policy=no-user-gesture-required", f"--window-size={width},{height}", f"--app={page}"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    time.sleep(8)
    record = (f"{args.record.rstrip('/')}/stream-{datetime.datetime.now(datetime.UTC):%Y%m%dT%H%M%SZ}.mkv"
              if args.record else None)
    ffmpeg = subprocess.Popen(ffmpeg_command(args.size, args.fps, args.bitrate, target, record),
                              env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    threading.Thread(target=relay, args=(ffmpeg.stderr, secrets), daemon=True).start()
    if cast_port:   # after ffmpeg, so the recording has the intro
        caster = subprocess.Popen(
            [sys.executable, str(CASTER), "--data", args.url.split("?")[0], "--port", str(cast_port),
             "--config", args.cast_config, *(["--title", args.title] if args.title else [])],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        threading.Thread(target=relay, args=(caster.stdout, secrets), daemon=True).start()
        procs.append(caster)
    procs.append(ffmpeg)
    where = " and ".join([*(["to the RTMP server"] if target else []), *([f"to {record}"] if record else [])])
    print(f"Streaming {args.url} at {args.size}, {args.fps} fps, {args.bitrate} {where}"
          + (", with the casters" if cast_port else ""), flush=True)

    game_over_since = gone_since = None
    try:
        while ffmpeg.poll() is None:
            time.sleep(POLL_SECONDS)
            now, s = time.monotonic(), state(args.url)
            gone_since = None if s is not None else gone_since or now
            if s and s.get("game_over") and game_over_since is None:
                game_over_since = now
                print(f"GAME OVER at T{s.get('turn')}; the final standings stay on for {args.linger:.0f} s", flush=True)
            if stop_at(game_over_since, gone_since, args.linger, now):
                print("The game is over; ending the stream", flush=True)
                break
    finally:
        for proc in reversed(procs):
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
    code = ffmpeg.returncode
    return 0 if code in (0, 255, -15) or game_over_since is not None else code


if __name__ == "__main__":
    sys.exit(main())
