from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from textwrap import dedent
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from minecraft_agent.containers import configure_tailnet_firewall
from minecraft_agent.events import EventLog
from minecraft_agent.sandbox import SandboxProbe
from minecraft_agent.workspace import TOOL_NAMES, Workspace

class TailnetFirewallTests(unittest.TestCase):
    def test_firewall_is_idempotent_and_limited_to_three_service_ports(self):
        calls = []

        def fake_iptables(arguments):
            calls.append(arguments)
            return subprocess.CompletedProcess(arguments, 1 if arguments[0] == "-C" else 0, "", "")

        with patch("minecraft_agent.containers._run_host_iptables", side_effect=fake_iptables):
            result = configure_tailnet_firewall("100.83.98.53", "100.125.162.22")

        self.assertEqual(result["ports"], [2718, 3000, 25565])
        self.assertEqual(result["allowed_client"], "100.125.162.22")
        inserts = [call for call in calls if call[0] == "-I"]
        self.assertEqual([call[-1] for call in inserts], ["DROP", "ACCEPT"])
        self.assertEqual(inserts[0][2], "1")
        self.assertEqual(inserts[1][2], "1")
        self.assertTrue(all("tailscale0" in call for call in inserts))
        self.assertTrue(all("2718,3000,25565" in call for call in inserts))

    def test_firewall_rejects_non_tailnet_addresses_without_mutation(self):
        with patch("minecraft_agent.containers._run_host_iptables") as firewall:
            with self.assertRaises(ValueError):
                configure_tailnet_firewall("192.168.1.10", "100.125.162.22")
        firewall.assert_not_called()

