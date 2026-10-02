using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using C7Engine;
using C7Engine.Lua;
using C7GameData;
using C7GameData.Save;
using Serilog;

namespace CivBridge;

/// <summary>One game, driven by protocol commands. Everything here runs on the engine thread.</summary>
sealed partial class Session(string luaDir, Watchdog watchdog, string autosaveDir, string recordDir, string savesDir) {
	public const string Version = "0.2.0";
	public const int MaxTurnLimit = 1000;

	static readonly string[] Commands = [
		"new_game", "load", "state", "map", "city_sites", "unit_order", "city", "set_production", "set_rates", "hurry",
		"techs", "set_research", "end_turn", "autoplay", "score", "world", "revolution", "diplomacy", "declare_war", "propose_peace",
	];
	static readonly string[] Policies = ["null", "found_capital", "settler_bot", "engine_ai"];

	/// <summary>Events that end an end_turn(until_attention) run even when no blocker appears.</summary>
	static readonly HashSet<string> AttentionEvents =
		["disorder_started", "riot_risk", "unit_lost", "city_destroyed", "gold_stolen", "war_declared", "defenseless", "threat",
		 "peace_offered", "government_picked"];

	GameMode mode;
	GameData gd;
	int seed, turnLimit;
	int eventTurn;

	/// <summary>UI messages that become turn events, with the seat whose command raised them (null: the turn's advance).</summary>
	readonly List<(MessageToUI Message, Seat Actor)> uiMessages = [];
	bool advancing;

	bool GameOver => human.defeated || gd.turn >= turnLimit || victory != null;

	public async Task<JsonNode> Run(string cmd, Args a) {
		if (cmd == "new_game") return NewGame(a);
		if (cmd == "load") return Load(a);
		if (!Commands.Contains(cmd))
			throw new BridgeError("unknown_command", $"'{cmd}' is not a CivBridge command.", BridgeError.Names(Commands));
		if (gd == null) throw new BridgeError("no_game", "No game is running yet; send new_game (or load) first.");
		seat = SeatArg(a);
		ids.Sync(gd, human);
		PruneOrders();
		return cmd switch {
			"state" => State(),
			"map" => Map(a),
			"city_sites" => CitySites(a),
			"unit_order" => await UnitOrder(a),
			"city" => CityInfo(a),
			"set_production" => SetProduction(a),
			"set_rates" => SetRates(a),
			"hurry" => Hurry(a),
			"techs" => Techs(),
			"set_research" => SetResearch(a),
			"end_turn" => await EndTurn(a),
			"autoplay" => await Autoplay(a),
			"world" => World(),
			"revolution" => Revolution(a),
			"diplomacy" => Diplomacy(),
			"declare_war" => DeclareWar(a),
			"propose_peace" => ProposePeace(a),
			_ => ScoreAll(),
		};
	}

	void EnsureFresh() {
		if (gd != null)
			throw new BridgeError("already_started", $"A game (seed {seed}) is already running; a bridge plays one game. Restart the bridge to start another.");
	}

