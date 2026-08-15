from __future__ import annotations

import sys
from pathlib import Path

from .catalog import PROFILES
from .constants import RECIPE_NAME
from .errors import DeviceError
from .inventory import AppInventory
from .launching import detect_framework, resolve_app_dir
from .profiles import compose_wiring_checks
from .recipe import Recipe
from .wiring import WiringCheck


def _resolve_doctor_framework(cwd: Path, override: str | None) -> str | None:
    try:
        return _resolve_doctor_target(cwd, override)[0]
    except DeviceError:
        return None


def _resolve_doctor_target(cwd: Path, override: str | None) -> tuple[str, Path]:
    if override:
        return (override, cwd)
    recipe_path = cwd / RECIPE_NAME
    recipe = Recipe.load(recipe_path) if recipe_path.exists() else Recipe({}, recipe_path)
    framework = detect_framework(cwd, recipe)
    return (framework, resolve_app_dir(cwd, recipe, framework))


def _run_detect(check: WiringCheck, cwd: Path) -> tuple[str, str]:
    try:
        return check.detect(cwd)
    except Exception as error:  # noqa: BLE001
        return ("problem", f"check could not run: {error}")


def _wiring_checks_for_framework(framework: str, cwd: Path) -> list[WiringCheck]:
    profile = PROFILES.get(framework)
    if profile is None:
        return []
    app = AppInventory(name="main", path=cwd, profile=framework)
    return list(profile.wiring_checks(app))


def cmd_doctor(cwd: Path, *, fix: bool = False, framework_override: str | None = None) -> int:
    try:
        framework, app_dir = _resolve_doctor_target(cwd, framework_override)
    except DeviceError as error:
        print(f"doctor: {error}", file=sys.stderr)
        print("  pass --framework=NAME to check a specific framework.", file=sys.stderr)
        return 1
    if app_dir != cwd:
        print(f"doctor: checking {app_dir.relative_to(cwd)} (`{framework}`)", file=sys.stderr)
    targets = [(check, app_dir) for check in _wiring_checks_for_framework(framework, app_dir)]
    targets += [(check, cwd) for check in compose_wiring_checks(cwd)]
    if not targets:
        profile = PROFILES.get(framework)
        if profile is not None and profile.env_only:
            print(f"  ✓  no wiring checks needed for `{framework}` (env-only)", file=sys.stderr)
        else:
            print(f"doctor: no wiring checks defined for framework `{framework}`.", file=sys.stderr)
        return 0

    bad = 0
    for check, check_dir in targets:
        if not check.applies(check_dir):
            print(f"  -  {check.id}: not applicable", file=sys.stderr)
            continue
        status, detail = _run_detect(check, check_dir)
        if status == "ok":
            print(f"  ✓  {check.id}: {check.description}", file=sys.stderr)
            continue
        if fix and check.autofix is not None:
            try:
                check.autofix(check_dir)
            except Exception as error:  # noqa: BLE001
                print(f"  ✗  {check.id}: autofix failed: {error}", file=sys.stderr)
                bad += 1
                continue
            status_after, detail_after = _run_detect(check, check_dir)
            if status_after == "ok":
                print(f"  ✓  {check.id}: {check.description} (fixed)", file=sys.stderr)
                continue
            print(f"  ✗  {check.id}: still problem after autofix: {detail_after}", file=sys.stderr)
            if check.manual_instructions is not None:
                for line in check.manual_instructions(check_dir).splitlines():
                    print(f"        {line}", file=sys.stderr)
            bad += 1
            continue
        print(f"  ✗  {check.id}: {detail}", file=sys.stderr)
        if check.manual_instructions is not None:
            for line in check.manual_instructions(check_dir).splitlines():
                print(f"        {line}", file=sys.stderr)
        bad += 1
    return 0 if bad == 0 else 1
