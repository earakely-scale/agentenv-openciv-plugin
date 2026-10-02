# The OpenCiv3 Codex player: an A2A agent that runs OpenAI's Codex CLI against the env's MCP tools (agent.py).
# Build: agent-env openciv3 setup --agent, or docker build -t openciv3-codex -f agents/codex-player/Dockerfile agents
FROM node:22-bookworm-slim
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get -o Acquire::Retries=5 update \
 && apt-get -o Acquire::Retries=5 install -y --no-install-recommends python3 python3-venv ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/agent \
 && /opt/agent/bin/pip install "agentenv-framework-protocol[agent]==0.1.275" \
 && npm install -g @openai/codex@0.147.0 \
 && codex --version
COPY common/openciv3_player.py /app/
COPY codex-player/agent.py /app/agent.py
USER node
WORKDIR /home/node
EXPOSE 8000
CMD ["/opt/agent/bin/python", "/app/agent.py"]
