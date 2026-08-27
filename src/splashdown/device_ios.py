from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import Any

from .capabilities import require_macos, translate_tool_errors
from .device_tools import (
    DISCOVERY_TIMEOUT,
    MUTATION_TIMEOUT,
    check_output_finite,
    run_finite,
)
from .errors import CapabilityError, DeviceError


def _xcrun_json(args: list[str]) -> Any:
    require_macos("simulator support")
    operation = f"xcrun {' '.join(args)}"
    try:
        with translate_tool_errors("ios", "xcrun", "install Xcode command-line tools"):
            out = check_output_finite(
                ["xcrun", *args],
                operation=operation,
                timeout=DISCOVERY_TIMEOUT,
                stderr=subprocess.DEVNULL,
            )
    except subprocess.CalledProcessError as error:
        raise DeviceError(f"{operation} failed: exit {error.returncode}") from error
    return json.loads(out)


def _ios_find_device_by_name(name: str) -> tuple[str, str] | None:
    """Return the UDID and state for an available named simulator."""
    data = _xcrun_json(["simctl", "list", "devices", "-j"])
    for devs in (data.get("devices") or {}).values():
        for device in devs:
            if device.get("name") == name and device.get("isAvailable"):
                return device.get("udid", ""), device.get("state", "")
    return None


def _ios_runtimes() -> list[dict[str, Any]]:
    """Available iOS runtimes, oldest version first."""
    data = _xcrun_json(["simctl", "list", "runtimes", "-j"])
    runtimes = [runtime for runtime in (data.get("runtimes") or []) if runtime.get("isAvailable")]
    if not runtimes:
        raise DeviceError("no available iOS runtimes; install one in Xcode")
    runtimes.sort(key=lambda runtime: _version_tuple(runtime.get("version", "0")))
    return runtimes


def _ios_latest_runtime() -> str:
    """Latest available iOS runtime identifier."""
    return str(_ios_runtimes()[-1].get("identifier", ""))


def _ios_latest_runtime_version() -> str:
    """Latest available iOS runtime version string, which drives auto-upgrade."""
    return str(_ios_runtimes()[-1].get("version", ""))


def _ios_runtime_models(runtime_id: str) -> list[str]:
    """iPhone model names the runtime can instantiate, newest first."""
    try:
        runtimes = _ios_runtimes()
    except DeviceError:
        return []
    for runtime in runtimes:
        if runtime.get("identifier") != runtime_id:
            continue
        supported = runtime.get("supportedDeviceTypes") or []
        return [
            str(device_type.get("name", ""))
            for device_type in supported
            if "iPhone" in str(device_type.get("name", ""))
        ]
    return []


def ios_x86_64_target() -> tuple[str, str] | None:
    """Return the newest installed runtime and model carrying an x86_64 slice."""
    try:
        runtimes = _ios_runtimes()
    except DeviceError:
        return None
    for runtime in reversed(runtimes):
        if "x86_64" not in (runtime.get("supportedArchitectures") or []):
            continue
        models = _ios_runtime_models(str(runtime.get("identifier", "")))
        model = next((item for item in models if item.endswith(" Pro")), None)
        model = model or (models[0] if models else None)
        if model:
            return str(runtime.get("version", "")), model
    return None


def _version_tuple(value: str) -> tuple[int, ...]:
    """Sort dotted versions numerically rather than lexically."""
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        return (0,)


def _ios_udid_exists(udid: str) -> bool:
    """Whether simctl currently knows the UDID."""
    try:
        data = _xcrun_json(["simctl", "list", "devices", "-j"])
    except CapabilityError:
        raise
    except DeviceError:
        return False
    return any(
        device.get("udid") == udid
        for devices in (data.get("devices") or {}).values()
        for device in devices
    )


def _ios_runtime_identifier(version: str) -> str:
    """Convert a dotted iOS version to its CoreSimulator runtime identifier."""
    return f"com.apple.CoreSimulator.SimRuntime.iOS-{version.replace('.', '-')}"


def _ios_device_type_identifier(model: str | None) -> str:
    """Resolve a requested device model or the latest iPhone Pro type."""
    data = _xcrun_json(["simctl", "list", "devicetypes", "-j"])
    device_types = data.get("devicetypes") or []
    if model:
        for device_type in device_types:
            if device_type.get("name") == model:
                return str(device_type.get("identifier", ""))
        raise DeviceError(
            f"unknown iOS device model `{model}` — try `xcrun simctl list devicetypes`"
        )
    pro_models = [
        device_type
        for device_type in device_types
        if re.search(r"iPhone.*Pro$", device_type.get("name", ""))
    ]
    if not pro_models:
        raise DeviceError("no iPhone Pro device types found; specify `model = ...` explicitly")
    pro_models.sort(key=lambda device_type: device_type.get("name", ""))
    return str(pro_models[-1].get("identifier", ""))


