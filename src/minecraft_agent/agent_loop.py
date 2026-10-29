"""Long-horizon tool loop for Nemotron-only and Nemotron-plus-Clef runs."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

from .models.interfaces import Candidate, validate_probabilities
from .workspace import TOOL_NAMES, Workspace, tool_schemas


OBJECTIVE = "The objective is to craft and place a diamond block. This is the only criterion. You may use any of the tools at your disposal to accomplish this."


def response_schema(mode: str = "nemotron_only") -> dict[str, Any]:
    if mode not in {"nemotron_only", "nemotron_clef"}:
        raise ValueError("mode must be nemotron_only or nemotron_clef")
    properties = {
        "kind": {"enum": ["act", "choose", "final"]},
        "rationale": {"type": "string", "maxLength": 800},
        "candidates": {
            "type": "array", "maxItems": 1 if mode == "nemotron_only" else 4,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["candidate_id", "description", "tool", "arguments"],
                "properties": {
                    "candidate_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$"},
                    "description": {"type": "string", "minLength": 1, "maxLength": 300},
                    "tool": {"enum": list(TOOL_NAMES)},
                    "arguments": {"type": "object", "additionalProperties": True},
                },
            },
        },
        "answer": {"type": "string", "maxLength": 800},
    }
    required = ["kind", "rationale", "candidates"]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


def system_prompt(objective: str = OBJECTIVE, mode: str = "nemotron_only") -> str:
    tools = json.dumps(tool_schemas(), ensure_ascii=False, separators=(",", ":"))
    if mode == "nemotron_only":
        decision_contract = "For each action turn, return kind=act with exactly one candidate: choose and specify the action yourself; the harness executes that candidate. When reporting final, return no candidates."
    elif mode == "nemotron_clef":
        decision_contract = "For each turn, return kind=choose with 1–4 distinct candidates and leave selection to the frozen Clef model. When reporting final, return no candidates."
    else:
        raise ValueError("mode must be nemotron_only or nemotron_clef")
    return (
        f"{objective}\n\n"
        "Observe before acting. For multi-step work, write a short plan in plan.md. "
        "Use the workspace to inspect documentation, write and test programs, and keep verified learnings in memory.md. "
        "For reusable abilities, create a tested skills/<name>/SKILL.md with a one-sentence description and reliable procedure; find skill files and read the relevant one before use. "
        "Mineflayer programs can be run with `mc-run scripts/<file>.js --timeout 60`; each program is an async JavaScript body or CommonJS module exporting an async function, receives the live bot, and has mineflayer-pathfinder loaded. API shapes: `const pos = bot.findBlocks(options)[0]` gives a Vec3 with `pos.x`, `pos.y`, `pos.z` directly; it has no `.position`. `bot.findBlock(options)` gives a Block or null, and a Block has `.position`. For example, `if (pos) { const { goals } = require('mineflayer-pathfinder'); await bot.pathfinder.goto(new goals.GoalNear(pos.x, pos.y, pos.z, 2)); }`. `bot.inventory.items()` returns inventory items; `bot.recipesFor(itemTypeId, metadata, minResultCount, craftingTable)` returns recipes, and `bot.craft` takes a recipe object, not an item. "
        "When a program result shows an error or no progress, use its exact command, script path, hash, and output to revise the program or choose a different action; do not repeat an unchanged program without new evidence. "
        "The bash tool also has a deadline; when a script needs longer than 50 seconds, set both its mc-run timeout and bash timeout_seconds, with bash at least 10 seconds longer. "
        "Use the supplied recent tool history, plan.md, and memory.md. Read the documentation index at docs/mineflayer/agent-quick-reference.md only when a specific API question remains unresolved, then reuse that result. Do not repeat successful reads or listings unless the file or directory changed or you need full content. Every turn should make one concrete, verified step toward the objective. Check tool results and a fresh game observation before claiming progress. A final answer is not success unless the live world verifies the placed diamond block. "
        f"{decision_contract} "
        "Use only the tools listed below. Return strict JSON with kind=final only when reporting a completed/incomplete check; otherwise follow the mode-specific action contract. "
        "Keep tool arguments exact and concise. Keep rationale to one short sentence under 40 words and the final answer to two sentences. Candidate IDs must be unique.\n"
        f"TOOLS={tools}"
    )


class AgentLoop:
    def __init__(
        self,
        *,
        planner,
        selector,
        bot,
        workspace: Workspace,
        events,
        mode: str,
        objective: str = OBJECTIVE,
        image_provider=None,
        on_update=None,
    ):
        if mode not in {"nemotron_only", "nemotron_clef"}:
            raise ValueError("mode must be nemotron_only or nemotron_clef")
        if mode == "nemotron_clef" and selector is None:
            raise ValueError("Nemotron-plus-Clef mode requires the frozen Clef selector")
        self.planner = planner
        self.selector = selector
        self.bot = bot
        self.workspace = workspace
        self.events = events
        self.mode = mode
        self.objective = objective
        self.image_provider = image_provider
        self.on_update = on_update or (lambda _state: None)
        self.resume_event = threading.Event()
        self.resume_event.set()
        self.stop_event = threading.Event()
        self.turn = 0
        self.recent_history: list[dict[str, Any]] = []

    def pause(self) -> None:
        self.resume_event.clear()
        self.workspace.cancel_active()
        try:
            self.bot.request("cancel_program", {}, request_timeout=2)
        except Exception:
            pass
        self.events.append("agent_paused", {"turn": self.turn})

    def resume(self) -> None:
        self.resume_event.set()
        self.events.append("agent_resumed", {"turn": self.turn})

    def stop(self) -> None:
        self.stop_event.set()
        self.resume_event.set()
        self.workspace.cancel_active()
        try:
            self.bot.request("cancel_program", {}, request_timeout=2)
        except Exception:
            pass

    def _wait_if_paused(self) -> bool:
        while not self.resume_event.wait(0.2):
            if self.stop_event.is_set():
                return False
        return not self.stop_event.is_set()

    def _capture_image(self) -> tuple[bytes | None, dict[str, Any]]:
        deadline = time.monotonic() + 20
        last_meta: dict[str, Any] = {"status": "waiting_for_first_frame"}
        while time.monotonic() < deadline and not self.stop_event.is_set():
            if not self._wait_if_paused():
                return None, {"status": "stopped_before_frame"}
            try:
                if self.image_provider is not None:
                    image, metadata = self.image_provider()
                else:
                    result = self.bot.request("capture_image", {}, request_timeout=15)
                    encoded = result.get("jpeg_base64")
                    image = base64.b64decode(encoded, validate=True) if encoded else None
                    metadata = {key: value for key, value in result.items() if key != "jpeg_base64"}
                last_meta = dict(metadata or {"status": "waiting_for_first_frame"})
                received = last_meta.get("received_at_monotonic")
                if received is not None and time.monotonic() - float(received) > 5:
                    image = None
                    last_meta["status"] = "stale_frame"
                if image and (not image.startswith(b"\xff\xd8") or not image.endswith(b"\xff\xd9")):
                    image = None
                    last_meta["status"] = "invalid_jpeg"
                if image:
                    target = self.workspace.root / ".runtime/current-image.jpg"
                    target.write_bytes(image)
                    last_meta["image_sha256"] = hashlib.sha256(image).hexdigest()
                    last_meta["bytes"] = len(image)
                    return image, last_meta
            except Exception as exc:
                last_meta = {"status": "unavailable", "error_type": type(exc).__name__, "message": str(exc)[:240]}
            self.stop_event.wait(0.1)
        raise RuntimeError(f"a current first-person JPEG frame is required for each model turn ({last_meta.get('status', 'unavailable')})")

    def _context(self, observation: dict[str, Any], image_meta: dict[str, Any]) -> dict[str, Any]:
        memory_path = self.workspace.root / "memory.md"
        plan_path = self.workspace.root / "plan.md"
        memory = memory_path.read_text(encoding="utf-8", errors="replace")[-900:] if memory_path.exists() else ""
        plan = plan_path.read_text(encoding="utf-8", errors="replace")[-700:] if plan_path.exists() else ""
        docs = [p.relative_to(self.workspace.root).as_posix() for p in sorted((self.workspace.root / "docs").rglob("*.md"))[:8]]
        history = []
        for item in self.recent_history[-12:]:
            compact = {key: item.get(key) for key in ("turn", "candidate_id", "tool", "description", "verified_goal") if key in item}
            arguments = item.get("arguments")
            if isinstance(arguments, dict):
                compact_arguments = {}
                for key, value in arguments.items():
                    if key == "content":
                        continue
                    if key == "command" and isinstance(value, str):
                        compact_arguments[key] = value if len(value) <= 160 else value[:120] + f" … [{len(value)} chars, sha256={hashlib.sha256(value.encode()).hexdigest()[:12]}]"
                    else:
                        compact_arguments[key] = value
                compact["arguments"] = compact_arguments
            result = item.get("result")
            if isinstance(result, dict):
                summarized = {key: value for key, value in result.items() if key not in {"stdout", "stderr", "matches", "content", "diff"}}
                if isinstance(result.get("content"), str) and result["content"]:
                    summarized["content_preview"] = result["content"][:250]
                    if len(result["content"]) > 250:
                        summarized["content_preview"] += "\n…"
                for key in ("stdout", "stderr"):
                    if isinstance(result.get(key), str) and result[key]:
                        text = result[key]
                        if key == "stdout":
                            try:
                                program = json.loads(text)
                            except json.JSONDecodeError:
                                program = None
                            if isinstance(program, dict) and "program_id" in program:
                                summarized["program_result"] = {
                                    field: program.get(field)
                                    for field in ("status", "script", "sha256", "duration_seconds", "controls_released")
                                    if field in program
                                }
                                if program.get("output"):
                                    summarized["program_result"]["output"] = [
                                        {"method": entry.get("method"), "text": str(entry.get("text", ""))[-120:]}
                                        for entry in program["output"][-3:]
                                    ]
                                if "result" in program:
                                    summarized["program_result"]["result"] = program["result"]
                            else:
                                summarized[key] = text[-180:]
                        else:
                            summarized[key] = text[-180:]
                if result.get("matches"):
                    summarized["matches_count"] = len(result["matches"])
                compact["result"] = summarized
            if "final_answer" in item:
                compact["final_answer"] = str(item["final_answer"])[-300:]
            history.append(compact)
        program_hash_counts: dict[str, int] = {}
        for item in history:
            program = item.get("result", {}).get("program_result", {})
            script_hash = program.get("sha256")
            if script_hash:
                program_hash_counts[script_hash] = program_hash_counts.get(script_hash, 0) + 1
        for item in history:
            program = item.get("result", {}).get("program_result", {})
            if program.get("sha256") in program_hash_counts:
                program["same_hash_runs_in_recent_history"] = program_hash_counts[program["sha256"]]
        latest_program = next((item.get("result", {}).get("program_result") for item in reversed(history) if item.get("result", {}).get("program_result")), None)
        if latest_program and isinstance(latest_program.get("script"), str):
            try:
                script_path = self.workspace._resolve(latest_program["script"])
                if script_path.is_file():
                    latest_program["script_source_preview"] = script_path.read_text(encoding="utf-8", errors="replace")[:350]
            except (OSError, ValueError):
                pass
        return {
            "run": {"mode": self.mode, "turn": self.turn, "objective": self.objective},
            "memory": memory,
            "plan": plan,
            "documentation_index": docs,
            "game_state": observation,
            "recent_tool_history": history,
            "image": {key: value for key, value in image_meta.items() if key not in {"jpeg_base64", "data_url"}},
            "output_contract": "Return one JSON object matching the response schema. Use final only as a report; the harness separately verifies success.",
        }

    def _record_model_metrics(self) -> dict[str, Any]:
        method = getattr(self.planner, "metrics_snapshot", None)
        if method is None:
            return {"status": "unavailable"}
        try:
            return method()
        except Exception as exc:
            return {"status": "unavailable", "error_type": type(exc).__name__}

    def _goal_status(self) -> dict[str, Any]:
        try:
            return self.bot.request("verify_goal", {}, request_timeout=5)
        except Exception as exc:
            return {"status": "unavailable", "error_type": type(exc).__name__, "message": str(exc)[:240]}

    def _execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.recent_history:
            previous = self.recent_history[-1]
            previous_args = previous.get("arguments", {})
            same_read = (
                tool_name == "read" and previous.get("tool") == "read"
                and arguments.get("path") == previous_args.get("path")
            )
            upgraded_read = same_read and bool(arguments.get("full")) and not bool(previous_args.get("full"))
            same_listing = (
                tool_name == "ls" and previous.get("tool") == "ls"
                and arguments.get("path", ".") == previous_args.get("path", ".")
                and bool(arguments.get("all")) == bool(previous_args.get("all"))
            )
            if (same_read and not upgraded_read) or same_listing:
                message = "This unchanged read/list result is already in recent tool history; use that result or choose a different action."
                self.events.append("agent_tool_repeat_suppressed", {
                    "turn": self.turn, "tool": tool_name, "arguments": arguments,
                    "previous_turn": previous.get("turn"), "message": message,
                })
                return {"ok": False, "error": {"code": "unchanged_repeat", "message": message}}
        return self.workspace.execute(tool_name, arguments)

    def run(self, *, max_turns: int | None = None) -> dict[str, Any]:
        started = time.monotonic()
        final_status: dict[str, Any] = {"status": "stopped"}
        self.events.append("agent_run_started", {"mode": self.mode, "objective": self.objective})
        try:
            while not self.stop_event.is_set() and (max_turns is None or self.turn < max_turns):
                if not self._wait_if_paused():
                    break
                self.turn += 1
                observation = self.bot.request("observe", {}, request_timeout=15)
                bound_observation = getattr(self.planner, "bound_observation", None)
                if bound_observation is not None:
                    observation = bound_observation(observation)
                image, image_meta = self._capture_image()
                context = self._context(observation, image_meta)
                metrics_before = self._record_model_metrics()
                model_started = time.monotonic()
                try:
                    turn_result = self.planner.generate_turn(
                        system_prompt=system_prompt(self.objective, self.mode),
                        context=context,
                        image_jpeg=image,
                        response_schema=response_schema(self.mode),
                    )
                except Exception as exc:
                    partial_turn = getattr(self.planner, "last_turn", {}) or {}
                    self.events.append("nemotron_turn_error", {
                        "turn": self.turn, "model": getattr(self.planner, "model", None),
                        "model_revision": getattr(self.planner, "artifact_revision", None),
                        "latency_seconds": time.monotonic() - model_started,
                        "error_type": type(exc).__name__, "message": str(exc)[:500],
                        "image": image_meta, "reasoning_trace": partial_turn.get("reasoning", ""),
                        "raw_response": partial_turn.get("raw_answer", getattr(self.planner, "last_raw_response", "")),
                        "metrics": partial_turn.get("metrics", "unavailable"),
                        "usage": "unavailable" if not partial_turn else partial_turn.get("metrics", {}).get("usage", "unavailable"),
                        "cost_status": "unavailable_local_inference",
                    })
                    raise
                response = turn_result["parsed"]
                metrics = turn_result["metrics"]
                metrics_after = self._record_model_metrics()
                cache_delta = _metric_delta(metrics_before, metrics_after)
                self.events.append("nemotron_turn", {
                    "turn": self.turn, "mode": self.mode, "rationale": response.get("rationale"),
                    "model": getattr(self.planner, "model", None), "model_revision": getattr(self.planner, "artifact_revision", None),
                    "reasoning_trace": turn_result.get("reasoning", ""), "final_answer": response.get("answer", ""),
                    "raw_response": turn_result.get("raw_answer", ""), "metrics": metrics,
                    "vllm_cache_metrics_before": metrics_before, "vllm_cache_metrics_after": metrics_after,
                    "vllm_cache_metric_delta": cache_delta, "image": image_meta,
                    "candidate_ids": [item.get("candidate_id") for item in response.get("candidates", [])],
                    "selected_candidate_id": response.get("selected_candidate_id"),
                    "structured_output_recovery": turn_result.get("recovery"),
                })
                goal_status = self._goal_status()
                if goal_status.get("status") == "passed":
                    final_status = {"status": "completed", "turns": self.turn, "goal": goal_status}
                    break
                if not self._wait_if_paused():
                    break
                kind = response.get("kind")
                candidates = response.get("candidates", [])
                if kind == "final":
                    result = {"status": "unverified_final", "answer": response.get("answer", ""), "goal": goal_status}
                    self.events.append("agent_unverified_final", {"turn": self.turn, **result})
                    self.recent_history.append({"turn": self.turn, "final_answer": result["answer"], "verified_goal": False})
                    self.on_update({"turn": self.turn, "status": "working", "last_result": result, "goal": goal_status})
                    continue
                if not candidates:
                    result = {"ok": False, "error": {"code": "empty_candidates", "message": "model returned no tool candidates"}}
                    self.events.append("agent_protocol_error", {"turn": self.turn, **result})
                    self.recent_history.append({"turn": self.turn, "result": result})
                    continue
                candidate_ids = [item.get("candidate_id") for item in candidates]
                if len(candidate_ids) != len(set(candidate_ids)):
                    raise ValueError("planner candidates must have unique IDs")
                if any(item.get("tool") not in TOOL_NAMES or not isinstance(item.get("arguments"), dict) for item in candidates):
                    raise ValueError("planner returned an unknown tool or non-object arguments")
                if self.mode == "nemotron_only":
                    if kind != "act" or len(candidates) != 1:
                        raise ValueError("Nemotron-only response must contain exactly one action candidate")
                    selected = candidates[0]
                    selected_id = selected["candidate_id"]
                    scores = {selected["candidate_id"]: 1.0}
                    selector_metrics = None
                    self.events.append("nemotron_self_choice", {
                        "turn": self.turn, "candidate_ids": candidate_ids,
                        "selected_candidate_id": selected_id,
                    })
                else:
                    if kind != "choose":
                        raise ValueError("Nemotron-plus-Clef response must contain a choice set")
                    typed = [Candidate(item["candidate_id"], item["description"], "primitive", {"tool": item["tool"], "arguments": item["arguments"]}) for item in candidates]
                    started_clef = time.monotonic()
                    try:
                        scores = self.selector.choose(observation, self.objective, typed)
                        validate_probabilities(scores, [item.candidate_id for item in typed])
                    except Exception as exc:
                        self.events.append("clef_choice_error", {
                            "turn": self.turn, "candidate_ids": [item.candidate_id for item in typed],
                            "model": getattr(self.selector, "model", "Cloudflare/clef-flash"),
                            "model_revision": getattr(self.selector, "artifact_revision", None),
                            "latency_seconds": time.monotonic() - started_clef,
                            "error_type": type(exc).__name__, "message": str(exc)[:500],
                            "usage": "unavailable", "cost_status": "unavailable_local_inference",
                        })
                        raise
                    selected_id = sorted(scores, key=lambda cid: (-scores[cid], cid))[0]
                    selected = next(item for item in candidates if item["candidate_id"] == selected_id)
                    selector_metrics = {
                        **getattr(self.selector, "last_metrics", {}),
                        "latency_seconds": getattr(self.selector, "last_latency_seconds", time.monotonic() - started_clef),
                        "scores": scores,
                    }
                    self.events.append("clef_choice", {"turn": self.turn, "model": getattr(self.selector, "model", "Cloudflare/clef-flash"), "model_revision": getattr(self.selector, "artifact_revision", None), "candidate_ids": [item.candidate_id for item in typed], "scores": scores, "selected_candidate_id": selected_id, "metrics": selector_metrics})
                tool_name = selected["tool"]
                tool_result = self._execute_tool(tool_name, selected["arguments"])
                self.events.append("agent_tool_result", {
                    "turn": self.turn, "candidate_id": selected["candidate_id"], "tool": tool_name,
                    "arguments": selected["arguments"], "result": tool_result,
                    "nemotron_scores": scores, "selector_metrics": selector_metrics,
                })
                goal_status = self._goal_status()
                self.recent_history.append({"turn": self.turn, "candidate_id": selected["candidate_id"], "tool": tool_name, "arguments": selected["arguments"], "description": selected["description"], "result": tool_result, "verified_goal": goal_status.get("status") == "passed"})
                self.recent_history = self.recent_history[-20:]
                self.on_update({"turn": self.turn, "status": "working", "last_tool": tool_name, "last_result": tool_result, "goal": goal_status, "metrics": metrics, "clef": selector_metrics})
                if goal_status.get("status") == "passed":
                    final_status = {"status": "completed", "turns": self.turn, "goal": goal_status}
                    break
                if not self._wait_if_paused():
                    break
            if self.stop_event.is_set():
                final_status = {"status": "stopped", "turns": self.turn, "goal": self._goal_status()}
            elif max_turns is not None and self.turn >= max_turns and final_status.get("status") != "completed":
                final_status = {"status": "turn_limit", "turns": self.turn, "goal": self._goal_status()}
            return final_status
        except Exception as exc:
            final_status = {"status": "failed", "turns": self.turn, "error_type": type(exc).__name__, "message": str(exc)[:500], "goal": self._goal_status()}
            return final_status
        finally:
            elapsed = time.monotonic() - started
            final_status["elapsed_seconds"] = round(elapsed, 3)
            self.events.append("agent_run_finished", final_status)
            self.on_update(final_status)


def _metric_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    if before.get("status") != "ok" or after.get("status") != "ok":
        return {"status": "unavailable"}
    start = before.get("counters", {})
    end = after.get("counters", {})
    counters = {key: end[key] - start[key] for key in end.keys() & start.keys()}
    queries = sum(value for key, value in counters.items() if "prefix_cache_queries" in key)
    hits = sum(value for key, value in counters.items() if "prefix_cache_hits" in key)
    cache = (
        {"status": "ok", "queries": queries, "hits": hits, "misses": max(0.0, queries - hits)}
        if queries or hits else
        {"status": "unavailable", "reason": "vLLM did not expose prefix-cache counters in this scrape"}
    )
    return {"status": "ok", "counters": counters, "prefix_cache": cache}
