from __future__ import annotations

from pathlib import Path
from typing import Any

from .device_types import LaunchDestination
from .inventory import AppInventory
from .package_json import package_dependencies
from .profile_core import Profile, _manual_port_guidance, _profile_port
from .recipe import Recipe
from .runners import _android_native_run, _expo_run, _flutter_run, _ios_native_run, _rn_run
from .wiring import _HOOK_WIRING_CHECK, _RN_WIRING_CHECKS, WiringCheck


def _detect_flutter(cwd: Path) -> bool:
    return (cwd / "pubspec.yaml").exists()


def _detect_expo(cwd: Path) -> bool:
    deps = package_dependencies(cwd)
    return "expo" in deps and (cwd / "app.json").exists()


def _detect_rn(cwd: Path) -> bool:
    return "react-native" in package_dependencies(cwd)


# `[project] run` (or a `[project.run]` table) overrides the framework's built-in
# launcher — the escape hatch for a specific package manager (yarn/pnpm), a
# monorepo subdir, or any non-standard invocation. Mobile-only (that's the only
# place `splash run` exists). See _resolve_custom_run / run_custom_command.


def _has_js_or_flutter(cwd: Path) -> bool:
    return _detect_flutter(cwd) or _detect_expo(cwd) or _detect_rn(cwd)


def _pbxproj_targets_ios(project: Path) -> bool:
    """Whether an .xcodeproj builds for iOS. Fails open unless the pbxproj says
    macOS and nothing says iOS — projects that keep deployment targets in an
    .xcconfig name neither, and must not be excluded on that silence."""
    try:
        text = (project / "project.pbxproj").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    if "IPHONEOS_DEPLOYMENT_TARGET" in text or "SDKROOT = iphoneos" in text:
        return True
    return "MACOSX_DEPLOYMENT_TARGET" not in text and "SDKROOT = macosx" not in text


def _detect_ios_native(cwd: Path) -> bool:
    if _has_js_or_flutter(cwd):
        return False
    # A macOS-only app matches the same globs but has no simulator to build for.
    # The workspace usually wraps a sibling project, so let the projects decide.
    projects = sorted(cwd.glob("*.xcodeproj"))
    if projects:
        return any(_pbxproj_targets_ios(p) for p in projects)
    return any(cwd.glob("*.xcworkspace"))


def _detect_android_native(cwd: Path) -> bool:
    if _has_js_or_flutter(cwd):
        return False
    has_build = (cwd / "build.gradle").exists() or (cwd / "build.gradle.kts").exists()
    has_settings = (cwd / "settings.gradle").exists() or (cwd / "settings.gradle.kts").exists()
    return has_build and has_settings


# Omitting ios/image keeps scanner defaults on the latest installed runtime.
_DEFAULT_SIM_TARGET: dict[str, dict[str, dict[str, str]]] = {
    "simulator": {"default": {"model": "iPhone 17"}}
}
_DEFAULT_EMULATOR_TARGET: dict[str, dict[str, dict[str, str]]] = {
    "emulator": {"default": {"device": "pixel_9"}}
}
_DEFAULT_MOBILE_TARGETS = {**_DEFAULT_SIM_TARGET, **_DEFAULT_EMULATOR_TARGET}


class ReactNativeProfile(Profile):
    name = "react-native"

    def detect(self, app_path: Path) -> bool:
        return _detect_rn(app_path)

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        return {"RCT_METRO_PORT": {"type": "port", "range": [8082, 8200]}}

    def targets(self, app: AppInventory) -> dict[str, dict[str, dict[str, str]]]:
        return _DEFAULT_MOBILE_TARGETS

    def wiring_checks(self, app: AppInventory) -> list[WiringCheck]:
        return list(_RN_WIRING_CHECKS)

    def agent_guidance(self, app: AppInventory, port_names: list[str]) -> list[str]:
        port = _profile_port(port_names, "RCT_METRO_PORT")
        return [
            "- Metro must use the allocated port; do not start it on a numeric default.",
            *_manual_port_guidance(
                "Metro", "npx react-native start --port {port}", port, app.project_path
            ),
            "- Launch with `splash run simulator` or `splash run emulator`.",
        ]

    def run(self, cwd: Path, recipe: Recipe, destination: LaunchDestination) -> int:
        return _rn_run(cwd, recipe, destination)


class ExpoProfile(Profile):
    name = "expo"

    def detect(self, app_path: Path) -> bool:
        return _detect_expo(app_path)

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        return {"RCT_METRO_PORT": {"type": "port", "range": [8082, 8200]}}

    def targets(self, app: AppInventory) -> dict[str, dict[str, dict[str, str]]]:
        return _DEFAULT_MOBILE_TARGETS

    def agent_guidance(self, app: AppInventory, port_names: list[str]) -> list[str]:
        port = _profile_port(port_names, "RCT_METRO_PORT")
        return [
            "- Metro must use the allocated port; do not start it on a numeric default.",
            *_manual_port_guidance(
                "Expo Metro", "npx expo start --port {port}", port, app.project_path
            ),
            "- Launch with `splash run simulator` or `splash run emulator`.",
        ]

    def run(self, cwd: Path, recipe: Recipe, destination: LaunchDestination) -> int:
        return _expo_run(cwd, recipe, destination)


class FlutterProfile(Profile):
    name = "flutter"

    def detect(self, app_path: Path) -> bool:
        return _detect_flutter(app_path)

    def targets(self, app: AppInventory) -> dict[str, dict[str, dict[str, str]]]:
        return _DEFAULT_MOBILE_TARGETS

    def run(self, cwd: Path, recipe: Recipe, destination: LaunchDestination) -> int:
        return _flutter_run(cwd, recipe, destination)


class IosNativeProfile(Profile):
    name = "ios-native"

    def detect(self, app_path: Path) -> bool:
        return _detect_ios_native(app_path)

    def targets(self, app: AppInventory) -> dict[str, dict[str, dict[str, str]]]:
        return _DEFAULT_SIM_TARGET

    def wiring_checks(self, app: AppInventory) -> list[WiringCheck]:
        return [_HOOK_WIRING_CHECK]

    def run(self, cwd: Path, recipe: Recipe, destination: LaunchDestination) -> int:
        return _ios_native_run(cwd, recipe, destination)


class AndroidNativeProfile(Profile):
    name = "android-native"

    def detect(self, app_path: Path) -> bool:
        return _detect_android_native(app_path)

    def targets(self, app: AppInventory) -> dict[str, dict[str, dict[str, str]]]:
        return _DEFAULT_EMULATOR_TARGET

    def wiring_checks(self, app: AppInventory) -> list[WiringCheck]:
        return [_HOOK_WIRING_CHECK]

    def run(self, cwd: Path, recipe: Recipe, destination: LaunchDestination) -> int:
        return _android_native_run(cwd, recipe, destination)
