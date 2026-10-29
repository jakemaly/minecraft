"""Client for the Linux-owned run service used by the reactive Marimo notebook."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .config import runtime_root


def socket_path() -> Path:
    return runtime_root() / "control.sock"


def request(method: str, params: dict[str, Any] | None = None, *, timeout: float = 15) -> dict[str, Any]:
    payload = {"version": 1, "request_id": uuid.uuid4().hex, "method": method, "params": params or {}}
    data = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(data) > 1_000_000:
        raise ValueError("control request exceeds 1 MB")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(socket_path()))
        client.sendall(data)
        response = bytearray()
        while chunk := client.recv(64 * 1024):
            response.extend(chunk)
            if len(response) > 2_000_000:
                raise ValueError("control response exceeds 2 MB")
    result = json.loads(response)
    if result.get("ok") is not True:
        error = result.get("error") or {}
        raise RuntimeError(f"{error.get('code', 'control_error')}: {error.get('message', 'control request failed')}")
    return result.get("result") or {}


def ensure_daemon(repo_root: str | Path) -> dict[str, Any]:
    """Start the detached Linux service once; notebook kernel restarts only reconnect."""
    try:
        return request("status", timeout=1)
    except (OSError, RuntimeError, TimeoutError):
        pass

    root = Path(repo_root).resolve()
    runtime = runtime_root()
    runtime.mkdir(parents=True, exist_ok=True)
    log_path = runtime / "logs" / "agentd.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["MINECRAFT_AGENT_REPO_ROOT"] = str(root)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(root / "src"), env.get("PYTHONPATH", ""))))
    with log_path.open("ab", buffering=0) as log:
        subprocess.Popen(
            [sys.executable, "-m", "minecraft_agent.daemon"],
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            return request("status", timeout=1)
        except (OSError, RuntimeError, TimeoutError):
            time.sleep(0.2)
    raise TimeoutError(f"Minecraft run service did not start; inspect {log_path}")
