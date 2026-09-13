from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .agentdocs import (
    GuidanceResult,
    inspect_agent_guidance,
    remove_agent_guidance,
    sync_agent_guidance,
)
from .constants import RECIPE_NAME
from .recipe import Recipe
from .registry import Registry


def _mutate(cwd: Path, action: str, replace: bool) -> tuple[GuidanceResult, ...]:
    recipe = None
    try:
        recipe = Recipe.load(cwd / RECIPE_NAME)
    except (OSError, ValueError) as error:
        if action == "update":
            return (GuidanceResult(RECIPE_NAME, "failed", detail=str(error)),)
    if action == "uninstall":
        return remove_agent_guidance(cwd, recipe, announce=False)
    if recipe is None:
        return (GuidanceResult(RECIPE_NAME, "failed", detail="recipe required for update"),)
    return sync_agent_guidance(cwd, recipe, replace=replace, announce=False)


def cmd_ai(cwd: Path, action: str, fmt: str = "text", *, replace: bool = False) -> int:
    if action == "status":
        results = inspect_agent_guidance(cwd)
    else:
        with Registry().operation_lock(str(cwd)):
            results = _mutate(cwd, action, replace)
    activation = "Agent sessions may need to reload instruction files."
    if fmt == "json":
        print(
            json.dumps(
                {
                    "action": action,
                    "files": [asdict(result) for result in results],
                    "activation": activation,
                }
            )
        )
    else:
        for result in results:
            detail = f"; {result.detail}" if result.detail else ""
            print(f"{result.file}: {result.status}{detail}")
        print(activation)
    return 0 if all(result.successful for result in results) else 1
