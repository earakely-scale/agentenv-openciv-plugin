using System.Text.Json.Nodes;
using C7GameData;

namespace CivBridge;

// Battles as the engine fights them (docs/protocol.md, known_map "battles"), for the play page to animate the way the
// client does. Animations are off, so the engine sends no animation messages; patches/0010 has MapUnit.Fight and
// MapUnit.BombardUnits tell an ICombatObserver of each round as they draw it. Nothing here draws from GameData.rng or
// changes game state.
sealed partial class Session : ICombatObserver {
	/// <summary>One side of a battle: the unit, and what it was when the rounds began.</summary>
	sealed class Side {
		public MapUnit Unit;
		public Player Owner;
		public string Type;
		public int X, Y, HpBefore, HpAfter, HpMax;

		/// <summary>The owner seat's id of the unit, as an autosave keeps it (a restored battle has no unit).</summary>
		public string SavedId;

		public static Side Of(MapUnit u) => new() {
			Unit = u, Owner = u.owner, Type = u.unitType.name, X = u.location.XCoordinate, Y = u.location.YCoordinate,
			HpBefore = u.hitPointsRemaining, HpAfter = u.hitPointsRemaining, HpMax = u.maxHitPoints,
		};
	}

	sealed class Battle {
		/// <summary>Id: the battle's number; Seq: its place among battles and steps (Moves.cs), as the rounds begin.</summary>
		public int Id, Seq, Turn;
		public bool Bombard;
		public Side Attacker, Defender;
		public readonly List<string> Rounds = [];
		/// <summary>"attacker", "defender" or "retreat"; null while the rounds go on.</summary>
		public string Winner;
		public City City;
		public (int X, int Y, string Name)? CityAt;
		/// <summary>A city that fell to the winner: taken (Captured), or destroyed (Razed: a city of size 1).</summary>
		public bool Razed, Captured, FallPending;
		public HashSet<Player> Seen = new(ReferenceEqualityComparer.Instance);
	}

	/// <summary>The battles of this turn and the last, oldest first.</summary>
	readonly List<Battle> battles = [];

	/// <summary>Battles whose rounds are under way, innermost last (a retreat can lead into another battle).</summary>
	readonly List<Battle> fighting = [];

	int battleCount;

	public void CombatStarted(MapUnit attacker, MapUnit defender, bool bombard) {
		SettleBattles();
		City city = defender.location.HasCity() ? defender.location.cityAtTile : null;
		var b = new Battle {
			Id = ++battleCount, Seq = ++eventSeq, Turn = gd.turn, Bombard = bombard, Attacker = Side.Of(attacker), Defender = Side.Of(defender),
			City = city, CityAt = city == null ? null : (city.location.XCoordinate, city.location.YCoordinate, city.name),
		};
		// Who could see it, as it began: the seats whose units fight, or that see either tile.
		foreach (Seat s in seats) {
			Player p = s.Player;
			if (attacker.owner == p || defender.owner == p
				|| p.tileKnowledge.isActiveTile(attacker.location) || p.tileKnowledge.isActiveTile(defender.location))
				b.Seen.Add(p);
		}
		battles.Add(b);
		fighting.Add(b);
	}

	public void CombatRound(MapUnit attacker, MapUnit defender, bool attackerWon) {
		if (Fighting(attacker, defender) is Battle b) b.Rounds.Add(attackerWon ? "a" : "d");
	}

	public void CombatEnded(MapUnit attacker, MapUnit defender) {
		if (Fighting(attacker, defender) is not Battle b) return;
		fighting.Remove(b);
		b.Attacker.HpAfter = Math.Max(0, attacker.hitPointsRemaining);
		b.Defender.HpAfter = Math.Max(0, defender.hitPointsRemaining);
		// Rounds go on until a side has no hit points left or retreats; a bombardment stops after its shots.
		b.Winner = defender.hitPointsRemaining <= 0 ? "attacker"
			: b.Bombard || attacker.hitPointsRemaining <= 0 ? "defender" : "retreat";
		// A winning attacker moves into the city at once when no defender is left and takes it, or destroys it at size
		// 1 (MapUnit.Move, then OnEnterTile: patches/0011); barbarians take gold instead. Whether it did shows the next
		// time the bridge looks, before any other battle or command.
		b.FallPending = !b.Bombard && b.Winner == "attacker" && b.City != null && !attacker.owner.isBarbarians;
	}

	Battle Fighting(MapUnit attacker, MapUnit defender) =>
		fighting.LastOrDefault(b => b.Attacker.Unit == attacker && b.Defender.Unit == defender);

	/// <summary>Settles the cities' falls pending since the last look and forgets battles older than the last turn.</summary>
	void SettleBattles() {
		foreach (Battle b in battles.Where(b => b.FallPending)) {
			b.Razed = !gd.cities.Contains(b.City);
			b.Captured = !b.Razed && b.City.owner == b.Attacker.Owner;
			b.FallPending = false;
		}
		battles.RemoveAll(b => b.Turn < gd.turn - 1 && !fighting.Contains(b));
	}

