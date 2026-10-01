using System.Text.Json.Nodes;
using C7GameData;

namespace CivBridge;

// Turn events: the engine reports little, so most come from comparing the human's state before and
// after the other players move. War declarations and destroyed cities/civs come from UI messages (DrainUi).
sealed partial class Session {
	sealed record CitySnap(int Size, int Buildings, bool Disorder);

	sealed record UnitSnap(string Label, Tile At, string Level);

	sealed record Snapshot(Dictionary<City, CitySnap> Cities, Dictionary<MapUnit, UnitSnap> Units, HashSet<ID> Techs, HashSet<ID> Met);

	Snapshot Take() {
		ids.Sync(gd, human);
		return new Snapshot(
			human.cities.ToDictionary(c => c, c => new CitySnap(c.residents.Count, c.constructed_buildings.Count, c.isInCivilDisorder)),
			human.units.ToDictionary(u => u, u => new UnitSnap(Label(u), u.location, u.experienceLevelKey)),
			[.. human.knownTechs],
			[.. human.playerRelationships.Keys]);
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
			foreach (CityBuilding b in c.constructed_buildings.Skip(s.Buildings))
				events.Add(Event("built", $"{c.name} built {b.building.name}.", c.location));
			foreach (MapUnit u in builtHere)
				events.Add(Event("built", $"{c.name} built {Label(u)}.", c.location));
			if (c.isInCivilDisorder && !s.Disorder)
				events.Add(Event("disorder", $"{c.name} is in civil disorder: it produces nothing until its citizens are content.", c.location));
		}
		foreach (var (u, s) in before.Units) {
			if (!Alive(u)) events.Add(Event("unit_lost", $"{s.Label} was lost at {At(s.At)}.", s.At));
			else if (u.experienceLevelKey != s.Level) events.Add(Event("unit_promoted", $"{s.Label} was promoted to {u.experienceLevel.displayName}.", u.location));
		}
		foreach (Tech t in gd.techs.Where(t => human.knownTechs.Contains(t.id) && !before.Techs.Contains(t.id)))
			events.Add(Event("tech_learned", $"Learned {t.Name}."));
		foreach (ID id in human.playerRelationships.Keys.Where(id => !before.Met.Contains(id)))
			events.Add(Event("contact", $"Met {gd.GetPlayer(id)?.civilization.name}."));
		return events;
	}

	/// <summary>
	/// Visible foreign or barbarian combat units within 3 tiles of a city or a settler. Barbarians and
	/// enemies are reported every turn; units of civs at peace only when they first come close.
	/// </summary>
	List<JsonObject> Threats() {
		var events = new List<JsonObject>();
		var seen = new HashSet<(object, MapUnit)>();
		var foes = gd.mapUnits.Where(f => f.owner != human && f.unitType.attack > 0 && human.tileKnowledge.isActiveTile(f.location)).ToList();
		var targets = HumanCities().Select(c => ((object)c, c.location, c.name))
			.Concat(HumanUnits().Where(u => u.unitType.isSettler).Select(u => ((object)u, u.location, Label(u))));
		foreach (var (key, at, name) in targets) {
			var near = foes.Where(f => f.location.DistanceTo(at) <= 3)
				.OrderBy(f => f.location.DistanceTo(at)).ThenBy(f => f.location.YCoordinate).ThenBy(f => f.location.XCoordinate).ToList();
			var report = near.Where(f => Hostile(f.owner) || !threatsSeen.Contains((key, f))).ToList();
			foreach (MapUnit n in near) seen.Add((key, n));
			if (report.Count == 0) continue;
			MapUnit f = report[0];
			string stance = f.owner.isBarbarians ? "" : Hostile(f.owner) ? " (at war)" : " (at peace)";
			string more = report.Count > 1 ? $" and {report.Count - 1} more" : "";
			int d = f.location.DistanceTo(at);
			events.Add(Event("threat",
				$"{Owner(f.owner)} {f.unitType.name}{stance}{more} {d} tile{(d == 1 ? "" : "s")} {Direction(at, f.location)} of {name}.", f.location));
		}
		threatsSeen = seen;
		return events;
	}

	HashSet<(object, MapUnit)> threatsSeen = [];

	bool Hostile(Player p) => p.isBarbarians || PlayerRelationship.AtWar(human, p);
}
