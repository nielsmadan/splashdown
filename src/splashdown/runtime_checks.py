from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from xml.parsers.expat import ExpatError

from .device_android import _android_bin, _android_home
from .errors import CapabilityError
from .inventory import AppInventory
from .recipe import Recipe
from .runners import _resolve_custom_run, android_gradle_command, runner_prerequisites
from .safe_files import UneditablePath, checkout_controlled_executable
from .wiring import WiringCheck, _js_mask_literals, _js_member, _js_value_end, _strip_js_comments

_LOOPBACK = re.compile(
    r"(?<![\w.:-])(?:localhost\.?|127(?:\.\d{1,3}){3}|\[?::1\]?)(?![\w.-])",
    re.IGNORECASE,
)
_CHECK_TIMEOUT = 3
_PODS_READ_LIMIT = 1024 * 1024


def loopback_warnings(resolved: dict[str, str]) -> list[str]:
    keys = sorted(key for key, value in resolved.items() if _LOOPBACK.search(value))
    if not keys:
        return []
    subject = "Resource" if len(keys) == 1 else "Resources"
    verb = "contains" if len(keys) == 1 else "contain"
    return [
        (
            f"{subject} {', '.join(keys)} {verb} loopback addresses. On a physical device these refer to "
            "the device itself. Use the development machine's reachable LAN address for host "
            "services, or verify that you configured port forwarding."
        )
    ]


def _local_network_description(data: object, source: str) -> list[str]:
    if not isinstance(data, dict):
        return [f"{source}: expected a plist dictionary; local-network description is unverified."]
    description = data.get("NSLocalNetworkUsageDescription")
    if not isinstance(description, str) or not description.strip():
        return [
            (
                f"{source}: add a nonempty NSLocalNetworkUsageDescription explaining the app's "
                "connection to Metro and local services, then rebuild and allow Local Network "
                "access on the iPhone."
            )
        ]
    if "$(" in description or "${" in description:
        return [f"{source}: local-network description uses build variables; verify the built app."]
    return []


def local_network_warnings(app_dir: Path, framework: str) -> list[str]:
    candidates = sorted((app_dir / "ios").glob("*/Info.plist"))
    candidates = [path for path in candidates if path.parent.name not in {"Pods", "build"}]
    if (app_dir / "ios" / "Info.plist").exists():
        candidates.append(app_dir / "ios" / "Info.plist")
    warnings: list[str] = []
    app_plists = 0
    for path in candidates:
        source = str(path.relative_to(app_dir))
        try:
            data = plistlib.loads(path.read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException, ExpatError) as error:
            warnings.append(
                f"{source}: could not read plist ({error}); local network is unverified."
            )
        else:
            if isinstance(data, dict) and (
                "NSExtension" in data or data.get("CFBundlePackageType") in ("BNDL", "FMWK")
            ):
                continue
            app_plists += 1
            warnings.extend(_local_network_description(data, source))
    if app_plists or warnings:
        return warnings
    if framework == "expo":
        if any(app_dir.glob("app.config.*")):
            return [
                (
                    "Expo uses dynamic app config; verify ios.infoPlist.NSLocalNetworkUsageDescription "
                    "in the resolved config and generated iOS app."
                )
            ]
        try:
            data = json.loads((app_dir / "app.json").read_text())
            plist = data.get("expo", {}).get("ios", {}).get("infoPlist", {})
        except (OSError, ValueError, AttributeError):
            return [
                "Could not read Expo app.json; the iOS local-network description is unverified."
            ]
        return _local_network_description(plist, "app.json expo.ios.infoPlist")
    return [
        (
            "No app Info.plist found under ios/; verify NSLocalNetworkUsageDescription in the "
            "built iOS app and allow Local Network access on the iPhone."
        )
    ]


def _watchman_search_path(cwd: Path) -> str:
    return os.pathsep.join(
        str(cwd / entry) if not Path(entry).is_absolute() else entry
        for entry in os.environ.get("PATH", os.defpath).split(os.pathsep)
    )


def watchman_available(cwd: Path | None = None) -> bool:
    return shutil.which("watchman", path=_watchman_search_path(cwd or Path.cwd())) is not None


