# /// script
# requires-python = ">=3.11"
# dependencies = ["marimo==0.23.11", "anywidget==0.9.21", "huggingface-hub==1.7.1", "boto3==1.42.32"]
# ///
"""Explicit-control dashboard for local Nemotron, Clef-Flash, and Mineflayer."""

import marimo

__generated_with = "0.23.11"
app = marimo.App(width="full")


@app.cell
def _():
    import os
    import sys
    from pathlib import Path
    import marimo as mo

    _repo = Path(__file__).resolve().parents[1]
    for _path in (str(_repo), str(_repo / "src")):
        if _path not in sys.path:
            sys.path.insert(0, _path)
    from minecraft_agent.config import RunConfig, runtime_root
    from minecraft_agent.preflight import inspect
    from minecraft_agent.session import get_session

    session = get_session()
    config = RunConfig.from_environment()
    config.validate()
    return config, inspect, mo, os, runtime_root, session, _repo


@app.cell
def _(inspect, mo):
    _environment = inspect()
    mo.vstack([mo.md("# Minecraft agent runtime\n\nRead-only preflight. Side effects require their dedicated buttons below. Secret values are never displayed."), mo.json(_environment)])
    return (_environment,)


@app.cell
def _(mo):
    create_run_button = mo.ui.run_button(label="Create run workspace")
    isolation_button = mo.ui.run_button(label="Measure generated-code isolation (informational)")
    install_bridge_button = mo.ui.run_button(label="Install locked Mineflayer dependencies")
    install_env_button = mo.ui.run_button(label="Install locked model environments")
    hydrate_button = mo.ui.run_button(label="Download pinned model snapshots")
    individual_probe_button = mo.ui.run_button(label="Probe each model alone")
    combined_start_button = mo.ui.run_button(label="Start both models together")
    combined_probe_button = mo.ui.run_button(label="Run alternating co-residency probe")
    stop_models_button = mo.ui.run_button(label="Stop model processes")
    download_server_button = mo.ui.run_button(label="Download pinned vanilla server")
    prepare_server_button = mo.ui.run_button(label="Prepare loopback server")
    start_server_button = mo.ui.run_button(label="Start private server")
    connect_bot_button = mo.ui.run_button(label="Connect Mineflayer")
    observe_button = mo.ui.run_button(label="Refresh bounded observation")
    action_button = mo.ui.run_button(label="Run bounded look smoke action")
    viewer_button = mo.ui.run_button(label="Refresh observer view")
    practice_button = mo.ui.run_button(label="Run two-model practice smoke turn")
    stop_button = mo.ui.run_button(label="Pause and stop active processes")
    evidence_export_button = mo.ui.run_button(label="Export verified practice evidence")
    checkpoint_button = mo.ui.run_button(label="Create and verify local checkpoint")
    storage_probe_button = mo.ui.run_button(label="Check optional remote storage")
    upload_button = mo.ui.run_button(label="Export checkpoint (remote storage optional)")
    restore_button = mo.ui.run_button(label="Download and verify remote checkpoint")
    resume_button = mo.ui.run_button(label="Resume restored world")
    mo.vstack([
        mo.md("## Controls\n\nEach button action is consumed once, even when a reactive descendant reruns."),
        create_run_button, isolation_button, install_bridge_button, install_env_button, hydrate_button,
        individual_probe_button, combined_start_button, combined_probe_button, stop_models_button,
        download_server_button, prepare_server_button, start_server_button,
        connect_bot_button, observe_button, action_button, viewer_button,
        practice_button, stop_button, evidence_export_button, checkpoint_button, storage_probe_button, upload_button, restore_button, resume_button,
    ])
    return (create_run_button, isolation_button, install_bridge_button, install_env_button, hydrate_button,
            individual_probe_button, combined_start_button, combined_probe_button, stop_models_button,
            download_server_button, prepare_server_button, start_server_button,
            connect_bot_button, observe_button, action_button, viewer_button,
            practice_button, stop_button, evidence_export_button, checkpoint_button, storage_probe_button, upload_button, restore_button, resume_button)


@app.cell
def _(create_run_button, inspect, mo, runtime_root, session, _repo):
    def _run():
        from datetime import datetime, timezone
        import json
        import uuid
        from minecraft_agent.events import EventLog
        from minecraft_agent.identity import source_identity
        from minecraft_agent.supervisor import Supervisor

        if session.claim_button("create_run", create_run_button.value):
            if session.bridge and session.bridge.process.poll() is None:
                session.reports["create_run"] = {"status": "blocked", "error": "stop the current Mineflayer connection before creating another run"}
            elif session.supervisor and any(item.process.poll() is None for item in session.supervisor.processes.values()):
                session.reports["create_run"] = {"status": "blocked", "error": "stop the current run before creating another"}
            else:
                # Keep prior-run measurements attached to their original world.
                for _key in ("individual_model_probes", "combined_start", "combined_probe", "models_stop", "server_download", "server_prepare", "server_start", "bot_connect", "observation", "smoke_action", "viewer_snapshot", "viewer_widget", "practice", "operator_stop", "checkpoint", "checkpoint_observation", "checkpoint_upload", "checkpoint_restore", "restore_state_comparison", "resume", "evidence_export"):
                    session.reports.pop(_key, None)
                _run_id = "run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
                _world_id = "world-" + uuid.uuid4().hex[:12]
                _preflight = inspect()
                _run_dir = runtime_root() / "runs" / _run_id
                _server_dir = runtime_root() / "worlds" / _run_id / "server"
                _helper_dir = runtime_root() / "worlds" / _run_id / "agent" / "helpers"
                _run_dir.mkdir(parents=True, exist_ok=False)
                _helper_dir.mkdir(parents=True, exist_ok=False)
                _identity = source_identity(_repo)
                session.reports["active_server_dir"] = str(_server_dir)
                session.reports["active_helper_dir"] = str(_helper_dir)
                session.run_id = _run_id
                session.supervisor = Supervisor(_run_dir, _run_id)
                session.events = EventLog(_run_dir / "events" / "events.jsonl", _run_id, _world_id)
                session.reports.pop("checkpoint_observation", None)
                _manifest = {
                    "schema_version": 1, "run_id": _run_id, "world_id": _world_id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "config": __import__("minecraft_agent.config", fromlist=["RunConfig"]).RunConfig.from_environment().public_dict(),
                    "preflight": _preflight,
                    "models": {"planner": {"id": "nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4", "revision": "unresolved"}, "selector": {"id": "Cloudflare/clef-flash", "revision": "unresolved", "head": "joint_schema_model.py + joint_head.safetensors"}},
                    "game": {"minecraft_version": "1.21.4", "mineflayer_version": "4.39.0", "mode": "private loopback offline-mode smoke server"},
                    "dependencies": {**_identity, "lockfiles": ["uv.lock", "environments/planner/uv.lock", "environments/clef/uv.lock", "bridge/package-lock.json"]},
                    "observation_policy": {"blocks": "Mineflayer loaded region; occlusion is not inferred", "human_viewer_excluded_from_models": True},
                }
                (_run_dir / "manifest.json").write_text(json.dumps(_manifest, indent=2) + "\n", encoding="utf-8")
                session.events.append("run_created", {"manifest": _manifest})
                session.reports["create_run"] = {"status": "created", "run_id": _run_id, "path": str(_run_dir)}
        _report = session.reports.get("create_run", {"status": "waiting for explicit click"})
        return mo.md(f"Run: **{_report.get('run_id', 'not created')}** · **{_report['status']}** · {_report.get('error', '')}")
    _run()


