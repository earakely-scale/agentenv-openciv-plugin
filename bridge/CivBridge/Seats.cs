using System.Text.Json.Nodes;
using C7GameData;

namespace CivBridge;

/// <summary>
/// A civilization the client plays and what the bridge keeps for it. A game has one seat, or several (new_game seats),
/// one per agent: each seat's commands name it with "seat", and the turn advances once every seat has ended it.
/// </summary>
sealed class Seat(Player player, string label) {
	public readonly Player Player = player;
	public readonly string Label = label;
	public readonly Ids Ids = new();
	public readonly Dictionary<MapUnit, Order> Orders = [];
	public List<JsonObject> LastEvents = [], TurnEvents = [], Autos = [];

	/// <summary>What other seats did to this one during the turn (an attack, a peace offer), reported when the turn ends.</summary>
	public List<JsonObject> Incoming = [];

	public bool Ready;

	/// <summary>The contacts and wars last reported to this seat.</summary>
	public HashSet<ID> MetSeen = [];
	public HashSet<(Player, Player)> WarsSeen = [];

	/// <summary>Peace this seat offered another seat, with the gold it pays and the turn it offered it.</summary>
	public readonly Dictionary<Player, (int Gold, int Turn)> PeaceOffers = [];

	public readonly Dictionary<City, string> ProducingSource = [];

	/// <summary>What each city builds next, after its current item (set_production's `then`), first first.</summary>
	public readonly Dictionary<City, List<IProducible>> Queues = [];
	public readonly HashSet<City> PendingProduction = [];
	public string ResearchSource = Source.Engine;
	public bool ResearchPending;
	public HashSet<Tech> AgentResearch = [];
	public readonly Dictionary<string, int> Decided = new() {
		["production/agent"] = 0, ["production/engine"] = 0, ["research/agent"] = 0, ["research/engine"] = 0,
	};

	public readonly Dictionary<City, int> ShieldsLost = [];
	public HashSet<City> RiskSeen = [], CappedSeen = [], DefenselessSeen = [];
	public HashSet<MapUnit> ThreatsSeen = [];
	public MapUnit BotExplorer;

	/// <summary>The government the revolution under way ends in; null leaves the pick to the bridge's default.</summary>
	public Government RevolutionTarget;

	/// <summary>The science and luxury rates before anarchy (which allows none), restored when it ends.</summary>
	public (int Science, int Luxury)? RatesBeforeAnarchy;
}

sealed partial class Session {
	readonly List<Seat> seats = [];

	/// <summary>The seat the current command plays; the rest of the bridge sees it as "the human".</summary>
	Seat seat;

	Player human => seat.Player;
	Ids ids => seat.Ids;
	Dictionary<MapUnit, Order> orders => seat.Orders;
	List<JsonObject> lastEvents { get => seat.LastEvents; set => seat.LastEvents = value; }
	List<JsonObject> turnEvents { get => seat.TurnEvents; set => seat.TurnEvents = value; }
	List<JsonObject> autos { get => seat.Autos; set => seat.Autos = value; }
	Dictionary<City, string> producingSource => seat.ProducingSource;
	HashSet<City> pendingProduction => seat.PendingProduction;
	string researchSource { get => seat.ResearchSource; set => seat.ResearchSource = value; }
	bool researchPending { get => seat.ResearchPending; set => seat.ResearchPending = value; }
	HashSet<Tech> agentResearch { get => seat.AgentResearch; set => seat.AgentResearch = value; }
	Dictionary<string, int> decided => seat.Decided;
	Dictionary<City, int> shieldsLost => seat.ShieldsLost;
	HashSet<City> riskSeen { get => seat.RiskSeen; set => seat.RiskSeen = value; }
	HashSet<City> cappedSeen { get => seat.CappedSeen; set => seat.CappedSeen = value; }
	HashSet<City> defenselessSeen { get => seat.DefenselessSeen; set => seat.DefenselessSeen = value; }
	HashSet<MapUnit> threatsSeen { get => seat.ThreatsSeen; set => seat.ThreatsSeen = value; }
	MapUnit botExplorer { get => seat.BotExplorer; set => seat.BotExplorer = value; }
	Government revolutionTarget { get => seat.RevolutionTarget; set => seat.RevolutionTarget = value; }
	(int Science, int Luxury)? ratesBeforeAnarchy { get => seat.RatesBeforeAnarchy; set => seat.RatesBeforeAnarchy = value; }

	bool MultiSeat => seats.Count > 1;

	Seat SeatOf(Player p) => seats.FirstOrDefault(s => s.Player == p);

	Seat SeatArg(Args a) {
		if (!a.Has("seat")) return seats[0];
		string civ = a.Str("seat");
		return seats.FirstOrDefault(s => Same(Owner(s.Player), civ))
			?? throw new BridgeError("unknown_seat", $"'{civ}' is not a seat in this game; the seats are {string.Join(", ", seats.Select(s => Owner(s.Player)))}.",
				BridgeError.Names(seats.Select(s => Owner(s.Player))));
	}

