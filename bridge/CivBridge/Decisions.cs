using System.Text.Json.Nodes;
using C7GameData;

namespace CivBridge;

static class Source {
	public const string Agent = "agent", Engine = "engine";
}

// Who made each production and research choice. The engine picks a city's first item, the next item
// after every completion, and the next tech after one is learned (unless the agent queued it); those
// picks stay pending (a blocker) until the agent sets its own or accepts them with end_turn(skip_idle).
sealed partial class Session {
	readonly Dictionary<City, string> producingSource = [];
	readonly HashSet<City> pendingProduction = [];
	string researchSource = Source.Engine;
	bool researchPending;
	HashSet<Tech> agentResearch = [];
	readonly Dictionary<string, int> decided = new() {
		["production/agent"] = 0, ["production/engine"] = 0, ["research/agent"] = 0, ["research/engine"] = 0,
	};

	string ProducingSource(City c) => producingSource.GetValueOrDefault(c, Source.Engine);

	void AgentPickedProduction(City c) {
		producingSource[c] = Source.Agent;
		pendingProduction.Remove(c);
	}

	void EnginePickedProduction(City c) {
		producingSource[c] = Source.Engine;
		if (c.itemBeingProduced != null) pendingProduction.Add(c);
	}

	void AgentPickedResearch(IEnumerable<Tech> plan) {
		researchSource = Source.Agent;
		researchPending = false;
		agentResearch = [.. plan];
	}

	void AcceptEnginePicks() {
		pendingProduction.Clear();
		researchPending = false;
	}

	/// <summary>After a tech is learned: the agent's queue continues as the agent's choice, anything else is the engine's.</summary>
	void ResearchMoved() {
		Tech now = gd.GetTech(human.currentlyResearchedTech);
		if (now == null) return;
		bool queued = agentResearch.Contains(now);
		researchSource = queued ? Source.Agent : Source.Engine;
		researchPending = !queued;
	}

	void Decided(string what, string source) => decided[$"{what}/{source}"]++;

	JsonObject Decisions() => new() {
		["production"] = new JsonObject { ["agent"] = decided["production/agent"], ["engine"] = decided["production/engine"] },
		["research"] = new JsonObject { ["agent"] = decided["research/agent"], ["engine"] = decided["research/engine"] },
	};
}
