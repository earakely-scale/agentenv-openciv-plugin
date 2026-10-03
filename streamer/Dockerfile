# Streams or records an OpenCiv3 env's live view: Chromium shows the page on a virtual display and plays its sound into
# PulseAudio, ffmpeg encodes both (stream.py), and caster.py's AI casters talk over the game.
# Build: agent-env openciv3 stream builds it, or docker build -t openciv3-streamer:2 streamer
FROM debian:bookworm-slim
RUN apt-get -o Acquire::Retries=5 update \
 && apt-get -o Acquire::Retries=5 install -y --no-install-recommends chromium xvfb ffmpeg python3 fonts-dejavu-core \
    fonts-noto-core ca-certificates pulseaudio pulseaudio-utils \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m streamer
COPY stream.py caster.py /app/
USER streamer
ENTRYPOINT ["python3", "/app/stream.py"]
