using System.Text.Json.Nodes;
using C7GameData;
using Serilog;

namespace CivBridge;

// Acting at scale (docs/protocol.md, unit_orders and set_production): orders for many units in one command, the same
// production for many cities, and production queues the bridge follows each time a city completes something, so a
// large empire doesn't wait on a choice per city per completion.
sealed partial class Session {
	const int MaxBatch = 100, MaxQueue = 10;

	/// <summary>
	/// The units a selector names: a unit id, "idle" (every unit waiting for orders), "idle:Type" (those of a type) or
	/// "all:Type" (every unit of a type).
	/// </summary>
	List<MapUnit> UnitsNamed(string selector) {
		string sel = selector.Trim();
		int colon = sel.IndexOf(':');
		string scope = (colon < 0 ? sel : sel[..colon]).Trim().ToLowerInvariant(), type = colon < 0 ? null : sel[(colon + 1)..].Trim();
		if (scope is not ("idle" or "all")) return [UnitArg(sel)];
		if (scope == "all" && type == null)
			throw new BridgeError("bad_args", "'all' needs a unit type, e.g. \"all:Warrior\"; \"idle\" alone names every unit waiting for orders.");
		var units = HumanUnits().Where(u => scope == "all" || NeedsOrders(u));
		if (type != null) {
			var types = HumanUnits().Select(u => u.unitType.name).Distinct().ToList();
			if (!types.Any(t => Same(t, type)))
				throw new BridgeError("unknown_unit", $"You have no {type}. Your unit types: {string.Join(", ", types)}.",
					BridgeError.Names(types.Select(t => $"{scope}:{t}")));
			units = units.Where(u => Same(u.unitType.name, type));
		}
		return [.. units];
	}

	/// <summary>Orders for many units, each as unit_order takes it; one that fails doesn't stop the rest.</summary>
	async Task<JsonNode> UnitOrders(Args a) {
		EnsurePlaying();
		List<Args> list = a.Objects("orders");
		if (list.Count == 0 || list.Count > MaxBatch)
			throw new BridgeError("bad_args", $"'orders' takes 1 to {MaxBatch} orders, each {{\"unit\", \"order\", \"x\", \"y\"}}.");
		var results = new JsonArray();
		int ok = 0, failed = 0;
		void Fail(string unit, string code, string message) {
			results.Add(new JsonObject { ["unit"] = unit, ["ok"] = false, ["code"] = code, ["message"] = message });
			failed++;
		}
		foreach (Args o in list) {
			string selector = o.Str("unit", "");
			List<MapUnit> units;
			try {
				units = UnitsNamed(selector);
			} catch (BridgeError e) {
				Fail(selector, e.Code, e.Message);
				continue;
			}
			if (units.Count == 0) Fail(selector, "no_units", $"No unit matches '{selector}' now.");
			foreach (MapUnit u in units) {
				string id = ids.Of(u);
				if (!Alive(u)) continue;   // lost earlier in this batch
				var args = new JsonObject { ["unit"] = id, ["order"] = o.Str("order", "") };
				if (o.Has("x")) args["x"] = o.Int("x");
				if (o.Has("y")) args["y"] = o.Int("y");
				try {
					JsonNode r = await UnitOrder(new Args(args));
					results.Add(new JsonObject { ["unit"] = id, ["ok"] = true, ["message"] = (string)r["message"] });
					ok++;
				} catch (BridgeError e) {
					Fail(id, e.Code, e.Message);
				} catch (Exception e) when (e is not OperationCanceledException) {
					Log.Error(e, "unit_orders: {Unit} failed", id);
					Fail(id, "engine_error", $"The engine failed: {e.GetType().Name}: {e.Message}");
				}
			}
		}
		DrainUi();
		ids.Sync(gd, human);
		return new JsonObject {
			["message"] = $"{ok} order{(ok == 1 ? "" : "s")} done" + (failed > 0 ? $", {failed} failed" : "") + ".",
			["ok"] = ok, ["failed"] = failed, ["results"] = results,
		};
	}

	/// <summary>The cities a selector names: a city id or name, several separated by commas, "all", or "pending" (cities
	/// whose next item the engine picked and nobody has kept or changed, or that build nothing).</summary>
	List<City> CitiesNamed(string selector, out bool many) {
		string sel = selector.Trim();
		many = true;
		if (Same(sel, "all")) return [.. HumanCities()];
		if (Same(sel, "pending")) return [.. HumanCities().Where(c => c.itemBeingProduced == null || pendingProduction.Contains(c))];
		var parts = sel.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
		many = parts.Length > 1;
		return [.. parts.Select(p => CityArg(new Args(new JsonObject { ["city"] = p }))).Distinct()];
	}

	static IEnumerable<IProducible> Producibles(GameData gd) =>
		gd.unitPrototypes.Cast<IProducible>().Concat(gd.Buildings).Concat(gd.Inflows);

	/// <summary>A production queue from names: anything a city can ever build, checked again when its turn comes.</summary>
	List<IProducible> QueueArg(List<string> names) {
		if (names.Count > MaxQueue) throw new BridgeError("bad_args", $"A queue holds at most {MaxQueue} items.");
		var all = Producibles(gd).ToList();
		return [.. names.Select(n => all.FirstOrDefault(p => Same(p.name, n)) ?? throw new BridgeError("unknown_item",
			$"'{n}' is not something a city can build." + (all.FirstOrDefault(p => Close(p.name, n)) is { } close ? $" The closest is {close.name}." : ""),
			BridgeError.Names(all.Where(p => Close(p.name, n)).Select(p => p.name))))];
	}

	Dictionary<City, List<IProducible>> queues => seat.Queues;

	/// <summary>
	/// The city's next item from its queue, after it completed something (or the engine changed what it builds): the
	/// first queued item it can build now. Items it cannot build any more are dropped with a note.
	/// </summary>
	IProducible NextQueued(City c, List<string> skipped) {
		if (!queues.TryGetValue(c, out var q)) return null;
		var options = c.ListProductionOptions(gd).ToList();
		while (q.Count > 0) {
			IProducible p = q[0];
			q.RemoveAt(0);
			IProducible can = options.FirstOrDefault(o => Same(o.name, p.name));
			if (can != null) {
				if (q.Count == 0) queues.Remove(c);
				return can;
			}
			skipped.Add(p.name);
		}
		queues.Remove(c);
		return null;
	}

	JsonArray QueueJson(City c) => Json.Strings(queues.TryGetValue(c, out var q) ? q.Select(p => p.name) : []);
}
