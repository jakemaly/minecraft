"""Validated, secret-free run configuration."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RunConfig:
    profile: str = "molab-local"
    minecraft_version: str = "1.21.4"
    mineflayer_version: str = "4.39.0"
    node_major: int = 22
    java_major: int = 21
    planner_model: str = "nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4"
    selector_model: str = "Cloudflare/clef-flash"
    planner_revision: str = "main"
    selector_revision: str = "main"
    planner_url: str = "http://127.0.0.1:8000/v1"
    max_observation_bytes: int = 32_000
    max_rpc_bytes: int = 1_000_000
    action_timeout_seconds: int = 30
    helper_timeout_seconds: int = 10
    viewer_enabled: bool = False

    @classmethod
    def from_environment(cls) -> "RunConfig":
        defaults = cls()
        return cls(
            profile=os.getenv("MINECRAFT_AGENT_PROFILE", defaults.profile),
            minecraft_version=os.getenv("MINECRAFT_VERSION", defaults.minecraft_version),
            mineflayer_version=os.getenv("MINEFLAYER_VERSION", defaults.mineflayer_version),
            node_major=int(os.getenv("NODE_MAJOR", defaults.node_major)),
            java_major=int(os.getenv("JAVA_MAJOR", defaults.java_major)),
            planner_model=os.getenv("PLANNER_MODEL", defaults.planner_model),
            selector_model=os.getenv("SELECTOR_MODEL", defaults.selector_model),
            planner_revision=os.getenv("PLANNER_REVISION", defaults.planner_revision),
            selector_revision=os.getenv("SELECTOR_REVISION", defaults.selector_revision),
            planner_url=os.getenv("PLANNER_URL", defaults.planner_url),
            max_observation_bytes=int(os.getenv("MAX_OBSERVATION_BYTES", defaults.max_observation_bytes)),
            max_rpc_bytes=int(os.getenv("MAX_RPC_BYTES", defaults.max_rpc_bytes)),
            action_timeout_seconds=int(os.getenv("ACTION_TIMEOUT_SECONDS", defaults.action_timeout_seconds)),
            helper_timeout_seconds=int(os.getenv("HELPER_TIMEOUT_SECONDS", defaults.helper_timeout_seconds)),
            viewer_enabled=os.getenv("VIEWER_ENABLED", "false").lower() == "true",
        )

    def validate(self) -> None:
        if self.profile not in {"molab-local", "mac-api"}:
            raise ValueError("profile must be 'molab-local' or 'mac-api'")
        for name in ("max_observation_bytes", "max_rpc_bytes", "action_timeout_seconds", "helper_timeout_seconds"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_observation_bytes > self.max_rpc_bytes:
            raise ValueError("max_observation_bytes cannot exceed max_rpc_bytes")
        if self.planner_url.startswith(("http://0.0.0.0", "https://0.0.0.0")):
            raise ValueError("planner endpoint must not bind to a public interface")

    def public_dict(self) -> dict[str, Any]:
        """Return only reproducibility settings; credentials are never config fields."""
        return asdict(self)


def runtime_root() -> Path:
    return Path(os.getenv("MINECRAFT_AGENT_HOME", Path.home() / ".local/share/minecraft-agent")).expanduser().resolve()
