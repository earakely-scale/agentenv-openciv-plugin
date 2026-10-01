using System.Text.Json.Nodes;

namespace CivBridge;

/// <summary>A failure the client can act on: the protocol's {code, message, alternatives, suggest}.</summary>
sealed class BridgeError(string code, string message, IEnumerable<JsonNode> alternatives = null, string suggest = null)
	: Exception(message) {
	public string Code { get; } = code;
	readonly JsonNode[] alternatives = alternatives?.ToArray();

	public JsonObject ToJson() {
		var o = new JsonObject { ["code"] = Code, ["message"] = Message };
		if (alternatives != null) o["alternatives"] = new JsonArray(alternatives.Select(a => a?.DeepClone()).ToArray());
		if (suggest != null) o["suggest"] = suggest;
		return o;
	}

	public static IEnumerable<JsonNode> Names(IEnumerable<string> names) => names.Select(n => (JsonNode)n);
}

sealed class Args(JsonObject o) {
	JsonNode Get(string name) => o?[name];

	public bool Has(string name) => Get(name) is not null;

	public int Int(string name, int? fallback = null, int min = int.MinValue, int max = int.MaxValue) {
		JsonNode n = Get(name);
		if (n is null) return fallback ?? throw Missing(name);
		if (n is not JsonValue v || !v.TryGetValue(out int i)) throw new BridgeError("bad_args", $"'{name}' must be an integer.");
		if (i < min || i > max) throw new BridgeError("bad_args", $"'{name}' must be between {min} and {max}; got {i}.");
		return i;
	}

	public string Str(string name, string fallback = null) {
		JsonNode n = Get(name);
		if (n is null) return fallback ?? throw Missing(name);
		if (n is JsonValue v && v.TryGetValue(out string s)) return s;
		// Accept bare numbers and booleans for string-valued choices such as ocean=70.
		return n.ToJsonString();
	}

	public bool Bool(string name, bool fallback) {
		JsonNode n = Get(name);
		if (n is null) return fallback;
		if (n is JsonValue v && v.TryGetValue(out bool b)) return b;
		throw new BridgeError("bad_args", $"'{name}' must be true or false.");
	}

	static BridgeError Missing(string name) => new("bad_args", $"Missing required argument '{name}'.");
}

static class Json {
	public static JsonObject Ok(JsonNode id, JsonNode result) => new() { ["id"] = id?.DeepClone(), ["ok"] = true, ["result"] = result };

	public static JsonObject Error(JsonNode id, BridgeError e) => new() { ["id"] = id?.DeepClone(), ["ok"] = false, ["error"] = e.ToJson() };

	public static JsonArray Array<T>(IEnumerable<T> items, Func<T, JsonNode> f) => new(items.Select(f).ToArray());

	public static JsonArray Strings(IEnumerable<string> items) => Array(items, s => (JsonNode)s);

	/// <summary>Engine "unknown" sentinels (int.MaxValue / int.MinValue) become null.</summary>
	public static JsonNode Turns(int turns) => turns is int.MaxValue or int.MinValue or < 0 ? null : (JsonNode)turns;
}
