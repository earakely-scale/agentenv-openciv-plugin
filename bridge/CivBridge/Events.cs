using System.Text.Json.Nodes;
using C7Engine;
using C7GameData;

namespace CivBridge;

// Turn events: the engine reports little, so most come from comparing the human's state before and
// after the other players move. War declarations, destroyed cities/civs and thefts come from UI
// messages (DrainUi); threats, undefended cities, riot risks and capped production from a look at the
// board once the human's next turn has started.
sealed partial class Session {
	sealed record CitySnap(int Size, int Buildings, bool Disorder, IProducible Item, int Stored);

	sealed record UnitSnap(string Label, Tile At, string Level);

	sealed record Snapshot(
		Dictionary<City, CitySnap> Cities, Dictionary<MapUnit, UnitSnap> Units, HashSet<ID> Techs, Tech Research,
		Dictionary<MapUnit, Tile> Barbarians);

	Snapshot Take() {
		ids.Sync(gd, human);
		return new Snapshot(
			human.cities.ToDictionary(c => c, c => new CitySnap(c.residents.Count, c.constructed_buildings.Count, c.isInCivilDisorder, c.itemBeingProduced, c.shieldsStored)),
			human.units.ToDictionary(u => u, u => new UnitSnap(Label(u), u.location, u.experienceLevelKey)),
			[.. human.knownTechs],
			gd.GetTech(human.currentlyResearchedTech),
			gd.mapUnits.Where(u => u.owner.isBarbarians).ToDictionary(u => u, u => u.location));
	}

	List<JsonObject> Diff(Snapshot before) {
		ids.Sync(gd, human);
		var events = new List<JsonObject>();
		var newUnits = HumanUnits().Where(u => !before.Units.ContainsKey(u)).ToList();
		foreach (City c in HumanCities()) {
			if (!before.Cities.TryGetValue(c, out CitySnap s)) continue;
			var builtHere = newUnits.Where(u => u.location == c.location).ToList();
			// Settlers and workers cost population, so count that before calling it starvation.
			int growth = c.residents.Count - s.Size + builtHere.Sum(u => u.unitType.populationCost);
			if (growth > 0) events.Add(Event("city_grew", $"{c.name} grew to size {c.residents.Count}.", c.location));
			if (growth < 0) events.Add(Event("city_starved", $"{c.name} starved and shrank to size {c.residents.Count}.", c.location));

			var built = c.constructed_buildings.Skip(s.Buildings).Select(b => (b.building.name, b.building == s.Item))
				.Concat(builtHere.Select(u => (Label(u), u.unitType == s.Item))).ToList();
			bool completed = built.Any(b => b.Item2);
			if (completed) Decided("production", ProducingSource(c));
			bool repicked = completed || c.itemBeingProduced != s.Item;
			// The city's queue (set_production's `then`) goes before the engine's pick.
			var skipped = new List<IProducible>();
			IProducible queued = repicked ? NextQueued(c, skipped) : null;
			if (queued != null) {
				c.SetItemBeingProduced(queued);
				AgentPickedProduction(c);
			} else if (repicked) EnginePickedProduction(c);
			string dropped = skipped.Count == 0 ? ""
				: $" ({string.Join("; ", skipped.Select(p => $"{p.name} left the queue: {WhyNot(c, p) ?? $"{c.name} cannot build it now"}"))})";
			for (int i = 0; i < built.Count; i++) {
				string next = repicked && i == built.Count - 1 && c.itemBeingProduced != null
					? (queued != null ? $"; next from your queue: {queued.name}" : $"; the engine picked {c.itemBeingProduced.name} next") + dropped
					: "";
				events.Add(Event("built", $"{c.name} built {built[i].Item1}{next}.", c.location));
			}

			int lost = 0;
			if (!repicked && c.itemBeingProduced is { } item and not Inflow && c.shieldsStored >= human.ShieldCost(item))
				lost = Math.Max(0, s.Stored + c.CurrentProductionYield().useful - human.ShieldCost(item));
			shieldsLost[c] = lost;

			if (c.isInCivilDisorder && !s.Disorder)
				events.Add(Event("disorder_started", $"{c.name} fell into civil disorder and produces nothing: {DisorderFixes(c)}.", c.location));
			if (!c.isInCivilDisorder && s.Disorder)
				events.Add(Event("disorder_ended", $"{c.name} is out of civil disorder.", c.location));
		}
		foreach (var (u, s) in before.Units) {
			if (!Alive(u)) events.Add(Event("unit_lost", $"{s.Label} was lost at {At(s.At)}.", s.At));
			else if (u.experienceLevelKey != s.Level) events.Add(Event("unit_promoted", $"{s.Label} was promoted to {u.experienceLevel.displayName}.", u.location));
		}
		var learned = gd.techs.Where(t => human.knownTechs.Contains(t.id) && !before.Techs.Contains(t.id)).ToList();
		foreach (Tech t in learned) Decided("research", t == before.Research ? researchSource : Source.Engine);
		if (learned.Count > 0) ResearchMoved();
		Tech now = gd.GetTech(human.currentlyResearchedTech);
		for (int i = 0; i < learned.Count; i++) {
			string next = i < learned.Count - 1 || now == null ? ""
				: researchSource == Source.Engine ? $"; the engine picked {now.Name} next" : $"; researching {now.Name} next, as queued";
			events.Add(Event("tech_learned", $"Learned {learned[i].Name}{next}."));
		}
		events.AddRange(Relations());
		return events;
	}

