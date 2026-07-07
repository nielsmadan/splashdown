from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .devices import DeviceError
from .recipe import Recipe
from .scanner import PROFILES, AppInventory
from .wiring import _HOOK_WIRING_CHECK, _RN_WIRING_CHECKS, WiringCheck

# ---------- scaffold registry ----------
# Named scaffolds for `splash init NAME`. Framework detection, wiring checks,
# and `splash run` logic live on `Profile` subclasses (below).


def _no_flag(label: str, value: str) -> str:
    """Reject a recipe-supplied value that argv would parse as an option (leading
    `-`). These reach xcodebuild/gradle/adb as bare positionals where a `-foo`
    would silently become a tool flag."""
    if value.startswith("-"):
        raise DeviceError(f"{label} must not start with '-': {value!r}")
    return value


def _detect_flutter(cwd: Path) -> bool:
    return (cwd / "pubspec.yaml").exists()


def _read_pkg_deps(cwd: Path) -> dict[str, Any]:
    pkg = cwd / "package.json"
    if not pkg.exists():
        return {}
    try:
        data = json.loads(pkg.read_text())
    except json.JSONDecodeError:
        return {}
    return {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}


def _detect_expo(cwd: Path) -> bool:
    deps = _read_pkg_deps(cwd)
    return "expo" in deps and (cwd / "app.json").exists()


def _detect_rn(cwd: Path) -> bool:
    return "react-native" in _read_pkg_deps(cwd)


