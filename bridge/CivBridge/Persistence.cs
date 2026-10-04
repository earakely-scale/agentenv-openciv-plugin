using System.IO.Compression;
using System.Reflection;
using System.Text.Json;
using System.Text.Json.Nodes;
using C7Engine;
using C7Engine.Lua;
using C7GameData;
using C7GameData.Save;
using Serilog;

namespace CivBridge;

// Autosave and load: every human turn start writes <autosave dir>/autosave.json, the engine's own save
// plus the bridge's state (each seat's ids, standing orders, decision sources and event memory, the RNG
// state), so a new bridge process can pick the game up from the start of that turn.
sealed partial class Session {
	const int SaveFormat = 2;

	// The engine's save serializer settings (camelCase, fields, its converters), without the indentation.
	static readonly JsonSerializerOptions EngineJson = new(
		(JsonSerializerOptions)typeof(SaveGame).GetProperty("JsonOptions", BindingFlags.NonPublic | BindingFlags.Static)!.GetValue(null)!) {
		WriteIndented = false,
	};

	public string AutosavePath => autosaveDir == null ? null : System.IO.Path.Combine(autosaveDir, "autosave.json");

	void Autosave() {
		if (autosaveDir == null) return;
		string tmp = AutosavePath + ".tmp";
		try {
			using (FileStream stream = File.Create(tmp))
			using (var w = new Utf8JsonWriter(stream)) {
				w.WriteStartObject();
				w.WriteNumber("format", SaveFormat);
				w.WritePropertyName("bridge");
				BridgeState().WriteTo(w);
				w.WritePropertyName("game");
				JsonSerializer.Serialize(w, SaveGame.FromGameData(gd), EngineJson);
				w.WriteEndObject();
			}
			File.Move(tmp, AutosavePath, overwrite: true);
		} catch (Exception e) {
			// A failed autosave must not fail the turn; the previous autosave stays in place.
			Log.Warning(e, "autosave to {Path} failed", AutosavePath);
			return;
		}
		if (savesDir != null) KeepSave();
	}

	/// <summary>--saves keeps every turn's autosave as turn-NNNN.json.gz, for renderers that load engine saves.</summary>
	void KeepSave() {
		string path = System.IO.Path.Combine(savesDir, $"turn-{gd.turn:0000}.json.gz"), tmp = path + ".tmp";
		try {
			using (FileStream source = File.OpenRead(AutosavePath))
			using (FileStream file = File.Create(tmp))
			using (var gzip = new GZipStream(file, CompressionLevel.Fastest))
				source.CopyTo(gzip);
			File.Move(tmp, path, overwrite: true);
		} catch (Exception e) {
			Log.Warning(e, "keeping the save {Path} failed", path);
		}
	}

	JsonObject BridgeState() {
		var states = new JsonArray();
		EachSeat(_ => states.Add(SeatState()));
		return new JsonObject {
			["seed"] = seed,
			["turn_limit"] = turnLimit,
			["seats"] = states,
			["victory"] = victory == null ? null : new JsonObject {
				["kind"] = victory.Kind, ["civ"] = Owner(victory.Player), ["turn"] = victory.Turn,
			},
			["rng"] = RngState.Get(GameData.rng),
			["battles"] = BattlesState(),
		};
	}

	JsonObject SeatState() {
		JsonArray Cities(IEnumerable<City> cities) => Json.Strings(cities.Select(ids.Of).Where(id => id != null));
		var (units, cityCount) = ids.Counters;
		return new JsonObject {
			["civ"] = Owner(human),
			["label"] = seat.Label,
			["ids"] = new JsonObject {
				["units"] = units, ["cities"] = cityCount,
				["live"] = new JsonObject(ids.Live(gd, human).Select(p => KeyValuePair.Create(EngineId(p.Object), (JsonNode)p.Id))),
			},
			["orders"] = Json.Array(orders.Where(kv => ids.Of(kv.Key) != null), kv => new JsonObject {
				["unit"] = ids.Of(kv.Key), ["kind"] = kv.Value.Kind, ["x"] = kv.Value.Target.XCoordinate, ["y"] = kv.Value.Target.YCoordinate,
			}),
			["exploring"] = Json.Strings(HumanUnits().Where(u => u.isAutomated && u.currentAI is ExplorerAI).Select(ids.Of)),
			["producing_source"] = new JsonObject(producingSource.Where(kv => ids.Of(kv.Key) != null)
				.Select(kv => KeyValuePair.Create(ids.Of(kv.Key), (JsonNode)kv.Value))),
			["pending_production"] = Cities(pendingProduction),
			["queues"] = new JsonObject(queues.Where(kv => ids.Of(kv.Key) != null && human.cities.Contains(kv.Key))
				.Select(kv => KeyValuePair.Create(ids.Of(kv.Key), (JsonNode)Json.Strings(kv.Value.Select(p => p.name))))),
			["research_source"] = researchSource,
			["research_pending"] = researchPending,
			["agent_research"] = Json.Strings(agentResearch.Select(t => t.Name)),
			["decided"] = new JsonObject(decided.Select(kv => KeyValuePair.Create(kv.Key, (JsonNode)kv.Value))),
			["last_events"] = Json.Array(lastEvents, e => e.DeepClone()),
			["turn_events"] = Json.Array(turnEvents, e => e.DeepClone()),
			["shields_lost"] = new JsonObject(shieldsLost.Where(kv => ids.Of(kv.Key) != null)
				.Select(kv => KeyValuePair.Create(ids.Of(kv.Key), (JsonNode)kv.Value))),
			["riot_risk"] = Cities(riskSeen),
			["capped"] = Cities(cappedSeen),
			["defenseless"] = Cities(defenselessSeen),
			["threats_seen"] = Json.Strings(threatsSeen.Where(gd.mapUnits.Contains).Select(EngineId)),
			["bot_explorer"] = botExplorer == null ? null : ids.Of(botExplorer),
			["revolution_target"] = revolutionTarget?.name,
			["rates_before_anarchy"] = ratesBeforeAnarchy is var (science, luxury) ? new JsonArray(science, luxury) : null,
			["peace_offers"] = Json.Array(seat.PeaceOffers, kv => new JsonObject {
				["civ"] = Owner(kv.Key), ["gold"] = kv.Value.Gold, ["turn"] = kv.Value.Turn,
			}),
		};
	}

