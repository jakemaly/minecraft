"""Minecraft server configuration and private runtime paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunConfig:
    minecraft_version: str = "1.21.4"

    @classmethod
    def from_environment(cls) -> "RunConfig":
        return cls(minecraft_version=os.getenv("MINECRAFT_VERSION", "1.21.4"))

    def validate(self) -> None:
        if not self.minecraft_version.strip():
            raise ValueError("minecraft_version must not be empty")


def runtime_root() -> Path:
    return Path(os.getenv("MINECRAFT_AGENT_HOME", Path.home() / ".local/share/minecraft-agent")).expanduser().resolve()
