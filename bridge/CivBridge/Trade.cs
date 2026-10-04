using System.Text.Json.Nodes;
using C7Engine;
using C7GameData;

namespace CivBridge;

/// <summary>
/// A trade offered to a seat (Seat.TradeOffers, by who offers it): what the offerer gives the seat, what it wants for it,
/// and the last turn it stands. An AI's offer stands for the turn the seat sees it, another seat's until the end of the
/// turn after the one it was made.
/// </summary>
sealed record StandingTrade(TradeOffer Gives, TradeOffer Wants, int Until);

// Trading techs and gold (docs/protocol.md): with an AI through the engine's own deal path, as the client's deal screen
// does it (DealScreen.AttemptDeal: the AI's WouldAcceptDealFrom judges, ExecuteDeal moves the gold and the techs); an AI
// offer made during its turn stands for the seat's next turn; between two seats a proposal stands until the other
// accepts it (or proposes the same trade back), within a turn of it.
sealed partial class Session {
	/// <summary>True while autoplay plays the seat: AI offers are declined then, as the env always did, so baselines stay put.</summary>
	bool autoplaying;
	int tradesDeclined;

	/// <summary>Techs `from` knows and `to` does not: what `from` can give `to`, most valuable to `to` first.</summary>
	List<Tech> Tradeable(Player from, Player to) => [.. gd.techs.Where(t => from.knownTechs.Contains(t.id) && !to.knownTechs.Contains(t.id))
		.OrderByDescending(t => gd.TechCostFor(t, to)).ThenBy(t => t.Name)];

	/// <summary>What `from` can give `to` (one of them the seat, the other p), with what each tech is worth to each side.</summary>
	JsonArray TechsJson(Player from, Player to, Player p) => Json.Array(Tradeable(from, to), t => new JsonObject {
		["name"] = t.Name, ["you_value"] = gd.TechCostFor(t, human), ["they_value"] = gd.TechCostFor(t, p),
	});

	static TradeOffer Offer(IEnumerable<Tech> techs, int gold) => new() { techs = [.. techs], gold = gold > 0 ? gold : null };

	static TradeOffer Copy(TradeOffer o) => Offer(o.techs, o.gold ?? 0);

	static bool SameOffer(TradeOffer a, TradeOffer b) => (a.gold ?? 0) == (b.gold ?? 0) && a.techs.ToHashSet().SetEquals(b.techs);

	static bool IsEmpty(TradeOffer o) => o.techs.Count == 0 && (o.gold ?? 0) == 0;

	/// <summary>The techs named in `key`, each one `from` can give `to`; an unknown or untradeable name fails with the ones that are.</summary>
	List<Tech> TechsArg(Args a, string key, Player from, Player to) {
		if (!a.Has(key)) return [];
		var tradeable = Tradeable(from, to);
		var techs = new List<Tech>();
		foreach (string name in a.Strings(key)) {
			Tech t = gd.techs.FirstOrDefault(x => Same(x.Name, name));
			if (t == null || !tradeable.Contains(t)) {
				string why = t == null ? $"There is no tech called '{name.Trim()}'"
					: from.knownTechs.Contains(t.id) ? $"{Owner(to)} already knows {t.Name}" : $"{Owner(from)} does not know {t.Name}";
				string can = tradeable.Count == 0 ? "none" : string.Join(", ", tradeable.Select(x => x.Name));
				throw new BridgeError("unknown_tech", $"{why}; {Owner(from)} can give {Owner(to)}: {can}.", BridgeError.Names(tradeable.Select(x => x.Name)));
			}
			if (!techs.Contains(t)) techs.Add(t);
		}
		return techs;
	}

