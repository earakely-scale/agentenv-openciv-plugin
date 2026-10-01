using System.Text.Json.Nodes;
using C7Engine;
using C7GameData;

namespace CivBridge;

// Observations. None of this may draw from GameData.rng or change game state.
sealed partial class Session {
	static readonly string[] EraNames = ["Ancient Times", "Middle Ages", "Industrial Age", "Modern Era"];

	JsonObject State() {
		Player h = human;
		return new JsonObject {
			["turn"] = gd.turn,
			["turn_limit"] = turnLimit,
			["game_over"] = GameOver,
			["defeated"] = h.defeated,
			["civ"] = h.civilization.name,
			["government"] = h.government.name,
			["anarchy_until"] = h.government.transitionType ? (JsonNode)h.inAnarchyUntilTurn : null,
			["gold"] = h.gold,
			["gold_per_turn"] = h.CalculateGoldPerTurn(),
			["rates"] = new JsonObject { ["tax"] = h.taxRate, ["science"] = h.scienceRate, ["luxury"] = h.luxuryRate },
			["research"] = Research(),
			["known_techs"] = Json.Strings(gd.techs.Where(t => h.knownTechs.Contains(t.id)).Select(t => t.Name)),
			["score"] = ScoreOf(h),
			["explored_pct"] = Math.Round(100.0 * h.tileKnowledge.knownTiles.Count / gd.map.tiles.Count, 1),
			["cities"] = Json.Array(HumanCities(), CityJson),
			["units"] = Json.Array(HumanUnits(), UnitJson),
			["rivals"] = Json.Array(Rivals(), p => {
				bool met = h.playerRelationships.ContainsKey(p.id);
				return new JsonObject {
					["civ"] = p.civilization.name,
					["met"] = met,
					["at_war"] = met && PlayerRelationship.AtWar(h, p),
					["cities_seen"] = p.cities.Count(c => h.tileKnowledge.isTileKnown(c.location)),
				};
			}),
			["blockers"] = Blockers(),
			["last_events"] = Json.Array(lastEvents, e => e.DeepClone()),
		};
	}

	JsonObject Research() {
		Tech t = gd.GetTech(human.currentlyResearchedTech);
		if (t == null) return new JsonObject { ["current"] = null, ["turns_left"] = null, ["beakers"] = human.beakers, ["cost"] = null, ["queue"] = new JsonArray() };
		return new JsonObject {
			["current"] = t.Name,
			["turns_left"] = Json.Turns(human.EstimateTurnsToResearch(gd, t)),
			["beakers"] = human.beakers,
			["cost"] = gd.TechCostFor(t, human) + human.beakers,
			["queue"] = Json.Strings(human.ResearchQueue.Select(q => q.Name)),
		};
	}

	JsonArray Blockers() {
		var blockers = new JsonArray();
		if (GameOver) return blockers;
		if (human.cities.Count > 0 && human.currentlyResearchedTech == null && human.GetAvailableTechsToResearch(gd.techs).Count > 0)
			blockers.Add(new JsonObject { ["kind"] = "no_research", ["message"] = "Nothing is being researched." });
		foreach (City c in HumanCities().Where(c => c.itemBeingProduced == null))
			blockers.Add(new JsonObject { ["kind"] = "no_production", ["id"] = ids.Of(c), ["message"] = $"{c.name} is producing nothing." });
		foreach (MapUnit u in HumanUnits().Where(NeedsOrders))
			blockers.Add(new JsonObject { ["kind"] = "idle_unit", ["id"] = ids.Of(u), ["message"] = $"{Label(u)} has moves and no orders" });
		return blockers;
	}

	JsonObject CityJson(City c) {
		IProducible item = c.itemBeingProduced;
		int food = c.FoodGrowthPerTurn();
		return new JsonObject {
			["id"] = ids.Of(c),
			["name"] = c.name,
			["x"] = c.location.XCoordinate,
			["y"] = c.location.YCoordinate,
			["size"] = c.residents.Count,
			["capital"] = c.IsCapital(),
			["food_stored"] = c.foodStored,
			["food_needed"] = c.FoodNeededToGrow(),
			["food_per_turn"] = food,
			["turns_to_grow"] = food > 0 ? Json.Turns(c.TurnsUntilGrowth()) : null,
			["shields_per_turn"] = c.CurrentProductionYield().useful,
			["producing"] = item?.name,
			["production_stored"] = c.shieldsStored,
			["production_cost"] = item == null ? null : (JsonNode)human.ShieldCost(item),
			["turns_to_complete"] = ProductionEta(c),
			["disorder"] = c.isInCivilDisorder,
			["buildings"] = Json.Strings(c.GetBuildings().Select(b => b.building.name)),
		};
	}

