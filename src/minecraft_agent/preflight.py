"""Read-only host inventory used before installation or model downloads."""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_MODEL_REPOSITORIES = (
    "nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4",
    "Cloudflare/clef-flash",
)


def _command(argv: list[str], timeout: float = 5) -> dict[str, Any]:
    executable = shutil.which(argv[0])
    if not executable:
        return {"available": False}
    try:
        result = subprocess.run([executable, *argv[1:]], capture_output=True, text=True, timeout=timeout, check=False)
        text = (result.stdout + result.stderr).strip()
        return {"available": True, "returncode": result.returncode, "output": text[-1000:]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": True, "error": type(exc).__name__}


def _memory_bytes() -> int | None:
    try:
        if Path("/proc/meminfo").exists():
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    try:
        import resource
        return int(resource.getrlimit(resource.RLIMIT_AS)[0])
    except (ImportError, OSError, ValueError):
        return None


def _cgroup_memory_limit() -> int | None:
    for name in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = Path(name).read_text().strip()
            if raw != "max":
                value = int(raw)
                if value < (1 << 60):
                    return value
        except (OSError, ValueError):
            continue
    return None


def _affinity_cpu_count() -> int | None:
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return None


def _public_model_access(timeout: float = 5) -> list[dict[str, Any]]:
    """Read public model metadata without sending or displaying credentials."""
    results = []
    for repo_id in _MODEL_REPOSITORIES:
        request = Request(f"https://huggingface.co/api/models/{repo_id}", headers={"User-Agent": "minecraft-agent-harness/0.1"})
        try:
            with urlopen(request, timeout=timeout) as response:
                metadata = json.loads(response.read(1_000_000))
            card = metadata.get("cardData") or {}
            results.append({"repo_id": repo_id, "accessible": True, "access_mode": "public metadata", "revision": metadata.get("sha"), "license": card.get("license"), "license_name": card.get("license_name"), "license_link": card.get("license_link"), "gated": metadata.get("gated", False)})
        except HTTPError as exc:
            results.append({"repo_id": repo_id, "accessible": False, "http_status": exc.code, "error": "model metadata request was rejected"})
        except (OSError, URLError, ValueError) as exc:
            results.append({"repo_id": repo_id, "accessible": False, "error": type(exc).__name__})
    return results


def inspect(path: str | Path = ".") -> dict[str, Any]:
    target = Path(path).resolve()
    usage = shutil.disk_usage(target if target.exists() else target.parent)
    token_names = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
    # Only report configured/not configured. Never read or return secret values.
    credentials = {name: bool(os.getenv(name)) for name in token_names}
    remaining = os.getenv("MOLAB_SESSION_REMAINING_SECONDS")
    disk_capacity_reliable = usage.total < (1 << 60)
    hf_destination = bool(os.getenv("MINECRAFT_ARTIFACT_REPO"))
    s3_destination = bool(os.getenv("MINECRAFT_S3_BUCKET"))
    hf_credential = credentials["HF_TOKEN"] or credentials["HUGGING_FACE_HUB_TOKEN"]
    storage_provider = "huggingface" if hf_destination and hf_credential else "s3" if s3_destination else "huggingface" if hf_destination else None
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "host": {"system": platform.system(), "release": platform.release(), "machine": platform.machine(), "python": sys.version.split()[0], "host_cpu_count": os.cpu_count(), "effective_cpu_count": _affinity_cpu_count(), "host_memory_bytes": _memory_bytes(), "container_memory_limit_bytes": _cgroup_memory_limit(), "container_memory_limit_measured": _cgroup_memory_limit() is not None},
        "disk": {"path": str(target), "total_bytes": usage.total if disk_capacity_reliable else None, "free_bytes": usage.free if disk_capacity_reliable else None, "capacity_reliable": disk_capacity_reliable},
        "software": {"java": _command(["java", "-version"]), "node": _command(["node", "--version"]), "git": _command(["git", "--version"]), "nvidia_smi": _command(["nvidia-smi", "--query-gpu=name,memory.total,memory.free,driver_version", "--format=csv,noheader"]), "bwrap": _command(["bwrap", "--version"]), "docker": _command(["docker", "--version"])},
        "python_modules": {name: importlib.util.find_spec(name) is not None for name in ("marimo", "torch", "transformers", "huggingface_hub", "anywidget")},
        "sandbox": {"gvisor_hint": "gvisor" in platform.release().lower(), "uid": getattr(os, "getuid", lambda: None)(), "unshare": shutil.which("unshare") is not None, "bwrap": shutil.which("bwrap") is not None, "docker": shutil.which("docker") is not None},
        "storage": {"hf_token_configured": credentials["HF_TOKEN"] or credentials["HUGGING_FACE_HUB_TOKEN"], "aws_credentials_configured": credentials["AWS_ACCESS_KEY_ID"] and credentials["AWS_SECRET_ACCESS_KEY"], "destination_configured": storage_provider is not None, "destination_provider": storage_provider, "private_destination_verified": False},
        "session": {"remaining_seconds": int(remaining) if remaining and remaining.isdigit() else None, "limit_measured": bool(remaining and remaining.isdigit())},
        "license_acknowledgements": {"nemotron_openmdw_1_1": os.getenv("NEMOTRON_LICENSE_ACCEPTED", "false").lower() == "true", "minecraft_eula": os.getenv("MINECRAFT_EULA_ACCEPTED", "false").lower() == "true"},
        "network": {"dns_huggingface": _dns("huggingface.co")},
        "model_access": _public_model_access(),
    }


def resource_sample(pids: list[int] | None = None) -> dict[str, Any]:
    pids = pids or []
    processes = {}
    for pid in pids:
        try:
            status = Path(f"/proc/{pid}/status").read_text()
            rss = next((int(line.split()[1]) * 1024 for line in status.splitlines() if line.startswith("VmRSS:")), None)
            processes[str(pid)] = {"rss_bytes": rss}
        except OSError:
            processes[str(pid)] = {"rss_bytes": None}
    gpu = _command(["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu", "--format=csv,noheader,nounits"], timeout=3)
    return {"at_monotonic": time.monotonic(), "processes": processes, "gpu": gpu}


def summarize_resource_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    rss_peaks: dict[str, int] = {}
    gpu_peaks = []
    for sample in samples:
        for pid, record in sample.get("processes", {}).items():
            value = record.get("rss_bytes")
            if isinstance(value, int):
                rss_peaks[pid] = max(rss_peaks.get(pid, 0), value)
        output = sample.get("gpu", {}).get("output", "")
        for line in output.splitlines():
            try:
                gpu_peaks.append(int(line.split(",", 1)[0].strip()))
            except ValueError:
                continue
    return {"sample_count": len(samples), "process_rss_peak_bytes": rss_peaks, "gpu_memory_used_peak_bytes": max(gpu_peaks) * 1024 * 1024 if gpu_peaks else None}


class ResourceSampler:
    """Sample process RSS and GPU allocation during an explicit probe."""

    def __init__(self, pids: list[int], interval: float = 0.5):
        self.pids = pids
        self.interval = interval
        self.samples: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("resource sampler already started")
        self._thread = threading.Thread(target=self._sample_loop, daemon=True)
        self._thread.start()

    def _sample_loop(self) -> None:
        while not self._stop.is_set():
            self.samples.append(resource_sample(self.pids))
            self._stop.wait(self.interval)

    def stop(self) -> list[dict[str, Any]]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(1, self.interval * 3))
        return list(self.samples)


def _dns(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return True
    except OSError:
        return False
