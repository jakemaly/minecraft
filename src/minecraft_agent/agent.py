"""Single-turn proposal, typed selection, bounded execution, and evidence."""

from __future__ import annotations

import uuid
import time
from typing import Any

from .events import EventLog
from .models.interfaces import Candidate, Planner, Proposal, Selector, validate_probabilities, validate_proposal


class AgentStep:
    def __init__(self, planner: Planner, selector: Selector, bot, events: EventLog, helper_runner=None):
        self.planner = planner
        self.selector = selector
        self.bot = bot
        self.events = events
        self.helper_runner = helper_runner

    def run(self, objective: str, memory: str = "", *, allowed_action_types: set[str] | None = None, required_candidate_kind: str | None = None) -> dict[str, Any]:
        observation = self.bot.request("observe", {})
        bound_observation = getattr(self.planner, "bound_observation", None)
        if bound_observation is not None:
            observation = bound_observation(observation)
        turn_id = str(uuid.uuid4())
        self.events.append("observation", {"turn_id": turn_id, "model_visible": observation})
        proposal = None
        for attempt in range(2):
            planner_started = time.monotonic()
            try:
                proposal = self.planner.propose(observation, objective, memory)
                validate_proposal(proposal)
                break
            except Exception as exc:
                self.events.append("planner_error", {"turn_id": turn_id, "attempt": attempt + 1, "error_type": type(exc).__name__, "message": str(exc)[:300]})
                proposal = None
        if proposal is None:
            proposal = Proposal(objective, "Planner failed validation twice; return control for a new observation.", (Candidate("replan", "Stop and return control to the planner", "replan", {}),), "")
        if required_candidate_kind and not any(candidate.kind == required_candidate_kind for candidate in proposal.candidates):
            raise RuntimeError(f"planner did not propose a required {required_candidate_kind} candidate; no action was executed")
        self.events.append("planner_proposal", {"turn_id": turn_id, "objective": proposal.objective, "rationale": proposal.rationale, "latency_seconds": proposal.latency_seconds or time.monotonic() - planner_started, "candidates": [{"candidate_id": c.candidate_id, "description": c.description, "kind": c.kind, "payload": c.payload} for c in proposal.candidates], "raw_response": proposal.raw_response})
        selector_started = time.monotonic()
        selector_failed = None
        try:
            scores = self.selector.choose(observation, proposal.objective, list(proposal.candidates))
            validate_probabilities(scores, [candidate.candidate_id for candidate in proposal.candidates])
            selector_latency = getattr(self.selector, "last_latency_seconds", time.monotonic() - selector_started)
        except Exception as exc:
            selector_failed = {"error_type": type(exc).__name__, "message": str(exc)[:300]}
            self.events.append("selector_error", {"turn_id": turn_id, **selector_failed})
            scores = {candidate.candidate_id: float(candidate.candidate_id == "replan") for candidate in proposal.candidates}
            selector_latency = time.monotonic() - selector_started
        selected = sorted(proposal.candidates, key=lambda candidate: (-scores[candidate.candidate_id], candidate.candidate_id))[0]
        self.events.append("selector_decision", {"turn_id": turn_id, "candidate_order": [c.candidate_id for c in proposal.candidates], "probabilities": scores, "latency_seconds": selector_latency, "selected_candidate_id": selected.candidate_id, "tie_break": "lexicographically smallest candidate ID"})
        if selector_failed or selected.kind == "replan":
            result = {"status": "replan", "observation": self.bot.request("observe", {})}
        elif required_candidate_kind and selected.kind != required_candidate_kind:
            result = {"status": "not_executed", "reason": f"practice requires Clef to select a {required_candidate_kind} candidate", "observation": self.bot.request("observe", {})}
        elif selected.kind == "helper":
            if self.helper_runner is None:
                raise RuntimeError("helper execution requires an available helper runner")
            source = selected.payload.get("source")
            try:
                result = self.helper_runner.run(source, "agent_helper")
                result["observation"] = self.bot.request("observe", {})
            except Exception as exc:
                result = {"status": "unknown", "error_type": type(exc).__name__, "message": str(exc)[:300]}
        else:
            action_type = selected.payload.get("type")
            if allowed_action_types is not None and action_type not in allowed_action_types:
                raise ValueError(f"action type {action_type!r} is outside the active action lease")
            action_id = str(uuid.uuid4())
            try:
                result = self.bot.request("execute", {"action_id": action_id, "action": selected.payload, "timeout_seconds": 5})
            except Exception as exc:
                result = {"status": "unknown", "action_id": action_id, "error_type": type(exc).__name__, "message": str(exc)[:300]}
        self.events.append("action_result", {"turn_id": turn_id, "candidate_id": selected.candidate_id, "result": result}, causal_id=turn_id)
        return {"turn_id": turn_id, "proposal": proposal, "scores": scores, "selected": selected, "result": result}
