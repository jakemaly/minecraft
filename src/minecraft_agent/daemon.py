"""Persistent Linux service that owns Minecraft processes independently of Marimo."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import secrets
import shlex
import shutil
import signal
import socket
import socketserver
import subprocess
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bridge_client import BridgeClient
from .containers import bot_container_command, cleanup_run_containers, connect_gateway_to_network, create_private_network, ensure_runtime_images, gateway_container_command, remove_private_network, server_container_command
from .config import RunConfig, runtime_root
from .events import EventLog
from .identity import source_identity
from .network import client_tailscale_address, tailscale_address, viewer_url
from .pi_client import PiClient
from .supervisor import RunState, Supervisor
from .workspace import Workspace


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
    workspace: Workspace | None = None
    state: str = "launching"
    phase: str = "preflight"
    error: str | None = None
    observer_username: str | None = None
    docker_network: str | None = None
    containerized: bool = False
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_monotonic: float = field(default_factory=time.monotonic)
    active_seconds_at_pause: float = 0.0
    running_since: float | None = None
    manifest: dict[str, Any] = field(default_factory=dict)
    thread: threading.Thread | None = None
    finalizing: bool = False
    last_gameplay_action: dict[str, Any] | None = None
    last_saved_checkpoint: dict[str, Any] | None = None


def _gameplay_state(observation: dict[str, Any]) -> dict[str, Any]:
    player = observation.get("player") or {}
    return {
        "dimension": (observation.get("world") or {}).get("dimension"),
        "position": player.get("position"), "health": player.get("health"),
        "yaw": player.get("yaw"), "pitch": player.get("pitch"), "on_ground": player.get("on_ground"),
        "food": player.get("food"), "oxygen": player.get("oxygen"),
        "equipment": player.get("equipment"),
        "inventory": observation.get("inventory_summary"),
        "nearby_entities": observation.get("nearby_entities") or [],
        "nearby_blocks": observation.get("nearby_blocks") or [],
    }


def _state_sha256(state: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _gameplay_preconditions(state: dict[str, Any]) -> dict[str, Any]:
    return {key: state[key] for key in ("dimension", "equipment")}


def _has_gameplay_progress(before: dict[str, Any], after: dict[str, Any]) -> bool:
    if before["dimension"] != after["dimension"]:
        return True
    for key in ("health", "food", "oxygen"):
        if isinstance(before[key], (int, float)) and isinstance(after[key], (int, float)) and after[key] > before[key]:
            return True
    old_inventory = before["inventory"] or {}
    new_inventory = after["inventory"] or {}
    if any(new_inventory.get(name, 0) > count for name, count in old_inventory.items()):
        return True
    if any(count > old_inventory.get(name, 0) for name, count in new_inventory.items()):
        return True
    old_equipment = before["equipment"] or {}
    new_equipment = after["equipment"] or {}
    if any(item and item != old_equipment.get(slot) for slot, item in new_equipment.items()):
        return True
    no_worse = all(
        not isinstance(before[key], (int, float)) or not isinstance(after[key], (int, float)) or after[key] >= before[key]
        for key in ("health", "food", "oxygen")
    )
    return no_worse and (
        before["position"] != after["position"]
        or before["nearby_blocks"] != after["nearby_blocks"]
        or before["nearby_entities"] != after["nearby_entities"]
    )


def _gameplay_evidence(observation: dict[str, Any]) -> dict[str, Any]:
    state = _gameplay_state(observation)
    hazards = [
        block for block in state["nearby_blocks"]
        if any(marker in (block.get("name") or "") for marker in ("lava", "fire", "magma", "cactus", "berry", "powder_snow", "water"))
    ][:12]
    return {
        "observed_at": observation.get("observed_at"),
        **{key: state[key] for key in ("dimension", "position", "yaw", "pitch", "health", "food", "oxygen", "equipment", "inventory")},
        "nearby_entities": state["nearby_entities"][:8], "nearby_hazards": hazards,
    }


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

    def serve(self) -> None:
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
        self._write_service_state()
        server.serve_forever(poll_interval=0.25)

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
        if method == "set_observer":
            return self.set_observer(str(params.get("username", "")))
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
            deadline = time.monotonic() + 20
            while True:
                try:
                    return run.bridge.request(bridge_method, params.get("arguments") or {}, request_timeout=20)
                except TimeoutError as exc:
                    if "bridge is busy; bounded request was deferred" not in str(exc) or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
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
        temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
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
                recent_events = self._recent_events(run)
                checkpoint = run.last_saved_checkpoint or next((
                    event for event in reversed(recent_events)
                    if event.get("type") == "run_paused" and (event.get("payload") or {}).get("saved")
                ), None)
                plan_path = run.run_directory / "agent-workspace" / "plan.md"
                try:
                    with plan_path.open(encoding="utf-8") as plan_file:
                        plan = plan_file.read(8_001)
                    if len(plan) > 8_000:
                        plan = plan[:8_000]
                        plan += "\n… plan truncated; edit plan.md to keep the live plan concise"
                except OSError:
                    plan = ""
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
                    "observer_username": run.observer_username,
                    "containerized": run.containerized,
                    "docker_network": run.docker_network,
                    "plan": plan,
                    "last_saved_checkpoint": checkpoint,
                    "supervisor": run.supervisor.status(),
                    "recent_events": recent_events,
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
    def _recent_events(run: ActiveRun, limit: int = 100) -> list[dict[str, Any]]:
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
                    tail.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return tail[-limit:]

    def start_run(self, kind: str) -> dict[str, Any]:
        if kind not in {"diamond_pilot", "dragon_proof"}:
            raise ValueError("kind must be diamond_pilot or dragon_proof")
        auth = self.pi.request("status", timeout=15).get("auth") or {}
        selection = auth.get("account_model_catalog") or {}
        model = selection.get("chosen")
        if not auth.get("connected") or not model:
            raise RuntimeError("Connect with ChatGPT and verify an account model before starting a run.")
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
                seed=0, bind_address=tailscale_address(), server_port=int(os.getenv("MINECRAFT_SERVER_PORT", "25565")),
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
            container_status = ensure_runtime_images()
            if not container_status.get("available"):
                raise RuntimeError(f"Docker runtime unavailable: {container_status.get('reason', 'pinned runtime is unavailable')}")
            run_id = "run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
            world_id = "world-" + uuid.uuid4().hex[:12]
            run_dir = self.runtime / "runs" / run_id
            server_dir = self.runtime / "worlds" / run_id / "server"
            run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
            server_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
            bind = tailscale_address()
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
            socket_path = run_dir / ".runtime" / "mineflayer.sock"
            socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            workspace_root = run_dir / "agent-workspace"
            workspace = Workspace(workspace_root, events=run.events, bridge_socket=socket_path, shared_skills=self.runtime / "skills-library")
            if not workspace.sandbox.available:
                raise RuntimeError(f"workspace sandbox unavailable: {workspace.sandbox.reason}")
            workspace.initialize(
                mineflayer_docs=self.repo_root / "bridge/node_modules/mineflayer/docs",
                pathfinder_docs=self.repo_root / "bridge/node_modules/mineflayer-pathfinder/readme.md",
            )
            if goal == "dragon_kill":
                workspace.execute("write", {"path": "plan.md", "content": (
                    "# Goal: Beat the Ender Dragon\n\n"
                    "## Milestones (adapt to this world; check only after server evidence)\n"
                    "- [ ] Secure an Overworld spawn and recovery supplies\n"
                    "- [ ] Prepare gear, food, and supplies for the Nether\n"
                    "- [ ] Gather blaze rods and ender pearls\n"
                    "- [ ] Locate the stronghold and prepare the portal\n"
                    "- [ ] Enter the End prepared and defeat the Dragon\n"
                    "- [ ] Verify the server's `minecraft:end/kill_dragon` advancement\n\n"
                    "## Current step\nObserve the actual world and choose a safe first step.\n\n"
                    "## Last verified progress\nNone yet.\n\n"
                    "## Blockers and recovery\nNone recorded.\n"
                )})
            run.workspace = workspace
            run.docker_network = create_private_network(run_id)
            run.containerized = True
            with self.lock:
                self.active = run
                self._write_service_state()
            run.manifest = {
                "schema_version": 2, "run_id": run_id, "world_id": world_id,
                "created_at": run.created_at, "kind": kind, "goal": goal,
                "objective": objective, "max_active_hours": run.duration_hours,
                "model": selected_model,
                "model_provider": "OpenAI Sign in with ChatGPT OAuth through Pi",
                "billing_policy": "ChatGPT subscription only; API-key fallback disabled",
                "game": {"minecraft_version": config.minecraft_version, "difficulty": "normal", "world_default_gamemode": "spectator", "agent_gamemode": "survival", "world_seed": seed, "server_bind": bind, "server_port": server_port, "online_mode": False, "server_jar_sha1": None},
                "viewer": {"url": run.viewer_url, "bind_address": bind, "port": viewer_port, "renderer": "Prismarine Viewer 1.33.0", "first_person": True, "browser_capture": "384x216 PNG; one current image per model turn; previous image pixels omitted"},
                "dependencies": source_identity(self.repo_root),
                "runtime": {
                    "containers": container_status,
                    "containerized": run.containerized,
                    "private_network": run.docker_network,
                    "gameplay_program_boundary": "bot process, Mineflayer program runner, and workspace are in the same unprivileged container; only the vanilla-server private network is attached",
                },
                "limits": {"model_context_window": selected_model.get("context_window"), "model_output_limit": selected_model.get("max_output_tokens"), "artificial_context_cap": None},
            }
            self._write_manifest(run)
            run.events.append("run_created", {"manifest": run.manifest})
            run.supervisor.transition(RunState.MODELS_READY)
            self._set_phase(run, "downloading_server")
            from scripts.server import download_server, prepare_server
            server_cache = self.runtime / "server-cache"
            jar = download_server(config.minecraft_version, server_cache)
            local_jar = server_dir / jar.name
            shutil.copy2(jar, local_jar)
            prepare_server(server_dir, seed=seed, bind_address="0.0.0.0", port=25565)
            run.manifest["game"]["server_jar_sha1"] = self._sha1(local_jar)
            self._write_manifest(run)
            memory_mb = int(os.getenv("MINECRAFT_SERVER_HEAP_MB", "4096"))
            server_argv = server_container_command(
                run_id=run_id, network=run.docker_network, server_directory=server_dir,
                memory_mb=memory_mb,
            )
            run.supervisor.start(
                "minecraft-server", server_argv,
                cwd=server_dir, env={"RUN_ID": run_id}, ready_pattern=r"Done \(.*\)! For help", timeout=240,
            )
            run.supervisor.transition(RunState.WORLD_READY)
            self._set_phase(run, "connecting_bot")
            bridge_env = {
                "PATH": os.getenv("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(run_dir / ".runtime"),
                "LANG": "C.UTF-8", "RUN_ID": run_id, "MINECRAFT_VERSION": config.minecraft_version,
                "MINECRAFT_HOST": bind, "MINECRAFT_PORT": str(server_port), "MINECRAFT_ALLOW_TAILSCALE_HOST": "true",
                "MINECRAFT_AGENT_SOCKET": str(socket_path), "MINECRAFT_VIEWER_ENABLED": "true",
                "MINECRAFT_VIEWER_HOST": bind, "MINECRAFT_VIEWER_PORT": str(viewer_port), "MINECRAFT_VIEWER_DISTANCE": "6",
            }
            bridge_env.pop("OPENAI_API_KEY", None)
            bridge_command = bot_container_command(
                run_id=run_id, network=run.docker_network, repo_root=self.repo_root,
                run_directory=run_dir, workspace=workspace_root,
                minecraft_version=config.minecraft_version,
            )
            bridge_env = {"PATH": os.getenv("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(run_dir / ".runtime"), "LANG": "C.UTF-8"}
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
            run.events.append("bot_connected", {"username": observation.get("player", {}).get("username"), "minecraft_version": config.minecraft_version, "player_uuid": bridge.ready_event.get("player_uuid") if bridge.ready_event else None})
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
            self.agent_bridge_request({"run_id": run.run_id, "method": "release_controls"})
        run.supervisor.send_command("minecraft-server", "tick freeze")
        run.supervisor.send_command("minecraft-server", "save-all flush")
        self.pi.request("pause_run", {"run_id": run.run_id}, timeout=10)
        with self.lock:
            if run.running_since is not None:
                run.active_seconds_at_pause += time.monotonic() - run.running_since
                run.running_since = None
            run.state = "paused"
            run.phase = "paused"
            if run.supervisor.state == RunState.RUNNING:
                run.supervisor.transition(RunState.PAUSED)
            run.last_saved_checkpoint = run.events.append("run_paused", {"frozen": True, "saved": True, "active_elapsed_seconds": round(run.active_seconds_at_pause, 2)})
            self._write_service_state()
        return {"status": "paused", "world_frozen": True, "saved": True}

    def resume_run(self) -> dict[str, Any]:
        run = self.active
        if not run or run.state != "paused":
            raise RuntimeError("there is no paused world to resume")
        try:
            self.pi.request("resume_run", {"run_id": run.run_id}, timeout=10)
        except RuntimeError as exc:
            if not str(exc).endswith("active Pi run not found"):
                raise
            remaining = max(0.0, run.duration_hours * 3600 - run.active_seconds_at_pause)
            self.pi.request("start_run", {
                "run_id": run.run_id, "kind": run.kind, "goal": run.goal, "objective": run.objective,
                "workspace": str(run.run_directory / "agent-workspace"), "run_directory": str(run.run_directory),
                "bridge_socket": str(run.run_directory / ".runtime/mineflayer.sock"), "viewer_url": run.viewer_url,
                "duration_seconds": remaining, "model_id": run.model.get("id"),
            }, timeout=30)
        run.supervisor.send_command("minecraft-server", "tick unfreeze")
        if run.supervisor.state == RunState.PAUSED:
            run.supervisor.transition(RunState.RUNNING)
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
                except Exception:
                    pass
                try:
                    run.bridge.stop()
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
            self._build_video(run)
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
                except Exception:
                    pass
                try:
                    run.bridge.stop()
                except Exception:
                    pass
            run.supervisor.stop_minecraft_server(grace_seconds=15)
            run.supervisor.stop_all()
            if run.containerized:
                cleanup_run_containers(run.run_id)
            self._build_video(run)
        except Exception as exc:
            run.error = (run.error or "") + f"; cleanup: {str(exc)[:200]}"
        finally:
            try:
                remove_private_network(run.docker_network)
            except Exception:
                pass
            run.docker_network = None

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
        gameplay = None
        command = []
        if name == "bash":
            try:
                command = shlex.split(str(arguments.get("command", "")))
            except ValueError:
                command = []
            if command and Path(command[0]).name == "mc-run" and len(command) > 1:
                gameplay = self._begin_gameplay_action(run, command)
        result = run.workspace.execute(name, arguments)
        if name == "bash":
            try:
                response = json.loads(result.get("stdout", ""))
                if not isinstance(response, dict):
                    response = {}
            except (ValueError, TypeError):
                response = {}
            timed_out = result.get("status") == "timeout" or response.get("status") == "timeout"
            if command and Path(command[0]).name == "mc-run" and timed_out:
                self._fail_timed_out_program(run, response)
            if gameplay:
                after = None
                if not timed_out:
                    try:
                        after = run.bridge.request("observe", {}, request_timeout=20)
                    except Exception as exc:
                        run.events.append("gameplay_action_observation_failed", {"script": gameplay["script"], "error": str(exc)[:300]})
                after_state = _gameplay_state(after) if after else None
                state_progress = _has_gameplay_progress(gameplay["state"], after_state) if after_state is not None else None
                run.last_gameplay_action = {"signature": gameplay["signature"], "state_progress": state_progress}
                run.events.append("gameplay_action", {
                    "script": gameplay["script"], "script_sha256": gameplay["script_sha256"],
                    "state_before_sha256": gameplay["state_sha256"],
                    "state_after_sha256": _state_sha256(after_state) if after_state is not None else None,
                    "state_before": gameplay["evidence"],
                    "state_after": _gameplay_evidence(after) if after else None,
                    "state_progress": state_progress,
                    "progress_basis": "observable state heuristic, not milestone verification",
                    "action_signature": gameplay["signature"],
                    "preconditions_sha256": gameplay["preconditions_sha256"],
                    "status": response.get("status") or result.get("status") or ("completed" if result.get("exit_code") == 0 else "failed"),
                })
        return result

    @staticmethod
    def _begin_gameplay_action(run: ActiveRun, command: list[str]) -> dict[str, Any] | None:
        # shortcut: only direct mc-run commands are tracked, broaden if compound shell calls become supported.
        if not run.workspace or not run.bridge:
            raise RuntimeError("cannot check gameplay action without the live workspace and bot")
        try:
            script_path = run.workspace._resolve(command[1])
            if not script_path.is_file():
                return None
            with script_path.open("rb") as script_file:
                script_sha256 = hashlib.file_digest(script_file, "sha256").hexdigest()
            before = run.bridge.request("observe", {}, request_timeout=20)
        except Exception as exc:
            raise RuntimeError(f"cannot check gameplay action before running it: {str(exc)[:250]}") from exc
        state = _gameplay_state(before)
        state_sha256 = _state_sha256(state)
        preconditions_sha256 = _state_sha256(_gameplay_preconditions(state))
        signature = hashlib.sha256(f"{script_sha256}:{preconditions_sha256}".encode()).hexdigest()
        previous = run.last_gameplay_action
        if previous and previous.get("signature") == signature and previous.get("state_progress") is False:
            run.events.append("gameplay_action_blocked", {"script": command[1], "script_sha256": script_sha256, "state_sha256": state_sha256})
            raise RuntimeError("repeated gameplay action blocked: no observable state progress; change the script or relevant prerequisites before retrying")
        return {
            "script": command[1], "script_sha256": script_sha256, "state_sha256": state_sha256,
            "signature": signature, "preconditions_sha256": preconditions_sha256,
            "state": state, "evidence": _gameplay_evidence(before),
        }

    def _fail_timed_out_program(self, run: ActiveRun, response: dict[str, Any]) -> None:
        with self.lock:
            if run.finalizing:
                return
            run.finalizing = True
            run.running_since = None
            run.state = "failed"
            run.phase = "failed"
            run.error = f"Minecraft program timed out: {response.get('script') or 'workspace script'}"
            run.events.append("gameplay_program_timeout", {"script": response.get("script"), "program_id": response.get("program_id")})
            self._write_service_state()
        threading.Thread(target=self._stop_run, args=(run,), kwargs={"final_state": "failed"}, name=f"timeout-cleanup-{run.run_id}", daemon=True).start()

    def agent_event(self, params: dict[str, Any]) -> dict[str, Any]:
        run = self.active
        if not run or params.get("run_id") != run.run_id:
            raise RuntimeError("event run id is stale")
        event_type = str(params.get("type", "agent_event"))[:100]
        payload = params.get("payload") or {}
        if not isinstance(payload, dict):
            raise ValueError("event payload must be an object")
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
            run.events.append("agent_run_finished", {"status": params.get("status"), "active_elapsed_seconds": params.get("active_elapsed_seconds"), "turns": params.get("turns")})
            self._write_service_state()
        return {"status": run.state}

    def set_observer(self, username: str) -> dict[str, Any]:
        run = self.active
        if not run or run.state not in self.ACTIVE_STATES:
            raise RuntimeError("there is no active world")
        if not username or len(username) > 16 or not username.replace("_", "").isalnum() or username.casefold() == "agent":
            raise ValueError("enter the Mac player's Minecraft username (1–16 letters, digits, or underscores)")
        run.supervisor.send_command("minecraft-server", f"gamemode spectator {username}")
        run.observer_username = username
        run.events.append("observer_spectator_requested", {"username": username})
        return {"status": "spectator_command_sent", "username": username}

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
                        archive.write(file, (Path(root_name) / relative).as_posix())
        os.replace(temp, target)
        with target.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if run:
            run.events.append("evidence_exported", {"path": str(target), "sha256": digest, "bytes": target.stat().st_size})
        return {"path": str(target), "sha256": digest, "bytes": target.stat().st_size}

    @staticmethod
    def _build_video(run: ActiveRun) -> dict[str, Any]:
        frames = run.run_directory / "media" / "frames"
        if not frames.is_dir() or not list(frames.glob("frame-*.png")):
            return {"status": "unavailable", "reason": "no Prismarine Viewer frames were captured"}
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return {"status": "unavailable", "reason": "ffmpeg is not installed; individual evidence frames remain available"}
        output = run.run_directory / "media" / "practice.mp4"
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-framerate", "1", "-pattern_type", "glob", "-i", str(frames / "frame-*.png"), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output)],
            capture_output=True, text=True, timeout=300, check=False,
        )
        if result.returncode:
            return {"status": "failed", "error": result.stderr[-500:]}
        return {"status": "ready", "path": str(output), "bytes": output.stat().st_size, "frame_count": len(list(frames.glob("frame-*.png"))), "fps": 1}


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
