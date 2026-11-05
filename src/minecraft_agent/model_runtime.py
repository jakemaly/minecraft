"""Optional Nemotron vLLM environment and local server."""

from __future__ import annotations

import os
import json
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


NEMOTRON_MODEL = "nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4"
NEMOTRON_REVISION = "da5b2650a835292c678994a1fa3fb653250fb508"
NEMOTRON_MAX_MODEL_LEN = 1_048_576
NEMOTRON_MAX_TOKENS = 16_384
LOCAL_VLLM_BASE_URL = "http://127.0.0.1:8000/v1"
LOCAL_PROVIDER_ID = "molab-vllm"
MAX_NUM_SEQS = 1
MAX_NUM_BATCHED_TOKENS = 8192
GPU_MEMORY_UTILIZATION = 0.89
MAMBA_BACKEND = "triton"
MAMBA_SSM_CACHE_DTYPE = "float16"


def download_nemotron(cache_dir: str | Path, token: str | None = None) -> Path:
    """Download only the reviewed immutable Nemotron snapshot."""
    from huggingface_hub import snapshot_download

    return Path(snapshot_download(
        repo_id=NEMOTRON_MODEL,
        revision=NEMOTRON_REVISION,
        cache_dir=str(cache_dir),
        token=token,
    ))


def probe_nemotron(base_url: str = LOCAL_VLLM_BASE_URL, timeout: float = 5) -> dict:
    """Check that the expected served model is reachable on this notebook only."""
    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.path.rstrip("/") != "/v1":
        raise ValueError("vLLM endpoint must be a loopback HTTP /v1 URL")
    request = urllib.request.Request(f"{base_url.rstrip('/')}/models", headers={"accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"local vLLM is unavailable: {type(exc).__name__}: {str(exc)[:250]}") from exc
    served = [item.get("id") for item in payload.get("data", []) if isinstance(item, dict)]
    if NEMOTRON_MODEL not in served:
        raise RuntimeError(f"local vLLM does not serve the pinned model ID {NEMOTRON_MODEL}")
    return {"status": "ready", "provider": LOCAL_PROVIDER_ID, "model": NEMOTRON_MODEL, "endpoint": base_url, "served_models": served}


def inference_config(base_url: str = LOCAL_VLLM_BASE_URL) -> dict:
    return {
        "provider": LOCAL_PROVIDER_ID,
        "endpoint": base_url,
        "model": NEMOTRON_MODEL,
        "revision": NEMOTRON_REVISION,
        "api": "openai-completions",
        "max_model_len": NEMOTRON_MAX_MODEL_LEN,
        "max_output_tokens": NEMOTRON_MAX_TOKENS,
        "max_num_seqs": MAX_NUM_SEQS,
        "max_num_batched_tokens": MAX_NUM_BATCHED_TOKENS,
        "gpu_memory_utilization": GPU_MEMORY_UTILIZATION,
        "mamba_backend": MAMBA_BACKEND,
        "mamba_ssm_cache_dtype": MAMBA_SSM_CACHE_DTYPE,
        "mamba_cache_mode": "align",
        "mamba_cache_stochastic_rounding": False,
        "tool_call_parser": "qwen3_coder",
        "reasoning_parser": "nemotron_v3",
    }


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _cuda_link_environment(project: Path) -> dict[str, str]:
    """Use the vLLM environment's CUDA runtime for extensions compiled at launch."""
    venv_nvcc = sorted(project.glob(".venv/lib/python*/site-packages/nvidia/cu13/bin/nvcc"))
    nvcc = str(venv_nvcc[0]) if venv_nvcc else shutil.which("nvcc")
    if not nvcc:
        return {}
    nvcc_path = Path(nvcc)
    cuda_root = nvcc_path.parent.parent
    libraries = sorted((cuda_root / "lib").glob("libcudart.so.*"))
    if not libraries:
        return {}
    link_dir = Path.home() / ".cache" / "minecraft-agent" / "cuda-link"
    link_dir.mkdir(parents=True, exist_ok=True)
    alias = link_dir / "libcudart.so"
    if alias.is_symlink() and alias.resolve() != libraries[-1].resolve():
        alias.unlink()
    elif alias.exists() and not alias.is_symlink():
        alias.unlink()
    if not alias.exists():
        alias.symlink_to(libraries[-1])
    return {
        "CUDA_HOME": str(cuda_root),
        "CUDA_PATH": str(cuda_root),
        # Pin FlashInfer's compiler to the same toolkit as its include headers.
        "FLASHINFER_NVCC": str(nvcc_path),
        "PATH": os.pathsep.join((str(Path(nvcc).parent), os.environ.get("PATH", ""))),
        "LIBRARY_PATH": os.pathsep.join((str(link_dir), str(cuda_root / "lib"), os.environ.get("LIBRARY_PATH", ""))),
        "LD_LIBRARY_PATH": os.pathsep.join((str(cuda_root / "lib"), os.environ.get("LD_LIBRARY_PATH", ""))),
    }


def start_nemotron(supervisor, run_dir: str | Path, model_path: str | Path):
    project = repo_root() / "environments" / "vllm"
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is unavailable")
    argv = [
        uv, "run", "--project", str(project), "--locked", "--", "vllm", "serve", str(model_path),
        "--host", "127.0.0.1", "--port", "8000", "--served-model-name", NEMOTRON_MODEL,
        "--max-model-len", str(NEMOTRON_MAX_MODEL_LEN), "--max-num-seqs", str(MAX_NUM_SEQS),
        "--max-num-batched-tokens", str(MAX_NUM_BATCHED_TOKENS), "--gpu-memory-utilization", str(GPU_MEMORY_UTILIZATION),
        "--enable-prefix-caching", "--enable-mamba-shared-prefix-checkpoint",
        "--mamba-backend", MAMBA_BACKEND, "--mamba-ssm-cache-dtype", MAMBA_SSM_CACHE_DTYPE,
        "--mamba-cache-mode", "align", "--video-pruning-rate", "0.5",
        "--media-io-kwargs", '{"video":{"fps":2,"num_frames":256}}',
        "--reasoning-parser", "nemotron_v3", "--enable-auto-tool-choice", "--tool-call-parser", "qwen3_coder", "--trust-remote-code",
    ]
    env = {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "FLASHINFER_WORKSPACE_BASE": str(Path(run_dir).resolve().parent / "flashinfer-workspace"),
    }
    env.update(_cuda_link_environment(project))
    return supervisor.start(
        "nemotron", argv, cwd=run_dir, env=env,
        ready_pattern=r"Application startup complete", timeout=1800,
    )
