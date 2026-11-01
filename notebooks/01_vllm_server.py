# /// script
# requires-python = ">=3.11"
# dependencies = ["marimo==0.23.11", "huggingface-hub==1.7.1"]
# ///
"""Optional local Nemotron vLLM server. This notebook does not control Minecraft."""

import marimo

__generated_with = "0.23.11"
app = marimo.App(width="medium")


@app.cell
def _():
    import os
    import sys
    from pathlib import Path
    import marimo as mo

    root = Path(__file__).resolve().parents[1]
    if str(root / "src") not in sys.path:
        sys.path.insert(0, str(root / "src"))
    from minecraft_agent.config import runtime_root
    from minecraft_agent.session import get_session

    session = get_session()
    runtime = runtime_root()
    return mo, os, runtime, session


@app.cell
def _(mo):
    mo.vstack([
        mo.md("# Local vLLM server\n\nDownload Nemotron, start one local OpenAI-compatible server, and stop it when finished. The Minecraft app uses Pi and remains separate."),
    ])
    return


@app.cell
def _(mo):
    download_button = mo.ui.run_button(label="Download pinned Nemotron snapshot")
    start_button = mo.ui.run_button(label="Start vLLM server")
    stop_button = mo.ui.run_button(label="Stop vLLM server")
    probe_button = mo.ui.run_button(label="Probe /v1/models")
    mo.vstack([download_button, start_button, stop_button, probe_button])
    return download_button, probe_button, start_button, stop_button


@app.cell
def _(download_button, mo, os, probe_button, runtime, session, start_button, stop_button):
    import json
    import urllib.request

    from minecraft_agent.model_runtime import start_nemotron
    from minecraft_agent.supervisor import Supervisor

    if session.claim_button("vllm_download", download_button.value):
        from huggingface_hub import snapshot_download

        snapshot = snapshot_download(
            repo_id="nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4",
            revision="da5b2650a835292c678994a1fa3fb653250fb508",
            cache_dir=str(runtime / "models-cache"),
            token=os.getenv("HF_TOKEN") or os.getenv("HUGGING_FACE_HUB_TOKEN"),
        )
        session.reports["vllm_model"] = {"status": "downloaded", "path": snapshot}

    if session.claim_button("vllm_start", start_button.value):
        model = session.reports.get("vllm_model", {}).get("path")
        if not model:
            session.reports["vllm_server"] = {"status": "blocked", "error": "download the model snapshot first"}
        elif session.vllm_supervisor and session.vllm_supervisor.status().get("processes", {}).get("nemotron", {}).get("running"):
            session.reports["vllm_server"] = {"status": "already running"}
        else:
            run_dir = runtime / "vllm-server"
            supervisor = Supervisor(run_dir, "vllm-server")
            process = start_nemotron(supervisor, run_dir, model)
            session.vllm_supervisor = supervisor
            session.reports["vllm_server"] = {"status": "ready", "pid": process.process.pid, "endpoint": "http://127.0.0.1:8000/v1"}

    if session.claim_button("vllm_stop", stop_button.value):
        if session.vllm_supervisor:
            session.vllm_supervisor.stop_all()
            session.vllm_supervisor = None
        session.reports["vllm_server"] = {"status": "stopped"}

    if session.claim_button("vllm_probe", probe_button.value):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000/v1/models", timeout=10) as response:
                session.reports["vllm_probe"] = {"status": response.status, "models": json.loads(response.read())}
        except Exception as exc:
            session.reports["vllm_probe"] = {"status": "unavailable", "error": f"{type(exc).__name__}: {str(exc)[:250]}"}

    mo.json({key: value for key, value in session.reports.items() if key.startswith("vllm_")})
    return


if __name__ == "__main__":
    app.run()
