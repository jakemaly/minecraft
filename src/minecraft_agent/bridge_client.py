"""Line-oriented client for the Mineflayer bridge supervisor process."""

from __future__ import annotations

import json
import select
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class _PendingResponse:
    ready: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None
    error: Exception | None = None


class BridgeClient:
    def __init__(self, command: list[str], cwd: str | Path, env: dict[str, str], *, timeout: float = 30):
        self.process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=True)
        self.timeout = timeout
        self._lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[str, _PendingResponse] = {}
        self._stderr_tail = deque(maxlen=64)
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
        self.events: list[dict[str, Any]] = []
        self.ready_event: dict[str, Any] | None = None
        self._ready = False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("Mineflayer bridge exited before ready")
            line = self._readline(min(0.5, max(0, deadline - time.monotonic())))
            if line:
                event = json.loads(line)
                if event.get("event") == "ready":
                    self.ready_event = event
                    self._ready = True
                    self._reader_thread = threading.Thread(target=self._read_responses, daemon=True)
                    self._reader_thread.start()
                    return
                self.events.append(event)
        self.stop()
        raise TimeoutError(f"Mineflayer bridge did not signal ready; stderr tail: {''.join(self._stderr_tail)[-1000:]}")

    def _readline(self, timeout: float) -> str:
        ready, _, _ = select.select([self.process.stdout], [], [], timeout)
        if not ready:
            return ""
        line = self.process.stdout.readline()
        if len(line.encode("utf-8")) > 1_000_000:
            raise RuntimeError("Mineflayer response exceeds maximum size")
        return line

    def _drain_stderr(self) -> None:
        for line in self.process.stderr:
            self._stderr_tail.append(line[-1000:])

    def _read_responses(self) -> None:
        try:
            for line in self.process.stdout:
                if len(line.encode("utf-8")) > 1_000_000:
                    raise RuntimeError("Mineflayer response exceeds maximum size")
                response = json.loads(line)
                request_id = response.get("request_id")
                with self._pending_lock:
                    pending = self._pending.get(request_id)
                if pending is not None:
                    pending.response = response
                    pending.ready.set()
        except Exception as exc:
            self._ready = False
            self._fail_pending(exc)
        finally:
            self._ready = False
            self._fail_pending(RuntimeError("Mineflayer bridge closed its protocol stream"))

    def _fail_pending(self, error: Exception) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
        for item in pending:
            item.error = error
            item.ready.set()

    def request(self, method: str, params: dict[str, Any], *, request_timeout: float | None = None) -> dict[str, Any]:
        if not self._ready or self.process.poll() is not None:
            raise RuntimeError("Mineflayer bridge is not ready")
        timeout = self.timeout if request_timeout is None else request_timeout
        if timeout <= 0:
            raise ValueError("bridge request timeout must be positive")
        request_id = str(uuid.uuid4())
        lock_timeout = min(timeout, 5) if request_timeout is None else min(timeout, 0.01)
        if not self._lock.acquire(timeout=lock_timeout):
            raise TimeoutError("Mineflayer bridge is busy; bounded request was deferred")
        pending = _PendingResponse()
        with self._pending_lock:
            self._pending[request_id] = pending
        try:
            self.process.stdin.write(json.dumps({"version": 1, "request_id": request_id, "method": method, "params": params}, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
            if not pending.ready.wait(timeout):
                raise TimeoutError("Mineflayer bridge request timed out; outcome is unknown")
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            self._lock.release()
        if pending.error:
            raise RuntimeError(f"Mineflayer bridge reader failed: {type(pending.error).__name__}") from pending.error
        response = pending.response
        if response is None:
            raise RuntimeError("Mineflayer bridge returned no correlated response")
        if response.get("version") != 1:
            raise RuntimeError("Mineflayer bridge protocol version mismatch")
        if response.get("request_id") != request_id:
            raise RuntimeError("Mineflayer bridge returned a mismatched request ID")
        if response.get("error"):
            raise RuntimeError(f"Mineflayer bridge error: {response['error'].get('message', 'unknown error')}")
        return response["result"]

    def stop(self) -> None:
        if self.process.poll() is None:
            try:
                self.request("stop", {})
            except (OSError, RuntimeError, TimeoutError):
                pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self._ready = False
        if getattr(self, "_reader_thread", None):
            self._reader_thread.join(timeout=1)
        if self._stderr_thread:
            self._stderr_thread.join(timeout=1)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream and not stream.closed:
                stream.close()
