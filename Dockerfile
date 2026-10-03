# The OpenCiv3 env: CivBridge (.NET 8, self-contained) driven by the Python MCP server.
# Build from the repo root: docker build -t openciv3 .
# With the real OpenCiv3 client as a recording renderer (docs/recording.md): docker build --target client ...

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

FROM python:3.12-slim AS env
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# The server's dependencies from pyproject.toml, without agentenv-framework (the control plane, which
# the CLI plugin, the bundle and the task steps use), and with the protocol pinned to the
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

# The client target: Godot 4.4.1 .NET runs the OpenCiv3 client on Xvfb with Mesa's CPU renderer (no GPU), and
# loads each turn's save to capture the map. The art is OpenCiv3's community art, fetched here and never
# committed; it carries no license, so never publish this image or push it to a public registry.
FROM --platform=$BUILDPLATFORM debian:bookworm-slim AS godot
ARG GODOT_VERSION=4.4.1
ARG BUILDARCH
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl unzip && rm -rf /var/lib/apt/lists/*
RUN for a in $(echo "$BUILDARCH $TARGETARCH" | tr ' ' '\n' | sort -u); do \
      g=$([ "$a" = arm64 ] && echo arm64 || echo x86_64); \
      curl -fsSL -o /tmp/godot.zip "https://github.com/godotengine/godot/releases/download/${GODOT_VERSION}-stable/Godot_v${GODOT_VERSION}-stable_mono_linux_${g}.zip" \
      && unzip -q /tmp/godot.zip -d /tmp/g && rm /tmp/godot.zip \
      && mv /tmp/g/Godot_v${GODOT_VERSION}-stable_mono_linux_${g} /opt/godot-$a && rmdir /tmp/g \
      && ln -s Godot_v${GODOT_VERSION}-stable_mono_linux.${g} /opt/godot-$a/godot || exit 1; \
    done

# The C# build and the resource import do not depend on the CPU, so they run natively on the build host.
FROM --platform=$BUILDPLATFORM mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim AS client-build
ARG BUILDARCH
ARG ASSETS_REF=716625cc6c68e872f253c48efe7f934b77d9ba0c
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
RUN apt-get update && apt-get install -y --no-install-recommends git procps libfontconfig1 && rm -rf /var/lib/apt/lists/*
RUN git init -q /assets && git -C /assets fetch -q --depth 1 https://github.com/C7-Game/Assets.git "$ASSETS_REF" \
 && git -C /assets checkout -q FETCH_HEAD
COPY --from=godot /opt/godot-${BUILDARCH} /opt/godot
COPY vendor/OpenCiv3 /src/vendor/OpenCiv3
COPY client /src/client
RUN --mount=type=cache,target=/root/.nuget/packages \
    GODOT=/opt/godot/godot ASSETS_REF=$ASSETS_REF ASSETS_SRC=/assets /src/client/prepare.sh /work \
 && rm -rf /work/OpenCiv3/*/obj /work/OpenCiv3/C7/.godot/mono/temp/obj /work/OpenCiv3/C7/.godot/editor

FROM env AS client
ARG TARGETARCH
RUN apt-get -o Acquire::Retries=5 update \
 && apt-get -o Acquire::Retries=5 install -y --no-install-recommends procps xvfb xauth libicu76 \
      libgl1 libglx-mesa0 libgl1-mesa-dri libegl1 libxcursor1 libxinerama1 libxrandr2 libxi6 libxkbcommon0 libfontconfig1 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=mcr.microsoft.com/dotnet/runtime:8.0-bookworm-slim /usr/share/dotnet /usr/share/dotnet
COPY --from=godot /opt/godot-${TARGETARCH} /opt/godot
COPY --from=client-build /work/OpenCiv3 /opt/openciv3-client/OpenCiv3
COPY client/capture.sh client/deadline.sh /opt/openciv3-client/
ENV DOTNET_ROOT=/usr/share/dotnet GODOT=/opt/godot/godot OPENCIV_CLIENT=/opt/openciv3-client LIBGL_ALWAYS_SOFTWARE=1 \
    GODOT_ARGS="--rendering-driver opengl3 --rendering-method gl_compatibility"
RUN "$GODOT" --version >/dev/null
# The client's art for the browser's play page (docs/play.md, section 6): converted here so it stays in this image.
RUN python -m agentenv_openciv3.webart /opt/openciv3-client/OpenCiv3/C7 /opt/openciv3-client/webart

# The default target is the env without the client.
FROM env
