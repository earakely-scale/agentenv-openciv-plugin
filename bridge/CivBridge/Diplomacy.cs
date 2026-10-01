using System.Text.Json.Nodes;
using C7Engine;
using C7GameData;

namespace CivBridge;

// Governments and diplomacy (docs/protocol.md): revolutions, what the human knows of the other civs, war and peace.
// Peace goes through the engine's own deal path, at the price the AI asks (Player.PeacePriceFor, patch 0009).
sealed partial class Session {
	/// <summary>The government the revolution under way ends in; null leaves the pick to the bridge's default.</summary>
	Government revolutionTarget;

	JsonObject Governments() => new() {
		["current"] = human.government.name,
		["anarchy_until"] = human.government.transitionType ? (JsonNode)human.inAnarchyUntilTurn : null,
		["revolution_target"] = revolutionTarget?.name,
		["available"] = Json.Array(human.GetAvailableGovernments(gd), g => new JsonObject {
			["name"] = g.name,
			["corruption"] = g.corruptionType.ToString().ToLowerInvariant(),
			["hurry"] = g.hurryingType switch {
				Government.HurryProductionType.ForcedLabor => "population",
				Government.HurryProductionType.PaidLabor => "gold",
				_ => "none",
			},
			["tile_penalty"] = g.hasTilePenalty,
			["trade_bonus"] = g.hasTradeBonus,
			["unit_cost"] = g.unitCost,
			["free_units_per_city"] = g.freeUnitsPerCity,
		}),
	};

	JsonObject Revolution(Args a) {
		EnsurePlaying();
		var options = human.GetAvailableGovernments(gd);
		string name = a.Str("government").Trim();
		Government g = options.FirstOrDefault(o => string.Equals(o.name, name, StringComparison.OrdinalIgnoreCase))
			?? throw new BridgeError("unknown_government",
				$"'{name}' is not a government {human.civilization.name} can choose; it can choose {string.Join(", ", options.Select(o => o.name))}.",
				BridgeError.Names(options.Select(o => o.name)));
		if (g == human.government) throw new BridgeError("same_government", $"{human.civilization.name} already has {g.name}.");
		revolutionTarget = g;
		string message;
		if (human.government.transitionType) {
			message = $"Anarchy already lasts until turn {human.inAnarchyUntilTurn}; then the government becomes {g.name}.";
		} else {
			new StartGovernmentTransitionMsg(human).process();
			DrainUi();
			int turns = human.inAnarchyUntilTurn - gd.turn;
			message = $"Revolution: {human.civilization.name} is in anarchy until turn {human.inAnarchyUntilTurn} ({turns} turns: no taxes and no "
				+ $"science), then becomes {g.name}.";
		}
		return new JsonObject { ["message"] = message, ["government"] = Governments() };
	}

	void PickGovernment() {
		if (!human.government.transitionType || gd.turn < human.inAnarchyUntilTurn) return;
		var options = human.GetAvailableGovernments(gd);
		Government g = options.FirstOrDefault(x => x == revolutionTarget) ?? options.FirstOrDefault(x => x.name == "Monarchy")
			?? options.FirstOrDefault(x => x.defaultType) ?? options.FirstOrDefault();
		if (g == null) return;
		human.government = g;
		revolutionTarget = null;
		autos.Add(Auto("government_picked", $"Anarchy ended and the government became {g.name}."));
	}

	JsonObject Diplomacy() => new() {
		["civs"] = Json.Array(Met(), CivJson),
		["unmet"] = Rivals().Count(p => !p.defeated && !human.playerRelationships.ContainsKey(p.id)),
	};

	IEnumerable<Player> Met() => Rivals().Where(p => !p.defeated && human.playerRelationships.ContainsKey(p.id));

