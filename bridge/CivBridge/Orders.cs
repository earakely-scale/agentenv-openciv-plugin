using System.Reflection;
using System.Text.Json.Nodes;
using C7Engine;
using C7Engine.Pathing;
using C7GameData;
using C7GameData.AIData;
using MoonSharp.Interpreter;
using Serilog;

namespace CivBridge;

sealed partial class Session {
	static readonly string[] AllOrders =
		["settle", "found_city", "goto", "explore", "auto_work", "fortify", "wake", "hold", "disband", "build_road", "build_mine", "irrigate", "clear_forest",
		 "attack", "bombard", "upgrade"];

	static readonly Dictionary<string, string> Jobs = new() {
		["build_road"] = C7Action.UnitBuildRoad,
		["build_mine"] = C7Action.UnitBuildMine,
		["irrigate"] = C7Action.UnitIrrigate,
		["clear_forest"] = C7Action.UnitClearForest,
	};

	async Task<JsonNode> UnitOrder(Args a) {
		EnsurePlaying();
		MapUnit u = UnitArg(a.Str("unit"));
		string order = a.Str("order").Trim().ToLowerInvariant();
		if (!AllOrders.Contains(order)) throw Invalid(u, $"'{order}' is not an order.");

		City city = null;
		JsonObject path = null, battle = null;
		string message;
		switch (order) {
			case "settle":
				(message, city, path) = await Settle(u, TargetArg(a));
				break;
			case "found_city":
				city = await FoundNow(u, a.Has("name") ? a.Str("name") : null);
				message = Founded(u, city);
				break;
			case "goto":
				(message, path) = await Goto(u, TargetArg(a));
				break;
			case "explore":
				message = Explore(u);
				break;
			case "attack":
				(message, battle) = await Attack(u, TargetArg(a));
				break;
			case "bombard":
				(message, battle) = await BombardOrder(u, TargetArg(a));
				break;
			case "auto_work":
				message = AutoWork(u);
				break;
			case "upgrade":
				message = Upgrade(u);
				break;
			case "fortify":
				if (!u.unitType.actions.Contains(UnitAction.Fortify)) throw Invalid(u, $"{Label(u)} cannot fortify.");
				Stop(u);
				u.Fortify();
				message = $"{Label(u)} is fortified at {At(u.location)} until woken.";
				break;
			case "wake":
				string was = Status(u);
				Stop(u);
				message = was is "idle" or "done" ? $"{Label(u)} had no standing order." : $"{Label(u)} stopped ({was}) and awaits orders.";
				break;
			case "hold":
				message = u.movementPoints.canMove ? $"{Label(u)} holds this turn." : $"{Label(u)} had no moves left anyway.";
				u.SkipTurn();
				break;
			case "disband":
				if (!u.unitType.actions.Contains(UnitAction.Disband)) throw Invalid(u, $"{Label(u)} cannot be disbanded.");
				string label = Label(u);
				Stop(u);
				// In one of our cities the ruleset's script adds shields to what it builds (patches/0014).
				City here = u.location.cityAtTile is { } c && c.owner == human ? c : null;
				int stored = here?.shieldsStored ?? 0;
				try {
					await u.Disband();
				} catch (InterpreterException e) {
					// A script error leaves the unit in play: remove it as the engine would have.
					Log.Error(e, "disband script failed for {Unit}", label);
					u.RemoveFromPlay();
				}
				message = $"{label} was disbanded." + (here != null && here.shieldsStored > stored
					? $" {here.name} gained {here.shieldsStored - stored} shields toward {here.itemBeingProduced?.name ?? "its production"}." : "")
					+ (human.defeated ? " You have no cities or settlers left: your civilization is defeated and the game is over." : "");
				break;
			default:
				message = StartJob(u, order);
				break;
		}
		message += VictoryNow();
		DrainUi();
		ids.Sync(gd, human);
		return new JsonObject {
			["message"] = message,
			["unit"] = Alive(u) ? UnitJson(u) : null,
			["city"] = city != null ? CityJson(city) : null,
			["path"] = path,
			["battle"] = battle,
		};
	}

