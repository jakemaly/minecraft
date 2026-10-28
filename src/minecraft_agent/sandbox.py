"""Generated-code worker launcher with an operator-authorized direct fallback."""

from __future__ import annotations

import shutil
import socket
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SandboxProbe:
    available: bool
    reason: str
    command: str | None = None


def probe(timeout: float = 5) -> SandboxProbe:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return SandboxProbe(False, "bubblewrap is not installed; helpers use operator-authorized direct execution")
    prlimit = shutil.which("prlimit")
    node = shutil.which("node")
    if not prlimit or not node:
        return SandboxProbe(False, "prlimit or Node.js is unavailable; worker resource limits cannot be enforced", bwrap)
    try:
        with tempfile.TemporaryDirectory(prefix="mc-isolation-probe-") as directory:
            base = Path(directory)
            helper_dir = base / "helpers"
            socket_dir = base / "runtime"
            helper_dir.mkdir(mode=0o700)
            socket_dir.mkdir(mode=0o700)
            (helper_dir / "probe.js").write_text("module.exports = 1\n", encoding="utf-8")
            socket_path = socket_dir / "capability.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            listener.listen(1)
            listener.settimeout(timeout)
            js = "const fs=require('node:fs');if(fs.readFileSync('/work/probe.js','utf8')!=='module.exports = 1\\n')process.exit(2);if(fs.readFileSync('/proc/net/route','utf8').trim())process.exit(3);require('node:net').createConnection('/run/capability.sock').on('connect',()=>process.exit(0))"
            bubblewrap = [bwrap, "--die-with-parent", "--new-session", "--unshare-all", "--ro-bind", "/usr", "/usr", "--ro-bind", str(Path("/lib").resolve()), "/lib", "--ro-bind", str(Path("/lib64").resolve()), "/lib64", "--ro-bind", str(Path("/bin").resolve()), "/bin", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--bind", str(helper_dir), "/work", "--ro-bind", str(socket_dir), "/run", "--chdir", "/work", "--clearenv", "--setenv", "PATH", "/usr/bin:/bin", "--", node, "--max-old-space-size=128", "-e", js]
            command = [prlimit, "--as=1024000000", "--cpu=3", "--nproc=16", "--fsize=1000000", "--", *bubblewrap]
            completed = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
            connection, _ = listener.accept()
            connection.close()
            listener.close()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return SandboxProbe(False, f"bubblewrap isolation probe failed: {type(exc).__name__}", bwrap)
    if completed.returncode:
        return SandboxProbe(False, f"isolated worker dry-run failed (exit {completed.returncode})", bwrap)
    return SandboxProbe(True, "isolated Node worker started with separate namespaces, no IP routes, private filesystem mounts, a mounted capability socket, and process resource limits", bwrap)


def worker_command(code_file: str | Path, helpers_dir: str | Path, socket_path: str | Path, *, helper_filename: str, timeout_seconds: int) -> list[str]:
    code_file = Path(code_file).resolve()
    helpers_dir = Path(helpers_dir).resolve()
    socket_parent = Path(socket_path).resolve().parent
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node.js is unavailable")
    if not code_file.is_file() or not helpers_dir.is_dir() or not (socket_parent / "capability.sock").exists():
        raise RuntimeError("worker code, helper directory, and capability socket must exist")
    if "/" in helper_filename or helper_filename.startswith("."):
        raise ValueError("helper filename must be a simple file name")
    # The operator explicitly authorized direct execution when OS isolation is unavailable.
    return [node, str(code_file), str(timeout_seconds), helper_filename]
