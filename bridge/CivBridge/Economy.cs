using System.Text.Json.Nodes;
using C7GameData;

namespace CivBridge;

// Order and money: citizen moods, the tax/science/luxury rates and hurrying production.
sealed partial class Session {
	sealed record Mood(int Happy, int Content, int Unhappy) {
		/// <summary>The engine's riot rule (City.RecalculateCitizenMoods): more unhappy than happy citizens.</summary>
		public bool Riots => Unhappy > 0 && Unhappy > Happy;
	}

	/// <summary>Land units with defence: under most governments each one in a city calms an unhappy citizen.</summary>
	static int Defenders(City c) => c.location.unitsOnTile.Count(u => u.CanDefendOnLand());

	/// <summary>
	/// City.RecalculateCitizenMoods on counts instead of on the residents, so hypotheticals (one more
	/// citizen, another luxury rate, more police) can be evaluated without touching the city.
	/// </summary>
	Mood Moods(City c, int extraCitizens = 0, int? luxury = null, int extraPolice = 0) {
		const int H = 0, C = 1, U = 2;
		int laborers = c.residents.Count(r => r.citizenType.IsDefaultCitizen) + extraCitizens;
		int born = Math.Min(gd.gameDifficulty.NumberOfCitizensBornContent, laborers);
		int[] n = [0, born, laborers - born];
		int Move(int count, int from, int to) {
			int k = Math.Clamp(count, 0, n[from]);
			n[from] -= k;
			n[to] += k;
			return k;
		}
		void Consume(int moves) {
			moves -= Move(moves, C, H);
			moves -= 2 * Move(moves / 2, U, H);
			Move(moves, U, C);
		}

		int toHappy = 0, toContent = 0;
		if (c.turnsOfUnhappinessDueToPopRushing > 0)
			toHappy -= (c.turnsOfUnhappinessDueToPopRushing - 1) / gd.rules.TurnPenaltyForEachHurrySacrifice + 1;
		foreach (CityBuilding cb in c.GetBuildings()) toContent += cb.building.contentFacesInCity - cb.building.unhappyFacesInCity;
		toContent += Math.Min(c.owner.government.militaryPoliceLimit, Defenders(c) + extraPolice);
		toHappy += LuxuryHappiness(c, luxury ?? c.owner.luxuryRate);
		int lux = c.GetLuxuries(gd).Keys.Count;
		if (c.GetBuildings().Any(x => x.building.increasesLuxuryTrade))
			lux = (int)(Math.Floor(lux / 2f) * Math.Ceiling(lux / 2f) + Math.Ceiling(lux / 2f));
		toHappy += lux;

		if (toHappy >= 0 && toContent >= 0) {
			Move(toContent, U, C);
			Consume(toHappy);
		} else if (toHappy >= 0) {
			Consume(Math.Max(0, toHappy + toContent));
			Move(Math.Max(0, -toContent - toHappy), H, C);
		} else if (toContent >= 0) {
			Move(Math.Max(0, -toHappy - toContent), C, U);
			Move(Math.Max(0, toContent + toHappy), U, C);
		} else {
			Move(-toHappy, C, U);
		}
		// Specialists keep whatever mood they have; the engine counts them too.
		var others = c.residents.Where(r => !r.citizenType.IsDefaultCitizen).ToList();
		return new Mood(
			n[H] + others.Count(r => r.mood == CityResident.Mood.Happy),
			n[C] + others.Count(r => r.mood == CityResident.Mood.Content),
			n[U] + others.Count(r => r.mood == CityResident.Mood.Unhappy));
	}

	/// <summary>Luxury happiness the city's commerce buys at a luxury rate (science gives way first, then tax).</summary>
	static int LuxuryHappiness(City c, int luxury) {
		Player p = c.owner;
		var (lux, sci, tax) = (p.luxuryRate, p.scienceRate, p.taxRate);
		try {
			p.luxuryRate = luxury;
			p.scienceRate = Math.Min(sci, 10 - luxury);
			p.taxRate = 10 - luxury - p.scienceRate;
			return c.CurrentCommerceYield(respectCivilDisorder: false).happiness;
		} finally {
			(p.luxuryRate, p.scienceRate, p.taxRate) = (lux, sci, tax);
		}
	}

	/// <summary>The engine's own mood calculation for the city as it stands, leaving the residents untouched.</summary>
	Mood EngineMoods(City c) {
		List<CityResident.Mood> moods = ResidentMoods(c);
		return new Mood(
			moods.Count(m => m == CityResident.Mood.Happy),
			moods.Count(m => m == CityResident.Mood.Content),
			moods.Count(m => m == CityResident.Mood.Unhappy));
	}

	/// <summary>Each resident's mood, by index, after the engine's City.RecalculateCitizenMoods (which the client runs
	/// before it draws a city's heads), leaving the residents untouched.</summary>
	List<CityResident.Mood> ResidentMoods(City c) {
		var saved = c.residents.Select(r => r.mood).ToList();
		try {
			c.RecalculateCitizenMoods(gd);
			return c.residents.Select(r => r.mood).ToList();
		} finally {
			for (int i = 0; i < saved.Count; i++) c.residents[i].mood = saved[i];
		}
	}

	bool RiotRisk(City c) => !Moods(c).Riots && Moods(c, extraCitizens: 1).Riots;

