"""Start, reach and stop an OpenCiv3 env: AgentEnv card, JSON-RPC data plane, REST extensions.

Standard library only, so the harness runs on any Python 3.11+ without the env's dependencies.
"""
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin

CARD_PATH = "/.well-known/agent-env.json"
RPC_PATH = "/agentenv"
AUTOPLAY_URI = "urn:openciv3:autoplay/v1"
DEFAULT_SERVER_CMD = f"{shlex.quote(sys.executable)} -m agentenv_openciv3.server"


class HarnessError(RuntimeError):
    """The harness or the env failed; the run says nothing about the agent."""


def free_ports(n: int = 1) -> list[int]:
    """Distinct free ports, held open together so parallel runs never share one."""
    socks = [socket.socket() for _ in range(n)]
    for s in socks:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in socks]
    for s in socks:
        s.close()
    return ports


def _http(method: str, url: str, body=None, timeout: float = 30) -> tuple[int, object]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, raw.decode(errors="replace")


def kill_group(proc: subprocess.Popen, grace: float = 10) -> None:
    """Stop a process started with start_new_session=True together with its children."""
    if proc.poll() is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(grace)
            return
        except subprocess.TimeoutExpired:
            pass


def score_total(score) -> float | None:
    """A score is a number or a score object {"total", "cities", ...}."""
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        return score
    if isinstance(score, dict):
        return score_total(score.get("total"))
    return None


class Env:
    def __init__(self, base_url: str, proc: subprocess.Popen | None = None, log_path: Path | None = None):
        self.base = base_url.rstrip("/")
        self.proc, self.log_path, self.card = proc, log_path, None

    @classmethod
    def start(cls, server_cmd: str, port: int, env_vars: dict, log_path: Path) -> "Env":
        env = {**os.environ, "MCP_HOST": "127.0.0.1", "MCP_PORT": str(port), **{k: str(v) for k, v in env_vars.items()}}
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(shlex.split(server_cmd), env=env, stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        return cls(f"http://127.0.0.1:{port}", proc, log_path)

    def log_tail(self, lines: int = 30) -> str:
        if not self.log_path or not self.log_path.exists():
            return ""
        return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-lines:])

    def wait_ready(self, timeout: float = 180) -> dict:
        """Ready means the card answers 200 with a non-empty name, as AgentEnv's providers check."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc and self.proc.poll() is not None:
                raise HarnessError(f"env exited ({self.proc.returncode}) before serving its card:\n{self.log_tail()}")
            try:
                status, card = _http("GET", self.base + CARD_PATH, timeout=5)
                if status == 200 and isinstance(card, dict) and card.get("name"):
                    self.card = card
                    return card
            except OSError:
                pass
            time.sleep(0.5)
        raise HarnessError(f"no card at {self.base}{CARD_PATH} after {timeout:.0f}s:\n{self.log_tail()}")

    @property
    def mcp_url(self) -> str:
        for iface in (self.card or {}).get("additionalInterfaces") or []:
            if iface.get("transport") == "mcp" and iface.get("url"):
                return urljoin(self.base + "/", iface["url"])
        return self.base + "/mcp"

    def rpc(self, method: str, params: dict | None = None, timeout: float = 120):
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        try:
            status, reply = _http("POST", self.base + RPC_PATH, body, timeout)
        except OSError as e:
            raise HarnessError(f"{method}: {e}") from e
        if status != 200 or not isinstance(reply, dict) or "result" not in reply:
            raise HarnessError(f"{method} failed ({status}): {reply}")
        return reply["result"]

    def summary(self, timeout: float = 60) -> dict:
        """The env's data/get summary: the first DataPart."""
        parts = self.rpc("data/get", timeout=timeout).get("parts") or []
        data = next((p["data"] for p in parts if p.get("kind") == "data"), None)
        if not isinstance(data, dict):
            raise HarnessError(f"data/get returned no DataPart: {parts}")
        return data

    def new_game(self, scenario: dict | None = None, timeout: float = 300) -> None:
        """data/reset replays the env's configured scenario; data/add with a scenario replaces it first."""
        if scenario:
            self.rpc("data/add", {"parts": [{"kind": "data", "data": {"scenario": scenario}}]}, timeout)
        else:
            self.rpc("data/reset", timeout=timeout)

    def extension(self, uri: str, timeout: float = 900, **args):
        """POST to the REST endpoint the card advertises for `uri`."""
        exts = ((self.card or {}).get("capabilities") or {}).get("extensions") or []
        ext = next((e for e in exts if e.get("uri") == uri), None)
        endpoint = ((ext or {}).get("params") or {}).get("endpoint")
        if not endpoint:
            raise HarnessError(f"the env's card does not advertise {uri}")
        try:
            status, reply = _http("POST", urljoin(self.base + "/", endpoint), args, timeout)
        except OSError as e:
            raise HarnessError(f"{uri}: {e}") from e
        if status != 200:
            raise HarnessError(f"{uri} failed ({status}): {reply}")
        return reply

    def stop(self) -> None:
        if self.proc:
            kill_group(self.proc)


def clean_claude_project(cwd: Path) -> list[Path]:
    """Remove ~/.claude/projects/<slug> that `claude -p` creates for its cwd, even without persistence."""
    root = Path.home() / ".claude" / "projects"
    removed = []
    for path in {str(cwd), os.path.realpath(cwd)}:
        slug = re.sub(r"[^A-Za-z0-9]", "-", path)
        # Claude truncates long slugs to 200 characters and appends a hash.
        candidates = [root / slug] if len(slug) <= 200 else [p for p in root.glob("*") if p.name.startswith(slug[:200])]
        for d in candidates:
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
                removed.append(d)
    return removed