	/// <summary>
	/// Whether `give` (the seat's side) and `get` (p's side) can change hands now: p is met, alive and at peace, every tech
	/// is known to its giver and not to its receiver, and each side has the gold it gives. Throws the reason otherwise.
	/// </summary>
	void CheckDeal(Player p, TradeOffer give, TradeOffer get, string code = null) {
		string civ = Owner(p);
		BridgeError Fail(string own, string message) => new(code ?? own, message);
		if (p.defeated || !human.playerRelationships.ContainsKey(p.id)) throw Fail("unknown_civ", $"{civ} is not a civilization you can trade with now.");
		CheckPeace(p);
		foreach (var (from, to, offer) in new[] { (human, p, give), (p, human, get) }) {
			if (offer.techs.FirstOrDefault(t => !from.knownTechs.Contains(t.id) || to.knownTechs.Contains(t.id)) is Tech t)
				throw Fail("unknown_tech", from.knownTechs.Contains(t.id) ? $"{Owner(to)} already knows {t.Name}." : $"{Owner(from)} does not know {t.Name}.");
			if ((offer.gold ?? 0) > from.gold)
				throw Fail("not_enough_gold", $"{(from == human ? "You have" : $"{Owner(from)} has")} {from.gold} gold, not {offer.gold}.");
		}
		if (IsEmpty(give) && IsEmpty(get)) throw Fail("bad_args", "The trade is empty; name techs or gold to give or to get.");
		if (SeatOf(p) == null && give.techs.Count + get.techs.Count == 0)
			throw Fail("bad_args", $"{civ} trades gold only along with a tech; name give_techs or get_techs.");
	}

	void CheckPeace(Player p) {
		string civ = Owner(p);
		if (PlayerRelationship.AtWar(human, p))
			throw new BridgeError("not_at_peace", $"You are at war with {civ}; make peace first (diplomacy(action=\"propose_peace\", civ=\"{civ}\")).");
	}

	(Player P, TradeOffer Give, TradeOffer Get) DealArgs(Args a) {
		Player p = CivArg(a.Str("civ"));
		CheckPeace(p);
		var give = Offer(TechsArg(a, "give_techs", human, p), a.Int("give_gold", 0, 0));
		var get = Offer(TechsArg(a, "get_techs", p, human), a.Int("get_gold", 0, 0));
		CheckDeal(p, give, get);
		return (p, give, get);
	}

	/// <summary>The env's call for a trade, as the agent would make it.</summary>
	static string TradeCall(string action, Player p, TradeOffer give, TradeOffer get) {
		var args = new List<string> { $"action=\"{action}\"", $"civ=\"{Owner(p)}\"" };
		static string Names(TradeOffer o) => "[" + string.Join(", ", o.techs.Select(t => $"\"{t.Name}\"")) + "]";
		if (give.techs.Count > 0) args.Add($"give_techs={Names(give)}");
		if (give.gold is > 0) args.Add($"give_gold={give.gold}");
		if (get.techs.Count > 0) args.Add($"get_techs={Names(get)}");
		if (get.gold is > 0) args.Add($"get_gold={get.gold}");
		return $"diplomacy({string.Join(", ", args)})";
	}

	JsonObject Quote(Player p, TradeOffer give, TradeOffer get) {
		bool ai = SeatOf(p) == null;
		int theyYours = give.GoldEquivalentFor(gd, p), theyTheirs = get.GoldEquivalentFor(gd, p);
		int shortfall = Math.Max(0, theyTheirs - theyYours);
		TradeOffer balanced = Offer(give.techs, (give.gold ?? 0) + shortfall);
		return new JsonObject {
			["civ"] = Owner(p),
			["agent"] = !ai,
			["you_give"] = GoodsJson(give),
			["you_get"] = GoodsJson(get),
			["you_value_give"] = give.GoldEquivalentFor(gd, human),
			["you_value_get"] = get.GoldEquivalentFor(gd, human),
			["they_value_give"] = theyYours,
			["they_value_get"] = theyTheirs,
			["accepts"] = ai ? p.WouldAcceptDealFrom(gd, human, give, get) : null,
			["gold_to_balance"] = ai ? shortfall : null,
			["gold_they_would_add"] = ai ? Math.Min(p.gold - (get.gold ?? 0), Math.Max(0, theyYours - theyTheirs)) : null,
			["suggest"] = ai && shortfall > 0 && balanced.gold <= human.gold ? TradeCall("propose_trade", p, balanced, get) : null,
		};
	}

	static JsonObject GoodsJson(TradeOffer o) => new() { ["techs"] = Json.Strings(o.techs.Select(t => t.Name)), ["gold"] = o.gold ?? 0 };

	JsonObject QuoteTrade(Args a) {
		EnsurePlaying();
		var (p, give, get) = DealArgs(a);
		return Quote(p, give, get);
	}

