from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from minecraft_agent.config import RunConfig
from minecraft_agent.events import EventLog
from minecraft_agent.models.interfaces import Candidate, Proposal, validate_probabilities, validate_proposal
from minecraft_agent.persistence import compare_player_state, create_snapshot, restore_snapshot, verify_snapshot
from minecraft_agent.protocol import ProtocolError, decode_line, encode_request
from minecraft_agent.session import NotebookSession
from minecraft_agent.supervisor import RunState, Supervisor


class SessionButtonTests(unittest.TestCase):
    def test_boolean_run_button_pulses_can_be_claimed_repeatedly(self):
        session = NotebookSession()
        with patch("minecraft_agent.session.time.monotonic", side_effect=[10.0, 10.5, 12.0]):
            self.assertTrue(session.claim_button("start", True))
            self.assertFalse(session.claim_button("start", True))
            self.assertFalse(session.claim_button("start", False))
            self.assertTrue(session.claim_button("start", True))


class ProtocolTests(unittest.TestCase):
    def test_request_round_trip_and_size_limit(self):
        raw = encode_request("run-1", "observe", {})
        self.assertEqual(decode_line(raw[:-1])["method"], "observe")
        with self.assertRaises(ProtocolError):
            decode_line(b"{" * 10, max_bytes=5)
        with self.assertRaises(ProtocolError):
            decode_line(b"{\"version\":2}")

    def test_bridge_discards_late_response_after_bounded_timeout(self):
        from minecraft_agent.bridge_client import BridgeClient
        script = "import json,sys,time; print(json.dumps({'event':'ready'}),flush=True);\nfor line in sys.stdin:\n r=json.loads(line); time.sleep(0.15) if r['method']=='slow' else None; print(json.dumps({'version':1,'request_id':r['request_id'],'result':{'method':r['method']}}),flush=True);\n if r['method']=='stop': break\n"
        with tempfile.TemporaryDirectory() as temp:
            client = BridgeClient([sys.executable, "-u", "-c", script], temp, {"PATH": os.getenv("PATH", ""), "HOME": temp}, timeout=1)
            try:
                with self.assertRaises(TimeoutError):
                    client.request("slow", {}, request_timeout=0.03)
                time.sleep(0.18)
                self.assertEqual(client.request("fast", {})["method"], "fast")
            finally:
                client.stop()


class ModelContractTests(unittest.TestCase):
    def test_probability_mapping_is_exact_and_normalized(self):
        validate_probabilities({"look": 0.7, "replan": 0.3}, ["look", "replan"])
        with self.assertRaises(ValueError):
            validate_probabilities({"look": 1.0}, ["look", "replan"])
        with self.assertRaises(ValueError):
            validate_probabilities({"look": float("nan")}, ["look"])

    def test_proposal_requires_reserved_replan_and_bounded_helper(self):
        valid = Proposal("safe", "ok", (Candidate("look", "Look", "primitive", {"type": "look"}), Candidate("replan", "Stop", "replan", {})), "{}")
        validate_proposal(valid)
        invalid = Proposal("safe", "ok", (Candidate("look", "Look", "primitive", {}),), "{}")
        with self.assertRaises(ValueError):
            validate_proposal(invalid)
        too_large = Proposal("safe", "ok", (Candidate("helper", "Run", "helper", {"source": "x" * 33000}), Candidate("replan", "Stop", "replan", {})), "{}")
        with self.assertRaises(ValueError):
            validate_proposal(too_large)

    def test_helper_source_allowlist_supports_nested_action_payloads(self):
        from minecraft_agent.models.local_nemotron import LocalNemotron

        dig = 'await api.act({"type":"dig","position":{"x":1,"y":64,"z":2}});'
        craft = 'await api.act({"type":"craft","item":"crafting_table","count":1});'
        objective = f"Choose one helper. <HELPER_SOURCE>{dig}</HELPER_SOURCE> <HELPER_SOURCE>{craft}</HELPER_SOURCE>"
        self.assertEqual(LocalNemotron.exact_helper_sources(objective), [dig, craft])

    def test_helper_prompt_is_compact_and_has_one_exact_candidate(self):
        from minecraft_agent.models.local_nemotron import LocalNemotron

        source = 'await api.act({"type":"move","duration_ms":500});'
        objective = f"Use the exact helper source. <HELPER_SOURCE>{source}</HELPER_SOURCE>"
        response_body = {
            "choices": [{"message": {"content": json.dumps({
                "objective": "Move toward the tree",
                "rationale": "The next bounded move advances the task.",
                "candidates": [{"candidate_id": "move", "description": "Move forward", "kind": "helper", "payload": {"source": source}}],
            })}}]
        }

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                return json.dumps(response_body).encode()

        def fake_urlopen(request, timeout):
            payload = json.loads(request.data)
            self.assertEqual(payload["max_tokens"], 256)
            schema = payload["response_format"]["json_schema"]["schema"]
            self.assertEqual(schema["properties"]["candidates"]["minItems"], 1)
            self.assertEqual(schema["properties"]["candidates"]["maxItems"], 1)
            prompt = json.loads(payload["messages"][0]["content"])
            self.assertEqual(prompt["observation"]["recent_events"], [])
            return Response()

        observation = {"recent_events": [{"message": "irrelevant history"}] * 12, "player": {"health": 20}}
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            proposal = LocalNemotron("http://127.0.0.1:8000/v1", "test-model").propose(observation, objective)
        self.assertEqual([candidate.kind for candidate in proposal.candidates], ["helper", "replan"])


