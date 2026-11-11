from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from minecraft_agent.config import RunConfig
from minecraft_agent.events import EventLog
from minecraft_agent.pi_client import PiClient
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


class BridgeClientTests(unittest.TestCase):
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


class PiClientTests(unittest.TestCase):
    def test_stop_closes_worker_pipes_after_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            client = PiClient(temp, temp, Path(temp) / "control.sock", lambda _event: None)
            client.process = subprocess.Popen(
                [sys.executable, "-c", "import sys; sys.stdin.read()"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            with patch.object(client, "request", return_value={"status": "stopped"}):
                client.stop()
            self.assertTrue(client.process.stdin.closed)
            self.assertTrue(client.process.stdout.closed)
            self.assertTrue(client.process.stderr.closed)


class SupervisorTests(unittest.TestCase):
    def test_readiness_and_shutdown(self):
        with tempfile.TemporaryDirectory() as temp:
            supervisor = Supervisor(temp, "run-1")
            process = supervisor.start("worker", [sys.executable, "-u", "-c", "import time; print('READY', flush=True); time.sleep(20)"], cwd=temp, ready_pattern="READY", timeout=3)
            self.assertTrue(process.ready)
            supervisor.stop_all()
            self.assertEqual(supervisor.state, RunState.STOPPED)
            self.assertTrue(process.process.stdin.closed)


class MinecraftServerSetupTests(unittest.TestCase):
    def test_server_preparation_records_operator_eula_override(self):
        from scripts.server import prepare_server

        with tempfile.TemporaryDirectory() as temp:
            prepare_server(temp)
            self.assertEqual((Path(temp) / "eula.txt").read_text(), "eula=true\n")
            properties = (Path(temp) / "server.properties").read_text()
            self.assertIn("server-ip=127.0.0.1", properties)
            self.assertIn("hardcore=false", properties)


class IdentityTests(unittest.TestCase):
    def test_identity_covers_uncommitted_source_and_lockfiles(self):
        from minecraft_agent.identity import source_identity

        root = Path(__file__).resolve().parents[1]
        identity = source_identity(root)
        self.assertEqual(len(identity["working_tree_sha256"]), 64)
        self.assertIn("uv.lock", identity["lockfile_sha256"])
        self.assertIn("bridge/package-lock.json", identity["lockfile_sha256"])


class ConfigTests(unittest.TestCase):
    def test_only_the_minecraft_version_is_configured(self):
        with patch.dict(os.environ, {"MINECRAFT_VERSION": "1.20.4"}):
            config = RunConfig.from_environment()
            self.assertEqual(config.minecraft_version, "1.20.4")
            config.validate()
        with patch.dict(os.environ, {"MINECRAFT_VERSION": ""}):
            with self.assertRaisesRegex(ValueError, "must not be empty"):
                RunConfig.from_environment().validate()


if __name__ == "__main__":
    unittest.main()
