"""Explicit, idempotent process lifecycle for a single run."""

from __future__ import annotations

import json
import atexit
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO


class RunState(StrEnum):
    PREFLIGHT = "preflight"
    MODELS_READY = "models_ready"
    WORLD_READY = "world_ready"
    RUNNING = "running"
    PAUSED = "paused"
    CHECKPOINTING = "checkpointing"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass
class ManagedProcess:
    name: str
    process: subprocess.Popen[bytes]
    log: BinaryIO
    log_path: Path
    started_at: float = field(default_factory=time.monotonic)
    ready: bool = False
    startup_output: bytearray = field(default_factory=bytearray)
    output_tail: bytearray = field(default_factory=bytearray)
    pump_thread: threading.Thread | None = None


class Supervisor:
    MAX_LOG_BYTES = 5 * 1024 * 1024
    LOG_BACKUPS = 2
    MAX_OUTPUT_TAIL_BYTES = 1024 * 1024

    def __init__(self, run_dir: str | Path, run_id: str):
        self.run_dir = Path(run_dir).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.state = RunState.PREFLIGHT
        self.processes: dict[str, ManagedProcess] = {}
        self._lock = threading.RLock()
        self._action_lock = threading.Lock()
        self._action_results: dict[str, dict] = {}
        self._cleaned_up = False
        atexit.register(self._cleanup_for_exit)
        self._write_state()

    def _cleanup_for_exit(self) -> None:
        if self._cleaned_up:
            return
        self._cleaned_up = True
        try:
            self.stop_minecraft_server(grace_seconds=20)
            self.stop_all()
        except Exception:
            pass

    def _write_state(self) -> None:
        state_file = self.run_dir / "runtime-state.json"
        temp = state_file.with_suffix(".tmp")
        data = {"run_id": self.run_id, "state": self.state.value, "processes": {name: {"pid": item.process.pid, "started_at_monotonic": item.started_at, "ready": item.ready} for name, item in self.processes.items()}}
        temp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, state_file)

    def start(self, name: str, argv: list[str], *, cwd: str | Path, env: dict[str, str] | None = None, ready_pattern: str | None = None, timeout: float = 120) -> ManagedProcess:
        with self._lock:
            if name in self.processes and self.processes[name].process.poll() is None:
                raise RuntimeError(f"{name} is already running for run {self.run_id}")
            log_path = self.run_dir / "logs" / f"{name}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log = log_path.open("ab", buffering=0)
            child_env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""), "LANG": os.environ.get("LANG", "C.UTF-8")}
            if env:
                child_env.update(env)
            read_fd, write_fd = os.pipe()
            try:
                process = subprocess.Popen(argv, cwd=Path(cwd), env=child_env, stdin=subprocess.PIPE, stdout=write_fd, stderr=subprocess.STDOUT, pass_fds=(write_fd,), start_new_session=True)
            finally:
                os.close(write_fd)
            managed = ManagedProcess(name, process, log, log_path)
            managed.pump_thread = threading.Thread(target=self._pump_output, args=(managed, read_fd), daemon=True)
            managed.pump_thread.start()
            self.processes[name] = managed
            self._write_state()
        if ready_pattern is None:
            return managed
        pattern = re.compile(ready_pattern)
        deadline = time.monotonic() + timeout
        log_file = log_path
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"{name} exited before readiness; inspect {log_path}")
            output = (managed.startup_output + managed.output_tail).decode("utf-8", errors="replace")
            if pattern.search(output):
                managed.ready = True
                self._write_state()
                return managed
            time.sleep(0.25)
        self.stop_process(name)
        raise TimeoutError(f"{name} did not become ready within {timeout:g}s; inspect {log_path}")

    def _rotate_log(self, managed: ManagedProcess) -> None:
        managed.log.close()
        oldest = managed.log_path.with_name(managed.log_path.name + f".{self.LOG_BACKUPS}")
        oldest.unlink(missing_ok=True)
        for index in range(self.LOG_BACKUPS - 1, 0, -1):
            previous = managed.log_path.with_name(managed.log_path.name + f".{index}")
            if previous.exists():
                os.replace(previous, managed.log_path.with_name(managed.log_path.name + f".{index + 1}"))
        if managed.log_path.exists():
            os.replace(managed.log_path, managed.log_path.with_name(managed.log_path.name + ".1"))
        managed.log = managed.log_path.open("wb", buffering=0)

    def _pump_output(self, managed: ManagedProcess, read_fd: int) -> None:
        try:
            with os.fdopen(read_fd, "rb", buffering=0) as reader:
                while chunk := reader.read(64 * 1024):
                    if len(managed.startup_output) < self.MAX_OUTPUT_TAIL_BYTES:
                        managed.startup_output.extend(chunk[: self.MAX_OUTPUT_TAIL_BYTES - len(managed.startup_output)])
                    managed.output_tail.extend(chunk)
                    if len(managed.output_tail) > self.MAX_OUTPUT_TAIL_BYTES:
                        del managed.output_tail[: len(managed.output_tail) - self.MAX_OUTPUT_TAIL_BYTES]
                    if managed.log.tell() + len(chunk) > self.MAX_LOG_BYTES:
                        self._rotate_log(managed)
                    if len(chunk) > self.MAX_LOG_BYTES:
                        chunk = chunk[-self.MAX_LOG_BYTES :]
                    managed.log.write(chunk)
        finally:
            try:
                managed.log.close()
            except OSError:
                pass

    def run_once(self, action_id: str, action) -> dict:
        with self._action_lock:
            if action_id in self._action_results:
                return self._action_results[action_id]
            result = action()
            self._action_results[action_id] = result
            return result

    def transition(self, state: RunState) -> None:
        allowed = {
            RunState.PREFLIGHT: {RunState.MODELS_READY, RunState.WORLD_READY, RunState.FAILED, RunState.STOPPED},
            RunState.MODELS_READY: {RunState.WORLD_READY, RunState.FAILED, RunState.STOPPED},
            RunState.WORLD_READY: {RunState.RUNNING, RunState.PAUSED, RunState.CHECKPOINTING, RunState.FAILED, RunState.STOPPED},
            RunState.RUNNING: {RunState.PAUSED, RunState.CHECKPOINTING, RunState.FAILED, RunState.STOPPED},
            RunState.PAUSED: {RunState.RUNNING, RunState.WORLD_READY, RunState.CHECKPOINTING, RunState.FAILED, RunState.STOPPED},
            RunState.CHECKPOINTING: {RunState.PAUSED, RunState.STOPPED, RunState.FAILED},
            RunState.FAILED: {RunState.STOPPED},
            RunState.STOPPED: set(),
        }
        with self._lock:
            if state not in allowed[self.state]:
                raise RuntimeError(f"invalid run state transition {self.state.value} -> {state.value}")
            self.state = state
            self._write_state()

    def stop_process(self, name: str, grace_seconds: float = 5) -> None:
        with self._lock:
            managed = self.processes.get(name)
            if managed is None:
                return
            process = managed.process
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=grace_seconds)
        if managed.pump_thread:
            managed.pump_thread.join(timeout=2)
        self._write_state()

    def stop_minecraft_server(self, grace_seconds: float = 45) -> None:
        """Flush the world and ask vanilla to stop before falling back to signals."""
        managed = self.processes.get("minecraft-server")
        if managed is None or managed.process.poll() is not None:
            return
        if managed.process.stdin is not None:
            managed.process.stdin.write(b"save-all flush\nstop\n")
            managed.process.stdin.flush()
        try:
            managed.process.wait(timeout=grace_seconds)
            managed.log.close()
            self._write_state()
        except subprocess.TimeoutExpired:
            self.stop_process("minecraft-server", grace_seconds=5)

    def stop_all(self) -> None:
        failures = []
        for name in reversed(list(self.processes)):
            try:
                self.stop_process(name)
            except (OSError, subprocess.SubprocessError) as exc:
                failures.append(f"{name}: {type(exc).__name__}")
        if self.state != RunState.STOPPED:
            try:
                self.transition(RunState.FAILED if failures else RunState.STOPPED)
            except RuntimeError:
                self.state = RunState.FAILED if failures else RunState.STOPPED
                self._write_state()
        if failures:
            raise RuntimeError("failed to stop process group(s): " + "; ".join(failures))

    def status(self) -> dict:
        with self._lock:
            return {"run_id": self.run_id, "state": self.state.value, "processes": {name: {"pid": p.process.pid, "running": p.process.poll() is None, "ready": p.ready, "exit_code": p.process.poll()} for name, p in self.processes.items()}}