class AgentStepTests(unittest.TestCase):
    def test_required_helper_runs_only_after_typed_selection(self):
        from minecraft_agent.agent import AgentStep

        class Bot:
            def __init__(self):
                self.executed = []

            def request(self, method, params):
                if method == "execute":
                    self.executed.append(params)
                    return {"status": "completed", "action_id": params["action_id"], "controls_released": True}
                return {"player": {"health": 20}}

        class Planner:
            def propose(self, observation, objective, memory):
                return Proposal(objective, "One safe look", (
                    Candidate("look_helper", "Invoke one harmless look through the capability", "helper", {"source": "await api.act({type: 'look', yaw: 0, pitch: 0.1});"}),
                    Candidate("replan", "Stop", "replan", {}),
                ), "{}")

        class Selector:
            def choose(self, state, objective, candidates):
                return {"look_helper": 1.0, "replan": 0.0}

        class HelperRunner:
            def __init__(self):
                self.calls = []

            def run(self, source, name):
                self.calls.append((source, name))
                return {"status": "completed", "helper": "agent_helper.js", "revision": 1, "actions": 1}

        with tempfile.TemporaryDirectory() as temp:
            events = EventLog(Path(temp) / "events.jsonl", "run-1", "world-1")
            bot = Bot()
            helper = HelperRunner()
            result = AgentStep(Planner(), Selector(), bot, events, helper).run("safe", required_candidate_kind="helper", allowed_action_types={"look"})
            self.assertEqual(result["selected"].kind, "helper")
            self.assertEqual(result["result"]["actions"], 1)
            self.assertEqual(result["result"]["observation"], {"player": {"health": 20}})
            self.assertEqual(len(helper.calls), 1)
            self.assertEqual(bot.executed, [])


class CapabilityServerTests(unittest.TestCase):
    def test_helper_actions_request_an_ack_without_a_second_observation(self):
        from minecraft_agent.helper import CapabilityServer

        class Bot:
            def __init__(self):
                self.calls = []

            def request(self, method, params):
                self.calls.append((method, params))
                return {"status": "completed", "controls_released": True}

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "capability.sock"
            bot = Bot()
            server = CapabilityServer(path, bot, allowed_action_types={"move"}, max_actions=1)
            server.start()
            try:
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.settimeout(1)
                client.connect(str(path))
                with client:
                    stream = client.makefile("rwb")
                    request = {"version": 1, "request_id": "1", "method": "execute", "params": {"action": {"type": "move", "keys": ["forward"], "duration_ms": 500}}}
                    stream.write(json.dumps(request).encode() + b"\n")
                    stream.flush()
                    response = json.loads(stream.readline())
                self.assertEqual(response["result"]["status"], "completed")
                self.assertEqual(bot.calls[0][0], "execute")
                self.assertIs(bot.calls[0][1]["include_observation"], False)
            finally:
                server.close()


