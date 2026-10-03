# Streams an OpenCiv3 env's live view to Twitch or any RTMP server: Chromium shows the page on a virtual display and
# ffmpeg pushes the display (stream.py). Build: agent-env openciv3 stream builds it, or docker build -t openciv3-streamer streamer
FROM debian:bookworm-slim
RUN apt-get -o Acquire::Retries=5 update \
 && apt-get -o Acquire::Retries=5 install -y --no-install-recommends chromium xvfb ffmpeg python3 fonts-dejavu-core \
    fonts-noto-core ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m streamer
COPY stream.py /app/stream.py
USER streamer
ENTRYPOINT ["python3", "/app/stream.py"]
