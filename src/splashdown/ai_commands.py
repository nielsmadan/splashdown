from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .agentdocs import (
    GuidanceResult,
    inspect_agent_guidance,
    remove_agent_guidance,
    sync_agent_guidance,
)
from .constants import RECIPE_NAME
from .errors import ApplicationError
from .recipe import Recipe
from .registry import Registry
from .results import CommandResult, Diagnostic, JsonObject


def _mutate(
    cwd: Path, action: str, replace: bool, on_result: Callable[[GuidanceResult], None]
) -> tuple[GuidanceResult, ...]:
    recipe = None
    try:
        recipe = Recipe.load(cwd / RECIPE_NAME)
    except (OSError, ValueError) as error:
        if action == "update":
            code = (
                "recipe_missing"
                if isinstance(error, FileNotFoundError)
                else "io_error"
                if isinstance(error, OSError)
                else "invalid_configuration"
            )
            raise ApplicationError(str(error), code=code) from error
    if action == "uninstall":
        return remove_agent_guidance(cwd, recipe, announce=False, on_result=on_result)
    if recipe is None:
        raise ApplicationError("Recipe required for update.", code="recipe_missing")
    return sync_agent_guidance(cwd, recipe, replace=replace, announce=False, on_result=on_result)


def _data(action: str, results: tuple[GuidanceResult, ...]) -> JsonObject:
    return {
        "action": action,
        "files": [
            {
                "file": row.file,
                "status": row.status,
                "content_current": row.content_current,
                "detail": row.detail,
                "desired_current": row.desired_current,
            }
            for row in results
        ],
        "activation": "Agent sessions may need to reload instruction files.",
    }


def _warnings(results: tuple[GuidanceResult, ...]) -> tuple[Diagnostic, ...]:
    return tuple(
        Diagnostic("guidance_generated", row.detail)
        for row in results
        if row.status == "generated" and row.detail
    )


def cmd_ai(cwd: Path, action: str, *, replace: bool = False) -> CommandResult:
    completed: list[GuidanceResult] = []
    try:
        if action == "status":
            results = inspect_agent_guidance(cwd, on_result=completed.append)
        else:
            with Registry().operation_lock(str(cwd)):
                results = _mutate(cwd, action, replace, completed.append)
    except KeyboardInterrupt as error:
        raise ApplicationError(
            "Guidance operation interrupted.",
            code="interrupted",
            exit_code=130,
            data=_data(action, tuple(completed)) if completed else None,
            partial=bool(completed),
            warnings=_warnings(tuple(completed)),
            next_steps=("Run `splash ai status` before retrying.",),
        ) from error
    except OSError as error:
        raise ApplicationError(
            str(error),
            code="io_error",
            data=_data(action, tuple(completed)) if completed else None,
            partial=bool(completed),
            warnings=_warnings(tuple(completed)),
            next_steps=("Run `splash ai status` before retrying.",),
        ) from error
    failed = [row for row in results if not row.successful]
    partial = any(row.successful or row.status in {"partial", "incomplete"} for row in results)
    return CommandResult(
        command=f"ai {action}",
        status="partial" if failed and partial else "error" if failed else "success",
        exit_code=1 if failed else 0,
        data=_data(action, results),
        warnings=_warnings(results),
        error=Diagnostic(
            "guidance_failed",
            f"Guidance {action} did not complete for "
            + ", ".join(row.file for row in failed)
            + ".",
        )
        if failed
        else None,
        next_steps=(f"Resolve the reported file problem, then run `splash ai {action}` again.",)
        if failed
        else (),
    )
