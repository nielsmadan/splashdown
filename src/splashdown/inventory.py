from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .recipe import Recipe


@dataclass
class AppInventory:
    name: str
    path: Path
    profile: str
    project_path: Path | None = None
    capabilities: tuple[str, ...] = ()


@dataclass
class ProjectInventory:
    workspace: str
    apps: list[AppInventory]
    loader: str


@runtime_checkable
class RunnableProfile(Protocol):
    def run(self, cwd: Path, recipe: Recipe, info: dict[str, str]) -> int: ...
