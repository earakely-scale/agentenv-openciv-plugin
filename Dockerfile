# The OpenCiv3 env: CivBridge (.NET 8, self-contained) driven by the Python MCP server.
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
 && scripts/build-bridge.sh -r "$rid" --self-contained -o /out \
 && mkdir /out/licenses \
 && cp vendor/OpenCiv3/LICENSE /out/licenses/OpenCiv3-LICENSE.txt \
 && cp /usr/share/dotnet/LICENSE.txt /out/licenses/dotnet-LICENSE.txt \
 && cp /usr/share/dotnet/ThirdPartyNotices.txt /out/licenses/dotnet-ThirdPartyNotices.txt

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# The server's dependencies from pyproject.toml, without agentenv-framework (the control plane, which
# the CLI plugin, the bundle and the save_env_recording step use), and with the protocol pinned to the
# version the supported agentenv-framework pins. imageio-ffmpeg carries a static ffmpeg (about 50 MB;
# Debian's adds about 400 MB), linked onto PATH for the recorder's mp4 output.
RUN pip install "agentenv-framework-protocol==0.1.275" "mcp>=1.25,<2" "pydantic>=2,<3" "pillow>=10,<13" \
                "imageio-ffmpeg==0.6.0" \
 && ln -s "$(python -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')" /usr/local/bin/ffmpeg \
 && ffmpeg -hide_banner -version >/dev/null
COPY --from=bridge /out /opt/civbridge
COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md /opt/civbridge/licenses/
COPY pyproject.toml README.md LICENSE NOTICE /tmp/pkg/
COPY src /tmp/pkg/src
RUN pip install --no-deps /tmp/pkg && rm -rf /tmp/pkg
# W^X off: with it on, .NET segfaults under QEMU, which is how an amd64 image runs on an arm64 host.
ENV CIVBRIDGE_CMD=/opt/civbridge/CivBridge DOTNET_EnableWriteXorExecute=0
EXPOSE 18765
CMD ["python3", "-m", "agentenv_openciv3.server"]
