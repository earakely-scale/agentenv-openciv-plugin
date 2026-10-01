#!/usr/bin/env bash
# Render saves to PNG with the real OpenCiv3 client (a tree built by prepare.sh), one Godot process for all.
#
# usage: client/capture.sh <C7 dir> <saves dir | a.json[,b.json...]> <out dir> [--capture-<opt>=<v> ...]
#        options: --capture-zoom=auto|<f>  --capture-hide-ui  --capture-settle-frames=<n>
# env:   GODOT        Godot 4.4.1 .NET binary (required)
#        DEADLINE     seconds for the whole run (default 300); Godot is killed when it passes
#        RESOLUTION   window size (default 1152x768, the client's own)
#        GODOT_ARGS   extra Godot arguments, e.g. "--rendering-driver opengl3"
# On Linux without $DISPLAY the run is wrapped in xvfb-run.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
. "$here/deadline.sh"
: "${GODOT:?set GODOT to the Godot 4.4.1 .NET binary}"
c7=$(cd "${1:?C7 dir}" && pwd)
saves_arg=${2:?saves}
out=${3:?out dir}
shift 3
deadline=${DEADLINE:-300}
resolution=${RESOLUTION:-1152x768}

abs() { (cd "$(dirname "$1")" && echo "$(pwd)/$(basename "$1")"); }
if [ -d "$saves_arg" ]; then
	saves=$(cd "$saves_arg" && pwd)
else
	saves=$(IFS=,; for s in $saves_arg; do printf '%s,' "$(abs "$s")"; done)
	saves=${saves%,}
fi
mkdir -p "$out"
out=$(cd "$out" && pwd)

cmd=("$GODOT" --path . --resolution "$resolution" --audio-driver Dummy ${GODOT_ARGS:-}
	-- --capture-saves="$saves" --capture-out="$out" --capture-timeout="$deadline" "$@")
if [ "$(uname)" = Linux ] && [ -z "${DISPLAY:-}" ]; then
	cmd=(xvfb-run -a -s "-screen 0 ${resolution}x24" "${cmd[@]}")
fi

cd "$c7"
start=$SECONDS
status=0
run_with_deadline $((deadline + 30)) "$out/capture.log" "${cmd[@]}" || status=$?
grep -E '^(CAPTURED|CAPTURE_)' "$out/capture.log" || true
echo "capture exit=$status wall=$((SECONDS - start))s log=$out/capture.log"
exit "$status"
