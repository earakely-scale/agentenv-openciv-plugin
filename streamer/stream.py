"""Streams an OpenCiv3 env's live view to an RTMP server such as Twitch: Chromium shows the page's stream layout
(`?stream`) full screen on a virtual display, and ffmpeg encodes the display, with a silent audio track, to
STREAM_URL. It waits for the env to answer, and stops `--linger` seconds after the game ends, or once the env has been
gone for a minute. The stream URL holds the stream key, so nothing it prints shows it."""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

DISPLAY = ":99"
POLL_SECONDS = 5
GONE_SECONDS = 60


def state(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/state.json", timeout=10) as r:
            return json.load(r)
    except (OSError, ValueError):
        return None


def redacted(line: str, secret: str) -> str:
    return line.replace(secret, "<stream key>") if secret else line


def relay(stream, secret: str) -> None:
    for line in iter(stream.readline, b""):
        print(redacted(line.decode(errors="replace").rstrip(), secret), flush=True)


def stop_at(game_over_since: float | None, gone_since: float | None, linger: float, now: float) -> bool:
    """Whether to end the stream: the game ended `linger` seconds ago, or the env has been gone for a minute."""
    if game_over_since is not None and now - game_over_since >= linger:
        return True
    return gone_since is not None and now - gone_since >= GONE_SECONDS


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", required=True, help="the env's live view, e.g. http://127.0.0.1:41589/live")
    p.add_argument("--size", default="1920x1080")
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--bitrate", default="4500k")
    p.add_argument("--linger", type=float, default=60)
    args = p.parse_args()
    target = os.environ["STREAM_URL"]
    secret = target.rsplit("/", 1)[-1]
    width, height = args.size.split("x")

    print(f"Waiting for {args.url}", flush=True)
    while state(args.url) is None:
        time.sleep(POLL_SECONDS)

    env = {**os.environ, "DISPLAY": DISPLAY}
    procs = [subprocess.Popen(["Xvfb", DISPLAY, "-screen", "0", f"{width}x{height}x24", "-nolisten", "tcp"])]
    time.sleep(2)
    procs.append(subprocess.Popen(
        ["chromium", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--no-first-run", "--noerrdialogs",
         "--disable-infobars", "--hide-scrollbars", "--kiosk", "--window-position=0,0",
         f"--window-size={width},{height}", f"--app={args.url}{'&' if '?' in args.url else '?'}stream"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    time.sleep(8)
    rate = int(args.bitrate.rstrip("k"))
    ffmpeg = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats_period", "60",
         "-f", "x11grab", "-video_size", f"{width}x{height}", "-framerate", str(args.fps), "-draw_mouse", "0",
         "-i", DISPLAY, "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
         "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-pix_fmt", "yuv420p",
         "-b:v", args.bitrate, "-maxrate", args.bitrate, "-bufsize", f"{2 * rate}k", "-g", str(2 * args.fps),
         "-c:a", "aac", "-b:a", "96k", "-f", "flv", target],
        env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    threading.Thread(target=relay, args=(ffmpeg.stderr, secret), daemon=True).start()
    procs.append(ffmpeg)
    print(f"Streaming {args.url} at {args.size}, {args.fps} fps, {args.bitrate}", flush=True)

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