	List<string> ValidOrders(MapUnit u) {
		var list = new List<string>();
		bool moves = u.movementPoints.canMove;
		HashSet<UnitAction> actions = u.unitType.actions;
		if (u.unitType.isSettler) {
			list.Add("settle");
			if (moves && FoundSite(u.location) == null) list.Add("found_city");
		}
		if (actions.Contains(UnitAction.Goto)) list.Add("goto");
		if (AttackTargets(u).Count > 0) list.Add("attack");
		if (BombardTargets(u).Count > 0) list.Add("bombard");
		if (u.canExplore()) list.Add("explore");
		if (u.canAutomate()) list.Add("auto_work");
		if (actions.Contains(UnitAction.Fortify) && !u.isFortified) list.Add("fortify");
		if (Status(u) is not ("idle" or "done")) list.Add("wake");
		if (moves) list.Add("hold");
		if (actions.Contains(UnitAction.Disband)) list.Add("disband");
		if (u.CanUpgrade()) list.Add("upgrade");
		if (moves && u.unitType.isWorker) list.AddRange(Jobs.Keys.Where(j => Job(u, j) != null));
		return list;
	}

	/// <summary>Cancels every kind of standing order.</summary>
	void Stop(MapUnit u) {
		orders.Remove(u);
		u.Wake();
		u.isAutomated = false;
		u.path = null;
		if (u.WorkerJob != null) u.resetWorkerJob();
	}

	/// <summary>Why a city cannot be founded on the tile under the engine's rules, or null if it can.</summary>
	static string FoundSite(Tile t) {
		if (!t.IsLand()) return "water";
		if (t.HasCity(out City here)) return $"{here.name} is already here";
		if (t.hasBarbarianCamp) return "a barbarian camp is here";
		if (!t.overlayTerrainType.allowCities) return $"cities cannot be built on {t.overlayTerrainType.DisplayName}";
		City near = t.neighbors.Values.FirstOrDefault(n => n.HasCity())?.cityAtTile;
		return near == null ? null : $"adjacent to {near.name} {At(near.location)}; cities need one empty tile between them";
	}

	async Task<(string, City, JsonObject)> Settle(MapUnit u, Tile target) {
		if (!u.unitType.isSettler) throw Invalid(u, $"{Label(u)} cannot found cities.");
		if (FoundSite(target) is string why) throw CannotFound(u, target, why);
		if (Occupant(target) is string who) throw Occupied(u, target, who);
		if (u.location == target) {
			if (u.movementPoints.canMove) {
				City here = await Found(u);
				return (Founded(u, here), here, null);
			}
			orders[u] = new Order("settle", target);
			return ($"{Label(u)} has no moves left; it will found a city here at the start of next turn.", null, null);
		}
		TilePath p = Path(u, target) ?? throw NoPath(u, target);
		var info = PathInfo(u, p);
		orders[u] = new Order("settle", target);
		string stuck = await Walk(u, target);
		if (u.location == target && u.movementPoints.canMove) {
			City c = await Found(u);
			return (Founded(u, c), c, info);
		}
		string eta = u.location == target ? "at the start of next turn" : $"in about {info["turns"]} turn(s)";
		return ($"{Label(u)} is heading to {At(target)}, now at {At(u.location)}; it will found a city there {eta}."
			+ (stuck != null ? $" It stopped early: {stuck}." : ""), null, info);
	}

	async Task<City> FoundNow(MapUnit u, string name) {
		if (!u.unitType.isSettler) throw Invalid(u, $"{Label(u)} cannot found cities.");
		if (FoundSite(u.location) is string why) throw CannotFound(u, u.location, why);
		if (!u.movementPoints.canMove)
			throw new BridgeError("no_moves", $"{Label(u)} has no moves left this turn, so it cannot found a city until next turn.",
				suggest: SettleCall(u, u.location));
		return await Found(u, name);
	}

	async Task<City> Found(MapUnit u, string name = null) {
		orders.Remove(u);
		City c = await u.BuildCity(string.IsNullOrWhiteSpace(name) ? human.GetNextCityName() : name.Trim());
		DrainUi();
		ids.Sync(gd, human);
		EnginePickedProduction(c);
		return c;
	}

