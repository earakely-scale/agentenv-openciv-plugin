using System.Collections.Concurrent;
using System.Globalization;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;
using C7Engine;
using CivBridge;
using Serilog;
using Serilog.Events;

// stdout carries protocol lines only: keep the real stream, then point Console (the engine and Lua
// print with it) and Serilog at stderr.
var output = new Output(new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false)) { AutoFlush = true, NewLine = "\n" });
Console.SetOut(Console.Error);
Log.Logger = new LoggerConfiguration().MinimumLevel.Warning()
	.WriteTo.Console(standardErrorFromLevel: LogEventLevel.Verbose).CreateLogger();

const string Usage = "usage: CivBridge [--lua-dir <dir>] [--timeout <seconds per turn>] [--autosave <dir>] [--record <dir>] [--saves <dir>]";
string luaDir = Path.Combine(AppContext.BaseDirectory, "Lua"), autosaveDir = null, recordDir = null, savesDir = null;
double timeout = 60;
for (int i = 0; i < args.Length; i++) {
	string value = i + 1 < args.Length ? args[i + 1] : null;
	switch (args[i]) {
		case "--lua-dir" when value != null: luaDir = args[++i]; break;
		case "--autosave" when value != null: autosaveDir = args[++i]; break;
		case "--record" when value != null: recordDir = args[++i]; break;
		case "--saves" when value != null: savesDir = args[++i]; break;
		case "--timeout" when double.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out timeout) && timeout > 0: i++; break;
		default: return Fail(2, "bad_args", $"Unknown or incomplete argument '{args[i]}'. {Usage}");
	}
}

if (!File.Exists(Path.Combine(luaDir, "civ3", "ruleset.json")))
	return Fail(1, "no_lua", $"No OpenCiv3 game modes at {luaDir}; pass --lua-dir <dir with civ3/ and standalone/>.");
// Without --autosave the saves go to a fresh temp directory, removed again when the bridge exits normally.
bool ownAutosaveDir = autosaveDir == null;
try {
	autosaveDir = ownAutosaveDir ? Directory.CreateTempSubdirectory("civbridge-").FullName : Directory.CreateDirectory(autosaveDir).FullName;
	if (recordDir != null) recordDir = Directory.CreateDirectory(recordDir).FullName;
	if (savesDir != null) savesDir = Directory.CreateDirectory(savesDir).FullName;
} catch (Exception e) when (e is IOException or UnauthorizedAccessException or ArgumentException) {
	return Fail(2, "bad_args", $"Cannot create the --autosave, --record or --saves directory: {e.Message}");
}

var watchdog = new Watchdog(output, TimeSpan.FromSeconds(timeout));
var input = new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false));
int status = EngineContext.Run(async () => {
	// Animation waits never complete without a UI, so switch them off before anything runs.
	new MsgSetAnimationsEnabled(false).send();
	EngineStorage.ProcessNextMessageToEngine();

	var session = new Session(luaDir, watchdog, autosaveDir, recordDir, savesDir);
	output.Write(Json.Ok(0, new JsonObject { ["ready"] = true, ["version"] = Session.Version, ["autosave"] = session.AutosavePath }));
	while (await Task.Run(input.ReadLine) is string line) {
		if (line.Trim().Length > 0) output.Write(await Handle(session, line));
	}
	return 0;
});
if (ownAutosaveDir) {
	try { Directory.Delete(autosaveDir, recursive: true); } catch (IOException) { /* best effort */ }
}
return status;

// Startup failures answer the ready line (id 0) with an error, then exit.
int Fail(int exitCode, string code, string message) {
	output.Write(Json.Error(0, new BridgeError(code, message)));
	return exitCode;
}