	JsonObject NewGame(Args a) {
		EnsureFresh();
		int seedArg = a.Int("seed", min: 0);
		mode ??= GameMode.Load(luaDir, new GameMode.Config("civ3", ["standalone"]));
		SaveGame save = mode.GetSave();
		Civilization civ = Pick(save.Civilizations.Where(c => !c.isBarbarian), c => c.name, a.Str("civ", "Rome"), "civ");
		int opponents = a.Int("opponents", 3, 1, 11);
		List<string> extra = SeatCivs(a, civ.name, opponents, save.Civilizations.Where(c => !c.isBarbarian).Select(c => c.name));
		JsonObject labels = a.Object("labels");
		WorldSize size = Pick(save.WorldSizes, w => w.name, a.Str("size", "Tiny"), "size");
		Difficulty difficulty = Pick(save.Difficulties, d => d.Name, a.Str("difficulty", "Regent"), "difficulty");
		var barbarians = Pick(Enum.GetValues<BarbarianActivity>(), b => b.ToString(), a.Str("barbarians", "Sedentary"), "barbarians");
		var landform = Pick(Enum.GetValues<WorldCharacteristics.Landform>(), l => l.ToString(), a.Str("landform", "Pangaea"), "landform");
		var ocean = Pick(Enum.GetValues<WorldCharacteristics.OceanCoverage>(), o => ((int)o).ToString(), a.Int("ocean", 70).ToString(), "ocean");
		int limit = a.Int("turn_limit", 60, 1, MaxTurnLimit);

		var world = new WorldSize {
			name = size.name, width = size.width, height = size.height, numberOfCivs = opponents + 1,
			distanceBetweenCivs = size.distanceBetweenCivs, techRate = size.techRate, optimalNumberOfCities = size.optimalNumberOfCities,
		};
		var setup = new GameSetup {
			playerCivilization = civ,
			difficulty = difficulty,
			worldCharacteristics = new WorldCharacteristics(save) {
				landform = landform, oceanCoverage = ocean, barbarianActivity = barbarians, worldSize = world, mapSeed = seedArg,
				age = WorldCharacteristics.Age.Billion_4, climate = WorldCharacteristics.Climate.Normal,
				temperature = WorldCharacteristics.Temperature.Temperate,
			},
			opponents = [
				.. extra.Select(name => new SelectedOpponent { Name = name }),
				.. Enumerable.Repeat(new SelectedOpponent { isRandom = true }, opponents - extra.Count),
			],
		};
		try {
			setup.Populate(save);
		} catch (ArgumentOutOfRangeException) {
			throw new BridgeError("bad_args",
				$"The {size.name} {landform} map has room for {save.Map.startingLocations.Count} civilizations, not {opponents + 1}; use fewer opponents or a larger size.");
		}

		Player player = CreateGame.createGame(save, _ => mode.behaviors).GetAwaiter().GetResult();
		(gd, seed, turnLimit) = (EngineStorage.gameData, seedArg, limit);
		SeatGame(player, extra, labels);
		foreach (Player p in gd.players) TurnHandling.InitTurnData(p, p.SitsOutFirstTurn());
		TurnHandling.OnBeginTurn();
		DrainUi();
		EachSeat(_ => ids.Sync(gd, human));
		Autosave();
		Record();
		return GameInfo();
	}

	JsonObject GameInfo() => new() {
		["turn"] = gd.turn,
		["turn_limit"] = turnLimit,
		["seed"] = seed,
		["civ"] = human.civilization.name,
		["opponents"] = Json.Strings(Rivals().Select(p => p.civilization.name)),
		["seats"] = SeatsJson(),
		["map"] = new JsonObject { ["width"] = gd.map.numTilesWide, ["height"] = gd.map.numTilesTall, ["wrap_x"] = gd.map.wrapHorizontally },
	};

	static T Pick<T>(IEnumerable<T> items, Func<T, string> name, string wanted, string arg) {
		foreach (T item in items) if (string.Equals(name(item), wanted.Trim(), StringComparison.OrdinalIgnoreCase)) return item;
		throw new BridgeError("bad_args", $"Unknown {arg} '{wanted}'.", BridgeError.Names(items.Select(name)));
	}

	async Task<JsonNode> EndTurn(Args a) {
		bool skipIdle = a.Bool("skip_idle", false), untilAttention = a.Bool("until_attention", false);
		int maxTurns = Math.Clamp(a.Int("max_turns", 1), 1, 20);
		EnsurePlaying();
		JsonArray blockers = Blockers();
		if (blockers.Count > 0 && !skipIdle) return new JsonObject { ["blocked"] = true, ["blockers"] = blockers };
		if (skipIdle) AcceptEnginePicks();
		if (MultiSeat) return await EndSeatTurn();

		autos = [];
		var events = new List<JsonObject>();
		int advanced = 0;
		bool attention;
		do {
			List<JsonObject> turn = await AdvanceTurn(engineAi: false);
			events.AddRange(turn);
			advanced++;
			attention = turn.Any(e => AttentionEvents.Contains((string)e["kind"]));
		} while (untilAttention && advanced < maxTurns && !GameOver && !attention && Blockers().Count == 0);
		lastEvents = events;

		return new JsonObject {
			["blocked"] = false,
			["turns_advanced"] = advanced,
			["turn"] = gd.turn,
			["game_over"] = GameOver,
			["defeated"] = human.defeated,
			["events"] = Json.Array(events, e => e.DeepClone()),
			["auto"] = Json.Array(autos, e => e.DeepClone()),
		};
	}

