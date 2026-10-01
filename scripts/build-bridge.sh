#!/usr/bin/env bash
# Build CivBridge into build/bridge (or --out <dir>).
#   scripts/build-bridge.sh                                  framework-dependent, host platform
#   scripts/build-bridge.sh -r linux-x64 --self-contained    for a container image
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
out="$root/build/bridge"
publish=()

while [ $# -gt 0 ]; do
	case "$1" in
	-r | --runtime) publish+=(-r "$2"); shift 2 ;;
	--self-contained) publish+=(--self-contained true); shift ;;
	-o | --out) out="$2"; shift 2 ;;
	*) echo "usage: $0 [-r <rid>] [--self-contained] [-o <dir>]" >&2; exit 2 ;;
	esac
done

"$root/scripts/prepare-engine.sh"

export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
dotnet publish "$root/bridge/CivBridge/CivBridge.csproj" -c Release -o "$out" ${publish[@]+"${publish[@]}"}
echo "built $out/CivBridge"
