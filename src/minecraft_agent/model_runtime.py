"""Pinned, separated model environment installation and local server launch."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


NEMOTRON_MAX_MODEL_LEN = 1_048_576
NEMOTRON_MAX_NUM_BATCHED_TOKENS = 8_192
NEMOTRON_GPU_MEMORY_UTILIZATION = 0.82


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def install_model_environments(timeout: int = 1800) -> dict[str, str]:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is required to install the locked model environments")
    result = {}
    for name in ("planner", "clef"):
        project = repo_root() / "environments" / name
        if not (project / "uv.lock").is_file():
            raise RuntimeError(f"{name} model environment has no lock file")
        proc = subprocess.run([uv, "sync", "--project", str(project), "--locked"], cwd=repo_root(), capture_output=True, text=True, timeout=timeout, check=False)
        if proc.returncode:
            raise RuntimeError(f"{name} environment install failed (exit {proc.returncode}): {(proc.stderr or proc.stdout)[-1200:]}")
        result[name] = "installed from lock"
    return result


def _hydration() -> dict:
    path = Path(os.getenv("MINECRAFT_AGENT_HOME", Path.home() / ".local/share/minecraft-agent")) / "models-cache" / "hydration-manifest.json"
    if not path.is_file():
        raise RuntimeError("download the selected model snapshots first")
    return json.loads(path.read_text(encoding="utf-8"))


def _cuda_link_environment(project: Path) -> dict[str, str]:
    """Prefer the model environment's compiler so JIT headers match its Torch CUDA runtime."""
    venv_nvcc = sorted(project.glob(".venv/lib/python*/site-packages/nvidia/cu13/bin/nvcc"))
    nvcc = str(venv_nvcc[0]) if venv_nvcc else shutil.which("nvcc")
    if not nvcc:
        return {}
    cuda_root = Path(nvcc).resolve().parent.parent
    runtime_libraries = sorted((cuda_root / "lib").glob("libcudart.so.*"))
    if not runtime_libraries:
        return {}
    link_dir = Path.home() / ".cache" / "minecraft-agent" / "cuda-link"
    link_dir.mkdir(parents=True, exist_ok=True)
    alias = link_dir / "libcudart.so"
    if alias.is_symlink():
        if alias.resolve() != runtime_libraries[-1].resolve():
            alias.unlink()
    elif alias.exists():
        alias.unlink()
    if not alias.exists():
        alias.symlink_to(runtime_libraries[-1])
    library_path = os.pathsep.join((str(link_dir), str(cuda_root / "lib"), os.environ.get("LIBRARY_PATH", ""))).rstrip(os.pathsep)
    runtime_path = os.pathsep.join((str(cuda_root / "lib"), os.environ.get("LD_LIBRARY_PATH", ""))).rstrip(os.pathsep)
    path = os.pathsep.join((str(Path(nvcc).parent), os.environ.get("PATH", ""))).rstrip(os.pathsep)
    return {"CUDA_HOME": str(cuda_root), "PATH": path, "LIBRARY_PATH": library_path, "LD_LIBRARY_PATH": runtime_path}


def start_nemotron(
    supervisor,
    run_dir: str | Path,
    *,
    max_model_len: int = NEMOTRON_MAX_MODEL_LEN,
    gpu_memory_utilization: float = NEMOTRON_GPU_MEMORY_UTILIZATION,
):
    if not 0.3 <= gpu_memory_utilization <= 0.98:
        raise ValueError("GPU memory utilization must be between 0.30 and 0.98")
    manifest = _hydration()
    snapshot = manifest["repositories"]["planner"]["snapshot_path"]
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is unavailable")
    project = repo_root() / "environments" / "planner"
    argv = [
        uv, "run", "--project", str(project), "--locked", "--", "vllm", "serve", snapshot,
        "--host", "127.0.0.1", "--port", "8000",
        "--served-model-name", manifest["repositories"]["planner"]["repo_id"],
        "--max-model-len", str(max_model_len), "--max-num-seqs", "1",
        "--max-num-batched-tokens", str(NEMOTRON_MAX_NUM_BATCHED_TOKENS),
        "--gpu-memory-utilization", str(gpu_memory_utilization),
        "--enable-prefix-caching", "--enable-mamba-shared-prefix-checkpoint",
        "--mamba-backend", "flashinfer", "--mamba-ssm-cache-dtype", "float16",
        "--enable-mamba-cache-stochastic-rounding", "--mamba-cache-philox-rounds", "5",
        "--mamba-cache-mode", "align",
        "--video-pruning-rate", "0.5",
        "--media-io-kwargs", '{"video":{"fps":2,"num_frames":256}}',
        "--reasoning-parser", "nemotron_v3", "--trust-remote-code",
    ]
    environment = {"RUN_ID": supervisor.run_id, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    environment.update(_cuda_link_environment(project))
    return supervisor.start("nemotron", argv, cwd=run_dir, env=environment, ready_pattern=r"Application startup complete", timeout=1800)


def start_clef(supervisor, run_dir: str | Path, *, port: int = 8765):
    manifest = _hydration()
    snapshot = manifest["repositories"]["selector"]["snapshot_path"]
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is unavailable")
    project = repo_root() / "environments" / "clef"
    argv = [uv, "run", "--project", str(project), "--locked", "--", "python", "-m", "minecraft_agent.clef_server", "--snapshot", snapshot, "--host", "127.0.0.1", "--port", str(port), "--device", "cuda"]
    return supervisor.start("clef", argv, cwd=run_dir, env={"RUN_ID": supervisor.run_id, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}, ready_pattern=r"CLEF_READY", timeout=1800)
