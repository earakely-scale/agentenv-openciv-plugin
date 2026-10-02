using System.IO.Compression;
using System.Text.Json;
using System.Text.Json.Nodes;
using C7GameData;
using Serilog;

namespace CivBridge;

// The world snapshot (docs/recording.md): every tile, city and unit regardless of fog, the players with
// their scores, and the seats' events of the turn that just ended. --record writes one per turn.
sealed partial class Session {
	// Stand-ins for Civ III's 32 player colours (the originals are palettes in the game's art files).
	static readonly int[][] Palette = [
		[128, 128, 128], [196, 52, 52], [232, 120, 170], [230, 196, 40], [236, 128, 36], [56, 156, 64], [64, 104, 200], [148, 76, 188],
		[136, 92, 52], [220, 220, 220], [56, 188, 208], [32, 56, 136], [156, 204, 56], [120, 28, 40], [212, 172, 112], [20, 112, 92],
		[248, 160, 120], [96, 64, 140], [180, 40, 120], [96, 128, 40], [40, 140, 160], [200, 96, 40], [120, 160, 220], [168, 148, 40],
		[88, 88, 88], [232, 216, 160], [160, 48, 48], [48, 168, 112], [104, 72, 40], [184, 132, 200], [24, 80, 48], [216, 80, 96],
	];

	JsonObject World() => WorldSnapshot();

	JsonObject WorldSnapshot() {
		var index = gd.players.Select((p, i) => (p, i)).ToDictionary(x => x.p, x => x.i);
		int[][] colors = PlayerColors();
		var tiles = new JsonArray();
		foreach (Tile t in gd.map.tiles) {
			Player owner = t.OwningPlayer();
			tiles.Add(new JsonArray(
				t.XCoordinate, t.YCoordinate, t.baseTerrainType.Key,
				t.overlayTerrainType != t.baseTerrainType ? t.overlayTerrainType.Key : null,
				owner == null ? -1 : index[owner], t.BordersRiver() ? 1 : 0, seats.Any(s => s.Player.tileKnowledge.isTileKnown(t)) ? 1 : 0));
		}
		return new JsonObject {
			["turn"] = gd.turn,
			["turn_limit"] = turnLimit,
			["seed"] = seed,
			["map"] = new JsonObject { ["width"] = gd.map.numTilesWide, ["height"] = gd.map.numTilesTall, ["wrap_x"] = gd.map.wrapHorizontally },
			["players"] = Json.Array(gd.players, p => new JsonObject {
				["index"] = index[p],
				["civ"] = Owner(p),
				["is_human"] = SeatOf(p) != null,
				["label"] = SeatOf(p)?.Label,
				["defeated"] = p.defeated,
				["color"] = new JsonArray(colors[index[p]].Select(v => (JsonNode)v).ToArray()),
				["score"] = ScoreOf(p),
			}),
			["tiles"] = tiles,
			["cities"] = Json.Array(gd.cities, c => new JsonObject {
				["x"] = c.location.XCoordinate, ["y"] = c.location.YCoordinate, ["name"] = c.name, ["owner"] = index[c.owner],
				["size"] = c.residents.Count, ["capital"] = c.IsCapital(),
			}),
			["units"] = Json.Array(gd.mapUnits.Where(u => Tile.IsTileValid(u.location)), u => new JsonObject {
				["x"] = u.location.XCoordinate, ["y"] = u.location.YCoordinate, ["owner"] = index[u.owner], ["type"] = u.unitType.name,
			}),
			["victory"] = VictoryJson(),
			["events"] = new JsonArray(seats.SelectMany(s => s.TurnEvents.Select(e => {
				JsonObject copy = e.DeepClone().AsObject();
				if (MultiSeat) copy["civ"] = Owner(s.Player);
				return (JsonNode)copy;
			})).ToArray()),
		};
	}

	/// <summary>
	/// Each civ's primary colour; a civ whose primary another player already has gets its secondary, then
	/// the first free one. Barbarians are grey.
	/// </summary>
	int[][] PlayerColors() {
		var taken = new HashSet<int> { 0 };
		return gd.players.Select(p => {
			if (p.isBarbarians) return Palette[0];
			int pick = new[] { p.civilization.primaryColorIndex, p.civilization.secondaryColorIndex }
				.Concat(Enumerable.Range(1, Palette.Length - 1)).First(i => i > 0 && i < Palette.Length && !taken.Contains(i));
			taken.Add(pick);
			return Palette[pick];
		}).ToArray();
	}

	void Record() {
		if (recordDir == null) return;
		string path = System.IO.Path.Combine(recordDir, $"turn-{gd.turn:0000}.json.gz");
		try {
			using FileStream file = File.Create(path);
			using var gzip = new GZipStream(file, CompressionLevel.Fastest);
			using var w = new Utf8JsonWriter(gzip);
			WorldSnapshot().WriteTo(w);
		} catch (Exception e) {
			Log.Warning(e, "recording {Path} failed", path);
		}
	}
}