	string Founded(MapUnit u, City c) =>
		$"{Label(u)} founded {c.name} ({ids.Of(c)}) at {At(c.location)}; the engine picked {c.itemBeingProduced?.name ?? "nothing"} for it to build"
		+ $" (change it with set_production(city=\"{ids.Of(c)}\", item=...))."
		+ (human.currentlyResearchedTech == null ? " Nothing is being researched yet: pick a tech with research(tech=...)." : "");

	async Task<(string, JsonObject)> Goto(MapUnit u, Tile target) {
		if (!u.unitType.actions.Contains(UnitAction.Goto)) throw Invalid(u, $"{Label(u)} cannot move on its own.");
		if (target == u.location) throw new BridgeError("bad_target", $"{Label(u)} is already at {At(target)}.");
		if (u.IsLandUnit() && !target.IsLand()) throw new BridgeError("bad_target", $"{At(target)} is {target.baseTerrainType.DisplayName}; {Label(u)} moves on land.");
		if (Occupant(target) is string who) throw Occupied(u, target, who);
		TilePath p = Path(u, target) ?? throw NoPath(u, target);
		var info = PathInfo(u, p);
		orders[u] = new Order("goto", target);
		string stuck = await Walk(u, target);
		if (u.location == target) {
			orders.Remove(u);
			return ($"{Label(u)} reached {At(target)}.", info);
		}
		return ($"{Label(u)} is heading to {At(target)} (about {info["turns"]} turn(s)), now at {At(u.location)}."
			+ (stuck != null ? $" It stopped early: {stuck}; it will retry next turn." : ""), info);
	}

	/// <summary>Upgrades the unit in place, in one of the civ's cities, for gold (patches/0019): same id, experience and hit points.</summary>
	string Upgrade(MapUnit u) {
		if (WhyNoUpgrade(u) is string why) throw Invalid(u, $"{Label(u)} cannot upgrade: {why}.");
		string label = Label(u);
		// A standing order ends with the upgrade, as it uses up the unit's moves; a fortified unit stays fortified.
		orders.Remove(u);
		u.isAutomated = false;
		int cost = u.Upgrade();
		return $"{label} is now {WithArticle(u.unitType.name)} ({cost} gold; {human.gold} left). It has no moves left this turn.";
	}

	/// <summary>Why the unit cannot upgrade now, or null: MapUnit.CanUpgrade's checks, in order, with what is missing.</summary>
	string WhyNoUpgrade(MapUnit u) {
		var chain = u.unitType.GetUpgradeChain(u.owner.civilization);
		if (chain.Count == 0) return $"nothing replaces the {u.unitType.name}";
		City c = u.UpgradeCity();
		if (c == null) return $"units upgrade only in one of your cities, and {At(u.location)} is not one";
		UnitPrototype target = u.UpgradeTarget();
		if (target == null) return $"{c.name} cannot build {chain[0].name}, the next in its line: {WhyNot(c, chain[0]) ?? "not yet"}";
		if (!u.movementPoints.canMove) return "it has no moves left this turn";
		int cost = u.UpgradeCost(target);
		return human.gold < cost ? $"the upgrade to {target.name} costs {cost} gold and you have {human.gold}" : null;
	}

	// Both check feasibility before Stop(u), so a refused order leaves the unit's orders as they were.
	string Explore(MapUnit u) {
		if (!u.canExplore()) throw Invalid(u, $"{Label(u)} cannot explore.");
		if (ExplorerAI.MaybeMakeAiData(u, human) == null) throw Invalid(u, $"There is nothing left that {Label(u)} can reach and explore.");
		Stop(u);
		u.Explore();
		DrainUi();
		return $"{Label(u)} is exploring automatically; it is now at {At(u.location)}.";
	}

	string AutoWork(MapUnit u) {
		if (!u.canAutomate()) throw Invalid(u, $"{Label(u)} cannot work automatically.");
		if (WorkerAI.MakeAiData(u, human) == null)
			throw Invalid(u, human.cities.Count == 0 ? "There is no work to automate before you have a city." : $"There is no tile in your territory that {Label(u)} can improve right now.");
		Stop(u);
		u.Automate();
		DrainUi();
		return $"{Label(u)} works automatically from now on.";
	}

