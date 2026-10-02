#!/usr/bin/env bash
# Install the OpenCiv3 plugin for AgentEnv, register its env, and play the smoke game.
#
#   curl -fsSL https://raw.githubusercontent.com/earakely-scale/agentenv-openciv-plugin/main/scripts/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --agent        # pass options after `bash -s --`
#   scripts/install.sh [options]                          # from a checkout
#
# Needs git, Docker (running) and uv. Options:
#   --dir DIR        where to clone (default ./agentenv-openciv-plugin; ignored when run from a checkout)
#   --agent          also build and register the player agents (openciv3-claude, openciv3-codex, openciv3-gemini)
#                    and configure a model for them; openciv3-claude is the default agent
#   --base-url URL   the agent's model endpoint (default https://api.anthropic.com; a LiteLLM URL works too)
#   --model NAME     the model the agent runs on (default: the agent's own, sonnet; a LiteLLM proxy may need its name,
#                    e.g. anthropic/claude-sonnet-5-5)
#   --client         also install the real OpenCiv3 client, for recordings of the real game's view. Its art carries
#                    no licence, so don't push that image to a public registry.
#   --no-smoke       skip the smoke game at the end
#
# With --agent the model key (an Anthropic API key, a `claude setup-token` token or a LiteLLM key) comes from
# $OPENCIV3_MODEL_KEY or a prompt that does not echo it, and is stored in ~/.config/agentenv/secrets.yaml (mode 600).
# The checkout's .agentenv/config.toml (git-ignored) refers to it, so run agent-env from inside the checkout.
set -euo pipefail

REPO=https://github.com/earakely-scale/agentenv-openciv-plugin
SECRETS=$HOME/.config/agentenv/secrets.yaml
dir=agentenv-openciv-plugin agent=0 client=0 smoke=1 base_url=https://api.anthropic.com model=

while [ $# -gt 0 ]; do
	case $1 in
		--dir) dir=$2; shift ;;
		--agent) agent=1 ;;
		--base-url) base_url=$2; shift ;;
		--model) model=$2; shift ;;
		--client) client=1 ;;
		--no-smoke) smoke=0 ;;
		-h | --help) sed -n '2,21p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null || echo "see $REPO/blob/main/scripts/install.sh"; exit 0 ;;
		*) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
	esac
	shift
done

say() { printf '\n==> %s\n' "$*"; }
need() { command -v "$1" >/dev/null || { echo "$1 is required: $2" >&2; exit 1; }; }

need git "https://git-scm.com"
need docker "https://docs.docker.com/get-docker/ (Docker Desktop, Rancher Desktop or OrbStack on a Mac)"
need uv "curl -LsSf https://astral.sh/uv/install.sh | sh"
docker info >/dev/null 2>&1 || { echo "Docker is installed but not running; start it and run this again." >&2; exit 1; }
docker buildx version >/dev/null 2>&1 || {
	echo "Docker's buildx plugin is required for the image builds. Docker Desktop has it; on Debian or Ubuntu:" >&2
	echo "  sudo apt-get install docker-buildx" >&2
	exit 1
}

# A checkout when run as scripts/install.sh from one, else clone (or update) one.
here=$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || true)
if [ -n "$here" ] && [ -f "$here/../Dockerfile" ] && [ -d "$here/../bridge" ]; then
	root=$(cd "$here/.." && pwd)
	say "Using the checkout at $root"
elif [ -d "$dir/.git" ]; then
	root=$(cd "$dir" && pwd)
	say "Updating $root"
	git -C "$root" pull --ff-only
else
	say "Cloning $REPO into $dir"
	git clone "$REPO" "$dir"
	root=$(cd "$dir" && pwd)
fi
# Only the engine: its art submodule is not needed (and the client target fetches the art it uses).
git -C "$root" submodule update --init vendor/OpenCiv3

say "Installing agent-env with the plugin (uv tool)"
uv tool install --force agentenv-framework --with-editable "$root"
bin=$(uv tool dir --bin)
agent_env=$bin/agent-env
case ":$PATH:" in *":$bin:"*) ;; *) echo "Note: $bin is not on your PATH; run \`uv tool update-shell\` once." ;; esac

if [ "$agent" = 1 ]; then
	mkdir -p "$root/.agentenv" "$(dirname "$SECRETS")"
	if ! grep -q '^OPENCIV3_MODEL_KEY:' "$SECRETS" 2>/dev/null; then
		key=${OPENCIV3_MODEL_KEY:-}
		if [ -z "$key" ]; then
			[ -r /dev/tty ] || { echo "--agent needs a model key: set OPENCIV3_MODEL_KEY." >&2; exit 1; }
			printf 'Model key for the agent (Anthropic API key, claude setup-token token, or LiteLLM key): ' >/dev/tty
			IFS= read -rs key </dev/tty
			echo >/dev/tty
		fi
		[ -n "$key" ] || { echo "No key given." >&2; exit 1; }
		(umask 077 && printf 'OPENCIV3_MODEL_KEY: %s\n' "$key" >>"$SECRETS")
		chmod 600 "$SECRETS"
		echo "Stored the key in $SECRETS"
	fi
	config=$root/.agentenv/config.toml
	if [ -f "$config" ]; then
		echo "Keeping the existing $config"
	else
		{
			echo '# Local and git-ignored: the default agent is the Claude Code player, the model key a secret reference.'
			echo '[agents]'
			echo 'default_a2a_agent_id = "openciv3-claude"'
			echo
			echo '[model]'
			echo "base_url = \"$base_url\""
			[ -n "$model" ] && echo "default = \"$model\""
			echo 'api_key = "secret:OPENCIV3_MODEL_KEY"'
			echo
			echo '[stores.secret]'
			echo 'impl = "agent_env.store.secret_store:LocalSecretStore"'
			echo
			echo '[stores.secret.config]'
			echo "file_path = \"$SECRETS\""
		} >"$config"
		echo "Wrote $config"
	fi
fi

say "Building and registering the env (first build: a few minutes)"
setup=()
[ "$client" = 1 ] && setup+=(--client)
[ "$agent" = 1 ] && setup+=(--agent)
(cd "$root" && "$agent_env" openciv3 setup ${setup[@]+"${setup[@]}"})

if [ "$smoke" = 1 ]; then
	say "Playing the smoke game (the scripted bot, 30 turns, graded and recorded)"
	(cd "$root" && "$agent_env" run openciv3 --task smoke)
fi

say "Done"
echo "From $root:"
echo "  agent-env run openciv3 --task smoke                 # the scripted check"
if [ "$agent" = 1 ]; then
	echo "  agent-env run openciv3 --task play                  # Claude plays 50 turns"
	echo "  agent-env run openciv3 --task full-game             # the full 540-turn game (an hour or so)"
else
	echo "  scripts/install.sh --agent                          # add the player agents, to have models play"
fi
echo "  agent-env openciv3 recordings --out recordings      # copy the games' videos and replays here"