	/// <summary>Turns until the current item completes, including waiting for the population it costs.</summary>
	JsonNode ProductionEta(City c) {
		IProducible item = c.itemBeingProduced;
		if (item == null || item is Inflow) return null;
		int turns = c.TurnsUntilProductionFinished();
		if (turns == int.MaxValue) return null;
		int missing = item.populationCost + 1 - c.residents.Count;
		if (missing > 0) {
			int food = c.FoodGrowthPerTurn(), first = c.TurnsUntilGrowth();
			if (food <= 0 || first is int.MaxValue or int.MinValue) return null;
			int grow = first + (missing - 1) * (int)Math.Ceiling((double)c.FoodNeededToGrow() / food);
			turns = Math.Max(turns, grow);
		}
		return turns;
	}

	JsonObject UnitJson(MapUnit u) {
		var o = new JsonObject {
			["id"] = ids.Of(u),
			["type"] = u.unitType.name,
			["x"] = u.location.XCoordinate,
			["y"] = u.location.YCoordinate,
			["moves_left"] = Math.Round(u.movementPoints.remaining, 2),
			["moves_max"] = u.unitType.movement,
			["hp"] = u.hitPointsRemaining,
			["hp_max"] = u.maxHitPoints,
			["status"] = Status(u),
			["target"] = orders.TryGetValue(u, out Order order) ? Relative(u.location, order.Target, withXY: true) : null,
		};
		if (u.unitType.isSettler) {
			string why = FoundSite(u.location) ?? (u.movementPoints.canMove ? null : "no moves left this turn");
			o["can_found_city"] = why == null ? new JsonObject { ["ok"] = true } : new JsonObject { ["ok"] = false, ["reason"] = why };
		}
		o["orders"] = Json.Strings(ValidOrders(u));
		o["needs_orders"] = NeedsOrders(u);
		return o;
	}

	string Status(MapUnit u) =>
		orders.TryGetValue(u, out Order o) ? o.Kind
		: u.isFortified ? "fortified"
		: u.isAutomated && u.currentAI is ExplorerAI ? "exploring"
		: u.isAutomated ? "auto_work"
		: u.WorkerJob != null ? "working:" + JobName(u.WorkerJob)
		: u.path?.PathLength() > 0 ? "goto"
		: u.movementPoints.canMove ? "idle" : "done";

	bool NeedsOrders(MapUnit u) => !GameOver && u.CanBeActive() && !orders.ContainsKey(u);

	JsonObject Map(Args a) {
		Tile center = TileArg(a.Int("x"), a.Int("y"), requireExplored: false);
		int r = Math.Clamp(a.Int("radius", 3), 0, 8);
		var tiles = new JsonArray();
		for (int dy = -2 * r; dy <= 2 * r; dy++)
			for (int dx = -2 * r; dx <= 2 * r; dx++) {
				if (((dx + dy) & 1) != 0 || Math.Abs(dx) + Math.Abs(dy) > 2 * r) continue;
				Tile t = gd.map.tileAt(center.XCoordinate + dx, center.YCoordinate + dy);
				if (Tile.IsTileValid(t) && human.tileKnowledge.isTileKnown(t)) tiles.Add(TileJson(center, t));
			}
		return new JsonObject {
			["center"] = new JsonObject { ["x"] = center.XCoordinate, ["y"] = center.YCoordinate },
			["radius"] = r,
			["tiles"] = tiles,
		};
	}

