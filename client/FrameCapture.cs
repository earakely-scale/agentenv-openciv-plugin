using Godot;
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;
using C7Engine;
using C7GameData;

// Autoload that turns the OpenCiv3 client into a batch renderer: load each save through the normal
// in-game load path, frame the human's explored area, write the viewport to PNG, quit.
// With --capture-players=<civ>,<civ>,... each save is loaded once and drawn once per listed civ, as if that
// civ were the human at the screen (its fog of war, units, status box and framing).
// Inert unless --capture-saves is passed after "--" on the Godot command line.
public partial class FrameCapture : Node {
	Dictionary<string, string> opts = new();
	string Opt(string k, string d) => opts.TryGetValue(k, out var v) ? v : d;

	public override void _Ready() {
		foreach (string a in OS.GetCmdlineUserArgs()) {
			if (!a.StartsWith("--capture-")) continue;
			int eq = a.IndexOf('=');
			opts[eq < 0 ? a[2..] : a[2..eq]] = eq < 0 ? "1" : a[(eq + 1)..];
		}
		if (!opts.ContainsKey("capture-saves")) return;
		double limit = double.Parse(Opt("capture-timeout", "300"), System.Globalization.CultureInfo.InvariantCulture);
		GetTree().CreateTimer(limit, processAlways: true).Timeout += () => {
			GD.PrintErr($"CAPTURE_TIMEOUT after {limit}s");
			GetTree().Quit(3);
		};
		_ = RunSafe();
	}

	async Task RunSafe() {
		try {
			await Run();
		} catch (Exception e) {
			GD.PrintErr($"CAPTURE_FAILED {e}");
			GetTree().Quit(2);
		}
	}

	async Task Frames(int n) {
		for (int i = 0; i < n; i++) await ToSignal(GetTree(), SceneTree.SignalName.ProcessFrame);
	}

	static bool IsSave(string path) => path.EndsWith(".json", StringComparison.Ordinal) || path.EndsWith(".json.gz", StringComparison.Ordinal);

	static string Stem(string path) => Path.GetFileName(path) is var n && n.EndsWith(".gz", StringComparison.Ordinal)
		? Path.GetFileNameWithoutExtension(Path.GetFileNameWithoutExtension(n)) : Path.GetFileNameWithoutExtension(n);

	// CivBridge saves (--autosave, and --saves as .json.gz) wrap the engine save as {"format", "bridge", "game"};
	// the client loads the bare "game" from a plain .json file.
	static string Unwrap(string save, string tmpDir) {
		using Stream raw = File.OpenRead(save);
		using Stream input = save.EndsWith(".gz", StringComparison.Ordinal) ? new GZipStream(raw, CompressionMode.Decompress) : raw;
		using JsonDocument doc = JsonDocument.Parse(input);
		JsonElement game = doc.RootElement.TryGetProperty("game", out JsonElement inner) ? inner : doc.RootElement;
		string path = Path.Combine(tmpDir, Stem(save) + ".json");
		File.WriteAllText(path, game.GetRawText());
		return path;
	}

	async Task Run() {
		string src = Opt("capture-saves", "");
		List<string> saves = Directory.Exists(src)
			? Directory.GetFiles(src).Where(IsSave).OrderBy(p => p, StringComparer.Ordinal).ToList()
			: src.Split(',').ToList();
		string outDir = Opt("capture-out", "frames");
		int settle = int.Parse(Opt("capture-settle-frames", "20"));
		string zoomArg = Opt("capture-zoom", "auto");
		bool hideUi = Opt("capture-hide-ui", "0") == "1";
		// Civ names; empty: one frame per save, drawn for the controller as the client loads it.
		string[] players = Opt("capture-players", "").Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
		// Frames to draw after switching to a player: the map, minimap and status box redraw every frame.
		int seatFrames = int.Parse(Opt("capture-seat-frames", "2"));
		Directory.CreateDirectory(outDir);
		string tmpDir = Directory.CreateTempSubdirectory("c7-capture-").FullName;

		// Let the main menu run its _Ready (it clears GlobalSingleton's load fields) before we set ours.
		await Frames(2);
		var global = GetNode<GlobalSingleton>("/root/GlobalSingleton");
		Node previous = GetTree().CurrentScene;
		var total = Stopwatch.StartNew();

		foreach (string save in saves) {
			var sw = Stopwatch.StartNew();
			global.LoadGamePath = Unwrap(Path.GetFullPath(save), tmpDir);
			GetTree().ChangeSceneToFile("res://C7Game.tscn");

			Game game = null;
			MapView mapView = null;
			for (int i = 0; i < 600 && mapView == null; i++) {
				await Frames(1);
				game = GetTree().CurrentScene as Game;
				if (game != null && game != previous && game.controller != null)
					mapView = game.GetChildren().OfType<MapView>().FirstOrDefault();
			}
			if (mapView == null) throw new Exception($"game scene did not come up for {save}");
			previous = game;
			long loadMs = sw.ElapsedMilliseconds;

			await Frames(settle);
			if (hideUi) HideUi(game);
			if (players.Length == 0) {
				StopBlinking(game);
				Frame(game.controller, mapView, zoomArg);
				await Frames(settle);
				string shot = await Capture(Path.Combine(outDir, Stem(save) + ".png"));
				GD.Print($"CAPTURED {shot} zoom={mapView.cameraZoom:F2} load_ms={loadMs} total_ms={sw.ElapsedMilliseconds}");
				continue;
			}
			foreach (string civ in players) {
				var seatSw = Stopwatch.StartNew();
				Player player = FindPlayer(civ) ?? throw new Exception($"no civ named {civ} in {save}");
				ViewAs(game, player);
				StopBlinking(game);
				Frame(player, mapView, zoomArg);
				await Frames(seatFrames);
				string shot = await Capture(Path.Combine(outDir, $"{Stem(save)}.{civ}.png"));
				GD.Print($"CAPTURED {shot} civ={civ} zoom={mapView.cameraZoom:F2} load_ms={loadMs} seat_ms={seatSw.ElapsedMilliseconds}");
			}
		}
		Directory.Delete(tmpDir, recursive: true);
		GD.Print($"CAPTURE_DONE frames={saves.Count * Math.Max(players.Length, 1)} ms={total.ElapsedMilliseconds}");
		GetTree().Quit(0);
	}

