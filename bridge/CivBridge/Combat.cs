using System.Text.Json.Nodes;
using C7GameData;

namespace CivBridge;

// Attack and bombard (docs/protocol.md). Combat is the engine's own: an attack is a move into a tile held by a civ
// at war with the human, and bombard is MapUnit.Bombard. Cities that fall are razed, as the engine does.
sealed partial class Session {
	sealed record AttackTarget(Tile Tile, MapUnit Defender, City City, Player Owner);

	/// <summary>The adjacent tiles this unit can attack now: an enemy unit or city of a civ at war with the human.</summary>
	List<AttackTarget> AttackTargets(MapUnit u) {
		if (u.unitType.attack <= 0 || !u.movementPoints.canMove) return [];
		return [.. u.location.neighbors.Values.Where(Tile.IsTileValid).Select(t => Enemy(u, t))
			.Where(e => e != null && PlayerRelationship.AtWar(human, e.Owner) && u.CanEnter(e.Tile))];
	}

	AttackTarget Enemy(MapUnit u, Tile t) {
		MapUnit d = t.FindTopDefender(u);
		d = d != MapUnit.NONE && d.owner != human ? d : null;
		City c = t.HasCity() && t.cityAtTile.owner != human ? t.cityAtTile : null;
		Player owner = d?.owner ?? c?.owner;
		return owner == null ? null : new AttackTarget(t, d, c, owner);
	}

	JsonObject TargetJson(MapUnit u, AttackTarget e) => new() {
		["x"] = e.Tile.XCoordinate,
		["y"] = e.Tile.YCoordinate,
		["dir"] = Direction(u.location, e.Tile),
		["owner"] = Owner(e.Owner),
		["defender"] = e.Defender == null ? null : $"{e.Defender.unitType.name} {e.Defender.hitPointsRemaining}/{e.Defender.maxHitPoints} hp",
		["city"] = e.City?.name,
		["win_chance"] = e.Defender == null ? 1.0 : Math.Round(WinChance(u, e.Defender), 2),
	};

	/// <summary>
	/// The attacker's chance to win, from the engine's strengths: each round the attacker wins with a/(a+d) and the loser
	/// loses one hit point. Retreats and defensive bombard are left out, so it is an estimate.
	/// </summary>
	static double WinChance(MapUnit a, MapUnit d) {
		TileDirection dir = a.location.DirectionTo(d.location);
		double sa = a.StrengthVersus(d, CombatRole.Attack, dir), sd = d.StrengthVersus(a, CombatRole.Defense, dir);
		if (sa <= 0) return 0;
		double p = sa / (sa + sd);
		int hpA = Math.Max(1, a.hitPointsRemaining), hpD = Math.Max(1, d.hitPointsRemaining);
		double win = 0, term = Math.Pow(p, hpD);
		for (int k = 0; k < hpA; k++) {
			win += term;
			term *= (hpD + k) / (double)(k + 1) * (1 - p);
		}
		return Math.Min(1, win);
	}

	async Task<string> Attack(MapUnit u, Tile target) {
		if (u.unitType.attack <= 0) throw Invalid(u, $"{Label(u)} cannot attack.");
		if (!u.movementPoints.canMove)
			throw new BridgeError("no_moves", $"{Label(u)} has no moves left this turn; it can attack next turn.", suggest: "end_turn()");
		AttackTarget e = Enemy(u, target);
		if (e == null) throw NoTarget(u, $"There is nothing to attack at {At(target)}.");
		if (!PlayerRelationship.AtWar(human, e.Owner))
			throw new BridgeError("at_peace", $"{Owner(e.Owner)} is at peace with you; declare war before attacking.",
				suggest: $"diplomacy(action=\"declare_war\", civ=\"{Owner(e.Owner)}\")");
		int distance = u.location.DistanceTo(target);
		if (distance != 1) throw NoTarget(u, $"{At(target)} is {distance} tiles away; an attack is on an adjacent tile.");
		if (!u.CanEnter(target)) throw NoTarget(u, $"{Label(u)} cannot reach {At(target)}.");

		Stop(u);
		string label = Label(u), foe = e.Defender == null ? null : $"the {Owner(e.Owner)} {e.Defender.unitType.name}";
		double odds = e.Defender == null ? 1 : WinChance(u, e.Defender);
		bool alive = await u.Move(u.location.DirectionTo(target), true);
		DrainUi();
		bool killed = e.Defender != null && !gd.mapUnits.Contains(e.Defender);
		bool razed = e.City != null && !gd.cities.Contains(e.City);
		string hp = alive ? $"; {label} has {u.hitPointsRemaining}/{u.maxHitPoints} hp" : "";
		string fell = razed ? $" {e.City.name} fell and was razed (this engine destroys the cities it takes)." : "";
		if (foe == null) return $"{label} entered {At(target)}.{fell}{hp}.";
		string outcome = !alive ? $"lost: {label} was destroyed" : killed ? $"won: {foe} was destroyed" : "ended with a retreat";
		if (SeatOf(e.Owner) is Seat victim) {
			string theirs = $"{victim.Ids.Of(e.Defender)} {e.Defender.unitType.name}", attacker = $"{Owner(human)} {u.unitType.name}";
			Notify(e.Owner, killed ? "unit_lost" : "attacked", killed
				? $"{theirs} was lost at {At(target)} to an attacking {attacker}."
				: $"{theirs} at {At(target)} held off an attacking {attacker}{(alive ? "" : ", which was destroyed")}.", target);
		}
		return $"{label} attacked {foe} at {At(target)} (win chance about {odds:P0}) and {outcome}{hp}.{fell}";
	}

