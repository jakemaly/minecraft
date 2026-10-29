"""Install and run a private vanilla Java server on a selected interface."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import secrets
import subprocess
import urllib.request
from pathlib import Path


MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"


def download_server(version: str, directory: str | Path) -> Path:
    target = Path(directory).resolve()
    target.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(urllib.request.urlopen(MANIFEST, timeout=30).read())
    record = next((item for item in manifest["versions"] if item["id"] == version), None)
    if record is None:
        raise RuntimeError(f"Minecraft Java version {version} is not in Mojang's official manifest")
    meta = json.loads(urllib.request.urlopen(record["url"], timeout=30).read())
    server = meta.get("downloads", {}).get("server")
    if not server:
        raise RuntimeError(f"official server jar is not available for {version}")
    jar = target / f"minecraft_server.{version}.jar"
    if not jar.exists():
        request = urllib.request.Request(server["url"], headers={"User-Agent": "minecraft-agent-harness/0.1"})
        with urllib.request.urlopen(request, timeout=180) as response, jar.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
    digest = hashlib.sha1()
    with jar.open("rb") as existing:
        while chunk := existing.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != server["sha1"]:
        jar.unlink(missing_ok=True)
        raise RuntimeError("server jar SHA-1 did not match Mojang's version manifest")
    return jar


def prepare_server(
    directory: str | Path,
    *,
    seed: str | int | None = None,
    bind_address: str = "127.0.0.1",
    port: int = 25565,
    default_gamemode: str = "spectator",
) -> None:
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    ipaddress.ip_address(bind_address)
    if not 1 <= int(port) <= 65535:
        raise ValueError("server port must be between 1 and 65535")
    if default_gamemode not in {"survival", "spectator"}:
        raise ValueError("default_gamemode must be survival or spectator")
    if seed is None:
        seed = secrets.randbits(63)
    # The operator explicitly authorized continuing past the EULA gate for this private practice run.
    (root / "eula.txt").write_text("eula=true\n", encoding="utf-8")
    properties = {
        "server-ip": bind_address, "server-port": str(port), "online-mode": "false",
        "difficulty": "normal", "gamemode": default_gamemode, "level-type": "minecraft:normal",
        "level-seed": str(seed),
        "allow-nether": "true", "enable-command-block": "false", "enable-rcon": "false",
        "enable-query": "false", "enable-status": "true", "spawn-protection": "0",
        "max-players": "2", "view-distance": "6", "simulation-distance": "4",
        "motd": "Private Minecraft agent trial", "white-list": "false",
    }
    (root / "server.properties").write_text("".join(f"{key}={value}\n" for key, value in properties.items()), encoding="utf-8")


def server_command(jar: str | Path, *, memory_mb: int = 4096) -> list[str]:
    if memory_mb < 1024 or memory_mb > 8192:
        raise ValueError("server heap must be between 1024 and 8192 MiB")
    return ["java", f"-Xms{min(memory_mb, 2048)}M", f"-Xmx{memory_mb}M", "-jar", str(Path(jar).resolve()), "nogui"]


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="1.21.4")
    parser.add_argument("--directory", required=True)
    parser.add_argument("--bind-address", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=25565)
    parser.add_argument("--seed")
    parser.add_argument("--default-gamemode", choices=("survival", "spectator"), default="spectator")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    root = Path(args.directory).resolve()
    if args.download:
        print(download_server(args.version, root))
    if args.prepare:
        prepare_server(root, seed=args.seed, bind_address=args.bind_address, port=args.port, default_gamemode=args.default_gamemode)


if __name__ == "__main__":
    main()
