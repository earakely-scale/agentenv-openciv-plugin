# The OpenCiv3 client as a renderer

The image's `client` target uses these files to render the real OpenCiv3 client's view of a game, one
frame per turn, with no human at the screen. It runs the game's own map view, art and HUD on Xvfb with
Mesa's CPU renderer (`--rendering-driver opengl3`), so it needs no GPU. The env uses them for the
`client_mp4` recording format ([docs/recording.md](../docs/recording.md#5-the-real-clients-view-client_mp4)).

| File | What it does |
|---|---|
| `prepare.sh` | Builds a client tree in a work dir. It copies the pinned `vendor/OpenCiv3` sources, adds the art at the commit the submodule pins for `C7/Assets` (`716625c`, fetched over HTTPS), the `FrameCapture` autoload and standalone mode. Then it runs `dotnet build` and `godot --import`. |
| `FrameCapture.cs` | The capture autoload, copied into `C7/Capture/`. |
| `capture.sh` | Runs Godot once over a directory of saves and writes one PNG per save (or per save and seat), under a hard deadline. |
| (`agentenv_openciv3.webart`) | Run by the Dockerfile after the client is copied in: converts the client's art for the browser's play page into `webart/` next to the client ([docs/play.md](../docs/play.md#6-the-games-art)). |
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
6. **Writes the PNG.** It waits for `FramePostDraw` and writes the viewport as `<save name>.png`. With
   `--capture-players`, steps 4 to 6 run once per listed civ instead, writing `<save name>.<civ>.png`.

When every save is done it quits. A `--capture-timeout` timer quits with exit code 3 if the run hangs, and
`capture.sh` kills the whole process tree at its own deadline.

Options (after `--`):
- `--capture-zoom=auto|<f>`;
- `--capture-hide-ui`, for the bare map;
- `--capture-settle-frames=<n>`. The default is 20; the env uses 5, which also gave complete frames;
- `--capture-players=<civ>[,<civ>...]`, one frame per save and civ (see below). `capture.sh` also takes it
  as the `CAPTURE_PLAYERS` environment variable;
- `--capture-seat-frames=<n>`, the frames drawn after switching to a civ before its capture. The default is 2;
  1 already gave the same image.

## One view per seat (`--capture-players`)

Without `--capture-players`, a frame is the view of `game.controller`: the first human player in the save,
which is the first civ (`civ`) of a multi-seat game. With `--capture-players=Rome,Greece,Egypt`, each save is
loaded once and captured once per listed civ, as `<save name>.<civ>.png` (e.g. `turn-0010.Greece.png`), in the
order given. The civ names are matched without regard to case. Any civ in the save works, an engine AI as well
as a seat. A name that is not in the save fails the run (exit 2, `CAPTURE_FAILED ... no civ named <civ>`).

The client decides whose view it draws in two places. The map's fog-of-war, tile and resource layers, the minimap
and the status box (civ, gold, research) ask `GameData.GetFirstHumanPlayer()` on every frame. The unit layer
(which units are shown, and the movement lights on your own units) and the selection ask `game.controller`. To
draw for a civ, the capture does what the client's own observer-mode toggle (`Game.SetObserverModeOff`) does:
it marks only that civ as human (`isHuman`). It also points `game.controller` and `EngineStorage.uiControllerID`
at the civ. Then it selects the unit the client would autoselect for that player: the first one that can move
and is not fortified or busy. Finally it frames that civ's explored tiles around its capital. Everything is
redrawn on the next frame, so no reload is needed. The `isHuman` flags of the other seats only matter when a turn
is played, and a capture never plays one. The first civ's frame matches the frame without the option, apart from
the selected unit's animated cursor.

`client.render_seats(saves, seats, fps=...)` uses this to make one mp4 per seat from a single capture run.
`client.frame(save, seat)` draws one save for one seat, for the live view.

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

Per seat, measured natively on a 4-CPU x86_64 Linux machine (Xvfb, llvmpipe, 5 settle frames), over the 11 saves
(turns 0 to 10) of a game with 3 seats on a Small map:

| | time |
|---|---|
| startup, until the first save loads | about 3.5 s |
| loading a save and its settle frames | 0.8 to 1.9 s, 1.6 s on average |
| each seat's frame from a loaded save (`seat_ms` in the log) | 0.1 to 0.3 s, 0.2 s on average |
| 11 saves, without `--capture-players` (11 frames) | 17.5 s in Godot, 21 s wall |
| 11 saves × 3 seats in one run (33 frames) | 20.8 s in Godot, 24 s for `render_seats` with the three mp4s |
| `frame(save, seat)`, one live frame | 6 to 7 s, mostly startup and loading |

So another seat costs about 0.2 s a turn, against about 1.6 s a turn for a separate run.

The client image is about 1.5 GB on disk, against about 390 MB without the client.

## Open issues

- **x86_64:** the CI job `client` renders on GitHub's x86_64 runners. Under QEMU on Apple Silicon,
  Mesa's LLVM JIT aborts, so an amd64 image cannot be checked locally on a Mac.
- **Editor binary:** the renderer runs the Godot editor binary on the project, not an exported build.
  An export would make the image smaller, but needs the export templates and a preset.
- **Engine mismatch:** the bridge's saves come from the patched engine (`patches/`), and the client
  runs the unpatched pin. The patches change turn-loop behaviour, not the save format.
- **Selected unit:** the selected unit's cursor is animated, so its few pixels can differ between runs, and
  between seats' captures of the same save, depending on when the frame was drawn.
- **Loading with several humans:** when a multi-seat save loads, the client's autoselector looks at the units of
  every human civ, not just the controller's. It can run a busy unit's orders (`MsgPerformUnitAction`) before
  the first capture. This is the client's own behaviour on load, and it happens without `--capture-players` too.
  The per-seat selection does not run any orders.
