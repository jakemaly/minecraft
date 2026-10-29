"""Kernel-local objects for marimo; no process is reattached by PID alone."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class NotebookSession:
    run_id: str | None = None
    supervisor: Any = None
    bridge: Any = None
    planner: Any = None
    selector: Any = None
    agent_loop: Any = None
    agent_thread: threading.Thread | None = None
    events: Any = None
    consumed_buttons: dict[str, int | float] = field(default_factory=dict)
    reports: dict[str, Any] = field(default_factory=dict)
    current_image_jpeg: bytes | None = None
    current_image_meta: dict[str, Any] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def claim_button(self, name: str, value: int | bool | None) -> bool:
        """Consume each run-button event once across reactive reruns."""
        if isinstance(value, bool):
            if not value:
                return False
            with self.lock:
                now = time.monotonic()
                previous = float(self.consumed_buttons.get(name, 0))
                if now - previous < 1.0:
                    return False
                self.consumed_buttons[name] = now
                return True
        counter = int(value or 0)
        with self.lock:
            previous = self.consumed_buttons.get(name, 0)
            if counter <= previous:
                return False
            self.consumed_buttons[name] = counter
            return True


_SESSION = NotebookSession()


def get_session() -> NotebookSession:
    return _SESSION