	JsonObject CivJson(Player p) {
		bool war = PlayerRelationship.AtWar(human, p);
		int price = war ? p.PeacePriceFor(gd, human) : 0;
		int refuseUntil = RefusesTalksUntil(p);
		double ours = Military(human);
		return new JsonObject {
			["civ"] = Owner(p),
			["at_war"] = war,
			["talks"] = refuseUntil <= gd.turn,
			["refuses_talks_until"] = refuseUntil > gd.turn ? refuseUntil : null,
			["peace_price"] = war && price != int.MaxValue ? price : null,
			["score"] = ScoreOf(p),
			["government"] = p.government.name,
			["military_vs_yours"] = ours > 0 ? Math.Round(Military(p) / ours, 1) : null,
			["at_war_with"] = Json.Strings(Rivals().Where(o => o != p && !o.defeated && Knows(o) && PlayerRelationship.AtWar(p, o)).Select(Owner)),
		};
	}

	int RefusesTalksUntil(Player p) =>
		PlayerRelationship.AtWar(human, p) && p.playerRelationships.TryGetValue(human.id, out PlayerRelationship r) ? r.refuseContactUntilTurn : -1;

	static double Military(Player p) => p.units.Where(u => u.IsCombatUnit()).Sum(u => Math.Max(u.unitType.attack, u.unitType.defense));

	Player CivArg(string name) {
		string given = (name ?? "").Trim();
		return Met().FirstOrDefault(p => string.Equals(Owner(p), given, StringComparison.OrdinalIgnoreCase))
			?? throw new BridgeError("unknown_civ",
				$"'{given}' is not a civilization you have met; you know {(Met().Any() ? string.Join(", ", Met().Select(Owner)) : "none yet")}.",
				BridgeError.Names(Met().Select(Owner)));
	}

	JsonObject DeclareWar(Args a) {
		EnsurePlaying();
		Player p = CivArg(a.Str("civ"));
		if (PlayerRelationship.AtWar(human, p)) throw new BridgeError("already_at_war", $"You are already at war with {Owner(p)}.");
		human.DeclareWarOn(p, gd.turn);
		DrainUi();
		return new JsonObject {
			["message"] = $"{human.civilization.name} declared war on {Owner(p)}, which refuses to talk until turn {RefusesTalksUntil(p)}.",
			["civ"] = CivJson(p),
		};
	}

	JsonObject ProposePeace(Args a) {
		EnsurePlaying();
		Player p = CivArg(a.Str("civ"));
		int gold = a.Int("gold", 0, 0);
		string civ = Owner(p);
		if (!PlayerRelationship.AtWar(human, p)) throw new BridgeError("not_at_war", $"You are not at war with {civ}.");
		if (!p.WillAcceptCommunicationFrom(human, gd.turn))
			throw new BridgeError("no_talks", $"{civ} refuses to talk to you until turn {RefusesTalksUntil(p)}.");
		int price = p.PeacePriceFor(gd, human);
		if (price == int.MaxValue) throw new BridgeError("refused", $"{civ} will not consider peace yet.");
		if (gold > human.gold) throw new BridgeError("not_enough_gold", $"You have {human.gold} gold, not {gold}.");
		if (gold < price) {
			string afford = human.gold >= price ? "" : $" You have {human.gold}.";
			throw new BridgeError("price", $"{civ} wants {price} gold for peace; you offered {gold}.{afford}",
				suggest: human.gold >= price ? $"diplomacy(action=\"propose_peace\", civ=\"{civ}\", gold={price})" : null);
		}
		var give = new TradeOffer { partOfPeaceTreaty = true, gold = gold > 0 ? gold : null };
		var get = new TradeOffer();
		if (!p.WouldAcceptDealFrom(gd, human, give, get)) throw new BridgeError("refused", $"{civ} turned the offer down.");
		p.ExecuteDeal(gd, human, give, get);
		DrainUi();
		return new JsonObject {
			["message"] = $"Peace with {civ}" + (gold > 0 ? $", for {gold} gold." : "."),
			["civ"] = CivJson(p),
		};
	}

	/// <summary>Every pair of known, living civs (the human included) at war, for peace events.</summary>
	HashSet<(Player, Player)> Wars() {
		var known = Rivals().Where(p => !p.defeated && Knows(p)).Append(human).ToList();
		return [.. known.SelectMany(p => known.Where(o => gd.players.IndexOf(p) < gd.players.IndexOf(o) && PlayerRelationship.AtWar(p, o))
			.Select(o => (p, o)))];
	}
}
