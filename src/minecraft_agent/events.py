"""Append-only JSONL event writer with secret-key redaction."""

from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SECRET_PARTS = ("token", "secret", "password", "api_key", "authorization", "credential")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if any(part in key.lower() for part in _SECRET_PARTS) else redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class EventLog:
    def __init__(self, path: str | Path, run_id: str, world_id: str, *, elapsed_offset: float | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.world_id = world_id
        self._lock = threading.Lock()
        self._sequence, self._last_elapsed = self._last_event()
        self._elapsed_offset = self._last_elapsed
        if elapsed_offset is not None:
            if not math.isfinite(elapsed_offset) or elapsed_offset < self._last_elapsed:
                raise ValueError("elapsed-time checkpoint marker is invalid or precedes the event log")
            self._elapsed_offset = elapsed_offset
        self._started = time.monotonic()

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def elapsed_seconds(self) -> float:
        return self._elapsed_offset + time.monotonic() - self._started

    def _last_event(self) -> tuple[int, float]:
        if not self.path.exists():
            return 0, 0.0
        last = 0
        elapsed = 0.0
        with self.path.open("rb") as handle:
            for line in handle:
                if line.strip():
                    event = json.loads(line)
                    sequence = int(event.get("sequence", 0))
                    if sequence != last + 1:
                        raise ValueError(f"event sequence is discontinuous at {sequence}; expected {last + 1}")
                    if event.get("run_id") != self.run_id or event.get("world_id") != self.world_id:
                        raise ValueError("event log belongs to a different run or world")
                    last = sequence
                    elapsed = float(event.get("elapsed_seconds", 0.0))
        return last, elapsed

    def append(self, event_type: str, payload: dict[str, Any], *, causal_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._sequence += 1
            elapsed = self._elapsed_offset + time.monotonic() - self._started
            event = {"schema_version": 1, "run_id": self.run_id, "world_id": self.world_id, "sequence": self._sequence, "wall_time": datetime.now(timezone.utc).isoformat(), "elapsed_seconds": round(elapsed, 6), "type": event_type, "causal_id": causal_id, "payload": redact(payload)}
            line = json.dumps(event, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return event
