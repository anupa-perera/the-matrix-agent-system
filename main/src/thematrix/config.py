from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field


class MatrixPaths(BaseModel):
    """Filesystem locations owned by The Matrix."""

    home: Path = Field(default_factory=lambda: default_home())
    vault: Path = Field(default_factory=lambda: default_vault())

    @property
    def runtime_db(self) -> Path:
        return self.home / "runtime.sqlite"

    @property
    def prompts_dir(self) -> Path:
        return self.home / "prompts"

    @property
    def agents_dir(self) -> Path:
        return self.home / "agents"


def default_home() -> Path:
    configured = os.getenv("THE_MATRIX_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".thematrix"


def default_vault() -> Path:
    configured = os.getenv("THE_MATRIX_VAULT")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Documents" / "The Matrix Vault"


def ensure_runtime_dirs(paths: MatrixPaths) -> None:
    paths.home.mkdir(parents=True, exist_ok=True)
    paths.prompts_dir.mkdir(parents=True, exist_ok=True)
    paths.agents_dir.mkdir(parents=True, exist_ok=True)

