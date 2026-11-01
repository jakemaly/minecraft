"""Molab lifecycle for local Nemotron, the existing Pi harness, and Minecraft."""

from __future__ import annotations

import json
import atexit
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .model_runtime import (
    LOCAL_PROVIDER_ID,
    LOCAL_VLLM_BASE_URL,
    NEMOTRON_MAX_MODEL_LEN,
    NEMOTRON_MAX_TOKENS,
    NEMOTRON_MODEL,
    NEMOTRON_REVISION,
    download_nemotron,
    inference_config,
    probe_nemotron,
    start_nemotron,
)
from .supervisor import Supervisor


class MolabRuntime:
    """Owns Molab-only preparation and delegates gameplay to the shared RunDaemon."""

    def __init__(self, repo_root: str | Path, runtime: str | Path | None = None):
        self.repo_root = Path(repo_root).resolve()
        self.runtime = Path(runtime or (Path.home() / ".local/share/minecraft-agent/molab")).expanduser().resolve()
        self.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runtime.chmod(0o700)
        self.model_cache = self.runtime / "models-cache"
        self.vllm_supervisor: Supervisor | None = None
        self.daemon = None
        self._daemon_thread: threading.Thread | None = None
        self._daemon_error: str | None = None
        self._model_start_thread: threading.Thread | None = None
        self._model_start_error: str | None = None
        self._model_stop_requested = False
        self._lock = threading.RLock()
        self.reports: dict[str, dict[str, Any]] = {}
        atexit.register(self.close)

    def prepare_runtime(self) -> dict[str, Any]:
        """Install locked Node dependencies and a local Chromium for viewer captures."""
        required = ("node", "npm", "java", "uv", "nvidia-smi")
        missing = [command for command in required if shutil.which(command) is None]
        if missing:
            raise RuntimeError(f"Molab runtime is missing required commands: {', '.join(missing)}")
        node_version = subprocess.run(["node", "--version"], capture_output=True, text=True, check=True).stdout.strip()
        major = int(node_version.removeprefix("v").split(".", 1)[0])
        if major < 22:
            raise RuntimeError(f"Pi requires Node.js 22 or newer; Molab has {node_version}")
        java_version = self._version(["java", "-version"])
        java_major = self._java_major(java_version)
        if java_major < 21:
            raise RuntimeError(f"Minecraft 1.21.4 requires Java 21 or newer; Molab has {java_version}")
        gpu = self._run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader"],
            cwd=self.repo_root,
            timeout=10,
        ).strip().splitlines()
        if not gpu:
            raise RuntimeError("nvidia-smi did not report a Molab GPU")

        bridge = self.repo_root / "bridge"
        if not (bridge / "package-lock.json").is_file() or not (bridge / "package.json").is_file():
            raise FileNotFoundError(f"the packaged Pi/Mineflayer bridge is missing from {bridge}")
        pi_package = bridge / "node_modules/@earendil-works/pi-coding-agent/package.json"
        if not pi_package.is_file():
            self._run(["npm", "ci"], cwd=bridge, timeout=1800)

        browser_cache = self.runtime / "browsers"
        browser_cache.mkdir(parents=True, exist_ok=True)
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_cache)
        try:
            import playwright.async_api
        except ImportError as exc:
            raise RuntimeError("the notebook environment must include its pinned Playwright dependency") from exc
        browser_env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(browser_cache)}
        self._run([sys.executable, "-m", "playwright", "install", "chromium"], cwd=self.repo_root, env=browser_env, timeout=1200)
        smoke_script = (
            "import asyncio\n"
            "from playwright.async_api import async_playwright\n"
            "async def main():\n"
            "    async with async_playwright() as playwright:\n"
            "        browser = await playwright.chromium.launch(headless=True, args=['--no-sandbox', '--disable-gpu-sandbox', '--disable-dev-shm-usage', '--enable-webgl', '--enable-unsafe-swiftshader', '--use-gl=angle', '--use-angle=swiftshader'])\n"
            "        page = await browser.new_page()\n"
            "        await page.set_content('<canvas id=\\\"viewer-smoke\\\"></canvas>')\n"
            "        webgl = await page.evaluate('document.getElementById(\\\"viewer-smoke\\\").getContext(\\\"webgl\\\") !== null')\n"
            "        if not webgl: raise RuntimeError('Chromium opened but SwiftShader WebGL is unavailable')\n"
            "        print(playwright.chromium.executable_path)\n"
            "        await browser.close()\n"
            "asyncio.run(main())\n"
        )
        smoke_result = self._run([sys.executable, "-c", smoke_script], cwd=self.repo_root, env=browser_env, timeout=120)
        executable = Path(smoke_result.strip().splitlines()[-1])
        if not executable.is_file():
            raise RuntimeError(f"Playwright did not install Chromium at {executable}")
        os.environ["MINECRAFT_CHROME"] = str(executable)
        self.reports["runtime"] = {
            "status": "ready", "node": node_version, "java": java_version, "gpu": gpu,
            "bridge": str(bridge), "browser": str(executable),
            "inference_endpoint_scope": "127.0.0.1 inside Molab",
        }
        return self.reports["runtime"]

    def download_model(self, token: str | None = None) -> dict[str, Any]:
        self.model_cache.mkdir(parents=True, exist_ok=True)
        if self._downloaded_snapshot() is None:
            free_bytes = shutil.disk_usage(self.model_cache).free
            if free_bytes < 80 * 1024**3:
                raise RuntimeError(f"Nemotron needs about 75 GB; only {free_bytes / 1024**3:.1f} GiB is free in {self.model_cache}")
        snapshot = download_nemotron(self.model_cache, token)
        result = {"status": "downloaded", "model": NEMOTRON_MODEL, "revision": NEMOTRON_REVISION, "path": str(snapshot)}
        self.reports["model_download"] = result
        return result

    def start_model(self) -> dict[str, Any]:
        with self._lock:
            if self._model_start_thread and self._model_start_thread.is_alive():
                return {"status": "starting", "provider": LOCAL_PROVIDER_ID, "model": NEMOTRON_MODEL}
            if self.vllm_supervisor and self._model_process_running():
                try:
                    return self.probe_model()
                except Exception:
                    # Recover readiness checks if the notebook request was interrupted while vLLM kept loading.
                    self._model_start_error = None
                    self.reports["vllm"] = {"status": "starting", "provider": LOCAL_PROVIDER_ID, "model": NEMOTRON_MODEL}
                    thread = threading.Thread(target=self._watch_model, args=(self.vllm_supervisor,), name="molab-vllm-probe", daemon=True)
                    self._model_start_thread = thread
                    thread.start()
                    return {"status": "starting", "provider": LOCAL_PROVIDER_ID, "model": NEMOTRON_MODEL}
            snapshot = self._downloaded_snapshot()
            if snapshot is None:
                raise RuntimeError("download the pinned Nemotron snapshot before starting vLLM")
            run_dir = self.runtime / "vllm-server"
            supervisor = Supervisor(run_dir, "molab-vllm")
            self.vllm_supervisor = supervisor
            self._model_start_error = None
            self._model_stop_requested = False
            thread = threading.Thread(
                target=self._launch_model,
                args=(supervisor, run_dir, snapshot),
                name="molab-vllm-start",
                daemon=True,
            )
            self._model_start_thread = thread
            thread.start()
            return {"status": "starting", "provider": LOCAL_PROVIDER_ID, "model": NEMOTRON_MODEL}

    def _launch_model(self, supervisor: Supervisor, run_dir: Path, snapshot: Path) -> None:
        try:
            start_nemotron(supervisor, run_dir, snapshot)
            if self._model_stop_requested:
                supervisor.stop_all()
                return
            result = probe_nemotron(timeout=15)
            result["process"] = supervisor.status().get("processes", {}).get("nemotron", {})
            with self._lock:
                if self.vllm_supervisor is supervisor and not self._model_stop_requested:
                    self.reports["vllm"] = result
                    self._model_start_error = None
        except Exception as exc:
            supervisor.stop_all()
            with self._lock:
                if self.vllm_supervisor is supervisor and not self._model_stop_requested:
                    self._model_start_error = f"{type(exc).__name__}: {str(exc)[:500]}"
                    self.reports["vllm"] = {"status": "error", "provider": LOCAL_PROVIDER_ID, "error": self._model_start_error}
                    self.vllm_supervisor = None
        finally:
            with self._lock:
                if self._model_start_thread is threading.current_thread():
                    self._model_start_thread = None

    def _watch_model(self, supervisor: Supervisor) -> None:
        deadline = time.monotonic() + 1800
        last_error = "vLLM did not report ready"
        while time.monotonic() < deadline and not self._model_stop_requested:
            if not supervisor.status().get("processes", {}).get("nemotron", {}).get("running"):
                last_error = "vLLM exited before the local model probe passed"
                break
            try:
                result = probe_nemotron(timeout=5)
                result["process"] = supervisor.status().get("processes", {}).get("nemotron", {})
                with self._lock:
                    if self.vllm_supervisor is supervisor and not self._model_stop_requested:
                        self.reports["vllm"] = result
                        self._model_start_error = None
                return
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {str(exc)[:400]}"
                time.sleep(1)
        if self._model_stop_requested:
            return
        supervisor.stop_all()
        with self._lock:
            if self.vllm_supervisor is supervisor:
                self._model_start_error = last_error
                self.reports["vllm"] = {"status": "error", "provider": LOCAL_PROVIDER_ID, "error": last_error}
                self.vllm_supervisor = None
            if self._model_start_thread is threading.current_thread():
                self._model_start_thread = None

    def probe_model(self) -> dict[str, Any]:
        result = probe_nemotron()
        self.reports["vllm"] = {**result, "process": self._model_process_status()}
        return self.reports["vllm"]

    def stop_model(self) -> dict[str, Any]:
        with self._lock:
            daemon_status = self.daemon.status() if self.daemon else {}
            active_run = daemon_status.get("active_run")
            if active_run and active_run.get("state") in {"launching", "probing_model", "running", "paused"}:
                raise RuntimeError("stop or finish the active Minecraft run before freeing the model GPU memory")
            supervisor = self.vllm_supervisor
            self._model_stop_requested = True
        if supervisor:
            supervisor.stop_all()
        with self._lock:
            if self.vllm_supervisor is supervisor:
                self.vllm_supervisor = None
            result = {"status": "stopped", "provider": LOCAL_PROVIDER_ID}
            self.reports["vllm"] = result
            return result

    def start_harness(self) -> dict[str, Any]:
        self.probe_model()
        if not os.environ.get("MINECRAFT_CHROME"):
            raise RuntimeError("prepare the Molab runtime to install Chromium before starting the harness")
        if self.daemon is not None:
            return self._harness_status()
        os.environ.update({
            "MINECRAFT_AGENT_REPO_ROOT": str(self.repo_root),
            "MINECRAFT_AGENT_HOME": str(self.runtime / "agent"),
            "MINECRAFT_MOLAB_MODE": "1",
            "MINECRAFT_LOCAL_MODEL_PROVIDER": LOCAL_PROVIDER_ID,
            "MINECRAFT_LOCAL_MODEL_ID": NEMOTRON_MODEL,
            "MINECRAFT_LOCAL_MODEL_BASE_URL": LOCAL_VLLM_BASE_URL,
            "MINECRAFT_LOCAL_MODEL_REVISION": NEMOTRON_REVISION,
            "MINECRAFT_LOCAL_MODEL_CONTEXT_WINDOW": str(NEMOTRON_MAX_MODEL_LEN),
            "MINECRAFT_LOCAL_MODEL_MAX_TOKENS": str(NEMOTRON_MAX_TOKENS),
            "MINECRAFT_DASHBOARD_HOST": "127.0.0.1",
            "PYTHONPATH": os.pathsep.join(filter(None, (str(self.repo_root / "src"), str(self.repo_root)))),
        })
        from .daemon import RunDaemon

        daemon = RunDaemon(repo_root=self.repo_root)
        self.daemon = daemon

        def serve_control() -> None:
            try:
                daemon.serve(include_dashboard=False)
            except Exception as exc:
                self._daemon_error = f"{type(exc).__name__}: {str(exc)[:400]}"

        self._daemon_thread = threading.Thread(target=serve_control, name="molab-minecraft-control", daemon=True)
        self._daemon_thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self._daemon_error:
                self.close_harness()
                raise RuntimeError(self._daemon_error)
            if daemon.socket_path.exists():
                break
            time.sleep(0.05)
        else:
            self.close_harness()
            raise TimeoutError("Molab control socket did not start")
        status = self._harness_status()
        if not status.get("chatgpt_auth", {}).get("connected"):
            self.close_harness()
            raise RuntimeError(status.get("chatgpt_auth", {}).get("error") or "local vLLM provider did not initialize in Pi")
        return status

    def start_run(self, kind: str) -> dict[str, Any]:
        if self.daemon is None:
            raise RuntimeError("initialize the harness before starting a Minecraft run")
        self.probe_model()
        return self.daemon.start_run(kind)

    def pause_run(self) -> dict[str, Any]:
        return self._require_daemon().pause_run()

    def resume_run(self) -> dict[str, Any]:
        return self._require_daemon().resume_run()

    def stop_run(self) -> dict[str, Any]:
        return self._require_daemon().stop_run()

    def export_run(self) -> dict[str, Any]:
        return self._require_daemon().export_run()

    def status(self) -> dict[str, Any]:
        model_status = self._model_process_status()
        try:
            model_probe = probe_nemotron()
        except Exception as exc:
            model_probe = {"status": "unavailable", "error": str(exc)[:400], "endpoint": LOCAL_VLLM_BASE_URL}
        try:
            harness = self._harness_status() if self.daemon else None
        except Exception as exc:
            harness = {"error": f"{type(exc).__name__}: {str(exc)[:400]}"}
        return {"model_process": model_status, "model": model_probe, "harness": harness, "runtime": self.reports.get("runtime")}

    def latest_frame(self) -> Path | None:
        daemon = self.daemon
        if daemon is None:
            return None
        run = daemon.active
        run_dir = run.run_directory if run and run.run_directory.is_dir() else None
        if run_dir is None and daemon.previous_run:
            run_dir = Path(daemon.previous_run.get("run_directory", ""))
        if run_dir is None:
            return None
        event_path = run_dir / "events/events.jsonl"
        if not event_path.is_file():
            return None
        try:
            for line in reversed(event_path.read_text(encoding="utf-8").splitlines()):
                event = json.loads(line)
                if event.get("type") != "viewer_frame":
                    continue
                relative = Path(str(event.get("payload", {}).get("path", "")))
                if relative.is_absolute() or ".." in relative.parts:
                    return None
                frame = (run_dir / relative).resolve()
                if frame.parent == (run_dir / "media/frames").resolve() and frame.is_file():
                    return frame
        except (OSError, json.JSONDecodeError):
            return None
        return None

    def close_harness(self) -> None:
        if self.daemon:
            self.daemon.close()
            self.daemon = None
        if self._daemon_thread and self._daemon_thread.is_alive():
            self._daemon_thread.join(timeout=5)
        self._daemon_thread = None

    def close(self) -> None:
        self.close_harness()
        if self.vllm_supervisor:
            try:
                self.vllm_supervisor.stop_all()
            finally:
                self.vllm_supervisor = None

    def _require_daemon(self):
        if self.daemon is None:
            raise RuntimeError("initialize the harness first")
        return self.daemon

    def _harness_status(self) -> dict[str, Any]:
        if self.daemon is None:
            return {}
        return self.daemon.status()

    def _downloaded_snapshot(self) -> Path | None:
        snapshot = (
            self.model_cache
            / f"models--{NEMOTRON_MODEL.replace('/', '--')}"
            / "snapshots"
            / NEMOTRON_REVISION
        )
        return snapshot if snapshot.is_dir() and (snapshot / "config.json").is_file() else None

    def _model_process_running(self) -> bool:
        return bool(self._model_process_status().get("running"))

    def _model_process_status(self) -> dict[str, Any]:
        if not self.vllm_supervisor:
            return {"running": False}
        return self.vllm_supervisor.status().get("processes", {}).get("nemotron", {"running": False})

    @staticmethod
    def _run(command: list[str], *, cwd: Path, timeout: float, env: dict[str, str] | None = None) -> str:
        result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False)
        if result.returncode:
            output = (result.stderr or result.stdout)[-1600:]
            raise RuntimeError(f"{command[0]} failed ({result.returncode}): {output}")
        return result.stdout

    @classmethod
    def _version(cls, command: list[str]) -> str:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        return (result.stdout or result.stderr).splitlines()[0][:200] if (result.stdout or result.stderr) else "unknown"

    @staticmethod
    def _java_major(version: str) -> int:
        import re

        match = re.search(r'version "(?:1\.)?(\d+)', version)
        return int(match.group(1)) if match else 0