def ios_ensure(name: str, model: str | None, ios_version: str | None) -> tuple[str, str]:
    """Find or create a simulator and return its UDID and state."""
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
                check_output_finite(
                    ["xcrun", "simctl", "create", name, device_type, runtime],
                    operation="simctl create",
                    timeout=MUTATION_TIMEOUT,
                    stderr=subprocess.PIPE,
                )
                .decode()
                .strip()
            )
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode().strip()
        raise DeviceError(
            f"simctl create failed: {detail}{_ios_create_hint(model, ios_version, runtime)}"
        ) from error
    return udid, "Shutdown"


_MODELS_SHOWN = 8


def _ios_create_hint(model: str | None, ios_version: str | None, runtime: str) -> str:
    """Explain a model/runtime mismatch when simctl rejects creation."""
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
        proc = run_finite(
            ["xcrun", "simctl", "boot", udid],
            operation="simctl boot",
            timeout=MUTATION_TIMEOUT,
            capture_output=True,
            text=True,
            check=False,
        )
    # A concurrent boot can flip state between discovery and this call.
    if proc.returncode != 0 and "current state: Booted" not in proc.stderr:
        raise DeviceError(
            f"simctl boot failed for {udid}: {proc.stderr.strip() or proc.returncode}"
        )
    with translate_tool_errors("ios", "open", "restore the macOS open command"):
        run_finite(
            ["open", "-a", "Simulator"],
            operation="open Simulator",
            timeout=DISCOVERY_TIMEOUT,
            check=False,
        )


def ios_shutdown(udid: str) -> None:
    require_macos("simulator support")
    # simctl reports an already-shut-down device as an error.
    if _ios_current_state(udid) == "Shutdown":
        return
    with translate_tool_errors("ios", "xcrun", "install Xcode command-line tools"):
        run_finite(
            ["xcrun", "simctl", "shutdown", udid],
            operation="simctl shutdown",
            timeout=MUTATION_TIMEOUT,
            check=False,
        )


def ios_destroy(udid: str) -> None:
    require_macos("simulator support")
    with translate_tool_errors("ios", "xcrun", "install Xcode command-line tools"):
        proc = run_finite(
            ["xcrun", "simctl", "delete", udid],
            operation="simctl delete",
            timeout=MUTATION_TIMEOUT,
            capture_output=True,
            text=True,
            check=False,
        )
    if proc.returncode != 0:
        raise DeviceError(
            f"simctl delete failed for {udid}: {proc.stderr.strip() or proc.returncode}"
        )


def _ios_current_state(udid: str) -> str:
    """Return Booted, Shutdown, or Unknown for a simulator UDID."""
    try:
        data = _xcrun_json(["simctl", "list", "devices", "-j"])
    except CapabilityError:
        raise
    except DeviceError:
        return "Unknown"
    for devices in (data.get("devices") or {}).values():
        for device in devices:
            if device.get("udid") == udid:
                return str(device.get("state", "Unknown"))
    return "Unknown"


def _devicectl_json(args: list[str], *, timeout: float = DISCOVERY_TIMEOUT) -> Any:
    """Run devicectl with JSON output and parse its result."""
    require_macos("physical-device support")
    try:
        with translate_tool_errors("ios", "xcrun", "install Xcode command-line tools"):
            out = check_output_finite(
                ["xcrun", "devicectl", *args, "--json-output", "-"],
                operation=f"devicectl {' '.join(args)}",
                timeout=timeout,
                stderr=subprocess.DEVNULL,
            )
    except subprocess.CalledProcessError as error:
        raise DeviceError(
            "xcrun devicectl failed (needs Xcode 15+ for physical-device support); "
            f"exit {error.returncode}"
        ) from error
    try:
        return json.loads(out)
    except json.JSONDecodeError as error:
        raise DeviceError(f"could not parse devicectl output: {error}") from error


def _ios_physical_devices(*, timeout: float = DISCOVERY_TIMEOUT) -> list[dict[str, str]]:
    """Return paired, available physical iOS devices."""
    data = _devicectl_json(["list", "devices"], timeout=timeout)
    devices = (data.get("result") or {}).get("devices") or []
    result: list[dict[str, str]] = []
    for device in devices:
        hardware = device.get("hardwareProperties") or {}
        if (hardware.get("platform") or "").lower() != "ios":
            continue
        connection = device.get("connectionProperties") or {}
        if (connection.get("pairingState") or "").lower() != "paired":
            continue
        if (connection.get("tunnelState") or "").lower() == "unavailable":
            continue
        udid = hardware.get("udid") or ""
        if not udid:
            continue
        name = (device.get("deviceProperties") or {}).get("name") or udid
        result.append({"id": udid, "name": name, "platform": "ios"})
    return result
