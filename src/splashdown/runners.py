"""Build/install/launch for the mobile and native profiles.

Split out of `profiles.py`: these functions are what `Profile.run` delegates to, and
they touch xcodebuild/gradle/adb rather than the detection-and-resources contract the
rest of that module is about. Depends on nothing in `profiles.py` — the arrow points
one way, profiles -> runners.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from .capabilities import require_macos, translate_tool_errors
from .errors import DeviceError
from .recipe import Recipe


def _no_flag(label: str, value: str) -> str:
    """Reject a recipe-supplied value that argv would parse as an option (leading
    `-`). These reach xcodebuild/gradle/adb as bare positionals where a `-foo`
    would silently become a tool flag."""
    if value.startswith("-"):
        raise DeviceError(f"{label} must not start with '-': {value!r}")
    return value


# Android package / activity names are restricted to identifiers, dots, and (for
# inner classes) `$`. `adb shell am start` re-parses its argv through the device's
# /bin/sh, so a recipe value like `.Main; rm -rf /sdcard` would be a shell injection
# on the device — validate against the legal charset rather than trusting argv.
_ANDROID_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.$]+$")


def _android_component(label: str, value: str) -> str:
    if not _ANDROID_COMPONENT_RE.match(value):
        raise DeviceError(f"{label} may only contain letters, digits, `_`, `.`, `$`: {value!r}")
    return value


def _flutter_run(cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
    device_id = (info.get("udid") if info["kind"] == "ios" else info.get("serial")) or ""
    with translate_tool_errors("flutter", "flutter", "install Flutter and add it to PATH"):
        return subprocess.call(["flutter", "run", "-d", device_id], cwd=cwd)


def _rn_ios_flags(recipe: Recipe) -> list[str]:
    """`react-native run-ios` scheme/mode from `[project.ios]`. The scheme selects
    the build environment for scheme-driven apps (dev/staging/prod each copying a
    different `.env`), so without it RN CLI silently builds the project-name
    scheme — often the production one."""
    cfg = recipe.project.get("ios") or {}
    flags: list[str] = []
    if scheme := cfg.get("scheme"):
        flags += ["--scheme", _no_flag("ios scheme", scheme)]
    if mode := cfg.get("mode"):
        flags += ["--mode", _no_flag("ios mode", mode)]
    return flags


def _rn_android_flags(recipe: Recipe) -> list[str]:
    """`react-native run-android` build variant from `[project.android] mode`
    (RN 0.73+ `--mode`, e.g. `developmentDebug`)."""
    cfg = recipe.project.get("android") or {}
    if mode := cfg.get("mode"):
        return ["--mode", _no_flag("android mode", mode)]
    return []


def _rn_ios_arch_hint(cwd: Path) -> str | None:
    """Advisory hint when the app's CocoaPods exclude arm64 for the iOS simulator
    (a vendored SDK like Google ML Kit ships no arm64-sim slice). Such an app can
    only build against an x86_64 simulator — which only iOS <= 18.x provides — so
    against splashdown's default (newest, arm64-only) sim `xcodebuild` fails with
    an opaque "Unable to find a destination". Returns None unless the exclusion is
    present, so the hint stays silent for apps that don't need x86_64.

    Reads `EXCLUDED_ARCHS[sdk=iphonesimulator*]` from the generated Pods build
    files rather than probing the sim's arch (simctl exposes no clean arch field)."""
    pods = cwd / "ios" / "Pods"
    if not pods.is_dir():
        return None
    needle = "EXCLUDED_ARCHS[sdk=iphonesimulator*]"
    candidates = list(pods.rglob("*.xcconfig"))
    pbxproj = pods / "Pods.xcodeproj" / "project.pbxproj"
    if pbxproj.exists():
        candidates.append(pbxproj)
    for path in candidates:
        try:
            text = path.read_text()
        except OSError:
            continue
        if any(needle in ln and "arm64" in ln for ln in text.splitlines()):
            return _x86_64_sim_advice()
    return None