	static string EngineId(object o) => o switch { MapUnit u => u.id.ToString(), City c => c.id.ToString(), _ => null };

	JsonObject Load(Args a) {
		EnsureFresh();
		string path = a.Str("path");
		SaveGame save;
		JsonObject state;
		try {
			using JsonDocument doc = JsonDocument.Parse(File.ReadAllBytes(path));
			JsonElement root = doc.RootElement;
			if (!root.TryGetProperty("format", out JsonElement format) || format.GetInt32() != SaveFormat
				|| !root.TryGetProperty("bridge", out JsonElement bridge) || !root.TryGetProperty("game", out JsonElement game))
				throw new BridgeError("bad_save", $"{path} is not a CivBridge autosave (format {SaveFormat}).");
			save = game.Deserialize<SaveGame>(EngineJson);
			state = JsonNode.Parse(bridge.GetRawText())!.AsObject();
		} catch (Exception e) when (e is IOException or UnauthorizedAccessException or JsonException or InvalidOperationException) {
			throw new BridgeError("bad_save", $"Cannot read the save {path}: {e.Message}");
		}

		mode ??= GameMode.Load(luaDir, new GameMode.Config("civ3", ["standalone"]));
		try {
			CreateGame.createGame(save, _ => mode.behaviors).GetAwaiter().GetResult();
		} catch (Exception e) {
			Log.Error(e, "loading {Path} failed", path);
			throw new BridgeError("bad_save", $"The engine could not restore {path}: {e.GetType().Name}: {e.Message}");
		}
		gd = EngineStorage.gameData;
		TurnHandling.OnBeginTurn();
		Restore(state);
		DrainUi();
		EachSeat(_ => ids.Sync(gd, human));
		Record();
		return GameInfo();
	}

	void Restore(JsonObject state) {
		seed = (int)state["seed"];
		turnLimit = (int)state["turn_limit"];
		foreach (JsonNode s in state["seats"]!.AsArray()) {
			Player p = gd.players.FirstOrDefault(x => !x.isBarbarians && x.civilization.name == (string)s["civ"])
				?? throw new BridgeError("bad_save", $"The save has a seat for {(string)s["civ"]}, which is not in its game.");
			seats.Add(seat = new Seat(p, (string)s["label"]));
			RestoreSeat(s!.AsObject());
			SeeRelations();
		}
		seat = seats[0];
		RestoreBattles(state["battles"] as JsonObject);
		if (state["victory"] is JsonObject v)
			victory = new Victory((string)v["kind"], Civs().First(p => Owner(p) == (string)v["civ"]), (int)v["turn"]);
		if (state["rng"] is JsonObject rng && !RngState.Set(GameData.rng, rng))
			Log.Warning("could not restore the engine's random state; the restored game continues from a fresh one");
	}

