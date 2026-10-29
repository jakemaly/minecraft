"""Explicit, measured model hydration from public Hugging Face repositories."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
from pathlib import Path
from typing import Any

MODEL_REPOSITORIES = {
    "planner": "nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4",
    "selector": "Cloudflare/clef-flash",
}
MODEL_REVISIONS = {
    "planner": "da5b2650a835292c678994a1fa3fb653250fb508",
    "selector": "f0b7d3cd65296abfe459e6c228b618f5d8b26d2f",
}


def estimate_and_hydrate(
    cache_dir: str | Path,
    *,
    reserve_fraction: float = 0.20,
    roles: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Download the selected immutable public snapshots and record their local hashes."""
    if not 0.05 <= reserve_fraction <= 0.5:
        raise ValueError("reserve_fraction must be between 5% and 50%")
    selected_roles = tuple(MODEL_REPOSITORIES) if roles is None else tuple(roles)
    if not selected_roles or any(role not in MODEL_REPOSITORIES for role in selected_roles):
        raise ValueError("roles must select one or more configured model roles")
    usage = shutil.disk_usage(Path(cache_dir).parent if Path(cache_dir).parent.exists() else Path.home())
    gpu = None
    if shutil.which("nvidia-smi"):
        import subprocess
        try:
            gpu = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader"], capture_output=True, text=True, timeout=5, check=True).stdout.strip() or None
        except Exception:
            pass
    session_seconds = os.getenv("MOLAB_SESSION_REMAINING_SECONDS")

    from huggingface_hub import HfApi, snapshot_download
    api = HfApi(token=os.getenv("HF_TOKEN") or os.getenv("HUGGING_FACE_HUB_TOKEN"))
    resolved = {}
    total_bytes = 0
    for role in selected_roles:
        repo_id = MODEL_REPOSITORIES[role]
        info = api.model_info(repo_id=repo_id, revision=MODEL_REVISIONS[role], files_metadata=True)
        sizes = [item.size for item in (info.siblings or []) if item.size is not None]
        size = sum(sizes)
        total_bytes += size
        resolved[role] = {"repo_id": repo_id, "revision": info.sha, "size_bytes": size, "license": getattr(info.cardData, "license", None)}
    required = math.ceil(total_bytes * (1 + reserve_fraction))
    base = Path(cache_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    for role, item in resolved.items():
        path = Path(snapshot_download(repo_id=item["repo_id"], revision=item["revision"], cache_dir=str(base), token=os.getenv("HF_TOKEN") or os.getenv("HUGGING_FACE_HUB_TOKEN")))
        hashes = {}
        for file_path in path.rglob("*"):
            if file_path.is_file():
                digest = hashlib.sha256()
                with file_path.open("rb") as file_obj:
                    while chunk := file_obj.read(8 * 1024 * 1024):
                        digest.update(chunk)
                hashes[file_path.relative_to(path).as_posix()] = digest.hexdigest()
        item["snapshot_path"] = str(path)
        item["sha256"] = hashes
    report = {"gpu": gpu, "disk_free_bytes_before": usage.free if usage.total < (1 << 60) else None, "disk_capacity_reliable": usage.total < (1 << 60), "session_seconds_reported": int(session_seconds) if session_seconds and session_seconds.isdigit() else None, "model_bytes": total_bytes, "reserve_bytes_suggested": required - total_bytes, "repositories": resolved}
    (base / "hydration-manifest.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report
