"""Async client for one CivBridge child process, speaking the JSON-lines protocol in docs/protocol.md."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
from typing import Any

log = logging.getLogger(__name__)

LINE_LIMIT = 64 * 1024 * 1024
READY_TIMEOUT = 60.0
DEFAULT_TIMEOUT = 30.0
TIMEOUTS = {"new_game": 120.0, "end_turn": 300.0, "autoplay": 900.0}


class BridgeError(Exception):
    """An `ok: false` reply, or the bridge process failing; `code` is the protocol error code."""

    def __init__(self, code: str, message: str, alternatives: list | None = None, suggest: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.alternatives = alternatives or []
        self.suggest = suggest


class Bridge:
    """One CivBridge process. One game per process: `new_game` restarts it."""

    def __init__(self, cmd: list[str]):
        self.cmd = cmd
        self.version: str | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=20)
        self._lock = asyncio.Lock()
        self._seq = 0

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        await self.close()
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=LINE_LIMIT)
        except OSError as e:
            raise BridgeError("bridge_failed", f"could not start the game engine ({' '.join(self.cmd)}): {e}") from e
        self._seq = 0
        self._stderr_tail.clear()
        self._stderr_task = asyncio.create_task(self._drain_stderr(self._proc.stderr))
        ready = await self._read(0, READY_TIMEOUT, "startup")
        self.version = ready.get("version")

    async def new_game(self, **args: Any) -> dict:
        await self.start()
        return await self.call("new_game", **args)

    async def call(self, cmd: str, *, timeout: float | None = None, **args: Any) -> dict:
        async with self._lock:
            if not self.running:
                raise BridgeError("bridge_down", "the game engine is not running; it stopped after an earlier error")
            self._seq += 1
            rid = self._seq
            line = json.dumps({"id": rid, "cmd": cmd, "args": args}, separators=(",", ":")) + "\n"
            try:
                self._proc.stdin.write(line.encode())
                await self._proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                raise await self._died(cmd) from None
            return await self._read(rid, timeout or TIMEOUTS.get(cmd, DEFAULT_TIMEOUT), cmd)

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(Exception):
                proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task
            self._stderr_task = None

    async def _read(self, rid: int, timeout: float, what: str) -> dict:
        """Read replies until the one for `rid`; earlier ids belong to cancelled calls and are dropped."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            try:
                raw = await asyncio.wait_for(self._proc.stdout.readline(), max(0.0, deadline - loop.time()))
            except TimeoutError:
                self._proc.kill()
                await self.close()
                raise BridgeError("timeout", f"the game engine did not answer {what} within {timeout:.0f}s; "
                                  "stopped it") from None
            except ValueError as e:
                await self.close()
                raise BridgeError("bridge_failed", f"unreadable reply to {what}: {e}") from e
            if not raw:
                raise await self._died(what)
            try:
                reply = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("CivBridge wrote a non-JSON line to stdout: %.200s", raw)
                continue
            if not isinstance(reply, dict) or reply.get("id") != rid:
                continue
            if reply.get("ok"):
                return reply.get("result") or {}
            err = reply.get("error") or {}
            raise BridgeError(err.get("code", "error"), err.get("message", "the game engine refused the command"),
                              err.get("alternatives"), err.get("suggest"))

    async def _died(self, what: str) -> BridgeError:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(self._stderr_task), 1)
        tail = " | ".join(list(self._stderr_tail)[-5:])
        await self.close()
        return BridgeError("bridge_failed", f"the game engine exited during {what}" + (f": {tail}" if tail else ""))

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        """Keep the stderr pipe empty (a full pipe blocks the engine) and remember the tail for crash reports."""
        while line := await stream.readline():
            text = line.decode(errors="replace").rstrip()
            self._stderr_tail.append(text)
            log.debug("civbridge: %s", text)