	/// <summary>The tiles in range this unit can bombard: an enemy unit, city or improvement of a civ at war with the human.</summary>
	List<Tile> BombardTargets(MapUnit u) {
		if (u.unitType.bombard <= 0 || !u.HasBombardAbility() || !u.movementPoints.canMove) return [];
		int range = Math.Max(1, u.unitType.bombardRange);
		return [.. gd.map.tiles.Where(t => t != u.location && u.location.DistanceTo(t) <= range && BombardOwner(u, t) is Player o
			&& PlayerRelationship.AtWar(human, o) && u.CanBombardTile(t))];
	}

	Player BombardOwner(MapUnit u, Tile t) {
		MapUnit d = t.FindTopDefenderForBombard(u);
		if (d != MapUnit.NONE && d.owner != human) return d.owner;
		if (t.HasCity() && t.cityAtTile.owner != human) return t.cityAtTile.owner;
		Player owner = t.OwningPlayer();
		return owner != null && owner != human && t.HasImprovements ? owner : null;
	}

	async Task<string> BombardOrder(MapUnit u, Tile target) {
		if (u.unitType.bombard <= 0 || !u.HasBombardAbility()) throw Invalid(u, $"{Label(u)} cannot bombard.");
		if (!u.movementPoints.canMove)
			throw new BridgeError("no_moves", $"{Label(u)} has no moves left this turn; it can bombard next turn.", suggest: "end_turn()");
		Player owner = BombardOwner(u, target);
		if (owner == null) throw NoBombardTarget(u, $"There is nothing to bombard at {At(target)}.");
		if (!PlayerRelationship.AtWar(human, owner))
			throw new BridgeError("at_peace", $"{Owner(owner)} is at peace with you; declare war before bombarding.",
				suggest: $"diplomacy(action=\"declare_war\", civ=\"{Owner(owner)}\")");
		if (!u.CanBombardTile(target)) throw NoBombardTarget(u, $"{Label(u)} cannot bombard {At(target)} (range {u.unitType.bombardRange}).");

		Stop(u);
		MapUnit d = target.FindTopDefenderForBombard(u);
		d = d != MapUnit.NONE ? d : null;
		City c = target.HasCity() ? target.cityAtTile : null;
		int hp = d?.hitPointsRemaining ?? 0, size = c?.residents.Count ?? 0, buildings = c?.constructed_buildings.Count ?? 0;
		await u.Bombard(target);
		DrainUi();
		var hits = new List<string>();
		if (d != null) hits.Add(gd.mapUnits.Contains(d) ? $"the {Owner(d.owner)} {d.unitType.name} lost {hp - d.hitPointsRemaining} hp" : $"the {Owner(d.owner)} {d.unitType.name} was destroyed");
		if (c != null && gd.cities.Contains(c)) {
			if (c.residents.Count < size) hits.Add($"{c.name} lost {size - c.residents.Count} population");
			if (c.constructed_buildings.Count < buildings) hits.Add($"{c.name} lost a building");
		}
		string damage = hits.Count == 0 ? "no damage." : string.Join(", ", hits) + ".";
		if (hits.Count > 0) Notify(owner, "bombarded", $"{Owner(human)} {u.unitType.name} bombarded {At(target)}: {damage}", target);
		return $"{Label(u)} bombarded {At(target)}: {damage}";
	}

	BridgeError NoBombardTarget(MapUnit u, string why) {
		var tiles = BombardTargets(u);
		return new BridgeError("bad_target", why + (tiles.Count == 0 ? $" Nothing of an enemy's is in range of {Label(u)}." : ""),
			tiles.Select(t => (JsonNode)At(t)),
			tiles.Count == 0 ? null : $"unit_order(unit=\"{ids.Of(u)}\", order=\"bombard\", x={tiles[0].XCoordinate}, y={tiles[0].YCoordinate})");
	}

	BridgeError NoTarget(MapUnit u, string why) {
		var targets = AttackTargets(u);
		return new BridgeError("bad_target", why + (targets.Count == 0 ? $" {Label(u)} has no enemy next to it." : ""),
			targets.Select(e => (JsonNode)TargetJson(u, e)),
			targets.Count == 0 ? null : $"unit_order(unit=\"{ids.Of(u)}\", order=\"attack\", x={targets[0].Tile.XCoordinate}, y={targets[0].Tile.YCoordinate})");
	}
}
