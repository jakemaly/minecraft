"""Notebook-controlled fresh-world comparison trials."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class TrialRuntime:
    """Own one background attempt and its run-scoped model, world, and workspace."""

    def __init__(self, repo_root: str | Path = "/marimo/minecraft"):
        self.repo_root = Path(repo_root).resolve()

    @staticmethod
    def _ensure_session_fields(session) -> None:
        defaults = {
            "agent_loop": None,
            "agent_thread": None,
            "current_image_jpeg": None,
            "current_image_meta": {},
            "long_viewer_widget": None,
            "long_viewer_run_id": None,
            "long_stop_requested": False,
            "long_pause_requested": False,
        }
        if not hasattr(session, "lock"):
            session.lock = threading.RLock()
        for name, initial in defaults.items():
            if not hasattr(session, name):
                setattr(session, name, initial)

    def start(self, session, mode: str) -> dict[str, Any]:
        if mode not in {"nemotron_only", "nemotron_clef"}:
            raise ValueError("mode must be nemotron_only or nemotron_clef")
        self._ensure_session_fields(session)
        if session.agent_thread and session.agent_thread.is_alive():
            raise RuntimeError("the prior attempt is still running")
        if session.supervisor and any(item["running"] for item in session.supervisor.status().get("processes", {}).values()):
            raise RuntimeError("stop the previous attempt before starting another fresh world")
        session.long_stop_requested = False
        session.long_pause_requested = False
        session.reports["long_run"] = {"status": "launching", "mode": mode}
        thread = threading.Thread(target=self._run, args=(session, mode), name=f"long-horizon-{mode}", daemon=True)
        session.agent_thread = thread
        thread.start()
        return dict(session.reports["long_run"])

    @staticmethod
    def pause(session) -> dict[str, Any]:
        TrialRuntime._ensure_session_fields(session)
        session.long_pause_requested = True
        if session.agent_loop:
            session.agent_loop.pause()
            from .supervisor import RunState
            if session.supervisor and session.supervisor.state == RunState.RUNNING:
                session.supervisor.transition(RunState.PAUSED)
        result = {"status": "pause_requested"}
        session.reports["long_control"] = result
        return result

    @staticmethod
    def resume(session) -> dict[str, Any]:
        TrialRuntime._ensure_session_fields(session)
        session.long_pause_requested = False
        if session.agent_loop:
            session.agent_loop.resume()
            from .supervisor import RunState
            if session.supervisor and session.supervisor.state == RunState.PAUSED:
                session.supervisor.transition(RunState.RUNNING)
        result = {"status": "resume_requested"}
        session.reports["long_control"] = result
        return result

    @staticmethod
    def stop(session) -> dict[str, Any]:
        TrialRuntime._ensure_session_fields(session)
        session.long_stop_requested = True
        session.long_pause_requested = False
        if session.agent_loop:
            session.agent_loop.stop()
        try:
            if session.bridge:
                session.bridge.stop()
                session.bridge = None
            if session.supervisor:
                session.supervisor.stop_minecraft_server()
                session.supervisor.stop_all()
            result = {"status": "stop_requested"}
        except Exception as exc:
            result = {"status": "stop_error", "message": str(exc)[:300]}
        session.reports["long_control"] = result
        return result

    def _run(self, session, mode: str) -> None:
        from .agent_loop import AgentLoop, OBJECTIVE
        from .bridge_client import BridgeClient
        from .config import RunConfig, runtime_root
        from .events import EventLog
        from .identity import source_identity
        from .model_runtime import NEMOTRON_GPU_MEMORY_UTILIZATION, NEMOTRON_MAX_MODEL_LEN, NEMOTRON_MAX_NUM_BATCHED_TOKENS, start_clef, start_nemotron
        from .models.local_nemotron import DEFAULT_MAX_COMPLETION_TOKENS
        from .models.local_clef import LocalClefClient
        from .models.local_nemotron import LocalNemotron
        from .persistence import _sha256
        from .preflight import ResourceSampler, summarize_resource_samples
        from .supervisor import RunState, Supervisor
        from .workspace import Workspace

        keep_open = False
        run_directory: Path | None = None
        try:
            config = RunConfig.from_environment()
            config.validate()
            runtime = runtime_root()
            run_id = "run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
            world_id = "world-" + uuid.uuid4().hex[:12]
            run_directory = runtime / "runs" / run_id
            server_directory = runtime / "worlds" / run_id / "server"
            run_directory.mkdir(parents=True, exist_ok=False)
            server_directory.mkdir(parents=True, exist_ok=False)
            session.run_id = run_id
            session.current_image_jpeg = None
            session.current_image_meta = {}
            session.events = EventLog(run_directory / "events" / "events.jsonl", run_id, world_id)
            session.supervisor = Supervisor(run_directory, run_id)
            socket_path = Path("/tmp") / f"mc-{run_id[-8:]}.sock"
            session.reports["active_server_dir"] = str(server_directory)
            session.reports["active_agent_socket"] = str(socket_path)

            hydration_path = runtime / "models-cache" / "hydration-manifest.json"
            hydration = json.loads(hydration_path.read_text(encoding="utf-8"))
            planner_info = hydration["repositories"]["planner"]
            selector_info = hydration["repositories"].get("selector")
            if mode == "nemotron_clef" and selector_info is None:
                raise RuntimeError("Clef artifacts are not present in this Nemotron-only model cache")
            previous = session.reports.get("long_comparison_runs", [])
            paired_revision_match = (
                all(item.get("model_revision") == planner_info["revision"] for item in previous)
                if mode == "nemotron_clef" else None
            )
            manifest = {
                "schema_version": 1,
                "run_id": run_id,
                "world_id": world_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "mode": mode,
                "objective": OBJECTIVE,
                "models": {
                    "planner": {key: planner_info.get(key) for key in ("repo_id", "revision", "snapshot_path", "sha256")},
                    "selector": ({key: selector_info.get(key) for key in ("repo_id", "revision", "snapshot_path", "sha256")}
                                 if selector_info is not None else None),
                },
                "inference": {
                    "max_model_len": NEMOTRON_MAX_MODEL_LEN,
                    "max_completion_tokens": DEFAULT_MAX_COMPLETION_TOKENS,
                    "max_num_batched_tokens": NEMOTRON_MAX_NUM_BATCHED_TOKENS,
                    "gpu_memory_utilization": NEMOTRON_GPU_MEMORY_UTILIZATION,
                    "max_num_seqs": 1,
                    "selector_loaded": mode == "nemotron_clef",
                },
                "game": {"minecraft_version": config.minecraft_version, "difficulty": "normal", "game_mode": "survival", "world_seed": "20261028"},
                "dependencies": source_identity(self.repo_root),
                "paired_nemotron_revision_match": paired_revision_match,
                "observation_policy": {"blocks": "Mineflayer loaded region", "prior_images_in_prompt": False,
                                       "image": "one current 384x216 JPEG appended last to each Nemotron request",
                                       "renderer": "python_voxel_raycast using Prismarine Viewer camera convention"},
            }
            self._write_json(run_directory / "manifest.json", manifest)
            session.events.append("run_created", {"manifest": manifest})
            self._raise_if_stopped(session)

            start_nemotron(session.supervisor, run_directory)
            session.planner = LocalNemotron(config.planner_url, planner_info["repo_id"])
            session.planner.artifact_revision = planner_info["revision"]
            session.selector = None
            if mode == "nemotron_clef":
                start_clef(session.supervisor, run_directory)
                session.selector = LocalClefClient()
                session.selector.model = selector_info["repo_id"]
                session.selector.artifact_revision = selector_info["revision"]
            if session.supervisor.state == RunState.PREFLIGHT:
                session.supervisor.transition(RunState.MODELS_READY)
            session.events.append("models_started", {
                "planner_revision": planner_info["revision"],
                "selector_revision": selector_info["revision"] if mode == "nemotron_clef" else None,
                "mode": mode,
            })
            self._raise_if_stopped(session)

            from scripts.server import download_server, prepare_server
            server_cache = runtime / "server-cache"
            server_cache.mkdir(parents=True, exist_ok=True)
            cached_jar = download_server(config.minecraft_version, server_cache)
            server_jar = server_directory / cached_jar.name
            shutil.copy2(cached_jar, server_jar)
            prepare_server(server_directory, seed="20261028")
            manifest["game"]["server_jar_sha1"] = hashlib.sha1(server_jar.read_bytes()).hexdigest()
            self._write_json(run_directory / "manifest.json", manifest)
            session.supervisor.start(
                "minecraft-server", ["java", "-Xms1G", "-Xmx2G", "-jar", str(server_jar), "nogui"],
                cwd=server_directory, env={"RUN_ID": run_id},
                ready_pattern=r"Done \(.*\)! For help", timeout=240,
            )
            session.supervisor.transition(RunState.WORLD_READY)
            self._raise_if_stopped(session)

            child_env = {
                "PATH": os.getenv("PATH", ""),
                "HOME": os.getenv("HOME", ""),
                "LANG": "C.UTF-8",
                "RUN_ID": run_id,
                "MINECRAFT_VERSION": config.minecraft_version,
                "MINECRAFT_HOST": "127.0.0.1",
                "MINECRAFT_PORT": "25565",
                "MINECRAFT_AGENT_SOCKET": str(socket_path),
            }
            session.bridge = BridgeClient(["node", str(self.repo_root / "bridge" / "src" / "bot.js")], cwd=self.repo_root / "bridge", env=child_env, timeout=90)
            observation = session.bridge.request("observe", {}, request_timeout=15)
            session.events.append("bot_connected", {
                "username": observation.get("player", {}).get("username"),
                "minecraft_version": session.bridge.ready_event.get("minecraft_version") if session.bridge.ready_event else config.minecraft_version,
            })

            workspace_root = run_directory / "agent-workspace"
            workspace = Workspace(workspace_root, events=session.events, bridge_socket=socket_path)
            workspace.initialize(
                mineflayer_docs=self.repo_root / "bridge/node_modules/mineflayer/docs",
                pathfinder_docs=self.repo_root / "bridge/node_modules/mineflayer-pathfinder/readme.md",
            )
            doc_revisions = json.loads((workspace_root / ".runtime/docs-manifest.json").read_text(encoding="utf-8"))
            session.events.append("agent_workspace_created", {
                "path": str(workspace_root), "objective": OBJECTIVE, "mode": mode,
                "documentation_revisions": doc_revisions,
                "tool_names": ["read", "write", "edit", "bash", "grep", "find", "ls"],
            })
            session.reports["active_workspace"] = str(workspace_root)

            def image_provider():
                from viewer.raster import publish_frame
                capture_started = time.monotonic()
                snapshot = session.bridge.request("viewer_snapshot", {}, request_timeout=2)
                image, metadata = publish_frame(session, snapshot, run_id)
                metadata["request_round_trip_ms"] = round((time.monotonic() - capture_started) * 1000, 3)
                session.reports["long_viewer"] = {
                    "status": "frame_ready", "sequence": snapshot.get("sequence"),
                    "blocks": len(snapshot.get("blocks", [])), "render_ms": metadata["render_ms"],
                    "request_round_trip_ms": metadata["request_round_trip_ms"],
                    "physics_tick_p95_ms": metadata.get("physics_tick_p95_ms"),
                    "renderer": metadata["renderer"],
                }
                session.events.append("viewer_snapshot_update", {
                    "sequence": snapshot.get("sequence"), "blocks": len(snapshot.get("blocks", [])),
                    "request_round_trip_ms": metadata["request_round_trip_ms"], "render_ms": metadata["render_ms"],
                    "capture_ms": metadata.get("capture_ms"), "physics_tick_samples": snapshot.get("physics_tick_samples"),
                    "physics_tick_p95_ms": snapshot.get("physics_tick_p95_ms"),
                })
                return image, metadata

            def update(state):
                with session.lock:
                    session.reports["long_run"] = {
                        "run_id": run_id, "world_id": world_id, "mode": mode,
                        "model_revision": planner_info["revision"],
                        "selector_revision": selector_info["revision"] if mode == "nemotron_clef" else None,
                        "paired_nemotron_revision_match": paired_revision_match,
                        **state,
                    }

            session.agent_loop = AgentLoop(
                planner=session.planner,
                selector=session.selector,
                bot=session.bridge,
                workspace=workspace,
                events=session.events,
                mode=mode,
                objective=OBJECTIVE,
                image_provider=image_provider,
                on_update=update,
            )
            session.supervisor.transition(RunState.RUNNING)
            if session.long_pause_requested:
                session.agent_loop.pause()
                session.supervisor.transition(RunState.PAUSED)
            session.reports["long_run"] = {
                "status": "running", "run_id": run_id, "world_id": world_id,
                "mode": mode, "model_revision": planner_info["revision"],
                "selector_revision": selector_info["revision"] if mode == "nemotron_clef" else None,
                "paired_nemotron_revision_match": paired_revision_match,
                "workspace": str(workspace_root),
            }

            pids = [item.process.pid for name, item in session.supervisor.processes.items() if name in {"nemotron", "clef", "minecraft-server"} and item.process.poll() is None]
            pids.append(session.bridge.process.pid)
            sampler = ResourceSampler(pids)
            sampler.start()
            try:
                result = session.agent_loop.run()
                keep_open = result.get("status") == "completed" and not session.long_stop_requested
            finally:
                resource_samples = sampler.stop()
            resource_summary = summarize_resource_samples(resource_samples)
            resource_path = run_directory / "agent-resource-samples.jsonl"
            resource_path.write_text("".join(json.dumps(item, separators=(",", ":")) + "\n" for item in resource_samples), encoding="utf-8")
            resource_hash = _sha256(resource_path)
            session.events.append("agent_resource_summary", {
                "mode": mode, "sample_count": len(resource_samples), "trace_path": str(resource_path),
                "trace_sha256": resource_hash, "summary": resource_summary,
                "session_elapsed_seconds": session.events.elapsed_seconds,
            })
            if keep_open and session.supervisor.state == RunState.RUNNING:
                session.supervisor.transition(RunState.PAUSED)

            exports = self.repo_root / "runs" / "exports"
            exports.mkdir(parents=True, exist_ok=True)
            evidence_path = shutil.make_archive(str(exports / (run_id + "-agent-trace")), "zip", root_dir=run_directory)
            evidence_hash = _sha256(Path(evidence_path))
            report = {
                "run_id": run_id, "world_id": world_id, "mode": mode,
                "model_revision": planner_info["revision"],
                "selector_revision": selector_info["revision"] if mode == "nemotron_clef" else None,
                "paired_nemotron_revision_match": paired_revision_match,
                **result, "resource_peaks": resource_summary,
                "resource_samples": len(resource_samples), "resource_trace": str(resource_path),
                "evidence_path": evidence_path, "evidence_sha256": evidence_hash,
            }
            session.reports["long_run"] = report
            session.reports.setdefault("long_comparison_runs", []).append({
                key: report.get(key) for key in (
                    "run_id", "world_id", "mode", "model_revision", "selector_revision",
                    "paired_nemotron_revision_match", "status", "turns", "elapsed_seconds",
                    "goal", "evidence_path", "evidence_sha256",
                )
            })
            session.events.append("agent_trace_exported", {"path": evidence_path, "sha256": evidence_hash, "status": result.get("status")})
        except Exception as exc:
            status = "stopped" if session.long_stop_requested else "failed"
            report = {"status": status, "mode": mode, "error_type": type(exc).__name__, "message": str(exc)[:500]}
            session.reports["long_run"] = report
            if session.events:
                session.events.append("long_run_error", report)
            session.reports.setdefault("long_comparison_runs", []).append(report)
        finally:
            if not keep_open or session.long_stop_requested:
                try:
                    if session.bridge:
                        session.bridge.stop()
                        session.bridge = None
                except Exception:
                    pass
                try:
                    if session.supervisor:
                        session.supervisor.stop_minecraft_server()
                        session.supervisor.stop_all()
                except Exception as exc:
                    session.reports["long_cleanup_error"] = str(exc)[:300]

    @staticmethod
    def _raise_if_stopped(session) -> None:
        if session.long_stop_requested:
            raise InterruptedError("operator stopped this attempt")

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        temp.replace(path)