	string StartJob(MapUnit u, string job) {
		if (!u.unitType.isWorker) throw Invalid(u, $"Only workers can {job}; {Label(u)} cannot.");
		Terraform tf = Job(u, job);
		if (tf == null) {
			Terraform any = gd.Terraforms.FirstOrDefault(t => t.UIAction == Jobs[job]);
			string why = u.location.HasCity() ? "workers cannot improve a city tile"
				: any?.Improvement != null && u.location.overlays.HasImprovement(any.Improvement) ? $"it already has {any.Name.ToLowerInvariant()}"
				: any != null && !human.HasTech(any.RequiredTech) ? $"it needs {gd.GetTech(any.RequiredTech)?.Name}"
				: $"{Terrain(u.location)} does not allow it";
			throw Invalid(u, $"{Label(u)} cannot {job} at {At(u.location)}: {why}.");
		}
		if (!u.movementPoints.canMove)
			throw new BridgeError("no_moves", $"{Label(u)} has no moves left this turn; start the job next turn.", suggest: "end_turn()");
		int turns = u.TurnsToCompleteTerraform(tf);
		Stop(u);
		u.PerformTerraformAction(tf);
		DrainUi();
		return u.WorkerJob == tf
			? $"{Label(u)} started {tf.Name} at {At(u.location)}; done in about {turns} turn(s)."
			: $"{Label(u)} completed {tf.Name} at {At(u.location)}.";
	}

	/// <summary>The terraform behind a worker order here, or null; roads upgrade to railroads where possible.</summary>
	Terraform Job(MapUnit u, string job) {
		Terraform tf = gd.Terraforms.FirstOrDefault(t => t.UIAction == Jobs[job]);
		if (job == "build_road" && u.location.HasRoad())
			tf = gd.Terraforms.FirstOrDefault(t => t.UIAction == C7Action.UnitBuildRailroad);
		return tf != null && u.CanPerformTerraformAction(tf) ? tf : null;
	}

	static string JobName(Terraform tf) => tf.UIAction.StartsWith("unit_") ? tf.UIAction[5..] : tf.Name.ToLowerInvariant().Replace(' ', '_');

	TilePath Path(MapUnit u, Tile target) {
		TilePath p = PathingAlgorithmChooser.GetAlgorithm(u).PathFrom(u.location, target, u);
		return p.PathLength() > 0 ? p : null;
	}

	JsonObject PathInfo(MapUnit u, TilePath p) => new() {
		["length"] = p.PathLength(),
		["turns"] = Math.Max(1, p.PathCost(human, u.location, u.unitType.movement, u.movementPoints.remaining)),
	};

	/// <summary>
	/// Moves the unit toward the target with the moves it has left, re-planning every step because each
	/// step can reveal terrain (unexplored tiles count as passable). Returns why it stopped short, or null.
	/// </summary>
	async Task<string> Walk(MapUnit u, Tile target) {
		for (int step = 0; step < 64 && u.movementPoints.canMove && u.location != target; step++) {
			if (Occupant(target) is string who) return $"{At(target)} is occupied by {who}";
			TilePath p = Path(u, target);
			if (p == null) return $"there is no route from {At(u.location)} to {At(target)}";
			Tile next = p.PeekNext(), from = u.location;
			if (!u.CanEnterPeacefully(next))
				return Occupant(next) is string blocker ? $"{At(next)} is occupied by {blocker}" : $"{At(next)} cannot be entered peacefully (foreign territory)";
			await u.Move(from.DirectionTo(next), true);
			DrainUi();
			if (!Alive(u)) return "the unit was lost";
			if (u.location == from) return $"it could not enter {At(next)}";
		}
		return null;
	}

