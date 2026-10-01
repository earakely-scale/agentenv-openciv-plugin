# The OpenCiv3 client as a renderer

The image's `client` target uses these files to render the real OpenCiv3 client's view of a game, one
frame per turn, with no human at the screen. It runs the game's own map view, art and HUD on Xvfb with
Mesa's CPU renderer (`--rendering-driver opengl3`), so it needs no GPU. The env uses them for the
`client_mp4` recording format ([docs/recording.md](../docs/recording.md#5-the-real-clients-view-client_mp4)).

| File | What it does |
|---|---|
| `prepare.sh` | Builds a client tree in a work dir. It copies the pinned `vendor/OpenCiv3` sources, adds the art at the commit the submodule pins for `C7/Assets` (`716625c`, fetched over HTTPS), the `FrameCapture` autoload and standalone mode. Then it runs `dotnet build` and `godot --import`. |
| `FrameCapture.cs` | The capture autoload, copied into `C7/Capture/`. |
| `capture.sh` | Runs Godot once over a directory of saves and writes one PNG per save, under a hard deadline. |
| `deadline.sh` | `run_with_deadline`, used by both scripts. macOS has no `timeout(1)`, and a Godot window never exits on its own. |

## How a capture works

`vendor/OpenCiv3` is never modified. `prepare.sh` adds two things to its copy:
`C7/Capture/FrameCapture.cs` and one autoload line in `project.godot`. `C7.ini` turns on standalone mode,
so no Civilization III files are needed.

The autoload does nothing unless `-- --capture-saves=<dir or list>` is on the command line. For each save
(`*.json`, or the bridge's `--saves` files, `turn-NNNN.json.gz`) it:

1. **Unwraps the save.** It unwraps the bridge's `{"format", "bridge", "game"}` envelope into a plain
   `.json` of the engine save.
2. **Loads it.** It sets `GlobalSingleton.LoadGamePath` and switches to `C7Game.tscn`, the same path the
   main menu's Load uses.
3. **Waits** for the map view, then for some settle frames.
4. **Stabilises the frame.** It stops the next-turn hint's blink timer, so consecutive frames match.
5. **Frames the view.** It fits the human's explored tiles into the view: wrap-aware around the capital,
   with zoom between 0.25 and 1.0.
6. **Writes the PNG.** It waits for `FramePostDraw` and writes the viewport as `<save name>.png`.

When every save is done it quits. A `--capture-timeout` timer quits with exit code 3 if the run hangs, and
`capture.sh` kills the whole process tree at its own deadline.

Options (after `--`):
- `--capture-zoom=auto|<f>`;
- `--capture-hide-ui`, for the bare map;
- `--capture-settle-frames=<n>`. The default is 20; the env uses 5, which also gave complete frames.

## Run it on a Mac (debugging)

Prerequisites:
- the .NET 8 SDK, git and python3;
- Godot 4.4.1 .NET for macOS, from
  `https://github.com/godotengine/godot/releases/download/4.4.1-stable/Godot_v4.4.1-stable_mono_macos.universal.zip`.

```bash
cd agentenv-openciv-plugin
export DOTNET_ROOT=/path/to/dotnet PATH=/path/to/dotnet:$PATH
export GODOT=/path/to/Godot_mono.app/Contents/MacOS/Godot
scripts/build-bridge.sh
printf '%s\n' '{"id":1,"cmd":"new_game","args":{"seed":1,"turn_limit":40}}' \
  '{"id":2,"cmd":"autoplay","args":{"turns":40,"policy":"settler_bot"}}' \
  | build/bridge/CivBridge --saves /tmp/civ/saves > /dev/null       # 41 saves in about 1 s
client/prepare.sh /tmp/civ/client                                  # ASSETS_SRC=<local Assets clone> skips the fetch
client/capture.sh /tmp/civ/client/OpenCiv3/C7 /tmp/civ/saves /tmp/civ/frames
```

A game window is open for the length of the run, because Metal rendering needs it. To render
`client_mp4` from a locally served env, point `OPENCIV_CLIENT` at a directory that holds `capture.sh`,
`deadline.sh` and the prepared `OpenCiv3/` tree, and set `GODOT`.

## Measured

These were measured on an Apple M4 Pro, natively and in a 4-CPU linux/arm64 Docker VM:

| | macOS, Metal | Linux Docker, llvmpipe |
|---|---|---|
| startup, until the first save loads | about 3 s | about 3 s |
| per frame, 20 settle frames | 1.0 s | 1.31 s |
| per frame, 5 settle frames | not measured | 0.76 s |
| one `client_mp4` of 21 turns, through the env | not measured | 21 s for the whole recording call (with `mp4` and `html`) |

The client image is about 1.5 GB on disk, against about 390 MB without the client.

## Open issues

- **x86_64:** the CI job `client` renders on GitHub's x86_64 runners. Under QEMU on Apple Silicon,
  Mesa's LLVM JIT aborts, so an amd64 image cannot be checked locally on a Mac.
- **Editor binary:** the renderer runs the Godot editor binary on the project, not an exported build.
  An export would make the image smaller, but needs the export templates and a preset.
- **Engine mismatch:** the bridge's saves come from the patched engine (`patches/`), and the client
  runs the unpatched pin. The patches change turn-loop behaviour, not the save format.