	async Task<JsonNode> Autoplay(Args a) {
		int turns = a.Int("turns", min: 1, max: MaxTurnLimit);
		string policy = a.Has("policy") ? a.Str("policy") : "null";
		if (!Policies.Contains(policy)) throw new BridgeError("bad_args", $"Unknown autoplay policy '{policy}'.", BridgeError.Names(Policies));
		if (MultiSeat) throw new BridgeError("multi_seat", "autoplay plays a one-seat game; in this game every seat ends its own turns.");
		bool record = a.Bool("record", false);

		autos = [];
		var trajectory = new JsonArray();
		if (record) trajectory.Add(Point());
		// Unlike end_turn this keeps going after a defeat, so baselines always cover the requested turns.
		for (int i = 0; i < turns && gd.turn < turnLimit; i++) {
			if (!human.defeated) {
				if (policy == "found_capital") await FoundCapitalAndAutomate();
				else if (policy == "settler_bot") await SettlerBotTurn();
			}
			lastEvents = await AdvanceTurn(engineAi: policy == "engine_ai");
			if (record) trajectory.Add(Point());
		}

		var result = new JsonObject {
			["turn"] = gd.turn,
			["game_over"] = GameOver,
			["defeated"] = human.defeated,
			["score"] = ScoreOf(human),
		};
		if (record) result["trajectory"] = trajectory;
		return result;
	}

	JsonObject Point() => new() { ["turn"] = gd.turn, ["score"] = ScoreOf(human) };

	async Task FoundCapitalAndAutomate() {
		if (human.cities.Count == 0) {
			MapUnit settler = HumanUnits().FirstOrDefault(u => u.unitType.isSettler);
			if (settler != null && !orders.ContainsKey(settler)) {
				if (settler.movementPoints.canMove && FoundSite(settler.location) == null) await Found(settler);
				else if (RankSites(settler.location, settler, 1).FirstOrDefault() is (Tile site, _)) {
					try { await Settle(settler, site); } catch (BridgeError) { /* no route yet: try again next turn */ }
				}
			}
		}
		if (human.cities.Count == 0) return;
		foreach (MapUnit u in HumanUnits())
			if (u.unitType.isWorker && !u.isAutomated && u.WorkerJob == null && !orders.ContainsKey(u)) u.Automate();
		DrainUi();
	}

	/// <summary>
	/// Ends the seats' turn and plays everyone else's, then does what the Godot client does when the human's next turn
	/// starts, for each seat. Returns the active seat's events of the ended turn; each seat keeps its own in TurnEvents.
	/// </summary>
	async Task<List<JsonObject>> AdvanceTurn(bool engineAi) {
		eventTurn = gd.turn;
		var before = new Dictionary<Seat, (Snapshot Snap, List<(MapUnit, Terraform, Tile)> Jobs)>();
		await EachSeat(async s => {
			if (human.defeated) return;
			if (engineAi) {
				// The engine AI plays the human seat during the human's own turn only, so human-side rules
				// (costs, support, trade offers) still apply during everyone else's turns.
				orders.Clear();
				ID research = human.currentlyResearchedTech;
				human.isHuman = false;
				try {
					await Pump(PlayerAI.PlayTurn(human, gd));
				} catch (Exception e) {
					// The same containment patches/0004 gives every other AI player.
					Log.Error(e, "the engine AI failed while playing the human seat; its turn ends here");
				} finally {
					human.isHuman = true;
				}
				if (human.currentlyResearchedTech != research) researchSource = Source.Engine;
			} else {
				foreach (MapUnit u in human.units.ToList()) if (NeedsOrders(u)) u.SkipTurn();
			}
			PickResearch();
			before[s] = (Take(), human.units.Where(u => u.WorkerJob != null && !u.isAutomated).Select(u => (u, u.WorkerJob, u.location)).ToList());
		});

		// The engine ends the other seats' turns itself (TurnHandling.PlayPlayerTurns); the UI controller's is the client's.
		Player controller = seats[0].Player;
		TurnHandling.OnEndTurn(controller);
		controller.units.Sort((x, y) => x.IsBusy().CompareTo(y.IsBusy()));
		controller.hasPlayedThisTurn = true;
		advancing = true;
		try {
			await Pump(TurnHandling.AdvanceTurn());
		} finally {
			advancing = false;
		}
		watchdog.Kick();

		var raised = uiMessages.ToList();
		uiMessages.Clear();
		var thefts = Thefts(raised, before.Values.FirstOrDefault().Snap?.Barbarians ?? []);
		await EachSeat(async s => {
			if (!before.TryGetValue(s, out var b)) return;
			var events = new List<JsonObject>();
			foreach (var (u, job, at) in b.Jobs)
				if (Alive(u) && u.WorkerJob == null) events.Add(Event("job_done", $"{Label(u)} finished {job.Name} at {At(at)}.", at));
			PlayerRelationship.CheckForObsoleteDeals(human, gd.players, gd.turn);
			PickGovernment();
			events.AddRange(Diff(b.Snap));
			events.AddRange(UiEvents(raised));
			events.AddRange(s.Incoming);
			s.Incoming = [];
			events.AddRange(thefts.GetValueOrDefault(s, []));
			if (!engineAi && !human.defeated) await RunStandingOrders(events);
			events.AddRange(Watch());
			DrainUi();
			ids.Sync(gd, human);
			PruneOrders();
			turnEvents = events;
			s.Ready = false;
		});
		if (MultiSeat && victory == null && CheckVictory() is Victory won) {
			victory = won;
			EachSeat(s => s.TurnEvents.Add(Event("victory", VictoryText(won))));
		}
		Autosave();
		Record();
		return turnEvents;
	}