	// Waits for the next drawn frame and writes the viewport to `png`; returns "<png> <w>x<h> turn=<n>" for the log.
	async Task<string> Capture(string png) {
		await ToSignal(RenderingServer.Singleton, RenderingServer.SignalName.FramePostDraw);
		Image img = GetViewport().GetTexture().GetImage();
		img.SavePng(png);
		return $"{png} {img.GetWidth()}x{img.GetHeight()} turn={EngineStorage.gameData.turn}";
	}

	static Player FindPlayer(string civ) => EngineStorage.gameData.players.Find(
		p => string.Equals(p.civilization?.name, civ, StringComparison.OrdinalIgnoreCase));

	// Make `player` the one the client draws for. The map's fog, unit and resource layers, the minimap and the
	// status box all ask GameData.GetFirstHumanPlayer() (or game.controller) every frame, so this is the client's own
	// observer-mode toggle (Game.SetObserverModeOff): only the viewing civ is human. The other seats' isHuman flags
	// only matter when a turn is played, which a capture never does. Then select the unit the client would
	// autoselect for that player, without UnitSelector's side effect of running busy units' orders.
	static void ViewAs(Game game, Player player) {
		foreach (Player p in EngineStorage.gameData.players) p.isHuman = p == player;
		EngineStorage.uiControllerID = player.id;
		game.controller = player;
		MapUnit next = player.units.FirstOrDefault(u => u.movementPoints.canMove && !u.isFortified && !u.IsBusy()) ?? MapUnit.NONE;
		game.unitSelector.SetSelectedUnit(next);
	}

	static IEnumerable<Node> Descendants(Node n) => n.GetChildren().SelectMany(c => Descendants(c).Prepend(c));

	// The lower-right box blinks a next-turn hint on a timer, so consecutive frames would flicker.
	static void StopBlinking(Game game) {
		foreach (LowerRightInfoBox box in Descendants(game).OfType<LowerRightInfoBox>()) {
			foreach (Godot.Timer t in Descendants(box).OfType<Godot.Timer>()) t.Stop();
			foreach (Godot.Label l in Descendants(box).OfType<Godot.Label>().Where(l => l.Text.StartsWith("ENTER or SPACEBAR"))) l.Visible = false;
		}
	}

	static void HideUi(Game game) {
		foreach (CanvasLayer layer in game.FindChildren("*", "CanvasLayer", true, false).OfType<CanvasLayer>())
			layer.Visible = false;
	}

	// Fit the player's explored tiles (wrap-aware around its capital) into the viewport.
	static void Frame(Player player, MapView mv, string zoomArg) {
		GameMap map = EngineStorage.gameData.map;
		Tile anchor = player.cities.Find(c => c.IsCapital())?.location
			?? player.cities.FirstOrDefault()?.location
			?? player.units.FirstOrDefault()?.location
			?? player.tileKnowledge.knownTiles.FirstOrDefault();
		if (anchor == null) return;

		int W = map.numTilesWide, H = map.numTilesTall;
		int Rel(int v, int a, int n, bool wrap) => wrap ? ((v - a + n / 2) % n + n) % n - n / 2 + a : v;
		var xs = new List<int>(); var ys = new List<int>();
		foreach (Tile t in player.tileKnowledge.knownTiles) {
			xs.Add(Rel(t.XCoordinate, anchor.XCoordinate, W, map.wrapHorizontally));
			ys.Add(Rel(t.YCoordinate, anchor.YCoordinate, H, map.wrapVertically));
		}
		if (xs.Count == 0) { xs.Add(anchor.XCoordinate); ys.Add(anchor.YCoordinate); }

		Vector2 view = mv.getVisibleAreaSize();
		const float topUi = 70, bottomUi = 150;
		Vector2 span = new Vector2(xs.Max() - xs.Min() + 4, ys.Max() - ys.Min() + 4) * MapView.cellSize;
		float zoom = zoomArg == "auto"
			? Math.Clamp(Math.Min(view.X / span.X, (view.Y - topUi - bottomUi) / span.Y), 0.25f, 1.0f)
			: float.Parse(zoomArg, System.Globalization.CultureInfo.InvariantCulture);
		mv.setCameraZoom(zoom, Vector2.Zero);

		Vector2 centerTile = new Vector2((xs.Min() + xs.Max()) / 2f + 1, (ys.Min() + ys.Max()) / 2f + 1);
		Vector2 centerPx = centerTile * mv.scaledCellSize;
		mv.setCameraLocation(centerPx - new Vector2(view.X / 2, topUi + (view.Y - topUi - bottomUi) / 2));
	}
}