	JsonObject TileJson(Tile from, Tile t) {
		bool visible = human.tileKnowledge.isActiveTile(t);
		JsonObject o = Relative(from, t, withXY: true);
		o["visible"] = visible;
		o["terrain"] = t.baseTerrainType.DisplayName;
		o["overlay"] = t.overlayTerrainType != t.baseTerrainType ? t.overlayTerrainType.DisplayName : null;
		o["resource"] = KnownResource(t);
		o["river"] = t.BordersRiver();
		o["improvements"] = Json.Strings(t.overlays.GetImprovements().Select(i => i.key == "barbarianCamp" ? "barbarian_camp" : i.key));
		o["owner"] = t.OwningPlayer()?.civilization.name;
		o["city"] = t.HasCity(out City c)
			? new JsonObject { ["name"] = c.name, ["owner"] = c.owner.civilization.name, ["size"] = c.residents.Count, ["id"] = ids.Of(c) }
			: null;
		o["units"] = visible ? TileUnits(t) : new JsonArray();
		o["yield"] = Yield(t.FoodYield(human).yield, t.ProductionYield(human).yield, t.CommerceYield(human).yield);
		string why = FoundSite(t);
		o["city_site"] = why == null ? new JsonObject { ["ok"] = true } : new JsonObject { ["ok"] = false, ["reason"] = why };
		return o;
	}

	JsonArray TileUnits(Tile t) {
		var list = new JsonArray();
		foreach (MapUnit u in t.unitsOnTile.Where(u => u.owner == human).OrderBy(u => Ids.Number(ids.Of(u))))
			list.Add(new JsonObject { ["owner"] = human.civilization.name, ["type"] = u.unitType.name, ["count"] = 1, ["id"] = ids.Of(u) });
		foreach (var g in t.unitsOnTile.Where(u => u.owner != human).GroupBy(u => (Owner(u.owner), u.unitType.name)))
			list.Add(new JsonObject { ["owner"] = g.Key.Item1, ["type"] = g.Key.name, ["count"] = g.Count() });
		return list;
	}

	static string Owner(Player p) => p.isBarbarians ? "Barbarians" : p.civilization.name;

	string KnownResource(Tile t) =>
		t.Resource != null && t.Resource != Resource.NONE && human.KnowsAboutResource(t.Resource) ? t.Resource.Name : null;

	static JsonObject Yield(int food, int shields, int commerce) => new() { ["food"] = food, ["shields"] = shields, ["commerce"] = commerce };

	JsonObject CityInfo(Args a) {
		City c = CityArg(a);
		JsonObject o = CityJson(c);
		o["options"] = Json.Array(c.ListProductionOptions(gd), p => new JsonObject {
			["name"] = p.name,
			["kind"] = p switch { UnitPrototype => "unit", Building => "building", _ => "wealth" },
			["cost"] = p is Inflow ? 0 : human.ShieldCost(p),
			["turns"] = p is Inflow ? null : Json.Turns(c.TurnsToProduce(p)),
		});
		o["tiles_worked"] = Json.Array(c.residents.Where(r => Tile.IsTileValid(r.tileWorked)), r => {
			Tile t = r.tileWorked;
			return new JsonObject {
				["x"] = t.XCoordinate, ["y"] = t.YCoordinate, ["terrain"] = Terrain(t),
				["yield"] = Yield(t.FoodYield(c).yield, t.ProductionYield(c).yield, t.CommerceYield(c).yield),
			};
		});
		return o;
	}

	JsonObject SetProduction(Args a) {
		City c = CityArg(a);
		string wanted = a.Str("item");
		var options = c.ListProductionOptions(gd).ToList();
		IProducible p = options.FirstOrDefault(o => Same(o.name, wanted));
		if (p == null) {
			IProducible known = gd.unitPrototypes.Cast<IProducible>().Concat(gd.Buildings).Concat(gd.Inflows).FirstOrDefault(o => Same(o.name, wanted));
			Tech missing = known?.requiredTech is Tech t && !human.knownTechs.Contains(t.id) ? t : null;
			string why = known == null ? $"'{wanted}' is not something a city can build"
				: missing != null ? $"{known.name} requires {missing.Name}"
				: $"{c.name} cannot build {known.name} now";
			throw new BridgeError("unknown_item", $"{why}. {c.name} can build: {string.Join(", ", options.Select(o => o.name))}.",
				BridgeError.Names(options.Select(o => o.name)),
				missing != null ? $"research(tech=\"{missing.Name}\")" : $"set_production(city=\"{ids.Of(c)}\", item=\"{options.First().name}\")");
		}
		c.SetItemBeingProduced(p);
		string message = p is Inflow
			? $"{c.name} now converts its shields into {p.name}."
			: $"{c.name} now builds {p.name} ({human.ShieldCost(p)} shields, {Eta(ProductionEta(c))}).";
		if (p.populationCost > 0 && c.residents.Count <= p.populationCost)
			message += $" A {p.name} costs {p.populationCost} population, so it completes only once {c.name} reaches size {p.populationCost + 1} (now {c.residents.Count}).";
		return new JsonObject { ["message"] = message, ["city"] = CityJson(c) };
	}