	/// <summary>Carries out standing orders at the start of the human's turn (the Godot client does this in UnitSelector).</summary>
	async Task RunStandingOrders(List<JsonObject> events) {
		foreach (MapUnit u in HumanUnits().ToList()) {
			if (!Alive(u)) continue;
			if (orders.TryGetValue(u, out Order o)) {
				await Continue(u, o, events);
				continue;
			}
			if (!u.movementPoints.canMove || u.isFortified || !u.IsBusy()) continue;
			bool exploring = u.isAutomated && u.currentAI is ExplorerAI, working = u.isAutomated && !exploring;
			await u.PerformBusyAction();
			DrainUi();
			if (!Alive(u) || u.isAutomated) continue;
			if (exploring) events.Add(Event("explore_done", $"{Label(u)} has nothing left to explore and is idle at {At(u.location)}.", u.location));
			else if (working) events.Add(Event("job_done", $"{Label(u)} found no more automatic work and is idle at {At(u.location)}.", u.location));
		}
	}

	async Task Continue(MapUnit u, Order o, List<JsonObject> events) {
		bool settle = o.Kind == "settle";
		string why = settle ? FoundSite(o.Target) : null;
		Tile start = u.location;
		if (why == null && u.location != o.Target) why = await Walk(u, o.Target);
		if (!Alive(u)) {
			orders.Remove(u);
			return;
		}
		if (u.location == o.Target) {
			if (!settle) {
				orders.Remove(u);
			} else if (FoundSite(o.Target) is string bad) {
				orders.Remove(u);
				events.Add(Event("settle_failed", $"{Label(u)} cannot found a city at {At(o.Target)}: {bad}. It is idle.", o.Target));
			} else if (u.movementPoints.canMove) {
				City c = await Found(u);
				events.Add(Event("city_founded", $"{c.name} ({ids.Of(c)}) was founded at {At(c.location)}.", c.location));
			}
			return;
		}
		// Keep the order while the unit makes progress; give up when it is stuck in place.
		if (why == null || (u.location != start && (!settle || FoundSite(o.Target) == null))) return;
		orders.Remove(u);
		events.Add(settle
			? Event("settle_failed", $"{Label(u)} gave up settling {At(o.Target)}: {why}. It is idle at {At(u.location)}.", o.Target)
			: Event("goto_blocked", $"{Label(u)} could not continue to {At(o.Target)}: {why}. It is idle at {At(u.location)}.", o.Target));
	}

	JsonObject CitySites(Args a) {
		MapUnit u = a.Has("unit") ? UnitArg(a.Str("unit")) : HumanUnits().FirstOrDefault(x => x.unitType.isSettler);
		City capital = HumanCities().FirstOrDefault(c => c.IsCapital()) ?? HumanCities().FirstOrDefault();
		Tile origin = u?.location ?? capital?.location
			?? throw new BridgeError("unknown_unit", "You have no settler and no city to measure city sites from.");
		int top = Math.Clamp(a.Int("top", 5), 1, 50);
		return new JsonObject {
			["origin"] = new JsonObject { ["x"] = origin.XCoordinate, ["y"] = origin.YCoordinate },
			["sites"] = Json.Array(RankSites(origin, u, top), s => SiteJson(origin, u, s.tile, s.score)),
			["nearby"] = Json.Array(NearbySites(origin), s => SiteJson(origin, u, s.tile, s.score)),
			["note"] = "sites ranks known sites on the unit's continent at least 2 tiles from any city, best first; "
				+ "nearby lists every legal site within 4 tiles (cities need one empty tile between them)",
		};
	}

	static readonly MethodInfo ScoreTiles = typeof(SettlerLocationAI).GetMethod("AssignTileScores", BindingFlags.NonPublic | BindingFlags.Static)
		?? throw new MissingMethodException(nameof(SettlerLocationAI), "AssignTileScores");

	/// <summary>Every explored tile within 4 tiles where a city may be founded, scored like the ranked sites.</summary>
	List<(Tile tile, float score)> NearbySites(Tile origin) {
		var legal = human.tileKnowledge.AllKnownTiles().Where(t => t.DistanceTo(origin) <= 4 && FoundSite(t) == null).ToList();
		var scores = (Dictionary<Tile, float>)ScoreTiles.Invoke(null, [origin, human, legal, new List<MapUnit>()]);
		return legal.Select(t => (t, scores.GetValueOrDefault(t)))
			.OrderByDescending(s => s.Item2).ThenBy(s => s.t.YCoordinate).ThenBy(s => s.t.XCoordinate).ToList();
	}