	/// <summary>The battles the active seat saw, this turn and the last, as known_map reports them.</summary>
	JsonArray BattlesJson() {
		SettleBattles();
		return Json.Array(battles.Where(b => b.Winner != null && b.Seen.Contains(human)), b => BattleJson(b));
	}

	/// <summary>A battle as a seat sees it; `allIds` (for an autosave) keeps every seat's unit ids.</summary>
	JsonObject BattleJson(Battle b, bool allIds = false) {
		var index = PlayerIndex();
		JsonObject SideJson(Side s) => new() {
			["owner"] = s.Owner == null ? -1 : index.GetValueOrDefault(s.Owner, -1),
			["type"] = s.Type,
			["x"] = s.X,
			["y"] = s.Y,
			["id"] = allIds || s.Owner == human ? SideId(s) : null,
			["hp_before"] = s.HpBefore,
			["hp_after"] = s.HpAfter,
			["hp_max"] = s.HpMax,
		};
		var o = new JsonObject {
			["id"] = b.Id,
			["seq"] = b.Seq,
			["turn"] = b.Turn,
			["kind"] = b.Bombard ? "bombard" : "attack",
			["attacker"] = SideJson(b.Attacker),
			["defender"] = SideJson(b.Defender),
			["rounds"] = Json.Strings(b.Rounds),
			["winner"] = b.Winner,
			["city"] = b.CityAt is var (x, y, name) ? new JsonObject { ["x"] = x, ["y"] = y, ["name"] = name } : null,
			["captured"] = b.Captured,
			["razed"] = b.Razed,
		};
		if (allIds) o["seen"] = Json.Array(b.Seen.Where(index.ContainsKey).Select(p => index[p]).Order(), i => (JsonNode)i);
		return o;
	}

	/// <summary>The owner seat's id of a unit that fought; null for a unit that died before the seat ever saw it.</summary>
	string SideId(Side s) => s.SavedId ?? (s.Unit != null ? SeatOf(s.Owner)?.Ids.Of(s.Unit) : null);

	/// <summary>The first battle `u` fought as the attacker since battle `after`, for the active seat.</summary>
	JsonObject BattleOf(MapUnit u, int after) {
		SettleBattles();
		Battle b = battles.FirstOrDefault(b => b.Id > after && b.Attacker.Unit == u && b.Winner != null);
		return b == null ? null : BattleJson(b);
	}

	/// <summary>Every battle begun after `since` (in the sequence), whoever saw it (docs/recording.md); `seen` is a mask over
	/// the seats.</summary>
	JsonArray SnapshotBattles(int since) {
		SettleBattles();
		return Json.Array(battles.Where(b => b.Winner != null && b.Seq > since), b => {
			JsonObject o = BattleJson(b);
			foreach (string side in new[] { "attacker", "defender" }) o[side]!.AsObject().Remove("id");
			o["seen"] = SeatMask(b.Seen);
			return o;
		});
	}

	/// <summary>The battle history and count, and the sequence battles and steps share, for an autosave.</summary>
	JsonObject BattlesState() {
		SettleBattles();
		return new JsonObject {
			["count"] = battleCount,
			["seq"] = eventSeq,
			["list"] = Json.Array(battles.Where(b => b.Winner != null), b => BattleJson(b, allIds: true)),
		};
	}

	void RestoreBattles(JsonObject state) {
		battles.Clear();
		steps.Clear();
		battleCount = state == null ? 0 : (int)state["count"];
		// Saves from before patches/0012 have no sequence: the battles' ids stand in for it.
		shownSeq = recordedSeq = eventSeq = state == null ? 0 : (int?)state["seq"] ?? battleCount;
		if (state == null) return;
		Player PlayerAt(JsonNode i) => (int)i >= 0 && (int)i < gd.players.Count ? gd.players[(int)i] : null;
		Side SideOf(JsonNode s) => new() {
			Owner = PlayerAt(s["owner"]), Type = (string)s["type"], X = (int)s["x"], Y = (int)s["y"], SavedId = (string)s["id"],
			HpBefore = (int)s["hp_before"], HpAfter = (int)s["hp_after"], HpMax = (int)s["hp_max"],
		};
		foreach (JsonNode o in state["list"]!.AsArray()) {
			var b = new Battle {
				Id = (int)o["id"], Seq = (int?)o["seq"] ?? (int)o["id"], Turn = (int)o["turn"], Bombard = (string)o["kind"] == "bombard",
				Attacker = SideOf(o["attacker"]), Defender = SideOf(o["defender"]),
				Winner = (string)o["winner"], Razed = (bool)o["razed"], Captured = (bool?)o["captured"] ?? false,
				CityAt = o["city"] is JsonObject c ? ((int)c["x"], (int)c["y"], (string)c["name"]) : null,
			};
			b.Rounds.AddRange(o["rounds"]!.AsArray().Select(r => (string)r));
			foreach (JsonNode i in o["seen"]!.AsArray()) if (PlayerAt(i) is Player p) b.Seen.Add(p);
			battles.Add(b);
		}
	}
}
