from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from minecraft_agent.config import RunConfig
from minecraft_agent.containers import configure_tailnet_firewall
from minecraft_agent.daemon import _new_bridge_socket_path
from minecraft_agent.events import EventLog
from minecraft_agent.model_runtime import LOCAL_PROVIDER_ID, MAX_NUM_SEQS, NEMOTRON_MODEL, NEMOTRON_REVISION, _cuda_link_environment, inference_config, probe_nemotron
from minecraft_agent.molab_runtime import MolabRuntime
from minecraft_agent.rollouts import build_rollout_export, state_delta
from minecraft_agent.session import NotebookSession
from minecraft_agent.supervisor import RunState, Supervisor
from minecraft_agent.workspace import TOOL_NAMES, Workspace


class SessionButtonTests(unittest.TestCase):
    def test_boolean_run_button_pulses_can_be_claimed_repeatedly(self):
        session = NotebookSession()
        with patch("minecraft_agent.session.time.monotonic", side_effect=[10.0, 10.5, 12.0]):
            self.assertTrue(session.claim_button("start", True))
            self.assertFalse(session.claim_button("start", True))
            self.assertFalse(session.claim_button("start", False))
            self.assertTrue(session.claim_button("start", True))


class EventTests(unittest.TestCase):
    def test_events_are_sequenced_and_secrets_are_redacted(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            log = EventLog(path, "run-1", "world-1")
            first = log.append("probe", {"api_key": "secret", "input_tokens": 120, "nested": {"password": "hidden"}})
            second = EventLog(path, "run-1", "world-1").append("next", {})
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([row["sequence"] for row in rows], [1, 2])
            self.assertEqual(first["payload"]["api_key"], "[REDACTED]")
            self.assertEqual(first["payload"]["input_tokens"], 120)
            self.assertEqual(rows[0]["payload"]["nested"]["password"], "[REDACTED]")
            self.assertEqual(second["sequence"], 2)

    def test_resume_rejects_a_discontinuous_event_log(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            path.write_text(json.dumps({"sequence": 2, "run_id": "run-1", "world_id": "world-1", "elapsed_seconds": 1}) + "\n")
            with self.assertRaisesRegex(ValueError, "discontinuous"):
                EventLog(path, "run-1", "world-1")


class SupervisorTests(unittest.TestCase):
    def test_waits_for_readiness_and_stops_the_process_group(self):
        with tempfile.TemporaryDirectory() as temp:
            supervisor = Supervisor(temp, "run-1")
            process = supervisor.start(
                "worker", [sys.executable, "-u", "-c", "import time; print('READY', flush=True); time.sleep(20)"],
                cwd=temp, ready_pattern="READY", timeout=3,
            )
            self.assertTrue(process.ready)
            supervisor.stop_all()
            self.assertEqual(supervisor.state, RunState.STOPPED)


class ConfigTests(unittest.TestCase):
    def test_minecraft_version_must_be_valid(self):
        RunConfig().validate()
        with self.assertRaises(ValueError):
            RunConfig(minecraft_version="").validate()

    def test_vllm_configuration_is_local_single_sequence_and_version_pinned(self):
        config = inference_config()
        self.assertEqual(config["provider"], LOCAL_PROVIDER_ID)
        self.assertEqual(config["model"], NEMOTRON_MODEL)
        self.assertEqual(config["revision"], NEMOTRON_REVISION)
        self.assertEqual(config["max_num_seqs"], MAX_NUM_SEQS)
        self.assertEqual(MAX_NUM_SEQS, 1)
        with self.assertRaisesRegex(ValueError, "loopback"):
            probe_nemotron("https://example.com/v1")

    def test_host_bridge_socket_uses_private_path_under_linux_limit(self):
        directory, socket_path = _new_bridge_socket_path()
        try:
            self.assertLess(len(os.fsencode(socket_path)), 108)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        finally:
            directory.rmdir()

    def test_flashinfer_nvcc_and_headers_use_the_same_pinned_cuda_root(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            cuda = project / ".venv/lib/python3.13/site-packages/nvidia/cu13"
            nvcc = cuda / "bin/nvcc"
            nvcc.parent.mkdir(parents=True)
            nvcc.touch()
            (cuda / "lib").mkdir()
            (cuda / "lib/libcudart.so.13").touch()
            with patch("minecraft_agent.model_runtime.shutil.which", return_value=None):
                environment = _cuda_link_environment(project)
        self.assertEqual(environment["CUDA_HOME"], str(cuda))
        self.assertEqual(environment["CUDA_PATH"], str(cuda))
        self.assertEqual(environment["FLASHINFER_NVCC"], str(nvcc))


class RolloutTests(unittest.TestCase):
    @staticmethod
    def observation(x, health, inventory):
        return {
            "player": {"position": {"x": x, "y": 64, "z": 4}, "health": health, "food": 20},
            "inventory_summary": inventory,
            "recent_events": [],
        }

    def test_state_delta_keeps_raw_movement_health_and_inventory_changes(self):
        before = self.observation(1, 20, {"oak_log": 1})
        after = self.observation(3, 17, {"oak_log": 2, "stick": 4})
        delta = state_delta(before, after)
        self.assertEqual(delta["position"], {"x": 2.0, "y": 0.0, "z": 0.0})
        self.assertEqual(delta["player_numeric"]["health"], -3.0)
        self.assertEqual(delta["inventory_counts"], {"oak_log": 1.0, "stick": 4.0})

    def test_rollout_export_uses_sparse_verified_reward_and_terminal_flags(self):
        before = self.observation(1, 20, {})
        middle = self.observation(2, 19, {"oak_log": 1})
        after = self.observation(3, 18, {"diamond": 1})
        manifest = {
            "run_id": "run-example", "world_id": "world-example", "objective": "diamond",
            "goal": "diamond_pilot", "created_at": "2026-01-01T00:00:00Z",
            "model": {"id": NEMOTRON_MODEL}, "model_revision": NEMOTRON_REVISION,
            "inference_config": inference_config(), "game": {"world_seed": 42},
        }
        events = [
            {"type": "agent_transition", "wall_time": "t1", "elapsed_seconds": 1, "payload": {
                "step_id": 1, "observation": before, "next_observation": middle,
                "actions": [{"tool": "bash"}], "verified_goal_after": {"status": "ongoing"},
            }},
            {"type": "agent_transition", "wall_time": "t2", "elapsed_seconds": 2, "payload": {
                "step_id": 2, "observation": middle, "next_observation": after,
                "actions": [{"tool": "minecraft_verify_goal"}], "verified_goal_after": {"status": "passed"},
                "terminated": True, "reward": 1.0,
            }},
            {"type": "agent_run_finished", "payload": {"status": "passed"}},
        ]
        episode, transitions = build_rollout_export(manifest, events)
        self.assertEqual(episode["seed"], 42)
        self.assertEqual([item["reward"] for item in transitions], [0.0, 1.0])
        self.assertEqual([item["terminated"] for item in transitions], [False, True])
        self.assertEqual([item["truncated"] for item in transitions], [False, False])
        self.assertEqual(transitions[-1]["raw_state_delta"]["inventory_counts"], {"diamond": 1.0, "oak_log": -1.0})

    def test_stopped_rollout_marks_only_the_final_step_truncated(self):
        observation = self.observation(1, 20, {})
        final_observation = self.observation(5, 15, {"oak_log": 2})
        manifest = {"run_id": "run-stop", "game": {"world_seed": 7}, "model": {"id": NEMOTRON_MODEL}}
        events = [
            {"type": "agent_transition", "payload": {"step_id": 1, "observation": observation, "next_observation": None}},
            {"type": "agent_run_finished", "payload": {"status": "stopped", "final_observation": final_observation}},
        ]
        episode, transitions = build_rollout_export(manifest, events)
        self.assertTrue(episode["truncated"])
        self.assertFalse(transitions[0]["terminated"])
        self.assertTrue(transitions[0]["truncated"])
        self.assertEqual(transitions[0]["next_observation"], final_observation)
        self.assertEqual(transitions[0]["raw_state_delta"]["position"]["x"], 4.0)

    def test_interrupted_turns_export_tool_results_and_keep_duplicate_step_ids_unique(self):
        before = self.observation(1, 20, {})
        after_first = self.observation(3, 18, {"oak_log": 1})
        after_second = self.observation(5, 16, {"oak_log": 2})
        manifest = {
            "run_id": "run-interrupted", "game": {"world_seed": 9},
            "model": {"id": NEMOTRON_MODEL, "revision": NEMOTRON_REVISION},
            "inference_config": inference_config(),
        }
        events = [
            {"type": "agent_turn_started", "wall_time": "t1", "payload": {
                "step_id": 1, "observation": before, "verified_goal": {"status": "ongoing"},
                "screenshot": {"path": "frame-1.png"},
            }},
            {"type": "tool_call_started", "payload": {
                "step_id": 1, "tool_call_id": "call-1", "tool": "write", "arguments": {"path": "scripts/gather.js", "content_bytes": 32},
            }},
            {"type": "rl_tool_action", "payload": {
                "step_id": 1, "tool_call_id": "call-1", "tool": "write",
                "arguments": {"path": "scripts/gather.js", "content": "await bot.dig(target);"},
            }},
            {"type": "tool_call_finished", "payload": {
                "step_id": 1, "tool_call_id": "call-1", "tool": "write", "is_error": False,
                "duration_ms": 1200, "result": "gathered one log",
            }},
            {"type": "agent_turn_started", "wall_time": "t2", "payload": {
                "step_id": 1, "observation": after_first, "verified_goal": {"status": "ongoing"},
            }},
            {"type": "tool_call_started", "payload": {
                "step_id": 1, "tool_call_id": "call-2", "tool": "minecraft_observe", "arguments": {},
            }},
            {"type": "tool_call_finished", "payload": {
                "step_id": 1, "tool_call_id": "call-2", "tool": "minecraft_observe", "is_error": False,
                "duration_ms": 20, "result": "observed",
            }},
            {"type": "agent_run_finished", "payload": {"status": "stopped", "final_observation": after_second}},
        ]
        episode, transitions = build_rollout_export(manifest, events)
        self.assertEqual([row["step_id"] for row in transitions], [1, 2])
        self.assertEqual([row["next_observation"] for row in transitions], [after_first, after_second])
        self.assertEqual(transitions[0]["actions"][0]["tool"], "write")
        self.assertEqual(transitions[0]["actions"][0]["arguments"]["content"], "await bot.dig(target);")
        self.assertEqual(transitions[0]["tool_results"][0]["result"], "gathered one log")
        self.assertEqual(transitions[0]["tool_results"][0]["duration_ms"], 1200)
        self.assertEqual(transitions[-1]["raw_state_delta"]["inventory_counts"], {"oak_log": 1.0})
        self.assertEqual([row["reward"] for row in transitions], [0.0, 0.0])
        self.assertEqual([row["terminated"] for row in transitions], [False, False])
        self.assertEqual([row["truncated"] for row in transitions], [False, True])
        self.assertEqual(transitions[-1]["token_metrics_status"], "unavailable_through_pi_openai_compatible_provider")
        self.assertTrue(episode["truncated"])


class MolabRuntimeTests(unittest.TestCase):
    def test_run_start_fails_closed_when_loopback_vllm_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = MolabRuntime(Path(__file__).resolve().parents[1], Path(temp) / "runtime")
            daemon = Mock()
            runtime.daemon = daemon
            with patch("minecraft_agent.molab_runtime.probe_nemotron", side_effect=RuntimeError("local vLLM is unavailable")):
                with self.assertRaisesRegex(RuntimeError, "local vLLM is unavailable"):
                    runtime.start_run("diamond_pilot")
            daemon.start_run.assert_not_called()
            runtime.close()

    def test_vllm_start_returns_to_notebook_while_server_initializes(self):
        class FakeSupervisor:
            def __init__(self):
                self.stopped = False

            def status(self):
                return {"processes": {"nemotron": {"running": not self.stopped, "ready": False}}}

            def stop_all(self):
                self.stopped = True

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = MolabRuntime(Path(__file__).resolve().parents[1], root / "runtime")
            snapshot = runtime.model_cache / f"models--{NEMOTRON_MODEL.replace('/', '--')}" / "snapshots" / NEMOTRON_REVISION
            snapshot.mkdir(parents=True)
            (snapshot / "config.json").write_text("{}", encoding="utf-8")
            fake_supervisor = FakeSupervisor()
            start_entered = __import__("threading").Event()
            release_start = __import__("threading").Event()

            def wait_during_start(*_args):
                start_entered.set()
                release_start.wait(2)

            with patch("minecraft_agent.molab_runtime.Supervisor", return_value=fake_supervisor), \
                 patch("minecraft_agent.molab_runtime.start_nemotron", side_effect=wait_during_start), \
                 patch("minecraft_agent.molab_runtime.probe_nemotron", return_value={"status": "ready"}):
                result = runtime.start_model()
                self.assertEqual(result["status"], "starting")
                self.assertTrue(start_entered.wait(1))
                release_start.set()
                runtime._model_start_thread.join(timeout=2)

            self.assertEqual(runtime.reports["vllm"]["status"], "ready")
            runtime.close()


class TailnetFirewallTests(unittest.TestCase):
    def test_firewall_is_limited_to_the_three_app_ports(self):
        calls = []

        def fake_iptables(arguments):
            calls.append(arguments)
            return subprocess.CompletedProcess(arguments, 1 if arguments[0] == "-C" else 0, "", "")

        with patch("minecraft_agent.containers._run_host_iptables", side_effect=fake_iptables):
            result = configure_tailnet_firewall("100.83.98.53", "100.125.162.22")
        self.assertEqual(result["ports"], [2718, 3000, 25565])
        inserts = [call for call in calls if call[0] == "-I"]
        self.assertEqual([call[-1] for call in inserts], ["DROP", "ACCEPT"])


class WorkspaceTests(unittest.TestCase):
    def test_workspace_tools_keep_files_inside_the_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            workspace = Workspace(root)
            workspace.initialize()
            self.assertEqual(TOOL_NAMES, ("read", "write", "edit", "bash", "grep", "find", "ls"))
            write = workspace.execute("write", {"path": "scripts/task.js", "content": "// needle\nreturn 1;\n"})
            self.assertTrue(write["ok"])
            self.assertEqual(workspace.execute("grep", {"pattern": "needle", "path": "scripts"})["matches"][0]["line"], 1)
            edit = workspace.execute("edit", {"path": "scripts/task.js", "old": "return 1", "new": "return 2"})
            self.assertEqual(edit["replacements"], 1)
            self.assertFalse(workspace.execute("read", {"path": "../outside"})["ok"])