@app.cell
def _(mo, session, storage_probe_button):
    def _run():
        if session.claim_button("storage_probe", storage_probe_button.value):
            try:
                from minecraft_agent.persistence import probe_configured_storage
                session.reports["storage_probe"] = probe_configured_storage()
            except Exception as exc:
                session.reports["storage_probe"] = {"status": "blocked", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
            if session.events:
                session.events.append("storage_probe", session.reports["storage_probe"])
        _report = session.reports.get("storage_probe", {"status": "waiting for explicit click"})
        return mo.md(f"Optional remote storage: **{_report['status']}** · {_report.get('provider', _report.get('error', ''))}")
    _run()


@app.cell
def _(install_bridge_button, inspect, mo, session, _repo):
    def _run():
        if session.claim_button("bridge_install", install_bridge_button.value):
            try:
                import json
                import subprocess
                import shutil
                _host = inspect()
                _node_text = _host["software"]["node"].get("output", "")
                if not _node_text.startswith("v") or int(_node_text[1:].split(".")[0]) < 22:
                    raise RuntimeError("Mineflayer 4.39.0 requires Node.js 22 or later")
                _npm = shutil.which("npm")
                if not _npm:
                    raise RuntimeError("npm is unavailable")
                _bridge = _repo / "bridge"
                _install = subprocess.run([_npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=_bridge, capture_output=True, text=True, timeout=600, check=True)
                _version = subprocess.run([_npm, "ls", "mineflayer", "--depth=0", "--json"], cwd=_bridge, capture_output=True, text=True, timeout=30, check=True)
                _tree = json.loads(_version.stdout)
                session.reports["bridge_install"] = {"status": "complete", "mineflayer": _tree["dependencies"]["mineflayer"]["version"], "node": _node_text, "lockfile": "bridge/package-lock.json", "install_output_tail": _install.stdout[-1000:]}
            except Exception as exc:
                session.reports["bridge_install"] = {"status": "blocked", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
            if session.events:
                session.events.append("bridge_install", session.reports["bridge_install"])
        _report = session.reports.get("bridge_install", {"status": "waiting for explicit click"})
        return mo.md(f"Mineflayer dependencies: **{_report['status']}** · {_report.get('mineflayer', _report.get('error', ''))}")
    _run()


@app.cell
def _(install_env_button, mo, session):
    def _run():
        if session.claim_button("model_env_install", install_env_button.value):
            try:
                from minecraft_agent.model_runtime import install_model_environments
                session.reports["model_env_install"] = {"status": "complete", "environments": install_model_environments()}
            except Exception as exc:
                session.reports["model_env_install"] = {"status": "blocked", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
            if session.events:
                session.events.append("model_env_install", session.reports["model_env_install"])
        _report = session.reports.get("model_env_install", {"status": "waiting for explicit click"})
        return mo.md(f"Model environments: **{_report['status']}** · {_report.get('error', '')}")
    _run()


@app.cell
def _(hydrate_button, mo, runtime_root, session):
    def _run():
        if session.claim_button("model_hydration", hydrate_button.value):
            try:
                from minecraft_agent.model_storage import estimate_and_hydrate
                session.reports["model_hydration"] = {"status": "complete", **estimate_and_hydrate(runtime_root() / "models-cache")}
                if session.run_id:
                    import json
                    _manifest_path = session.supervisor.run_dir / "manifest.json"
                    _manifest = json.loads(_manifest_path.read_text(encoding="utf-8"))
                    for _role, _model in session.reports["model_hydration"]["repositories"].items():
                        _manifest["models"][_role].update({"revision": _model["revision"], "license": _model.get("license"), "sha256": _model["sha256"]})
                    _manifest_path.write_text(json.dumps(_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            except Exception as exc:
                session.reports["model_hydration"] = {"status": "blocked", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
            if session.events:
                session.events.append("model_hydration", {"status": session.reports["model_hydration"]["status"], "error": session.reports["model_hydration"].get("error")})
        _report = session.reports.get("model_hydration", {"status": "waiting for explicit click"})
        return mo.md(f"Model hydration: **{_report['status']}** · {_report.get('error', '')}")
    _run()


@app.cell
def _(isolation_button, mo, session):
    def _run():
        if session.claim_button("isolation_probe", isolation_button.value):
            from minecraft_agent.sandbox import probe
            _probe = probe()
            session.reports["isolation"] = {"available": _probe.available, "reason": _probe.reason}
            if session.events:
                session.events.append("isolation_probe", session.reports["isolation"])
        _report = session.reports.get("isolation", {"available": False, "reason": "probe not run"})
        return mo.md(f"Generated-code isolation (informational): **{'available' if _report['available'] else 'not enforced'}** · {_report['reason']} · helper execution proceeds under the operator-authorized direct worker mode.")
    _run()


@app.cell
def _(individual_probe_button, mo, session):
    def _run():
        if session.claim_button("individual_model_probes", individual_probe_button.value):
            _completed = {}
            try:
                if not session.supervisor:
                    raise RuntimeError("create a run workspace first")
                from minecraft_agent.model_runtime import start_clef, start_nemotron
                from minecraft_agent.models.interfaces import Candidate
                from minecraft_agent.models.local_clef import LocalClefClient
                from minecraft_agent.models.local_nemotron import LocalNemotron
                from minecraft_agent.preflight import ResourceSampler, summarize_resource_samples
                import time

                _planner_process = start_nemotron(session.supervisor, session.supervisor.run_dir)
                _planner = LocalNemotron("http://127.0.0.1:8000/v1", "nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4")
                _sampler = ResourceSampler([_planner_process.process.pid]); _sampler.start()
                try:
                    _started = time.monotonic()
                    _text = _planner.propose({"status": "ready"}, "Say ready in a short rationale", "")
                    _text_latency = time.monotonic() - _started
                    _started = time.monotonic()
                    _structured = _planner.propose({"player": {"health": 20, "food": 20}, "inventory": [], "nearby_blocks": [], "nearby_entities": []}, "Propose one harmless bounded look action using the supplied observation", "")
                    _samples = _sampler.stop()
                    _completed["planner"] = {"status": "passed", "artifact_revision": session.reports["model_hydration"]["repositories"]["planner"]["revision"], "text_response_chars": len(_text.raw_response), "structured_response_chars": len(_structured.raw_response), "text_latency_seconds": round(_text_latency, 3), "structured_latency_seconds": round(time.monotonic() - _started, 3), "resource_peaks": summarize_resource_samples(_samples), "resource_samples": _samples}
                finally:
                    _sampler.stop(); session.supervisor.stop_process("nemotron")

                _selector_process = start_clef(session.supervisor, session.supervisor.run_dir)
                _selector = LocalClefClient()
                _candidates = [Candidate("wait", "Wait briefly for a fresh state", "primitive", {"type": "wait", "duration_ms": 100}), Candidate("replan", "Return control to the planner", "replan", {})]
                _sampler = ResourceSampler([_selector_process.process.pid]); _sampler.start()
                try:
                    _started = time.monotonic()
                    _scores = _selector.choose({"player": {"health": 20, "food": 20}}, "Choose a safe bounded smoke action", _candidates)
                    _samples = _sampler.stop()
                    _completed["selector"] = {"status": "passed", "artifact_revision": session.reports["model_hydration"]["repositories"]["selector"]["revision"], "typed_scores": _scores, "latency_seconds": round(time.monotonic() - _started, 3), "resource_peaks": summarize_resource_samples(_samples), "resource_samples": _samples, "model": "Cloudflare/clef-flash joint schema head"}
                finally:
                    _sampler.stop(); session.supervisor.stop_process("clef")
                session.reports["individual_model_probes"] = {"status": "passed", "models": _completed}
            except Exception as exc:
                session.reports["individual_model_probes"] = {"status": "blocked", "error": f"{type(exc).__name__}: {str(exc)[:500]}", "completed": _completed}
            if session.events:
                session.events.append("individual_model_probes", session.reports["individual_model_probes"])
        _report = session.reports.get("individual_model_probes", {"status": "waiting for explicit click"})
        return mo.md(f"Individual model probes: **{_report['status']}** · {_report.get('error', '')}")
    _run()


@app.cell
def _(combined_start_button, mo, runtime_root, session):
    def _run():
        if session.claim_button("combined_start", combined_start_button.value):
            try:
                if not session.supervisor:
                    raise RuntimeError("create a run workspace first")
                from minecraft_agent.model_runtime import start_clef, start_nemotron
                import json
                from minecraft_agent.models.local_clef import LocalClefClient
                from minecraft_agent.models.local_nemotron import LocalNemotron
                _hydration = json.loads((runtime_root() / "models-cache" / "hydration-manifest.json").read_text(encoding="utf-8"))
                _run_manifest_path = session.supervisor.run_dir / "manifest.json"
                _run_manifest = json.loads(_run_manifest_path.read_text(encoding="utf-8"))
                for _role, _model in _hydration["repositories"].items():
                    _run_manifest["models"][_role].update({"revision": _model["revision"], "license": _model.get("license"), "sha256": _model["sha256"]})
                _run_manifest_path.write_text(json.dumps(_run_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                start_nemotron(session.supervisor, session.supervisor.run_dir)
                session.planner = LocalNemotron("http://127.0.0.1:8000/v1", "nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4")
                start_clef(session.supervisor, session.supervisor.run_dir)
                session.selector = LocalClefClient()
                from minecraft_agent.supervisor import RunState
                if session.supervisor.state == RunState.PREFLIGHT:
                    session.supervisor.transition(RunState.MODELS_READY)
                session.reports["combined_start"] = {"status": "both model processes resident", "planner_pid": session.supervisor.processes["nemotron"].process.pid, "selector_pid": session.supervisor.processes["clef"].process.pid}
            except Exception as exc:
                if session.supervisor:
                    for _name in ("clef", "nemotron"):
                        try:
                            session.supervisor.stop_process(_name)
                        except Exception:
                            pass
                session.planner = None; session.selector = None
                session.reports["combined_start"] = {"status": "blocked", "error": f"{type(exc).__name__}: {str(exc)[:500]}"}
            if session.events:
                session.events.append("combined_start", session.reports["combined_start"])
        _report = session.reports.get("combined_start", {"status": "waiting for explicit click"})
        return mo.md(f"Combined models: **{_report['status']}** · {_report.get('error', '')}")
    _run()


@app.cell
def _(combined_probe_button, mo, session):
    def _run():
        if session.claim_button("combined_probe", combined_probe_button.value):
            try:
                if not session.planner or not session.selector:
                    raise RuntimeError("start both exact selected models first; sequential swaps do not count as co-residency")
                from minecraft_agent.preflight import ResourceSampler, summarize_resource_samples
                _pids = [session.supervisor.processes[_name].process.pid for _name in ("nemotron", "clef")]
                _sampler = ResourceSampler(_pids); _sampler.start()
                _sample = {"player": {"health": 20, "food": 20}, "nearby_blocks": [], "nearby_entities": []}
                _rounds = []
                try:
                    for _index in range(3):
                        import time
                        _planner_started = time.monotonic()
                        _proposal = session.planner.propose(_sample, "Choose among harmless observation actions; do not generate gameplay strategy", "")
                        _planner_latency = time.monotonic() - _planner_started
                        _selector_started = time.monotonic()
                        _scores = session.selector.choose(_sample, _proposal.objective, list(_proposal.candidates))
                        _selector_latency = time.monotonic() - _selector_started
                        _rounds.append({"round": _index, "planner_response_chars": len(_proposal.raw_response), "planner_latency_seconds": _planner_latency, "selector_latency_seconds": _selector_latency, "candidate_ids": [item.candidate_id for item in _proposal.candidates], "selector_scores": _scores})
                    _long_memory = "Prior verified state summary: inventory and health remain unchanged; no movement or inventory action has been taken. " * 38
                    _long_proposal = session.planner.propose(_sample, "Read this longer context and propose one safe bounded observation action", _long_memory)
                    _long_scores = session.selector.choose(_sample, _long_proposal.objective, list(_long_proposal.candidates))
                    _rounds.append({"round": "long_context", "context_chars": len(_long_memory), "planner_response_chars": len(_long_proposal.raw_response), "candidate_ids": [item.candidate_id for item in _long_proposal.candidates], "selector_scores": _long_scores})
                    for _index in range(2):
                        _proposal = session.planner.propose(_sample, "After the longer context, propose one harmless observation action", _long_memory)
                        _scores = session.selector.choose(_sample, _proposal.objective, list(_proposal.candidates))
                        _rounds.append({"round": f"post_long_context_{_index + 1}", "planner_response_chars": len(_proposal.raw_response), "candidate_ids": [item.candidate_id for item in _proposal.candidates], "selector_scores": _scores})
                finally:
                    _samples = _sampler.stop()
                session.reports["combined_probe"] = {"status": "alternating requests complete", "planner_revision": session.reports["model_hydration"]["repositories"]["planner"]["revision"], "selector_revision": session.reports["model_hydration"]["repositories"]["selector"]["revision"], "rounds": _rounds, "resource_peaks": summarize_resource_samples(_samples), "resource_samples": _samples}
            except Exception as exc:
                session.reports["combined_probe"] = {"status": "blocked", "error": f"{type(exc).__name__}: {str(exc)[:500]}"}
            if session.events:
                session.events.append("combined_probe", session.reports["combined_probe"])
        _report = session.reports.get("combined_probe", {"status": "waiting for explicit click"})
        return mo.md(f"Co-residency: **{_report['status']}** · {_report.get('error', '')}")
    _run()


@app.cell
def _(mo, session, stop_models_button):
    def _run():
        if session.claim_button("models_stop", stop_models_button.value):
            try:
                for _name in ("clef", "nemotron"):
                    if session.supervisor and _name in session.supervisor.processes:
                        session.supervisor.stop_process(_name)
                session.planner = None; session.selector = None
                session.reports["models_stop"] = {"status": "stopped"}
            except Exception as exc:
                session.reports["models_stop"] = {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
            if session.events:
                session.events.append("models_stop", session.reports["models_stop"])
        _report = session.reports.get("models_stop", {"status": "waiting for explicit click"})
        return mo.md(f"Model processes: **{_report['status']}** · {_report.get('error', '')}")
    _run()


@app.cell
def _(download_server_button, mo, runtime_root, session, config):
    def _run():
        if session.claim_button("server_download", download_server_button.value):
            try:
                if not session.run_id:
                    raise RuntimeError("create a run workspace first")
                import hashlib, json, subprocess, sys
                from pathlib import Path
                _repo = __import__("pathlib").Path(__file__).resolve().parents[1]
                _server_dir = __import__("pathlib").Path(session.reports.get("active_server_dir", runtime_root() / "worlds" / session.run_id / "server"))
                _result = subprocess.run([sys.executable, str(_repo / "scripts/server.py"), "--version", config.minecraft_version, "--directory", str(_server_dir), "--download"], capture_output=True, text=True, timeout=240, check=True)
                _jar = Path(_result.stdout.strip())
                _digest = hashlib.sha1(_jar.read_bytes()).hexdigest()
                _run_manifest_path = session.supervisor.run_dir / "manifest.json"
                _run_manifest = json.loads(_run_manifest_path.read_text(encoding="utf-8"))
                _run_manifest["game"]["server_jar_sha1"] = _digest
                _run_manifest_path.write_text(json.dumps(_run_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                session.reports["server_download"] = {"status": "downloaded_and_verified", "artifact": str(_jar), "sha1": _digest}
            except Exception as exc:
                session.reports["server_download"] = {"status": "blocked", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
            if session.events:
                session.events.append("server_download", session.reports["server_download"])
        _report = session.reports.get("server_download", {"status": "waiting for explicit click"})
        return mo.md(f"Server artifact: **{_report['status']}** · {_report.get('artifact', _report.get('error', ''))}")
    _run()


@app.cell
def _(mo, prepare_server_button, runtime_root, session):
    def _run():
        if session.claim_button("server_prepare", prepare_server_button.value):
            try:
                if not session.run_id:
                    raise RuntimeError("create a run workspace first")
                from scripts.server import prepare_server
                _server_dir = __import__("pathlib").Path(session.reports.get("active_server_dir", runtime_root() / "worlds" / session.run_id / "server"))
                _jar = next(_server_dir.glob("minecraft_server.*.jar"))
                prepare_server(_server_dir)
                session.reports["server_prepare"] = {"status": "prepared", "jar": str(_jar)}
            except Exception as exc:
                session.reports["server_prepare"] = {"status": "blocked", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
            if session.events:
                session.events.append("server_prepare", session.reports["server_prepare"])
        _report = session.reports.get("server_prepare", {"status": "waiting for explicit click"})
        return mo.md(f"Server preparation: **{_report['status']}** · {_report.get('error', '')}")
    _run()


@app.cell
def _(mo, runtime_root, session, start_server_button):
    def _run():
        if session.claim_button("server_start", start_server_button.value):
            try:
                if not session.supervisor or not session.run_id:
                    raise RuntimeError("create a run workspace first")
                from minecraft_agent.supervisor import RunState
                _server_dir = __import__("pathlib").Path(session.reports.get("active_server_dir", runtime_root() / "worlds" / session.run_id / "server"))
                _jar = next(_server_dir.glob("minecraft_server.*.jar"))
                _proc = session.supervisor.start("minecraft-server", ["java", "-Xms1G", "-Xmx2G", "-jar", str(_jar), "nogui"], cwd=_server_dir, env={"RUN_ID": session.run_id}, ready_pattern=r"Done \(.*\)! For help", timeout=180)
                if session.supervisor.state in {RunState.PREFLIGHT, RunState.MODELS_READY}:
                    session.supervisor.transition(RunState.WORLD_READY)
                session.reports["server_start"] = {"status": "ready", "pid": _proc.process.pid, "endpoint": "127.0.0.1:25565"}
            except Exception as exc:
                session.reports["server_start"] = {"status": "blocked", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
            if session.events:
                session.events.append("server_start", session.reports["server_start"])
        _report = session.reports.get("server_start", {"status": "waiting for explicit click"})
        return mo.md(f"Server: **{_report['status']}** · {_report.get('error', _report.get('endpoint', ''))}")
    _run()


@app.cell
def _(connect_bot_button, config, mo, os, session, _repo):
    def _run():
        if session.claim_button("bot_connect", connect_bot_button.value):
            try:
                if not session.supervisor:
                    raise RuntimeError("create a run workspace first")
                if session.bridge and session.bridge.process.poll() is None:
                    raise RuntimeError("Mineflayer is already connected; stop it before reconnecting")
                if session.bridge:
                    session.bridge.stop(); session.bridge = None
                from minecraft_agent.bridge_client import BridgeClient
                _child_env = {"PATH": os.getenv("PATH", ""), "HOME": os.getenv("HOME", ""), "LANG": os.getenv("LANG", "C.UTF-8"), "RUN_ID": session.run_id, "MINECRAFT_VERSION": config.minecraft_version, "MINECRAFT_HOST": "127.0.0.1", "MINECRAFT_PORT": "25565", "MINECRAFT_VIEWER_SEQUENCE_OFFSET": str((session.reports.get("viewer_snapshot") or {}).get("sequence", 0))}
                session.bridge = BridgeClient(["node", str(_repo / "bridge/src/bot.js")], cwd=_repo / "bridge", env=_child_env, timeout=90)
                _ready = session.bridge.ready_event or {}
                _observation = session.bridge.request("observe", {})
                session.reports["observation"] = _observation
                _comparison = None
                _restore = session.reports.get("checkpoint_restore", {})
                if _restore.get("status") == "verified_download_and_extracted":
                    from minecraft_agent.persistence import compare_player_state
                    _expected = _restore["manifest"].get("metadata", {}).get("checkpoint_observation")
                    _comparison = compare_player_state(_expected, _observation)
                    session.reports["restore_state_comparison"] = _comparison
                    if session.events:
                        session.events.append("restore_player_state_comparison", {"comparison": _comparison, "fresh_observation": _observation})
                _connect_status = "connected" if _comparison is None or _comparison["status"] == "passed" else "connected_state_mismatch"
                session.reports["bot_connect"] = {"status": _connect_status, "minecraft_version": _ready.get("minecraft_version"), "username": _ready.get("username"), "restore_state_comparison": _comparison}
                _manifest_path = session.supervisor.run_dir / "manifest.json"
                _manifest = __import__("json").loads(_manifest_path.read_text(encoding="utf-8"))
                _manifest["game"]["server_protocol_version"] = _ready.get("minecraft_version")
                _manifest_path.write_text(__import__("json").dumps(_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            except Exception as exc:
                session.reports["bot_connect"] = {"status": "blocked", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
            if session.events:
                session.events.append("bot_connect", session.reports["bot_connect"])
        _report = session.reports.get("bot_connect", {"status": "waiting for explicit click"})
        return mo.md(f"Mineflayer: **{_report['status']}** · {_report.get('error', (_report.get('restore_state_comparison') or {}).get('status', ''))}")
    _run()


@app.cell
def _(mo, observe_button, session):
    def _run():
        if session.claim_button("bot_observe", observe_button.value):
            try:
                if not session.bridge:
                    raise RuntimeError("connect Mineflayer first")
                session.reports["observation"] = session.bridge.request("observe", {})
                if session.events:
                    session.events.append("observation", {"model_visible": session.reports["observation"], "source": "operator refresh"})
            except Exception as exc:
                session.reports["observation"] = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        return mo.json(session.reports.get("observation", {"status": "waiting for explicit click"}))
    _run()


@app.cell
def _(action_button, mo, session):
    def _run():
        if session.claim_button("smoke_action", action_button.value):
            try:
                if not session.bridge:
                    raise RuntimeError("connect Mineflayer first")
                _result = session.bridge.request("execute", {"action_id": "smoke-" + str(action_button.value), "action": {"type": "look", "yaw": 0.0, "pitch": 0.15}, "timeout_seconds": 3})
                session.reports["smoke_action"] = {"status": "completed", "result": _result}
                if session.events:
                    session.events.append("smoke_action", session.reports["smoke_action"])
            except Exception as exc:
                session.reports["smoke_action"] = {"status": "blocked", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        _report = session.reports.get("smoke_action", {"status": "waiting for explicit click"})
        return mo.md(f"Bounded Mineflayer look: **{_report['status']}** · {_report.get('error', '')}")
    _run()


@app.cell
def _(mo, session, viewer_button):
    def _run():
        if session.claim_button("viewer_refresh", viewer_button.value):
            try:
                if not session.bridge:
                    raise RuntimeError("connect Mineflayer first")
                from viewer.widget import build_widget
                import json
                import time
                _started = time.monotonic()
                _snapshot = session.bridge.request("viewer_snapshot", {}, request_timeout=0.15)
                _snapshot["request_round_trip_ms"] = round((time.monotonic() - _started) * 1000, 3)
                _size = len(json.dumps(_snapshot).encode("utf-8"))
                if _size > 100_000:
                    raise RuntimeError("viewer snapshot exceeded transfer limit")
                session.reports["viewer_snapshot"] = _snapshot
                session.reports["viewer_widget"] = build_widget(_snapshot, session)
                if session.events:
                    session.events.append("viewer_snapshot", {"sequence": _snapshot["sequence"], "blocks": len(_snapshot["blocks"]), "bytes": _size, "request_round_trip_ms": _snapshot["request_round_trip_ms"], "capture_ms": _snapshot["capture_ms"], "physics_tick_samples": _snapshot["physics_tick_samples"], "physics_tick_p95_ms": _snapshot["physics_tick_p95_ms"]})
            except Exception as exc:
                session.reports["viewer_snapshot"] = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        _widget = session.reports.get("viewer_widget")
        if _widget is not None:
            return _widget
        return mo.md("Custom Three.js snapshot observer is idle. Click **Refresh observer view** after Mineflayer connects. Upstream Prismarine Viewer rendering is not integrated or verified yet.")
    _run()


@app.cell
def _(mo, practice_button, runtime_root, session):
    def _run():
        if session.claim_button("practice_smoke", practice_button.value):
            try:
                from minecraft_agent.agent import AgentStep
                from minecraft_agent.helper import HelperRunner
                from minecraft_agent.supervisor import RunState
                if not session.planner or not session.selector or not session.bridge:
                    raise RuntimeError("load both models, start the server, and connect Mineflayer first")
                if session.supervisor.state == RunState.WORLD_READY:
                    session.supervisor.transition(RunState.RUNNING)
                _helper_dir = __import__("pathlib").Path(session.reports.get("active_helper_dir", runtime_root() / "worlds" / session.run_id / "agent" / "helpers"))
                _helper_runner = HelperRunner(session.bridge, _helper_dir, timeout_seconds=10, allowed_action_types={"look", "wait"}, max_actions=1)
                _step = AgentStep(session.planner, session.selector, session.bridge, session.events, _helper_runner)
                _result = _step.run("Practice the integration loop: propose an agent-authored JavaScript helper whose exact source is await api.act({type:'look', yaw:0.1, pitch:0}); include it as a candidate alongside replan.", allowed_action_types={"look"}, required_candidate_kind="helper")
                _action_records = _result["result"].get("action_results", [])
                _mineflayer_result = _action_records[0]["result"] if len(_action_records) == 1 else {}
                _fresh_observation = _result["result"].get("observation")
                _did_action = (_result["selected"].kind == "helper" and _result["result"].get("status") == "completed" and _result["result"].get("actions") == 1 and len(_action_records) == 1 and _mineflayer_result.get("status") == "completed" and _mineflayer_result.get("controls_released") is True and isinstance(_fresh_observation, dict))
                session.supervisor.transition(RunState.PAUSED)
                if not _did_action:
                    raise RuntimeError("the planner, Clef selector, and helper did not complete exactly one permitted Mineflayer action; inspect the trace")
                session.reports["practice"] = {"status": "passed", "turn_id": _result["turn_id"], "candidate_id": _result["selected"].candidate_id, "scores": _result["scores"], "helper": {key: _result["result"].get(key) for key in ("helper", "revision", "helper_sha256", "execution_mode")}, "action_id": _action_records[0]["action_id"], "mineflayer_result": _mineflayer_result, "fresh_observation": _fresh_observation}
            except Exception as exc:
                if session.supervisor and session.supervisor.state == RunState.RUNNING:
                    from minecraft_agent.supervisor import RunState
                    session.supervisor.transition(RunState.PAUSED)
                session.reports["practice"] = {"status": "blocked", "error": f"{type(exc).__name__}: {str(exc)[:400]}"}
            if session.events:
                session.events.append("practice_smoke", session.reports["practice"])
        _report = session.reports.get("practice", {"status": "waiting for explicit click"})
        return mo.md(f"Practice run: **{_report['status']}** · {_report.get('error', '')}")
    _run()


@app.cell
def _(mo, runtime_root, session, stop_button):
    def _run():
        if session.claim_button("operator_stop", stop_button.value):
            try:
                if session.bridge:
                    try:
                        _final_observation = session.bridge.request("observe", {})
                        session.reports["checkpoint_observation"] = _final_observation
                        if session.events:
                            session.events.append("checkpoint_pre_observation", {"model_visible": _final_observation, "source": "operator stop"})
                    except Exception as _observe_error:
                        session.reports["checkpoint_observation"] = None
                        if session.events:
                            session.events.append("checkpoint_pre_observation_error", {"error_type": type(_observe_error).__name__})
                    session.bridge.stop(); session.bridge = None
                if session.supervisor:
                    session.supervisor.stop_minecraft_server()
                    session.supervisor.stop_all()
                session.planner = None; session.selector = None
                session.reports["operator_stop"] = {"status": "stopped cleanly"}
            except Exception as exc:
                session.reports["operator_stop"] = {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
            if session.events:
                session.events.append("operator_stop", session.reports["operator_stop"])
        _report = session.reports.get("operator_stop", {"status": "waiting for explicit click"})
        return mo.md(f"Process cleanup: **{_report['status']}** · {_report.get('error', '')}")
    _run()


@app.cell
def _(checkpoint_button, mo, runtime_root, session):
    def _run():
        if session.claim_button("checkpoint", checkpoint_button.value):
            try:
                if not session.supervisor or not session.run_id:
                    raise RuntimeError("create a run first")
                from minecraft_agent.persistence import create_snapshot
                import json
                import uuid
                from minecraft_agent.supervisor import RunState
                _run_dir = session.supervisor.run_dir
                _world_dir = __import__("pathlib").Path(session.reports.get("active_server_dir", runtime_root() / "worlds" / session.run_id / "server"))
                _helper_dir = __import__("pathlib").Path(session.reports.get("active_helper_dir", runtime_root() / "worlds" / session.run_id / "agent" / "helpers"))
                _destination = runtime_root() / "runs" / (session.run_id + "-" + uuid.uuid4().hex[:12] + ".zip")
                if session.bridge:
                    session.reports["checkpoint_observation"] = session.bridge.request("observe", {})
                    if session.events:
                        session.events.append("checkpoint_pre_observation", {"model_visible": session.reports["checkpoint_observation"], "source": "checkpoint"})
                    session.bridge.stop(); session.bridge = None
                if session.supervisor.state in {RunState.WORLD_READY, RunState.RUNNING}:
                    session.supervisor.transition(RunState.PAUSED)
                if session.supervisor.state in {RunState.PAUSED, RunState.WORLD_READY}:
                    session.supervisor.transition(RunState.CHECKPOINTING)
                if session.supervisor.state == RunState.CHECKPOINTING:
                    session.supervisor.stop_minecraft_server()
                    for _name in ("clef", "nemotron"):
                        session.supervisor.stop_process(_name)
                    session.planner = None; session.selector = None
                    session.supervisor.transition(RunState.PAUSED)
                if session.events:
                    session.events.append("checkpoint_begin", {"state": session.supervisor.state.value, "elapsed_seconds": session.events.elapsed_seconds})
                _run_manifest = json.loads((_run_dir / "manifest.json").read_text(encoding="utf-8"))
                _previous = session.reports.get("checkpoint", {}).get("snapshot_id")
                _manifest = create_snapshot({"agent": _run_dir, "world": _world_dir, "helpers": _helper_dir}, _destination, metadata={"run_id": session.run_id, "world_id": _run_manifest["world_id"], "state": session.supervisor.state.value, "final_event_sequence": session.events.sequence if session.events else 0, "elapsed_seconds": session.events.elapsed_seconds if session.events else 0.0, "checkpoint_observation": session.reports["checkpoint_observation"]}, previous_snapshot_id=_previous)
                session.reports["checkpoint"] = {"status": "verified_local_only", "snapshot_id": _manifest["snapshot_id"], "path": str(_destination), "remote": "not configured"}
            except Exception as exc:
                try:
                    from minecraft_agent.supervisor import RunState
                    if session.supervisor and session.supervisor.state == RunState.CHECKPOINTING:
                        session.supervisor.transition(RunState.PAUSED)
                except Exception:
                    pass
                session.reports["checkpoint"] = {"status": "blocked", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
            if session.events:
                session.events.append("checkpoint", session.reports["checkpoint"])
        _report = session.reports.get("checkpoint", {"status": "waiting for explicit click"})
        return mo.md(f"Checkpoint: **{_report['status']}** · {_report.get('error', _report.get('path', ''))}")
    _run()


@app.cell
def _(mo, session, upload_button):
    def _run():
        if session.claim_button("checkpoint_upload", upload_button.value):
            try:
                _checkpoint = session.reports.get("checkpoint", {})
                if _checkpoint.get("status") != "verified_local_only":
                    raise RuntimeError("create and locally verify a checkpoint first")
                import os
                from minecraft_agent.persistence import upload_huggingface, upload_s3
                _path = _checkpoint["path"]
                _hf_token = os.getenv("HF_TOKEN") or os.getenv("HUGGING_FACE_HUB_TOKEN", "")
                if os.getenv("MINECRAFT_ARTIFACT_REPO") and _hf_token:
                    _uri = upload_huggingface(_path, os.environ["MINECRAFT_ARTIFACT_REPO"], _hf_token)
                elif os.getenv("MINECRAFT_S3_BUCKET"):
                    _uri = upload_s3(_path, os.environ["MINECRAFT_S3_BUCKET"], os.getenv("MINECRAFT_S3_KEY_PREFIX", "minecraft-agent") + "/" + __import__("pathlib").Path(_path).name)
                elif os.getenv("MINECRAFT_ARTIFACT_REPO"):
                    raise RuntimeError("HF artifact repository is configured but no HF token is available")
                else:
                    session.reports["checkpoint_upload"] = {"status": "local_export_ready", "path": _path, "remote": "not configured"}
                    _uri = None
                if _uri:
                    session.reports["checkpoint_upload"] = {"status": "uploaded", "uri": _uri}
            except Exception as exc:
                session.reports["checkpoint_upload"] = {"status": "blocked", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
            if session.events:
                session.events.append("checkpoint_upload", session.reports["checkpoint_upload"])
        _report = session.reports.get("checkpoint_upload", {"status": "waiting for explicit click"})
        _download = None
        if _report.get("status") == "local_export_ready":
            from pathlib import Path
            _path = Path(_report["path"])
            _download = mo.download(data=lambda: _path.read_bytes(), filename=_path.name, mimetype="application/zip", label="Download local checkpoint")
        return mo.vstack([mo.md(f"Checkpoint export: **{_report['status']}** · {_report.get('uri', _report.get('error', _report.get('remote', '')))}"), _download] if _download is not None else [mo.md(f"Checkpoint export: **{_report['status']}** · {_report.get('error', _report.get('uri', ''))}")])
    _run()


@app.cell
def _(mo, runtime_root, session, restore_button):
    def _run():
        if session.claim_button("checkpoint_restore", restore_button.value):
            try:
                import os
                _upload = session.reports.get("checkpoint_upload", {})
                _uri = os.getenv("MINECRAFT_RESTORE_URI") or (_upload.get("uri") if _upload.get("status") == "uploaded" else None)
                for _key in ("bot_connect", "observation", "practice", "evidence_export", "restore_state_comparison", "resume"):
                    session.reports.pop(_key, None)
                import uuid
                from minecraft_agent.persistence import restore_remote, restore_snapshot, _sha256
                _restore_root = runtime_root() / "runs" / "restores" / ("restore-" + uuid.uuid4().hex[:8])
                if _uri:
                    _restored = restore_remote(_uri, _restore_root)
                    _manifest = _restored["manifest"]
                    _restored_path = _restored["restore_path"]
                    _archive_sha256 = _restored["archive_sha256"]
                else:
                    _checkpoint = session.reports.get("checkpoint", {})
                    if _checkpoint.get("status") != "verified_local_only":
                        raise RuntimeError("create a local checkpoint first")
                    _manifest = restore_snapshot(_checkpoint["path"], _restore_root)
                    _restored_path = str(_restore_root)
                    _archive_sha256 = _sha256(_checkpoint["path"])
                _expected_snapshot = session.reports.get("checkpoint", {}).get("snapshot_id")
                if _expected_snapshot and _manifest["snapshot_id"] != _expected_snapshot:
                    raise RuntimeError("restored snapshot ID differs from the checkpoint selected for this run")
                session.reports["checkpoint_restore"] = {"status": "verified_download_and_extracted", "snapshot_id": _manifest["snapshot_id"], "sha256": _archive_sha256, "manifest": _manifest, "uri": _uri or "local", "restore_path": _restored_path, "game_resume_requires_fresh_session": True}
            except Exception as exc:
                session.reports["checkpoint_restore"] = {"status": "blocked", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
            if session.events:
                session.events.append("checkpoint_restore_verify", session.reports["checkpoint_restore"])
        _report = session.reports.get("checkpoint_restore", {"status": "waiting for explicit click"})
        return mo.md(f"Remote restore verification: **{_report['status']}** · {_report.get('error', _report.get('snapshot_id', ''))}")
    _run()


@app.cell
def _(mo, resume_button, session):
    def _run():
        if session.claim_button("resume_restored_world", resume_button.value):
            try:
                _restore = session.reports.get("checkpoint_restore", {})
                if _restore.get("status") != "verified_download_and_extracted":
                    raise RuntimeError("download, hash-verify, and extract a checkpoint first")
                import json
                from pathlib import Path
                from minecraft_agent.events import EventLog
                from minecraft_agent.supervisor import RunState, Supervisor
                _restored = Path(_restore["restore_path"])
                _agent_dir = _restored / "agent"
                _world_dir = _restored / "world"
                _helper_dir = _restored / "helpers"
                _manifest = json.loads((_agent_dir / "manifest.json").read_text(encoding="utf-8"))
                _run_id = _manifest["run_id"]
                _world_id = _manifest["world_id"]
                if session.supervisor and session.supervisor.run_id == _run_id and Path(session.supervisor.run_dir).resolve() == _agent_dir.resolve() and session.supervisor.state != RunState.STOPPED:
                    if session.supervisor.status().get("processes", {}).get("minecraft-server", {}).get("running"):
                        raise RuntimeError("stop the current Minecraft server before resuming a restored snapshot")
                    _supervisor = session.supervisor
                else:
                    if session.supervisor and any(item.process.poll() is None for item in session.supervisor.processes.values()):
                        raise RuntimeError("stop the current run before restoring another world")
                    _supervisor = Supervisor(_agent_dir, _run_id)
                session.run_id = _run_id
                session.supervisor = _supervisor
                _checkpoint_elapsed = float(_restore["manifest"].get("metadata", {}).get("elapsed_seconds", 0.0))
                session.events = EventLog(_agent_dir / "events" / "events.jsonl", _run_id, _world_id, elapsed_offset=_checkpoint_elapsed)
                _expected_sequence = int(_restore["manifest"].get("metadata", {}).get("final_event_sequence", 0))
                if session.events.sequence != _expected_sequence:
                    raise RuntimeError(f"restored event sequence {session.events.sequence} differs from checkpoint marker {_expected_sequence}")
                session.events.append("session_start", {"restored_from_snapshot": _restore["snapshot_id"], "previous_sequence": session.events.sequence})
                _elapsed_before_resume = _checkpoint_elapsed
                _jar = next(_world_dir.glob("minecraft_server.*.jar"))
                _proc = session.supervisor.start("minecraft-server", ["java", "-Xms1G", "-Xmx2G", "-jar", str(_jar), "nogui"], cwd=_world_dir, env={"RUN_ID": _run_id}, ready_pattern=r"Done \(.*\)! For help", timeout=180)
                session.supervisor.transition(RunState.WORLD_READY)
                session.reports["active_server_dir"] = str(_world_dir)
                session.reports["active_helper_dir"] = str(_helper_dir)
                session.reports["server_start"] = {"status": "ready", "pid": _proc.process.pid, "endpoint": "127.0.0.1:25565", "restored_snapshot": _restore["snapshot_id"]}
                _archive_files = _restore["manifest"].get("files", [])
                _world_entries = [item for item in _archive_files if item["path"].startswith("world/")]
                _player_entries = [item for item in _world_entries if "/playerdata/" in item["path"]]
                _helper_entries = [item for item in _archive_files if item["path"].startswith("helpers/")]
                session.reports["resume"] = {"status": "server_restored_and_ready", "run_id": _run_id, "world_id": _world_id, "world_files_verified": len(_world_entries), "playerdata_files_verified": len(_player_entries), "helper_revisions_restored": {item["path"]: item["sha256"] for item in _helper_entries}, "event_sequence_continues_from": session.events.sequence, "player_state_comparison": "pending Mineflayer reconnect", "elapsed_seconds_before_resume": _elapsed_before_resume, "elapsed_seconds": session.events.elapsed_seconds}
            except Exception as exc:
                session.reports["resume"] = {"status": "blocked", "error": f"{type(exc).__name__}: {str(exc)[:400]}"}
            if session.events:
                session.events.append("resume_attempt", session.reports["resume"])
        _report = session.reports.get("resume", {"status": "waiting for explicit click"})
        return mo.md(f"World resume: **{_report['status']}** · {_report.get('error', _report.get('run_id', ''))}")
    _run()


@app.cell
def _(evidence_export_button, mo, runtime_root, session):
    def _run():
        if session.claim_button("practice_evidence_export", evidence_export_button.value):
            try:
                from pathlib import Path
                from minecraft_agent.persistence import create_snapshot
                import uuid
                _run_dir = session.supervisor.run_dir
                _helper_dir = Path(session.reports["active_helper_dir"])
                if session.events:
                    session.events.append("evidence_export_begin", {"practice_turn_id": session.reports["practice"]["turn_id"]})
                _destination = runtime_root() / "runs" / "exports" / (session.run_id + "-practice-" + uuid.uuid4().hex[:8] + ".zip")
                _snapshot = create_snapshot({"agent": _run_dir, "helpers": _helper_dir}, _destination, metadata={"evidence_type": "bounded_model_to_game_practice", "run_id": session.run_id, "practice": session.reports["practice"], "final_event_sequence": session.events.sequence if session.events else 0, "elapsed_seconds": session.events.elapsed_seconds if session.events else 0.0})
                session.reports["evidence_export"] = {"status": "verified", "snapshot_id": _snapshot["snapshot_id"], "path": str(_destination), "files": len(_snapshot["files"])}
            except Exception as exc:
                session.reports["evidence_export"] = {"status": "blocked", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
            if session.events:
                session.events.append("evidence_export", session.reports["evidence_export"])
        _report = session.reports.get("evidence_export", {"status": "waiting for explicit click"})
        _download = None
        if _report.get("status") == "verified":
            from pathlib import Path
            _path = Path(_report["path"])
            _download = mo.download(data=lambda: _path.read_bytes(), filename=_path.name, mimetype="application/zip", label="Download verified practice evidence")
        return mo.vstack([mo.md(f"Practice evidence export: **{_report['status']}** · {_report.get('error', _report.get('snapshot_id', ''))}"), _download] if _download is not None else [mo.md(f"Practice evidence export: **{_report['status']}** · {_report.get('error', '')}")])
    _run()


@app.cell
def _(mo, session):
    _status = session.supervisor.status() if session.supervisor else {"state": "no run"}
    mo.json(_status)
    return (_status,)


if __name__ == "__main__":
    app.run()
