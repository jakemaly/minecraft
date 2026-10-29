"""OpenAI-compatible adapter for a separately supervised local vLLM server."""

from __future__ import annotations

import json
import re
import time
import base64
import hashlib
import urllib.error
import urllib.request
import uuid
from typing import Any

from .interfaces import Candidate, Proposal, validate_proposal


DEFAULT_MAX_COMPLETION_TOKENS = 8_192


def _partial_action_object(raw: str) -> dict[str, Any]:
    """Recover complete top-level fields when a length-limited reply cuts off its wrapper."""
    decoder = json.JSONDecoder()
    position = 0

    def skip_space(index: int) -> int:
        while index < len(raw) and raw[index].isspace():
            index += 1
        return index

    position = skip_space(position)
    if position >= len(raw) or raw[position] != "{":
        return {}
    position += 1
    fields: dict[str, Any] = {}
    while True:
        position = skip_space(position)
        if position >= len(raw) or raw[position] == "}":
            break
        try:
            key, position = decoder.raw_decode(raw, position)
        except json.JSONDecodeError:
            break
        if not isinstance(key, str) or key in fields:
            return {}
        position = skip_space(position)
        if position >= len(raw) or raw[position] != ":":
            break
        position = skip_space(position + 1)
        try:
            value, position = decoder.raw_decode(raw, position)
        except json.JSONDecodeError:
            break
        fields[key] = value
        position = skip_space(position)
        if position < len(raw) and raw[position] == ",":
            position += 1
            continue
        if position < len(raw) and raw[position] == "}":
            break
        break

    if fields.get("kind") not in {"act", "choose"} or not isinstance(fields.get("candidates"), list):
        return {}
    if not fields["candidates"] or not all(isinstance(item, dict) for item in fields["candidates"]):
        return {}
    return fields