	/// <summary>The extra seats new_game asks for take the first opponent slots; the rest are the engine's AI.</summary>
	static List<string> SeatCivs(Args a, string primary, int opponents, IEnumerable<string> civs) {
		var names = a.Has("seats") ? a.Strings("seats") : [];
		var known = civs.ToList();
		var picked = new List<string>();
		foreach (string name in names) {
			string civ = known.FirstOrDefault(c => Same(c, name))
				?? throw new BridgeError("bad_args", $"Unknown seat civ '{name}'.", BridgeError.Names(known));
			if (Same(civ, primary) || picked.Contains(civ)) throw new BridgeError("bad_args", $"{civ} is given twice; each seat is a different civ.");
			picked.Add(civ);
		}
		if (picked.Count > opponents)
			throw new BridgeError("bad_args", $"{picked.Count} seats need at least {picked.Count} opponents, not {opponents}: seats are opponent slots.");
		return picked;
	}

	void SeatGame(Player primary, List<string> extra, JsonObject labels) {
		string Label(string civ) => labels?.FirstOrDefault(kv => Same(kv.Key, civ)).Value?.GetValue<string>();
		seats.Clear();
		seats.Add(new Seat(primary, Label(primary.civilization.name)));
		foreach (string civ in extra) {
			Player p = gd.players.First(x => x.civilization.name == civ);
			// Human players get no AI turn (TurnHandling.PlayPlayerTurns) and the human's costs; the first stays the UI controller.
			p.isHuman = true;
			seats.Add(new Seat(p, Label(civ)));
		}
		EachSeat(_ => SeeRelations());
		seat = seats[0];
	}

	/// <summary>Runs `body` as each seat in turn, then restores the active seat.</summary>
	async Task EachSeat(Func<Seat, Task> body) {
		Seat active = seat;
		try {
			foreach (Seat s in seats) {
				seat = s;
				await body(s);
			}
		} finally {
			seat = active;
		}
	}

	void EachSeat(Action<Seat> body) {
		Seat active = seat;
		try {
			foreach (Seat s in seats) {
				seat = s;
				body(s);
			}
		} finally {
			seat = active;
		}
	}

	/// <summary>Ends this seat's turn; the last seat to end it advances the game, and every seat gets its own result.</summary>
	async Task<JsonNode> EndSeatTurn() {
		seat.Ready = true;
		var waiting = seats.Where(s => !s.Ready && !s.Player.defeated).ToList();
		if (waiting.Count > 0) {
			return new JsonObject {
				["blocked"] = false, ["turns_advanced"] = 0, ["turn"] = gd.turn,
				["waiting_for"] = Json.Strings(waiting.Select(s => Owner(s.Player))),
			};
		}
		foreach (Seat s in seats) s.Autos = [];
		await AdvanceTurn(engineAi: false);
		var results = new JsonObject();
		EachSeat(s => {
			s.LastEvents = s.TurnEvents;
			results[Owner(s.Player)] = new JsonObject {
				["blocked"] = false,
				["turns_advanced"] = 1,
				["turn"] = gd.turn,
				["game_over"] = GameOver,
				["defeated"] = human.defeated,
				["events"] = Json.Array(s.TurnEvents, e => e.DeepClone()),
				["auto"] = Json.Array(s.Autos, e => e.DeepClone()),
			};
		});
		return new JsonObject { ["turn"] = gd.turn, ["seats"] = results };
	}

	/// <summary>Tells another seat what the active seat did to it; it reads the event with its events of this turn.</summary>
	void Notify(Player p, string kind, string text, Tile at = null) {
		if (SeatOf(p) is not Seat other || other == seat) return;
		JsonObject e = Event(kind, text, at);
		e["turn"] = gd.turn;
		other.Incoming.Add(e);
	}

	sealed record Victory(string Kind, Player Player, int Turn);

	/// <summary>Who won, once someone has: the game is then over for every seat.</summary>
	Victory victory;

	/// <summary>
	/// Civ III's victories, for any civilization, an agent's or the AI's, checked after every turn. Conquest: it is
	/// the last civilization left (with seats, also when its seat is the last one an agent still plays). Domination:
	/// it holds two thirds of the world's land and two thirds of its population. Culture: one of its cities has
	/// 20,000 culture points, or it has 100,000 and at least twice as many as any other civilization. Score: at the
	/// turn limit, the highest score wins, and a tie on top is no one's victory.
	/// </summary>
	Victory CheckVictory() {
		if (MultiSeat && seats.Where(s => !s.Player.defeated).ToList() is [Seat last]) return new Victory("conquest", last.Player, gd.turn);
		var civs = Civs().Where(p => !p.defeated).ToList();
		if (civs is [Player only]) return new Victory("conquest", only, gd.turn);
		foreach (Player p in civs) {
			var (land, pop) = ShareOf(p);
			if (land >= Domination && pop >= Domination) return new Victory("domination", p, gd.turn);
		}
		if (CultureWinner(civs) is Player cultured) return new Victory("culture", cultured, gd.turn);
		if (gd.turn < turnLimit) return null;
		var top = civs.OrderByDescending(p => (int)ScoreOf(p)["total"]).Take(2).ToList();
		if (top.Count == 0) return null;
		return top.Count == 1 || (int)ScoreOf(top[0])["total"] > (int)ScoreOf(top[1])["total"] ? new Victory("score", top[0], gd.turn) : null;
	}

