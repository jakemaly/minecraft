"""Persistent service for the Minecraft app, agent, and world processes."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import secrets
import shutil
import signal
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bridge_client import BridgeClient
from .app_server import create_server as create_app_server
from .containers import bot_container_command, cleanup_run_containers, connect_gateway_to_network, create_private_network, ensure_runtime_images, gateway_container_command, remove_private_network, server_container_command
from .config import RunConfig, runtime_root
from .events import EventLog
from .network import client_tailscale_address, tailscale_address, viewer_url
from .pi_client import PiClient
from .supervisor import RunState, Supervisor
from .workspace import Workspace


def _visible_event(event: dict[str, Any]) -> dict[str, Any] | None:
    if event.get("type") == "reasoning_update":
        return None

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            if value.get("type") in {"thinking", "reasoning"}:
                return None
            return {
                key: clean(item) for key, item in value.items()
                if key not in {"thinking", "exposed_reasoning"}
                and not (key == "reasoning" and isinstance(item, (str, dict, list)))
            }
        if isinstance(value, list):
            return [cleaned for item in value if (cleaned := clean(item)) is not None]
        return value

    return clean(event)


def _new_bridge_socket_path() -> tuple[Path, Path]:
    """Create a private, short Unix socket path that fits Linux's sun_path limit."""
    directory = Path(tempfile.mkdtemp(prefix="mc-", dir="/tmp"))
    socket_path = directory / "bridge.sock"
    if len(os.fsencode(socket_path)) >= 108:
        shutil.rmtree(directory, ignore_errors=True)
        raise RuntimeError("temporary directory produced an overlong Mineflayer socket path")
    return directory, socket_path


@dataclass
class ActiveRun:
    run_id: str
    world_id: str
    kind: str
    goal: str
    objective: str
    duration_hours: float
    run_directory: Path
    server_directory: Path
    seed: int
    bind_address: str
    server_port: int
    viewer_port: int
    viewer_url: str | None
    events: EventLog
    supervisor: Supervisor
    model: dict[str, Any]
    bridge: BridgeClient | None = None
    bridge_socket_directory: Path | None = None
    workspace: Workspace | None = None
    state: str = "launching"
    phase: str = "preflight"
    error: str | None = None
    live_message: dict[str, Any] | None = None
    docker_network: str | None = None
    containerized: bool = False
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_monotonic: float = field(default_factory=time.monotonic)
    active_seconds_at_pause: float = 0.0
    running_since: float | None = None
    manifest: dict[str, Any] = field(default_factory=dict)
    thread: threading.Thread | None = None
    finalizing: bool = False


class _ControlHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline(2_000_001)
        if len(line) > 2_000_000:
            self._send({"ok": False, "error": {"code": "request_too_large", "message": "control request exceeds 2 MB"}})
            return
        try:
            request = json.loads(line)
            if request.get("version") != 1 or not isinstance(request.get("method"), str) or not isinstance(request.get("params", {}), dict):
                raise ValueError("invalid control request envelope")
            result = self.server.daemon.dispatch(request["method"], request.get("params", {}))  # type: ignore[attr-defined]
            self._send({"ok": True, "result": result})
        except Exception as exc:
            self._send({"ok": False, "error": {"code": type(exc).__name__, "message": str(exc)[:700]}})

    def _send(self, value: dict[str, Any]) -> None:
        try:
            data = (json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str) + "\n").encode("utf-8")
            self.wfile.write(data[:2_000_000])
        except (BrokenPipeError, ConnectionResetError):
            pass