	/// <summary>The engine AI's settler scoring, minus sites another settler of ours is already heading to.</summary>
	List<(Tile tile, float score)> RankSites(Tile origin, MapUnit unit, int top) {
		var taken = orders.Where(kv => kv.Value.Kind == "settle" && kv.Key != unit).Select(kv => kv.Value.Target).ToList();
		return SettlerLocationAI.GetScoredSettlerCandidates(origin, human)
			.Where(kv => !taken.Any(t => t.DistanceTo(kv.Key) <= 2) && FoundSite(kv.Key) == null)
			.OrderByDescending(kv => kv.Value).ThenBy(kv => kv.Key.YCoordinate).ThenBy(kv => kv.Key.XCoordinate)
			.Take(top).Select(kv => (kv.Key, kv.Value)).ToList();
	}

	JsonObject SiteJson(Tile origin, MapUnit u, Tile t, float score) {
		var area = t.neighbors.Values.Where(Tile.IsTileValid).Append(t).ToList();
		return new JsonObject {
			["x"] = t.XCoordinate,
			["y"] = t.YCoordinate,
			["score"] = Math.Round(score, 1),
			["dist"] = origin.DistanceTo(t),
			["dir"] = Direction(origin, t),
			["turns"] = u == null ? null : TravelTurns(u, t),
			["terrain"] = Terrain(t),
			["river"] = t.BordersRiver(),
			["coastal"] = t.NeighborsWater(),
			["yield"] = Yield(area.Sum(x => x.FoodYield(human).yield), area.Sum(x => x.ProductionYield(human).yield), area.Sum(x => x.CommerceYield(human).yield)),
		};
	}

	JsonNode TravelTurns(MapUnit u, Tile t) {
		if (u.location == t) return 0;
		TilePath p = Path(u, t);
		return p == null ? null : Math.Max(1, p.PathCost(human, u.location, u.unitType.movement, u.movementPoints.remaining));
	}

	BridgeError CannotFound(MapUnit u, Tile t, string why) {
		var sites = RankSites(u.location, u, 5);
		string best = sites.Count == 0 ? " No valid site is known on this continent yet; explore more." :
			$" The best known site is {At(sites[0].tile)}, {u.location.DistanceTo(sites[0].tile)} tiles {Direction(u.location, sites[0].tile)}.";
		return new BridgeError("cannot_found", $"Cannot found a city at {At(t)}: {why}.{best}",
			sites.Select(s => (JsonNode)SiteJson(u.location, u, s.tile, s.score)),
			sites.Count == 0 ? null : SettleCall(u, sites[0].tile));
	}

	/// <summary>"a Barbarians Warrior" or "Babylon (Babylonians' city)" when a foreign unit or city holds the tile, else null.</summary>
	string Occupant(Tile t) {
		if (t.HasCity(out City c) && c.owner != human) return $"{c.name}, a {c.owner.civilization.name} city";
		var foes = t.unitsOnTile.Where(x => x.owner != human).ToList();
		return foes.Count == 0 ? null : $"a {Owner(foes[0].owner)} {foes[0].unitType.name}{(foes.Count > 1 ? $" and {foes.Count - 1} more" : "")}";
	}

	BridgeError Occupied(MapUnit u, Tile t, string who) {
		var sites = u.unitType.isSettler ? RankSites(u.location, u, 6).Where(s => s.tile != t && Occupant(s.tile) == null).Take(5).ToList() : [];
		string next = sites.Count == 0 ? " Pick a free tile, or wait for it to move on." :
			$" Wait for it to move on, or settle elsewhere: the best free site is {At(sites[0].tile)}, {u.location.DistanceTo(sites[0].tile)} tiles {Direction(u.location, sites[0].tile)}.";
		return new BridgeError("occupied", $"{At(t)} is occupied by {who}; {Label(u)} only moves peacefully.{next}",
			u.unitType.isSettler ? sites.Select(s => (JsonNode)SiteJson(u.location, u, s.tile, s.score)) : null,
			sites.Count > 0 ? SettleCall(u, sites[0].tile) : null);
	}

