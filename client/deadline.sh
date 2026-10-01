# Sourced by prepare.sh and capture.sh. macOS has no timeout(1), and a Godot window never exits on its own.

tree_pids() {
	local c
	echo "$1"
	for c in $(pgrep -P "$1" 2>/dev/null); do tree_pids "$c"; done
}

# run_with_deadline <seconds> <log file> <command...>: run the command with its output in the log; kill it
# and everything it started when the deadline passes (exit status 124).
run_with_deadline() {
	local secs=$1 log=$2 pid start pids
	shift 2
	"$@" > "$log" 2>&1 &
	pid=$!
	start=$SECONDS
	trap 'kill -KILL $(tree_pids '"$pid"') 2>/dev/null; exit 130' INT TERM
	while kill -0 "$pid" 2>/dev/null; do
		if (( SECONDS - start >= secs )); then
			echo "deadline of ${secs}s passed; killing: $*" >&2
			pids=$(tree_pids "$pid")
			kill -TERM $pids 2>/dev/null
			sleep 3
			kill -KILL $pids 2>/dev/null
			wait "$pid" 2>/dev/null
			trap - INT TERM
			return 124
		fi
		sleep 1
	done
	trap - INT TERM
	wait "$pid"
}