	/// <summary>
	/// Contacts and peace since the seat last looked, whoever's move made them (its own, another seat's, an AI's), so a
	/// contact made after one seat's report reaches it the next turn instead of never.
	/// </summary>
	List<JsonObject> Relations() {
		var events = new List<JsonObject>();
		foreach (ID id in human.playerRelationships.Keys.Where(id => !seat.MetSeen.Contains(id)))
			events.Add(Event("contact", $"Met {gd.GetPlayer(id)?.civilization.name}."));
		foreach (var (p, o) in seat.WarsSeen.Where(w => !w.Item1.defeated && !w.Item2.defeated && !PlayerRelationship.AtWar(w.Item1, w.Item2)))
			events.Add(Event("peace_signed", p == human || o == human
				? $"Peace with {Owner(p == human ? o : p)}." : $"{Owner(p)} and {Owner(o)} made peace."));
		SeeRelations();
		return events;
	}

	void SeeRelations() {
		seat.MetSeen = [.. human.playerRelationships.Keys];
		seat.WarsSeen = Wars();
	}

	/// <summary>
	/// Barbarians that enter a city take gold and vanish; the city is the seats' city nearest to a vanished barbarian. A theft
	/// no city explains is the one seat's, or nobody's when there are several.
	/// </summary>
	Dictionary<Seat, List<JsonObject>> Thefts(List<(MessageToUI Message, Seat Actor)> raised, Dictionary<MapUnit, Tile> barbarians) {
		var bySeat = new Dictionary<Seat, List<JsonObject>>();
		var gone = barbarians.Where(kv => !gd.mapUnits.Contains(kv.Key)).Select(kv => kv.Value).ToList();
		var amounts = raised.Where(r => r.Actor == null).Select(r => r.Message)
			.OfType<MsgShowMilitaryAdvisorPopup>().Select(p => Stolen.Match(p.message)).Where(m => m.Success).Select(m => int.Parse(m.Groups[1].Value));
		foreach (int amount in amounts) {
			var (owner, city, from) = seats.SelectMany(s => CitiesOf(s).SelectMany(c => gone.Select(t => (s, c, t))))
				.Where(p => p.t.DistanceTo(p.c.location) <= 3)
				.OrderBy(p => p.t.DistanceTo(p.c.location)).FirstOrDefault();
			if (city != null) gone.Remove(from);
			owner ??= MultiSeat ? null : seats[0];
			if (owner == null) continue;
			JsonObject e = Event("gold_stolen", city == null
				? $"Barbarians stole {amount} gold from your treasury."
				: $"Barbarians entered {city.name} and stole {amount} gold; {(Defenders(city) == 0 ? "it has no defender" : "keep defenders inside")}.",
				city?.location);
			e["amount"] = amount;
			(bySeat.TryGetValue(owner, out var list) ? list : bySeat[owner] = []).Add(e);
		}
		return bySeat;
	}

