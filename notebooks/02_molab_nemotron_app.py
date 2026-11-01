# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["marimo==0.25.2", "huggingface-hub==1.7.1", "playwright==1.64.1"]
# ///
"""Molab-native Minecraft harness using the existing Pi loop and local Nemotron."""

import marimo

__generated_with = "0.25.2"
app = marimo.App(width="medium")


@app.cell
def _():
    import os
    import sys
    from pathlib import Path

    import marimo as mo

    project_root = Path(__file__).resolve().parents[1]
    if str(project_root / "src") not in sys.path:
        sys.path.insert(0, str(project_root / "src"))
    os.environ.setdefault(
        "MINECRAFT_AGENT_HOME",
        str(Path.home() / ".local/share/minecraft-agent/molab/agent"),
    )
    from minecraft_agent.molab_runtime import MolabRuntime
    from minecraft_agent.session import get_session

    notebook_session = get_session()
    molab_runtime = getattr(notebook_session, "molab_runtime", None)
    if molab_runtime is None or molab_runtime.repo_root != project_root:
        molab_runtime = MolabRuntime(project_root)
        notebook_session.molab_runtime = molab_runtime
    return mo, molab_runtime, notebook_session, Path, os


@app.cell
def _(mo):
    mo.md(
        "# Minecraft × Nemotron on Molab\n\n"
        "This runs the Minecraft server, Mineflayer bot, Pi agent loop, viewer capture, and Nemotron vLLM process inside this notebook runtime. The vLLM endpoint stays on `127.0.0.1`; no OpenAI sign-in or remote inference fallback is used.\n\n"
        "Prepare the runtime once per fresh Molab environment, download the pinned model, start vLLM, then initialize the harness and start a run. The existing objectives, tools, screenshots, workspace, and run controls are preserved."
    )
    return


@app.cell
def _(mo):
    prepare_button = mo.ui.run_button(label="Prepare runtime (Pi, Java, Chromium)")
    download_button = mo.ui.run_button(label="Download pinned Nemotron snapshot")
    start_model_button = mo.ui.run_button(label="Start local vLLM")
    probe_model_button = mo.ui.run_button(label="Probe local vLLM")
    stop_model_button = mo.ui.run_button(label="Stop vLLM and free GPU")
    start_harness_button = mo.ui.run_button(label="Initialize Minecraft harness")
    objective_select = mo.ui.dropdown(
        options={"Diamond pilot": "diamond_pilot", "Ender Dragon": "dragon_proof"},
        value="Diamond pilot",
        label="Objective",
    )
    start_run_button = mo.ui.run_button(label="Start run")
    pause_run_button = mo.ui.run_button(label="Pause run")
    resume_run_button = mo.ui.run_button(label="Resume run")
    stop_run_button = mo.ui.run_button(label="Stop run")
    export_button = mo.ui.run_button(label="Export run + RL transitions")
    refresh_button = mo.ui.run_button(label="Refresh status")
    mo.vstack(
        [
            mo.md("## Model and runtime"),
            mo.hstack([prepare_button, download_button]),
            mo.hstack([start_model_button, probe_model_button, stop_model_button]),
            mo.md("## Minecraft harness"),
            start_harness_button,
            mo.hstack([objective_select, start_run_button]),
            mo.hstack([pause_run_button, resume_run_button, stop_run_button]),
            mo.hstack([export_button, refresh_button]),
        ]
    )
    return (
        download_button,
        export_button,
        objective_select,
        pause_run_button,
        prepare_button,
        probe_model_button,
        refresh_button,
        resume_run_button,
        start_harness_button,
        start_model_button,
        start_run_button,
        stop_model_button,
        stop_run_button,
    )


@app.cell
def _(
    download_button,
    export_button,
    molab_runtime,
    notebook_session,
    objective_select,
    pause_run_button,
    prepare_button,
    probe_model_button,
    refresh_button,
    resume_run_button,
    start_harness_button,
    start_model_button,
    start_run_button,
    stop_model_button,
    stop_run_button,
    mo,
    os,
):
    def _attempt(action, key):
        try:
            value = action()
            notebook_session.reports[key] = {"status": "ok", "result": value}
            return value
        except Exception as exc:
            notebook_session.reports[key] = {
                "status": "error",
                "error": f"{type(exc).__name__}: {str(exc)[:600]}",
            }
            return None

    if notebook_session.claim_button("molab_prepare", prepare_button.value):
        if _attempt(molab_runtime.prepare_runtime, "runtime"):
            mo.status.toast("Molab runtime is ready — the Minecraft lab is wired up.")
    if notebook_session.claim_button("molab_download", download_button.value):
        _attempt(lambda: molab_runtime.download_model(os.getenv("HF_TOKEN") or os.getenv("HUGGING_FACE_HUB_TOKEN")), "model_download")
    if notebook_session.claim_button("molab_vllm_start", start_model_button.value):
        _attempt(molab_runtime.start_model, "vllm")
    if notebook_session.claim_button("molab_vllm_probe", probe_model_button.value):
        _attempt(molab_runtime.probe_model, "vllm_probe")
    if notebook_session.claim_button("molab_vllm_stop", stop_model_button.value):
        _attempt(molab_runtime.stop_model, "vllm")
    if notebook_session.claim_button("molab_harness_start", start_harness_button.value):
        _attempt(molab_runtime.start_harness, "harness")
    if notebook_session.claim_button("molab_run_start", start_run_button.value):
        _attempt(lambda: molab_runtime.start_run(objective_select.value), "run")
    if notebook_session.claim_button("molab_run_pause", pause_run_button.value):
        _attempt(molab_runtime.pause_run, "run_control")
    if notebook_session.claim_button("molab_run_resume", resume_run_button.value):
        _attempt(molab_runtime.resume_run, "run_control")
    if notebook_session.claim_button("molab_run_stop", stop_run_button.value):
        _attempt(molab_runtime.stop_run, "run_control")
    if notebook_session.claim_button("molab_run_export", export_button.value):
        _attempt(molab_runtime.export_run, "export")
    if notebook_session.claim_button("molab_refresh", refresh_button.value):
        _attempt(molab_runtime.status, "status")
    return


@app.cell
def _(mo, molab_runtime, notebook_session, Path):
    import json

    try:
        molab_status = molab_runtime.status()
    except Exception as exc:
        molab_status = {"error": f"{type(exc).__name__}: {str(exc)[:500]}"}
    latest_frame = molab_runtime.latest_frame()
    frame_view = mo.image(src=str(latest_frame), alt="Latest terrain-validated first-person frame") if latest_frame else mo.md("_No captured Minecraft frame yet._")
    exported = notebook_session.reports.get("export", {}).get("result", {})
    export_view = (
        mo.download(
            data=Path(exported["path"]).read_bytes(),
            filename=Path(exported["path"]).name,
            mimetype="application/zip",
            label="Download run archive and RL transitions",
        )
        if exported.get("path") and Path(exported["path"]).is_file()
        else mo.md("_Stop the run before exporting its world and transitions._")
    )
    mo.vstack(
        [
            mo.md("## Live state"),
            mo.json(molab_status),
            frame_view,
            mo.md("## Recent operation results"),
            mo.json(notebook_session.reports),
            export_view,
        ]
    )
    return


if __name__ == "__main__":
    app.run()
