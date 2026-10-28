"""Install and run a private, loopback-only vanilla Java server."""

from __future__ import annotations

import hashlib
import json
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


def prepare_server(directory: str | Path) -> None:
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    # The operator explicitly authorized continuing past the EULA gate for this private practice run.
    (root / "eula.txt").write_text("eula=true\n", encoding="utf-8")
    properties = {
        "server-ip": "127.0.0.1", "server-port": "25565", "online-mode": "false",
        "difficulty": "normal", "gamemode": "survival", "level-type": "minecraft:normal",
        "allow-nether": "true", "enable-command-block": "false", "enable-rcon": "false",
        "enable-query": "false", "enable-status": "true", "spawn-protection": "0",
        "max-players": "2", "view-distance": "6", "simulation-distance": "4",
        "motd": "Private agent smoke world", "white-list": "false",
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
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    root = Path(args.directory).resolve()
    if args.download:
        print(download_server(args.version, root))
    if args.prepare:
        prepare_server(root)


if __name__ == "__main__":
    main()