	BridgeError NoPath(MapUnit u, Tile t) {
		var sites = u.unitType.isSettler ? RankSites(u.location, u, 5) : [];
		return new BridgeError("no_path", $"{Label(u)} has no route from {At(u.location)} to {At(t)}.",
			u.unitType.isSettler ? sites.Select(s => (JsonNode)SiteJson(u.location, u, s.tile, s.score)) : null,
			sites.Count > 0 ? SettleCall(u, sites[0].tile) : null);
	}

	BridgeError Invalid(MapUnit u, string reason) {
		var valid = ValidOrders(u);
		return new BridgeError("invalid_order", $"{reason} {Label(u)} can: {(valid.Count == 0 ? "nothing this turn" : string.Join(", ", valid))}.",
			BridgeError.Names(valid), SuggestFor(u, valid));
	}

	string SuggestFor(MapUnit u, List<string> valid) {
		string id = ids.Of(u);
		if (valid.Contains("found_city")) return $"unit_order(unit=\"{id}\", order=\"found_city\")";
		if (u.unitType.isSettler && RankSites(u.location, u, 1) is [var best]) return SettleCall(u, best.tile);
		foreach (string o in new[] { "auto_work", "explore", "fortify", "hold" })
			if (valid.Contains(o)) return $"unit_order(unit=\"{id}\", order=\"{o}\")";
		return null;
	}

	string SettleCall(MapUnit u, Tile t) => $"unit_order(unit=\"{ids.Of(u)}\", order=\"settle\", x={t.XCoordinate}, y={t.YCoordinate})";

	MapUnit UnitArg(string id) {
		MapUnit u = ids.Find<MapUnit>(id);
		if (u != null && Alive(u)) return u;
		var mine = HumanUnits().Select(x => ids.Of(x)).ToList();
		throw new BridgeError("unknown_unit", $"There is no unit '{id}' of yours{(u != null ? " any more (it was lost or used)" : "")}. Your units: {(mine.Count == 0 ? "none" : string.Join(", ", mine))}.",
			BridgeError.Names(mine));
	}

	City CityArg(Args a) {
		string id = a.Has("city") ? a.Str("city") : "";
		City c = ids.Find<City>(id) ?? human.cities.FirstOrDefault(x => Same(x.name, id));
		if (c != null && human.cities.Contains(c)) return c;
		var mine = HumanCities().Select(x => ids.Of(x)).ToList();
		throw new BridgeError("unknown_city", $"There is no city '{id}' of yours. Your cities: {(mine.Count == 0 ? "none yet" : string.Join(", ", HumanCities().Select(x => $"{ids.Of(x)} {x.name}")))}.",
			BridgeError.Names(mine));
	}

	Tile TargetArg(Args a) {
		if (!a.Has("x") || !a.Has("y")) throw new BridgeError("bad_target", "This order needs a target tile: pass x and y.");
		return TileArg(a.Int("x"), a.Int("y"), requireExplored: true);
	}

	Tile TileArg(int x, int y, bool requireExplored) {
		if (((x + y) & 1) != 0)
			throw new BridgeError("bad_target", $"({x},{y}) is not a tile: x + y must be even. Neighbours of that point include ({x + 1},{y}) and ({x},{y + 1}).",
				[new JsonObject { ["x"] = x + 1, ["y"] = y }, new JsonObject { ["x"] = x, ["y"] = y + 1 }]);
		Tile t = gd.map.tileAt(x, y);
		if (!Tile.IsTileValid(t))
			throw new BridgeError("bad_target", $"({x},{y}) is off the map (width {gd.map.numTilesWide}, height {gd.map.numTilesTall}).");
		if (requireExplored && !human.tileKnowledge.isTileKnown(t))
			throw new BridgeError("bad_target", $"{At(t)} has not been explored yet; pick an explored tile or explore toward it first.", suggest: $"view_map(x={x}, y={y})");
		return t;
	}
}