class WorkspaceToolTests(unittest.TestCase):
    def test_shell_fails_closed_when_enforceable_sandbox_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temp, patch(
            "minecraft_agent.sandbox.probe", return_value=SandboxProbe(False, "no isolated backend")
        ):
            root = Path(temp) / "workspace"
            workspace = Workspace(root)
            workspace.initialize()
            result = workspace.execute("bash", {"command": "touch host-fallback-ran"})
            self.assertFalse(result["ok"])
            self.assertIn("sandbox", result["error"]["message"])
            self.assertFalse((root / "host-fallback-ran").exists())

    def test_shell_deadline_includes_children_that_keep_output_pipes_open(self):
        class LocalWorkspace(Workspace):
            def _sandbox_command(self, command, timeout_seconds):
                return ["bash", "-c", command], None

        with tempfile.TemporaryDirectory() as temp:
            with patch("minecraft_agent.sandbox.probe", return_value=SandboxProbe(True, "test")):
                workspace = LocalWorkspace(Path(temp) / "workspace")
            workspace.initialize()
            started = time.monotonic()
            result = workspace.execute("bash", {"command": "sleep 10 &", "timeout_seconds": 1})
            self.assertEqual(result["status"], "timeout")
            self.assertLess(time.monotonic() - started, 4)

    def test_shell_output_and_revisions_stay_bounded_under_memory_limit(self):
        script = dedent("""
            import hashlib, json, resource, sys, tempfile
            from pathlib import Path
            from minecraft_agent.events import EventLog
            from minecraft_agent.sandbox import SandboxProbe
            from minecraft_agent.workspace import Workspace
            from unittest.mock import patch

            limit = 96 * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

            class LocalWorkspace(Workspace):
                def _sandbox_command(self, command, timeout_seconds):
                    return ["bash", "-c", command], None

            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / "workspace"
                events = EventLog(Path(temp) / "events.jsonl", "run-1", "world-1")
                with patch("minecraft_agent.sandbox.probe", return_value=SandboxProbe(True, "test")):
                    workspace = LocalWorkspace(root, events=events)
                workspace.initialize()
                created = workspace.execute("bash", {"command": "dd if=/dev/zero of=large.js bs=1048576 count=80 status=none"})
                assert created["exit_code"] == 0, created
                output = workspace.execute("bash", {"command": "(yes x | head -c 67108864) & (yes y | head -c 67108864 >&2) & wait"})
                assert output["exit_code"] == 0, output
                assert output["stdout_truncated"] and output["stderr_truncated"], output
                assert len(output["stdout"]) <= 13000 and len(output["stderr"]) <= 13000, output
                revisions = [json.loads(line) for line in events.path.read_text().splitlines() if '"workspace_revision"' in line]
                changed = next(file for event in revisions for file in event["payload"]["files"] if file["path"] == "large.js")
                with (root / "large.js").open("rb") as handle:
                    assert changed["sha256"] == hashlib.file_digest(handle, "sha256").hexdigest(), changed
                assert changed["diff"] == "", changed
        """)
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_seven_tools_docs_revisions_and_compact_file_workflow(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            repo = Path(__file__).resolve().parents[1]
            events = EventLog(Path(temp) / "events.jsonl", "run-1", "world-1")
            workspace = Workspace(root, events=events)
            workspace.initialize(
                mineflayer_docs=repo / "bridge/node_modules/mineflayer/docs",
                pathfinder_docs=repo / "bridge/node_modules/mineflayer-pathfinder/readme.md",
            )
            self.assertEqual(TOOL_NAMES, ("read", "write", "edit", "bash", "grep", "find", "ls"))
            self.assertTrue((root / "scripts").is_dir())
            self.assertTrue((root / "skills").is_dir())
            self.assertTrue((root / "docs/mineflayer/api.md").is_file())
            self.assertTrue((root / "docs/pathfinder/readme.md").is_file())
            quick_reference = (root / "docs/mineflayer/agent-quick-reference.md").read_text()
            self.assertIn("Vec3` positions", quick_reference)
            self.assertIn("GoalNear", quick_reference)
            self.assertIn("bot.recipesFor", quick_reference)
            self.assertIn("mineflayer@4.39.0", (root / "docs/README.md").read_text())
            self.assertEqual(workspace.execute("ls", {"path": "scripts"})["empty"], True)

            write = workspace.execute("write", {"path": "scripts/task.js", "content": "// needle\nreturn 1;\n"})
            self.assertTrue(write["ok"])
            self.assertFalse(workspace.execute("read", {"path": "scripts/task.js"})["truncated"])
            self.assertEqual(workspace.execute("grep", {"pattern": "needle", "path": "scripts"})["matches"][0]["line"], 1)
            self.assertIn("scripts/task.js", [item["path"] for item in workspace.execute("find", {"path": "scripts", "pattern": "**/*.js"})["matches"]])
            edit = workspace.execute("edit", {"path": "scripts/task.js", "old": "return 1", "new": "return 2"})
            self.assertEqual(edit["replacements"], 1)
            shell = workspace.execute("bash", {"command": "printf shell-ok"})
            self.assertEqual(shell["stdout"], "shell-ok")
            self.assertFalse(workspace.execute("read", {"path": "../outside"})["ok"])
            self.assertFalse(workspace.execute("read", {"path": "missing.js"})["ok"])
            rows = [json.loads(line) for line in events.path.read_text().splitlines()]
            self.assertTrue(any(row["type"] == "workspace_revision" for row in rows))

    def test_only_passing_skills_are_available_in_later_workspaces(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            library = root / "library"
            author = Workspace(root / "author", shared_skills=library)
            author.initialize()
            command = {"command": "mc-skill publish skills/gather"}
            for path, content in (("SKILL.md", "Gather logs."), ("test.sh", "exit 1")):
                self.assertTrue(author.execute("write", {"path": f"skills/gather/{path}", "content": content})["ok"])
            self.assertEqual(author.execute("bash", command)["status"], "test_failed")
            author.execute("write", {"path": "skills/gather/test.sh", "content": "exit 0"})
            published = author.execute("bash", command)
            self.assertEqual(published["status"], "published", published)
            self.assertEqual(published["version"], 1)

            later = Workspace(root / "later", shared_skills=library)
            later.initialize()
            files = later.execute("find", {"path": "skills/library", "pattern": "**/SKILL.md"})
            self.assertEqual(len(files["matches"]), 1)
            copied = later.execute("read", {"path": files["matches"][0]["path"]})
            self.assertEqual(copied["content"], "Gather logs.")

    def test_mc_run_sends_workspace_program_and_timeout_over_private_socket(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            socket_path = Path(temp) / "run.sock"
            workspace = Workspace(root, bridge_socket=socket_path)
            workspace.initialize()
            workspace.execute("write", {"path": "scripts/step.js", "content": "console.log('one'); await bot.waitForTicks(2); return 7;"})
            seen = {}
            ready = threading.Event()

            def serve_once():
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                    server.bind(str(socket_path))
                    server.listen(1)
                    ready.set()
                    connection, _ = server.accept()
                    with connection:
                        request = json.loads(connection.makefile("rb").readline())
                        seen.update(request)
                        connection.sendall(json.dumps({"status": "completed", "output": [], "result": 7}).encode())

            thread = threading.Thread(target=serve_once)
            thread.start()
            self.assertTrue(ready.wait(1))
            result = workspace.execute("bash", {"command": "mc-run scripts/step.js --timeout 17"})
            thread.join(timeout=2)
            self.assertEqual(result["exit_code"], 0)
            response = json.loads(result["stdout"])
            self.assertEqual(response["result"], 7)
            self.assertEqual(seen["timeout_seconds"], 17)
            self.assertIn("waitForTicks", seen["source"])

    def test_bash_timeout_terminates_the_process_group(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(Path(temp) / "workspace")
            workspace.initialize()
            result = workspace.execute("bash", {"command": "sleep 10", "timeout_seconds": 1})
            self.assertEqual(result["status"], "timeout")
            self.assertNotEqual(result["exit_code"], 0)