	/// <summary>Why the city riots and the moves that would end it, as one sentence fragment.</summary>
	string DisorderFixes(City c) {
		Mood m = Moods(c);
		Player p = c.owner;
		int limit = p.government.militaryPoliceLimit, defenders = Defenders(c);
		var fixes = new List<string>();
		int luxury = Enumerable.Range(p.luxuryRate + 1, Math.Max(0, p.maxLuxuryRate - p.luxuryRate)).FirstOrDefault(l => !Moods(c, luxury: l).Riots);
		if (luxury > 0)
			fixes.Add($"raise luxury to {luxury * 10}% with set_rates(science={Math.Min(p.scienceRate, 10 - luxury)}, luxury={luxury})");
		if (defenders < limit) {
			int police = Enumerable.Range(1, limit - defenders).FirstOrDefault(k => !Moods(c, extraPolice: k).Riots);
			string units = police > 1 ? $"{police} military units" : "a military unit";
			fixes.Add(police > 0
				? $"move {units} into {c.name} and fortify (each calms one unhappy citizen, up to {limit} under {p.government.name}; it has {defenders})"
				: $"military units in {c.name} help (each calms one unhappy citizen, up to {limit} under {p.government.name}; it has {defenders}) but cannot end this alone");
		}
		fixes.Add("or accept it: end_turn(skip_idle=true) ends the turn anyway");
		return $"{m.Unhappy} unhappy vs {m.Happy} happy citizens. Fix: {string.Join("; ", fixes)}";
	}

	JsonObject SetRates(Args a) {
		EnsurePlaying();
		int science = a.Has("science") ? a.Int("science") : human.scienceRate;
		int luxury = a.Has("luxury") ? a.Int("luxury") : human.luxuryRate;
		if (science < human.minScienceRate || science > human.maxScienceRate || luxury < human.minLuxuryRate || luxury > human.maxLuxuryRate
			|| science + luxury > 10)
			throw new BridgeError("bad_rates",
				$"Rates are in tenths: science {human.minScienceRate}-{human.maxScienceRate} and luxury {human.minLuxuryRate}-{human.maxLuxuryRate} "
				+ $"under {human.government.name}, with science + luxury at most 10 (tax is the rest); got science={science}, luxury={luxury}.");
		(human.scienceRate, human.luxuryRate, human.taxRate) = (science, luxury, 10 - science - luxury);

		int gpt = human.CalculateGoldPerTurn();
		Tech t = gd.GetTech(human.currentlyResearchedTech);
		JsonNode turns = t == null ? null : Json.Turns(human.EstimateTurnsToResearch(gd, t));
		string research = t == null ? "nothing is being researched" : $"{t.Name} {(turns == null ? "makes no progress" : $"in {turns} turn(s)")}";
		var rioting = HumanCities().Where(c => Moods(c).Riots).Select(c => c.name).ToList();
		string order = rioting.Count == 0 ? "no city riots at these rates" : $"still rioting at these rates: {string.Join(", ", rioting)}";
		return new JsonObject {
			["message"] = $"Rates are now tax {human.taxRate * 10}%, science {science * 10}%, luxury {luxury * 10}%: {gpt:+0;-0;0} gold a turn, {research}; {order}.",
			["rates"] = Rates(),
			["gold_per_turn"] = gpt,
			["turns_left_research"] = turns,
		};
	}

	JsonObject Rates() => new() { ["tax"] = human.taxRate, ["science"] = human.scienceRate, ["luxury"] = human.luxuryRate };

	JsonObject Hurry(Args a) {
		EnsurePlaying();
		City c = CityArg(a);
		IProducible item = c.itemBeingProduced;
		if (item == null || item is Inflow)
			throw new BridgeError("cannot_hurry", $"{c.name} is producing {item?.name ?? "nothing"}, which cannot be hurried.");
		int cost = human.ShieldCost(item);
		if (c.shieldsStored >= cost)
			throw new BridgeError("cannot_hurry", $"{c.name} already has all {cost} shields for {item.name}; "
				+ (Capped(c) ? $"it is waiting for size {item.populationCost + 1} (now {c.residents.Count})." : "it completes at the end of the turn."));

		int shields = c.shieldsStored == 0 ? 2 * cost : cost - c.shieldsStored;
		int goldCost = shields * gd.rules.ShieldValueInGold, popCost = (int)Math.Ceiling((double)shields / gd.rules.CitizenValueInShields);
		City.HurryProductionDetails details = c.GetHurryProductionDetails();
		if (details.errorMessage != null) {
			string price = human.government.hurryingType switch {
				Government.HurryProductionType.PaidLabor => $" Hurrying costs {goldCost} gold; you have {human.gold}.",
				Government.HurryProductionType.ForcedLabor =>
					$" {human.government.name} hurries with forced labour: it would cost {popCost} citizen(s), and at most half of {c.name}'s {c.residents.Count} can be sacrificed.",
				_ => "",
			};
			throw new BridgeError("cannot_hurry", $"{c.name} cannot hurry {item.name}: {details.errorMessage}{price}");
		}

		int gold = human.gold, size = c.residents.Count;
		c.HurryProduction();
		(goldCost, popCost) = (gold - human.gold, size - c.residents.Count);
		string paid = popCost > 0
			? $"{popCost} citizen(s) (each makes one more citizen unhappy for {gd.rules.TurnPenaltyForEachHurrySacrifice} turns)"
			: $"{goldCost} gold";
		string when = item.populationCost > 0 && c.residents.Count <= item.populationCost
			? $"it completes once {c.name} reaches size {item.populationCost + 1} (now {c.residents.Count})"
			: "it completes at the end of the turn";
		return new JsonObject {
			["message"] = $"{c.name} hurried {item.name} for {paid}; {when}.",
			["gold_cost"] = goldCost,
			["pop_cost"] = popCost,
			["city"] = CityJson(c),
		};
	}

	/// <summary>Production is full but the item waits for population (e.g. a Settler at size 1 or 2): extra shields are lost.</summary>
	bool Capped(City c) =>
		c.itemBeingProduced is { } item and not Inflow && item.populationCost > 0
		&& c.shieldsStored >= human.ShieldCost(item) && c.residents.Count <= item.populationCost;
}