	static string Eta(JsonNode turns) => turns == null ? "never at the current rate" : $"{turns} turn{((int)turns == 1 ? "" : "s")}";

	JsonObject Techs() {
		Tech current = gd.GetTech(human.currentlyResearchedTech);
		return new JsonObject {
			["current"] = current?.Name,
			["turns_left"] = current == null ? null : Json.Turns(human.EstimateTurnsToResearch(gd, current)),
			["known"] = Json.Strings(gd.techs.Where(t => human.knownTechs.Contains(t.id)).Select(t => t.Name)),
			["available"] = Json.Array(human.GetAvailableTechsToResearch(gd.techs), t => new JsonObject {
				["name"] = t.Name,
				["cost"] = gd.TechCostFor(t, human),
				["turns"] = ResearchTurns(t),
				["era"] = EraName(t),
				["unlocks"] = Json.Strings(Unlocks(t)),
			}),
		};
	}

	JsonNode ResearchTurns(Tech t) {
		if (t.id == human.currentlyResearchedTech) return Json.Turns(human.EstimateTurnsToResearch(gd, t));
		int perTurn = human.cities.Sum(c => c.CurrentCommerceYield().beakers);
		if (perTurn <= 0) return null;
		int turns = (int)Math.Ceiling((double)gd.TechCostFor(t, human) / perTurn);
		return Math.Clamp(turns, gd.rules.MinimumResearchTime, gd.rules.MaximumResearchTime);
	}

	IEnumerable<string> Unlocks(Tech t) =>
		gd.unitPrototypes.Where(u => u.requiredTech == t && !u.unproducible && u.producibleBy.Contains(human.civilization)).Select(u => u.name)
			.Concat(gd.Buildings.Where(b => b.requiredTech == t).Select(b => b.name))
			.Concat(gd.governments.Where(g => g.prerequisiteTech == t.id).Select(g => g.name))
			.Concat(gd.Terraforms.Where(f => f.RequiredTech == t.id).Select(f => f.Name));

	static string EraName(Tech t) => EraNames[Math.Clamp(EraUtils.GetEraIndex(t.EraCivilopediaName), 0, EraNames.Length - 1)];

	JsonObject SetResearch(Args a) {
		string wanted = a.Str("tech");
		var available = human.GetAvailableTechsToResearch(gd.techs);
		string first = available.FirstOrDefault()?.Name;
		Tech goal = gd.techs.FirstOrDefault(t => Same(t.Name, wanted));
		if (goal == null)
			throw new BridgeError("unknown_tech", $"There is no tech called '{wanted}'. Researchable now: {string.Join(", ", available.Select(t => t.Name))}.",
				BridgeError.Names(available.Select(t => t.Name)), first == null ? null : $"research(tech=\"{first}\")");
		if (human.knownTechs.Contains(goal.id))
			throw new BridgeError("already_known", $"{goal.Name} is already known. Researchable now: {string.Join(", ", available.Select(t => t.Name))}.",
				BridgeError.Names(available.Select(t => t.Name)), first == null ? null : $"research(tech=\"{first}\")");

		List<Tech> plan = ResearchPlan(goal);
		human.ResearchQueue.Clear();
		foreach (Tech t in plan) human.AddTechItemToResearchQueue(t);
		// Switching away loses the beakers spent so far; staying on the same tech keeps them.
		if (human.currentlyResearchedTech != plan[0].id) human.SetCurrentlyResearchedTech(plan[0].id);

		Tech now = gd.GetTech(human.currentlyResearchedTech);
		string message = plan.Count == 1
			? $"Researching {goal.Name} ({Eta(ResearchTurns(goal))})."
			: $"Researching {now?.Name} first; {goal.Name} needs {string.Join(" -> ", plan.Select(t => t.Name))}.";
		return new JsonObject {
			["message"] = message,
			["current"] = now?.Name,
			["queue"] = Json.Strings(human.ResearchQueue.Select(t => t.Name)),
		};
	}