def watchman_watch_root(cwd: Path) -> tuple[str, str]:
    if not watchman_available(cwd):
        return (
            "skipped",
            "Optional Watchman is absent; active daemon watch roots were not inspected.",
        )
    search_path = _watchman_search_path(cwd)
    candidate = shutil.which("watchman", path=search_path)
    if candidate is None:
        return "incomplete", "Watchman executable lookup failed; watch roots are unverified."
    executable = Path(candidate).resolve()
    if checkout_controlled_executable(Path(candidate), cwd, resolved=executable):
        return (
            "incomplete",
            "Refusing to run a checkout-controlled Watchman executable; watch roots are unverified.",
        )
    if not stat.S_ISREG(executable.stat().st_mode):
        return (
            "incomplete",
            "Watchman is not an executable regular file; watch roots are unverified.",
        )
    try:
        result = subprocess.run(
            ["watchman", "--no-spawn", "--no-local", "watch-list"],
            executable=str(executable),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_CHECK_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (
            "incomplete",
            "Watchman query timed out after 3 seconds; watch roots are unverified",
        )
    except OSError as error:
        return (
            "incomplete",
            f"Watchman query could not start ({error}); watch roots are unverified",
        )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        return (
            "incomplete",
            f"Watchman query exited {result.returncode}: {detail}; daemon state and watch roots are unverified.",
        )
    return _watchman_roots(cwd, result.stdout)


def _watchman_roots(cwd: Path, output: str) -> tuple[str, str]:
    try:
        data = json.loads(output)
    except ValueError:
        data = None
    if not isinstance(data, dict) or "error" in data:
        return (
            "incomplete",
            "Watchman returned an error or invalid data; watch roots are unverified",
        )
    roots = data.get("roots")
    if not isinstance(roots, list) or any(
        not isinstance(root, str) or not root or "\x00" in root or not Path(root).is_absolute()
        for root in roots
    ):
        return ("incomplete", "Watchman returned invalid roots; watch roots are unverified")
    checkout = cwd.resolve()
    ancestors = sorted(
        {str(Path(root).resolve()) for root in roots if Path(root).resolve() in checkout.parents}
    )
    if ancestors:
        return (
            "warning",
            (
                f"Watchman watches an ancestor of this checkout: {', '.join(ancestors)}. "
                "Other checkouts may share these watches. Review their intended scope and users; "
                "use a checkout-scoped watch when isolation is intended. Intentional sharing is valid."
            ),
        )
    return (
        "ok",
        "The inspected Watchman list has no ancestor watch for this checkout; Metro and hot reload are unverified.",
    )


WATCHMAN_CHECK = WiringCheck(
    id="watchman-root",
    description="Watchman watches this checkout or has no watch for it",
    applies=lambda _cwd: True,
    detect=watchman_watch_root,
    autofix=None,
    manual_instructions=None,
)


@dataclass(frozen=True)
class ToolFact:
    available: bool | None
    detail: str


@dataclass(frozen=True)
class RouteTools:
    platform: str
    available: bool | None
    detail: str
    custom: bool = False


@dataclass(frozen=True)
class AppTooling:
    common: tuple[ToolFact, ...]
    routes: tuple[RouteTools, ...]
    guidance: str

    @property
    def ios_optional(self) -> bool:
        return any(route.platform == "android" and route.available for route in self.routes)


def _combined_tools(facts: tuple[ToolFact, ...]) -> ToolFact:
    available = (
        None
        if any(fact.available is None for fact in facts)
        else all(fact.available for fact in facts)
    )
    return ToolFact(available, "; ".join(fact.detail for fact in facts))


def _directory_present(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.stat().st_mode)
    except FileNotFoundError:
        return False


class ToolObservations:
    def __init__(self) -> None:
        self.cache: dict[str, ToolFact] = {}

    def once(self, key: str, probe: Callable[[], ToolFact]) -> ToolFact:
        if key not in self.cache:
            try:
                self.cache[key] = probe()
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                self.cache[key] = ToolFact(None, f"{key}: inspection failed: {error}")
        return self.cache[key]

    def executable(
        self, name: str, *, path: Path | None = None, cwd: Path | None = None
    ) -> ToolFact:
        def inspect() -> ToolFact:
            search_path = os.pathsep.join(
                str(cwd / entry) if cwd is not None and not Path(entry).is_absolute() else entry
                for entry in os.environ.get("PATH", os.defpath).split(os.pathsep)
            )
            candidate = path if path is not None else shutil.which(name, path=search_path)
            if candidate is None:
                return ToolFact(
                    False, f"{name} missing from PATH; install it and expose it on PATH"
                )
            if (
                name == "java"
                and sys.platform == "darwin"
                and Path(candidate).resolve() == Path("/usr/bin/java")
            ):
                return ToolFact(
                    None,
                    "The macOS Java launcher does not establish a JDK installation; configure JAVA_HOME to a JDK. Version compatibility is unverified.",
                )
            try:
                mode = Path(candidate).stat().st_mode
            except FileNotFoundError:
                return ToolFact(False, f"{candidate} missing; install/configure {name}")
            if not stat.S_ISREG(mode) or not os.access(candidate, os.X_OK):
                return ToolFact(False, f"{candidate} is not an executable regular file")
            return ToolFact(True, f"{name}: executable present at {candidate} (not executed)")

        return self.once(f"executable:{path or name}:{cwd}", inspect)

    def ios(self) -> ToolFact:
        def inspect() -> ToolFact:
            if sys.platform != "darwin":
                return ToolFact(
                    False, "iOS builds require macOS and Xcode; this host is unsupported"
                )
            configured = os.environ.get("DEVELOPER_DIR")
            if configured:
                developer = Path(configured)
                if not developer.is_absolute():
                    return ToolFact(
                        None,
                        "Relative DEVELOPER_DIR lookup is unsupported; configure an absolute Xcode path to inspect the selected SDK.",
                    )
                if developer.suffix == ".app":
                    developer /= "Contents/Developer"
            else:
                result = subprocess.run(
                    ["/usr/bin/xcode-select", "--print-path"],
                    cwd="/",
                    capture_output=True,
                    text=True,
                    timeout=_CHECK_TIMEOUT,
                    check=False,
                )
                if result.returncode:
                    return ToolFact(
                        None,
                        f"Selected Xcode path inspection failed: {result.stderr.strip() or result.returncode}",
                    )
                raw = result.stdout.strip()
                if not raw or not Path(raw).is_absolute() or "\n" in raw:
                    raise ValueError("xcode-select returned an invalid developer path")
                developer = Path(raw)
            sdk_dir = developer / "Platforms/iPhoneOS.platform/Developer/SDKs"
            if not _directory_present(sdk_dir) or not any(
                path.name.startswith("iPhoneOS")
                and path.suffix == ".sdk"
                and _directory_present(path)
                for path in sdk_dir.iterdir()
            ):
                return ToolFact(
                    False, f"iOS SDK missing under {developer}; install/select full Xcode"
                )
            return _combined_tools(
                (
                    self.executable("xcodebuild", path=developer / "usr/bin/xcodebuild"),
                    ToolFact(True, f"iOS SDK present under {sdk_dir}"),
                )
            )

        return self.once("ios-sdk", inspect)

    def android(self) -> ToolFact:
        def inspect() -> ToolFact:
            configured = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
            if configured and not Path(configured).is_absolute():
                return ToolFact(
                    None,
                    "Relative Android SDK environment paths are unsupported; configure an absolute ANDROID_HOME or ANDROID_SDK_ROOT to inspect the selected SDK.",
                )
            try:
                home = _android_home()
                adb = Path(_android_bin("adb"))
            except CapabilityError as error:
                return ToolFact(False, str(error))
            missing = [
                name
                for name in ("platforms", "build-tools")
                if not _directory_present(home / name)
                or not any(_directory_present(path) for path in (home / name).iterdir())
            ]
            if missing:
                return ToolFact(
                    False,
                    f"Android SDK at {home} lacks {', '.join(missing)}; install them with Android Studio SDK Manager",
                )
            return _combined_tools(
                (
                    self.executable("adb", path=adb),
                    ToolFact(True, f"Android SDK build directories present at {home}"),
                )
            )

        return self.once("android-sdk", inspect)

    def jdk(self, profile: str, cwd: Path) -> ToolFact:
        if profile == "flutter":
            return ToolFact(
                None,
                "Flutter JDK selection is unverified: configured jdk-dir and Android Studio bundled JDK lookup are unsupported. JAVA_HOME/PATH alone cannot establish the JDK Flutter selects; verify with Flutter yourself.",
            )
        java_home = os.environ.get("JAVA_HOME")
        if java_home and not Path(java_home).is_absolute():
            return ToolFact(
                None,
                "Relative JAVA_HOME lookup is unsupported; configure an absolute JDK path to inspect Java prerequisites.",
            )
        return self.executable(
            "java", path=Path(java_home) / "bin/java" if java_home else None, cwd=cwd
        )


def app_tooling(app: AppInventory, recipe: Recipe, observations: ToolObservations) -> AppTooling:
    prerequisites = runner_prerequisites(app.profile)
    if prerequisites is None:
        return AppTooling(
            (),
            (),
            "No built-in Splashdown launcher prerequisite checks for this profile; custom command dependencies are not inspected.",
        )
    custom = {
        platform: _resolve_custom_run(recipe, platform) is not None
        for platform in prerequisites.platforms
    }
    common = (
        tuple(observations.executable(name, cwd=app.path) for name in prerequisites.common)
        if not all(custom.values())
        else ()
    )
    routes = []
    for platform in prerequisites.platforms:
        if custom[platform]:
            routes.append(
                RouteTools(
                    platform,
                    None,
                    "Custom run dependencies are unsupported; review the command yourself. It was not executed.",
                    True,
                )
            )
            continue
        fact = observations.ios() if platform == "ios" else observations.android()
        if fact.available is not False:
            executable = (
                observations.executable("xcrun", cwd=app.path)
                if platform == "ios"
                else observations.jdk(app.profile, app.path)
            )
            fact = _combined_tools((fact, executable))
        if platform == "ios" and app.profile == "ios-native" and fact.available is not False:
            fact = _combined_tools((fact, observations.executable("xcodebuild", cwd=app.path)))
        if platform == "android" and app.profile == "android-native":
            root = (
                recipe.path.parent
                if recipe.project.get("workspace") == "gradle"
                and (recipe.project.get("android") or {}).get("module")
                else app.path
            )
            command = android_gradle_command(root)
            fact = _combined_tools(
                (
                    fact,
                    observations.executable(
                        command, path=root / "gradlew" if command == "./gradlew" else None, cwd=root
                    ),
                    observations.executable("adb", cwd=root),
                )
            )
        routes.append(RouteTools(platform, fact.available, fact.detail))
    return AppTooling(common, tuple(routes), prerequisites.guidance)


def tooling_checks(
    app: AppInventory, recipe: Recipe, observations: ToolObservations
) -> list[WiringCheck]:
    def facts() -> AppTooling:
        return app_tooling(app, recipe, observations)

    def common(_: Path) -> tuple[str, str]:
        result = facts()
        if not result.common:
            return "skipped", "No common built-in executable requirements for this app."
        fact = _combined_tools(result.common)
        return (
            "incomplete" if fact.available is None else "ok" if fact.available else "problem",
            fact.detail,
        )

    prerequisites = runner_prerequisites(app.profile)
    if prerequisites is None:
        return [
            WiringCheck(
                "tool-coverage",
                "launcher coverage",
                lambda _: True,
                lambda _: ("skipped", facts().guidance),
                None,
                None,
            )
        ]
    checks = [
        WiringCheck(
            "tools-common",
            "common launcher tools",
            lambda _: True,
            common,
            None,
            lambda _: facts().guidance,
        )
    ]
    for platform in prerequisites.platforms:

        def route(_: Path, platform: str = platform) -> tuple[str, str]:
            result = facts()
            selected = next(route for route in result.routes if route.platform == platform)
            if selected.custom:
                return "skipped", selected.detail
            if selected.available is None:
                return "incomplete", selected.detail
            if selected.available:
                return (
                    "ok",
                    f"{platform} executable/SDK prerequisites observed. {selected.detail}. Build, signing, installation and runtime behavior are unverified.",
                )
            known_absent = all(item.available is False for item in result.routes)
            return (
                "problem" if known_absent else "warning",
                f"{platform} route unavailable: {selected.detail}",
            )

        checks.append(
            WiringCheck(
                f"tools-{platform}",
                f"{platform} launcher tools",
                lambda _: True,
                route,
                None,
                lambda _: facts().guidance,
            )
        )
    return checks


def _pods_bytes(path: Path, root: Path) -> bytes | None:
    path, root = Path(os.path.abspath(path)), Path(os.path.abspath(root))
    if not path.is_relative_to(root) or path == root:
        raise UneditablePath(path, f"path is outside checkout {root}")
    current = root
    for part in path.relative_to(root).parts[:-1]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise UneditablePath(path, f"path component {current} is a symlink or not a directory")
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(entry.st_mode):
        raise UneditablePath(path, "input is a symlink or not a regular file")
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (entry.st_dev, entry.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise UneditablePath(path, "input changed during inspection or is not a regular file")
        if opened.st_size > _PODS_READ_LIMIT:
            raise UneditablePath(path, f"input exceeds {_PODS_READ_LIMIT}-byte inspection limit")
        raw = stream.read(_PODS_READ_LIMIT + 1)
        if len(raw) > _PODS_READ_LIMIT:
            raise UneditablePath(path, f"input exceeds {_PODS_READ_LIMIT}-byte inspection limit")
        return raw


def _pods_literal(value: str) -> str:
    match = re.fullmatch(r"\s*([\"'])([^\"'\\\r\n]*)\1\s*", value)
    if match is None or not match[2]:
        raise ValueError("custom native path is not a nonempty static quoted literal")
    return match[2]


def _pods_object(value: str) -> None:
    if not value.startswith("{") or not value.endswith("}"):
        raise ValueError("native configuration is not a static object")
    depth = 0
    for index, char in enumerate(_js_mask_literals(value)):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and index != len(value) - 1:
                raise ValueError("native configuration is a dynamic object expression")
    if depth:
        raise ValueError("unbalanced native configuration object")


def _pods_source_dir(app: AppInventory, root: Path) -> Path | None:
    configs = [
        (path, raw)
        for suffix in ("js", "cjs", "mjs", "ts")
        if (raw := _pods_bytes(path := app.path / f"react-native.config.{suffix}", root))
        is not None
    ]
    if not configs:
        return app.path / "ios"
    if len(configs) != 1:
        raise ValueError("multiple React Native configuration files; native layout is unverified")
    path, raw = configs[0]
    code = _strip_js_comments(raw.decode("utf-8"))
    match = re.match(r"\s*(?:module\.exports\s*=|export\s+default)\s*(\{)", code)
    if match is None:
        raise ValueError(f"{path}: dynamic or unsupported React Native configuration")
    start = match.start(1)
    end = _js_value_end(code, start)
    if code[end:].strip() not in {"", ";"}:
        raise ValueError(f"{path}: configuration has additional executable statements")
    value = code[start:end].strip()
    _pods_object(value)
    if _js_member(value, 0, "platforms") is not None:
        raise ValueError(f"{path}: custom platforms have unsupported native layout coverage")
    for key in ("project", "ios"):
        member = _js_member(value, 0, key)
        if member is None:
            return app.path / "ios"
        value = value[slice(*member)].strip()
        if key == "ios" and value == "null":
            return None
        _pods_object(value)
    source = _js_member(value, 0, "sourceDir")
    return app.path / _pods_literal(value[slice(*source)]) if source else app.path / "ios"


def _pods_locations(app: AppInventory, recipe: Recipe) -> tuple[Path, Path] | None:
    root = recipe.path.parent
    if app.profile in {"react-native", "expo"}:
        native = _pods_source_dir(app, root)
        if native is None:
            return None
    elif app.profile == "flutter":
        native = app.path / "ios"
    else:
        cfg = recipe.project.get("ios") or {}
        configured = cfg.get("workspace") or cfg.get("project")
        native = (app.path / configured).parent if configured else app.path
    if _pods_bytes(native / "Podfile", root) is None:
        return None
    for config in sorted(native.glob("*.xcconfig")):
        raw = _pods_bytes(config, root)
        if raw is not None and re.search(
            r"(?m)^\s*(?:PODS_ROOT|PODS_PODFILE_DIR_PATH)(?:\s|\[|=)", raw.decode("utf-8")
        ):
            raise ValueError(
                f"{config}: CocoaPods path settings require manual verification of the effective Xcode configuration; a standalone xcconfig does not establish active paths"
            )
    return native / "Podfile.lock", native / "Pods" / "Manifest.lock"


def pods_check(
    app: AppInventory, recipe: Recipe, observations: ToolObservations
) -> WiringCheck | None:
    if app.profile not in {"react-native", "expo", "flutter", "ios-native"}:
        return None
    root = recipe.path.parent

    @cache
    def layout() -> tuple[tuple[Path, Path] | None, str | None]:
        try:
            return _pods_locations(app, recipe), None
        except (OSError, ValueError, UnicodeError) as error:
            return None, str(error)

    def detect(_: Path) -> tuple[str, str]:
        locations, layout_error = layout()
        if layout_error is not None:
            return (
                "incomplete",
                f"{app.name}: CocoaPods layout inspection incomplete: {layout_error}",
            )
        if locations is None:
            return (
                "skipped",
                f"{app.name}: no established CocoaPods integration in the configured native scope; Expo may have no generated iOS project. Custom/dynamic layouts require manual verification.",
            )
        lock, manifest = locations
        values: dict[Path, bytes | None] = {}
        errors = []
        for path in locations:
            try:
                values[path] = _pods_bytes(path, root)
            except (OSError, ValueError) as error:
                errors.append(f"{path}: {error}")
        context = f"{app.name}: {lock} and {manifest}"
        if errors:
            return "incomplete", f"{context}: cannot inspect inputs: {'; '.join(errors)}"
        missing = [str(path) for path, value in values.items() if value is None]
        if missing:
            detail = f"missing {', '.join(missing)}; installation is incomplete or has not been performed"
        elif values[lock] != values[manifest]:
            detail = "lock and installed manifest differ; the current CocoaPods installation is inconsistent"
        else:
            return (
                "ok",
                f"{context}: equal bytes establish manifest consistency only. Podfile/dependency changes, freshness and build readiness are unverified.",
            )
        optional = app_tooling(app, recipe, observations).ios_optional
        return (
            "warning" if optional else "problem",
            f"{context}: {detail}. "
            + (
                "iOS is optional for this app because its Android tool route is available. "
                if optional
                else ""
            )
            + "This is the current installation snapshot; ordinary project preparation may repair it.",
        )

    def guidance(_: Path) -> str:
        locations, _error = layout()
        if locations is None:
            return "Verify the app's native source, Podfile, Podfile.lock and installed Pods/Manifest.lock paths manually without executing them through doctor."
        native = locations[0].parent
        if os.environ.get("BUNDLE_GEMFILE"):
            return (
                "Run the project's normal iOS dependency preparation. Verify the Gemfile selected "
                "by BUNDLE_GEMFILE and its CocoaPods setup manually before running pod install."
            )
        bundled = False
        for directory in (native, *native.parents):
            gemfile = _pods_bytes(directory / "Gemfile", root)
            if gemfile is not None:
                bundled = bool(
                    re.search(
                        r"(?m)^\s*gem\s*\(?\s*['\"]cocoapods['\"](?:\s*[,)]|\s*(?:#.*)?$)",
                        gemfile.decode("utf-8"),
                    )
                )
                break
            if directory == root:
                break
        command = "bundle exec pod install" if bundled else "pod install"
        return (
            "Run the project's normal iOS dependency preparation, or "
            f"`cd {shlex.quote(str(native))} && {command}`. "
            "Doctor leaves the installation unchanged, including with --fix. Verify custom/dynamic layouts manually."
        )

    return WiringCheck(
        "pods-manifest", "CocoaPods manifest consistency", lambda _: True, detect, None, guidance
    )
