#!/usr/bin/env bash
# Copy the pinned OpenCiv3 engine sources into build/engine and apply patches/*.patch.
# The submodule stays untouched. Re-running is a no-op until the sources or patches change.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
src="$root/vendor/OpenCiv3"
dst="$root/build/engine"
parts=(C7Engine QueryCiv3 Blast C7/Lua)

if [ ! -f "$src/C7Engine/C7Engine.csproj" ]; then
	echo "vendor/OpenCiv3 is missing; run: git submodule update --init vendor/OpenCiv3" >&2
	exit 1
fi

sha() { if command -v sha256sum >/dev/null; then sha256sum; else shasum -a 256; fi; }

stamp="$(
	{
		(cd "$src" && find "${parts[@]}" -type f -not -path '*/bin/*' -not -path '*/obj/*' -print0 |
			LC_ALL=C sort -z | xargs -0 cat)
		cat "$root"/patches/*.patch
	} | sha | cut -d' ' -f1
)"

if [ -f "$dst/.stamp" ] && [ "$(cat "$dst/.stamp")" = "$stamp" ]; then
	echo "build/engine is up to date"
	exit 0
fi

rm -rf "$dst"
mkdir -p "$dst"
tar -C "$src" --exclude=bin --exclude=obj -cf - "${parts[@]}" | tar -C "$dst" -xf -

for p in "$root"/patches/*.patch; do
	if command -v git >/dev/null; then
		(cd "$dst" && git apply -p1 "$p")
	else
		patch -d "$dst" -p1 --forward --quiet <"$p"
	fi
	echo "applied $(basename "$p")"
done

echo "$stamp" >"$dst/.stamp"
echo "prepared build/engine"