	JsonObject ProposeTrade(Args a) {
		EnsurePlaying();
		var (p, give, get) = DealArgs(a);
		string civ = Owner(p);
		if (SeatOf(p) is Seat other) {
			// The same trade proposed back signs it, once: the offer it answers goes.
			if (Standing(seat, p) is StandingTrade theirs && SameOffer(theirs.Gives, get) && SameOffer(theirs.Wants, give))
				return Sign(p, theirs);
			other.TradeOffers[human] = new StandingTrade(give, get, gd.turn + 1);
			Notify(p, "trade_offered", $"{Owner(human)} offers {Describe(give)} for {Describe(get)}. Accept before turn {gd.turn + 2}: "
				+ $"diplomacy(action=\"accept_trade\", civ=\"{Owner(human)}\").");
			return new JsonObject {
				["message"] = $"Offered {civ} {Describe(give)} for {Describe(get)}; the trade is made if {civ} accepts it before turn {gd.turn + 2}.",
				["civ"] = CivJson(p),
				["gold"] = human.gold,
				["research"] = Research(),
			};
		}
		if (!p.WouldAcceptDealFrom(gd, human, give, get)) {
			JsonObject q = Quote(p, give, get);
			int need = (int)q["gold_to_balance"];
			string afford = (string)q["suggest"] != null ? "" : $" You have {human.gold} gold.";
			throw new BridgeError("refused",
				$"{civ} values what you give at {q["they_value_give"]} gold and what it gives at {q["they_value_get"]}; "
				+ $"it wants {need} gold more.{afford}", suggest: (string)q["suggest"]);
		}
		return Execute(p, give, get, $"Traded with {civ}: you gave {Describe(give)} and got {Describe(get)}.");
	}

	/// <summary>The trade `from` offers `s`'s civ, while it stands: not lapsed, and the two still alive and at peace.</summary>
	StandingTrade Standing(Seat s, Player from) =>
		s != null && s.TradeOffers.TryGetValue(from, out StandingTrade t) && gd.turn <= t.Until && !from.defeated && !s.Player.defeated
			&& !PlayerRelationship.AtWar(s.Player, from) ? t : null;

	JsonObject AcceptTrade(Args a) {
		EnsurePlaying();
		Player p = CivArg(a.Str("civ"));
		return Sign(p, Standing(seat, p) ?? throw NoOffer(p));
	}

	BridgeError NoOffer(Player p) {
		string civ = Owner(p);
		string why = !seat.TradeOffers.TryGetValue(p, out StandingTrade t) ? $"{civ} has no trade offer standing for you"
			: PlayerRelationship.AtWar(human, p) ? $"{civ}'s offer ended with the war"
			: $"{civ}'s offer lapsed at the end of turn {t.Until}";
		return new BridgeError("no_offer", $"{why}; ask for a trade with diplomacy(action=\"quote_trade\", civ=\"{civ}\", ...).");
	}

	/// <summary>Makes the trade `p` offered the seat, if it still can be: the seat gives what p wants and gets what p gives.</summary>
	JsonObject Sign(Player p, StandingTrade t) {
		string civ = Owner(p);
		CheckDeal(p, t.Wants, t.Gives, "offer_changed");
		if (SeatOf(p) == null && !p.WouldAcceptDealFrom(gd, human, t.Wants, t.Gives))
			throw new BridgeError("offer_changed", $"{civ} no longer accepts its own offer: it values what you give at "
				+ $"{t.Wants.GoldEquivalentFor(gd, p)} gold and what it gives at {t.Gives.GoldEquivalentFor(gd, p)}.");
		seat.TradeOffers.Remove(p);
		SeatOf(p)?.TradeOffers.Remove(human);
		JsonObject result = Execute(p, t.Wants, t.Gives, $"Traded with {civ}: you gave {Describe(t.Wants)} and got {Describe(t.Gives)}.");
		Notify(p, "trade_signed", $"{Owner(human)} accepted your trade: you got {Describe(t.Wants)} for {Describe(t.Gives)}.");
		return result;
	}