	void PruneOrders() {
		foreach (MapUnit gone in orders.Keys.Where(u => !Alive(u)).ToList()) orders.Remove(gone);
	}

	void PickResearch() {
		if (human.cities.Count == 0 || human.currentlyResearchedTech != null) return;
		PlayerAI.MaybePickTechToResearch(human, gd.techs);
		if (gd.GetTech(human.currentlyResearchedTech) is Tech t) {
			researchSource = Source.Engine;
			autos.Add(Auto("research_picked", $"Nothing was being researched, so the engine picked {t.Name}."));
		}
	}

	void EnsurePlaying() {
		if (!GameOver) return;
		string why = victory != null ? $"{SeatName(victory.Seat)} won by {victory.Kind} on turn {victory.Turn}"
			: human.defeated ? "Your civilization has no cities or settlers left, so it is defeated"
			: $"The game reached its turn limit ({turnLimit})";
		throw new BridgeError("game_over", $"{why}; the game is over and no more orders are accepted.");
	}

	/// <summary>Runs an engine task to completion, answering whatever the engine asks of the UI meanwhile.</summary>
	async Task Pump(Task task) {
		while (!task.IsCompleted) {
			DrainUi();
			EngineStorage.ProcessNextMessageToEngine();
			await Task.Yield();
		}
		DrainUi();
		await task;
	}

	static readonly Regex Stolen = new(@"stolen (\d+) gold", RegexOptions.Compiled);

	void DrainUi() {
		while (EngineStorage.TryDequeueNextMessageToUI(out MessageToUI m)) {
			switch (m) {
				case MsgShowTradeOffer o when o.aiWant.partOfPeaceTreaty || o.aiGive.partOfPeaceTreaty:
					// The AI's turn waits on this "screen", so the offer is reported and answered at once; the agent can
					// take it up with propose_peace, at the price the AI asks.
					uiMessages.Add((m, advancing ? null : seat));
					new MsgDiplomacyCompleted().send();
					EngineStorage.ProcessNextMessageToEngine();
					break;
				case MsgShowTradeOffer o:
					// The AI's turn is suspended until the "diplomacy screen" closes: decline right away.
					(SeatOf(o.humanPlayer) ?? seat).Autos.Add(Auto("trade_declined",
						$"Declined {o.aiPlayer.civilization.name}'s offer of {Describe(o.aiGive)} for {Describe(o.aiWant)}: the env declines every trade."));
					new MsgDiplomacyCompleted().send();
					EngineStorage.ProcessNextMessageToEngine();
					break;
				case MsgWarDeclaration or MsgCityDestroyed or MsgCivilizationDestroyed:
				case MsgShowMilitaryAdvisorPopup { happy: false }:
					uiMessages.Add((m, advancing ? null : seat));
					break;
			}
		}
		while (EngineStorage.TryDequeueNextAnimationMessage(out AnimationMessage a)) a.markCompleted();
	}

	/// <summary>The active seat's events from the turn's UI messages; what its own commands raised it already knows.</summary>
	List<JsonObject> UiEvents(List<(MessageToUI Message, Seat Actor)> raised) {
		var events = new List<JsonObject>();
		foreach (var (m, actor) in raised) {
			if (actor == seat) continue;
			switch (m) {
				case MsgShowTradeOffer o when o.humanPlayer == human:
					events.Add(Event("peace_offered", $"{o.aiPlayer.civilization.name} offered peace"
						+ (o.aiWant.gold is > 0 ? $" for {o.aiWant.gold} gold" : "")
						+ $". Accept with diplomacy(action=\"propose_peace\", civ=\"{o.aiPlayer.civilization.name}\")."));
					break;
				case MsgWarDeclaration w when Knows(w.aggressor) || Knows(w.opponent):
					events.Add(Event("war_declared", $"{w.aggressor.civilization.name} declared war on {w.opponent.civilization.name}."));
					break;
				case MsgCityDestroyed d when d.city.owner == human || human.tileKnowledge.isTileKnown(d.city.location):
					events.Add(Event("city_destroyed", $"{d.city.name} ({d.city.owner.civilization.name}) was destroyed.", d.city.location));
					break;
				case MsgCivilizationDestroyed d:
					events.Add(Event("civ_destroyed", $"{d.civilization.name} has been destroyed."));
					break;
			}
		}
		return events;
	}