	/// <summary>Conditions that start mattering at some turn: each is reported on the turn it first appears.</summary>
	List<JsonObject> Watch() {
		var events = new List<JsonObject>();
		var hostiles = gd.mapUnits
			.Where(f => f.owner != human && f.unitType.attack > 0 && Hostile(f.owner) && human.tileKnowledge.isActiveTile(f.location)).ToList();

		var risk = HumanCities().Where(RiotRisk).ToHashSet();
		foreach (City c in risk.Where(c => !riskSeen.Contains(c))) {
			Mood m = Moods(c);
			events.Add(Event("riot_risk",
				$"{c.name} riots if it grows ({m.Unhappy} unhappy vs {m.Happy} happy now; {Defenders(c)} of {c.owner.government.militaryPoliceLimit} military police): "
				+ "keep a military unit inside or raise luxury with set_rates.", c.location));
		}
		riskSeen = risk;

		var capped = HumanCities().Where(Capped).ToHashSet();
		foreach (City c in capped.Where(c => !cappedSeen.Contains(c))) {
			IProducible item = c.itemBeingProduced;
			events.Add(Event("production_capped",
				$"{c.name}'s {item.name} has all its shields but waits for size {item.populationCost + 1} (now {c.residents.Count}); "
				+ $"its {c.CurrentProductionYield().useful} shields a turn are lost until then.", c.location));
		}
		cappedSeen = capped;

		var defenseless = new HashSet<City>();
		foreach (City c in HumanCities().Where(c => Defenders(c) == 0)) {
			MapUnit f = hostiles.Where(f => f.location.DistanceTo(c.location) <= 3).OrderBy(f => f.location.DistanceTo(c.location))
				.ThenBy(f => f.location.YCoordinate).ThenBy(f => f.location.XCoordinate).FirstOrDefault();
			if (f == null) continue;
			defenseless.Add(c);
			if (defenselessSeen.Contains(c)) continue;
			int d = f.location.DistanceTo(c.location);
			events.Add(Event("defenseless",
				$"{c.name} has no defender and {Owner(f.owner)} {f.unitType.name} is {d} tile{(d == 1 ? "" : "s")} {Direction(c.location, f.location)}.", c.location));
		}
		defenselessSeen = defenseless;

		events.AddRange(Threats(hostiles));
		return events;
	}

	/// <summary>
	/// Hostile (barbarian or at-war) combat units within 3 tiles of a city or a settler. Each unit is
	/// reported once, when it comes close, and again only after it has left and come back.
	/// </summary>
	List<JsonObject> Threats(List<MapUnit> hostiles) {
		var events = new List<JsonObject>();
		var near = new HashSet<MapUnit>();
		var targets = HumanCities().Select(c => (c.location, c.name))
			.Concat(HumanUnits().Where(u => u.unitType.isSettler).Select(u => (u.location, Label(u))));
		foreach (var (at, name) in targets) {
			var close = hostiles.Where(f => f.location.DistanceTo(at) <= 3)
				.OrderBy(f => f.location.DistanceTo(at)).ThenBy(f => f.location.YCoordinate).ThenBy(f => f.location.XCoordinate).ToList();
			var fresh = close.Where(f => !threatsSeen.Contains(f) && !near.Contains(f)).ToList();
			near.UnionWith(close);
			if (fresh.Count == 0) continue;
			MapUnit f = fresh[0];
			string stance = f.owner.isBarbarians ? "" : " (at war)";
			string more = fresh.Count > 1 ? $" and {fresh.Count - 1} more" : "";
			int d = f.location.DistanceTo(at);
			events.Add(Event("threat",
				$"{Owner(f.owner)} {f.unitType.name}{stance}{more} {d} tile{(d == 1 ? "" : "s")} {Direction(at, f.location)} of {name}.", f.location));
		}
		threatsSeen = near;
		return events;
	}

	bool Hostile(Player p) => p.isBarbarians || PlayerRelationship.AtWar(human, p);
}