	/// <summary>The goal and its missing prerequisites, prerequisites first (cheapest first among ready techs).</summary>
	List<Tech> ResearchPlan(Tech goal) {
		var needed = new HashSet<Tech>();
		void Need(Tech t) {
			if (human.knownTechs.Contains(t.id) || !needed.Add(t)) return;
			foreach (Tech p in t.Prerequisites) Need(p);
		}
		Need(goal);
		// A later-era goal also needs the techs that advance the era.
		int era = human.EraIndex();
		foreach (Tech t in gd.techs)
			if (t.RequiredForEraAdvancement && EraUtils.GetEraIndex(t.EraCivilopediaName) < EraUtils.GetEraIndex(goal.EraCivilopediaName)
				&& EraUtils.GetEraIndex(t.EraCivilopediaName) >= era)
				Need(t);

		var done = new HashSet<ID>(human.knownTechs);
		var plan = new List<Tech>();
		while (needed.Count > 0) {
			Tech next = needed.Where(t => t.Prerequisites.All(p => done.Contains(p.id)))
				.OrderBy(t => EraUtils.GetEraIndex(t.EraCivilopediaName)).ThenBy(t => t.Cost).ThenBy(t => t.Name, StringComparer.Ordinal).First();
			plan.Add(next);
			done.Add(next.id);
			needed.Remove(next);
		}
		return plan;
	}

	IEnumerable<MapUnit> HumanUnits() => human.units.Where(u => ids.Of(u) != null).OrderBy(u => Ids.Number(ids.Of(u)));

	IEnumerable<City> HumanCities() => human.cities.Where(c => ids.Of(c) != null).OrderBy(c => Ids.Number(ids.Of(c)));

	bool Alive(MapUnit u) => u.hitPointsRemaining > 0 && human.units.Contains(u);

	string Label(MapUnit u) => $"{ids.Of(u)} {u.unitType.name}";

	static string At(Tile t) => $"({t.XCoordinate},{t.YCoordinate})";

	static string Terrain(Tile t) =>
		t.overlayTerrainType != t.baseTerrainType ? $"{t.overlayTerrainType.DisplayName} on {t.baseTerrainType.DisplayName}" : t.baseTerrainType.DisplayName;

	static bool Same(string a, string b) => string.Equals(a?.Trim(), b?.Trim(), StringComparison.OrdinalIgnoreCase);

	/// <summary>{"dist", "dir"} from one tile to another, plus the target's x, y when asked.</summary>
	static JsonObject Relative(Tile from, Tile to, bool withXY = false) {
		var o = new JsonObject();
		if (withXY) {
			o["x"] = to.XCoordinate;
			o["y"] = to.YCoordinate;
		}
		o["dist"] = from.DistanceTo(to);
		o["dir"] = Direction(from, to);
		return o;
	}

	static string Direction(Tile from, Tile to) => from == to ? "here" : from.DirectionTo(to) switch {
		TileDirection.NORTH => "N", TileDirection.NORTHEAST => "NE", TileDirection.EAST => "E", TileDirection.SOUTHEAST => "SE",
		TileDirection.SOUTH => "S", TileDirection.SOUTHWEST => "SW", TileDirection.WEST => "W", _ => "NW",
	};

	JsonObject Event(string kind, string text, Tile at = null) {
		var e = new JsonObject { ["turn"] = eventTurn, ["kind"] = kind, ["text"] = text };
		if (Tile.IsTileValid(at)) {
			e["x"] = at.XCoordinate;
			e["y"] = at.YCoordinate;
		}
		return e;
	}

	static JsonObject Auto(string kind, string text) => new() { ["kind"] = kind, ["text"] = text };
}