def _flutter_run(cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
    device_id = (info.get("udid") if info["kind"] == "ios" else info.get("serial")) or ""
    return subprocess.call(["flutter", "run", "-d", device_id], cwd=cwd)


def _rn_run(cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
    if info["kind"] == "ios":
        return subprocess.call(["npx", "react-native", "run-ios", "--udid", info["udid"]], cwd=cwd)
    return subprocess.call(
        ["npx", "react-native", "run-android", "--deviceId", info["serial"]], cwd=cwd
    )


def _expo_run(cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
    if info["kind"] == "ios":
        return subprocess.call(["npx", "expo", "run:ios", "--device", info["udid"]], cwd=cwd)
    return subprocess.call(["npx", "expo", "run:android", "--device", info["serial"]], cwd=cwd)


def _has_js_or_flutter(cwd: Path) -> bool:
    return _detect_flutter(cwd) or _detect_expo(cwd) or _detect_rn(cwd)


def _detect_ios_native(cwd: Path) -> bool:
    if _has_js_or_flutter(cwd):
        return False
    return any(cwd.glob("*.xcworkspace")) or any(cwd.glob("*.xcodeproj"))


def _detect_android_native(cwd: Path) -> bool:
    if _has_js_or_flutter(cwd):
        return False
    has_build = (cwd / "build.gradle").exists() or (cwd / "build.gradle.kts").exists()
    has_settings = (cwd / "settings.gradle").exists() or (cwd / "settings.gradle.kts").exists()
    return has_build and has_settings


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


def _ios_native_run(cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
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
    rc = subprocess.call([*common, "build"], cwd=cwd)
    if rc != 0:
        return rc

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
        rc = subprocess.call(
            ["xcrun", "devicectl", "device", "install", "app", "--device", udid, str(app_path)]
        )
        if rc != 0:
            return rc
        return subprocess.call(
            ["xcrun", "devicectl", "device", "process", "launch", "--device", udid, bundle_id]
        )

    rc = subprocess.call(["xcrun", "simctl", "install", udid, str(app_path)])
    if rc != 0:
        return rc
    return subprocess.call(["xcrun", "simctl", "launch", udid, bundle_id])


def _android_native_run(cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
    cfg = recipe.project.get("android") or {}
    module = _no_flag("android module", cfg.get("module", "app"))
    variant = _no_flag("android variant", cfg.get("variant", "debug"))
    serial = info["serial"]
    gradlew = cwd / "gradlew"
    gradle_cmd = [f"./{gradlew.name}"] if gradlew.exists() else ["gradle"]

    install_task = f":{module}:install{variant[:1].upper()}{variant[1:]}"
    env = {**os.environ, "ANDROID_SERIAL": serial}
    rc = subprocess.call([*gradle_cmd, install_task], cwd=cwd, env=env)
    if rc != 0:
        return rc

    app_id = cfg.get("application_id")
    if not app_id:
        try:
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
    app_id = _no_flag("android application_id", app_id)

    if activity := cfg.get("launch_activity"):
        activity = _no_flag("android launch_activity", activity)
        return subprocess.call(
            ["adb", "-s", serial, "shell", "am", "start", "-n", f"{app_id}/{activity}"],
        )
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


# ---------- profiles ----------
# A Profile encodes how splashdown integrates with one framework. The Scanner
# matches each app to a Profile by filesystem detection; Profiles contribute
# resources (which end up in [resources.*]) and wiring checks (which the doctor
# runs to patch consumer configs). Built-in only — to add a Profile, ship it
# upstream as a new subclass + entry in PROFILES.


class Profile:
    """Abstract base. Subclasses set `name` and override `detect`, `resources`,
    and (where relevant) `wiring_checks` / `run`."""

    name: str = ""

    # Whether this framework reads values from a plain dotenv file (`.env` /
    # `.env.local`) on its own. True for server frameworks where loading a
    # dotenv file is the conventional setup (Next.js natively; Django/FastAPI/
    # Node via the near-ubiquitous dotenv libraries). False for frameworks that
    # only see values already exported into the process environment — Vite
    # (config rewritten to read `process.env`), Spring Boot (`${PORT}`
    # placeholder), and the mobile build systems. Used to decide, when no shell
    # loader is detected, whether a dotenv-file fallback can actually reach the app.
    reads_dotenv: bool = False

    def detect(self, app_path: Path) -> bool:
        raise NotImplementedError

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        """Return a {resource_name: {type, range, ...}} dict to merge into the
        recipe's [resources.*] tables. Resource names should be canonical; the
        Scanner mangles them with the app name when more than one app of the
        same profile is present."""
        return {}

    def targets(self, app: AppInventory) -> dict[str, dict[str, dict[str, str]]]:
        """Return default device targets keyed {dtype: {variant: {field: value}}}
        to emit as [targets.*] tables in a scanner-driven `splash init`. Mobile
        profiles override this; non-device profiles return {} (no targets)."""
        return {}

    def wiring_checks(self, app: AppInventory) -> list[WiringCheck]:
        """Return WiringCheck instances for consumer-side config patches. The
        existing doctor flow runs these."""
        return []

    def run(self, cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
        """Build + install + launch the app on the given device. Mobile
        Profiles override; web/backend Profiles raise (no `splash run` semantics
        for them — those use `pnpm dev` / `gradle bootRun` / etc. directly)."""
        raise DeviceError(f"don't know how to run framework `{self.name}`")


_VITE_CONFIG_NAMES = ("vite.config.ts", "vite.config.js", "vite.config.mjs", "vite.config.mts")
# Matches `env.VAR_NAME` access (the loadEnv idiom). The wiring autofix rewrites
# these to `process.env.VAR_NAME` so splashdown.env + mise loading works.
# Negative lookbehind on `process.` ensures already-fixed `process.env.VAR` is
# not re-matched.
_VITE_ENV_ACCESS_RE = re.compile(r"(?<!process\.)(?<!\.)env\.([A-Z][A-Z0-9_]*)\b")


def _vite_config_path(app_path: Path) -> Path | None:
    for name in _VITE_CONFIG_NAMES:
        candidate = app_path / name
        if candidate.exists():
            return candidate
    return None


class ViteProfile(Profile):
    name = "vite"

    def detect(self, app_path: Path) -> bool:
        return _vite_config_path(app_path) is not None

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {
            "WEB_DEV_PORT": {"type": "port", "range": [5174, 5200]},
        }
        # Only emit API_DEV_PORT if the config has a server.proxy block —
        # otherwise this app doesn't need to know the api's port at all.
        cfg = _vite_config_path(app.path)
        if cfg and "proxy" in cfg.read_text():
            out["API_DEV_PORT"] = {"type": "template", "template": "{{ PORT }}"}
        return out

    def wiring_checks(self, app: AppInventory) -> list[WiringCheck]:
        return [_vite_process_env_check()]


def _vite_process_env_check() -> WiringCheck:
    return WiringCheck(
        id="vite-config-process-env",
        description="vite.config reads env vars from process.env, not loadEnv",
        applies=lambda cwd: _vite_config_path(cwd) is not None,
        detect=_vite_process_env_detect,
        autofix=_vite_process_env_autofix,
        manual_instructions=_vite_process_env_manual,
    )


def _vite_process_env_detect(cwd: Path) -> tuple[str, str]:
    cfg = _vite_config_path(cwd)
    if cfg is None:  # applies() guarantees the vite config exists
        raise DeviceError("vite.config.* not found")
    text = cfg.read_text()
    if "loadEnv" in text and _VITE_ENV_ACCESS_RE.search(text):
        return ("problem", "vite.config uses loadEnv; should read process.env")
    return ("ok", "vite.config reads process.env")


def _vite_process_env_autofix(cwd: Path) -> None:
    cfg = _vite_config_path(cwd)
    if cfg is None:  # applies() guarantees the vite config exists
        raise DeviceError("vite.config.* not found")
    text = cfg.read_text()
    # Rewrite every `env.VAR` access to `process.env.VAR`. Keep loadEnv lines
    # untouched (the user may want them for other purposes) — the new access
    # path just bypasses them.
    new_text = _VITE_ENV_ACCESS_RE.sub(r"process.env.\1", text)
    if new_text != text:
        cfg.write_text(new_text)
        print(f"patched {cfg.name} (env.X → process.env.X)", file=sys.stderr)


def _vite_process_env_manual(cwd: Path) -> str:
    return (
        "Edit vite.config so any `env.VAR_NAME` access reads `process.env.VAR_NAME`\n"
        "instead. Splashdown.env is loaded into the parent shell via your shell-env\n"
        "loader (mise/direnv/devbox), so process.env carries the values."
    )


PROFILES["vite"] = ViteProfile()

_NODE_BACKEND_DEPS = {"hono", "express", "fastify", "koa", "@hapi/hapi", "@nestjs/core"}


class NodeBackendProfile(Profile):
    name = "node-backend"
    reads_dotenv = True

    def detect(self, app_path: Path) -> bool:
        pkg = app_path / "package.json"
        if not pkg.exists():
            return False
        try:
            data = json.loads(pkg.read_text())
        except json.JSONDecodeError:
            return False
        deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        return any(d in deps for d in _NODE_BACKEND_DEPS)

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        return {"PORT": {"type": "port", "range": [9081, 9100]}}


PROFILES["node-backend"] = NodeBackendProfile()

_NEXTJS_CONFIG_NAMES = ("next.config.js", "next.config.ts", "next.config.mjs")


class NextJsProfile(Profile):
    name = "nextjs"
    reads_dotenv = True

    def detect(self, app_path: Path) -> bool:
        for name in _NEXTJS_CONFIG_NAMES:
            if (app_path / name).exists():
                return True
        pkg = app_path / "package.json"
        if pkg.exists():
            try:
                data = json.loads(pkg.read_text())
            except json.JSONDecodeError:
                return False
            deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
            return "next" in deps
        return False

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        return {"PORT": {"type": "port", "range": [3000, 3100]}}


PROFILES["nextjs"] = NextJsProfile()


class DjangoProfile(Profile):
    name = "django"
    reads_dotenv = True

    def detect(self, app_path: Path) -> bool:
        mp = app_path / "manage.py"
        if not mp.exists():
            return False
        try:
            return "django" in mp.read_text()
        except OSError:
            return False

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        return {"PORT": {"type": "port", "range": [8000, 8100]}}


PROFILES["django"] = DjangoProfile()


class FastApiProfile(Profile):
    name = "fastapi"
    reads_dotenv = True

    def detect(self, app_path: Path) -> bool:
        for f in ("requirements.txt", "requirements-dev.txt"):
            req = app_path / f
            if req.exists() and "fastapi" in req.read_text().lower():
                return True
        pyproject = app_path / "pyproject.toml"
        return bool(pyproject.exists() and "fastapi" in pyproject.read_text().lower())

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        return {"PORT": {"type": "port", "range": [8000, 8100]}}


PROFILES["fastapi"] = FastApiProfile()


class SpringBootProfile(Profile):
    name = "springboot"

    def detect(self, app_path: Path) -> bool:
        pom = app_path / "pom.xml"
        if pom.exists() and "spring-boot-starter" in pom.read_text():
            return True
        for grade in ("build.gradle", "build.gradle.kts"):
            g = app_path / grade
            if g.exists() and "org.springframework.boot" in g.read_text():
                return True
        return False

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        return {"PORT": {"type": "port", "range": [8080, 8180]}}

    def wiring_checks(self, app: AppInventory) -> list[WiringCheck]:
        return [_springboot_application_properties_check()]


def _springboot_application_properties_check() -> WiringCheck:
    return WiringCheck(
        id="springboot-application-properties",
        description="application.properties reads server.port from PORT env",
        applies=lambda cwd: (
            (cwd / "src" / "main" / "resources" / "application.properties").exists()
            or (cwd / "src" / "main" / "resources" / "application.yml").exists()
        ),
        detect=_springboot_app_props_detect,
        autofix=None,  # manual-only — patching Java configs is too risky to auto-rewrite
        manual_instructions=_springboot_app_props_manual,
    )


def _springboot_app_props_detect(cwd: Path) -> tuple[str, str]:
    props = cwd / "src" / "main" / "resources" / "application.properties"
    yml = cwd / "src" / "main" / "resources" / "application.yml"
    text = (props.read_text() if props.exists() else "") + (yml.read_text() if yml.exists() else "")
    if re.search(r"server\.port\s*[:=]\s*\$\{PORT", text):
        return ("ok", "server.port uses PORT env placeholder")
    return ("problem", "server.port should read ${PORT:8080} from env")


def _springboot_app_props_manual(cwd: Path) -> str:
    return (
        "In application.properties: server.port=${PORT:8080}\n"
        "In application.yml:      server:\n                              port: ${PORT:8080}"
    )


PROFILES["springboot"] = SpringBootProfile()


# Default device targets emitted by scanner-driven `splash init`, kept in sync
# with the preset scaffolds (`_RN_SCAFFOLD` etc.). iOS sim left at implicit
# `ios = "latest"` (auto-recreate on newer iOS); Android emulator on pixel_9.
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
        return {"RCT_METRO_PORT": {"type": "port", "range": [8081, 8200]}}

    def targets(self, app: AppInventory) -> dict[str, dict[str, dict[str, str]]]:
        return _DEFAULT_MOBILE_TARGETS

    def wiring_checks(self, app: AppInventory) -> list[WiringCheck]:
        return list(_RN_WIRING_CHECKS)

    def run(self, cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
        return _rn_run(cwd, recipe, info)


class ExpoProfile(Profile):
    name = "expo"

    def detect(self, app_path: Path) -> bool:
        return _detect_expo(app_path)

    def resources(self, app: AppInventory) -> dict[str, dict[str, Any]]:
        return {"RCT_METRO_PORT": {"type": "port", "range": [8081, 8200]}}

    def targets(self, app: AppInventory) -> dict[str, dict[str, dict[str, str]]]:
        return _DEFAULT_MOBILE_TARGETS

    def run(self, cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
        return _expo_run(cwd, recipe, info)


class FlutterProfile(Profile):
    name = "flutter"

    def detect(self, app_path: Path) -> bool:
        return _detect_flutter(app_path)

    def targets(self, app: AppInventory) -> dict[str, dict[str, dict[str, str]]]:
        return _DEFAULT_MOBILE_TARGETS

    def run(self, cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
        return _flutter_run(cwd, recipe, info)


class IosNativeProfile(Profile):
    name = "ios-native"

    def detect(self, app_path: Path) -> bool:
        return _detect_ios_native(app_path)

    def targets(self, app: AppInventory) -> dict[str, dict[str, dict[str, str]]]:
        return _DEFAULT_SIM_TARGET

    def wiring_checks(self, app: AppInventory) -> list[WiringCheck]:
        return [_HOOK_WIRING_CHECK]

    def run(self, cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
        return _ios_native_run(cwd, recipe, info)


class AndroidNativeProfile(Profile):
    name = "android-native"

    def detect(self, app_path: Path) -> bool:
        return _detect_android_native(app_path)

    def targets(self, app: AppInventory) -> dict[str, dict[str, dict[str, str]]]:
        return _DEFAULT_EMULATOR_TARGET

    def wiring_checks(self, app: AppInventory) -> list[WiringCheck]:
        return [_HOOK_WIRING_CHECK]

    def run(self, cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
        return _android_native_run(cwd, recipe, info)


# Mobile precedence: pubspec.yaml (flutter) wins over an `expo` or `react-native`
# dep in package.json — a flutter project might transitively pull in JS tooling
# but rarely the other way around. Then expo (requires both `expo` dep and
# app.json) before plain react-native.
PROFILES["flutter"] = FlutterProfile()
PROFILES["expo"] = ExpoProfile()
PROFILES["react-native"] = ReactNativeProfile()
PROFILES["ios-native"] = IosNativeProfile()
PROFILES["android-native"] = AndroidNativeProfile()


# Named scaffolds for `splash init NAME`. Decoupled from PROFILES because some
# entries (minimal, electron, server) don't have a detectable framework, and
# some Profiles (vite, springboot, etc.) don't have a stock scaffold.

_MINIMAL_SCAFFOLD = """\
# splashdown.toml — minimal preset. One uuid slot; no apps, no devices.

[project]
workspace = "single"
loader = "__SPLASH_LOADER__"

[resources.RUN_ID]
type = "uuid"
"""

_RN_SCAFFOLD = """\
# splashdown.toml — React Native preset.

[project]
workspace = "single"
loader = "__SPLASH_LOADER__"

[apps.main]
path = "."
profile = "react-native"
resources = ["RCT_METRO_PORT"]

[resources.RCT_METRO_PORT]
type  = "port"
range = [8081, 8200]

[targets.simulator.default]
model = "iPhone 17"
# ios = "latest"   # implicit; auto-recreate when a newer iOS lands. Pin to e.g.
                   # "18.5" if you want a fixed version that never upgrades.

[targets.emulator.default]
device = "pixel_9"

# Run on a plugged-in phone with `splash run device`. With one device
# connected, auto-pick resolves it — no config needed. Uncomment to pin a
# specific device by id/name, or to scope auto-pick to one platform.
# [targets.device.default]
# platform = "ios"        # optional: "ios" | "android"
# name     = "My iPhone"  # optional: match by device name
# id       = "..."        # optional: exact udid / adb serial
"""

_FLUTTER_SCAFFOLD = """\
# splashdown.toml — Flutter preset.
# Flutter's `flutter run` auto-assigns the Dart VM / DevTools port on each
# launch; there is no equivalent of RN's RCT_METRO_PORT to pin. Splashdown's
# value for Flutter is per-checkout sim/emulator naming.

[project]
workspace = "single"
loader = "__SPLASH_LOADER__"

[apps.main]
path = "."
profile = "flutter"
resources = []

[targets.simulator.default]
model = "iPhone 17"

[targets.emulator.default]
device = "pixel_9"

# Run on a plugged-in phone with `splash run device`. With one device
# connected, auto-pick resolves it — no config needed. Uncomment to pin a
# specific device by id/name, or to scope auto-pick to one platform.
# [targets.device.default]
# platform = "ios"        # optional: "ios" | "android"
# name     = "My iPhone"  # optional: match by device name
# id       = "..."        # optional: exact udid / adb serial
"""

_SERVER_SCAFFOLD = """\
# splashdown.toml — generic web/server preset (Next.js, Django, Rails, FastAPI,
# Spring Boot, etc.). Allocates a free PORT per checkout and a unique DATABASE_URL
# so worktrees don't clobber each other's databases.

[project]
workspace = "single"
loader = "__SPLASH_LOADER__"

[resources.PORT]
type  = "port"
range = [3000, 3100]

[resources.DATABASE_URL]
type     = "template"
template = "postgres://localhost:5432/myapp_{{ slug(cwd) }}"

# Add extra ports as needed, e.g.:
# [resources.STORYBOOK_PORT]
# type  = "port"
# range = [6006, 6100]
"""

_ELECTRON_SCAFFOLD = """\
# splashdown.toml — Electron preset.
# Two per-checkout collisions to solve for parallel Electron dev:
#   1. PORT — the renderer dev server (Vite / Webpack / Parcel / etc.).
#   2. ELECTRON_USER_DATA_DIR — Electron's userData path. By default every
#      instance reads/writes ~/Library/Application Support/<productName>; when
#      two checkouts run side by side they clobber each other's settings,
#      IndexedDB, and SingleInstanceLock. Wire your main process to honour the
#      env var (early, before app.whenReady()):
#         if (process.env.ELECTRON_USER_DATA_DIR) {
#           app.setPath('userData', process.env.ELECTRON_USER_DATA_DIR)
#         }

[project]
workspace = "single"
loader = "__SPLASH_LOADER__"

[resources.PORT]
type  = "port"
range = [3000, 3100]

[resources.ELECTRON_USER_DATA_DIR]
type     = "template"
template = "{{ cwd_abs }}/.electron-userdata"
"""

_IOS_NATIVE_SCAFFOLD = """\
# splashdown.toml — Native iOS preset (Swift/Obj-C + xcodebuild).

[project]
workspace = "single"
loader = "__SPLASH_LOADER__"

[project.ios]
# Required: the Xcode scheme to build.
scheme = "MyApp"
# Optional, defaults shown:
# configuration = "Debug"
# workspace     = "MyApp.xcworkspace"  # auto-detected from root if absent
# project       = "MyApp.xcodeproj"    # auto-detected from root if absent

[apps.main]
path = "."
profile = "ios-native"
resources = []

[targets.simulator.default]
model = "iPhone 17"
"""

_ANDROID_NATIVE_SCAFFOLD = """\
# splashdown.toml — Native Android preset (Kotlin/Java + Gradle).

[project]
workspace = "single"
loader = "__SPLASH_LOADER__"

[project.android]
# Optional, defaults shown:
# module          = "app"
# variant         = "debug"
# application_id  = "com.example.myapp"  # asked from Gradle if not set
# launch_activity = ".MainActivity"      # uses LAUNCHER intent if not set

[apps.main]
path = "."
profile = "android-native"
resources = []

[targets.emulator.default]
device = "pixel_9"
"""


SCAFFOLDS: dict[str, str] = {
    "minimal": _MINIMAL_SCAFFOLD,
    "react-native": _RN_SCAFFOLD,
    "rn": _RN_SCAFFOLD,
    "flutter": _FLUTTER_SCAFFOLD,
    "ios-native": _IOS_NATIVE_SCAFFOLD,
    "android-native": _ANDROID_NATIVE_SCAFFOLD,
    "electron": _ELECTRON_SCAFFOLD,
    "server": _SERVER_SCAFFOLD,
    "nextjs": _SERVER_SCAFFOLD,  # historical alias for server
}
