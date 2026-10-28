"""OpenAI-compatible adapter for a separately supervised local vLLM server."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

from .interfaces import Candidate, Proposal, validate_proposal


class LocalNemotron:
    def __init__(self, base_url: str, model: str, timeout: int = 180):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.last_raw_response = ""

    @staticmethod
    def bound_observation(observation: dict[str, Any], max_bytes: int = 5_000) -> dict[str, Any]:
        """Trim the largest variable-length fields to fit the pinned context window."""
        bounded = dict(observation)
        original_counts = {
            key: len(value)
            for key, value in bounded.items()
            if key in {"nearby_blocks", "nearby_entities", "inventory", "recent_events"} and isinstance(value, list)
        }

        def size() -> int:
            return len(json.dumps(bounded, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

        for key in ("nearby_blocks", "nearby_entities", "recent_events", "inventory"):
            value = bounded.get(key)
            if not isinstance(value, list):
                continue
            while value and size() > max_bytes:
                value = value[: len(value) // 2]
                bounded[key] = value
        if size() > max_bytes:
            for key in ("nearby_blocks", "nearby_entities", "recent_events", "inventory"):
                if isinstance(bounded.get(key), list):
                    bounded[key] = []
                if size() <= max_bytes:
                    break
        bounded["observation_trimmed"] = {
            key: {"observed": original_counts[key], "sent": len(bounded.get(key, []))}
            for key in original_counts
            if len(bounded.get(key, [])) < original_counts[key]
        }
        return bounded

    def propose(self, observation: dict[str, Any], objective: str, memory: str = "") -> Proposal:
        observation = self.bound_observation(observation)
        helper_requested = "helper" in objective.casefold()
        exact_helper = re.search(r"await\s+api\.act\(\{[^}]{0,200}\}\);", objective)
        source_schema: dict[str, Any] = {"type": "string", "minLength": 16, "maxLength": 32000}
        if helper_requested and exact_helper:
            source_schema = {"type": "string", "enum": [exact_helper.group(0)]}
        payload_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "source": source_schema,
                "type": {"enum": ["look", "wait"]},
                "yaw": {"type": "number"},
                "pitch": {"type": "number"},
                "duration_ms": {"type": "integer"},
            },
            "additionalProperties": True,
        }
        if helper_requested:
            payload_schema["required"] = ["source"]
        else:
            payload_schema["minProperties"] = 1
        candidate_schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["candidate_id", "description", "kind", "payload"],
            "properties": {
                "candidate_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$"},
                "description": {"type": "string", "minLength": 1, "maxLength": 1000},
                "kind": {"enum": ["helper"] if helper_requested else ["primitive", "helper"]},
                "payload": payload_schema,
            },
        }
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["objective", "rationale", "candidates"],
            "properties": {
                "objective": {"type": "string"},
                "rationale": {"type": "string"},
                "candidates": {"type": "array", "maxItems": 8, "items": candidate_schema},
            },
        }
        prompt = {"objective": objective, "memory": memory[-1200:], "observation": observation, "instructions": "Propose bounded actions from the supplied state only. Candidate actions must be agent-authored. Give each candidate a short ASCII ID using only letters, digits, underscore, or hyphen. A helper payload must include a nonempty source field containing runnable JavaScript using only the documented api.act/api.observe capability API; never use ellipses or placeholder text. A primitive payload must include a valid action type and its required arguments. Do not assume hidden world state. Return JSON matching the schema."}
        payload = {"model": self.model, "messages": [{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}], "temperature": 0.2, "max_tokens": 512, "response_format": {"type": "json_schema", "json_schema": {"name": "agent_proposal", "strict": True, "schema": schema}}}
        request = urllib.request.Request(self.base_url + "/chat/completions", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read(2_000_000))
        except urllib.error.HTTPError as exc:
            detail = exc.read(1_000).decode("utf-8", errors="replace")
            raise RuntimeError(f"Nemotron returned HTTP {exc.code}: {detail[:500]}") from exc
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Nemotron request failed: {type(exc).__name__}") from exc
        content = result["choices"][0]["message"]["content"]
        raw = content if isinstance(content, str) else json.dumps(content)
        self.last_raw_response = raw
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            decoder = json.JSONDecoder()

            def recover_field(name: str):
                matches = list(re.finditer(rf'"{re.escape(name)}"\s*:\s*', raw))
                if not matches:
                    raise ValueError(f"Nemotron response omitted the {name} field")
                match = matches[-1] if name == "candidates" else matches[0]
                try:
                    return decoder.raw_decode(raw, match.end())[0]
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Nemotron response has an unreadable {name} field") from exc

            objective = recover_field("objective")
            candidates = recover_field("candidates")
            try:
                rationale = recover_field("rationale")
            except ValueError:
                rationale = "Rationale text was malformed; candidate fields were recovered from the raw response."
            parsed = {"objective": objective, "rationale": rationale, "candidates": candidates}
        candidates = []
        seen = set()
        for item in parsed["candidates"]:
            cid = item["candidate_id"]
            if cid == "replan" and item["kind"] != "replan":
                # Keep the adapter-owned stop option distinct from a planner choice.
                cid = "planner_replan_choice"
            if cid in seen or item["kind"] == "replan":
                raise ValueError("proposal contains duplicate IDs or an adapter-reserved replan option")
            seen.add(cid)
            candidates.append(Candidate(cid, item["description"], item["kind"], item["payload"]))
        candidates.append(Candidate("replan", "Stop and return control to the planner", "replan", {}))
        proposal = Proposal(parsed["objective"], parsed["rationale"], tuple(candidates), raw, time.monotonic() - started)
        validate_proposal(proposal)
        return proposal
