from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from enum import StrEnum
from pathlib import Path
from typing import Any

from .capabilities import require_macos, translate_tool_errors, warn_capability
from .constants import (
    LOCAL_NAME,
    RECIPE_NAME,
    REGISTRY_DIR,
    TARGET_TYPES,
    TARGET_VARIANT_RE,
)

# DeviceError is defined in errors.py (dependency-free) and re-exported here for
# the many `from .devices import DeviceError` call sites; recipe.py imports it
# straight from errors so it needn't depend on this module. The `as DeviceError`
# marks it an explicit re-export for mypy strict.
from .errors import CapabilityError
from .errors import DeviceError as DeviceError  # noqa: PLC0414 — explicit re-export for mypy
from .recipe import (
    GLOBAL_SKELETON,
    LOCAL_SKELETON,
    GlobalConfig,
    LocalConfig,
    Recipe,
    _current_branch,
    _global_config_path,
    _make_scope,
    render_template,
    validate_target_spec,
)
from .registry import DeviceRow, Registry


def _default_sim_name(cwd: Path, variant: str) -> str:
    """Sim instance name: '<parent>/<basename>/<variant>'. The path component
    keeps different worktrees / clones isolated; the variant suffix lets the
    same checkout host multiple sim configs (default, lowest-supported, etc.)."""
    return f"{cwd.parent.name}/{cwd.name}/{variant}"


_AVD_INVALID_RE = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize_avd_name(name: str) -> str:
    """avdmanager rejects names containing characters outside [A-Za-z0-9._-].
    Replace anything else with `_` so the default `<parent>/<basename>/<variant>`
    scheme works on Android too."""
    return _AVD_INVALID_RE.sub("_", name)


def _resolve_device_name(
    spec: dict[str, Any], cwd: Path, variant: str, dtype: str | None = None
) -> str:
    """Sim/AVD name: explicit `name` field on the variant (string or template),
    otherwise the path-derived default. For emulator, sanitize the
    result (avdmanager allows only [A-Za-z0-9._-])."""
    raw = spec.get("name")
    if not raw:
        name = _default_sim_name(cwd, variant)
    elif isinstance(raw, str) and "{{" in raw:
        scope = _make_scope(cwd, _current_branch(cwd), {})
        name = render_template(raw, scope)
    else:
        name = str(raw)
    if name.startswith("-"):
        # A leading dash would be read as a flag by simctl/avdmanager create.
        raise DeviceError(f"device name must not start with '-': {name!r}")
    if dtype == "emulator":
        return _sanitize_avd_name(name)
    return name