def _x86_64_sim_advice() -> str:
    head = (
        "splashdown: this app excludes arm64 for the iOS simulator (a vendored SDK "
        "like Google ML Kit ships no arm64-sim slice), so it needs an x86_64 "
        "simulator — Apple dropped x86_64 from the iOS 26 runtimes.\n"
        '  If the build failed with "Unable to find a destination...", '
    )
    # Lazy: architecture advice is the only runner dependency on device lifecycle.
    from .devices import ios_x86_64_target  # noqa: PLC0415

    target = ios_x86_64_target()
    if target is None:
        return head + (
            "no installed runtime has an x86_64 slice.\n"
            "  Install an iOS 18.x runtime (Xcode > Settings > Components), then pin it "
            "under [targets.simulator.default] in splashdown.toml."
        )
    version, model = target
    # The model has to come from the same runtime: device types are per-runtime, so
    # pinning `ios` alone against a newer default model fails `simctl create`.
    return head + (
        "pin an x86_64-capable runtime in splashdown.toml:\n"
        "      [targets.simulator.default]\n"
        f'      model = "{model}"\n'
        f'      ios = "{version}"\n'
        "  then re-run `splash sync`."
    )


def _rn_run(cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
    if info["kind"] == "ios":
        cmd = ["npx", "react-native", "run-ios", "--udid", info["udid"], *_rn_ios_flags(recipe)]
        with translate_tool_errors("node", "npx", "install Node.js and add npx to PATH"):
            rc = subprocess.call(cmd, cwd=cwd)
        if rc != 0 and (hint := _rn_ios_arch_hint(cwd)):
            print(hint, file=sys.stderr)
        return rc
    cmd = [
        "npx",
        "react-native",
        "run-android",
        "--deviceId",
        info["serial"],
        *_rn_android_flags(recipe),
    ]
    with translate_tool_errors("node", "npx", "install Node.js and add npx to PATH"):
        return subprocess.call(cmd, cwd=cwd)


def _expo_run(cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
    # No scheme/mode forwarding: `expo run:ios --scheme` means a URL scheme, not
    # an Xcode scheme, so `[project.ios] scheme` can't be mapped cleanly here.
    if info["kind"] == "ios":
        with translate_tool_errors("node", "npx", "install Node.js and add npx to PATH"):
            return subprocess.call(["npx", "expo", "run:ios", "--device", info["udid"]], cwd=cwd)
    with translate_tool_errors("node", "npx", "install Node.js and add npx to PATH"):
        return subprocess.call(["npx", "expo", "run:android", "--device", info["serial"]], cwd=cwd)


_RUN_PLACEHOLDER = re.compile(r"\{(device_id|device_name|platform)\}")


def _resolve_custom_run(recipe: Recipe, kind: str) -> str | None:
    """The user's custom run command for this platform, or None if unset (or the
    platform is unset in a `[project.run]` table — the other platform stays on
    auto-detection). `[project] run` is either a single string (shared across
    platforms) or a `[project.run]` table with `ios`/`android` keys."""
    run = recipe.project.get("run")
    if run is None:
        return None
    if isinstance(run, dict):
        cmd = run.get(kind)
        if cmd is None:
            return None  # this platform isn't customized → fall back to detection
    elif isinstance(run, str):
        cmd = run
    else:
        raise DeviceError("`[project] run` must be a string or a [project.run] table")
    if not isinstance(cmd, str):
        raise DeviceError(f"`[project] run` command must be a string, got {type(cmd).__name__}")
    if not cmd.strip():
        raise DeviceError("`[project] run` command is empty")
    return cmd


def _substitute_run_placeholders(cmd: str, info: dict[str, str]) -> str:
    """Substitute {device_id}/{device_name}/{platform} in a custom run command.
    Device values are shell-quoted so spaces/quotes can't break the command;
    unknown `{...}` sequences are left untouched (shell brace-expansion survives)."""
    device_id = info.get("udid") or info.get("serial") or ""
    if "{device_id}" in cmd and not device_id:
        raise DeviceError("run command uses {device_id} but no device id is available")
    values = {
        "device_id": shlex.quote(device_id),
        "device_name": shlex.quote(info.get("name", "")),
        "platform": info.get("kind", ""),
    }
    return _RUN_PLACEHOLDER.sub(lambda m: values[m.group(1)], cmd)


def run_custom_command(cwd: Path, recipe: Recipe, info: dict[str, str]) -> int | None:
    """Run the user's custom command with the booted device identifier injected.
    Returns the exit code, or None when no custom command is configured (caller
    falls back to framework detection). Runs via a shell (like `[setup.*]`) so
    pipes / `&&` / `$ENV` / `cd` work."""
    cmd = _resolve_custom_run(recipe, info["kind"])
    if cmd is None:
        return None
    cmd = _substitute_run_placeholders(cmd, info)
    return subprocess.call(cmd, shell=True, cwd=cwd)  # noqa: S602 — user-authored run command by design


def _ios_xcodebuild_args(cwd: Path, cfg: dict[str, Any]) -> list[str]:
    """Build the workspace/project flag for xcodebuild — explicit setting wins,
    else first match at repo root."""
    if w := cfg.get("workspace"):
        return ["-workspace", str(w)]
    if p := cfg.get("project"):
        return ["-project", str(p)]
    workspaces = sorted(cwd.glob("*.xcworkspace"))
    if workspaces:
        return ["-workspace", workspaces[0].name]
    projects = sorted(cwd.glob("*.xcodeproj"))
    if projects:
        return ["-project", projects[0].name]
    raise DeviceError(
        "ios-native: no .xcworkspace or .xcodeproj at repo root; "
        'set `[project.ios] workspace = "..."` or `project = "..."`'
    )


def _ios_native_schemes(cwd: Path) -> list[str]:
    argv = ["xcodebuild", *_ios_xcodebuild_args(cwd, {}), "-list", "-json"]
    try:
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise DeviceError(f"ios-native: couldn't list Xcode schemes: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or f"xcodebuild exited {result.returncode}"
        raise DeviceError(f"ios-native: couldn't list Xcode schemes: {detail}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DeviceError("ios-native: xcodebuild returned invalid scheme data") from exc

    schemes: list[str] = []
    if isinstance(data, dict):
        for container in data.values():
            if not isinstance(container, dict):
                continue
            values = container.get("schemes")
            if not isinstance(values, list):
                continue
            for value in values:
                if isinstance(value, str) and value and value not in schemes:
                    schemes.append(value)
    return schemes


def _ios_native_run(cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
    require_macos("native build support")
    cfg = recipe.project.get("ios") or {}
    scheme = cfg.get("scheme")
    if not scheme:
        raise DeviceError(
            'ios-native: set `[project.ios] scheme = "<your-scheme>"` in splashdown.toml'
        )
    scheme = _no_flag("ios scheme", scheme)
    configuration = _no_flag("ios configuration", cfg.get("configuration", "Debug"))
    udid = info["udid"]
    derived = cwd / "build" / "splash-derived"
    project_flag = _ios_xcodebuild_args(cwd, cfg)

    common = [
        "xcodebuild",
        *project_flag,
        "-scheme",
        scheme,
        "-configuration",
        configuration,
        "-destination",
        f"id={udid}",
        "-derivedDataPath",
        str(derived),
    ]
    with translate_tool_errors(
        "ios", "xcodebuild", "install Xcode and select it with xcode-select"
    ):
        rc = subprocess.call([*common, "build"], cwd=cwd)
    if rc != 0:
        return rc

    with translate_tool_errors(
        "ios", "xcodebuild", "install Xcode and select it with xcode-select"
    ):
        settings = subprocess.run(
            [*common, "-showBuildSettings", "-json"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    try:
        entries = json.loads(settings.stdout)
        bs = entries[0]["buildSettings"]
        app_path = Path(bs["BUILT_PRODUCTS_DIR"]) / bs["WRAPPER_NAME"]
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        raise DeviceError(f"ios-native: couldn't read xcodebuild settings: {e}") from e
    if not app_path.exists():
        raise DeviceError(f"ios-native: built .app missing at {app_path}")

    try:
        with (app_path / "Info.plist").open("rb") as f:
            plist = plistlib.load(f)
        bundle_id = plist["CFBundleIdentifier"]
    except (FileNotFoundError, KeyError) as e:
        raise DeviceError(f"ios-native: couldn't read bundle id from {app_path}: {e}") from e

    if info.get("physical"):
        # Physical iOS devices aren't reachable via simctl (simulator-only);
        # devicectl (Xcode 15+) installs and launches on real hardware.
        with translate_tool_errors("ios", "xcrun", "install Xcode command-line tools"):
            rc = subprocess.call(
                [
                    "xcrun",
                    "devicectl",
                    "device",
                    "install",
                    "app",
                    "--device",
                    udid,
                    str(app_path),
                ]
            )
        if rc != 0:
            return rc
        with translate_tool_errors("ios", "xcrun", "install Xcode command-line tools"):
            return subprocess.call(
                [
                    "xcrun",
                    "devicectl",
                    "device",
                    "process",
                    "launch",
                    "--device",
                    udid,
                    bundle_id,
                ]
            )

    with translate_tool_errors("ios", "xcrun", "install Xcode command-line tools"):
        rc = subprocess.call(["xcrun", "simctl", "install", udid, str(app_path)])
    if rc != 0:
        return rc
    with translate_tool_errors("ios", "xcrun", "install Xcode command-line tools"):
        return subprocess.call(["xcrun", "simctl", "launch", udid, bundle_id])


def _android_native_run(cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
    cfg = recipe.project.get("android") or {}
    module = _no_flag("android module", cfg.get("module", "app"))
    variant = _no_flag("android variant", cfg.get("variant", "debug"))
    serial = info["serial"]
    gradlew = cwd / "gradlew"
    gradle_cmd = [f"./{gradlew.name}"] if gradlew.exists() else ["gradle"]
    gradle_tool = gradle_cmd[0]

    install_task = f":{module}:install{variant[:1].upper()}{variant[1:]}"
    env = {**os.environ, "ANDROID_SERIAL": serial}
    with translate_tool_errors(
        "gradle",
        gradle_tool,
        "install Gradle or add an executable gradlew wrapper",
    ):
        rc = subprocess.call([*gradle_cmd, install_task], cwd=cwd, env=env)
    if rc != 0:
        return rc

    app_id = cfg.get("application_id")
    if not app_id:
        try:
            with translate_tool_errors(
                "gradle",
                gradle_tool,
                "install Gradle or add an executable gradlew wrapper",
            ):
                out = subprocess.check_output(
                    [*gradle_cmd, f":{module}:properties", "-q"],
                    cwd=cwd,
                    text=True,
                    env=env,
                )
            for line in out.splitlines():
                if line.startswith("applicationId:"):
                    app_id = line.split(":", 1)[1].strip()
                    break
        except subprocess.CalledProcessError:
            pass
    if not app_id:
        raise DeviceError(
            "android-native: couldn't resolve applicationId; set "
            '`[project.android] application_id = "..."` in splashdown.toml'
        )
    app_id = _android_component("android application_id", app_id)

    if activity := cfg.get("launch_activity"):
        activity = _android_component("android launch_activity", activity)
        with translate_tool_errors(
            "android", "adb", "install Android SDK platform-tools and add adb to PATH"
        ):
            return subprocess.call(
                [
                    "adb",
                    "-s",
                    serial,
                    "shell",
                    "am",
                    "start",
                    "-n",
                    f"{app_id}/{activity}",
                ],
            )
    with translate_tool_errors(
        "android", "adb", "install Android SDK platform-tools and add adb to PATH"
    ):
        return subprocess.call(
            [
                "adb",
                "-s",
                serial,
                "shell",
                "monkey",
                "-p",
                app_id,
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
            ],
        )
