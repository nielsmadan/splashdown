from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .capabilities import require_macos
from .device_types import LaunchDestination
from .errors import DeviceError
from .inventory import AppInventory
from .package_json import package_dependencies, read_package_json
from .profile_core import Profile, _manual_port_guidance, _profile_port
from .recipe import Recipe
from .runners import (
    _IOS_NATIVE_DESTINATION_ERROR,
    _android_native_run,
    _expo_run,
    _flutter_run,
    _ios_native_run,
    _ios_native_scheme,
    _rn_run,
)
from .wiring import _HOOK_WIRING_CHECK, WiringCheck, rn_wiring_checks

_RN_LAUNCH_SCRIPT_RE = re.compile(
    r"(?:^|[\s;&|])react-native\s+(?:start|run-ios|run-android)(?=$|[\s;&|])"
)


def _detect_flutter(cwd: Path) -> bool:
    return (cwd / "pubspec.yaml").exists()


def _detect_expo(cwd: Path) -> bool:
    deps = package_dependencies(cwd)
    if "expo" not in deps or not (cwd / "app.json").exists():
        return False
    scripts = read_package_json(cwd).get("scripts")
    if "react-native" in deps and isinstance(scripts, dict):
        for name in ("start", "ios", "android"):
            script = scripts.get(name)
            if isinstance(script, str) and _RN_LAUNCH_SCRIPT_RE.search(script):
                return False
    return True


def _detect_rn(cwd: Path) -> bool:
    return "react-native" in package_dependencies(cwd)


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
    build = cwd / "build.gradle"
    if not build.exists():
        build = cwd / "build.gradle.kts"
    has_build = build.exists()
    has_settings = (cwd / "settings.gradle").exists() or (cwd / "settings.gradle.kts").exists()
    if not has_build:
        return False
    if has_settings:
        return True
    try:
        text = build.read_text()
    except OSError:
        return False
    return "com.android.application" in text or "libs.plugins.android.application" in text


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

    def wiring_checks(self, app: AppInventory, env_file: str) -> list[WiringCheck]:
        return rn_wiring_checks(env_file)

    def agent_guidance(self, app: AppInventory, port_names: list[str]) -> list[str]:
        port = _profile_port(port_names, "RCT_METRO_PORT")
        return [
            "- Metro must use the allocated port; do not start it on a numeric default.",
            *_manual_port_guidance(
                "Metro", "npx react-native start --port {port}", port, app.project_path
            ),
            "- Launch with `splash run simulator` or `splash run emulator`.",
        ]

    def run(
        self,
        cwd: Path,
        recipe: Recipe,
        destination: LaunchDestination,
        *,
        env: dict[str, str] | None = None,
    ) -> int:
        return _rn_run(cwd, recipe, destination, env=env)


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

    def run(
        self,
        cwd: Path,
        recipe: Recipe,
        destination: LaunchDestination,
        *,
        env: dict[str, str] | None = None,
    ) -> int:
        return _expo_run(cwd, recipe, destination, env=env)


class FlutterProfile(Profile):
    name = "flutter"

    def detect(self, app_path: Path) -> bool:
        return _detect_flutter(app_path)

    def targets(self, app: AppInventory) -> dict[str, dict[str, dict[str, str]]]:
        return _DEFAULT_MOBILE_TARGETS

    def run(
        self,
        cwd: Path,
        recipe: Recipe,
        destination: LaunchDestination,
        *,
        env: dict[str, str] | None = None,
    ) -> int:
        return _flutter_run(cwd, recipe, destination, env=env)


class IosNativeProfile(Profile):
    name = "ios-native"

    def detect(self, app_path: Path) -> bool:
        return _detect_ios_native(app_path)

    def targets(self, app: AppInventory) -> dict[str, dict[str, dict[str, str]]]:
        return _DEFAULT_SIM_TARGET

    def wiring_checks(self, app: AppInventory, env_file: str) -> list[WiringCheck]:
        return [_HOOK_WIRING_CHECK]

    def validate_run(self, cwd: Path, recipe: Recipe, kind: str | None) -> None:
        if kind is not None and kind != "ios":
            raise DeviceError(_IOS_NATIVE_DESTINATION_ERROR)
        require_macos("native build support")
        cfg = recipe.project.setdefault("ios", {})
        cfg["scheme"] = _ios_native_scheme(cwd, cfg)

    def run(
        self,
        cwd: Path,
        recipe: Recipe,
        destination: LaunchDestination,
        *,
        env: dict[str, str] | None = None,
    ) -> int:
        return _ios_native_run(cwd, recipe, destination, env=env)


class AndroidNativeProfile(Profile):
    name = "android-native"

    def detect(self, app_path: Path) -> bool:
        return _detect_android_native(app_path)

    def targets(self, app: AppInventory) -> dict[str, dict[str, dict[str, str]]]:
        return _DEFAULT_EMULATOR_TARGET

    def wiring_checks(self, app: AppInventory, env_file: str) -> list[WiringCheck]:
        return [_HOOK_WIRING_CHECK]

    def run(
        self,
        cwd: Path,
        recipe: Recipe,
        destination: LaunchDestination,
        *,
        env: dict[str, str] | None = None,
    ) -> int:
        return _android_native_run(cwd, recipe, destination, env=env)
