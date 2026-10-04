using System.Text.Json.Nodes;
using C7GameData;

namespace CivBridge;

// Moves as the engine makes them (docs/protocol.md, known_map "moves"), so the play page and the viewer can show units
// walking where they went instead of jumping there. Animations are off, so the engine sends no animation messages;
// patches/0012 has MapUnit.Move tell an IMoveObserver of every step. Steps and battles take their numbers from one
// sequence (`seq`), so a page plays them in the order they happened. Nothing here draws from GameData.rng or changes
// game state.
sealed partial class Session : IMoveObserver {
	/// <summary>One step of one unit, and the seats that saw it.</summary>
	sealed class Step {
		public int Seq, Turn;
		public MapUnit Unit;
		public Player Owner;
		public string Type;
		public int FromX, FromY, ToX, ToY;
		public HashSet<Player> Seen = new(ReferenceEqualityComparer.Instance);
	}

	/// <summary>The steps of this turn and the last, oldest first.</summary>
	readonly List<Step> steps = [];

	/// <summary>The sequence steps and battles share: each takes the next number as it begins.</summary>
	int eventSeq;

	/// <summary>eventSeq when the last snapshot was written (the next one holds what comes after), and when the one before it
	/// was: the `world` command holds what came after that, so it matches the last snapshot until something else
	/// happens.</summary>
	int recordedSeq, shownSeq;

	public void UnitMoved(MapUnit unit, Tile from, Tile to) {
		if (gd == null) return;
		var s = new Step {
			Seq = ++eventSeq, Turn = gd.turn, Unit = unit, Owner = unit.owner, Type = unit.unitType.name,
			FromX = from.XCoordinate, FromY = from.YCoordinate, ToX = to.XCoordinate, ToY = to.YCoordinate,
		};
		// Who could see it: the unit's own seat, and the seats that see the tile it left or the one it entered.
		foreach (Seat seat in seats) {
			Player p = seat.Player;
			if (unit.owner == p || p.tileKnowledge.isActiveTile(from) || p.tileKnowledge.isActiveTile(to)) s.Seen.Add(p);
		}
		if (steps.Count > 0 && steps[0].Turn < gd.turn - 1) steps.RemoveAll(x => x.Turn < gd.turn - 1);
		steps.Add(s);
	}

	/// <summary>
	/// Steps as paths: a unit's run of steps with nothing else in between (no other step or battle), in one turn and with
	/// the same `key` (who saw them), each as {seq of its first step, the steps, the key}. A run never spans two turns, so
	/// an entry stays the same while known_map lists it: the turn that goes takes whole runs with it.
	/// </summary>
	static List<(int Seq, List<Step> Steps, TKey Key)> Paths<TKey>(IEnumerable<Step> list, Func<Step, TKey> key) {
		var paths = new List<(int, List<Step>, TKey)>();
		List<Step> run = null;
		TKey runKey = default;
		foreach (Step s in list) {
			TKey k = key(s);
			Step last = run?[^1];
			if (last != null && s.Unit == last.Unit && s.Seq == last.Seq + 1 && s.Turn == last.Turn && s.FromX == last.ToX
				&& s.FromY == last.ToY && EqualityComparer<TKey>.Default.Equals(k, runKey)) {
				run.Add(s);
				continue;
			}
			run = [s];
			runKey = k;
			paths.Add((s.Seq, run, k));
		}
		return paths;
	}

	static JsonArray PathJson(List<Step> run) {
		var path = new JsonArray(new JsonArray(run[0].FromX, run[0].FromY));
		foreach (Step s in run) path.Add(new JsonArray(s.ToX, s.ToY));
		return path;
	}

	/// <summary>The moves the active seat saw, this turn and the last, as known_map reports them.</summary>
	JsonArray MovesJson() {
		var index = PlayerIndex();
		steps.RemoveAll(x => x.Turn < gd.turn - 1);
		return Json.Array(Paths(steps.Where(s => s.Seen.Contains(human)), _ => true), p => {
			Step first = p.Steps[0];
			return new JsonObject {
				["seq"] = p.Seq,
				["turn"] = first.Turn,
				["owner"] = index.GetValueOrDefault(first.Owner, -1),
				["type"] = first.Type,
				// The seat's own unit's id, while it lives; another civ's units have none for the seat.
				["id"] = first.Owner == human && Alive(first.Unit) ? ids.Of(first.Unit) : null,
				["path"] = PathJson(p.Steps),
			};
		});
	}

	/// <summary>Bit k for each seat k that saw it (as a tile's `known` in the world snapshot).</summary>
	int SeatMask(HashSet<Player> seen) {
		int mask = 0;
		for (int k = 0; k < seats.Count; k++)
			if (seen.Contains(seats[k].Player)) mask |= 1 << k;
		return mask;
	}

	/// <summary>Every move after `since` (in the sequence), whoever saw it (docs/recording.md).</summary>
	JsonArray SnapshotMoves(int since) {
		var index = PlayerIndex();
		return Json.Array(Paths(steps.Where(s => s.Seq > since), s => SeatMask(s.Seen)), p => {
			Step first = p.Steps[0];
			return new JsonObject {
				["seq"] = p.Seq,
				["unit"] = first.Unit.id?.ToString(),
				["owner"] = index.GetValueOrDefault(first.Owner, -1),
				["type"] = first.Type,
				["path"] = PathJson(p.Steps),
				["seen"] = p.Key,
			};
		});
	}

	Dictionary<Player, int> PlayerIndex() {
		var index = new Dictionary<Player, int>(ReferenceEqualityComparer.Instance);
		for (int i = 0; i < gd.players.Count; i++) index[gd.players[i]] = i;
		return index;
	}
}