class _ThreadedUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class RunDaemon:
    ACTIVE_STATES = {"launching", "probing_model", "running", "paused"}

    def __init__(self, repo_root: str | Path | None = None):
        self.repo_root = Path(repo_root or os.getenv("MINECRAFT_AGENT_REPO_ROOT") or Path(__file__).resolve().parents[2]).resolve()
        self.runtime = runtime_root()
        self.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runtime.chmod(0o700)
        self.socket_path = self.runtime / "control.sock"
        self.lock = threading.RLock()
        self.active: ActiveRun | None = None
        self.previous_run: dict[str, Any] | None = None
        self.oauth_state: dict[str, Any] = {"connected": False}
        self.closing = False
        self._restore_state()
        self.pi = PiClient(self.repo_root, self.runtime, self.socket_path, self._on_pi_event)
        self.pi.start()
        self._server: _ThreadedUnixServer | None = None
        self._control_thread: threading.Thread | None = None
        self._app_server = None
        self._app_thread: threading.Thread | None = None

    def serve(self, *, include_dashboard: bool = True) -> None:
        if self.socket_path.exists():
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as check:
                check.settimeout(0.2)
                try:
                    check.connect(str(self.socket_path))
                    raise RuntimeError(f"another Minecraft run service is listening at {self.socket_path}")
                except (ConnectionRefusedError, FileNotFoundError, TimeoutError):
                    self.socket_path.unlink(missing_ok=True)
        server = _ThreadedUnixServer(str(self.socket_path), _ControlHandler)
        server.daemon = self
        os.chmod(self.socket_path, 0o600)
        self._server = server
        self._control_thread = threading.Thread(target=server.serve_forever, name="minecraft-control", daemon=True)
        self._control_thread.start()
        if include_dashboard:
            app_host = os.getenv("MINECRAFT_DASHBOARD_HOST", tailscale_address())
            app_port = int(os.getenv("MINECRAFT_DASHBOARD_PORT", "2718"))
            self._app_server = create_app_server(self, app_host, app_port)
            self._app_thread = threading.Thread(target=self._app_server.serve_forever, name="minecraft-app", daemon=True)
            self._app_thread.start()
        self._write_service_state()
        self._control_thread.join()

    def close(self) -> None:
        self.closing = True
        try:
            if self.active and self.active.state in self.ACTIVE_STATES:
                self._stop_run(self.active, final_state="stopped")
        except Exception:
            pass
        try:
            self.pi.stop()
        except Exception:
            pass
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._app_server:
            self._app_server.shutdown()
            self._app_server.server_close()
        self.socket_path.unlink(missing_ok=True)

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "status":
            return self.status()
        if method == "auth_start":
            with self.lock:
                self.oauth_state = {"connected": False, "login_in_progress": True}
            try:
                return self.pi.request("auth_start", timeout=15)
            except Exception as exc:
                with self.lock:
                    self.oauth_state = {"connected": False, "login_in_progress": False, "error": str(exc)[:300]}
                raise
        if method == "auth_callback":
            return self.pi.request("auth_callback", {"url": params.get("url", "")}, timeout=15)
        if method == "pin_default_model":
            return self.pi.request("pin_default_model", {"model_id": params.get("model_id", "")}, timeout=60)
        if method == "disconnect_chatgpt":
            return self.pi.request("logout", timeout=15)
        if method == "start_run":
            return self.start_run(str(params.get("kind", "diamond_pilot")))
        if method == "pause_run":
            return self.pause_run()
        if method == "resume_run":
            return self.resume_run()
        if method == "stop_run":
            return self.stop_run()
        if method == "export_run":
            return self.export_run()
        if method == "workspace_tool":
            return self.workspace_tool(params)
        if method == "agent_event":
            return self.agent_event(params)
        if method == "agent_bridge_request":
            return self.agent_bridge_request(params)
        if method == "usage_limit":
            return self.usage_limit(params)
        if method == "agent_run_finished":
            return self.agent_run_finished(params)
        raise ValueError(f"unsupported control method: {method}")

    def agent_bridge_request(self, params: dict[str, Any]) -> dict[str, Any]:
        run_id = str(params.get("run_id", ""))
        bridge_method = str(params.get("method", ""))
        if bridge_method not in {"observe", "verify_goal", "release_controls"}:
            raise ValueError("unsupported agent bridge request")
        with self.lock:
            run = self.active
            if not run or run.run_id != run_id or not run.bridge:
                raise RuntimeError("active Mineflayer bridge not found for this run")
        try:
            return run.bridge.request(bridge_method, params.get("arguments") or {}, request_timeout=20)
        except Exception as exc:
            run.events.append("bridge_request_failed", {"method": bridge_method, "error": str(exc)[:300]})
            raise

    def _restore_state(self) -> None:
        state_path = self.runtime / "service-state.json"
        if not state_path.is_file():
            return
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.previous_run = state.get("previous_run")
        except (OSError, json.JSONDecodeError):
            self.previous_run = None

    def _write_service_state(self) -> None:
        path = self.runtime / "service-state.json"
        temp = path.with_suffix(".tmp")
        value = {"schema_version": 1, "updated_at": datetime.now(timezone.utc).isoformat(), "previous_run": self.previous_run}
        temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp, path)

    def _on_pi_event(self, message: dict[str, Any]) -> None:
        event = message.get("event")
        if event in {"oauth_url", "oauth_prompt", "oauth_progress", "oauth_complete", "oauth_failed"}:
            with self.lock:
                if event == "oauth_url":
                    self.oauth_state = {"connected": False, "login_in_progress": True, "authorization_url": message.get("url"), "instructions": message.get("instructions")}
                elif event == "oauth_prompt":
                    self.oauth_state["callback_placeholder"] = message.get("placeholder")
                elif event == "oauth_progress":
                    self.oauth_state["progress"] = message.get("message")
                elif event == "oauth_complete":
                    self.oauth_state = {"connected": True, "login_in_progress": False, "models": message.get("model_count"), "chosen": message.get("chosen")}
                elif event == "oauth_failed":
                    self.oauth_state = {"connected": False, "login_in_progress": False, "error": message.get("message")}
                self._write_service_state()
            return
        run_id = message.get("run_id")
        with self.lock:
            run = self.active
            if not run or run_id != run.run_id:
                return
            if event == "agent_model_selected":
                run.model = message.get("model") or run.model
                run.phase = "agent_running"
            elif event == "agent_error":
                run.error = message.get("message")
                run.phase = "agent_error"
            elif event == "subscription_limit":
                run.state = "paused"
                run.phase = "subscription_limit"
            elif event == "agent_run_finished":
                self._on_run_finished_locked(run, str(message.get("status", "failed")), int(message.get("active_elapsed_seconds", 0)))
            self._write_service_state()

    def status(self) -> dict[str, Any]:
        try:
            pi_status = self.pi.request("status", timeout=15)
            auth = pi_status.get("auth") or {}
        except Exception as exc:
            auth = {"connected": False, "error": str(exc)[:300]}
        with self.lock:
            run = self.active
            if not auth.get("connected"):
                oauth = self.oauth_state
                auth = {**auth, "login_in_progress": bool(auth.get("login_in_progress") or oauth.get("login_in_progress"))}
                for key in ("authorization_url", "instructions", "callback_placeholder", "progress", "error"):
                    if oauth.get(key):
                        auth[key] = oauth[key]
            if run:
                active_elapsed = run.active_seconds_at_pause
                if run.running_since is not None:
                    active_elapsed += max(0, time.monotonic() - run.running_since)
                run_status = {
                    "run_id": run.run_id, "world_id": run.world_id, "kind": run.kind,
                    "state": run.state, "phase": run.phase, "error": run.error,
                    "objective": run.objective, "goal": run.goal,
                    "active_elapsed_seconds": round(active_elapsed, 1),
                    "duration_hours": run.duration_hours,
                    "model": run.model, "world_seed": run.seed,
                    "run_directory": str(run.run_directory),
                    "server_address": f"{run.bind_address}:{run.server_port}",
                    "viewer_url": run.viewer_url,
                    "containerized": run.containerized,
                    "docker_network": run.docker_network,
                    "supervisor": run.supervisor.status(),
                    "live_message": dict(run.live_message) if run.live_message else None,
                    "recent_events": self._recent_events(run),
                    "export_path": str(self.runtime / "exports" / f"{run.run_id}.zip") if (self.runtime / "exports" / f"{run.run_id}.zip").exists() else None,
                }
            else:
                run_status = None
            return {
                "service": "ready", "service_pid": os.getpid(), "runtime_root": str(self.runtime),
                "chatgpt_auth": auth, "selected_model": (auth.get("account_model_catalog") or {}).get("chosen"),
                "active_run": run_status, "previous_run": self.previous_run,
                "tailnet": {"server_address": tailscale_address(), "client_address": client_tailscale_address()},
            }

    @staticmethod
    def _recent_events(run: ActiveRun, limit: int = 40) -> list[dict[str, Any]]:
        path = run.run_directory / "events" / "events.jsonl"
        if not path.exists():
            return []
        tail: list[dict[str, Any]] = []
        with path.open("rb") as file:
            file.seek(0, os.SEEK_END)
            end = file.tell()
            file.seek(max(0, end - 512_000))
            if file.tell():
                file.readline()
            for line in file:
                try:
                    event = json.loads(line)
                    visible = _visible_event(event) if isinstance(event, dict) else None
                    if visible is not None:
                        tail.append(visible)
                except json.JSONDecodeError:
                    continue
        visible = []
        for event in tail[-limit:]:
            payload = event.get("payload") or {}
            if event.get("type") == "tool_call_started":
                payload = {key: payload[key] for key in ("tool", "tool_call_id", "arguments") if key in payload}
            elif event.get("type") == "tool_call_finished":
                payload = {key: payload[key] for key in ("tool", "tool_call_id", "is_error", "duration_ms", "result", "player_health") if key in payload}
                if isinstance(payload.get("result"), str):
                    payload["result"] = payload["result"][:6000]
            elif event.get("type") == "model_context":
                keys = ("request_id", "model", "context_window", "estimated_input_tokens", "provider_input_tokens", "context_utilization_percent", "image_policy", "current_frame")
                payload = {key: payload[key] for key in keys if key in payload}
                payload["segments"] = [
                    {key: section[key] for key in ("key", "label", "kind", "estimated_tokens", "preview") if key in section}
                    for section in (event.get("payload", {}).get("segments") or [])
                ]
                for section in payload["segments"]:
                    if isinstance(section.get("preview"), str):
                        section["preview"] = section["preview"][:300]
                messages = event.get("payload", {}).get("transcript") or []
                payload["transcript"] = messages[-6:]
                while payload["transcript"] and len(json.dumps(payload["transcript"], ensure_ascii=False)) > 6000:
                    payload["transcript"].pop(0)
            elif event.get("type") == "model_message":
                keys = ("request_id", "model", "text", "tool_calls", "usage", "timing", "error", "estimated_input_tokens", "context_window", "context_utilization_percent", "reasoning_visibility")
                payload = {key: payload[key] for key in keys if key in payload}
                if isinstance(payload.get("text"), str):
                    payload["text"] = payload["text"][:6000]
            elif event.get("type") in {"agent_error", "run_failed", "run_stopped", "agent_run_finished", "model_probe_passed"}:
                keys = ("message", "error", "status", "turns", "active_elapsed_seconds", "model")
                payload = {key: payload[key] for key in keys if key in payload}
            elif event.get("type") == "bot_connected":
                keys = ("username", "minecraft_version", "health", "food")
                payload = {key: payload[key] for key in keys if key in payload}
            else:
                payload = {}
            visible.append({**event, "payload": payload})
        return visible

    def live_status(self) -> dict[str, Any]:
        with self.lock:
            run = self.active
            return {
                "run_id": run.run_id if run else None,
                "message": dict(run.live_message) if run and run.live_message else None,
            }

    def events_page(self, run_id: str, *, before: int | None = None, limit: int = 100) -> dict[str, Any]:
        if not run_id or not all(char.isalnum() or char == "-" for char in run_id) or not run_id.startswith("run-"):
            raise ValueError("invalid run ID")
        if before is not None and before < 1:
            raise ValueError("before must be a positive event sequence")
        limit = min(25, max(1, limit))
        run_root = (self.runtime / "runs" / run_id).resolve()
        runs_root = (self.runtime / "runs").resolve()
        if run_root.parent != runs_root or run_root.name != run_id:
            raise ValueError("invalid run directory")
        path = run_root / "events" / "events.jsonl"
        if not path.is_file():
            return {"events": [], "has_more": False, "next_before": None}

        descending: list[dict[str, Any]] = []
        chunk_size = 64 * 1024
        remainder = b""
        with path.open("rb") as file:
            offset = file.seek(0, os.SEEK_END)
            while offset > 0 and len(descending) <= limit:
                start = max(0, offset - chunk_size)
                file.seek(start)
                data = file.read(offset - start) + remainder
                lines = data.split(b"\n")
                remainder = lines[0]
                offset = start
                candidates = lines[1:]
                if offset == 0:
                    candidates.insert(0, remainder)
                    remainder = b""
                for line in reversed(candidates):
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(event, dict) or not isinstance(event.get("sequence"), int):
                        continue
                    sequence = int(event.get("sequence", 0))
                    if before is None or sequence < before:
                        visible = _visible_event(event)
                        if visible is not None:
                            descending.append(visible)
                            if len(descending) > limit:
                                break
        has_more = len(descending) > limit or offset > 0
        events = list(reversed(descending[:limit]))
        return {
            "events": events,
            "has_more": has_more,
            "next_before": events[0]["sequence"] if events else None,
        }

    def start_run(self, kind: str) -> dict[str, Any]:
        if kind not in {"diamond_pilot", "dragon_proof"}:
            raise ValueError("kind must be diamond_pilot or dragon_proof")
        auth = self.pi.request("status", timeout=15).get("auth") or {}
        selection = auth.get("account_model_catalog") or {}
        model = selection.get("chosen")
        if not auth.get("connected") or not model:
            raise RuntimeError("Start the configured inference provider and verify its multimodal model before starting a run.")
        if os.getenv("MINECRAFT_MOLAB_MODE") == "1" and model.get("provider") != "molab-vllm":
            raise RuntimeError("Molab runs are restricted to the local vLLM provider; remote model fallback is disabled.")
        with self.lock:
            if self.active and self.active.state in self.ACTIVE_STATES:
                raise RuntimeError(f"run {self.active.run_id} is already {self.active.state}")
            self.active = None
            thread = threading.Thread(target=self._start_run, args=(kind, model), name=f"minecraft-{kind}", daemon=True)
            placeholder = ActiveRun(
                run_id="launching", world_id="launching", kind=kind,
                goal="diamond_pilot" if kind == "diamond_pilot" else "dragon_kill",
                objective=("Have at least one diamond in the agent's inventory. This is the only success criterion. You may use any available tools to accomplish this." if kind == "diamond_pilot" else "Beat the Ender Dragon. The only success criterion is that the server records the minecraft:end/kill_dragon advancement. You may use any available tools to accomplish this."),
                duration_hours=4 if kind == "diamond_pilot" else float(os.getenv("MINECRAFT_DRAGON_RUN_HOURS", "72")),
                run_directory=self.runtime / "runs" / "launching", server_directory=self.runtime / "worlds" / "launching" / "server",
                seed=0, bind_address="127.0.0.1" if os.getenv("MINECRAFT_MOLAB_MODE") == "1" else tailscale_address(), server_port=int(os.getenv("MINECRAFT_SERVER_PORT", "25565")),
                viewer_port=int(os.getenv("MINECRAFT_VIEWER_PORT", "3000")), viewer_url=None,
                events=EventLog(self.runtime / "launching-events.jsonl", "launching", "launching"),
                supervisor=Supervisor(self.runtime / "launching-supervisor", "launching"), model=model, state="launching", phase="preflight",
            )
            placeholder.thread = thread
            self.active = placeholder
            self._write_service_state()
            thread.start()
            return {"status": "launching", "kind": kind}

    def _start_run(self, kind: str, selected_model: dict[str, Any]) -> None:
        run: ActiveRun | None = None
        try:
            config = RunConfig.from_environment()
            config.validate()
            container_status = (
                {"available": False, "reason": "Molab runs the harness directly inside the notebook host"}
                if os.getenv("MINECRAFT_MOLAB_MODE") == "1"
                else ensure_runtime_images()
            )
            run_id = "run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
            world_id = "world-" + uuid.uuid4().hex[:12]
            run_dir = self.runtime / "runs" / run_id
            server_dir = self.runtime / "worlds" / run_id / "server"
            run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
            server_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
            bind = "127.0.0.1" if os.getenv("MINECRAFT_MOLAB_MODE") == "1" else tailscale_address()
            if bind != "127.0.0.1" and ipaddress.ip_address(bind).version != 4:
                raise RuntimeError("Minecraft server and desktop clients currently require a Tailscale IPv4 address")
            server_port = int(os.getenv("MINECRAFT_SERVER_PORT", "25565"))
            viewer_port = int(os.getenv("MINECRAFT_VIEWER_PORT", "3000"))
            seed = secrets.randbits(63)
            goal = "diamond_pilot" if kind == "diamond_pilot" else "dragon_kill"
            objective = ("Have at least one diamond in the agent's inventory. This is the only success criterion. You may use any available tools to accomplish this." if kind == "diamond_pilot" else "Beat the Ender Dragon. The only success criterion is that the server records the minecraft:end/kill_dragon advancement. You may use any available tools to accomplish this.")
            run = ActiveRun(
                run_id=run_id, world_id=world_id, kind=kind, goal=goal, objective=objective,
                duration_hours=4 if kind == "diamond_pilot" else float(os.getenv("MINECRAFT_DRAGON_RUN_HOURS", "72")),
                run_directory=run_dir, server_directory=server_dir, seed=seed, bind_address=bind,
                server_port=server_port, viewer_port=viewer_port, viewer_url=viewer_url(bind, viewer_port) or f"http://127.0.0.1:{viewer_port}/",
                events=EventLog(run_dir / "events" / "events.jsonl", run_id, world_id),
                supervisor=Supervisor(run_dir, run_id), model=selected_model,
            )
            if container_status.get("available"):
                try:
                    run.docker_network = create_private_network(run_id)
                    run.containerized = True
                except Exception as exc:
                    container_status = {**container_status, "available": False, "reason": str(exc)[:500]}
            with self.lock:
                self.active = run
                self._write_service_state()
            run.manifest = {
                "schema_version": 2, "run_id": run_id, "world_id": world_id,
                "created_at": run.created_at, "kind": kind, "goal": goal,
                "objective": objective, "max_active_hours": run.duration_hours,
                "model": selected_model,
                "model_provider": "Molab-local vLLM through Pi" if selected_model.get("provider") == "molab-vllm" else "OpenAI Sign in with ChatGPT OAuth through Pi",
                "billing_policy": "local notebook inference; remote provider fallback disabled" if selected_model.get("provider") == "molab-vllm" else "ChatGPT subscription only; API-key fallback disabled",
                "game": {"minecraft_version": config.minecraft_version, "difficulty": "normal", "world_default_gamemode": "spectator", "agent_gamemode": "survival", "world_seed": seed, "server_bind": bind, "server_port": server_port, "online_mode": False, "server_jar_sha1": None},
                "viewer": {"url": run.viewer_url, "bind_address": bind, "port": viewer_port, "renderer": "Prismarine Viewer 1.33.0", "first_person": True, "browser_capture": "384x216 terrain-validated PNG; refreshed after each Mineflayer action; prior image pixels replaced"},
                "dependencies": {"bridge_lock": "bridge/package-lock.json"},
                "runtime": {
                    "containers": container_status,
                    "containerized": run.containerized,
                    "private_network": run.docker_network,
                    "gameplay_program_boundary": "bot process, Mineflayer program runner, and workspace are in the same unprivileged container; only the vanilla-server private network is attached" if run.containerized else "Mineflayer program runner is a host process; Docker workspace shell isolation does not confine mc-run",
                },
                "limits": {"model_context_window": selected_model.get("context_window"), "model_output_limit": selected_model.get("max_output_tokens"), "artificial_context_cap": None},
            }
            if selected_model.get("provider") == "molab-vllm":
                from .model_runtime import inference_config
                run.manifest["model_revision"] = selected_model.get("revision")
                run.manifest["inference_config"] = inference_config()
            self._write_manifest(run)
            run.events.append("run_created", {"manifest": run.manifest})
            run.supervisor.transition(RunState.MODELS_READY)
            self._set_phase(run, "downloading_server")
            from scripts.server import download_server, prepare_server, server_command
            server_cache = self.runtime / "server-cache"
            jar = download_server(config.minecraft_version, server_cache)
            local_jar = server_dir / jar.name
            shutil.copy2(jar, local_jar)
            prepare_server(server_dir, seed=seed, bind_address="0.0.0.0" if run.containerized else bind, port=25565 if run.containerized else server_port)
            run.manifest["game"]["server_jar_sha1"] = self._sha1(local_jar)
            self._write_manifest(run)
            memory_mb = int(os.getenv("MINECRAFT_SERVER_HEAP_MB", "4096"))
            server_argv = server_container_command(
                run_id=run_id, network=run.docker_network or "", server_directory=server_dir,
                memory_mb=memory_mb,
            ) if run.containerized else server_command(local_jar, memory_mb=memory_mb)
            run.supervisor.start(
                "minecraft-server", server_argv,
                cwd=server_dir, env={"RUN_ID": run_id}, ready_pattern=r"Done \(.*\)! For help", timeout=240,
            )
            run.supervisor.transition(RunState.WORLD_READY)
            self._set_phase(run, "connecting_bot")
            if run.containerized:
                socket_path = run_dir / ".runtime" / "mineflayer.sock"
                socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                # Linux Unix-domain socket names cap at 107 path bytes; Molab's long run roots exceed that.
                run.bridge_socket_directory, socket_path = _new_bridge_socket_path()
            workspace_root = run_dir / "agent-workspace"
            workspace = Workspace(workspace_root, events=run.events, bridge_socket=socket_path, shared_skills=self.runtime / "skills-library")
            workspace.initialize(
                mineflayer_docs=self.repo_root / "bridge/node_modules/mineflayer/docs",
                pathfinder_docs=self.repo_root / "bridge/node_modules/mineflayer-pathfinder/readme.md",
            )
            run.workspace = workspace
            bridge_env = {
                "PATH": os.getenv("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(run_dir / ".runtime"),
                "LANG": "C.UTF-8", "RUN_ID": run_id, "MINECRAFT_VERSION": config.minecraft_version,
                "MINECRAFT_HOST": bind, "MINECRAFT_PORT": str(server_port), "MINECRAFT_ALLOW_TAILSCALE_HOST": "true",
                "MINECRAFT_AGENT_SOCKET": str(socket_path), "MINECRAFT_VIEWER_ENABLED": "true",
                "MINECRAFT_VIEWER_HOST": bind, "MINECRAFT_VIEWER_PORT": str(viewer_port), "MINECRAFT_VIEWER_DISTANCE": "6",
            }
            bridge_env.pop("OPENAI_API_KEY", None)
            if run.containerized:
                bridge_command = bot_container_command(
                    run_id=run_id, network=run.docker_network or "", repo_root=self.repo_root,
                    run_directory=run_dir, workspace=workspace_root,
                    minecraft_version=config.minecraft_version,
                )
                bridge_env = {"PATH": os.getenv("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(run_dir / ".runtime"), "LANG": "C.UTF-8"}
            else:
                bridge_command = ["node", str(self.repo_root / "bridge/src/bot.js")]
            bridge = BridgeClient(bridge_command, cwd=self.repo_root / "bridge", env=bridge_env, timeout=90)
            run.bridge = bridge
            observation = bridge.request("observe", {}, request_timeout=20)
            bot_username = observation.get("player", {}).get("username")
            if not bot_username:
                raise RuntimeError("Mineflayer connected without reporting its player username")
            run.supervisor.send_command("minecraft-server", f"gamemode survival {bot_username}")
            mode_deadline = time.monotonic() + 15
            while observation.get("world", {}).get("game_mode") != "survival" and time.monotonic() < mode_deadline:
                time.sleep(0.25)
                observation = bridge.request("observe", {}, request_timeout=10)
            if observation.get("world", {}).get("game_mode") != "survival":
                raise RuntimeError("server did not confirm the control bot entered Survival from the spectator-default world")
            player = observation.get("player") or {}
            run.events.append("bot_connected", {"username": player.get("username"), "minecraft_version": config.minecraft_version, "player_uuid": bridge.ready_event.get("player_uuid") if bridge.ready_event else None, "health": player.get("health"), "food": player.get("food")})
            if run.containerized:
                run.supervisor.start(
                    "network-gateway",
                    gateway_container_command(run_id=run_id, bind_address=bind, server_port=server_port, viewer_port=viewer_port),
                    cwd=self.repo_root, env={}, ready_pattern="gateway_ready", timeout=30,
                )
                connect_gateway_to_network(run_id, run.docker_network or "")
                run.events.append("network_gateway_ready", {"published_ports": {"minecraft": server_port, "viewer": viewer_port}, "host_bind": bind, "forward_targets": ["minecraft-server:25565", "minecraft-agent:3000"]})
            self._wait_for_viewer(bind, viewer_port, timeout=60)
            run.supervisor.transition(RunState.RUNNING)
            run.state = "probing_model"
            run.running_since = time.monotonic()

            docs_manifest = json.loads((workspace_root / ".runtime/docs-manifest.json").read_text(encoding="utf-8"))
            isolation = {"available": workspace.sandbox.available, "backend": workspace.sandbox.backend, "image": workspace.sandbox.image, "reason": workspace.sandbox.reason}
            run.manifest["workspace"] = {"path": str(workspace_root), "documentation_revisions": docs_manifest, "isolation": isolation, "gameplay_programs_containerized": run.containerized, "tools": ["read", "write", "edit", "bash", "grep", "find", "ls", "web_search", "minecraft_observe", "minecraft_verify_goal"], "skills_library": str(self.runtime / "skills-library")}
            self._write_manifest(run)
            run.events.append("workspace_created", {"path": str(workspace_root), "sandbox": isolation, "documentation_revisions": docs_manifest})
            self._set_phase(run, "probing_model")
            probe = self.pi.request("probe_model", {
                "run_id": run_id, "kind": kind, "goal": goal, "objective": objective,
                "workspace": str(workspace_root), "run_directory": str(run_dir),
                "bridge_socket": str(socket_path), "viewer_url": run.viewer_url,
            }, timeout=300)
            run.manifest["model_probe"] = {"status": probe.get("status"), "model": probe.get("model"), "screenshot": probe.get("probe", {}).get("screenshot"), "tool_calls": probe.get("probe", {}).get("assistant", {}).get("tool_calls")}
            self._write_manifest(run)
            run.events.append("model_probe_passed", run.manifest["model_probe"])
            self._set_phase(run, "starting_agent")
            active_duration = run.duration_hours * 3600
            self.pi.request("start_run", {
                "run_id": run_id, "kind": kind, "goal": goal, "objective": objective,
                "workspace": str(workspace_root), "run_directory": str(run_dir),
                "bridge_socket": str(socket_path), "viewer_url": run.viewer_url,
                "duration_seconds": active_duration, "model_id": probe["model"]["id"],
            }, timeout=30)
            run.state = "running"
            run.phase = "agent_running"
            run.running_since = time.monotonic()
            run.events.append("agent_started", {"model": probe["model"], "active_duration_seconds": active_duration})
            self._write_manifest(run)
            self._write_service_state()
        except Exception as exc:
            with self.lock:
                if run is None:
                    run = self.active
                if run:
                    run.error = str(exc)[:700]
                    run.state = "failed"
                    run.phase = "failed"
                    try:
                        run.events.append("run_failed", {"error": run.error})
                    except Exception:
                        pass
                    self._cleanup_processes(run)
                    self.previous_run = self._run_summary(run)
                    self.active = run
                self._write_service_state()

    @staticmethod
    def _sha1(path: Path) -> str:
        digest = hashlib.sha1()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _write_manifest(run: ActiveRun) -> None:
        path = run.run_directory / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(run.manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp, path)

    @staticmethod
    def _wait_for_viewer(host: str, port: int, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, port), timeout=1):
                    return
            except OSError:
                time.sleep(0.25)
        raise TimeoutError(f"Prismarine Viewer did not bind to {host}:{port} in {timeout:g}s")

    def _set_phase(self, run: ActiveRun, phase: str) -> None:
        with self.lock:
            if self.active is run:
                run.phase = phase
                self._write_service_state()

    def pause_run(self) -> dict[str, Any]:
        run = self.active
        if not run or run.state not in {"running", "probing_model"}:
            raise RuntimeError("there is no running world to pause")
        if run.state == "probing_model":
            raise RuntimeError("wait for the initial model probe to finish before pausing")
        if run.workspace:
            run.workspace.cancel_active()
        if run.bridge:
            run.bridge.request("release_controls", {}, request_timeout=10)
        run.supervisor.send_command("minecraft-server", "tick freeze")
        run.supervisor.send_command("minecraft-server", "save-all flush")
        try:
            self.pi.request("pause_run", {"run_id": run.run_id}, timeout=30)
        except Exception:
            run.supervisor.send_command("minecraft-server", "tick unfreeze")
            raise
        with self.lock:
            if run.running_since is not None:
                run.active_seconds_at_pause += time.monotonic() - run.running_since
                run.running_since = None
            run.state = "paused"
            run.phase = "paused"
            if run.supervisor.state == RunState.RUNNING:
                run.supervisor.transition(RunState.PAUSED)
            run.events.append("run_paused", {"frozen": True, "saved": True, "active_elapsed_seconds": round(run.active_seconds_at_pause, 2)})
            self._write_service_state()
        return {"status": "paused", "world_frozen": True, "saved": True}

    def resume_run(self) -> dict[str, Any]:
        run = self.active
        if not run or run.state != "paused":
            raise RuntimeError("there is no paused world to resume")
        run.supervisor.send_command("minecraft-server", "tick unfreeze")
        if run.supervisor.state == RunState.PAUSED:
            run.supervisor.transition(RunState.RUNNING)
        try:
            self.pi.request("resume_run", {"run_id": run.run_id}, timeout=10)
        except RuntimeError:
            remaining = max(0.0, run.duration_hours * 3600 - run.active_seconds_at_pause)
            bridge_socket = run.workspace.bridge_socket if run.workspace else run.run_directory / ".runtime/mineflayer.sock"
            self.pi.request("start_run", {
                "run_id": run.run_id, "kind": run.kind, "goal": run.goal, "objective": run.objective,
                "workspace": str(run.run_directory / "agent-workspace"), "run_directory": str(run.run_directory),
                "bridge_socket": str(bridge_socket), "viewer_url": run.viewer_url,
                "duration_seconds": remaining, "model_id": run.model.get("id"),
            }, timeout=30)
        with self.lock:
            run.state = "running"
            run.phase = "agent_running"
            run.running_since = time.monotonic()
            run.events.append("run_resumed", {"world_restored": True, "workspace_restored": True, "remaining_active_seconds": round(max(0.0, run.duration_hours * 3600 - run.active_seconds_at_pause), 2)})
            self._write_service_state()
        return {"status": "resumed", "world_restored": True, "workspace_restored": True}

    def stop_run(self) -> dict[str, Any]:
        run = self.active
        if not run:
            return {"status": "idle"}
        return self._stop_run(run, final_state="stopped")

    def _stop_run(self, run: ActiveRun, *, final_state: str) -> dict[str, Any]:
        run.finalizing = True
        try:
            if run.workspace:
                run.workspace.cancel_active()
            if run.bridge:
                try:
                    run.bridge.request("release_controls", {}, request_timeout=5)
                    run.bridge.request("stop", {}, request_timeout=5)
                except Exception:
                    pass
            try:
                self.pi.request("stop_run", {"run_id": run.run_id}, timeout=10)
            except Exception:
                pass
            try:
                run.supervisor.stop_minecraft_server()
            finally:
                run.supervisor.stop_all()
            if run.containerized:
                cleanup_run_containers(run.run_id)
            run.state = final_state
            run.phase = final_state
            run.error = None if final_state in {"passed", "time_limit", "stopped"} else run.error
            run.events.append("run_stopped", {"status": final_state, "controls_released": True, "server_saved": True})
        except Exception as exc:
            run.error = str(exc)[:500]
            run.state = "failed"
            run.phase = "cleanup_failed"
        finally:
            try:
                remove_private_network(run.docker_network)
            except Exception:
                pass
            run.docker_network = None
            if run.bridge_socket_directory:
                shutil.rmtree(run.bridge_socket_directory, ignore_errors=True)
                run.bridge_socket_directory = None
            with self.lock:
                self.previous_run = self._run_summary(run)
                self._write_service_state()
        return {"status": run.state, "error": run.error}

    def _cleanup_processes(self, run: ActiveRun) -> None:
        try:
            if run.workspace:
                run.workspace.cancel_active()
            if run.bridge:
                try:
                    run.bridge.request("release_controls", {}, request_timeout=3)
                    run.bridge.request("stop", {}, request_timeout=3)
                except Exception:
                    pass
            run.supervisor.stop_minecraft_server(grace_seconds=15)
            run.supervisor.stop_all()
            if run.containerized:
                cleanup_run_containers(run.run_id)
        except Exception as exc:
            run.error = (run.error or "") + f"; cleanup: {str(exc)[:200]}"
        finally:
            try:
                remove_private_network(run.docker_network)
            except Exception:
                pass
            run.docker_network = None
            if run.bridge_socket_directory:
                shutil.rmtree(run.bridge_socket_directory, ignore_errors=True)
                run.bridge_socket_directory = None

    def _on_run_finished_locked(self, run: ActiveRun, status: str, active_seconds: int) -> None:
        run.active_seconds_at_pause = max(run.active_seconds_at_pause, float(active_seconds))
        run.running_since = None
        if status == "paused":
            run.state = "paused"
            run.phase = "paused"
            return
        if status == "usage_limited":
            run.state = "paused"
            run.phase = "subscription_limit"
            try:
                if run.bridge:
                    run.bridge.request("release_controls", {}, request_timeout=5)
                run.supervisor.send_command("minecraft-server", "tick freeze")
                run.supervisor.send_command("minecraft-server", "save-all flush")
                if run.supervisor.state == RunState.RUNNING:
                    run.supervisor.transition(RunState.PAUSED)
            except Exception as exc:
                run.error = str(exc)[:300]
        elif status in {"passed", "time_limit", "failed", "stopped"} and not run.finalizing:
            run.finalizing = True
            run.state = status
            run.phase = status
            if status == "failed":
                run.error = run.error or "Pi agent stopped after a model or runtime error"
            threading.Thread(target=self._stop_run, args=(run,), kwargs={"final_state": status}, daemon=True).start()

    def _run_summary(self, run: ActiveRun) -> dict[str, Any]:
        return {"run_id": run.run_id, "kind": run.kind, "state": run.state, "phase": run.phase, "goal": run.goal, "objective": run.objective, "model": run.model, "run_directory": str(run.run_directory), "viewer_url": run.viewer_url, "error": run.error}

    def workspace_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        run = self.active
        if not run or params.get("run_id") != run.run_id or not run.workspace:
            raise RuntimeError("run workspace is unavailable or the run id is stale")
        if run.state not in self.ACTIVE_STATES:
            raise RuntimeError(f"workspace is not active while run is {run.state}")
        name = str(params.get("tool", ""))
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        return run.workspace.execute(name, arguments)

    def workspace_tree(self, run_id: str) -> dict[str, Any]:
        with self.lock:
            known = run_id == (self.active.run_id if self.active else None) or run_id == (self.previous_run or {}).get("run_id")
        if not known:
            raise ValueError("workspace is not available for this run")
        runs_root = (self.runtime / "runs").resolve()
        run_root = (runs_root / run_id).resolve()
        if run_root.parent != runs_root or run_root.name != run_id:
            raise ValueError("invalid run workspace")
        workspace_root = run_root / "agent-workspace"
        if workspace_root.is_symlink() or not workspace_root.is_dir():
            return {"run_id": run_id, "tree": [], "truncated": False}

        budget = [800]
        truncated = [False]

        def scan(directory: Path, relative: str, depth: int) -> list[dict[str, Any]]:
            nodes = []
            try:
                entries = sorted(os.scandir(directory), key=lambda entry: (not entry.is_dir(follow_symlinks=False), entry.name.casefold()))
            except OSError:
                return nodes
            for entry in entries:
                if entry.name in {".runtime", ".history", ".git", "node_modules"}:
                    continue
                if budget[0] <= 0:
                    truncated[0] = True
                    break
                budget[0] -= 1
                path = f"{relative}/{entry.name}" if relative else entry.name
                node = {"name": entry.name, "path": path}
                try:
                    if entry.is_symlink():
                        node["kind"] = "link"
                    elif entry.is_dir(follow_symlinks=False):
                        node["kind"] = "directory"
                        if depth < 8:
                            node["children"] = scan(Path(entry.path), path, depth + 1)
                        else:
                            node["children"] = []
                            truncated[0] = True
                    elif entry.is_file(follow_symlinks=False):
                        node["kind"] = "file"
                    else:
                        node["kind"] = "other"
                except OSError:
                    node["kind"] = "other"
                nodes.append(node)
            return nodes

        return {"run_id": run_id, "tree": scan(workspace_root, "", 0), "truncated": truncated[0]}

    def agent_event(self, params: dict[str, Any]) -> dict[str, Any]:
        run = self.active
        if not run or params.get("run_id") != run.run_id:
            raise RuntimeError("event run id is stale")
        event_type = str(params.get("type", "agent_event"))[:100]
        payload = params.get("payload") or {}
        if not isinstance(payload, dict):
            raise ValueError("event payload must be an object")
        if event_type == "model_stream":
            request_id = str(payload.get("request_id", ""))[:120]
            if not request_id:
                raise ValueError("model stream update requires a request ID")
            with self.lock:
                current = run.live_message
                if payload.get("active") is False:
                    if current and current.get("request_id") == request_id:
                        run.live_message = None
                else:
                    if not current or current.get("request_id") != request_id or payload.get("reset"):
                        current = {"request_id": request_id, "model": str(payload.get("model", "model"))[:100], "text": ""}
                        run.live_message = current
                    delta = str(payload.get("delta", ""))[:2000]
                    current["text"] = (current.get("text", "") + delta)[:12_000]
            return {"status": "updated"}
        event = run.events.append(event_type, payload)
        return {"sequence": event["sequence"]}

    def usage_limit(self, params: dict[str, Any]) -> dict[str, Any]:
        run = self.active
        if not run or params.get("run_id") != run.run_id:
            raise RuntimeError("run id is stale")
        run.events.append("subscription_usage_limit", {"message": str(params.get("message", ""))[:400], "automatic_api_fallback": False})
        try:
            if run.workspace:
                run.workspace.cancel_active()
            if run.bridge:
                run.bridge.request("release_controls", {}, request_timeout=5)
            run.supervisor.send_command("minecraft-server", "tick freeze")
            run.supervisor.send_command("minecraft-server", "save-all flush")
            if run.supervisor.state == RunState.RUNNING:
                run.supervisor.transition(RunState.PAUSED)
        finally:
            run.state = "paused"
            run.phase = "subscription_limit"
            run.error = str(params.get("message", "ChatGPT subscription usage limit reached."))[:400]
            run.running_since = None
            self._write_service_state()
        return {"status": "paused", "automatic_billing_fallback": False}

    def agent_run_finished(self, params: dict[str, Any]) -> dict[str, Any]:
        run = self.active
        if not run or params.get("run_id") != run.run_id:
            raise RuntimeError("run id is stale")
        with self.lock:
            self._on_run_finished_locked(run, str(params.get("status", "failed")), int(params.get("active_elapsed_seconds", 0)))
            run.events.append("agent_run_finished", {
                "status": params.get("status"),
                "active_elapsed_seconds": params.get("active_elapsed_seconds"),
                "turns": params.get("turns"),
                "final_observation": params.get("final_observation"),
            })
            self._write_service_state()
        return {"status": run.state}

    def export_run(self) -> dict[str, Any]:
        run = self.active
        if run and run.state in self.ACTIVE_STATES:
            raise RuntimeError("pause or stop the run before exporting its trace and world")
        if not run and self.previous_run:
            run_id = self.previous_run["run_id"]
            run_dir = Path(self.previous_run["run_directory"])
            world_dir = self.runtime / "worlds" / run_id
        elif run:
            run_id = run.run_id
            run_dir = run.run_directory
            world_dir = run.server_directory.parent
        else:
            raise RuntimeError("there is no run to export")
        if not run_dir.is_dir():
            raise FileNotFoundError("run evidence directory is missing")
        exports = self.runtime / "exports"
        exports.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = exports / f"{run_id}.zip"
        temp = target.with_suffix(".zip.tmp")
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=4) as archive:
            for root_name, root in (("run", run_dir), ("world", world_dir)):
                if not root.exists():
                    continue
                for file in sorted(root.rglob("*")):
                    relative = file.relative_to(root)
                    if relative.parts[:2] == (".runtime", "chrome") or file.suffix == ".sock":
                        continue
                    if file.is_file() and not file.is_symlink():
                        archive_name = (Path(root_name) / relative).as_posix()
                        if root_name == "run" and relative.as_posix() == "events/events.jsonl":
                            with archive.open(archive_name, "w") as target_file, file.open("r", encoding="utf-8") as source_file:
                                for line in source_file:
                                    try:
                                        event = json.loads(line)
                                    except json.JSONDecodeError:
                                        continue
                                    visible = _visible_event(event) if isinstance(event, dict) else None
                                    if visible is not None:
                                        target_file.write((json.dumps(visible, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
                            continue
                        archive.write(file, archive_name)
            manifest_path = run_dir / "manifest.json"
            events_path = run_dir / "events/events.jsonl"
            if manifest_path.is_file() and events_path.is_file():
                from .rollouts import build_rollout_export

                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                event_rows = []
                for line in events_path.read_text(encoding="utf-8").splitlines():
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict):
                        event_rows.append(item)
                episode, transitions = build_rollout_export(manifest, event_rows)
                archive.writestr("run/rollout/episode.json", json.dumps(episode, ensure_ascii=False, indent=2) + "\n")
                archive.writestr(
                    "run/rollout/transitions.jsonl",
                    "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in transitions),
                )
        os.replace(temp, target)
        digest = hashlib.sha256()
        with target.open("rb") as archive_file:
            while chunk := archive_file.read(1024 * 1024):
                digest.update(chunk)
        if run:
            run.events.append("evidence_exported", {"path": str(target), "sha256": digest.hexdigest(), "bytes": target.stat().st_size})
        return {"path": str(target), "sha256": digest.hexdigest(), "bytes": target.stat().st_size}

def main() -> None:
    daemon = RunDaemon()
    def stop(_signum, _frame):
        threading.Thread(target=daemon.close, name="minecraft-daemon-shutdown", daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        daemon.serve()
    finally:
        daemon.close()


if __name__ == "__main__":
    main()
