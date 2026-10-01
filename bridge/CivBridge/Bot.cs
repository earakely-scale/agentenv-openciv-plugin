using C7GameData;

namespace CivBridge;

// The settler_bot autoplay policy: a scripted reference that follows the env's own suggestions. It
// issues the same orders an agent can (settle, auto_work, explore, goto, fortify, set_production,
// set_research) and ends turns the same way, so standing orders and rules apply to it unchanged.
sealed partial class Session {
	const int BotMaxCities = 8;
	MapUnit botExplorer;

	static bool Military(MapUnit u) => u.CanDefendOnLand() && !u.unitType.isSettler && !u.unitType.isWorker;

	async Task SettlerBotTurn() {
		foreach (MapUnit u in HumanUnits().ToList()) {
			if (!Alive(u) || orders.ContainsKey(u) || !u.movementPoints.canMove) continue;
			if (u.unitType.isSettler) await BotSettle(u);
			else if (u.unitType.isWorker) {
				if (!u.isAutomated && u.WorkerJob == null) Try(() => AutoWork(u));
			} else if (Military(u) && !u.isAutomated && !u.isFortified) {
				if (botExplorer == null) {
					botExplorer = u;
					if (Try(() => Explore(u))) continue;
				}
				await BotGarrison(u);
			}
		}

		int workers = human.units.Count(u => u.unitType.isWorker), cities = human.cities.Count;
		foreach (City c in HumanCities()) {
			string want = !c.location.unitsOnTile.Any(Military) ? "Warrior"
				: c.residents.Count >= 2 && cities < BotMaxCities ? "Settler"
				: workers < cities ? "Worker"
				: "Warrior";
			IProducible item = c.ListProductionOptions(gd).FirstOrDefault(o => o.name == want);
			if (item == null) continue;
			if (c.itemBeingProduced != item) c.SetItemBeingProduced(item);
			AgentPickedProduction(c);
		}

		if (human.cities.Count > 0 && (human.currentlyResearchedTech == null || researchSource == Source.Engine)) {
			Tech cheapest = human.GetAvailableTechsToResearch(gd.techs)
				.OrderBy(t => gd.TechCostFor(t, human)).ThenBy(t => t.Name, StringComparer.Ordinal).FirstOrDefault();
			if (cheapest != null) {
				human.ResearchQueue.Clear();
				human.AddTechItemToResearchQueue(cheapest);
				if (human.currentlyResearchedTech != cheapest.id) human.SetCurrentlyResearchedTech(cheapest.id);
				AgentPickedResearch([cheapest]);
			}
		}
		DrainUi();
	}

	/// <summary>The capital where the first settler stands on turn 0, then the top-ranked city site from each settler.</summary>
	async Task BotSettle(MapUnit u) {
		if (gd.turn == 0 && human.cities.Count == 0 && FoundSite(u.location) == null) {
			await Found(u);
			return;
		}
		if (RankSites(u.location, u, 1) is [var best]) {
			try {
				await Settle(u, best.tile);
				return;
			} catch (BridgeError) { /* no route or occupied: hold this turn */ }
		}
		u.SkipTurn();
	}

	/// <summary>Fortify in the nearest city without a defender (counting units already on their way), else the nearest city.</summary>
	async Task BotGarrison(MapUnit u) {
		var guarded = HumanCities().Where(c =>
			c.location.unitsOnTile.Any(x => x != u && Military(x))
			|| orders.Any(kv => kv.Key != u && kv.Value.Kind == "goto" && kv.Value.Target == c.location)).ToHashSet();
		City target = HumanCities().OrderBy(c => guarded.Contains(c)).ThenBy(c => c.location.DistanceTo(u.location)).FirstOrDefault();
		if (target == null || target.location == u.location) {
			Stop(u);
			u.Fortify();
			return;
		}
		try {
			await Goto(u, target.location);
		} catch (BridgeError) {
			Stop(u);
			u.Fortify();
		}
		if (Alive(u) && u.location == target.location && !orders.ContainsKey(u)) u.Fortify();
	}

	static bool Try(Func<string> order) {
		try {
			order();
			return true;
		} catch (BridgeError) {
			return false;
		}
	}
}