def _xcrun_json(args: list[str]) -> Any:
    require_macos("simulator support")
    try:
        with translate_tool_errors("ios", "xcrun", "install Xcode command-line tools"):
            out = subprocess.check_output(["xcrun", *args], stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError as e:
        raise DeviceError(f"xcrun {' '.join(args)} failed: exit {e.returncode}") from e
    return json.loads(out)


def _ios_find_device_by_name(name: str) -> tuple[str, str] | None:
    """Returns (udid, state) for the named simulator, or None."""
    data = _xcrun_json(["simctl", "list", "devices", "-j"])
    for _runtime, devs in (data.get("devices") or {}).items():
        for d in devs:
            if d.get("name") == name and d.get("isAvailable"):
                return d.get("udid", ""), d.get("state", "")
    return None


def _ios_runtimes() -> list[dict[str, Any]]:
    """Available iOS runtimes, oldest version first."""
    data = _xcrun_json(["simctl", "list", "runtimes", "-j"])
    runtimes = [r for r in (data.get("runtimes") or []) if r.get("isAvailable")]
    if not runtimes:
        raise DeviceError("no available iOS runtimes; install one in Xcode")
    runtimes.sort(key=lambda r: _version_tuple(r.get("version", "0")))
    return runtimes


def _ios_latest_runtime() -> str:
    """Latest available iOS runtime identifier."""
    return str(_ios_runtimes()[-1].get("identifier", ""))


def _ios_latest_runtime_version() -> str:
    """Latest available iOS runtime version string, e.g. '18.5'. Drives auto-upgrade."""
    return str(_ios_runtimes()[-1].get("version", ""))


def _ios_runtime_models(runtime_id: str) -> list[str]:
    """iPhone model names the runtime can actually instantiate, newest first.
    Device types are per-runtime: `iPhone 17` exists only from iOS 26 on, so a
    model/runtime pair valid in isolation can still be rejected by simctl."""
    try:
        runtimes = _ios_runtimes()
    except DeviceError:
        return []
    for r in runtimes:
        if r.get("identifier") != runtime_id:
            continue
        supported = r.get("supportedDeviceTypes") or []
        return [str(t.get("name", "")) for t in supported if "iPhone" in str(t.get("name", ""))]
    return []


def ios_x86_64_target() -> tuple[str, str] | None:
    """(version, model) for the newest installed runtime carrying an x86_64 slice,
    or None if every installed runtime is arm64-only. Apple dropped x86_64 from the
    iOS 26 runtimes, so this is what an app whose pods exclude arm64 for the
    simulator has to pin — see `_rn_ios_arch_hint` in `runners.py`."""
    try:
        runtimes = _ios_runtimes()
    except DeviceError:
        return None
    for r in reversed(runtimes):
        if "x86_64" not in (r.get("supportedArchitectures") or []):
            continue
        models = _ios_runtime_models(str(r.get("identifier", "")))
        pro = next((m for m in models if m.endswith(" Pro")), None)
        model = pro or (models[0] if models else None)
        if model:
            return str(r.get("version", "")), model
    return None


def _version_tuple(s: str) -> tuple[int, ...]:
    """Sort '18.5' / '19.0' / '17.0' as version numbers, not strings."""
    try:
        return tuple(int(p) for p in s.split("."))
    except ValueError:
        return (0,)


def _ios_udid_exists(udid: str) -> bool:
    """Is `udid` known to xcrun simctl right now?"""
    try:
        data = _xcrun_json(["simctl", "list", "devices", "-j"])
    except CapabilityError:
        raise
    except DeviceError:
        return False
    for devs in (data.get("devices") or {}).values():
        for d in devs:
            if d.get("udid") == udid:
                return True
    return False


def _ios_runtime_identifier(version: str) -> str:
    """`18.5` -> `com.apple.CoreSimulator.SimRuntime.iOS-18-5`."""
    return f"com.apple.CoreSimulator.SimRuntime.iOS-{version.replace('.', '-')}"


def _ios_device_type_identifier(model: str | None) -> str:
    """Prefer the user's named device, else the latest iPhone Pro."""
    data = _xcrun_json(["simctl", "list", "devicetypes", "-j"])
    types = data.get("devicetypes") or []
    if model:
        for t in types:
            if t.get("name") == model:
                return str(t.get("identifier", ""))
        raise DeviceError(
            f"unknown iOS device model `{model}` — try `xcrun simctl list devicetypes`"
        )
    pros = [t for t in types if re.search(r"iPhone.*Pro$", t.get("name", ""))]
    if not pros:
        raise DeviceError("no iPhone Pro device types found; specify `model = ...` explicitly")
    pros.sort(key=lambda t: t.get("name", ""))
    return str(pros[-1].get("identifier", ""))


def ios_ensure(name: str, model: str | None, ios_version: str | None) -> tuple[str, str]:
    """Find-or-create sim. Returns (udid, state)."""
    require_macos("simulator support")
    existing = _ios_find_device_by_name(name)
    if existing:
        return existing
    runtime = _ios_runtime_identifier(ios_version) if ios_version else _ios_latest_runtime()
    device_type = _ios_device_type_identifier(model)
    print(f"creating iOS sim '{name}' ({device_type} on {runtime})", file=sys.stderr)
    try:
        with translate_tool_errors("ios", "xcrun", "install Xcode command-line tools"):
            udid = (
                subprocess.check_output(
                    ["xcrun", "simctl", "create", name, device_type, runtime],
                    stderr=subprocess.PIPE,
                )
                .decode()
                .strip()
            )
    except subprocess.CalledProcessError as e:
        detail = e.stderr.decode().strip()
        raise DeviceError(
            f"simctl create failed: {detail}{_ios_create_hint(model, ios_version, runtime)}"
        ) from e
    return udid, "Shutdown"


_MODELS_SHOWN = 8


def _ios_create_hint(model: str | None, ios_version: str | None, runtime: str) -> str:
    """Translate simctl's opaque `Incompatible device` into the pairing that is
    actually wrong. Empty string when the model does look creatable on that runtime,
    so an unrelated create failure isn't given a misleading explanation."""
    if not model:
        return ""
    models = _ios_runtime_models(runtime)
    if not models or model in models:
        return ""
    where = f"iOS {ios_version}" if ios_version else runtime
    shown, rest = models[:_MODELS_SHOWN], models[_MODELS_SHOWN:]
    return (
        f"\n  `{model}` is not a device type {where} can create."
        f"\n  Models it has: {', '.join(shown)}{', ...' if rest else ''}"
        f"\n  Set a compatible `model` under [targets.simulator.<variant>]."
    )


def ios_boot(udid: str, state: str) -> None:
    require_macos("simulator support")
    if state == "Booted":
        return
    with translate_tool_errors("ios", "xcrun", "install Xcode command-line tools"):
        proc = subprocess.run(
            ["xcrun", "simctl", "boot", udid], capture_output=True, text=True, check=False
        )
    # A concurrent boot can flip the state between our check and this call; simctl
    # reports that benign race as a non-zero "current state: Booted".
    if proc.returncode != 0 and "current state: Booted" not in proc.stderr:
        raise DeviceError(
            f"simctl boot failed for {udid}: {proc.stderr.strip() or proc.returncode}"
        )
    with translate_tool_errors("ios", "open", "restore the macOS open command"):
        subprocess.run(["open", "-a", "Simulator"], check=False)


def ios_shutdown(udid: str) -> None:
    require_macos("simulator support")
    # simctl errors with code 405 if the sim is already Shutdown — noisy and
    # useless. Skip the call entirely when there's nothing to do.
    if _ios_current_state(udid) == "Shutdown":
        return
    with translate_tool_errors("ios", "xcrun", "install Xcode command-line tools"):
        subprocess.run(["xcrun", "simctl", "shutdown", udid], check=False)


def ios_destroy(udid: str) -> None:
    require_macos("simulator support")
    with translate_tool_errors("ios", "xcrun", "install Xcode command-line tools"):
        proc = subprocess.run(
            ["xcrun", "simctl", "delete", udid],
            capture_output=True,
            text=True,
            check=False,
        )
    if proc.returncode != 0:
        raise DeviceError(
            f"simctl delete failed for {udid}: {proc.stderr.strip() or proc.returncode}"
        )


def _android_home() -> Path:
    h = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if h and Path(h).exists():
        return Path(h)
    candidates = [
        Path.home() / "Library/Android/sdk",  # macOS default
        Path.home() / "Android/Sdk",  # Linux default (Android Studio)
    ]
    for c in candidates:
        if c.exists():
            return c
    raise CapabilityError(
        "android",
        "Android SDK not found; set ANDROID_HOME or ANDROID_SDK_ROOT",
    )


def _android_bin(name: str) -> str:
    h = _android_home()
    candidates = [
        h / "cmdline-tools" / "latest" / "bin" / name,
        h / "tools" / "bin" / name,
        h / "emulator" / name,
        h / "platform-tools" / name,
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    raise CapabilityError(
        "android",
        f"{name} not found under {h}; install Android SDK command-line tools",
    )


def _android_avd_exists(name: str) -> bool:
    try:
        with translate_tool_errors(
            "android", "avdmanager", "install Android SDK command-line tools"
        ):
            out = subprocess.check_output(
                [_android_bin("avdmanager"), "list", "avd", "-c"],
                stderr=subprocess.DEVNULL,
            )
    except subprocess.CalledProcessError:
        return False
    return name in [line.strip() for line in out.decode().splitlines()]


def _is_orphan_device(row: DeviceRow) -> bool:
    """A registered device whose underlying sim/AVD no longer exists. Happens
    when the user runs `xcrun simctl delete` or `avdmanager delete avd` by
    hand, leaving the registry pointing at a ghost."""
    if row.dtype == "simulator":
        return not _ios_udid_exists(row.udid)
    if row.dtype == "emulator":
        return not _android_avd_exists(row.udid)
    return False


def _android_latest_image() -> str:
    """Pick a sensible default system image. Prefers installed; falls back to a known-good name."""
    sdkmgr = _android_bin("sdkmanager")
    try:
        with translate_tool_errors(
            "android", "sdkmanager", "install Android SDK command-line tools"
        ):
            out = subprocess.check_output(
                [sdkmgr, "--list_installed"], stderr=subprocess.DEVNULL
            ).decode()
    except subprocess.CalledProcessError:
        out = ""
    installed = re.findall(r"^\s*(system-images;android-\d+;[^\s|]+)", out, re.M)
    if installed:

        def _api_level(s: str) -> int:
            m = re.search(r"android-(\d+)", s)
            return int(m.group(1)) if m else 0

        installed.sort(key=_api_level, reverse=True)
        return str(installed[0])
    return "system-images;android-34;google_apis;arm64-v8a"


def android_ensure(name: str, device: str | None, image: str | None) -> str:
    """Find-or-create AVD. Returns AVD name (which is the identifier)."""
    if _android_avd_exists(name):
        return name
    image = image or _android_latest_image()
    device = device or "pixel_9"
    print(f"creating Android AVD '{name}' (device={device}, image={image})", file=sys.stderr)
    avdmgr = _android_bin("avdmanager")
    with translate_tool_errors("android", "avdmanager", "install Android SDK command-line tools"):
        proc = subprocess.run(
            [avdmgr, "create", "avd", "-n", name, "-k", image, "-d", device, "--force"],
            input=b"\n",  # answer "no" to "create custom hardware profile?"
            capture_output=True,
            check=False,
        )
    if proc.returncode != 0:
        raise DeviceError(f"avdmanager create failed: {proc.stderr.decode().strip()}")
    return name


# `adb devices` rows are "<serial>\t<state>"; both columns must be present.
_ADB_ROW_COLS = 2


def _android_running_serial(avd_name: str) -> str | None:
    """Match a running emulator to an AVD via `adb -s <serial> emu avd name`."""
    adb = _android_bin("adb")
    try:
        with translate_tool_errors("android", "adb", "install Android SDK platform-tools"):
            out = subprocess.check_output([adb, "devices"], stderr=subprocess.DEVNULL).decode()
    except subprocess.CalledProcessError:
        return None
    for line in out.splitlines()[1:]:
        parts = line.split()
        if (
            len(parts) >= _ADB_ROW_COLS
            and parts[0].startswith("emulator-")
            and parts[1] == "device"
        ):
            serial = parts[0]
            try:
                with translate_tool_errors("android", "adb", "install Android SDK platform-tools"):
                    got = (
                        subprocess.check_output(
                            [adb, "-s", serial, "emu", "avd", "name"],
                            stderr=subprocess.DEVNULL,
                            timeout=2,
                        )
                        .decode()
                        .splitlines()
                    )
                if got and got[0].strip() == avd_name:
                    return serial
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                continue
    return None


def android_boot(avd_name: str) -> str:
    """Start emulator in background. Returns its adb serial once it appears."""
    serial = _android_running_serial(avd_name)
    if serial:
        return serial
    emu = _android_bin("emulator")
    from .recipe import _slug as recipe_slug  # noqa: PLC0415

    log = REGISTRY_DIR / f"emulator-{recipe_slug(avd_name)}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    print(f"booting Android AVD '{avd_name}' (log: {log})", file=sys.stderr)
    with (
        log.open("ab") as f,
        translate_tool_errors("android", "emulator", "install the Android SDK emulator"),
    ):
        subprocess.Popen(
            [emu, "-avd", avd_name],
            stdout=f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    for _ in range(60):
        time.sleep(1)
        serial = _android_running_serial(avd_name)
        if serial:
            return serial
    raise DeviceError(f"AVD '{avd_name}' did not come up within 60s; see {log}")


def android_shutdown(avd_name: str) -> None:
    serial = _android_running_serial(avd_name)
    if not serial:
        return
    with translate_tool_errors("android", "adb", "install Android SDK platform-tools"):
        subprocess.run([_android_bin("adb"), "-s", serial, "emu", "kill"], check=False)


def android_destroy(avd_name: str) -> None:
    with translate_tool_errors("android", "avdmanager", "install Android SDK command-line tools"):
        proc = subprocess.run(
            [_android_bin("avdmanager"), "delete", "avd", "-n", avd_name],
            capture_output=True,
            text=True,
            check=False,
        )
    if proc.returncode != 0:
        raise DeviceError(
            f"avdmanager delete failed for {avd_name}: {proc.stderr.strip() or proc.returncode}"
        )


# Physical hardware is the odd one out: splashdown can't create, boot, or destroy
# it. The only operation that makes sense is *discovery* — list what's plugged in
# and hand its native identifier (iOS udid / adb serial) to the framework's
# launcher, exactly the id `flutter run -d`, `run-ios --udid`, etc. already expect.


def _devicectl_json(args: list[str]) -> Any:
    """Run `xcrun devicectl … --json-output -` and parse stdout. devicectl ships
    with Xcode 15+; raise a pointed error when it's missing or fails."""
    require_macos("physical-device support")
    try:
        with translate_tool_errors("ios", "xcrun", "install Xcode command-line tools"):
            out = subprocess.check_output(
                ["xcrun", "devicectl", *args, "--json-output", "-"],
                stderr=subprocess.DEVNULL,
            )
    except subprocess.CalledProcessError as e:
        raise DeviceError(
            "xcrun devicectl failed (needs Xcode 15+ for physical-device support); "
            f"exit {e.returncode}"
        ) from e
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise DeviceError(f"could not parse devicectl output: {e}") from e


def _ios_physical_devices() -> list[dict[str, str]]:
    """Paired physical iOS devices as {id (udid), name, platform: 'ios'}.

    Gate on `pairingState == paired` (devices set up to run on from this Mac)
    rather than `tunnelState == connected`: wifi devices routinely sit at
    tunnelState `disconnected` and only establish a tunnel on demand at launch,
    so requiring `connected` would hide every wireless device. `unavailable`
    tunnels (the macOS host, long-gone pairings) are excluded."""
    data = _devicectl_json(["list", "devices"])
    devices = (data.get("result") or {}).get("devices") or []
    out: list[dict[str, str]] = []
    for d in devices:
        hw = d.get("hardwareProperties") or {}
        if (hw.get("platform") or "").lower() != "ios":
            continue
        conn = d.get("connectionProperties") or {}
        if (conn.get("pairingState") or "").lower() != "paired":
            continue
        if (conn.get("tunnelState") or "").lower() == "unavailable":
            continue
        udid = hw.get("udid") or ""
        if not udid:
            continue
        name = (d.get("deviceProperties") or {}).get("name") or udid
        out.append({"id": udid, "name": name, "platform": "ios"})
    return out


def _android_physical_devices() -> list[dict[str, str]]:
    """Connected physical Android devices as {id (serial), name, platform: 'android'}.
    Emulators (serials starting `emulator-`) are excluded — those are the
    `emulator` device type."""
    adb = _android_bin("adb")
    try:
        with translate_tool_errors("android", "adb", "install Android SDK platform-tools"):
            out = subprocess.check_output(
                [adb, "devices", "-l"], stderr=subprocess.DEVNULL
            ).decode()
    except subprocess.CalledProcessError as e:
        raise DeviceError(f"adb devices failed: exit {e.returncode}") from e
    devices: list[dict[str, str]] = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < _ADB_ROW_COLS or parts[1] != "device":
            continue
        serial = parts[0]
        if serial.startswith("emulator-"):
            continue
        model = next((p.split(":", 1)[1] for p in parts[2:] if p.startswith("model:")), serial)
        devices.append({"id": serial, "name": model, "platform": "android"})
    return devices


def physical_discover(
    platform: str | None = None, *, warned: set[str] | None = None
) -> list[dict[str, str]]:
    """All connected physical devices, optionally scoped to one platform. When
    scanning broadly (platform=None) a missing toolchain for one platform is
    tolerated — an Android-only dev without Xcode still discovers their phone.
    An explicitly requested platform propagates discovery errors."""
    _ios = _ios_physical_devices
    _android = _android_physical_devices
    out: list[dict[str, str]] = []
    warning_keys = warned if warned is not None else set()
    if platform in (None, "ios"):
        try:
            out += _ios()
        except CapabilityError as error:
            if platform == "ios":
                raise
            warn_capability(error, warning_keys)
    if platform in (None, "android"):
        try:
            out += _android()
        except CapabilityError as error:
            if platform == "android":
                raise
            warn_capability(error, warning_keys)
    return out


def _physical_match(
    spec: dict[str, Any], *, warned: set[str] | None = None
) -> list[dict[str, str]]:
    """Discover and filter physical devices by a variant spec's platform/id/name."""
    devices = physical_discover(spec.get("platform"), warned=warned)
    want_id = spec.get("id")
    want_name = spec.get("name")
    if want_id:
        devices = [d for d in devices if d["id"] == want_id]
    elif want_name:
        needle = str(want_name).lower()
        devices = [d for d in devices if needle in d["name"].lower()]
    return devices


def ensure_physical(spec: dict[str, Any], *, warned: set[str] | None = None) -> dict[str, str]:
    """Resolve a `device` target (physical hardware) to a connected device.
    Auto-picks the lone device; `id`/`name`/`platform` on the spec narrow the
    selection. Returns the same `info` shape as the sim/emulator path, plus
    `physical: True`."""
    devices = _physical_match(spec, warned=warned)
    if not devices:
        raise DeviceError(_physical_no_match_msg(spec))
    if len(devices) > 1:
        listing = ", ".join(f"{d['name']} ({d['id']})" for d in devices)
        raise DeviceError(
            f"multiple connected physical devices ({listing}); narrow with "
            "`id`/`name`/`platform` on the variant"
        )
    d = devices[0]
    info: dict[str, Any] = {
        "kind": d["platform"] if d["platform"] == "ios" else "android",
        "name": d["name"],
        "physical": True,
    }
    info["udid" if d["platform"] == "ios" else "serial"] = d["id"]
    return info


def _physical_no_match_msg(spec: dict[str, Any]) -> str:
    bits = [f"{k}={spec[k]!r}" for k in ("platform", "id", "name") if spec.get(k)]
    qualifier = f" matching {', '.join(bits)}" if bits else ""
    return (
        f"no connected physical device{qualifier}; plug one in and unlock it "
        "(iOS: trust this computer; Android: enable USB debugging)"
    )


def physical_status(spec: dict[str, Any], *, warned: set[str] | None = None) -> str:
    """Liveness for `splash targets`: connected / absent / ambiguous."""
    devices = _physical_match(spec, warned=warned)
    if not devices:
        return "absent"
    if len(devices) > 1:
        return "ambiguous"
    return "connected"


def _latest_os_for(dtype: str, cache: dict[str, str] | None) -> str:
    """Latest iOS runtime version / Android system image. Each shells out, so a
    shared `cache` (keyed by platform) resolves it at most once per command run."""
    key = "ios" if dtype == "simulator" else "android"
    if cache is not None and key in cache:
        return cache[key]
    value = _ios_latest_runtime_version() if dtype == "simulator" else _android_latest_image()
    if cache is not None:
        cache[key] = value
    return value


def _target_os_for(dtype: str, spec: dict[str, Any], cache: dict[str, str] | None) -> str:
    """The OS the variant should be on: its pinned value, or the current latest."""
    requested = spec.get("ios" if dtype == "simulator" else "image", "latest")
    return _latest_os_for(dtype, cache) if requested == "latest" else requested


class DeviceHealth(StrEnum):
    HEALTHY = "healthy"
    MISSING = "missing"
    ORPHAN = "orphan"
    DRIFTED = "drifted"
    UNDECLARED = "undeclared"


def device_health(
    registry: Registry,
    cwd: Path,
    dtype: str,
    variant: str,
    spec: dict[str, Any] | None,
    *,
    cache: dict[str, str] | None = None,
) -> DeviceHealth:
    if dtype == "device":
        return DeviceHealth.UNDECLARED if spec is None else DeviceHealth.HEALTHY
    row = registry.get_device(str(cwd.resolve()), dtype, variant)
    if row is not None and _is_orphan_device(row):
        return DeviceHealth.ORPHAN
    if spec is None:
        return DeviceHealth.UNDECLARED
    if row is None:
        return DeviceHealth.MISSING
    target = _target_os_for(dtype, spec, cache)
    if dtype == "simulator":
        drifted = row.ios != target or row.model != spec.get("model", "")
    elif dtype == "emulator":
        resolved_name = _resolve_device_name(spec, cwd, variant, dtype)
        drifted = (
            row.udid != resolved_name or row.ios != target or row.model != spec.get("device", "")
        )
    else:
        raise DeviceError(f"unknown target type `{dtype}`")
    return DeviceHealth.DRIFTED if drifted else DeviceHealth.HEALTHY


def device_needs_recreate(
    registry: Registry,
    cwd: Path,
    dtype: str,
    variant: str,
    spec: dict[str, Any],
    *,
    cache: dict[str, str] | None = None,
) -> bool:
    """Whether `ensure_fresh_sim` would destroy+recreate this device: no registry
    row, the sim/AVD is gone, or its OS/model has drifted from the variant spec.
    The single source of truth for staleness — the actuator (`ensure_fresh_sim`)
    and the `target refresh` counter both call it, so they can never disagree."""
    return (
        device_health(registry, cwd, dtype, variant, spec, cache=cache) is not DeviceHealth.HEALTHY
    )


def ensure_fresh_sim(
    registry: Registry,
    cwd: Path,
    dtype: str,
    variant: str,
    spec: dict[str, Any],
    *,
    cache: dict[str, str] | None = None,
) -> dict[str, str]:
    """Reconcile a sim/AVD instance against the variant spec. Destroys + recreates
    if the OS image (or model) has drifted from what's in the registry. Pinned
    variants (`ios = "<explicit>"`) are kept on their declared version forever."""
    if dtype == "device":
        return ensure_physical(spec)

    checkout = str(cwd.resolve())
    sim_name = _resolve_device_name(spec, cwd, variant, dtype)
    stale = device_needs_recreate(registry, cwd, dtype, variant, spec, cache=cache)
    row = registry.get_device(checkout, dtype, variant)

    if dtype == "simulator":
        target_ios = _target_os_for(dtype, spec, cache)
        model_spec = spec.get("model", "")
        if not stale:
            if row is None:  # unreachable (stale is True when row is None); narrows the type
                raise DeviceError("internal: simulator row vanished mid-check")
            return {"kind": "ios", "udid": row.udid, "name": sim_name}
        if row is not None:
            device_destroy_row(row)
        udid, _state = ios_ensure(sim_name, model_spec or None, target_ios)
        registry.set_device(checkout, dtype, variant, udid, model_spec, target_ios)
        return {"kind": "ios", "udid": udid, "name": sim_name}

    if dtype == "emulator":
        target_image = _target_os_for(dtype, spec, cache)
        device_spec = spec.get("device", "")
        if not stale:
            return {"kind": "android", "serial": "", "name": sim_name}
        if row is not None:
            device_destroy_row(row)
        android_ensure(sim_name, device_spec or None, target_image)
        registry.set_device(checkout, dtype, variant, sim_name, device_spec, target_image)
        return {"kind": "android", "serial": "", "name": sim_name}

    raise DeviceError(f"unknown target type `{dtype}`")


def device_status(dtype: str, resolved_name: str) -> str:
    if dtype == "simulator":
        found = _ios_find_device_by_name(resolved_name)
        if not found:
            return "absent"
        return found[1].lower()  # 'booted' / 'shutdown'
    if dtype == "emulator":
        if not _android_avd_exists(resolved_name):
            return "absent"
        return "running" if _android_running_serial(resolved_name) else "stopped"
    raise DeviceError(f"unknown target type `{dtype}`")


def device_shutdown(dtype: str, resolved_name: str) -> None:
    if dtype == "simulator":
        found = _ios_find_device_by_name(resolved_name)
        if found:
            ios_shutdown(found[0])
    elif dtype == "emulator":
        android_shutdown(resolved_name)


def device_destroy(dtype: str, resolved_name: str) -> None:
    if dtype == "simulator":
        found = _ios_find_device_by_name(resolved_name)
        if found:
            ios_shutdown(found[0])
            ios_destroy(found[0])
    elif dtype == "emulator":
        if not _android_avd_exists(resolved_name):
            return
        android_shutdown(resolved_name)
        android_destroy(resolved_name)


def device_destroy_row(row: DeviceRow) -> None:
    """Destroy the sim/AVD a registry row points at, using the identifier the
    row actually stores: the real UDID for simulators (set_device stores the
    UDID in the udid column), the AVD name for emulators. Unlike `device_destroy`
    this needs no by-name lookup, so it also reaches orphaned instances whose
    recipe variant is gone."""
    if row.dtype == "simulator":
        if not _ios_udid_exists(row.udid):
            return
        ios_shutdown(row.udid)
        ios_destroy(row.udid)
    elif row.dtype == "emulator":
        if not _android_avd_exists(row.udid):
            return
        android_shutdown(row.udid)
        android_destroy(row.udid)


def _ios_current_state(udid: str) -> str:
    """'Booted' / 'Shutdown' / 'Unknown' for the given UDID."""
    try:
        data = _xcrun_json(["simctl", "list", "devices", "-j"])
    except CapabilityError:
        raise
    except DeviceError:
        return "Unknown"
    for devs in (data.get("devices") or {}).values():
        for d in devs:
            if d.get("udid") == udid:
                return str(d.get("state", "Unknown"))
    return "Unknown"


def _device_status_for_row(row: DeviceRow) -> str:
    """Liveness/state for a registry device row. Reuses udid for iOS lookup
    and the AVD name for Android (the `udid` column doubles as the AVD name
    for emulator rows)."""
    if row.dtype == "simulator":
        if not _ios_udid_exists(row.udid):
            return "absent"
        return _ios_current_state(row.udid).lower()
    if row.dtype == "emulator":
        if not _android_avd_exists(row.udid):
            return "absent"
        return "running" if _android_running_serial(row.udid) else "stopped"
    return "unknown"


def _short_path(abspath: str) -> str:
    """`/Users/x/wrksp/y` → `~/wrksp/y` when under $HOME; else the full path
    unchanged. Predictable rule, used by the `splash status --all` table."""
    home = str(Path.home())
    if abspath == home:
        return "~"
    if abspath.startswith(home + "/"):
        return "~" + abspath[len(home) :]
    return abspath


# Order + labels for the `splash status --all` summary column. Singular/plural
# forms are spelled out so the formatter stays a pure mapping.
_SUMMARY_PARTS = (
    ("port", "port", "ports"),
    ("kv", "var", "vars"),
    ("simulator", "sim", "sims"),
    ("emulator", "emu", "emus"),
)


def _summary_string(counts: dict[str, int]) -> str:
    """Human-friendly count summary: `2 ports, 1 var, 1 sim`. Empty → `—`."""
    parts: list[str] = []
    for key, sing, plur in _SUMMARY_PARTS:
        n = counts.get(key, 0)
        if n == 1:
            parts.append(f"1 {sing}")
        elif n > 1:
            parts.append(f"{n} {plur}")
    return ", ".join(parts) if parts else "—"


def _load_recipe_or_empty(cwd: Path) -> Recipe:
    path = cwd / RECIPE_NAME
    return Recipe.load(path) if path.exists() else Recipe({}, path)


def _validate_target_fields(
    dtype: str,
    variant: str,
    fields: dict[str, str | None],
) -> dict[str, str]:
    try:
        return validate_target_spec(
            dtype,
            fields,
            source="command line",
            path=f"targets.{dtype}.{variant}",
        )
    except ValueError as error:
        raise DeviceError(str(error)) from error


def target_add(cwd: Path, dtype: str, variant: str, fields: dict[str, str | None]) -> None:
    """Append a [targets.<type>.<variant>] table to splashdown.local.toml. Errors
    if the (type, variant) pair already exists in either the recipe or the local
    file — pick a different variant name."""
    if dtype not in TARGET_TYPES:
        raise DeviceError(f"target type `{dtype}` must be one of: {', '.join(TARGET_TYPES)}")
    if not TARGET_VARIANT_RE.match(variant):
        raise DeviceError(f"variant `{variant}` must match [A-Za-z][A-Za-z0-9_-]*")
    validated_fields = _validate_target_fields(dtype, variant, fields)

    path = cwd / LOCAL_NAME
    existing_text = path.read_text() if path.exists() else LOCAL_SKELETON

    recipe = _load_recipe_or_empty(cwd)
    local = LocalConfig.load(path)
    if variant in recipe.targets.get(dtype, {}):
        raise DeviceError(
            f"target `{dtype}.{variant}` is declared in the recipe; "
            f"edit {RECIPE_NAME} or pick a different variant name"
        )
    if variant in local.targets.get(dtype, {}):
        raise DeviceError(
            f"target `{dtype}.{variant}` already exists in {LOCAL_NAME}; remove it first"
        )

    from .tomlio import target_add_text  # noqa: PLC0415

    rendered = target_add_text(existing_text, dtype, variant, validated_fields)
    LocalConfig.parse(rendered, path)
    path.write_text(rendered)


def _prepare_target_remove(cwd: Path, dtype: str, variant: str) -> tuple[dict[str, Any], Path, str]:
    recipe = _load_recipe_or_empty(cwd)
    if variant in recipe.targets.get(dtype, {}):
        raise DeviceError(
            f"`{dtype}.{variant}` is declared in the recipe; edit {RECIPE_NAME} to remove it"
        )
    path = cwd / LOCAL_NAME
    if not path.exists():
        raise DeviceError(f"no target `{dtype}.{variant}` in {LOCAL_NAME}")
    spec = LocalConfig.load(path).targets.get(dtype, {}).get(variant)
    if spec is None:
        raise DeviceError(f"no target `{dtype}.{variant}` in {LOCAL_NAME}")
    from .tomlio import target_remove_text  # noqa: PLC0415

    new_text = target_remove_text(path.read_text(), dtype, variant)
    if new_text is None:
        raise DeviceError(f"no target `{dtype}.{variant}` in {LOCAL_NAME}")
    return spec, path, new_text


def target_remove(cwd: Path, dtype: str, variant: str) -> None:
    """Delete the [targets.<type>.<variant>] table from splashdown.local.toml.
    Refuses to touch recipe-declared variants (those you remove by editing the recipe)."""
    _spec, path, new_text = _prepare_target_remove(cwd, dtype, variant)
    path.write_text(new_text)


def global_target_add(dtype: str, variant: str, fields: dict[str, str | None]) -> Path:
    """Append a [targets.<type>.<variant>] table to the machine-wide config
    (~/.config/splashdown/config.toml), making the variant available to every
    project. Errors if the (type, variant) pair already exists there. Returns the
    config path so the caller can name it in a message."""
    if dtype not in TARGET_TYPES:
        raise DeviceError(f"target type `{dtype}` must be one of: {', '.join(TARGET_TYPES)}")
    if not TARGET_VARIANT_RE.match(variant):
        raise DeviceError(f"variant `{variant}` must match [A-Za-z][A-Za-z0-9_-]*")
    validated_fields = _validate_target_fields(dtype, variant, fields)

    path = _global_config_path()
    existing_text = path.read_text() if path.exists() else GLOBAL_SKELETON
    if variant in GlobalConfig.load(path).targets.get(dtype, {}):
        raise DeviceError(f"target `{dtype}.{variant}` already exists in {path}; remove it first")

    from .tomlio import target_add_text  # noqa: PLC0415

    rendered = target_add_text(existing_text, dtype, variant, validated_fields)
    GlobalConfig.parse(rendered, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered)
    return path


def global_target_remove(dtype: str, variant: str) -> Path:
    """Delete the [targets.<type>.<variant>] table from the machine-wide config.
    Returns the config path."""
    from .tomlio import target_remove_text  # noqa: PLC0415

    path = _global_config_path()
    if not path.exists() or variant not in GlobalConfig.load(path).targets.get(dtype, {}):
        raise DeviceError(f"no target `{dtype}.{variant}` in {path}")
    new_text = target_remove_text(path.read_text(), dtype, variant)
    if new_text is None:
        raise DeviceError(f"no target `{dtype}.{variant}` in {path}")
    path.write_text(new_text)
    return path
