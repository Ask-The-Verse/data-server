"""Environment-backed service configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    data_root: Path
    log_level: str
    request_timeout: int = 120

    @classmethod
    def from_env(cls) -> "Settings":
        configured_root = os.getenv("DATA_ROOT")
        data_root = (
            Path(configured_root).expanduser()
            if configured_root
            else PROJECT_ROOT / "data"
        )
        return cls(
            data_root=data_root.resolve(),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