	bool Knows(Player p) => p == human || human.playerRelationships.ContainsKey(p.id);

	static string Describe(TradeOffer offer) {
		var parts = offer.techs.Select(t => t.Name).ToList();
		if (offer.gold is int gold and > 0) parts.Add($"{gold} gold");
		if (offer.partOfPeaceTreaty) parts.Add("peace");
		return parts.Count == 0 ? "nothing" : string.Join(", ", parts);
	}

	JsonObject ScoreOf(Player p) {
		int cities = p.cities.Count(c => c.residents.Count > 0);
		int pop = p.cities.Sum(c => c.residents.Count);
		int tiles = gd.map.tiles.Count(t => t.OwningPlayer() == p && t.IsCountedForScore());
		int techs = p.knownTechs.Count;
		return new JsonObject {
			["total"] = 10 * cities + 3 * pop + tiles + 4 * techs, ["cities"] = cities, ["pop"] = pop, ["tiles"] = tiles, ["techs"] = techs,
		};
	}

	/// <summary>A player's fractions of the world's land tiles and of its population.</summary>
	(double Land, double Pop) ShareOf(Player p) {
		int land = gd.map.tiles.Count(t => t.IsLand());
		int pop = gd.players.Where(x => !x.isBarbarians).Sum(x => x.cities.Sum(c => c.residents.Count));
		return (land == 0 ? 0 : (double)gd.map.tiles.Count(t => t.IsLand() && t.OwningPlayer() == p) / land,
			pop == 0 ? 0 : (double)p.cities.Sum(c => c.residents.Count) / pop);
	}

	JsonObject ScoreAll() {
		// Civ III's domination victory needs two thirds of each: the world's land, and its population.
		JsonObject Share(Player p) {
			var (land, pop) = ShareOf(p);
			return new JsonObject { ["land"] = Math.Round(land, 4), ["pop"] = Math.Round(pop, 4) };
		}
		return new JsonObject {
			["turn"] = gd.turn,
			["human"] = ScoreOf(human),
			["players"] = Json.Array(gd.players.Where(p => !p.isBarbarians), p => new JsonObject {
				["civ"] = p.civilization.name, ["is_human"] = p == human, ["seat"] = SeatOf(p) is Seat s ? s.Label ?? Owner(p) : null,
				["defeated"] = p.defeated, ["score"] = ScoreOf(p), ["share"] = Share(p),
			}),
			["human_share"] = Share(human),
			["victory"] = VictoryJson(),
		};
	}

	IEnumerable<Player> Rivals() => gd.players.Where(p => !p.isBarbarians && p != human);
}

sealed record Order(string Kind, Tile Target);

/// <summary>Short ids (u1, c1, ...) for the human's units and cities, in order of first sight, never reused.</summary>
sealed class Ids {
	readonly Dictionary<object, string> byObject = new(ReferenceEqualityComparer.Instance);
	readonly Dictionary<string, object> byId = [];
	int units, cities;

	// Scanning after every step (not only when observed) keeps ids independent of which observations were made.
	public void Sync(GameData gd, Player human) {
		foreach (MapUnit u in gd.mapUnits) if (u.owner == human && !byObject.ContainsKey(u)) Add(u, $"u{++units}");
		foreach (City c in gd.cities) if (c.owner == human && !byObject.ContainsKey(c)) Add(c, $"c{++cities}");
	}

	public void Add(object o, string id) => (byObject[o], byId[id]) = (id, o);

	public string Of(object o) => byObject.GetValueOrDefault(o);

	public T Find<T>(string id) where T : class => byId.GetValueOrDefault(id.Trim().ToLowerInvariant()) as T;

	public static int Number(string id) => int.Parse(id.AsSpan(1));

	/// <summary>The last unit and city numbers handed out, so a restored game never reuses an id.</summary>
	public (int Units, int Cities) Counters {
		get => (units, cities);
		set => (units, cities) = value;
	}

	/// <summary>Id pairs for live objects only: what a save can carry over to the restored game.</summary>
	public IEnumerable<(object Object, string Id)> Live(GameData gd, Player human) =>
		gd.mapUnits.Where(u => u.owner == human).Cast<object>().Concat(gd.cities.Where(c => c.owner == human))
			.Where(byObject.ContainsKey).Select(o => (o, byObject[o]));
}
