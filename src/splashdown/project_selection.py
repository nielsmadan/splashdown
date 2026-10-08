from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from .constants import RECIPE_NAME
from .errors import ApplicationError


@dataclass(frozen=True)
class ProjectSelection:
    start: Path
    directory: Path
    worktree_root: Path | None
    recipe_path: Path | None


def resolve_start_directory(
    value: str | Path | None, *, known_checkouts: Collection[str] = ()
) -> Path:
    try:
        path = Path.cwd() if value is None else Path(value)
        try:
            path = path.resolve(strict=True)
        except FileNotFoundError:
            canonical = path.resolve(strict=False)
            if value is not None and str(canonical) in known_checkouts:
                return canonical
            raise
        if not stat.S_ISDIR(path.stat().st_mode):
            raise NotADirectoryError(f"not a directory: {path}")
        if not os.access(path, os.R_OK | os.X_OK):
            raise PermissionError(f"directory is not readable and searchable: {path}")
        with os.scandir(path):
            pass
        return path
    except (OSError, RuntimeError, ValueError) as error:
        raise ApplicationError(
            f"Invalid starting directory {value!s}: {error}", code="invalid_directory"
        ) from error


def _worktree_root(start: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as error:
        raise ApplicationError(
            f"Unable to discover Git worktree: {error}", code="io_error"
        ) from error
    if result.returncode:
        message = result.stderr.strip()
        if (
            message.startswith(
                (
                    "fatal: not a git repository (or any of the parent directories):",
                    "fatal: not a git repository (or any parent up to mount point ",
                )
            )
            or message == "fatal: this operation must be run in a work tree"
        ):
            return None
        raise ApplicationError(f"Unable to discover Git worktree: {message}", code="io_error")
    root = Path(result.stdout.rstrip("\n")).resolve(strict=True)
    if not result.stdout.strip() or not start.is_relative_to(root):
        raise ApplicationError("Git returned an invalid worktree root", code="io_error")
    return root


def _recipe_entry(directory: Path) -> Path | None:
    path = directory / RECIPE_NAME
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    return path


def select_project(
    start: Path, *, exact: bool = False, known_checkouts: Collection[str] = ()
) -> ProjectSelection:
    if str(start) in known_checkouts:
        try:
            start.stat()
        except FileNotFoundError:
            return ProjectSelection(start, start, None, None)
    root = None if exact else _worktree_root(start)
    directory = start
    while True:
        recipe = _recipe_entry(directory)
        if recipe is not None or str(directory) in known_checkouts:
            return ProjectSelection(start, directory, root, recipe)
        if root is None or directory == root:
            return ProjectSelection(start, start, root, None)
        directory = directory.parent
