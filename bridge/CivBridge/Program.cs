using System.Diagnostics;
using C7Engine;
using C7Engine.Lua;
using C7GameData;
using C7GameData.Save;

string modesDir = args.Length > 0 ? args[0] : "../../vendor/OpenCiv3/C7/Lua/";
int seed = args.Length > 1 ? int.Parse(args[1]) : 12345;
int turns = args.Length > 2 ? int.Parse(args[2]) : 50;
const int civs = 8;

var sw = Stopwatch.StartNew();
SaveGame save = GameMode.Load(modesDir, new GameMode.Config("civ3", ["standalone"])).GetSave();
WorldSize worldSize = new() { width = 80, height = 80, numberOfCivs = civs, distanceBetweenCivs = 12, techRate = 240, optimalNumberOfCities = 20 };
WorldCharacteristics wc = new(save) {
	landform = WorldCharacteristics.Landform.Pangaea,
	oceanCoverage = WorldCharacteristics.OceanCoverage.Percent_70,
	age = WorldCharacteristics.Age.Billion_4,
	climate = WorldCharacteristics.Climate.Normal,
	temperature = WorldCharacteristics.Temperature.Temperate,
	barbarianActivity = BarbarianActivity.Roaming,
	worldSize = worldSize,
	mapSeed = seed,
};
new GameSetup {
	playerCivilization = save.Civilizations.Find(c => !c.isBarbarian),
	difficulty = save.Difficulties.First(),
	worldCharacteristics = wc,
	opponents = Enumerable.Repeat(new SelectedOpponent { isRandom = true }, civs - 1).ToList(),
}.Populate(save);
BehaviorEngine behaviors = GameMode.Load(modesDir, new GameMode.Config("civ3")).behaviors;
Player human = await CreateGame.createGame(save, _ => behaviors);
new MsgSetAnimationsEnabled(false).process();
await TurnHandling.AdvanceTurn();
Console.WriteLine($"setup {sw.ElapsedMilliseconds} ms, seed {seed}, human = {human.civilization.name}, turn {EngineStorage.gameData.turn}");

void Report() {
	GameData gd = EngineStorage.gameData;
	var rows = gd.players.Where(p => !p.isBarbarians).Select(p =>
		$"{p.civilization.name}{(p.isHuman ? "*" : "")}: cities {gd.cities.Count(c => c.owner == p)}, units {gd.mapUnits.Count(u => u.owner == p)}, techs {p.knownTechs.Count}, gold {p.gold}");
	Console.WriteLine($"turn {gd.turn}: " + string.Join(" | ", rows));
}

sw.Restart();
for (int t = 0; t < turns; t++) {
	TurnHandling.OnEndTurn(human);
	human.hasPlayedThisTurn = true;
	await TurnHandling.AdvanceTurn();
	while (EngineStorage.TryDequeueNextMessageToUI(out _)) { }
	if ((t + 1) % 10 == 0) Report();
}
Console.WriteLine($"{turns} turns in {sw.ElapsedMilliseconds} ms");
