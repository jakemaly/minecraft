"""Hash-verified local snapshots and private remote storage adapters."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import secrets
import shutil
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .events import redact


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _files(root: Path) -> list[Path]:
    paths = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"snapshot refuses symlinks: {path}")
        if path.is_file():
            paths.append(path)
    return sorted(paths)


def create_snapshot(source_dirs: dict[str, str | Path], destination: str | Path, *, metadata: dict[str, Any], previous_snapshot_id: str | None = None) -> dict[str, Any]:
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mc-snapshot-") as temp_name:
        stage = Path(temp_name)
        entries = []
        for label, source in source_dirs.items():
            if not label or "/" in label or ".." in label:
                raise ValueError("snapshot source labels must be simple directory names")
            source_path = Path(source).resolve()
            if not source_path.is_dir():
                raise FileNotFoundError(source_path)
            target = stage / label
            _files(source_path)  # Reject source symlinks instead of snapshotting outside the declared tree.
            shutil.copytree(source_path, target, symlinks=False)
            for file_path in _files(target):
                relative = file_path.relative_to(stage).as_posix()
                digest = _sha256(file_path)
                entries.append({"path": relative, "size_bytes": file_path.stat().st_size, "sha256": digest})
        manifest = {"schema_version": 1, "snapshot_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"), "created_at": datetime.now(timezone.utc).isoformat(), "previous_snapshot_id": previous_snapshot_id, "metadata": redact(metadata), "files": entries}
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp_archive = destination.with_suffix(destination.suffix + ".tmp")
        with zipfile.ZipFile(temp_archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3, allowZip64=True) as archive:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(stage).as_posix())
        with temp_archive.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp_archive, destination)
    verify_snapshot(destination)
    return manifest


def verify_snapshot(archive_path: str | Path) -> dict[str, Any]:
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or any(PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts for name in names):
            raise ValueError("snapshot contains duplicate or unsafe paths")
        manifest = json.loads(archive.read("manifest.json"))
        expected = {entry["path"]: entry for entry in manifest["files"]}
        actual = set(names) - {"manifest.json"}
        if actual != set(expected):
            raise ValueError("snapshot file manifest does not match archive contents")
        for name, entry in expected.items():
            data = archive.read(name)
            if len(data) != entry["size_bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise ValueError(f"snapshot integrity check failed for {name}")
        return manifest


def restore_snapshot(archive_path: str | Path, destination: str | Path) -> dict[str, Any]:
    manifest = verify_snapshot(archive_path)
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive_path) as archive:
        for name in archive.namelist():
            path = PurePosixPath(name)
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if name.endswith("/"):
                continue
            target.write_bytes(archive.read(name))
    return manifest


def compare_player_state(expected: dict[str, Any] | None, actual: dict[str, Any]) -> dict[str, Any]:
    """Compare the bounded Mineflayer observation saved at checkpoint with a fresh reconnect."""
    if not isinstance(expected, dict):
        return {"status": "blocked", "reason": "checkpoint has no fresh player observation"}
    expected_player = expected.get("player") or {}
    actual_player = actual.get("player") or {}
    expected_position = expected_player.get("position") or {}
    actual_position = actual_player.get("position") or {}
    position_ok = all(
        isinstance(expected_position.get(axis), (int, float))
        and isinstance(actual_position.get(axis), (int, float))
        and math.isclose(expected_position[axis], actual_position[axis], rel_tol=0, abs_tol=0.1)
        for axis in ("x", "y", "z")
    )

    def inventory_signature(observation: dict[str, Any]) -> list[tuple[Any, Any, Any]]:
        return sorted((item.get("name"), item.get("count"), item.get("slot")) for item in observation.get("inventory", []))

    checks = {
        "dimension": (expected.get("world") or {}).get("dimension") == (actual.get("world") or {}).get("dimension"),
        "position": position_ok,
        "health": isinstance(expected_player.get("health"), (int, float)) and expected_player.get("health") == actual_player.get("health"),
        "food": isinstance(expected_player.get("food"), (int, float)) and expected_player.get("food") == actual_player.get("food"),
        "inventory": inventory_signature(expected) == inventory_signature(actual),
    }
    return {"status": "passed" if all(checks.values()) else "mismatch", "checks": checks}


def upload_huggingface(snapshot: str | Path, repo_id: str, token: str) -> str:
    if not token:
        raise RuntimeError("HF token is not configured")
    from huggingface_hub import HfApi
    api = HfApi(token=token)
    info = api.repo_info(repo_id=repo_id, repo_type="dataset")
    if not getattr(info, "private", False):
        raise RuntimeError("artifact repository must already exist and be private")
    path = Path(snapshot)
    manifest = verify_snapshot(path)
    marker = {"schema_version": 1, "snapshot_id": manifest["snapshot_id"], "archive_sha256": _sha256(path), "archive_size_bytes": path.stat().st_size}
    api.upload_file(path_or_fileobj=str(path), path_in_repo=path.name, repo_id=repo_id, repo_type="dataset", commit_message=f"Upload verified run snapshot {path.name}")
    api.upload_file(path_or_fileobj=io.BytesIO(json.dumps(marker, sort_keys=True).encode("utf-8")), path_in_repo=path.name + ".manifest.json", repo_id=repo_id, repo_type="dataset", commit_message=f"Publish verified snapshot marker {path.name}")
    return f"hf://datasets/{repo_id}/{path.name}"


def upload_s3(snapshot: str | Path, bucket: str, key: str) -> str:
    if not bucket:
        raise RuntimeError("S3 bucket is not configured")
    import boto3
    client = boto3.client("s3")
    client.head_bucket(Bucket=bucket)
    access = client.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"]
    required = ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")
    if not all(access.get(flag) is True for flag in required):
        raise RuntimeError("S3 bucket must have all four public access block settings enabled")
    path = Path(snapshot)
    manifest = verify_snapshot(path)
    marker = {"schema_version": 1, "snapshot_id": manifest["snapshot_id"], "archive_sha256": _sha256(path), "archive_size_bytes": path.stat().st_size}
    client.upload_file(str(path), bucket, key, ExtraArgs={"Metadata": {"sha256": marker["archive_sha256"]}})
    client.put_object(Bucket=bucket, Key=key + ".manifest.json", Body=json.dumps(marker, sort_keys=True).encode("utf-8"), ServerSideEncryption="AES256", ContentType="application/json")
    return f"s3://{bucket}/{key}"


def probe_configured_storage() -> dict[str, Any]:
    """Round-trip and delete a tiny object in an already configured private target."""
    repo_id = os.getenv("MINECRAFT_ARTIFACT_REPO")
    bucket = os.getenv("MINECRAFT_S3_BUCKET")
    token = os.getenv("HF_TOKEN") or os.getenv("HUGGING_FACE_HUB_TOKEN", "")
    if repo_id and token:
        return _probe_huggingface(repo_id, token)
    if bucket:
        return _probe_s3(bucket, os.getenv("MINECRAFT_S3_KEY_PREFIX", "minecraft-agent"))
    if repo_id:
        raise RuntimeError("HF artifact repository is configured but no HF token is available")
    raise RuntimeError("configure an existing private HF dataset repo or existing S3 bucket")


def _probe_huggingface(repo_id: str, token: str) -> dict[str, Any]:
    if not token:
        raise RuntimeError("HF token is not configured")
    from huggingface_hub import HfApi, hf_hub_download

    api = HfApi(token=token)
    info = api.repo_info(repo_id=repo_id, repo_type="dataset")
    if not getattr(info, "private", False):
        raise RuntimeError("artifact repository must already exist and be private")
    probe_name = f".minecraft-agent-probe-{uuid.uuid4().hex}.bin"
    payload = secrets.token_bytes(4096)
    uploaded = False
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="mc-storage-probe-") as temp_name:
            local = Path(temp_name) / "probe.bin"
            local.write_bytes(payload)
            api.upload_file(path_or_fileobj=str(local), path_in_repo=probe_name, repo_id=repo_id, repo_type="dataset", commit_message="Temporary private storage connectivity probe")
            uploaded = True
            downloaded = Path(hf_hub_download(repo_id=repo_id, filename=probe_name, repo_type="dataset", token=token))
            if downloaded.read_bytes() != payload:
                raise RuntimeError("Hugging Face storage probe payload did not round-trip exactly")
    finally:
        if uploaded:
            api.delete_file(path_in_repo=probe_name, repo_id=repo_id, repo_type="dataset", commit_message="Remove temporary storage probe")
    return {"status": "passed", "provider": "huggingface", "private_destination_verified": True, "bytes": len(payload), "round_trip_seconds": round(time.monotonic() - started, 3), "probe_object_deleted": True}


def _probe_s3(bucket: str, key_prefix: str) -> dict[str, Any]:
    if not bucket:
        raise RuntimeError("S3 bucket is not configured")
    import boto3

    client = boto3.client("s3")
    client.head_bucket(Bucket=bucket)
    access = client.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"]
    required = ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")
    if not all(access.get(flag) is True for flag in required):
        raise RuntimeError("S3 bucket must have all four public access block settings enabled")
    prefix = key_prefix.strip("/")
    key = f"{prefix + '/' if prefix else ''}.minecraft-agent-probe-{uuid.uuid4().hex}.bin"
    payload = secrets.token_bytes(4096)
    uploaded = False
    started = time.monotonic()
    try:
        client.put_object(Bucket=bucket, Key=key, Body=payload, ServerSideEncryption="AES256")
        uploaded = True
        body = client.get_object(Bucket=bucket, Key=key)["Body"]
        try:
            downloaded = body.read()
        finally:
            body.close()
        if downloaded != payload:
            raise RuntimeError("S3 storage probe payload did not round-trip exactly")
    finally:
        if uploaded:
            client.delete_object(Bucket=bucket, Key=key)
    return {"status": "passed", "provider": "s3", "private_destination_verified": True, "bytes": len(payload), "round_trip_seconds": round(time.monotonic() - started, 3), "probe_object_deleted": True}


def download_remote(uri: str, destination: str | Path) -> Path:
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if uri.startswith("hf://datasets/"):
        from huggingface_hub import hf_hub_download
        parts = uri.removeprefix("hf://datasets/").split("/")
        if len(parts) < 3:
            raise ValueError("invalid Hugging Face dataset URI")
        repo_id = "/".join(parts[:2])
        filename = "/".join(parts[2:])
        downloaded = Path(hf_hub_download(repo_id=repo_id, filename=filename, repo_type="dataset", token=os.getenv("HF_TOKEN") or os.getenv("HUGGING_FACE_HUB_TOKEN")))
        shutil.copyfile(downloaded, destination)
    elif uri.startswith("s3://"):
        import boto3
        bucket, key = uri.removeprefix("s3://").split("/", 1)
        boto3.client("s3").download_file(bucket, key, str(destination))
    else:
        raise ValueError("unsupported snapshot destination")
    return destination


def restore_remote(uri: str, destination: str | Path) -> dict[str, Any]:
    restore_root = Path(destination).resolve()
    restore_root.mkdir(parents=True, exist_ok=False)
    archive_path = restore_root / "snapshot.zip"
    download_remote(uri, archive_path)
    marker_path = restore_root / "snapshot.manifest.json"
    download_remote(uri + ".manifest.json", marker_path)
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    archive_sha256 = _sha256(archive_path)
    if marker.get("schema_version") != 1 or marker.get("archive_sha256") != archive_sha256 or marker.get("archive_size_bytes") != archive_path.stat().st_size:
        raise ValueError("remote snapshot does not match its published hash marker")
    manifest = restore_snapshot(archive_path, restore_root / "restored")
    if marker.get("snapshot_id") != manifest["snapshot_id"]:
        raise ValueError("remote snapshot marker refers to a different snapshot ID")
    return {"manifest": manifest, "restore_path": str(restore_root / "restored"), "archive_sha256": archive_sha256}