class LocalNemotron:
    def __init__(self, base_url: str, model: str, timeout: int = 180):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.last_raw_response = ""
        self.last_turn: dict[str, Any] = {}

    def generate_turn(
        self,
        *,
        system_prompt: str,
        context: dict[str, Any],
        image_jpeg: bytes | None,
        response_schema: dict[str, Any],
        max_tokens: int = DEFAULT_MAX_COMPLETION_TOKENS,
    ) -> dict[str, Any]:
        """Stream one multimodal decision and retain exposed reasoning plus usage."""
        self.last_turn = {}
        self.last_raw_response = ""
        if not isinstance(image_jpeg, bytes) or not image_jpeg.startswith(b"\xff\xd8") or not image_jpeg.endswith(b"\xff\xd9"):
            raise ValueError("each Nemotron turn requires exactly one current JPEG image")
        image_hash = hashlib.sha256(image_jpeg).hexdigest()
        content: list[dict[str, Any]] = [
            {"type": "text", "text": json.dumps(context, ensure_ascii=False, separators=(",", ":"))},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(image_jpeg).decode("ascii"), "detail": "low"}},
        ]
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": content}],
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
            "chat_template_kwargs": {"enable_thinking": True},
            "repetition_detection": {
                "min_pattern_size": 1,
                "max_pattern_size": 4,
                "min_count": 32,
            },
            "response_format": {"type": "json_schema", "json_schema": {"name": "agent_turn", "strict": True, "schema": response_schema}},
        }
        request_id = str(uuid.uuid4())
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Request-ID": request_id},
            method="POST",
        )
        started = time.monotonic()
        first_token = None
        answer_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage: dict[str, Any] = {}
        finish_reason = None
        response_id = None
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                while line := response.readline():
                    if not line.startswith(b"data:"):
                        continue
                    raw_line = line[5:].strip()
                    if not raw_line or raw_line == b"[DONE]":
                        continue
                    chunk = json.loads(raw_line)
                    response_id = chunk.get("id", response_id)
                    if isinstance(chunk.get("usage"), dict):
                        usage.update(chunk["usage"])
                    for choice in chunk.get("choices", []):
                        if choice.get("finish_reason") is not None:
                            finish_reason = choice["finish_reason"]
                        delta = choice.get("delta", {})
                        text = delta.get("content")
                        reasoning = delta.get("reasoning") or delta.get("reasoning_content")
                        if isinstance(reasoning, str) and reasoning:
                            reasoning_parts.append(reasoning)
                            if first_token is None:
                                first_token = time.monotonic() - started
                        if isinstance(text, str) and text:
                            answer_parts.append(text)
                            if first_token is None:
                                first_token = time.monotonic() - started
        except urllib.error.HTTPError as exc:
            detail = exc.read(1_000).decode("utf-8", errors="replace")
            raise RuntimeError(f"Nemotron returned HTTP {exc.code}: {detail[:500]}") from exc
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Nemotron streaming request failed: {type(exc).__name__}") from exc

        elapsed = time.monotonic() - started
        raw_answer = "".join(answer_parts)
        reasoning = "".join(reasoning_parts)
        self.last_raw_response = raw_answer
        completion_tokens = usage.get("completion_tokens")
        finish_reason_source = "server" if finish_reason is not None else "unavailable"
        if finish_reason is None and isinstance(completion_tokens, (int, float)) and completion_tokens >= max_tokens:
            finish_reason = "max_tokens"
            finish_reason_source = "completion_token_limit"
        generation_seconds = elapsed - first_token if first_token is not None else None
        metrics = {
            "request_id": request_id,
            "response_id": response_id,
            "finish_reason": finish_reason,
            "finish_reason_source": finish_reason_source,
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": completion_tokens,
            "cached_input_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
            "ttft_seconds": first_token,
            "elapsed_seconds": elapsed,
            "output_tokens_per_second": completion_tokens / generation_seconds if isinstance(completion_tokens, (int, float)) and generation_seconds and generation_seconds > 0 else None,
            "generation_seconds": generation_seconds,
            "usage": usage or None,
            "image_sha256": image_hash,
            "image_bytes": len(image_jpeg) if image_jpeg else 0,
            "cost_usd": None,
            "cost_status": "unavailable_local_inference",
        }
        recovery = None
        try:
            parsed = json.loads(raw_answer)
        except json.JSONDecodeError as exc:
            stopped_for_repetition = finish_reason in {"repetition", "repetition_detection"}
            hit_token_limit = finish_reason in {"length", "max_tokens"} or (
                isinstance(completion_tokens, (int, float)) and completion_tokens >= max_tokens
            )
            recovered = _partial_action_object(raw_answer) if hit_token_limit or stopped_for_repetition else {}
            if not recovered:
                self.last_turn = {"parsed": None, "reasoning": reasoning, "raw_answer": raw_answer, "metrics": metrics}
                raise ValueError(f"Nemotron returned malformed structured output at character {exc.pos}") from exc
            missing_fields = [key for key in ("rationale", "answer") if key not in recovered]
            recovered.setdefault("rationale", "")
            recovered.setdefault("answer", "")
            parsed = recovered
            recovery = {
                "status": "recovered_complete_action",
                "stop_reason": finish_reason,
                "missing_fields": missing_fields,
            }
            metrics["structured_output_recovered"] = True
        if recovery is None:
            metrics["structured_output_recovered"] = False
        turn = {"parsed": parsed, "reasoning": reasoning, "raw_answer": raw_answer, "metrics": metrics, "recovery": recovery}
        self.last_turn = turn
        return turn

    def metrics_snapshot(self) -> dict[str, Any]:
        """Read local vLLM Prometheus counters without exposing other endpoints."""
        metrics_url = self.base_url.removesuffix("/v1") + "/metrics"
        request = urllib.request.Request(metrics_url, headers={"Accept": "text/plain"})
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                text = response.read(2_000_000).decode("utf-8", errors="replace")
        except Exception as exc:
            return {"status": "unavailable", "error_type": type(exc).__name__}
        counters: dict[str, float] = {}
        for line in text.splitlines():
            if not line or line.startswith("#"):
                continue
            name, _, raw_value = line.rpartition(" ")
            if not name.startswith(("vllm:prefix_cache_", "vllm:external_prefix_cache_", "vllm:gpu_prefix_cache_", "vllm:kv_cache_", "vllm:gpu_cache_")):
                continue
            try:
                counters[name] = float(raw_value)
            except ValueError:
                continue
        return {"status": "ok", "counters": counters, "sampled_at_monotonic": time.monotonic()}

    @staticmethod
    def bound_observation(observation: dict[str, Any], max_bytes: int = 2_500) -> dict[str, Any]:
        """Trim the largest variable-length fields to fit the pinned context window."""
        bounded = dict(observation)
        # Event history is persisted separately; current state is enough for each action.
        bounded["recent_events"] = []
        original_counts = {
            key: len(value)
            for key, value in bounded.items()
            if key in {"nearby_blocks", "nearby_logs", "nearby_entities", "inventory", "recent_events"} and isinstance(value, list)
        }

        def size() -> int:
            return len(json.dumps(bounded, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

        for key in ("nearby_blocks", "nearby_entities", "recent_events", "inventory", "nearby_logs"):
            value = bounded.get(key)
            if not isinstance(value, list):
                continue
            while value and size() > max_bytes:
                value = value[: len(value) // 2]
                bounded[key] = value
        if size() > max_bytes:
            for key in ("nearby_blocks", "nearby_entities", "recent_events", "inventory", "nearby_logs"):
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

    @staticmethod
    def exact_helper_sources(objective: str) -> list[str]:
        """Read explicit source allowlists while retaining the original one-source prompt form."""
        marked = re.findall(r"<HELPER_SOURCE>(.*?)</HELPER_SOURCE>", objective, flags=re.DOTALL)
        if marked:
            sources = [source.strip() for source in marked if source.strip()]
        else:
            match = re.search(r"await\s+api\.act\(\{[^}]{0,200}\}\);", objective)
            sources = [match.group(0)] if match else []
        return list(dict.fromkeys(sources))

    def propose(self, observation: dict[str, Any], objective: str, memory: str = "") -> Proposal:
        observation = self.bound_observation(observation)
        helper_requested = "helper" in objective.casefold()
        helper_sources = self.exact_helper_sources(objective) if helper_requested else []
        source_schema: dict[str, Any] = {"type": "string", "minLength": 16, "maxLength": 32000}
        if helper_sources:
            source_schema = {"type": "string", "enum": helper_sources}
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
                "description": {"type": "string", "minLength": 1, "maxLength": 120},
                "kind": {"enum": ["helper"] if helper_requested else ["primitive", "helper"]},
                "payload": payload_schema,
            },
        }
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["objective", "rationale", "candidates"],
            "properties": {
                "objective": {"type": "string", "maxLength": 300},
                "rationale": {"type": "string", "maxLength": 160},
                "candidates": {"type": "array", "minItems": 1 if helper_requested else 0, "maxItems": 1 if helper_requested else 8, "items": candidate_schema},
            },
        }
        instructions = "Propose bounded actions from the supplied state only. Use short IDs and descriptions. Do not assume hidden world state. Return JSON matching the schema."
        if helper_requested:
            instructions += " Return exactly one helper candidate and copy its offered source exactly. Keep objective and rationale short."
        instructions += " Helper source must use only api.act/api.observe; never use placeholders."
        prompt = {"objective": objective, "memory": memory[-1200:], "observation": observation, "instructions": instructions}
        payload = {"model": self.model, "messages": [{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}], "temperature": 0.2, "max_tokens": 256 if helper_requested else 512, "chat_template_kwargs": {"enable_thinking": False}, "response_format": {"type": "json_schema", "json_schema": {"name": "agent_proposal", "strict": True, "schema": schema}}}
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
