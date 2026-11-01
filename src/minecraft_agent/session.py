"""Kernel-local state for the optional vLLM notebook."""

from __future__ import annotations

import threading
import time


class NotebookSession:
    def __init__(self):
        self.vllm_supervisor = None
        self.molab_runtime = None
        self.reports: dict[str, dict] = {}
        self._clicks: dict[str, float | int] = {}
        self._lock = threading.Lock()

    def claim_button(self, name: str, value: int | bool | None) -> bool:
        if isinstance(value, bool):
            if not value:
                return False
            with self._lock:
                now = time.monotonic()
                if now - float(self._clicks.get(name, 0)) < 1:
                    return False
                self._clicks[name] = now
                return True
        with self._lock:
            count = int(value or 0)
            previous = int(self._clicks.get(name, 0))
            if count <= previous:
                return False
            self._clicks[name] = count
            return True


_SESSION = NotebookSession()


def get_session() -> NotebookSession:
    return _SESSION
