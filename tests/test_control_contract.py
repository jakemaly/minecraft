"""Regression tests through the same Unix RPC used by the notebook and Pi."""

import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from textwrap import dedent
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from minecraft_agent.daemon import ActiveRun, RunDaemon
from minecraft_agent.daemon_client import request
from minecraft_agent.events import EventLog
from minecraft_agent.sandbox import SandboxProbe
from minecraft_agent.supervisor import RunState, Supervisor
from minecraft_agent.workspace import Workspace
from scripts.server import download_server


ROOT = Path(__file__).resolve().parents[1]
CALLBACK = "http://127.0.0.1:1455/auth/callback?code=test"
LIVE = os.getenv("MINECRAFT_LIVE_SMOKE") == "1"


def local_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return str(listener.getsockname()[1])


class ControlContractTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="mc-")))
        node = shutil.which("node")
        self.assertIsNotNone(node, "Install Node and run npm ci --prefix bridge")
        preload = ROOT / "tests/fixtures/pi-model.mjs"
        wrapper = self.root / "node"
        wrapper.write_text(
            f'#!/bin/sh\ncase "$1" in */pi_agent.mjs) '
            f'exec {shlex.quote(node)} --import {shlex.quote(str(preload))} "$@";; esac\n'
            f'exec {shlex.quote(node)} "$@"\n'
        )
        wrapper.chmod(0o700)
        self.enterContext(patch.dict(os.environ, {
            "PATH": f"{self.root}{os.pathsep}{os.environ['PATH']}",
            "MINECRAFT_AGENT_HOME": str(self.root),
            "MINECRAFT_VERSION": "1.21.4",
            "MINECRAFT_SERVER_PORT": local_port(),
            "MINECRAFT_VIEWER_PORT": local_port(),
            "MINECRAFT_SERVER_HEAP_MB": "1024",
        }))
        self.enterContext(patch("minecraft_agent.daemon.tailscale_address", return_value="127.0.0.1"))
        self.enterContext(patch("minecraft_agent.daemon.client_tailscale_address", return_value=None))

    def start_service(self):
        daemon = RunDaemon(ROOT)
        thread = threading.Thread(target=daemon.serve, daemon=True)
        thread.start()
        self.addCleanup(self.close_service, daemon, thread)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                self.assertEqual(request("status", timeout=0.2)["service"], "ready")
                return daemon, thread
            except (ConnectionRefusedError, FileNotFoundError, TimeoutError):
                time.sleep(0.02)
        self.fail("run service did not accept RPC requests")

    @staticmethod
    def close_service(daemon, thread):
        if thread.is_alive():
            daemon.close()
            thread.join(timeout=5)

    def wait_status(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while True:
            status = request("status")
            if predicate(status):
                return status
            run = status["active_run"]
            if (run and run["state"] == "failed") or time.monotonic() >= deadline:
                self.fail(f"Expected service state did not arrive: {run or status['chatgpt_auth']}")
            time.sleep(0.1)

    def connect(self):
        self.assertEqual(request("auth_start")["status"], "started")
        auth = self.wait_status(lambda s: s["chatgpt_auth"].get("callback_placeholder"))["chatgpt_auth"]
        self.assertEqual(auth["authorization_url"], "https://example.test/login")
        self.assertEqual(request("auth_callback", {"url": CALLBACK})["status"], "submitted")
        return self.wait_status(lambda s: s["selected_model"])

    def test_signed_out_controls_and_invalid_requests_fail_without_creating_a_run(self):
        self.start_service()
        for method, params, error in (
            ("start_run", {"kind": "unknown"}, "kind must be"),
            ("start_run", {"kind": "dragon_proof"}, "Connect with ChatGPT"),
            ("auth_callback", {"url": CALLBACK}, "callback is waiting"),
            ("pause_run", {}, "no running world"),
            ("resume_run", {}, "no paused world"),
            ("export_run", {}, "no run to export"),
            ("unknown", {}, "unsupported control method"),
        ):
            with self.subTest(method=method), self.assertRaisesRegex(RuntimeError, error):
                request(method, params)
        self.assertEqual(request("stop_run")["status"], "idle")
        self.assertIsNone(request("status")["active_run"])

    def test_status_rpc_exposes_run_plan_and_latest_saved_checkpoint(self):
        daemon, _ = self.start_service()
        run_directory = self.root / "runs" / "run-plan"
        workspace_root = run_directory / "agent-workspace"
        workspace_root.mkdir(parents=True)
        plan = "# Beat the Ender Dragon\n\n- [x] Secure spawn\n- [ ] Prepare for the Nether\n"
        (workspace_root / "plan.md").write_text(plan, encoding="utf-8")
        events = EventLog(run_directory / "events" / "events.jsonl", "run-plan", "world-plan")
        checkpoint = events.append("run_paused", {"saved": True, "frozen": True})
        supervisor = Mock()
        supervisor.status.return_value = {"state": "paused", "processes": {}}
        daemon.active = ActiveRun(
            run_id="run-plan", world_id="world-plan", kind="dragon_proof",
            goal="dragon_kill", objective="Beat the Ender Dragon", duration_hours=72,
            run_directory=run_directory, server_directory=run_directory / "server", seed=1,
            bind_address="127.0.0.1", server_port=25565, viewer_port=3000,
            viewer_url=None, events=events, supervisor=supervisor, model={},
            workspace=SimpleNamespace(root=workspace_root), state="paused",
        )

        active = request("status")["active_run"]

        self.assertEqual(active["plan"], plan)
        self.assertEqual(active["last_saved_checkpoint"]["sequence"], checkpoint["sequence"])
        (workspace_root / "plan.md").write_text("x" * 9_000, encoding="utf-8")
        active = request("status")["active_run"]
        self.assertIn("plan truncated", active["plan"])
        self.assertLessEqual(len(active["plan"]), 8_080)

    def test_pause_waits_for_an_inflight_bridge_request_before_saving(self):
        daemon, _ = self.start_service()
        run_directory = self.root / "runs" / "run-pause"
        run_directory.mkdir(parents=True)
        bridge = Mock()
        bridge.request.side_effect = [
            TimeoutError("Mineflayer bridge is busy; bounded request was deferred"),
            {"status": "idle", "controls_released": True},
        ]
        supervisor = Mock()
        supervisor.state = RunState.RUNNING
        events = EventLog(run_directory / "events" / "events.jsonl", "run-pause", "world-pause")
        daemon.active = ActiveRun(
            run_id="run-pause", world_id="world-pause", kind="dragon_proof",
            goal="dragon_kill", objective="Beat the Ender Dragon", duration_hours=72,
            run_directory=run_directory, server_directory=run_directory / "server", seed=1,
            bind_address="127.0.0.1", server_port=25565, viewer_port=3000,
            viewer_url=None, events=events, supervisor=supervisor, model={},
            workspace=Mock(), bridge=bridge, state="running",
        )

        with patch.object(daemon.pi, "request", return_value={"status": "pause_requested"}):
            result = request("pause_run")

        self.assertEqual(result, {"status": "paused", "world_frozen": True, "saved": True})
        self.assertEqual(bridge.request.call_count, 2)
        self.assertEqual([call.args[1] for call in supervisor.send_command.call_args_list], ["tick freeze", "save-all flush"])
        self.assertEqual(daemon.active.last_saved_checkpoint["type"], "run_paused")

    def test_agent_bridge_rpc_retries_only_when_another_request_holds_the_bridge(self):
        daemon, _ = self.start_service()
        events = EventLog(self.root / "runs" / "run-busy" / "events.jsonl", "run-busy", "world-busy")
        bridge = Mock()
        bridge.request.side_effect = [
            TimeoutError("Mineflayer bridge is busy; bounded request was deferred"),
            {"player": {"health": 20}},
        ]
        daemon.active = SimpleNamespace(run_id="run-busy", bridge=bridge, events=events)

        result = request("agent_bridge_request", {"run_id": "run-busy", "method": "observe"})

        self.assertEqual(result["player"]["health"], 20)
        self.assertEqual(bridge.request.call_count, 2)
        self.assertFalse(events.path.exists())

    def test_daemon_blocks_repeating_a_gameplay_script_without_state_progress(self):
        daemon, _ = self.start_service()
        run_directory = self.root / "runs" / "run-repeat"
        workspace_root = run_directory / "agent-workspace"
        (workspace_root / "scripts").mkdir(parents=True)
        script = workspace_root / "scripts" / "recover.js"
        script.write_text("return 'try once';", encoding="utf-8")
        workspace = Workspace(workspace_root)
        workspace.execute = Mock(return_value={
            "ok": True, "exit_code": 0,
            "stdout": json.dumps({"status": "completed", "script": "scripts/recover.js"}),
        })
        observation = {
            "world": {"dimension": "overworld"},
            "player": {
                "position": {"x": 1, "y": 64, "z": 1}, "health": 20, "food": 20,
                "oxygen": 300, "equipment": {},
            },
            "inventory_summary": {"wooden_sword": 1}, "nearby_entities": [], "nearby_blocks": [],
        }
        hurt = json.loads(json.dumps(observation))
        hurt["player"]["position"]["x"] = 4
        hurt["player"]["health"] = 8
        bridge = Mock()
        bridge.request.side_effect = [observation, hurt, hurt]
        events = EventLog(run_directory / "events" / "events.jsonl", "run-repeat", "world-repeat")
        supervisor = Mock()
        supervisor.status.return_value = {"state": "running", "processes": {}}
        daemon.active = ActiveRun(
            run_id="run-repeat", world_id="world-repeat", kind="dragon_proof",
            goal="dragon_kill", objective="Beat the Ender Dragon", duration_hours=72,
            run_directory=run_directory, server_directory=run_directory / "server", seed=1,
            bind_address="127.0.0.1", server_port=25565, viewer_port=3000,
            viewer_url=None, events=events, supervisor=supervisor, model={},
            workspace=workspace, bridge=bridge, state="running",
        )
        arguments = {"command": "mc-run scripts/recover.js --timeout 60"}

        first = request("workspace_tool", {"run_id": "run-repeat", "tool": "bash", "arguments": arguments})
        self.assertTrue(first["ok"])
        with self.assertRaisesRegex(RuntimeError, "repeated gameplay action blocked"):
            request("workspace_tool", {"run_id": "run-repeat", "tool": "bash", "arguments": arguments})

        workspace.execute.assert_called_once()
        self.assertIn("gameplay_action_blocked", [json.loads(line)["type"] for line in events.path.read_text().splitlines()])

        script.write_text("return 'changed plan';", encoding="utf-8")
        bridge.request.side_effect = [hurt, hurt]
        self.assertTrue(request("workspace_tool", {"run_id": "run-repeat", "tool": "bash", "arguments": arguments})["ok"])
        changed_world = json.loads(json.dumps(hurt))
        changed_world["player"]["position"]["x"] = 2
        changed_world["player"]["equipment"]["hand"] = {"name": "iron_sword"}
        progressed_world = json.loads(json.dumps(changed_world))
        progressed_world["inventory_summary"]["iron_ingot"] = 1
        bridge.request.side_effect = [changed_world, progressed_world]
        self.assertTrue(request("workspace_tool", {"run_id": "run-repeat", "tool": "bash", "arguments": arguments})["ok"])
        bridge.request.side_effect = [progressed_world, progressed_world]
        self.assertTrue(request("workspace_tool", {"run_id": "run-repeat", "tool": "bash", "arguments": arguments})["ok"])
        self.assertEqual(workspace.execute.call_count, 4)

    def test_oauth_catalog_filters_models_and_pin_survives_service_restart(self):
        daemon, thread = self.start_service()
        status = self.connect()
        catalog = status["chatgpt_auth"]["account_model_catalog"]
        self.assertEqual([model["id"] for model in catalog["candidates"]], ["gpt-6-astra", "gpt-6-sol"])
        self.assertEqual(status["selected_model"]["id"], "gpt-6-astra")
        with self.assertRaisesRegex(RuntimeError, "not an account-accessible image model"):
            request("pin_default_model", {"model_id": "text-only"})
        self.assertEqual(request("pin_default_model", {"model_id": "gpt-6-sol"})["status"], "pinned")
        self.close_service(daemon, thread)
        self.start_service()
        self.assertEqual(request("status")["selected_model"]["id"], "gpt-6-sol")
        self.assertEqual(request("disconnect_chatgpt")["status"], "disconnected")
        self.assertFalse(request("status")["chatgpt_auth"]["connected"])

    def test_failed_start_is_reported_without_leaving_an_active_world(self):
        self.start_service()
        self.connect()
        with patch.dict(os.environ, {"MINECRAFT_VERSION": ""}):
            self.assertEqual(request("start_run", {"kind": "dragon_proof"})["status"], "launching")
            status = self.wait_status(lambda s: (s["previous_run"] or {}).get("state") == "failed")
        self.assertIn("minecraft_version must not be empty", status["previous_run"]["error"])
        self.assertEqual(status["active_run"]["supervisor"]["processes"], {})

    def test_concurrent_service_state_writes_use_distinct_atomic_temps(self):
        with tempfile.TemporaryDirectory() as temp:
            daemon = RunDaemon.__new__(RunDaemon)
            daemon.runtime = Path(temp)
            daemon.previous_run = None
            barrier = threading.Barrier(2)
            replace = os.replace
            errors = []

            def simultaneous_replace(source, target):
                barrier.wait(timeout=5)
                return replace(source, target)

            def write():
                try:
                    daemon._write_service_state()
                except Exception as exc:
                    errors.append(exc)

            with patch("minecraft_agent.daemon.os.replace", side_effect=simultaneous_replace):
                writers = [threading.Thread(target=write) for _ in range(2)]
                for writer in writers:
                    writer.start()
                for writer in writers:
                    writer.join(timeout=5)
            self.assertFalse(any(writer.is_alive() for writer in writers))
            self.assertEqual(errors, [])
            self.assertEqual(json.loads((Path(temp) / "service-state.json").read_text())["previous_run"], None)

    def test_start_fails_closed_when_pinned_docker_runtime_is_unavailable(self):
        self.start_service()
        self.connect()
        with (
            patch("minecraft_agent.daemon.ensure_runtime_images", return_value={"available": False, "reason": "Docker daemon is unavailable"}),
            patch("minecraft_agent.daemon.Supervisor.start") as start_process,
            patch("scripts.server.download_server", side_effect=AssertionError("server download started")) as download_server,
        ):
            self.assertEqual(request("start_run", {"kind": "dragon_proof"})["status"], "launching")
            status = self.wait_status(lambda s: (s["previous_run"] or {}).get("state") == "failed")
        self.assertIn("Docker daemon is unavailable", status["previous_run"]["error"])
        self.assertEqual(status["active_run"]["supervisor"]["processes"], {})
        start_process.assert_not_called()
        download_server.assert_not_called()

    def test_start_fails_closed_when_workspace_sandbox_is_unavailable(self):
        self.start_service()
        self.connect()
        with (
            patch("minecraft_agent.daemon.ensure_runtime_images", return_value={"available": True}),
            patch("minecraft_agent.sandbox.probe", return_value=SandboxProbe(False, "namespace unavailable")),
            patch("minecraft_agent.daemon.create_private_network") as create_network,
            patch("minecraft_agent.daemon.Supervisor.start") as start_process,
        ):
            self.assertEqual(request("start_run", {"kind": "dragon_proof"})["status"], "launching")
            status = self.wait_status(lambda s: (s["previous_run"] or {}).get("state") == "failed")
        self.assertIn("workspace sandbox unavailable: namespace unavailable", status["previous_run"]["error"])
        create_network.assert_not_called()
        start_process.assert_not_called()

    def test_mc_run_socket_deadline_fails_run_and_enters_normal_cleanup(self):
        daemon, _ = self.start_service()
        run_id = "run-timeout-test"
        run_dir = self.root / "run-timeout"
        server_dir = self.root / "world" / "server"
        run_dir.mkdir()
        server_dir.mkdir(parents=True)
        events = EventLog(run_dir / "events.jsonl", run_id, "world-timeout")
        supervisor = Supervisor(run_dir, run_id)
        socket_path = run_dir / ".runtime" / "program.sock"
        socket_path.parent.mkdir()
        class LocalWorkspace(Workspace):
            def _sandbox_command(self, command, timeout_seconds):
                return ["bash", "-c", command], None

        with patch("minecraft_agent.sandbox.probe", return_value=SandboxProbe(True, "test")):
            workspace = LocalWorkspace(run_dir / "workspace", events=events, bridge_socket=socket_path)
        workspace.initialize()
        workspace.execute("write", {"path": "scripts/block.js", "content": "while (true) {}"})
        run = ActiveRun(
            run_id=run_id, world_id="world-timeout", kind="dragon_proof", goal="dragon_kill",
            objective="smoke test", duration_hours=1, run_directory=run_dir,
            server_directory=server_dir, seed=1, bind_address="127.0.0.1", server_port=25565,
            viewer_port=3000, viewer_url=None, events=events, supervisor=supervisor, model={},
            workspace=workspace, state="running", phase="agent_running",
        )
        run.bridge = Mock()
        run.bridge.request.return_value = {
            "world": {"dimension": "overworld"},
            "player": {"position": {"x": 0, "y": 64, "z": 0}, "health": 20, "food": 20, "oxygen": 300, "equipment": {}},
            "inventory_summary": {}, "nearby_entities": [], "nearby_blocks": [],
        }
        daemon.active = run
        blocked_socket = subprocess.Popen(
            ["node", "-e", "require('node:net').createServer(c=>c.on('data',()=>{while(true){}})).listen(process.argv[1])", str(socket_path)],
            cwd=self.root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        cleanup_called = threading.Event()
        cleanup = []

        def record_cleanup(candidate, *, final_state):
            cleanup.append((candidate, final_state))
            cleanup_called.set()

        self.enterContext(patch.object(daemon, "_stop_run", side_effect=record_cleanup))
        try:
            deadline = time.monotonic() + 5
            while not socket_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            result = request("workspace_tool", {
                "run_id": run_id, "tool": "bash",
                "arguments": {"command": "mc-run scripts/block.js --timeout 1"},
            })
            self.assertEqual(result["exit_code"], 124, result)
            self.assertEqual(json.loads(result["stdout"])["status"], "timeout")
            self.assertEqual(run.state, "failed")
            self.assertIn("timed out", run.error)
            self.assertTrue(cleanup_called.wait(5), "timeout did not enter the normal run cleanup path")
            self.assertEqual(cleanup, [(run, "failed")])
        finally:
            blocked_socket.kill()
            blocked_socket.wait(timeout=5)

    def test_rejected_pi_resume_keeps_run_paused_and_does_not_unfreeze_world(self):
        daemon, _ = self.start_service()
        supervisor = Mock()
        supervisor.state = RunState.PAUSED
        run = SimpleNamespace(
            run_id="run-paused", state="paused", supervisor=supervisor, duration_hours=1,
            active_seconds_at_pause=0, kind="dragon_proof", goal="dragon_kill", objective="test",
            run_directory=self.root, viewer_url=None, model={},
        )
        daemon.active = run
        with patch.object(daemon.pi, "request", side_effect=RuntimeError("pi_request_failed: resume denied")) as pi_request:
            with self.assertRaisesRegex(RuntimeError, "resume denied"):
                request("resume_run")
        self.assertEqual(run.state, "paused")
        supervisor.send_command.assert_not_called()
        supervisor.transition.assert_not_called()
        self.assertEqual([call.args[0] for call in pi_request.call_args_list], ["resume_run"])
        daemon.active = None

    def test_stop_run_closes_bridge_client_process(self):
        daemon, _ = self.start_service()
        bridge = Mock()
        run = SimpleNamespace(
            run_id="run-stop", state="running", finalizing=False, error=None,
            bridge=bridge, workspace=None, supervisor=Mock(), containerized=False,
            docker_network=None, events=Mock(),
        )
        daemon.active = run
        self.enterContext(patch.object(daemon, "_build_video", return_value={}))
        self.enterContext(patch.object(daemon, "_run_summary", return_value={"run_id": run.run_id}))
        self.assertEqual(request("stop_run")["status"], "stopped")
        bridge.stop.assert_called_once()
        daemon.active = None

    def test_missing_pi_run_is_restarted_before_world_unfreezes(self):
        daemon, _ = self.start_service()
        calls = []
        supervisor = Mock()
        supervisor.state = RunState.PAUSED
        supervisor.send_command.side_effect = lambda _, command: calls.append(("world", command))
        supervisor.transition.side_effect = lambda state: calls.append(("supervisor", state))
        run = SimpleNamespace(
            run_id="run-paused", state="paused", supervisor=supervisor, duration_hours=1,
            active_seconds_at_pause=5, kind="dragon_proof", goal="dragon_kill", objective="test",
            run_directory=self.root, viewer_url=None, model={}, running_since=None,
            events=Mock(),
        )
        daemon.active = run

        def pi_request(method, *_args, **_kwargs):
            calls.append(("pi", method))
            if method == "resume_run":
                raise RuntimeError("pi_request_failed: active Pi run not found")
            return {"status": "started"}

        with patch.object(daemon.pi, "request", side_effect=pi_request):
            self.assertEqual(request("resume_run")["status"], "resumed")
        self.assertEqual(run.state, "running")
        self.assertLess(calls.index(("pi", "start_run")), calls.index(("world", "tick unfreeze")))
        daemon.active = None

    def test_large_export_hash_and_zip_integrity_fit_memory_limit(self):
        script = dedent("""
            import hashlib, resource, subprocess, tempfile, zipfile
            from pathlib import Path
            from types import SimpleNamespace
            from minecraft_agent.daemon import RunDaemon
            from minecraft_agent.events import EventLog

            limit = 96 * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
            with tempfile.TemporaryDirectory() as temp:
                runtime = Path(temp) / "runtime"
                run_dir = runtime / "runs/run-large"
                world_dir = runtime / "worlds/run-large"
                run_dir.mkdir(parents=True)
                world_dir.mkdir(parents=True)
                source = world_dir / "large.bin"
                subprocess.run(["dd", "if=/dev/urandom", f"of={source}", "bs=1048576", "count=80", "status=none"], check=True)
                daemon = RunDaemon.__new__(RunDaemon)
                daemon.runtime = runtime
                daemon.active = SimpleNamespace(
                    run_id="run-large", state="failed", run_directory=run_dir,
                    server_directory=world_dir / "server", events=EventLog(run_dir / "events.jsonl", "run-large", "world-large"),
                )
                artifact = daemon.export_run()
                assert artifact["bytes"] == Path(artifact["path"]).stat().st_size
                with Path(artifact["path"]).open("rb") as handle:
                    assert artifact["sha256"] == hashlib.file_digest(handle, "sha256").hexdigest()
                with zipfile.ZipFile(artifact["path"]) as archive:
                    assert archive.testzip() is None
                    with archive.open("world/large.bin") as handle, source.open("rb") as original:
                        assert hashlib.file_digest(handle, "sha256").hexdigest() == hashlib.file_digest(original, "sha256").hexdigest()
        """)
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            capture_output=True, text=True, timeout=90,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(LIVE, "set MINECRAFT_LIVE_SMOKE=1 to run vanilla Java, Chrome, and Docker")
    def test_live_run_preserves_world_on_resume_and_exports_verified_completion(self):
        # A temporary loopback world keeps the smoke test independent of an operator's world.
        self.assertIsNotNone(shutil.which("docker"), "Install Docker for enforced isolation")
        self.assertIsNotNone(shutil.which("java"), "Install Java for the live smoke test")
        chrome = shutil.which("google-chrome")
        self.assertIsNotNone(chrome, "Install Google Chrome for the live smoke test")
        browser = self.root / "google-chrome"
        profile = shlex.quote(str(self.root / "chrome-profile"))
        browser.write_text(f'#!/bin/sh\nexec {shlex.quote(chrome)} --user-data-dir={profile} "$@"\n')
        browser.chmod(0o700)
        jar = download_server("1.21.4", self.root / "server-cache")
        self.enterContext(patch("scripts.server.download_server", return_value=jar))
        daemon, _ = self.start_service()
        self.connect()
        self.assertEqual(request("start_run", {"kind": "diamond_pilot"})["status"], "launching")
        run = self.wait_status(
            lambda s: any(event["type"] == "tool_call" for event in (s["active_run"] or {}).get("recent_events", [])),
            timeout=120,
        )["active_run"]
        run_id = run["run_id"]
        self.assertEqual(run["state"], "running")

        def tool(name, **arguments):
            return request("workspace_tool", {"run_id": run_id, "tool": name, "arguments": arguments})

        def verify(goal):
            return request("agent_bridge_request", {"run_id": run_id, "method": "verify_goal", "arguments": {"goal": goal}})

        self.assertEqual(verify("diamond_pilot")["status"], "incomplete")
        self.assertEqual(verify("dragon_kill")["status"], "incomplete")
        with self.assertRaisesRegex(RuntimeError, "already running"):
            request("start_run", {"kind": "dragon_proof"})
        with self.assertRaisesRegex(RuntimeError, "stale"):
            request("workspace_tool", {"run_id": "old-run", "tool": "ls"})
        with self.assertRaisesRegex(RuntimeError, "pause or stop"):
            request("export_run")

        daemon.active.supervisor.send_command("minecraft-server", "kill Agent")
        death_deadline = time.monotonic() + 15
        death_observation = None
        while time.monotonic() < death_deadline:
            try:
                death_observation = request("agent_bridge_request", {"run_id": run_id, "method": "observe", "arguments": {}})
            except RuntimeError as exc:
                if not any(reason in str(exc) for reason in (
                    "bridge is busy; bounded request was deferred",
                    "Mineflayer bridge error: bot is not ready",
                )):
                    raise
                time.sleep(0.1)
                continue
            if any(event["type"] == "death" for event in death_observation["recent_events"]) and death_observation["player"]["health"] > 0:
                break
            time.sleep(0.1)
        self.assertIsNotNone(death_observation)
        self.assertTrue(any(event["type"] == "death" for event in death_observation["recent_events"]))
        death_event = next(event for event in death_observation["recent_events"] if event["type"] == "death")
        self.assertEqual(death_event["cause"], "unknown")
        self.assertIn("last_observed", death_event)
        self.assertGreater(death_observation["player"]["health"], 0, "the bot should respawn after death")
        after_death = request("status")["active_run"]
        self.assertEqual(
            (after_death["run_id"], after_death["world_id"], after_death["state"]),
            (run_id, run["world_id"], "running"), json.dumps(after_death, default=str),
        )

        reflection_deadline = time.monotonic() + 15
        while time.monotonic() < reflection_deadline:
            current = request("status")["active_run"]
            if current["state"] != "running":
                self.fail(f"run ended before death reflection: {current.get('error')}; events={current.get('recent_events', [])[-5:]}")
            try:
                memory = request("workspace_tool", {"run_id": run_id, "tool": "read", "arguments": {"path": "memory.md"}})
                plan = request("workspace_tool", {"run_id": run_id, "tool": "read", "arguments": {"path": "plan.md"}})
            except RuntimeError as exc:
                current = request("status")["active_run"]
                self.fail(f"reflection workspace became unavailable: {exc}; run={current}")
            if (
                "Death reflection: preserve this world and continue the objective" in memory.get("content", "")
                and "Recovery after death" in plan.get("content", "")
            ):
                break
            time.sleep(0.1)
        self.assertIn("Death reflection: preserve this world and continue the objective", memory.get("content", ""))
        self.assertIn("Recovery after death", plan.get("content", ""))

        self.assertEqual(request("pause_run"), {"status": "paused", "world_frozen": True, "saved": True})
        with self.assertRaisesRegex(RuntimeError, "username"):
            request("set_observer", {"username": "Agent"})
        self.assertEqual(request("set_observer", {"username": "Observer_1"})["status"], "spectator_command_sent")
        paused_seconds = request("status")["active_run"]["active_elapsed_seconds"]
        shell = tool("bash", command="mc-run scripts/live.js --timeout 5")
        self.assertEqual(shell["exit_code"], 0, shell)
        self.assertEqual(json.loads(shell["stdout"])["result"], "Agent")
        self.assertEqual(request("status")["active_run"]["active_elapsed_seconds"], paused_seconds)

        self.assertTrue(tool("write", path="memory.md", content="simulate-subscription-limit")["ok"])
        self.assertEqual(request("resume_run")["status"], "resumed")
        limited = self.wait_status(
            lambda s: s["active_run"]["phase"] == "subscription_limit"
            and any(event["type"] == "agent_run_finished" for event in s["active_run"]["recent_events"])
        )["active_run"]
        self.assertEqual((limited["run_id"], limited["world_id"], limited["state"]), (run_id, run["world_id"], "paused"))
        limit_event = next(event for event in limited["recent_events"] if event["type"] == "subscription_usage_limit")
        self.assertFalse(limit_event["payload"]["automatic_api_fallback"])

        self.assertTrue(tool("write", path="memory.md", content="Preserve this run memory.")["ok"])
        # Arrange the end-game state through the vanilla console, then verify it through the bridge.
        console = daemon.active.supervisor
        console.send_command("minecraft-server", "advancement grant Agent only minecraft:end/kill_dragon")
        console.send_command("minecraft-server", "give Agent minecraft:diamond 1")
        deadline = time.monotonic() + 10
        while verify("diamond_pilot")["status"] != "passed" and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(verify("diamond_pilot")["inventory_diamonds"], 1)
        deadline = time.monotonic() + 10
        dragon = verify("dragon_kill")
        while dragon["status"] != "passed" and time.monotonic() < deadline:
            time.sleep(0.1)
            dragon = verify("dragon_kill")
        self.assertEqual(dragon["status"], "passed")
        self.assertEqual(request("resume_run")["status"], "resumed")
        self.wait_status(lambda s: (s["previous_run"] or {}).get("state") == "passed", timeout=60)

        artifact = request("export_run")
        path = Path(artifact["path"])
        self.assertEqual(artifact["bytes"], path.stat().st_size)
        with path.open("rb") as stream:
            self.assertEqual(artifact["sha256"], hashlib.file_digest(stream, "sha256").hexdigest())
        with zipfile.ZipFile(path) as archive:
            memory = archive.read("run/agent-workspace/memory.md")
            self.assertIn(b"Preserve this run memory.", memory)
            self.assertIn(b"Death reflection: preserve this world and continue the objective", memory)
            self.assertIn("world/server/world/level.dat", archive.namelist())
            self.assertTrue(any(name.startswith("run/media/frames/frame-") for name in archive.namelist()))
            if shutil.which("ffmpeg"):
                with archive.open("run/media/practice.mp4") as video:
                    self.assertEqual(video.read(8)[4:], b"ftyp")
            self.assertFalse(any(name.endswith(".sock") or name.startswith("run/.runtime/chrome/") for name in archive.namelist()))
            events = [json.loads(line) for line in archive.read("run/events/events.jsonl").splitlines()]
        audits = [event["payload"]["image_policy"] for event in events if event["type"] == "model_context"]
        self.assertGreaterEqual(len(audits), 2)
        self.assertIn(0, [audit["image_count"] for audit in audits])
        self.assertIn(1, [audit["image_count"] for audit in audits])
        for audit in audits:
            self.assertLessEqual(audit["image_count"], 1)
            self.assertEqual(audit["current_image_is_last_user_content"], audit["image_count"] == 1)
            self.assertTrue(audit["older_images_absent"])

        self.assertEqual(request("start_run", {"kind": "diamond_pilot"})["status"], "launching")
        second = self.wait_status(
            lambda s: s["active_run"] and s["active_run"]["state"] == "running", timeout=120,
        )["active_run"]
        self.assertTrue(request("workspace_tool", {
            "run_id": second["run_id"], "tool": "write",
            "arguments": {"path": "scripts/block.js", "content": "while (true) {}"},
        })["ok"])
        timed_out = request("workspace_tool", {
            "run_id": second["run_id"], "tool": "bash",
            "arguments": {"command": "mc-run scripts/block.js --timeout 1"},
        })
        self.assertEqual(timed_out["exit_code"], 124, timed_out)
        self.assertEqual(json.loads(timed_out["stdout"])["status"], "timeout")
        self.assertEqual(request("status")["active_run"]["state"], "failed")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            status = request("status")
            previous = status["previous_run"] or {}
            if previous.get("run_id") == second["run_id"] and previous.get("state") == "failed":
                break
            time.sleep(0.1)
        else:
            self.fail("timed-out live run did not finish cleanup")
        for name in ("mc-bot", "mc-server", "mc-gateway"):
            removed = subprocess.run(
                ["docker", "container", "inspect", f"{name}-{second['run_id'][-16:]}"],
                capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(removed.returncode, 0, removed.stderr)