async Task<JsonObject> Handle(Session session, string line) {
	JsonNode id = null;
	try {
		if (JsonNode.Parse(line) is not JsonObject request) throw new BridgeError("bad_request", "Each request must be a JSON object.");
		id = request["id"];
		string cmd = request["cmd"] is JsonValue c && c.TryGetValue(out string s) ? s : throw new BridgeError("bad_request", "The request has no \"cmd\".");
		watchdog.Arm(id, cmd);
		JsonNode result = await session.Run(cmd, new Args(request["args"] as JsonObject));
		watchdog.Disarm();
		return Json.Ok(id, result);
	} catch (Exception e) {
		watchdog.Disarm();
		BridgeError error = e switch {
			BridgeError b => b,
			JsonException => new BridgeError("bad_request", $"The request is not valid JSON: {e.Message}"),
			_ => new BridgeError("engine_error", $"The engine failed: {e.GetType().Name}: {e.Message}"),
		};
		if (e is not BridgeError) Log.Error(e, "command failed");
		return Json.Error(id, error);
	}
}

namespace CivBridge {
	/// <summary>The protocol stream. Once the watchdog has written its final line nothing else goes out.</summary>
	sealed class Output(StreamWriter writer) {
		// The client parses JSON, never HTML, so keep names like "Tenochtitlán" readable.
		static readonly JsonSerializerOptions Options = new() { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };
		readonly object gate = new();
		bool closed;

		public void Write(JsonObject line, bool last = false) {
			lock (gate) {
				if (closed) return;
				writer.WriteLine(line.ToJsonString(Options));
				closed = last;
			}
		}
	}

	/// <summary>
	/// Ends the process when a command makes no progress for longer than the budget (an engine hang
	/// would otherwise block the client forever). Long commands re-arm it after every turn.
	/// </summary>
	sealed class Watchdog {
		readonly Output output;
		readonly long budgetMs;
		readonly object gate = new();
		long deadline = long.MaxValue;
		JsonNode id;
		string cmd;

		public Watchdog(Output output, TimeSpan budget) {
			this.output = output;
			budgetMs = (long)budget.TotalMilliseconds;
			new Thread(Watch) { IsBackground = true, Name = "watchdog" }.Start();
		}

		public void Arm(JsonNode id, string cmd) {
			lock (gate) (this.id, this.cmd, deadline) = (id?.DeepClone(), cmd, Environment.TickCount64 + budgetMs);
		}

		public void Kick() {
			lock (gate) if (deadline != long.MaxValue) deadline = Environment.TickCount64 + budgetMs;
		}

		public void Disarm() {
			lock (gate) deadline = long.MaxValue;
		}

		void Watch() {
			while (true) {
				Thread.Sleep(20);
				lock (gate) {
					if (Environment.TickCount64 < deadline) continue;
					output.Write(Json.Error(id, new BridgeError("timeout",
						$"'{cmd}' made no progress for {budgetMs / 1000.0:0.###} s, so the bridge stopped. Start a new bridge process and a new game.")), last: true);
				}
				Environment.Exit(3);
			}
		}
	}

	/// <summary>
	/// Single-threaded context for the engine thread. The engine has no locks, and continuations after
	/// AI diplomacy hop through the thread pool; this brings them back here.
	/// </summary>
	sealed class EngineContext : SynchronizationContext {
		readonly BlockingCollection<(SendOrPostCallback, object)> queue = new();

		public override void Post(SendOrPostCallback d, object state) => queue.Add((d, state));

		public override void Send(SendOrPostCallback d, object state) => throw new NotSupportedException();

		public override SynchronizationContext CreateCopy() => this;

		public static int Run(Func<Task<int>> main) {
			var context = new EngineContext();
			SetSynchronizationContext(context);
			Task<int> task = main();
			task.ContinueWith(_ => context.queue.CompleteAdding(), TaskScheduler.Default);
			foreach (var (callback, state) in context.queue.GetConsumingEnumerable()) {
				// Exceptions from the engine's async-void handlers land here; log them instead of dying.
				try { callback(state); } catch (Exception e) { Log.Error(e, "engine callback failed"); }
			}
			return task.GetAwaiter().GetResult();
		}
	}
}
