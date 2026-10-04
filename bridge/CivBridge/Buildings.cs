using System.Text.Json.Nodes;
using C7GameData;
using C7GameData.Save;

namespace CivBridge;

// What buildings do: the percentages a city's buildings add to its science, taxes, luxuries and shields (patch 0016's
// City.Boost), and, for city_info's options, each building's effects in words with what it would add in that city.
sealed partial class Session {
	/// <summary>The sums of the city's buildings' percentages (City.GetBuildings, wonder-granted ones included).</summary>
	static JsonObject BuildingBonus(City c) {
		List<CityBuilding> bs = c.GetBuildings();
		return new JsonObject {
			["science"] = bs.Sum(b => b.building.sciencePercent),
			["tax"] = bs.Sum(b => b.building.taxPercent),
			["luxury"] = bs.Sum(b => b.building.luxuryPercent),
			["shields"] = bs.Sum(b => b.building.productionPercent),
		};
	}

	/// <summary>
	/// The city's yields with the building added, as the engine computes them: it is put in the city's buildings for
	/// the call and taken out again (as LuxuryHappiness does with the rates). Corruption is the city's current figure.
	/// </summary>
	static (CommerceBreakdown, CorruptableValue) YieldsWith(City c, Building b) {
		var cb = new CityBuilding { building = b, builtByPlayer = c.owner, year = 0 };
		c.constructed_buildings.Add(cb);
		try {
			return (c.CurrentCommerceYield(), c.CurrentProductionYield());
		} finally {
			c.constructed_buildings.Remove(cb);
		}
	}

	/// <summary>What a building does, in short phrases from its engine fields, economic effects first; a multiplier says
	/// what it would add in this city now (at the current rates, tiles and corruption).</summary>
	List<string> BuildingEffects(Building b, City c) {
		var effects = new List<string>();
		if (b.sciencePercent > 0 || b.taxPercent > 0 || b.luxuryPercent > 0 || b.productionPercent > 0) {
			CommerceBreakdown now = c.CurrentCommerceYield();
			CorruptableValue shieldsNow = c.CurrentProductionYield();
			var (commerce, shields) = YieldsWith(c, b);
			if (b.sciencePercent > 0)
				effects.Add($"+{b.sciencePercent}% science (+{commerce.beakers - now.beakers} here)");
			if (b.taxPercent > 0 && b.taxPercent == b.luxuryPercent)
				effects.Add($"+{b.taxPercent}% tax and luxury (+{commerce.taxes - now.taxes} gold, +{commerce.happiness - now.happiness} luxury here)");
			else {
				if (b.taxPercent > 0) effects.Add($"+{b.taxPercent}% tax (+{commerce.taxes - now.taxes} gold here)");
				if (b.luxuryPercent > 0) effects.Add($"+{b.luxuryPercent}% luxury (+{commerce.happiness - now.happiness} here)");
			}
			if (b.productionPercent > 0)
				effects.Add($"+{b.productionPercent}% shields (+{shields.useful - shieldsNow.useful} here)");
		}
		if (b.treasuryEarnsInterest)
			effects.Add($"interest on the treasury ({gd.rules.TreasuryInterestRate * 100:0}%, at most {gd.rules.MaxInterest} gold a turn)");
		if (b.reducesCorruption) {
			CommerceBreakdown commerce = c.CurrentCommerceYield();
			effects.Add($"less corruption ({commerce.corrupted} commerce, {c.CurrentProductionYield().corrupt} shields lost here)");
		}
		if (b.isForbiddenPalace) effects.Add("a second centre against corruption");
		if (b.contentFacesInCity > 0) effects.Add($"{b.contentFacesInCity} unhappy made content");
		if (b.unhappyFacesInCity > 0) effects.Add($"{b.unhappyFacesInCity} more unhappy");
		if (b.increasesLuxuryTrade) effects.Add("more happiness from luxury resources");
		if (b.allowsCitySize2) effects.Add($"grows past {gd.rules.MaximumLevel1CitySize}");
		if (b.allowsCitySize3) effects.Add($"grows past {gd.rules.MaximumLevel2CitySize}");
		if (b.doublesCityGrowthRate) effects.Add("keeps half its food on growth");
		if (b.combatDefenseBonus is { } defence)
			effects.Add($"+{defence.amount * 100:0}% defence" + (b.onlyUsefulInTowns ? $" up to size {gd.rules.MaximumLevel1CitySize}" : ""));
		if (BuildingSource.GetValue(b) is SaveBuilding source) {
			foreach (var (flag, what) in new[] {
				(SaveBuilding.Flag.IncreasesFoodInWater, "food"), (SaveBuilding.Flag.IncreasesShieldsInWater, "shield"),
				(SaveBuilding.Flag.IncreasesTradeInWater, "commerce") })
				if (source.flags.Contains(flag)) effects.Add($"+1 {what} on water tiles");
			if (source.flags.Contains(SaveBuilding.Flag.VeteranSeaUnits)) effects.Add("veteran sea units");
		}
		if (b.providesVeteranGroundUnits) effects.Add("veteran land units");
		if (b.greatWonderProperties?.buildingGainedInEveryCity is { } every) effects.Add($"a {every.name} in every city");
		if (b.greatWonderProperties?.buildingGainedInEveryCityOnContinent is { } continent)
			effects.Add($"a {continent.name} in every city on the continent");
		if (b.culturePerTurn > 0) effects.Add($"+{b.culturePerTurn} culture");
		if (b.maintenanceCost > 0) effects.Add($"upkeep {b.maintenanceCost}");
		return effects;
	}
}
