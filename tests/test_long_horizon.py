from __future__ import annotations

import base64
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from minecraft_agent.agent_loop import AgentLoop, response_schema, system_prompt
from minecraft_agent.containers import configure_tailnet_firewall
from minecraft_agent.events import EventLog
from minecraft_agent.models.local_nemotron import LocalNemotron
from minecraft_agent.workspace import TOOL_NAMES, Workspace
from viewer.raster import render_first_person


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


class ViewerRasterTests(unittest.TestCase):
    def test_current_snapshot_renders_a_low_resolution_jpeg(self):
        blocks = [
            {"name": "stone", "x": x, "y": y, "z": 2}
            for x in range(-2, 3) for y in range(0, 4)
        ]
        jpeg, metadata = render_first_person({
            "sequence": 8,
            "center": {"x": 0, "y": 0, "z": 0},
            "player": {"position": {"x": 0, "y": 0, "z": 0}, "yaw": 0, "pitch": 0},
            "blocks": blocks,
            "source": "mineflayer_loaded_region",
            "physics_tick_p95_ms": 50,
        })
        self.assertTrue(jpeg.startswith(b"\xff\xd8") and jpeg.endswith(b"\xff\xd9"))
        self.assertEqual((metadata["width"], metadata["height"]), (384, 216))
        self.assertEqual(metadata["sequence"], 8)
        self.assertEqual(metadata["renderer"], "python_voxel_raycast")
        self.assertGreater(metadata["render_ms"], 0)
        self.assertLess(len(jpeg), 50_000)


class WorkspaceToolTests(unittest.TestCase):
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


