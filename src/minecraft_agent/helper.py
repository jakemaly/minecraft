"""Invocation-scoped capability bridge and direct JavaScript helper runner."""

from __future__ import annotations

import json
import hashlib
import os
import re
import socket
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

from .sandbox import cleanup_worker, stop_worker, worker_command, worker_execution_mode


class CapabilityServer:
    def __init__(self, socket_path: Path, bot, *, allowed_action_types: set[str] | None = None, max_actions: int = 20, max_bytes: int = 100_000):
        self.socket_path = socket_path
        self.bot = bot
        self.max_actions = max_actions
        self.max_bytes = max_bytes
        self.allowed_action_types = allowed_action_types
        self.actions_done = 0
        self.action_results: list[dict[str, Any]] = []
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self.error: Exception | None = None

    def start(self) -> None:
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        self._server.listen(1)
        self._server.settimeout(0.5)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            assert self._server is not None
            connection, _ = self._server.accept()
            connection.settimeout(15)
            with connection, connection.makefile("rwb") as stream:
                actions = 0
                while line := stream.readline(self.max_bytes + 1):
                    request = None
                    try:
                        if len(line) > self.max_bytes or not line.endswith(b"\n"):
                            raise ValueError("capability request exceeds size limit")
                        request = json.loads(line)
                        if request.get("version") != 1 or not isinstance(request.get("request_id"), str):
                            raise ValueError("invalid capability envelope")
                        method = request.get("method")
                        if method == "observe":
                            result = self.bot.request("observe", {})
                        elif method == "execute":
                            actions += 1
                            if actions > self.max_actions:
                                raise ValueError("helper action budget exhausted")
                            params = request.get("params", {})
                            action = params.get("action")
                            if not isinstance(action, dict):
                                raise ValueError("action must be an object")
                            if self.allowed_action_types is not None and action.get("type") not in self.allowed_action_types:
                                raise ValueError("action type is outside this helper's capability lease")
                            self.actions_done += 1
                            action_id = f"{uuid.uuid4()}:{actions}"
                            try:
                                # Helpers need the action acknowledgement; AgentStep fetches a fresh observation separately.
                                result = self.bot.request("execute", {"action_id": action_id, "action": action, "timeout_seconds": 5, "include_observation": False})
                                self.action_results.append({"action_id": action_id, "result": result})
                            except Exception as exc:
                                self.action_results.append({"action_id": action_id, "result": {"status": "unknown", "controls_released": False, "error_type": type(exc).__name__}})
                                raise
                        else:
                            raise ValueError("capability method is unavailable")
                        response = {"version": 1, "request_id": request["request_id"], "result": result}
                    except Exception as exc:
                        response = {"version": 1, "request_id": request.get("request_id") if isinstance(request, dict) else None, "error": {"code": "capability_denied", "message": str(exc)[:300]}}
                    payload = json.dumps(response, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
                    if len(payload) > self.max_bytes:
                        payload = json.dumps({"version": 1, "request_id": request.get("request_id"), "error": {"code": "response_too_large", "message": "capability response exceeds size limit"}}, separators=(",", ":")).encode("utf-8") + b"\n"
                    stream.write(payload)
                    stream.flush()
        except Exception as exc:
            self.error = exc

    def close(self) -> None:
        if self._server:
            self._server.close()
        if self._thread:
            self._thread.join(timeout=1)
        self.socket_path.unlink(missing_ok=True)


class HelperRunner:
    def __init__(self, bot, helper_directory: str | Path, *, timeout_seconds: int = 10, allowed_action_types: set[str] | None = None, max_actions: int = 20):
        self.bot = bot
        self.helper_directory = Path(helper_directory).resolve()
        self.timeout_seconds = timeout_seconds
        self.allowed_action_types = allowed_action_types
        if not 1 <= max_actions <= 100:
            raise ValueError("helper action budget must be between 1 and 100")
        self.max_actions = max_actions

    def run(self, source: str, helper_name: str = "agent_helper") -> dict[str, Any]:
        if not isinstance(source, str) or not source.strip() or len(source.encode("utf-8")) > 32_000:
            raise ValueError("helper source must be non-empty and no larger than 32 KiB")
        self.helper_directory.mkdir(parents=True, exist_ok=True)
        helper_name = re.sub(r"[^A-Za-z0-9_]", "_", helper_name)
        if not helper_name or len(helper_name) > 48:
            raise ValueError("invalid helper name")
        helper_path = self.helper_directory / f"{helper_name}.js"
        revision = 1
        while helper_path.exists():
            revision += 1
            helper_path = self.helper_directory / f"{helper_name}.v{revision}.js"
        helper_path.write_text(source, encoding="utf-8")
        helper_sha256 = hashlib.sha256(helper_path.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory(prefix="mc-capability-") as temp_dir:
            socket_dir = Path(temp_dir)
            socket_path = socket_dir / "capability.sock"
            server = CapabilityServer(socket_path, self.bot, allowed_action_types=self.allowed_action_types, max_actions=self.max_actions)
            server.start()
            wrapper = socket_dir / "worker.js"
            worker_template = Path(__file__).resolve().parents[2] / "bridge" / "src" / "worker.js"
            wrapper.write_bytes(worker_template.read_bytes())
            command = worker_command(wrapper, self.helper_directory, socket_path, helper_filename=helper_path.name, timeout_seconds=self.timeout_seconds)
            execution_mode = worker_execution_mode(command)
            try:
                child_env = {"PATH": os.getenv("PATH", "/usr/bin:/bin"), "HOME": str(self.helper_directory), "LANG": os.getenv("LANG", "C.UTF-8"), "CAPABILITY_SOCKET": str(socket_path), "HELPER_DIRECTORY": str(self.helper_directory)}
                result = subprocess.run(command, cwd=self.helper_directory, env=child_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=self.timeout_seconds + 3, check=False)
                self.action_results = list(server.action_results)
                if result.returncode:
                    return {"status": "unknown", "helper": helper_path.name, "revision": revision, "helper_sha256": helper_sha256, "execution_mode": execution_mode, "actions": server.actions_done, "action_results": self.action_results, "error_type": "WorkerExit", "exit_code": result.returncode, "stderr_tail": result.stderr.decode("utf-8", errors="replace")[-500:]}
                if server.error:
                    return {"status": "unknown", "helper": helper_path.name, "revision": revision, "helper_sha256": helper_sha256, "execution_mode": execution_mode, "actions": server.actions_done, "action_results": self.action_results, "error_type": type(server.error).__name__}
                return {"status": "completed", "helper": helper_path.name, "revision": revision, "helper_sha256": helper_sha256, "execution_mode": execution_mode, "actions": server.actions_done, "action_results": self.action_results}
            except subprocess.TimeoutExpired:
                stop_worker(command)
                self.action_results = list(server.action_results)
                return {"status": "unknown", "helper": helper_path.name, "revision": revision, "helper_sha256": helper_sha256, "execution_mode": execution_mode, "actions": server.actions_done, "action_results": self.action_results, "error_type": "TimeoutExpired"}
            finally:
                cleanup_worker(command)
                server.close()