	void RestoreSeat(JsonObject s) {
		var byEngineId = gd.mapUnits.Where(u => u.owner == human).Cast<object>().Concat(gd.cities.Where(c => c.owner == human))
			.ToDictionary(EngineId);
		JsonObject idState = s["ids"]!.AsObject();
		foreach (var (engineId, shortId) in idState["live"]!.AsObject())
			if (byEngineId.TryGetValue(engineId, out object o)) ids.Add(o, (string)shortId);
		ids.Counters = ((int)idState["units"], (int)idState["cities"]);

		City CityOf(JsonNode id) => ids.Find<City>((string)id);
		MapUnit UnitOf(JsonNode id) => ids.Find<MapUnit>((string)id);
		IEnumerable<City> Cities(string key) => s[key]!.AsArray().Select(CityOf).Where(c => c != null);

		foreach (JsonNode o in s["orders"]!.AsArray())
			if (UnitOf(o["unit"]) is MapUnit u) orders[u] = new Order((string)o["kind"], gd.map.tileAt((int)o["x"], (int)o["y"]));
		// Saves keep isAutomated but not the unit's AI; give automated units theirs back without moving them.
		var exploring = s["exploring"]!.AsArray().Select(UnitOf).ToHashSet();
		foreach (MapUnit u in human.units.Where(u => u.isAutomated)) {
			if (exploring.Contains(u) && ExplorerAI.MaybeMakeAiData(u, human) is { } explore) u.currentAI = new ExplorerAI(explore);
			else if (u.unitType.isWorker && WorkerAI.MakeAiData(u, human) is { } work) u.currentAI = new WorkerAI(work);
			else u.isAutomated = false;
		}

		foreach (var (id, source) in s["producing_source"]!.AsObject())
			if (CityOf(id) is City c) producingSource[c] = (string)source;
		pendingProduction.UnionWith(Cities("pending_production"));
		var producibles = Producibles(gd).ToList();
		if (s["queues"] is JsonObject qs)   // saves from before production queues have none
			foreach (var (id, names) in qs)
				if (CityOf(id) is City c)
					queues[c] = [.. names!.AsArray().Select(n => producibles.FirstOrDefault(p => p.name == (string)n)).Where(p => p != null)];
		researchSource = (string)s["research_source"];
		researchPending = (bool)s["research_pending"];
		agentResearch = s["agent_research"]!.AsArray().Select(n => gd.techs.FirstOrDefault(t => t.Name == (string)n)).Where(t => t != null).ToHashSet();
		foreach (var (key, n) in s["decided"]!.AsObject()) decided[key] = (int)n;
		lastEvents = s["last_events"]!.AsArray().Select(e => e!.AsObject().DeepClone().AsObject()).ToList();
		turnEvents = s["turn_events"]!.AsArray().Select(e => e!.AsObject().DeepClone().AsObject()).ToList();
		foreach (var (id, n) in s["shields_lost"]!.AsObject())
			if (CityOf(id) is City c) shieldsLost[c] = (int)n;
		riskSeen = [.. Cities("riot_risk")];
		cappedSeen = [.. Cities("capped")];
		defenselessSeen = [.. Cities("defenseless")];
		var seen = s["threats_seen"]!.AsArray().Select(n => (string)n).ToHashSet();
		threatsSeen = [.. gd.mapUnits.Where(u => seen.Contains(u.id.ToString()))];
		botExplorer = s["bot_explorer"] is JsonNode explorer ? UnitOf(explorer) : null;
		revolutionTarget = s["revolution_target"] is JsonNode target ? gd.governments.FirstOrDefault(g => g.name == (string)target) : null;
		ratesBeforeAnarchy = s["rates_before_anarchy"] is JsonArray r ? ((int)r[0], (int)r[1]) : null;
		foreach (JsonNode o in s["peace_offers"]!.AsArray())
			if (gd.players.FirstOrDefault(x => x.civilization.name == (string)o["civ"]) is Player to)
				seat.PeaceOffers[to] = ((int)o["gold"], (int)o["turn"]);
	}
}

/// <summary>
/// The engine's System.Random (seeded, so the .NET 5-compatible generator) read and written through its
/// private fields, so a restored game draws the same numbers the original would have.
/// </summary>
static class RngState {
	const BindingFlags Private = BindingFlags.NonPublic | BindingFlags.Instance;

	static (object impl, FieldInfo prngField, object prng) Parts(Random r) {
		object impl = typeof(Random).GetField("_impl", Private)?.GetValue(r);
		FieldInfo prngField = impl?.GetType().GetField("_prng", Private);
		return (impl, prngField, prngField?.GetValue(impl));
	}

	public static JsonObject Get(Random r) {
		var (_, _, prng) = Parts(r);
		Type t = prng?.GetType();
		if (t?.GetField("_seedArray", Private)?.GetValue(prng) is not int[] seeds) return null;
		return new JsonObject {
			["seed_array"] = new JsonArray(seeds.Select(x => (JsonNode)x).ToArray()),
			["inext"] = (int)t.GetField("_inext", Private)!.GetValue(prng)!,
			["inextp"] = (int)t.GetField("_inextp", Private)!.GetValue(prng)!,
		};
	}

	public static bool Set(Random r, JsonObject state) {
		var (impl, prngField, prng) = Parts(r);
		if (prng == null) return false;
		Type t = prng.GetType();
		t.GetField("_seedArray", Private)!.SetValue(prng, state["seed_array"]!.AsArray().Select(n => (int)n).ToArray());
		t.GetField("_inext", Private)!.SetValue(prng, (int)state["inext"]);
		t.GetField("_inextp", Private)!.SetValue(prng, (int)state["inextp"]);
		prngField.SetValue(impl, prng);
		return true;
	}
}
