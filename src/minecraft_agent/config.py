"""Minecraft version and persistent runtime location."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunConfig:
    minecraft_version: str = "1.21.4"

    @classmethod
    def from_environment(cls) -> "RunConfig":
        return cls(minecraft_version=os.getenv("MINECRAFT_VERSION", "1.21.4"))

    def validate(self) -> None:
        if not self.minecraft_version or any(char.isspace() for char in self.minecraft_version):
            raise ValueError("MINECRAFT_VERSION must be a non-empty version identifier")

    def public_dict(self) -> dict[str, str]:
        return asdict(self)


def runtime_root() -> Path:
    return Path(os.getenv("MINECRAFT_AGENT_HOME", Path.home() / ".local/share/minecraft-agent")).expanduser().resolve()
