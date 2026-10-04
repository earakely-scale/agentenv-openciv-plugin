using System.IO.Compression;
using System.Text.Json;
using System.Text.Json.Nodes;
using C7GameData;
using Serilog;

namespace CivBridge;

// The world snapshot (docs/recording.md, schema 2 in docs/viewer.md): every tile, city and unit regardless of fog,
// the players with their scores, and the seats' events of the turn that just ended. --record writes one per turn.
// Like every observation it must not draw from GameData.rng or change game state.
sealed partial class Session {
	// Stand-ins for Civ III's 32 player colours (the originals are palettes in the game's art files).
	static readonly int[][] Palette = [
		[128, 128, 128], [196, 52, 52], [232, 120, 170], [230, 196, 40], [236, 128, 36], [56, 156, 64], [64, 104, 200], [148, 76, 188],
		[136, 92, 52], [220, 220, 220], [56, 188, 208], [32, 56, 136], [156, 204, 56], [120, 28, 40], [212, 172, 112], [20, 112, 92],
		[248, 160, 120], [96, 64, 140], [180, 40, 120], [96, 128, 40], [40, 140, 160], [200, 96, 40], [120, 160, 220], [168, 148, 40],
		[88, 88, 88], [232, 216, 160], [160, 48, 48], [48, 168, 112], [104, 72, 40], [184, 132, 200], [24, 80, 48], [216, 80, 96],
	];

	JsonObject World() => WorldSnapshot(shownSeq);

	const int SnapshotSchema = 2;

	/// <summary>The snapshot, with the moves and battles after `since` in their sequence.</summary>
	JsonObject WorldSnapshot(int since) {
		var index = gd.players.Select((p, i) => (p, i)).ToDictionary(x => x.p, x => x.i);
		var indexById = gd.players.Select((p, i) => (p, i)).ToDictionary(x => x.p.id, x => x.i);
		int[][] colors = PlayerColors();
		// Bit k of a tile's `known` is seats[k]; seats are opponent slots, so at most 31 fit a positive int.
		var known = seats.Select(s => s.Player.tileKnowledge.knownTiles).ToArray();
		// A resource shows once some civ knows of it (Player.KnowsAboutResource): Iron, say, only once a civ has Bronze Working.
		var civs = gd.players.Where(p => !p.isBarbarians).ToList();
		var resources = new Dictionary<Resource, bool>();
		bool Discovered(Resource r) {
			if (!resources.TryGetValue(r, out bool k)) resources[r] = k = civs.Any(p => p.KnowsAboutResource(r));
			return k;
		}
		var tiles = new JsonArray();
		foreach (Tile t in gd.map.tiles) {
			Player owner = t.OwningPlayer();
			int mask = 0;
			for (int k = 0; k < known.Length; k++)
				if (known[k].Contains(t)) mask |= 1 << k;
			tiles.Add(new JsonArray(
				t.XCoordinate, t.YCoordinate, t.baseTerrainType.Key,
				t.overlayTerrainType != t.baseTerrainType ? t.overlayTerrainType.Key : null,
				owner == null ? -1 : index[owner], RiverMask(t), mask,
				t.Resource != null && t.Resource != Resource.NONE && Discovered(t.Resource) ? t.Resource.Name : null,
				Json.Strings(ImprovementKeys(t)), BonusGrassland(t) ? 1 : 0));
		}
		return new JsonObject {
			["schema"] = SnapshotSchema,
			["turn"] = gd.turn,
			["turn_limit"] = turnLimit,
			["seed"] = seed,
			["map"] = new JsonObject { ["width"] = gd.map.numTilesWide, ["height"] = gd.map.numTilesTall, ["wrap_x"] = gd.map.wrapHorizontally },
			["seats"] = Json.Array(seats, s => new JsonObject { ["index"] = index[s.Player], ["civ"] = Owner(s.Player), ["label"] = s.Label }),
			["players"] = Json.Array(gd.players, p => new JsonObject {
				["index"] = index[p],
				["civ"] = Owner(p),
				["is_human"] = SeatOf(p) != null,
				["label"] = SeatOf(p)?.Label,
				["defeated"] = p.defeated,
				["color"] = new JsonArray(colors[index[p]].Select(v => (JsonNode)v).ToArray()),
				["score"] = ScoreOf(p),
				["gold"] = p.gold,
				["government"] = p.government?.name,
				["research"] = gd.GetTech(p.currentlyResearchedTech)?.Name,
				// Barbarians are at war with everyone (PlayerRelationship.AtWar), so they are left out on both sides.
				["at_war"] = Ints(p.isBarbarians ? [] : gd.players.Where(o => o != p && !o.isBarbarians && PlayerRelationship.AtWar(p, o)).Select(o => index[o])),
				["contacts"] = Ints(p.playerRelationships.Keys.Where(indexById.ContainsKey).Select(id => indexById[id])
					.Where(i => i != index[p] && !gd.players[i].isBarbarians).Order()),
			}),
			["tiles"] = tiles,
			// Ids are the engine's own (City.id, MapUnit.id) as strings, "city-3" and "Warrior-12": unique in the game, kept
			// for an object's life, and carried through saves.
			["cities"] = Json.Array(gd.cities, c => new JsonObject {
				["id"] = c.id?.ToString(),
				["x"] = c.location.XCoordinate, ["y"] = c.location.YCoordinate, ["name"] = c.name, ["owner"] = index[c.owner],
				["size"] = c.residents.Count, ["capital"] = c.IsCapital(), ["production"] = c.itemBeingProduced?.name,
				["era"] = Math.Clamp(c.owner.EraIndex(), 0, EraNames.Length - 1), ["walls"] = c.HasWalls(),
			}),
			["units"] = Json.Array(gd.mapUnits.Where(u => Tile.IsTileValid(u.location)), u => new JsonObject {
				["id"] = u.id?.ToString(),
				["x"] = u.location.XCoordinate, ["y"] = u.location.YCoordinate, ["owner"] = index[u.owner], ["type"] = u.unitType.name,
				["hp"] = u.hitPointsRemaining, ["hp_max"] = u.maxHitPoints, ["fortified"] = u.isFortified,
			}),
			// What happened since the last snapshot, in order (patches/0010 and 0012): each unit's steps and every battle.
			["moves"] = SnapshotMoves(since),
			["battles"] = SnapshotBattles(since),
			["victory"] = VictoryJson(),
			["events"] = new JsonArray(seats.SelectMany(s => s.TurnEvents.Select(e => {
				JsonObject copy = e.DeepClone().AsObject();
				if (MultiSeat) copy["civ"] = Owner(s.Player);
				return (JsonNode)copy;
			})).ToArray()),
		};
	}

	static JsonArray Ints(IEnumerable<int> values) => new(values.Select(v => (JsonNode)v).ToArray());

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

	/// <summary>Writes the turn's snapshot (with --record), with the moves and battles since the last one; either way, the
	/// next one starts its moves and battles here.</summary>
	void Record() {
		if (recordDir != null) {
			string path = System.IO.Path.Combine(recordDir, $"turn-{gd.turn:0000}.json.gz");
			try {
				using FileStream file = File.Create(path);
				using var gzip = new GZipStream(file, CompressionLevel.Fastest);
				using var w = new Utf8JsonWriter(gzip);
				WorldSnapshot(recordedSeq).WriteTo(w);
			} catch (Exception e) {
				Log.Warning(e, "recording {Path} failed", path);
			}
		}
		(shownSeq, recordedSeq) = (recordedSeq, eventSeq);
	}
}
