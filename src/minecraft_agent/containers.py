"""Pinned Docker runtime for the isolated vanilla server and Mineflayer bot."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


BOT_IMAGE = "node@sha256:4e6b70dd6cbfc88c8157ba19aa3d9f9cce6ba4703576d55459e45efcbc9c5f5d"
SERVER_IMAGE = "eclipse-temurin@sha256:cd35bb33859641c5374415826a5951845104093eaa401cbe9d3d9023444f1c13"
TAILNET_SERVICE_PORTS = "2718,3000,25565"


def docker_cli() -> str | None:
    return shutil.which("docker")


def runtime_status() -> dict[str, Any]:
    docker = docker_cli()
    if not docker:
        return {"available": False, "reason": "Docker CLI is not installed"}
    try:
        result = subprocess.run([docker, "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True, timeout=8, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "reason": f"Docker daemon check failed: {type(exc).__name__}"}
    if result.returncode:
        return {"available": False, "reason": result.stderr.strip()[-300:] or "Docker daemon is unavailable"}
    images = {"bot": BOT_IMAGE, "server": SERVER_IMAGE}
    for role, image in images.items():
        checked = subprocess.run([docker, "image", "inspect", image], capture_output=True, text=True, timeout=8, check=False)
        if checked.returncode:
            return {"available": False, "reason": f"pinned {role} image is missing: {image}", "missing_image": image}
    return {"available": True, "docker_version": result.stdout.strip(), "bot_image": BOT_IMAGE, "server_image": SERVER_IMAGE, "network_mode": "per-run internal bridge with a fixed-port forwarding gateway", "host_gateway_policy": "temporary INPUT drop on the private bridge"}


def ensure_runtime_images() -> dict[str, Any]:
    """Fetch the two pinned public runtime images when Docker is usable."""
    status = runtime_status()
    image = status.get("missing_image")
    docker = docker_cli()
    if image and docker:
        try:
            pulled = subprocess.run([docker, "pull", image], capture_output=True, text=True, timeout=600, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return {**status, "reason": f"pinned runtime image pull failed: {type(exc).__name__}"}
        if pulled.returncode:
            return {**status, "reason": f"pinned runtime image pull failed: {pulled.stderr.strip()[-300:]}"}
        status = runtime_status()
    return status


def create_private_network(run_id: str) -> str:
    docker = docker_cli()
    if not docker:
        raise RuntimeError("Docker CLI is unavailable")
    name = f"mc-{run_id}".lower().replace("_", "-")[:60]
    result = subprocess.run([docker, "network", "create", "--driver", "bridge", "--internal", name], capture_output=True, text=True, timeout=20, check=False)
    if result.returncode:
        raise RuntimeError(f"could not create private Docker network: {result.stderr.strip()[-500:]}")
    try:
        _set_host_gateway_block(name, present=True)
    except Exception:
        subprocess.run([docker, "network", "rm", name], capture_output=True, text=True, timeout=20, check=False)
        raise
    return name


def remove_private_network(name: str | None) -> None:
    docker = docker_cli()
    if not docker or not name:
        return
    inspected = subprocess.run([docker, "network", "inspect", name, "--format", "{{.Id}}"], capture_output=True, text=True, timeout=8, check=False)
    if inspected.returncode:
        return
    removed = subprocess.run([docker, "network", "rm", name], capture_output=True, text=True, timeout=20, check=False)
    if removed.returncode:
        return
    _set_host_gateway_block(name, present=False, network_id=inspected.stdout.strip())


def cleanup_run_containers(run_id: str) -> None:
    """Remove only the three containers owned by one generated run ID."""
    docker = docker_cli()
    if not docker:
        return
    suffix = run_id[-16:]
    for name in (f"mc-gateway-{suffix}", f"mc-bot-{suffix}", f"mc-server-{suffix}"):
        inspected = subprocess.run([docker, "container", "inspect", "--format", "{{.Id}}", name], capture_output=True, text=True, timeout=8, check=False)
        if inspected.returncode:
            continue
        removed = subprocess.run([docker, "container", "rm", "--force", name], capture_output=True, text=True, timeout=30, check=False)
        if removed.returncode:
            raise RuntimeError(f"could not remove run container {name}: {removed.stderr.strip()[-300:]}")


def _run_host_iptables(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    docker = docker_cli()
    if not docker:
        raise RuntimeError("Docker CLI is unavailable for the temporary host firewall rule")
    command = [
        docker, "run", "--rm", "--pull=never", "--network", "host", "--read-only",
        "--cap-drop=ALL", "--cap-add=NET_ADMIN", "--tmpfs", "/run:rw,nosuid,nodev",
        "--tmpfs", "/tmp:rw,nosuid,nodev", "--volume", "/usr:/usr:ro",
        "--volume", "/lib:/lib:ro", "--volume", "/lib64:/lib64:ro",
        "--volume", "/etc/alternatives:/etc/alternatives:ro", "--volume", "/etc/ld.so.cache:/etc/ld.so.cache:ro",
        "--entrypoint", "/usr/sbin/iptables", BOT_IMAGE, *arguments,
    ]
    return subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)


def configure_tailnet_firewall(host_address: str, client_address: str) -> dict[str, Any]:
    """Limit the notebook, viewer, and game ports to the operator Mac on tailscale0."""
    import ipaddress

    host = ipaddress.ip_address(host_address)
    client = ipaddress.ip_address(client_address)
    if host.version != 4 or client.version != 4:
        raise ValueError("the current service firewall requires IPv4 Tailscale addresses")
    tailnet_ipv4 = ipaddress.ip_network("100.64.0.0/10")
    if host not in tailnet_ipv4 or client not in tailnet_ipv4:
        raise ValueError("service firewall addresses must be IPv4 Tailscale addresses")
    drop_tag = "minecraft-agent:tailnet-services-drop"
    allow_tag = "minecraft-agent:tailnet-services-allow"
    common = ["-i", "tailscale0", "-d", str(host), "-p", "tcp", "-m", "multiport", "--dports", TAILNET_SERVICE_PORTS, "-m", "comment"]
    drop_rule = [*common, "--comment", drop_tag, "-j", "DROP"]
    allow_rule = [*common, "-s", f"{client}/32", "--comment", allow_tag, "-j", "ACCEPT"]
    if _run_host_iptables(["-C", "INPUT", *drop_rule]).returncode:
        result = _run_host_iptables(["-I", "INPUT", "1", *drop_rule])
        if result.returncode:
            raise RuntimeError(f"could not restrict tailnet service ports: {result.stderr.strip()[-350:]}")
    if _run_host_iptables(["-C", "INPUT", *allow_rule]).returncode:
        result = _run_host_iptables(["-I", "INPUT", "1", *allow_rule])
        if result.returncode:
            raise RuntimeError(f"could not allow the operator Mac to reach service ports: {result.stderr.strip()[-350:]}")
    return {"status": "configured", "host": str(host), "allowed_client": str(client), "ports": [2718, 3000, 25565], "interface": "tailscale0", "scope": "only these inbound TCP service ports"}


def _set_host_gateway_block(name: str, *, present: bool, network_id: str | None = None) -> None:
    docker = docker_cli()
    if not docker:
        raise RuntimeError("Docker CLI is unavailable for the temporary host firewall rule")
    if network_id is None:
        inspected = subprocess.run([docker, "network", "inspect", name, "--format", "{{.Id}}"], capture_output=True, text=True, timeout=8, check=False)
        if inspected.returncode:
            raise RuntimeError(f"cannot identify private network {name}: {inspected.stderr.strip()[-250:]}")
        network_id = inspected.stdout.strip()
    bridge = f"br-{network_id[:12]}"
    tag = f"minecraft-agent:{name}"
    arguments = ["-I", "INPUT", "1", "-i", bridge, "-m", "comment", "--comment", tag, "-j", "DROP"] if present else ["-D", "INPUT", "-i", bridge, "-m", "comment", "--comment", tag, "-j", "DROP"]
    result = _run_host_iptables(arguments)
    if result.returncode and present:
        raise RuntimeError(f"could not block container access to host services on {bridge}: {result.stderr.strip()[-350:]}")


def server_container_command(
    *,
    run_id: str,
    network: str,
    server_directory: str | Path,
    memory_mb: int,
) -> list[str]:
    docker = docker_cli()
    if not docker:
        raise RuntimeError("Docker CLI is unavailable")
    if memory_mb < 1024 or memory_mb > 8192:
        raise ValueError("server heap must be between 1024 and 8192 MiB")
    root = Path(server_directory).resolve()
    jars = list(root.glob("minecraft_server.*.jar"))
    if len(jars) != 1:
        raise RuntimeError("server directory must contain exactly one pinned Minecraft server jar")
    container_memory = memory_mb + 1536
    return [
        docker, "run", "--rm", "--pull=never", "-i", "--name", f"mc-server-{run_id[-16:]}",
        "--network", network, "--network-alias", "minecraft-server",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--cap-drop=ALL", "--security-opt", "no-new-privileges", "--pids-limit", "256",
        "--memory", f"{container_memory}m", "--cpus", "4", "--read-only",
        "--tmpfs", "/tmp:rw,nosuid,nodev", "--volume", f"{root}:/data:rw", "--workdir", "/data",
        "--env", "HOME=/data", "--entrypoint", "java", SERVER_IMAGE,
        f"-Xms{min(memory_mb, 2048)}M", f"-Xmx{memory_mb}M", "-jar", str(Path("/data") / jars[0].name), "nogui",
    ]


def bot_container_command(
    *,
    run_id: str,
    network: str,
    repo_root: str | Path,
    run_directory: str | Path,
    workspace: str | Path,
    minecraft_version: str,
) -> list[str]:
    docker = docker_cli()
    if not docker:
        raise RuntimeError("Docker CLI is unavailable")
    root = Path(repo_root).resolve()
    run_dir = Path(run_directory).resolve()
    workspace_dir = Path(workspace).resolve()
    socket_dir = run_dir / ".runtime"
    socket_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return [
        docker, "run", "--rm", "--pull=never", "-i", "--name", f"mc-bot-{run_id[-16:]}",
        "--network", network, "--network-alias", "minecraft-agent",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--cap-drop=ALL", "--security-opt", "no-new-privileges", "--pids-limit", "128",
        "--memory", "2g", "--cpus", "2", "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev",
        "--volume", f"{root / 'bridge/src'}:/app/src:ro",
        "--volume", f"{root / 'bridge/node_modules'}:/node_modules:ro",
        "--volume", f"{workspace_dir}:/workspace:rw", "--volume", f"{socket_dir}:/runtime:rw",
        "--workdir", "/workspace", "--env", "PATH=/usr/local/bin:/usr/bin:/bin",
        "--env", "HOME=/workspace", "--env", "NODE_PATH=/node_modules", "--env", f"RUN_ID={run_id}",
        "--env", f"MINECRAFT_VERSION={minecraft_version}", "--env", "MINECRAFT_HOST=minecraft-server",
        "--env", "MINECRAFT_PORT=25565", "--env", "MINECRAFT_ALLOW_INTERNAL_SERVER=true",
        "--env", "MINECRAFT_AGENT_SOCKET=/runtime/mineflayer.sock", "--env", "MINECRAFT_VIEWER_ENABLED=true",
        "--env", "MINECRAFT_VIEWER_HOST=0.0.0.0", "--env", "MINECRAFT_VIEWER_PORT=3000",
        "--env", "MINECRAFT_VIEWER_DISTANCE=6", "--entrypoint", "node", BOT_IMAGE, "/app/src/bot.js",
    ]


def gateway_name(run_id: str) -> str:
    return f"mc-gateway-{run_id[-16:]}"


def connect_gateway_to_network(run_id: str, network: str) -> None:
    docker = docker_cli()
    if not docker:
        raise RuntimeError("Docker CLI is unavailable")
    result = subprocess.run(
        [docker, "network", "connect", "--alias", "minecraft-gateway", network, gateway_name(run_id)],
        capture_output=True, text=True, timeout=20, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"could not attach the fixed-port gateway to the private network: {result.stderr.strip()[-400:]}")


def gateway_container_command(*, run_id: str, bind_address: str, server_port: int, viewer_port: int) -> list[str]:
    """Publish only fixed TCP forwards; the model's bot stays on the internal network."""
    docker = docker_cli()
    if not docker:
        raise RuntimeError("Docker CLI is unavailable")
    forwarder = r"""const net=require('node:net');
for(const [listenPort,targetHost,targetPort] of [[25565,'minecraft-server',25565],[3000,'minecraft-agent',3000]]){
  net.createServer((client)=>{
    const upstream=net.connect(targetPort,targetHost);
    client.pipe(upstream);upstream.pipe(client);
    const close=()=>{client.destroy();upstream.destroy()};
    client.on('error',close);upstream.on('error',close);client.on('close',()=>upstream.destroy());upstream.on('close',()=>client.destroy());
  }).listen(listenPort,'0.0.0.0');
}
console.log('gateway_ready');
"""
    return [
        docker, "run", "--rm", "--pull=never", "-i", "--name", gateway_name(run_id),
        "--network", "bridge", "--publish", f"{bind_address}:{server_port}:25565/tcp",
        "--publish", f"{bind_address}:{viewer_port}:3000/tcp", "--user", f"{os.getuid()}:{os.getgid()}",
        "--cap-drop=ALL", "--security-opt", "no-new-privileges", "--pids-limit", "64",
        "--memory", "256m", "--cpus", "1", "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev",
        "--entrypoint", "node", BOT_IMAGE, "-e", forwarder,
    ]
