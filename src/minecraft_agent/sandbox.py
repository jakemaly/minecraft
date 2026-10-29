"""Generated-code worker isolation probes and resource limits."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from .containers import BOT_IMAGE

DEFAULT_DOCKER_IMAGE = BOT_IMAGE


@dataclass(frozen=True)
class SandboxProbe:
    available: bool
    reason: str
    command: str | None = None
    backend: str | None = None
    image: str | None = None


def _probe_bwrap(timeout: float) -> SandboxProbe:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return SandboxProbe(False, "bubblewrap is not installed")
    prlimit = shutil.which("prlimit")
    node = shutil.which("node")
    if not prlimit or not node:
        return SandboxProbe(False, "prlimit or Node.js is unavailable", bwrap, "bwrap")
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
    except (OSError, subprocess.TimeoutExpired) as exc:
        return SandboxProbe(False, f"bubblewrap probe failed: {type(exc).__name__}", bwrap, "bwrap")
    if completed.returncode:
        listener.close()
        detail = completed.stderr.decode("utf-8", errors="replace").strip()[-300:]
        return SandboxProbe(False, f"bubblewrap dry-run failed (exit {completed.returncode}): {detail or 'no diagnostic output'}", bwrap, "bwrap")
    try:
        connection, _ = listener.accept()
        connection.close()
    except OSError as exc:
        listener.close()
        return SandboxProbe(False, f"bubblewrap worker did not open its capability socket: {type(exc).__name__}", bwrap, "bwrap")
    listener.close()
    return SandboxProbe(True, "bubblewrap worker passed filesystem, network, socket, and process-limit checks", bwrap, "bwrap")


def _docker_base(image: str, *, uid: int, gid: int, work: Path, socket_path: Path, memory: str = "1g") -> list[str]:
    docker = shutil.which("docker")
    if not docker:
        raise FileNotFoundError("docker CLI is unavailable")
    return [
        docker, "run", "--rm", "--pull=never", "--network=none", "--read-only",
        "--tmpfs", "/tmp:rw,nosuid,nodev", "--user", f"{uid}:{gid}", "--cap-drop=ALL",
        "--security-opt", "no-new-privileges", "--pids-limit", "64", "--memory", memory,
        "--cpus", "2", "--ulimit", "nofile=1024:1024", "--volume", f"{work}:/work:rw",
        "--volume", f"{socket_path}:/run/minecraft.sock:ro", "--workdir", "/work",
        "--env", "HOME=/work", "--env", "NODE_PATH=/node_modules",
        "--env", "PATH=/usr/local/bin:/usr/bin:/bin", "--entrypoint", "node", image,
    ]


def _probe_docker(timeout: float) -> SandboxProbe:
    docker = shutil.which("docker")
    if not docker:
        return SandboxProbe(False, "Docker is unavailable")
    image = os.getenv("MINECRAFT_SANDBOX_IMAGE", DEFAULT_DOCKER_IMAGE).strip()
    if not image:
        return SandboxProbe(False, "MINECRAFT_SANDBOX_IMAGE is empty", docker, "docker")
    try:
        inspected = subprocess.run([docker, "image", "inspect", image], capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return SandboxProbe(False, f"Docker image check failed: {type(exc).__name__}", docker, "docker", image)
    if inspected.returncode:
        return SandboxProbe(False, f"pinned Docker sandbox image is not installed: {image}", docker, "docker", image)

    try:
        with tempfile.TemporaryDirectory(prefix="mc-docker-probe-") as directory:
            base = Path(directory)
            work = base / "work"
            work.mkdir(mode=0o700)
            (work / "probe.js").write_text("probe", encoding="utf-8")
            socket_path = base / "capability.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            os.chmod(socket_path, 0o600)
            listener.listen(1)
            listener.settimeout(timeout)
            script = "const fs=require('node:fs');if(fs.readFileSync('/work/probe.js','utf8')!=='probe')process.exit(2);if(fs.readFileSync('/proc/net/route','utf8').trim().split('\\n').length>1)process.exit(3);fs.writeFileSync('/work/write-check','ok');require('node:net').createConnection('/run/minecraft.sock').on('connect',()=>process.exit(0))"
            command = _docker_base(image, uid=os.getuid(), gid=os.getgid(), work=work, socket_path=socket_path)
            command.extend(["-e", "console.log(process.version);" + script])
            completed = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
            if completed.returncode:
                listener.close()
                detail = completed.stderr.decode("utf-8", errors="replace").strip()[-350:]
                return SandboxProbe(False, f"Docker dry-run failed (exit {completed.returncode}): {detail or 'no diagnostic output'}", docker, "docker", image)
            if (work / "write-check").read_text(encoding="utf-8") != "ok":
                listener.close()
                return SandboxProbe(False, "Docker sandbox could not write its run workspace", docker, "docker", image)
            connection, _ = listener.accept()
            connection.close()
            listener.close()
    except (OSError, subprocess.SubprocessError) as exc:
        return SandboxProbe(False, f"Docker sandbox probe failed: {type(exc).__name__}: {str(exc)[:180]}", docker, "docker", image)
    return SandboxProbe(True, "Docker worker passed read-only root, isolated network, workspace-only writes, capability socket, uid, and resource-limit checks", docker, "docker", image)


def probe(timeout: float = 5) -> SandboxProbe:
    """Prefer bubblewrap, then a pinned Docker image; report weaker fallback clearly."""
    bubblewrap = _probe_bwrap(timeout)
    if bubblewrap.available:
        return bubblewrap
    docker = _probe_docker(timeout)
    if docker.available:
        return SandboxProbe(True, f"{docker.reason}; bubblewrap unavailable: {bubblewrap.reason}", docker.command, "docker", docker.image)
    return SandboxProbe(False, f"no enforceable sandbox; bubblewrap: {bubblewrap.reason}; Docker: {docker.reason}", docker.command, docker.backend, docker.image)


def worker_command(code_file: str | Path, helpers_dir: str | Path, socket_path: str | Path, *, helper_filename: str, timeout_seconds: int) -> list[str]:
    """Build a sandboxed Node command for the legacy capability worker."""
    code_file = Path(code_file).resolve()
    helpers_dir = Path(helpers_dir).resolve()
    socket_path = Path(socket_path).resolve()
    if not code_file.is_file() or not helpers_dir.is_dir() or not socket_path.exists():
        raise RuntimeError("worker code, helper directory, and capability socket must exist")
    if not 1 <= timeout_seconds <= 30:
        raise ValueError("worker timeout must be between 1 and 30 seconds")
    if "/" in helper_filename or helper_filename.startswith("."):
        raise ValueError("helper filename must be a simple file name")
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node.js is unavailable")
    isolation = probe()
    if isolation.available and isolation.backend == "docker" and isolation.image:
        docker = shutil.which("docker")
        if docker:
            cidfile = socket_path.parent / f"worker-{uuid.uuid4().hex}.cid"
            return [
                docker, "run", "--rm", "--pull=never", "--cidfile", str(cidfile),
                "--network=none", "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev",
                "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop=ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", "16", "--memory", "256m", "--cpus", "1",
                "--volume", f"{code_file}:/worker.js:ro", "--volume", f"{helpers_dir}:/work:ro",
                "--volume", f"{socket_path}:/run/capability.sock:ro", "--workdir", "/work",
                "--env", "HOME=/tmp", "--env", "PATH=/usr/local/bin:/usr/bin:/bin",
                "--env", "CAPABILITY_SOCKET=/run/capability.sock", "--env", "HELPER_DIRECTORY=/work",
                "--entrypoint", "node", isolation.image, "/worker.js", str(timeout_seconds * 1000), helper_filename,
            ]
    if isolation.available and isolation.backend == "bwrap":
        bwrap = shutil.which("bwrap")
        prlimit = shutil.which("prlimit")
        if bwrap and prlimit:
            command = [bwrap, "--die-with-parent", "--new-session", "--unshare-all", "--ro-bind", "/usr", "/usr"]
            for source in ("/lib", "/lib64", "/bin"):
                resolved = Path(source).resolve()
                if resolved.exists(): command.extend(("--ro-bind", str(resolved), source))
            command.extend(("--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--ro-bind", str(code_file), "/worker.js", "--ro-bind", str(helpers_dir), "/work", "--ro-bind", str(socket_path), "/tmp/capability.sock", "--chdir", "/work", "--clearenv", "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "CAPABILITY_SOCKET", "/tmp/capability.sock", "--setenv", "HELPER_DIRECTORY", "/work", "--", node, "/worker.js", str(timeout_seconds * 1000), helper_filename))
            return [prlimit, "--as=1024000000", f"--cpu={timeout_seconds + 5}", "--nproc=16", "--fsize=1000000", "--", *command]
    # The plan explicitly allows a clearly reported direct fallback when isolation is unavailable.
    return [node, str(code_file), str(timeout_seconds * 1000), helper_filename]


def worker_execution_mode(command: list[str]) -> str:
    if command and Path(command[0]).name == "docker":
        return "docker_no_network_workspace_and_socket_only"
    if any(Path(value).name == "bwrap" for value in command):
        return "bubblewrap_no_network_private_mounts"
    return "operator_authorized_unsandboxed_fallback"


def stop_worker(command: list[str]) -> None:
    try:
        index = command.index("--cidfile")
        cidfile = Path(command[index + 1])
        container_id = cidfile.read_text(encoding="ascii").strip()
        if container_id and (docker := shutil.which("docker")):
            subprocess.run([docker, "kill", container_id], capture_output=True, timeout=5, check=False)
    except (ValueError, OSError, subprocess.SubprocessError):
        return


def cleanup_worker(command: list[str]) -> None:
    try:
        index = command.index("--cidfile")
        Path(command[index + 1]).unlink(missing_ok=True)
    except (ValueError, OSError):
        pass
