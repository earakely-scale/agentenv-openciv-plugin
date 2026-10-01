#!/usr/bin/env bash
# Build a capture-ready OpenCiv3 client tree in <work dir>: the pinned vendor/OpenCiv3 sources, the art
# at the commit the submodule pins for C7/Assets, the FrameCapture autoload and standalone mode; then
# build the C# assemblies (dotnet) and, when GODOT is set, import the Godot resources.
#
# usage: client/prepare.sh <work dir>
# env:   GODOT        Godot 4.4.1 .NET binary used for the resource import (optional)
#        ASSETS_SRC   local checkout of github.com/C7-Game/Assets to copy instead of fetching (optional)
#        ASSETS_REF   Assets commit when vendor/OpenCiv3 is not a git checkout (e.g. inside Docker)
#        OPENCIV3_SRC OpenCiv3 sources (default: vendor/OpenCiv3 next to this directory)
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
src=${OPENCIV3_SRC:-$here/../vendor/OpenCiv3}
work=${1:?usage: prepare.sh <work dir>}
mkdir -p "$work/OpenCiv3"
work=$(cd "$work" && pwd)
c7=$work/OpenCiv3/C7
. "$here/deadline.sh"

ts() { date +%s; }
t0=$(ts)

echo "== sources: $src -> $work/OpenCiv3"
(cd "$src" && tar -cf - --exclude .git --exclude ./C7/Assets --exclude bin --exclude obj --exclude .godot .) \
	| tar -xf - -C "$work/OpenCiv3"

ref=${ASSETS_REF:-$(git -C "$src" ls-tree HEAD C7/Assets | awk '{print $3}')}
[ -n "$ref" ] || { echo "cannot tell the pinned C7/Assets commit; set ASSETS_REF" >&2; exit 1; }
if [ -f "$c7/Assets/.pinned" ] && [ "$(cat "$c7/Assets/.pinned")" = "$ref" ]; then
	echo "== assets: $ref already present"
elif [ -n "${ASSETS_SRC:-}" ]; then
	echo "== assets: copy $ASSETS_SRC @ $ref"
	[ "$(git -C "$ASSETS_SRC" rev-parse HEAD)" = "$ref" ] || { echo "$ASSETS_SRC is not at $ref" >&2; exit 1; }
	rm -rf "$c7/Assets" && mkdir -p "$c7/Assets"
	(cd "$ASSETS_SRC" && tar -cf - --exclude .git .) | tar -xf - -C "$c7/Assets"
	echo "$ref" > "$c7/Assets/.pinned"
else
	echo "== assets: fetch https://github.com/C7-Game/Assets.git @ $ref"
	rm -rf "$c7/Assets" && git init -q "$c7/Assets"
	git -C "$c7/Assets" fetch -q --depth 1 https://github.com/C7-Game/Assets.git "$ref"
	git -C "$c7/Assets" checkout -q FETCH_HEAD
	rm -rf "$c7/Assets/.git"
	echo "$ref" > "$c7/Assets/.pinned"
fi

echo "== FrameCapture autoload, standalone mode"
mkdir -p "$c7/Capture"
cp "$here/FrameCapture.cs" "$c7/Capture/FrameCapture.cs"
if ! grep -q '^FrameCapture=' "$c7/project.godot"; then
	awk '{ print } /^LogManager=/ { print "FrameCapture=\"*res://Capture/FrameCapture.cs\"" }' "$c7/project.godot" > "$c7/project.godot.new"
	mv "$c7/project.godot.new" "$c7/project.godot"
fi
grep -q '^FrameCapture=' "$c7/project.godot" || { echo "could not register the FrameCapture autoload" >&2; exit 1; }
printf '[locations]\nuseStandaloneMode = true\n' > "$c7/C7.ini"
t1=$(ts)

echo "== dotnet build"
dotnet build "$c7/C7.csproj" -c Debug -nologo -v q -clp:ErrorsOnly
t2=$(ts)

if [ -n "${GODOT:-}" ]; then
	echo "== godot --import"
	(cd "$c7" && run_with_deadline "${IMPORT_DEADLINE:-600}" "$work/import.log" "$GODOT" --headless --path . --import)
	ls "$c7/.godot/imported" | grep -q fontdata || { echo "import produced no font data; see $work/import.log" >&2; exit 1; }
fi
t3=$(ts)
echo "prepared $c7 (sources+assets $((t1 - t0))s, build $((t2 - t1))s, import $((t3 - t2))s)"