class NemotronTurnTests(unittest.TestCase):
    class Stream:
        def __init__(self, lines):
            self.lines = iter(lines)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def readline(self):
            try:
                line = next(self.lines)
            except StopIteration:
                return b""
            if line == b"SLOW":
                time.sleep(0.015)
                return self.readline()
            return line

    def test_streams_exposed_reasoning_usage_and_one_final_current_image(self):
        jpeg = b"\xff\xd8current-frame\xff\xd9"
        answer = {"kind": "act", "rationale": "inspect", "candidates": [], "answer": ""}
        lines = [
            b'data: {"id":"response-1","choices":[{"delta":{"reasoning":"I should inspect the current terrain."}}]}\n',
            b"SLOW",
            ("data: " + json.dumps({"choices": [{"delta": {"content": json.dumps(answer)}}]})).encode() + b"\n",
            b'data: {"choices":[{"finish_reason":"stop","delta":{}}],"usage":{"prompt_tokens":220,"completion_tokens":40,"prompt_tokens_details":{"cached_tokens":110}}}\n',
            b"data: [DONE]\n",
        ]

        def fake_urlopen(request, timeout):
            payload = json.loads(request.data)
            self.assertEqual(payload["messages"][0]["role"], "system")
            self.assertEqual(payload["max_tokens"], 8192)
            self.assertEqual(payload["repetition_detection"], {"min_pattern_size": 1, "max_pattern_size": 4, "min_count": 32})
            content = payload["messages"][1]["content"]
            self.assertEqual([part["type"] for part in content], ["text", "image_url"])
            self.assertTrue(content[-1]["image_url"]["url"].endswith(base64.b64encode(jpeg).decode()))
            self.assertEqual(content[-1]["image_url"]["detail"], "low")
            self.assertEqual(payload["response_format"]["json_schema"]["schema"], response_schema("nemotron_clef"))
            return self.Stream(lines)

        client = LocalNemotron("http://127.0.0.1:8000/v1", "frozen-nemotron")
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            turn = client.generate_turn(system_prompt=system_prompt(mode="nemotron_clef"), context={"turn": 1}, image_jpeg=jpeg, response_schema=response_schema("nemotron_clef"))
        self.assertEqual(turn["reasoning"], "I should inspect the current terrain.")
        self.assertEqual(turn["parsed"]["kind"], "act")
        self.assertEqual(turn["metrics"]["input_tokens"], 220)
        self.assertEqual(turn["metrics"]["cached_input_tokens"], 110)
        self.assertEqual(turn["metrics"]["finish_reason"], "stop")
        self.assertGreater(turn["metrics"]["ttft_seconds"], 0)
        self.assertEqual(turn["metrics"]["image_sha256"], __import__("hashlib").sha256(jpeg).hexdigest())

    def test_turn_rejects_missing_or_invalid_image_before_network_request(self):
        client = LocalNemotron("http://127.0.0.1:8000/v1", "frozen-nemotron")
        with patch("urllib.request.urlopen") as request:
            with self.assertRaisesRegex(ValueError, "exactly one current JPEG"):
                client.generate_turn(system_prompt=system_prompt(), context={}, image_jpeg=None, response_schema=response_schema())
            request.assert_not_called()

    def test_malformed_response_keeps_exposed_reasoning_and_usage(self):
        lines = [
            b'data: {"choices":[{"delta":{"reasoning":"I need to inspect the terrain."}}]}\n',
            b'data: {"choices":[{"delta":{"content":"{\\\"kind\\\":"}}]}\n',
            b'data: {"choices":[{"finish_reason":"length","delta":{}}],"usage":{"prompt_tokens":500,"completion_tokens":700}}\n',
            b'data: [DONE]\n',
        ]
        client = LocalNemotron("http://127.0.0.1:8000/v1", "frozen-nemotron")
        with patch("urllib.request.urlopen", return_value=self.Stream(lines)):
            with self.assertRaisesRegex(ValueError, "malformed structured output"):
                client.generate_turn(system_prompt=system_prompt(), context={}, image_jpeg=b"\xff\xd8x\xff\xd9", response_schema=response_schema())
        self.assertEqual(client.last_turn["reasoning"], "I need to inspect the terrain.")
        self.assertEqual(client.last_turn["metrics"]["input_tokens"], 500)
        self.assertEqual(client.last_turn["metrics"]["output_tokens"], 700)
        self.assertEqual(client.last_turn["metrics"]["finish_reason"], "length")
        self.assertEqual(client.last_raw_response, '{"kind":')

    def test_length_limited_complete_action_is_recovered_from_open_wrapper(self):
        candidate = {
            "candidate_id": "inspect_scripts", "description": "Inspect scripts", "tool": "ls",
            "arguments": {"path": "scripts"},
        }
        partial = json.dumps({
            "kind": "act", "rationale": "Reuse known state", "candidates": [candidate],
        }, indent=2)[:-1] + "  " * 128
        lines = [
            ("data: " + json.dumps({"choices": [{"delta": {"content": partial}}]})).encode() + b"\n",
            b'data: {"choices":[{"finish_reason":"length","delta":{}}],"usage":{"prompt_tokens":500,"completion_tokens":8192}}\n',
            b"data: [DONE]\n",
        ]
        client = LocalNemotron("http://127.0.0.1:8000/v1", "frozen-nemotron")
        with patch("urllib.request.urlopen", return_value=self.Stream(lines)):
            turn = client.generate_turn(
                system_prompt=system_prompt(), context={}, image_jpeg=b"\xff\xd8x\xff\xd9",
                response_schema=response_schema(),
            )
        self.assertEqual(turn["parsed"]["candidates"], [candidate])
        self.assertEqual(turn["parsed"]["answer"], "")
        self.assertEqual(turn["recovery"]["status"], "recovered_complete_action")
        self.assertTrue(turn["metrics"]["structured_output_recovered"])

    def test_repetition_stop_recovers_a_complete_action_without_waiting_for_token_cap(self):
        candidate = {
            "candidate_id": "inspect_scripts", "description": "Inspect scripts", "tool": "ls",
            "arguments": {"path": "scripts"},
        }
        partial = json.dumps({"kind": "act", "candidates": [candidate]})[:-1] + ("  " * 40)
        lines = [
            ("data: " + json.dumps({"choices": [{"delta": {"content": partial}}]})).encode() + b"\n",
            b'data: {"choices":[{"finish_reason":"repetition","delta":{}}],"usage":{"prompt_tokens":500,"completion_tokens":400}}\n',
            b"data: [DONE]\n",
        ]
        client = LocalNemotron("http://127.0.0.1:8000/v1", "frozen-nemotron")
        with patch("urllib.request.urlopen", return_value=self.Stream(lines)):
            turn = client.generate_turn(
                system_prompt=system_prompt(), context={}, image_jpeg=b"\xff\xd8x\xff\xd9",
                response_schema=response_schema(),
            )
        self.assertEqual(turn["parsed"]["candidates"], [candidate])
        self.assertEqual(turn["recovery"]["stop_reason"], "repetition")

    def test_action_schema_does_not_require_unused_answer_field(self):
        self.assertNotIn("answer", response_schema()["required"])

    def test_observation_bounding_limits_variable_state(self):
        observation = {"nearby_blocks": [{"name": "stone", "position": {"x": x, "y": 0, "z": 0}} for x in range(500)], "recent_events": ["move"] * 100}
        bounded = LocalNemotron.bound_observation(observation)
        self.assertLessEqual(len(json.dumps(bounded, separators=(",", ":")).encode()), 3500)
        self.assertEqual(bounded["recent_events"], [])
        self.assertIn("nearby_blocks", bounded["observation_trimmed"])


