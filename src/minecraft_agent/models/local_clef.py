"""Adapter scaffold around Cloudflare's released Clef joint-schema head.

The checkpoint's Python API is an upstream private release API. Resolve and
validate it at runtime; fail closed if symbols or tensor shapes differ.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .interfaces import Candidate, validate_probabilities


class ClefReleaseAdapter:
    def __init__(self, snapshot: str | Path, *, device: str = "cuda"):
        self.snapshot = Path(snapshot).resolve()
        helper = self.snapshot / "joint_schema_model.py"
        if not helper.is_file() or not (self.snapshot / "joint_head.safetensors").is_file():
            raise RuntimeError("Clef snapshot is missing the official joint_schema_model.py or joint_head.safetensors")
        spec = importlib.util.spec_from_file_location("clef_release_joint_schema_model", helper)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load Clef reference helper")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        for name in ("load_release_model", "encode_record", "collate_records"):
            if not hasattr(module, name):
                raise RuntimeError(f"Clef reference release is missing {name}")
        self.module = module
        # Clef's released AutoProcessor metadata is not recognized by the pinned
        # Transformers build; text-only choices need its exact tokenizer and head.
        import torch
        from safetensors.torch import load_file
        from transformers import AutoTokenizer, Qwen3_5ForConditionalGeneration

        backbone = Qwen3_5ForConditionalGeneration.from_pretrained(
            self.snapshot,
            dtype=torch.bfloat16,
            device_map={"": device},
        )
        backbone.config.use_cache = False
        head_config = json.loads((self.snapshot / "joint_head_config.json").read_text(encoding="utf-8"))
        head = module.JointSchemaHead(**head_config)
        head.load_state_dict(load_file(self.snapshot / "joint_head.safetensors"), strict=True)
        head = head.to(device=device, dtype=torch.bfloat16)
        self.model = module.ClefModel(backbone, head).eval()
        self.processor = SimpleNamespace(tokenizer=AutoTokenizer.from_pretrained(self.snapshot))
        self.model.eval()
        self.device = device

    def choose(self, state: dict[str, Any], objective: str, candidates: list[Candidate]) -> dict[str, float]:
        started = time.monotonic()
        if not candidates:
            raise ValueError("selector needs at least one candidate")
        question = {"type": "choice", "instructions": f"Choose an available action for the objective: {objective}", "criteria": {item.candidate_id: item.description for item in candidates}}
        record = {"state": state, "questions": {"next_action": question}}
        # Fail clearly if the upstream encoding/head contract has changed; never fabricate typed scores.
        encoded = self.module.encode_record(self.processor.tokenizer, record, processor=self.processor)
        import torch
        batch = self.module.collate_records([encoded], self.processor.tokenizer.pad_token_id, torch.device(self.device))
        with torch.inference_mode():
            logits = self.model(batch)[0]
        if len(logits) != 1 or len(encoded.questions) != 1:
            raise RuntimeError("Clef returned an unexpected number of typed questions")
        option_ids = list(encoded.questions[0].option_ids)
        if len(option_ids) != len(candidates) or set(option_ids) != {candidate.candidate_id for candidate in candidates}:
            raise RuntimeError("Clef's encoded option mapping differs from the offered candidate IDs")
        probabilities = torch.softmax(logits[0].float(), dim=-1).detach().cpu().tolist()
        scores = {candidate_id: float(value) for candidate_id, value in zip(option_ids, probabilities, strict=True)}
        validate_probabilities(scores, [candidate.candidate_id for candidate in candidates], tolerance=1e-4)
        input_ids = batch.get("input_ids") if isinstance(batch, dict) else None
        self.last_metrics = {
            "status": "ok",
            "input_tokens": int(input_ids.numel()) if input_ids is not None and hasattr(input_ids, "numel") else None,
            "output_tokens": 0,
            "latency_seconds": time.monotonic() - started,
            "ttft_seconds": None,
            "output_tokens_per_second": None,
            "reasoning_trace": "not_exposed_by_typed_choice_head",
            "final_answer": "not_applicable_typed_choice_scores",
            "kv_cache": "not_used_by_selector",
            "cost_usd": None,
            "cost_status": "unavailable_local_inference",
        }
        return scores


class LocalClefClient:
    """Loopback-only client for the separately loaded Clef model process."""

    def __init__(self, base_url: str = "http://127.0.0.1:8765", timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def choose(self, state: dict[str, Any], objective: str, candidates: list[Candidate]) -> dict[str, float]:
        import json
        import urllib.error
        import urllib.request
        from dataclasses import asdict
        request_data = {"state": state, "objective": objective, "candidates": [asdict(item) for item in candidates]}
        started = time.monotonic()
        request = urllib.request.Request(self.base_url + "/choose", data=json.dumps(request_data, allow_nan=False).encode(), headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read(1_000_000))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Clef local service failed: {type(exc).__name__}") from exc
        scores = body.get("scores")
        validate_probabilities(scores, [item.candidate_id for item in candidates])
        self.last_latency_seconds = time.monotonic() - started
        remote_metrics = body.get("metrics")
        self.last_metrics = {
            **(remote_metrics if isinstance(remote_metrics, dict) else {}),
            "status": "ok" if isinstance(remote_metrics, dict) else "unavailable",
            "request_latency_seconds": self.last_latency_seconds,
            "cost_usd": None,
            "cost_status": "unavailable_local_inference",
        }
        return scores


def check_typed_scores(scores: dict[str, float], candidates: list[Candidate]) -> None:
    validate_probabilities(scores, [candidate.candidate_id for candidate in candidates])
    if any(not math.isfinite(value) for value in scores.values()):
        raise ValueError("Clef emitted a non-finite score")
