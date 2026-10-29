"""Persistent Pi SDK process with OAuth state kept outside every run workspace."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable


class PiClient:
    def __init__(self, repo_root: str | Path, runtime: str | Path, control_socket: str | Path, on_event: Callable[[dict[str, Any]], None]):
        self.repo_root = Path(repo_root).resolve()
        self.runtime = Path(runtime).resolve()
        self.control_socket = Path(control_socket).resolve()
        self.on_event = on_event
        self.process: subprocess.Popen[str] | None = None
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[str, tuple[threading.Event, dict[str, Any] | None]] = {}
        self._stderr_tail: list[str] = []

    def start(self) -> None:
        agent_dir = self.runtime / "pi"
        agent_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        agent_dir.chmod(0o700)
        bridge = self.repo_root / "bridge"
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(agent_dir),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PI_AGENT_DIR": str(agent_dir),
            "MINECRAFT_AGENT_CONTROL_SOCKET": str(self.control_socket),
            "MINECRAFT_AGENT_REPO_ROOT": str(self.repo_root),
            "MINECRAFT_RUNTIME_HOME": str(self.runtime),
        }
        self.process = subprocess.Popen(
            ["node", str(bridge / "src/pi_agent.mjs")],
            cwd=bridge,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        threading.Thread(target=self._read_stdout, name="pi-agent-events", daemon=True).start()
        threading.Thread(target=self._read_stderr, name="pi-agent-stderr", daemon=True).start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("Pi worker exited during startup: " + "".join(self._stderr_tail)[-1000:])
            try:
                self.request("status", timeout=1)
                return
            except (OSError, RuntimeError, TimeoutError):
                time.sleep(0.1)
        raise TimeoutError("Pi worker did not become ready")

    def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        for line in self.process.stdout:
            if len(line.encode("utf-8", errors="replace")) > 2_000_000:
                self.on_event({"event": "pi_protocol_error", "message": "Pi worker output exceeded 2 MB"})
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self.on_event({"event": "pi_protocol_error", "message": "Pi worker emitted invalid JSON"})
                continue
            request_id = message.get("request_id")
            if request_id:
                with self._pending_lock:
                    pending = self._pending.get(request_id)
                    if pending:
                        pending[1]["result"] = message
                        pending[0].set()
                continue
            if message.get("event"):
                self.on_event(message)

    def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        for line in self.process.stderr:
            self._stderr_tail.append(line[-1000:])
            del self._stderr_tail[:-32]

    def request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30) -> dict[str, Any]:
        if not self.process or not self.process.stdin or self.process.poll() is not None:
            raise RuntimeError("Pi worker is not running")
        request_id = uuid.uuid4().hex
        message = {"version": 1, "request_id": request_id, "method": method, "params": params or {}}
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        event = threading.Event()
        holder: dict[str, Any] = {}
        with self._pending_lock:
            self._pending[request_id] = (event, holder)
        try:
            with self._write_lock:
                self.process.stdin.write(encoded)
                self.process.stdin.flush()
            if not event.wait(timeout):
                raise TimeoutError(f"Pi request {method} timed out")
            response = holder.get("result") or {}
            if response.get("ok") is not True:
                error = response.get("error") or {}
                raise RuntimeError(f"{error.get('code', 'pi_error')}: {error.get('message', 'Pi request failed')}")
            return response.get("result") or {}
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def stop(self) -> None:
        if not self.process or self.process.poll() is not None:
            return
        try:
            self.request("shutdown", timeout=5)
        except (OSError, RuntimeError, TimeoutError):
            self.process.terminate()
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
