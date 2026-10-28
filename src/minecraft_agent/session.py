"""Kernel-local objects for marimo; no process is reattached by PID alone."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class NotebookSession:
    run_id: str | None = None
    supervisor: Any = None
    bridge: Any = None
    planner: Any = None
    selector: Any = None
    events: Any = None
    consumed_buttons: dict[str, int] = field(default_factory=dict)
    reports: dict[str, Any] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def claim_button(self, name: str, value: int | bool | None) -> bool:
        """Consume each run-button event once across reactive reruns."""
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
