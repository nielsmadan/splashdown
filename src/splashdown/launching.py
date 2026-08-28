from __future__ import annotations

from pathlib import Path

from .catalog import PROFILES
from .constants import RECIPE_NAME
from .device_types import DestinationLike, as_launch_destination
from .errors import DeviceError
from .inventory import RunnableProfile
from .recipe import Recipe
from .runners import _resolve_custom_run, run_custom_command


def detect_framework(cwd: Path, recipe: Recipe) -> str:
    override = recipe.project.get("framework")
    if override and override != "auto":
        return str(override)
    for name, profile in PROFILES.items():
        if profile.detect(cwd):
            return name
    declared = {
        name: str(spec["profile"])
        for name, spec in recipe.apps.items()
        if spec.get("profile") != "unknown"
    }
    if len(declared) == 1:
        return next(iter(declared.values()))
    if declared:
        listed = ", ".join(f"`{name}` → `{profile}`" for name, profile in sorted(declared.items()))
        raise DeviceError(
            f"ambiguous project framework; apps declare {listed} — "
            f"set `[project] framework` in {RECIPE_NAME}"
        )
    raise DeviceError(
        "could not detect project framework; set `[project] framework = "
        + "|".join(f'"{name}"' for name in PROFILES)
        + f"` in {RECIPE_NAME}"
    )


def resolve_app_dir(cwd: Path, recipe: Recipe, framework: str) -> Path:
    if (
        framework == "android-native"
        and recipe.project.get("workspace") == "gradle"
        and (recipe.project.get("android") or {}).get("module")
    ):
        return cwd
    profile = PROFILES.get(framework)
    if profile is not None and profile.detect(cwd):
        return cwd
    matches = [
        str(spec["path"]) for spec in recipe.apps.values() if spec.get("profile") == framework
    ]
    if len(matches) == 1:
        candidate = cwd / matches[0]
        if candidate.is_dir():
            return candidate
    return cwd


def validate_device_run(cwd: Path, recipe: Recipe, kind: str | None) -> None:
    if kind is not None and _resolve_custom_run(recipe, kind) is not None:
        return
    if kind is None and recipe.project.get("run"):
        return
    framework = detect_framework(cwd, recipe)
    profile = PROFILES.get(framework)
    if not isinstance(profile, RunnableProfile):
        raise DeviceError(f"framework `{framework}` does not support `splash run`")


def device_run(cwd: Path, recipe: Recipe, destination: DestinationLike) -> int:
    destination = as_launch_destination(destination)
    rc = run_custom_command(cwd, recipe, destination)
    if rc is not None:
        return rc
    framework = detect_framework(cwd, recipe)
    profile = PROFILES.get(framework)
    if not isinstance(profile, RunnableProfile):
        raise DeviceError(f"framework `{framework}` does not support `splash run`")
    return int(profile.run(resolve_app_dir(cwd, recipe, framework), recipe, destination))