	JsonObject DeclineTrade(Args a) {
		EnsurePlaying();
		Player p = CivArg(a.Str("civ"));
		StandingTrade t = Standing(seat, p) ?? throw NoOffer(p);
		seat.TradeOffers.Remove(p);
		Notify(p, "trade_declined", $"{Owner(human)} declined your offer of {Describe(t.Gives)} for {Describe(t.Wants)}.");
		return new JsonObject { ["message"] = $"Declined {Owner(p)}'s offer.", ["civ"] = CivJson(p) };
	}

	/// <summary>
	/// The engine's deal (Player.ExecuteDeal: gold and techs change hands), then each side's research bookkeeping: a tech
	/// got that was the one being researched moves the research on, as learning it would.
	/// </summary>
	JsonObject Execute(Player p, TradeOffer give, TradeOffer get, string message) {
		var research = new Dictionary<Seat, ID>();
		foreach (Seat s in seats) research[s] = s.Player.currentlyResearchedTech;
		p.ExecuteDeal(gd, human, give, get);
		EachSeat(s => {
			if (human.currentlyResearchedTech != research[s]) ResearchMoved();
		});
		DrainUi();
		return new JsonObject {
			["message"] = message,
			["civ"] = CivJson(p),
			["gold"] = human.gold,
			["research"] = Research(),
		};
	}

	/// <summary>An AI's offer, made during its turn: it stands for the seat's next turn (the AI's turn cannot wait for the agent).</summary>
	void OfferedByAi(MsgShowTradeOffer o) {
		if (SeatOf(o.humanPlayer) is not Seat s) return;
		s.TradeOffers[o.aiPlayer] = new StandingTrade(Copy(o.aiGive), Copy(o.aiWant), advancing ? eventTurn + 1 : gd.turn);
	}

	JsonObject TradeOfferedEvent(MsgShowTradeOffer o) {
		string civ = Owner(o.aiPlayer);
		return Event("trade_offered", $"{civ} offers {Describe(o.aiGive)} (worth {o.aiGive.GoldEquivalentFor(gd, human)} to you) for "
			+ $"{Describe(o.aiWant)} (worth {o.aiWant.GoldEquivalentFor(gd, human)} to you), until the end of turn {gd.turn}. "
			+ $"Accept with diplomacy(action=\"accept_trade\", civ=\"{civ}\").");
	}

	/// <summary>A standing offer as the seat sees it: what it gets and gives, and what each is worth to it.</summary>
	JsonObject TradeJson(StandingTrade t, bool toMe) {
		if (t == null) return null;
		TradeOffer get = toMe ? t.Gives : t.Wants, give = toMe ? t.Wants : t.Gives;
		return new JsonObject {
			["you_get"] = GoodsJson(get), ["you_give"] = GoodsJson(give),
			["you_value_get"] = get.GoldEquivalentFor(gd, human), ["you_value_give"] = give.GoldEquivalentFor(gd, human),
			["until_turn"] = t.Until,
		};
	}

	/// <summary>Forgets offers that lapsed before the last turn; one that lapsed last turn is kept to say so (NoOffer).</summary>
	void PruneTrades() {
		foreach (Seat s in seats)
			foreach (Player from in s.TradeOffers.Where(kv => gd.turn > kv.Value.Until + 1).Select(kv => kv.Key).ToList())
				s.TradeOffers.Remove(from);
	}

	JsonArray TradesState() => Json.Array(seat.TradeOffers, kv => new JsonObject {
		["civ"] = Owner(kv.Key), ["gives"] = GoodsJson(kv.Value.Gives), ["wants"] = GoodsJson(kv.Value.Wants), ["until"] = kv.Value.Until,
	});

	void RestoreTrades(JsonArray saved) {
		if (saved == null) return;   // saves from before trading have none
		TradeOffer Read(JsonNode o) => Offer(o["techs"]!.AsArray().Select(n => gd.techs.FirstOrDefault(t => t.Name == (string)n)).Where(t => t != null),
			(int)o["gold"]);
		foreach (JsonNode o in saved)
			if (gd.players.FirstOrDefault(x => !x.isBarbarians && x.civilization.name == (string)o["civ"]) is Player from)
				seat.TradeOffers[from] = new StandingTrade(Read(o["gives"]), Read(o["wants"]), (int)o["until"]);
	}
}