class WorkerCommandTests(unittest.TestCase):
    def test_timeout_seconds_are_passed_to_node_as_milliseconds(self):
        from minecraft_agent.sandbox import worker_command

        with tempfile.TemporaryDirectory() as temp, patch("minecraft_agent.sandbox.shutil.which", return_value="/usr/bin/node"):
            root = Path(temp)
            helpers = root / "helpers"
            helpers.mkdir()
            wrapper = root / "worker.js"
            wrapper.write_text("", encoding="utf-8")
            socket_path = root / "runtime" / "capability.sock"
            socket_path.parent.mkdir()
            socket_path.touch()
            command = worker_command(wrapper, helpers, socket_path, helper_filename="task.js", timeout_seconds=12)
            self.assertEqual(command[2], "12000")
            with self.assertRaisesRegex(ValueError, "between 1 and 30"):
                worker_command(wrapper, helpers, socket_path, helper_filename="task.js", timeout_seconds=31)

    def test_required_helper_is_not_executed_if_selector_chooses_primitive(self):
        from minecraft_agent.agent import AgentStep

        class Bot:
            def __init__(self): self.executed = []
            def request(self, method, params):
                if method == "execute": self.executed.append(params)
                return {"player": {"health": 20}}

        class Planner:
            def propose(self, observation, objective, memory):
                return Proposal(objective, "Safe candidates", (
                    Candidate("look", "Look", "primitive", {"type": "look", "yaw": 0, "pitch": 0.1}),
                    Candidate("helper", "Helper", "helper", {"source": "await api.act({type: 'look', yaw: 0, pitch: 0.1});"}),
                    Candidate("replan", "Stop", "replan", {}),
                ), "{}")

        class Selector:
            def choose(self, state, objective, candidates):
                return {"look": 1.0, "helper": 0.0, "replan": 0.0}

        class HelperRunner:
            def run(self, source, name): raise AssertionError("unselected helper must not execute")

        with tempfile.TemporaryDirectory() as temp:
            bot = Bot()
            result = AgentStep(Planner(), Selector(), bot, EventLog(Path(temp) / "events.jsonl", "run-1", "world-1"), HelperRunner()).run("safe", required_candidate_kind="helper", allowed_action_types={"look"})
            self.assertEqual(result["result"]["status"], "not_executed")
            self.assertEqual(bot.executed, [])