	const double Domination = 2.0 / 3;

	/// <summary>
	/// Civ III's cultural victory, at its default thresholds (the BIQ's OneCityCultureWin and AllCitiesCultureWin,
	/// QueryCiv3/Game.cs; the ruleset leaves them out): a city with 20,000 culture points wins for its owner, and so
	/// does a civilization with 100,000 that has at least twice as many as the next one.
	/// </summary>
	const int CityCultureWin = 20000, CivCultureWin = 100000;

	/// <summary>A civilization's culture as the engine's history counts it (Player.UpdateHistory): its cities' culture.</summary>
	static int CultureOf(Player p) => p.cities.Sum(c => c.GetCultureFor(p));

	/// <summary>The city with the most culture for its owner among these civilizations', the oldest on a tie; or null.</summary>
	City BestCultureCity(IEnumerable<Player> civs) {
		var owners = civs.ToHashSet();
		return gd.cities.Where(c => owners.Contains(c.owner)).OrderByDescending(c => c.GetCultureFor(c.owner)).FirstOrDefault();
	}

	/// <summary>The civilization that wins by culture now (CityCultureWin, CivCultureWin), if one does.</summary>
	Player CultureWinner(List<Player> civs) {
		if (BestCultureCity(civs) is City city && city.GetCultureFor(city.owner) >= CityCultureWin) return city.owner;
		var top = civs.OrderByDescending(CultureOf).Take(2).ToList();
		if (top.Count == 0 || CultureOf(top[0]) < CivCultureWin) return null;
		return top.Count == 1 || CultureOf(top[0]) >= 2L * CultureOf(top[1]) ? top[0] : null;
	}

	/// <summary>
	/// A one-seat game ends at once when its civ is defeated in its own turn (its last unit disbanded or lost), so no
	/// turn ends to check for a winner: check now (the last civilization left, say). With seats the others play on,
	/// and the end of the turn checks.
	/// </summary>
	string VictoryNow() {
		if (MultiSeat || victory != null || !human.defeated || CheckVictory() is not Victory won) return "";
		victory = won;
		return " " + VictoryText(won);
	}

	/// <summary>The civilizations in the game, the barbarians aside.</summary>
	IEnumerable<Player> Civs() => gd.players.Where(p => !p.isBarbarians);

	string SeatName(Seat s) => s.Label == null ? Owner(s.Player) : $"{Owner(s.Player)} ({s.Label})";

	string CivName(Player p) => SeatOf(p) is Seat s ? SeatName(s) : Owner(p);

	string VictoryText(Victory v) {
		string who = CivName(v.Player);
		switch (v.Kind) {
			case "conquest":
				return Civs().Count(p => !p.defeated) == 1
					? $"{who} won by conquest: it is the last civilization left."
					: $"{who} won by conquest: it is the last civilization an agent still plays.";
			case "domination":
				var (land, pop) = ShareOf(v.Player);
				return $"{who} won by domination: {land:P0} of the world's land and {pop:P0} of its population.";
			case "culture":
				City best = BestCultureCity([v.Player]);
				if (best != null && best.GetCultureFor(v.Player) >= CityCultureWin)
					return $"{who} won by culture: {best.name} has {best.GetCultureFor(v.Player):N0} culture points ({CityCultureWin:N0} win).";
				var next = Civs().Where(p => p != v.Player && !p.defeated).OrderByDescending(CultureOf).FirstOrDefault();
				return $"{who} won by culture: {CultureOf(v.Player):N0} culture points"
					+ (next == null ? "." : $", at least twice {CivName(next)}'s {CultureOf(next):N0}.");
			default:
				var runnerUp = Civs().Where(p => p != v.Player).OrderByDescending(p => (int)ScoreOf(p)["total"]).FirstOrDefault();
				return $"{who} won on score at the turn limit: {ScoreOf(v.Player)["total"]}"
					+ (runnerUp == null ? "." : $" to {CivName(runnerUp)}'s {ScoreOf(runnerUp)["total"]}.");
		}
	}

	JsonObject VictoryJson() => victory == null ? null : new JsonObject {
		["kind"] = victory.Kind, ["civ"] = Owner(victory.Player), ["label"] = SeatOf(victory.Player)?.Label, ["turn"] = victory.Turn,
	};

	JsonArray SeatsJson() => Json.Array(seats, s => new JsonObject { ["civ"] = Owner(s.Player), ["label"] = s.Label });
}
