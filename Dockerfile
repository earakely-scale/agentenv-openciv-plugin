# The OpenCiv3 env: CivBridge (.NET, self-contained) driven by the Python MCP server.
# Build from the repo root: docker build -t openciv3 .

# The bridge is cross-compiled on the build host, so an arm64 host building amd64 never emulates .NET.
FROM --platform=$BUILDPLATFORM mcr.microsoft.com/dotnet/sdk:8.0 AS bridge
ARG TARGETARCH
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
WORKDIR /src
COPY vendor/OpenCiv3 vendor/OpenCiv3
COPY patches patches
COPY scripts scripts
COPY bridge bridge
RUN --mount=type=cache,target=/root/.nuget/packages \
    case "$TARGETARCH" in \
      amd64) rid=linux-x64 ;; \
      arm64) rid=linux-arm64 ;; \
      *) echo "unsupported TARGETARCH '$TARGETARCH'" >&2; exit 1 ;; \
    esac \
 && scripts/build-bridge.sh -r "$rid" --self-contained -o /out

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# The env server needs only the protocol SDK; agentenv-framework (the control plane, which the CLI
# plugin and bundle use) is left out of the image.
RUN pip install "agentenv-framework-protocol>=0.1.272" "mcp>=1.25,<2" "pydantic>=2"
COPY --from=bridge /out /opt/civbridge
COPY pyproject.toml README.md LICENSE /tmp/pkg/
COPY src /tmp/pkg/src
RUN pip install --no-deps /tmp/pkg && rm -rf /tmp/pkg
# W^X off: with it on, .NET segfaults under QEMU, which is how an amd64 image runs on an arm64 host.
ENV CIVBRIDGE_CMD=/opt/civbridge/CivBridge DOTNET_EnableWriteXorExecute=0
EXPOSE 18765
CMD ["python3", "-m", "agentenv_openciv3.server"]