class EventTests(unittest.TestCase):
    def test_events_are_sequenced_and_redacted(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            log = EventLog(path, "run-1", "world-1")
            first = log.append("probe", {
                "api_key": "secret", "access_token": "hidden",
                "input_tokens": 120, "output_tokens": 35,
                "nested": {"password": "hidden", "prompt_tokens": 100},
            })
            second = EventLog(path, "run-1", "world-1").append("next", {})
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([row["sequence"] for row in rows], [1, 2])
            self.assertEqual(first["payload"]["api_key"], "[REDACTED]")
            self.assertEqual(first["payload"]["access_token"], "[REDACTED]")
            self.assertEqual(first["payload"]["input_tokens"], 120)
            self.assertEqual(first["payload"]["output_tokens"], 35)
            self.assertEqual(rows[0]["payload"]["nested"]["password"], "[REDACTED]")
            self.assertEqual(rows[0]["payload"]["nested"]["prompt_tokens"], 100)
            self.assertEqual(second["sequence"], 2)

    def test_resume_rejects_event_gap_and_wrong_world(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            path.write_text(json.dumps({"sequence": 2, "run_id": "run-1", "world_id": "world-1", "elapsed_seconds": 1}) + "\n")
            with self.assertRaisesRegex(ValueError, "discontinuous"):
                EventLog(path, "run-1", "world-1")
            path.write_text(json.dumps({"sequence": 1, "run_id": "run-1", "world_id": "other", "elapsed_seconds": 1}) + "\n")
            with self.assertRaisesRegex(ValueError, "different run or world"):
                EventLog(path, "run-1", "world-1")


class SnapshotTests(unittest.TestCase):
    def test_checkpoint_player_state_comparison_is_exact_with_position_tolerance(self):
        saved = {"world": {"dimension": "overworld"}, "player": {"position": {"x": 1.0, "y": 64.0, "z": -2.0}, "health": 20, "food": 20}, "inventory": [{"name": "oak_log", "count": 3, "slot": 36}]}
        restored = {"world": {"dimension": "overworld"}, "player": {"position": {"x": 1.05, "y": 64.0, "z": -2.0}, "health": 20, "food": 20}, "inventory": [{"name": "oak_log", "count": 3, "slot": 36}]}
        self.assertEqual(compare_player_state(saved, restored)["status"], "passed")
        restored["inventory"][0]["count"] = 2
        self.assertEqual(compare_player_state(saved, restored)["status"], "mismatch")
        self.assertEqual(compare_player_state(None, restored)["status"], "blocked")

    def test_snapshot_verifies_and_restores(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent = root / "agent"
            world = root / "world"
            agent.mkdir(); world.mkdir()
            (agent / "events.jsonl").write_text("{}\n")
            (world / "level.dat").write_bytes(b"world-state")
            archive = root / "run.zip"
            created = create_snapshot({"agent": agent, "world": world}, archive, metadata={"run_id": "run-1", "api_token": "private"})
            checked = verify_snapshot(archive)
            self.assertEqual(created["snapshot_id"], checked["snapshot_id"])
            self.assertEqual(checked["metadata"]["api_token"], "[REDACTED]")
            restored = restore_snapshot(archive, root / "restored")
            self.assertEqual(restored["snapshot_id"], created["snapshot_id"])
            self.assertEqual((root / "restored/world/level.dat").read_bytes(), b"world-state")

    def test_tampered_payload_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "data"; source.mkdir()
            (source / "file.txt").write_text("original")
            archive = root / "run.zip"
            create_snapshot({"agent": source}, archive, metadata={})
            replacement = root / "tampered.zip"
            with zipfile.ZipFile(archive) as source_zip, zipfile.ZipFile(replacement, "w") as target_zip:
                for name in source_zip.namelist():
                    target_zip.writestr(name, b"changed" if name == "agent/file.txt" else source_zip.read(name))
            with self.assertRaises(ValueError):
                verify_snapshot(replacement)

    def test_remote_restore_requires_matching_published_marker(self):
        from minecraft_agent import persistence
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"; source.mkdir()
            (source / "player.json").write_text('{"health":20}')
            archive = root / "snapshot.zip"
            manifest = create_snapshot({"world": source}, archive, metadata={"final_event_sequence": 4})
            marker = {"schema_version": 1, "snapshot_id": manifest["snapshot_id"], "archive_sha256": persistence._sha256(archive), "archive_size_bytes": archive.stat().st_size}

            def copy_remote(uri, destination):
                destination = Path(destination)
                if uri.endswith(".manifest.json"):
                    destination.write_text(json.dumps(marker))
                else:
                    import shutil
                    shutil.copyfile(archive, destination)
                return destination

            with patch.object(persistence, "download_remote", side_effect=copy_remote):
                restored = persistence.restore_remote("s3://private/snapshot.zip", root / "restore")
            self.assertEqual(restored["manifest"]["snapshot_id"], manifest["snapshot_id"])
            self.assertEqual((Path(restored["restore_path"]) / "world/player.json").read_text(), '{"health":20}')

            marker["archive_sha256"] = "0" * 64
            with patch.object(persistence, "download_remote", side_effect=copy_remote):
                with self.assertRaisesRegex(ValueError, "published hash marker"):
                    persistence.restore_remote("s3://private/snapshot.zip", root / "bad-restore")


class SupervisorTests(unittest.TestCase):
    def test_readiness_idempotency_and_shutdown(self):
        with tempfile.TemporaryDirectory() as temp:
            supervisor = Supervisor(temp, "run-1")
            process = supervisor.start("worker", [sys.executable, "-u", "-c", "import time; print('READY', flush=True); time.sleep(20)"], cwd=temp, ready_pattern="READY", timeout=3)
            self.assertTrue(process.ready)
            calls = []
            first = supervisor.run_once("action-1", lambda: calls.append(1) or {"status": "done"})
            second = supervisor.run_once("action-1", lambda: calls.append(2) or {"status": "bad"})
            self.assertEqual(first, second)
            self.assertEqual(calls, [1])
            supervisor.stop_all()
            self.assertEqual(supervisor.state, RunState.STOPPED)


class PreflightGateTests(unittest.TestCase):
    def test_model_hydration_runs_without_gpu_disk_session_or_license_gate(self):
        from minecraft_agent import model_storage
        from types import SimpleNamespace
        revisions = model_storage.MODEL_REVISIONS
        fake_api = SimpleNamespace(model_info=lambda repo_id, revision, files_metadata: SimpleNamespace(sha=revision, siblings=[SimpleNamespace(size=3)], cardData=SimpleNamespace(license="other")))
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp) / "cache"
            snapshots = {}
            def download(repo_id, revision, cache_dir, token):
                root = Path(temp) / repo_id.replace("/", "--")
                root.mkdir(); (root / "weights.bin").write_bytes(b"weights")
                snapshots[revision] = root
                return str(root)
            with patch.dict(os.environ, {"NEMOTRON_LICENSE_ACCEPTED": "false", "MOLAB_SESSION_REMAINING_SECONDS": ""}), patch("huggingface_hub.HfApi", return_value=fake_api), patch("huggingface_hub.snapshot_download", side_effect=download), patch.object(model_storage.shutil, "which", return_value=None):
                result = model_storage.estimate_and_hydrate(cache)
            self.assertEqual(result["repositories"]["planner"]["revision"], revisions["planner"])
            self.assertEqual(result["repositories"]["selector"]["revision"], revisions["selector"])
            self.assertIsNone(result["gpu"])

    def test_model_hydration_can_download_only_nemotron(self):
        from minecraft_agent import model_storage
        from types import SimpleNamespace
        fake_api = SimpleNamespace(model_info=lambda repo_id, revision, files_metadata: SimpleNamespace(sha=revision, siblings=[SimpleNamespace(size=3)], cardData=SimpleNamespace(license="openmdw-1.1")))
        with tempfile.TemporaryDirectory() as temp:
            calls = []

            def download(repo_id, revision, cache_dir, token):
                calls.append(repo_id)
                root = Path(temp) / repo_id.replace("/", "--")
                root.mkdir(); (root / "weights.bin").write_bytes(b"weights")
                return str(root)

            with patch("huggingface_hub.HfApi", return_value=fake_api), patch("huggingface_hub.snapshot_download", side_effect=download), patch.object(model_storage.shutil, "which", return_value=None):
                report = model_storage.estimate_and_hydrate(Path(temp) / "cache", roles=("planner",))

        self.assertEqual(calls, [model_storage.MODEL_REPOSITORIES["planner"]])
        self.assertEqual(set(report["repositories"]), {"planner"})

    def test_server_preparation_records_operator_eula_override(self):
        from scripts.server import prepare_server
        with tempfile.TemporaryDirectory() as temp:
            prepare_server(temp)
            self.assertEqual((Path(temp) / "eula.txt").read_text(), "eula=true\n")
            self.assertIn("server-ip=127.0.0.1", (Path(temp) / "server.properties").read_text())

    def test_storage_probe_requires_an_existing_destination(self):
        from minecraft_agent.persistence import probe_configured_storage
        with patch.dict(os.environ, {"MINECRAFT_ARTIFACT_REPO": "", "MINECRAFT_S3_BUCKET": ""}):
            with self.assertRaisesRegex(RuntimeError, "existing private"):
                probe_configured_storage()

    def test_resource_summary_reports_peak_rss_and_gpu_memory(self):
        from minecraft_agent.preflight import summarize_resource_samples
        result = summarize_resource_samples([
            {"processes": {"10": {"rss_bytes": 100}, "11": {"rss_bytes": 200}}, "gpu": {"output": "512, 16384, 25"}},
            {"processes": {"10": {"rss_bytes": 150}, "11": {"rss_bytes": 180}}, "gpu": {"output": "768, 16384, 40"}},
        ])
        self.assertEqual(result["process_rss_peak_bytes"], {"10": 150, "11": 200})
        self.assertEqual(result["gpu_memory_used_peak_bytes"], 768 * 1024 * 1024)


class IdentityTests(unittest.TestCase):
    def test_identity_covers_uncommitted_source_and_lockfiles(self):
        from minecraft_agent.identity import source_identity
        root = Path(__file__).resolve().parents[1]
        identity = source_identity(root)
        self.assertEqual(len(identity["working_tree_sha256"]), 64)
        self.assertIn("uv.lock", identity["lockfile_sha256"])
        self.assertIn("bridge/package-lock.json", identity["lockfile_sha256"])


class ConfigTests(unittest.TestCase):
    def test_profile_and_resource_bounds(self):
        RunConfig().validate()
        with self.assertRaises(ValueError):
            RunConfig(profile="unknown").validate()
        with self.assertRaises(ValueError):
            RunConfig(max_observation_bytes=100, max_rpc_bytes=50).validate()


class ModelRuntimeTests(unittest.TestCase):
    def test_cuda_environment_prefers_project_toolchain_over_host_nvcc(self):
        from minecraft_agent import model_runtime

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            nvcc = project / ".venv/lib/python3.13/site-packages/nvidia/cu13/bin/nvcc"
            nvcc.parent.mkdir(parents=True)
            nvcc.write_text("", encoding="utf-8")
            cuda_root = nvcc.parent.parent
            (cuda_root / "lib").mkdir()
            (cuda_root / "lib/libcudart.so.13.0.96").write_text("", encoding="utf-8")
            home = project / "home"
            with patch.dict(os.environ, {"PATH": "/usr/bin", "LIBRARY_PATH": "", "LD_LIBRARY_PATH": ""}), \
                 patch.object(model_runtime.Path, "home", return_value=home):
                env = model_runtime._cuda_link_environment(project)

            self.assertEqual(env["CUDA_HOME"], str(cuda_root))
            self.assertTrue(env["PATH"].startswith(str(nvcc.parent)))
            self.assertIn(str(cuda_root / "lib"), env["LD_LIBRARY_PATH"])

    def test_nemotron_runs_alone_with_1m_context_and_long_context_cache_settings(self):
        from minecraft_agent import model_runtime

        class Supervisor:
            run_id = "run-1m"

            def __init__(self):
                self.started = []

            def start(self, *args, **kwargs):
                self.started.append((args, kwargs))
                return "started"

        supervisor = Supervisor()
        hydration = {"repositories": {"planner": {"snapshot_path": "/models/nemotron", "repo_id": "pinned-nemotron"}}}
        with patch.object(model_runtime, "_hydration", return_value=hydration), \
             patch.object(model_runtime, "repo_root", return_value=Path("/repo")), \
             patch.object(model_runtime.shutil, "which", return_value="/usr/bin/uv"), \
             patch.object(model_runtime, "_cuda_link_environment", return_value={}):
            self.assertEqual(model_runtime.start_nemotron(supervisor, "/run/dir"), "started")

        self.assertEqual(len(supervisor.started), 1)
        args, kwargs = supervisor.started[0]
        self.assertEqual(args[0], "nemotron")
        command = args[1]
        self.assertEqual(command[command.index("--max-model-len") + 1], "1048576")
        self.assertEqual(command[command.index("--max-num-seqs") + 1], "1")
        self.assertEqual(command[command.index("--max-num-batched-tokens") + 1], "8192")
        self.assertEqual(command[command.index("--gpu-memory-utilization") + 1], "0.82")
        self.assertEqual(command[command.index("--mamba-ssm-cache-dtype") + 1], "float16")
        self.assertEqual(command[command.index("--video-pruning-rate") + 1], "0.5")
        self.assertIn('{"video":{"fps":2,"num_frames":256}}', command)
        self.assertIn("--enable-prefix-caching", command)
        self.assertNotIn("clef", command)
        self.assertEqual(kwargs["timeout"], 1800)


if __name__ == "__main__":
    unittest.main()
