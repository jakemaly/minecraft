"""Content identity for the exact checked-out harness and pinned dependencies."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any


SOURCE_PATHS = (
    "src/minecraft_agent",
    "notebooks/01_agent_runtime.py",
    "bridge/src",
    "scripts",
    "schemas",
    "viewer",
    "pyproject.toml",
    "uv.lock",
    "environments/planner/pyproject.toml",
    "environments/planner/uv.lock",
    "environments/clef/pyproject.toml",
    "environments/clef/uv.lock",
    "bridge/package.json",
    "bridge/package-lock.json",
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def source_identity(root: str | Path) -> dict[str, Any]:
    """Hash tracked implementation inputs, including uncommitted source changes."""
    root = Path(root).resolve()
    file_hashes: dict[str, str] = {}
    for entry in SOURCE_PATHS:
        path = root / entry
        candidates = sorted(path.rglob("*") if path.is_dir() else [path])
        for candidate in candidates:
            if candidate.is_file() and not candidate.is_symlink() and "__pycache__" not in candidate.parts and candidate.suffix != ".pyc":
                file_hashes[candidate.relative_to(root).as_posix()] = _sha256_file(candidate)
    combined = hashlib.sha256()
    for name, digest in sorted(file_hashes.items()):
        combined.update(name.encode("utf-8"))
        combined.update(b"\0")
        combined.update(digest.encode("ascii"))
        combined.update(b"\n")

    try:
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=3, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = None

    locks = {
        name: file_hashes[name]
        for name in (
            "uv.lock", "environments/planner/uv.lock",
            "environments/clef/uv.lock", "bridge/package-lock.json",
        )
        if name in file_hashes
    }
    return {
        "git_commit": commit,
        "working_tree_sha256": combined.hexdigest(),
        "source_files": len(file_hashes),
        "lockfile_sha256": locks,
    }
