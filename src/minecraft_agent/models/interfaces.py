"""Model-neutral planner and selector contracts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    description: str
    kind: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class Proposal:
    objective: str
    rationale: str
    candidates: tuple[Candidate, ...]
    raw_response: str
    latency_seconds: float = 0.0


def validate_proposal(proposal: Proposal) -> None:
    if not proposal.objective.strip() or len(proposal.objective) > 1000:
        raise ValueError("planner objective must contain 1–1000 characters")
    if len(proposal.rationale) > 4000 or len(proposal.candidates) > 9:
        raise ValueError("planner rationale or candidate count exceeds its bound")
    ids = set()
    for candidate in proposal.candidates:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", candidate.candidate_id):
            raise ValueError("candidate ID contains unsupported characters")
        if candidate.candidate_id in ids:
            raise ValueError("candidate IDs must be unique")
        ids.add(candidate.candidate_id)
        if len(candidate.description) > 1000 or candidate.kind not in {"primitive", "helper", "replan"} or not isinstance(candidate.payload, dict):
            raise ValueError("candidate description, kind, or payload is invalid")
        if (candidate.candidate_id == "replan") != (candidate.kind == "replan"):
            raise ValueError("only the reserved replan candidate may use the replan kind")
        if len(json.dumps(candidate.payload, ensure_ascii=False).encode("utf-8")) > 64_000:
            raise ValueError("candidate payload exceeds the 64 KiB limit")
        if candidate.kind == "helper":
            source = candidate.payload.get("source")
            if not isinstance(source, str) or not source.strip() or len(source.encode("utf-8")) > 32_000:
                raise ValueError("helper candidate requires source no larger than 32 KiB")
    replan = [candidate for candidate in proposal.candidates if candidate.candidate_id == "replan"]
    if len(replan) != 1 or replan[0].kind != "replan":
        raise ValueError("proposal must contain exactly one reserved replan candidate")


class Planner(Protocol):
    def propose(self, observation: dict[str, Any], objective: str, memory: str = "") -> Proposal: ...


class Selector(Protocol):
    def choose(self, state: dict[str, Any], objective: str, candidates: list[Candidate]) -> dict[str, float]: ...


def validate_probabilities(scores: dict[str, float], candidate_ids: list[str], *, tolerance: float = 1e-5) -> None:
    import math
    if set(scores) != set(candidate_ids):
        raise ValueError("selector result must cover exactly the offered candidate IDs")
    if any(not math.isfinite(value) or value < 0 for value in scores.values()):
        raise ValueError("selector probabilities must be finite and non-negative")
    if abs(sum(scores.values()) - 1.0) > tolerance:
        raise ValueError("selector probabilities must sum to one")