class AgentLoopTests(unittest.TestCase):
    def test_prompt_explains_findblocks_vec3_shape_and_goal_near(self):
        prompt = system_prompt()
        self.assertIn("it has no `.position`", prompt)
        self.assertIn("new goals.GoalNear", prompt)
        self.assertIn("bot.craft` takes a recipe object", prompt)

    class Bot:
        def __init__(self):
            self.calls = []

        def request(self, method, _params, **_kwargs):
            self.calls.append(method)
            if method == "observe":
                return {"player": {"health": 20}, "inventory": [], "recent_events": []}
            if method == "verify_goal":
                return {"status": "incomplete"}
            return {"status": "idle", "controls_released": True}

    class Planner:
        model = "frozen-nemotron"
        artifact_revision = "abcdef"

        def __init__(self, response):
            self.response = response
            self.requests = []

        def generate_turn(self, **kwargs):
            self.requests.append(kwargs)
            return {"parsed": self.response, "reasoning": "visible reasoning", "raw_answer": json.dumps(self.response), "metrics": {"input_tokens": 10}}

    class Selector:
        last_latency_seconds = 0.001

        def __init__(self, candidate_id):
            self.candidate_id = candidate_id
            self.seen = []

        def choose(self, _state, _objective, candidates):
            self.seen = [candidate.candidate_id for candidate in candidates]
            return {candidate.candidate_id: float(candidate.candidate_id == self.candidate_id) for candidate in candidates}

    def test_recent_file_reads_include_a_bounded_content_preview(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(Path(temp) / "workspace")
            workspace.initialize()
            loop = AgentLoop(
                planner=self.Planner({}), selector=None, bot=self.Bot(), workspace=workspace,
                events=EventLog(Path(temp) / "events.jsonl", "run-1", "world-1"), mode="nemotron_only",
            )
            loop.recent_history.append({"turn": 1, "tool": "read", "result": {"path": "scripts/task.js", "content": "x" * 1200}})
            context = loop._context({"player": {}}, {})
            preview = context["recent_tool_history"][0]["result"]["content_preview"]
            self.assertEqual(len(preview), 252)
            self.assertTrue(preview.endswith("\n…"))

    def test_context_carries_a_longer_compacted_action_history(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(Path(temp) / "workspace")
            workspace.initialize()
            loop = AgentLoop(
                planner=self.Planner({}), selector=None, bot=self.Bot(), workspace=workspace,
                events=EventLog(Path(temp) / "events.jsonl", "run-1", "world-1"), mode="nemotron_only",
            )
            loop.recent_history = [
                {"turn": turn, "tool": "ls", "arguments": {"path": f"scripts/{turn}"}, "result": {"empty": True}}
                for turn in range(1, 21)
            ]
            history = loop._context({"player": {}}, {})["recent_tool_history"]
            self.assertEqual(len(history), 12)
            self.assertEqual(history[0]["turn"], 9)

    def test_unchanged_consecutive_read_and_listing_are_suppressed(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(Path(temp) / "workspace")
            workspace.initialize()
            (workspace.root / "notes.md").write_text("known result\n")
            events = EventLog(Path(temp) / "events.jsonl", "run-1", "world-1")
            loop = AgentLoop(
                planner=self.Planner({}), selector=None, bot=self.Bot(), workspace=workspace,
                events=events, mode="nemotron_only",
            )

            first_read = loop._execute_tool("read", {"path": "notes.md"})
            loop.recent_history.append({"turn": 1, "tool": "read", "arguments": {"path": "notes.md"}, "result": first_read})
            repeated_read = loop._execute_tool("read", {"path": "notes.md"})
            self.assertEqual(repeated_read["error"]["code"], "unchanged_repeat")

            full_read = loop._execute_tool("read", {"path": "notes.md", "full": True})
            self.assertTrue(full_read["ok"])
            loop.recent_history.append({"turn": 2, "tool": "ls", "arguments": {"path": "scripts"}, "result": {"empty": True}})
            repeated_listing = loop._execute_tool("ls", {"path": "scripts"})
            self.assertEqual(repeated_listing["error"]["code"], "unchanged_repeat")

            rows = [json.loads(line) for line in events.path.read_text().splitlines()]
            self.assertEqual([row["type"] for row in rows], ["agent_tool_repeat_suppressed"] * 2)

    def test_recent_program_history_keeps_exact_command_hash_and_output(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(Path(temp) / "workspace")
            workspace.initialize()
            script_path = workspace.root / "scripts/search.js"
            script_path.write_text("module.exports = async (bot) => { console.log('no target'); };\n")
            loop = AgentLoop(
                planner=self.Planner({}), selector=None, bot=self.Bot(), workspace=workspace,
                events=EventLog(Path(temp) / "events.jsonl", "run-1", "world-1"), mode="nemotron_only",
            )
            loop.recent_history.append({
                "turn": 2, "candidate_id": "search", "tool": "bash",
                "arguments": {"command": "mc-run scripts/search.js --timeout 30", "timeout_seconds": 40},
                "description": "Search for trees", "result": {
                    "status": "completed", "exit_code": 0,
                    "stdout": json.dumps({"status": "completed", "program_id": "p1", "script": "scripts/search.js", "sha256": "abc123", "duration_seconds": 4.2, "controls_released": True, "output": [{"method": "log", "text": "No trees found"}], "result": None}),
                    "stderr": "",
                },
            })
            loop.recent_history.append({
                "turn": 3, "candidate_id": "retry", "tool": "bash",
                "arguments": {"command": "mc-run scripts/search.js --timeout 30", "timeout_seconds": 40},
                "description": "Retry the same search script", "result": {
                    "status": "completed", "exit_code": 0,
                    "stdout": json.dumps({"status": "completed", "program_id": "p2", "script": "scripts/search.js", "sha256": "abc123", "duration_seconds": 4.1, "controls_released": True, "output": [{"method": "log", "text": "No trees found"}], "result": None}),
                    "stderr": "",
                },
            })
            history = loop._context({"player": {}}, {})["recent_tool_history"][0]
            self.assertEqual(len(loop._context({"player": {}}, {})["recent_tool_history"]), 2)
            self.assertEqual(history["arguments"]["command"], "mc-run scripts/search.js --timeout 30")
            retry = loop._context({"player": {}}, {})["recent_tool_history"][1]["result"]["program_result"]
            self.assertEqual(retry["sha256"], "abc123")
            self.assertEqual(retry["output"][0]["text"], "No trees found")
            self.assertEqual(retry["same_hash_runs_in_recent_history"], 2)
            self.assertNotIn("script_source_preview", history["result"]["program_result"])
            self.assertIn("no target", retry["script_source_preview"])
            self.assertLessEqual(len(retry["script_source_preview"]), 350)

    def test_nemotron_only_runs_its_single_candidate_without_selector_schema(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(Path(temp) / "workspace")
            workspace.initialize()
            events = EventLog(Path(temp) / "events.jsonl", "run-1", "world-1")
            candidate = {"candidate_id": "write_plan", "description": "Write the selected plan", "tool": "write", "arguments": {"path": "plan.md", "content": "chosen"}}
            response = {"kind": "act", "rationale": "Record the plan.", "candidates": [candidate], "answer": ""}
            image = b"\xff\xd8current\xff\xd9"
            loop = AgentLoop(
                planner=self.Planner(response), selector=None, bot=self.Bot(), workspace=workspace,
                events=events, mode="nemotron_only",
                image_provider=lambda: (image, {"run_id": "run-1", "received_at_monotonic": time.monotonic()}),
            )
            result = loop.run(max_turns=1)
            self.assertEqual(result["status"], "turn_limit")
            self.assertEqual((workspace.root / "plan.md").read_text(), "chosen")
            rows = [json.loads(line) for line in events.path.read_text().splitlines()]
            choice = next(row for row in rows if row["type"] == "nemotron_self_choice")
            self.assertEqual(choice["payload"]["selected_candidate_id"], "write_plan")
            self.assertNotIn("selected_candidate_id", response_schema("nemotron_only")["required"])
            self.assertNotIn("selected_candidate_id", response_schema("nemotron_clef")["required"])
            self.assertEqual(response_schema("nemotron_only")["properties"]["candidates"]["maxItems"], 1)
            self.assertEqual(response_schema("nemotron_clef")["properties"]["candidates"]["maxItems"], 4)

    def test_clef_selects_an_exact_planner_candidate_and_preserves_the_latest_image_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            workspace = Workspace(root)
            workspace.initialize()
            events = EventLog(Path(temp) / "events.jsonl", "run-1", "world-1")
            candidates = [
                {"candidate_id": "write_plan", "description": "Write plan", "tool": "write", "arguments": {"path": "plan.md", "content": "old"}},
                {"candidate_id": "write_memory", "description": "Write memory", "tool": "write", "arguments": {"path": "memory.md", "content": "selected"}},
            ]
            response = {"kind": "choose", "rationale": "Use the memory note", "candidates": candidates, "answer": ""}
            planner = self.Planner(response)
            selector = self.Selector("write_memory")
            image1, image2 = b"\xff\xd8frame-one\xff\xd9", b"\xff\xd8frame-two\xff\xd9"
            supplied = iter([image1, image2])

            def image_provider():
                image = next(supplied)
                return image, {"image_sha256": __import__("hashlib").sha256(image).hexdigest(), "received_at_monotonic": time.monotonic(), "run_id": "run-1"}

            loop = AgentLoop(planner=planner, selector=selector, bot=self.Bot(), workspace=workspace, events=events, mode="nemotron_clef", image_provider=image_provider)
            result = loop.run(max_turns=1)
            self.assertEqual(result["status"], "turn_limit", result)
            self.assertEqual(selector.seen, ["write_plan", "write_memory"])
            self.assertFalse((root / "plan.md").read_text())
            self.assertEqual((root / "memory.md").read_text(), "selected")
            self.assertEqual(planner.requests[0]["image_jpeg"], image1)
            self.assertEqual(planner.requests[0]["context"]["image"]["image_sha256"], __import__("hashlib").sha256(image1).hexdigest())
            self.assertNotIn("frame-one", json.dumps(planner.requests[0]["context"]))
            self.assertTrue((root / ".runtime/current-image.jpg").read_bytes().endswith(b"frame-one\xff\xd9"))
            turn_events = [json.loads(line) for line in events.path.read_text().splitlines() if '"type":"nemotron_turn"' in line]
            self.assertEqual(turn_events[0]["payload"]["model_revision"], "abcdef")

    def test_pause_resume_gate_does_not_mutate_run_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(Path(temp) / "workspace")
            workspace.initialize()
            workspace.execute("write", {"path": "memory.md", "content": "learned"})
            events = EventLog(Path(temp) / "events.jsonl", "run-1", "world-1")
            loop = AgentLoop(planner=self.Planner({}), selector=None, bot=self.Bot(), workspace=workspace, events=events, mode="nemotron_only")
            loop.pause()
            waited = []
            thread = threading.Thread(target=lambda: waited.append(loop._wait_if_paused()))
            thread.start()
            time.sleep(0.03)
            self.assertTrue(thread.is_alive())
            loop.resume()
            thread.join(timeout=1)
            self.assertEqual(waited, [True])
            self.assertEqual((workspace.root / "memory.md").read_text(), "learned")


if __name__ == "__main__":
    unittest.main()
