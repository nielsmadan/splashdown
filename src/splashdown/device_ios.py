from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import replace
from typing import Any

from .capabilities import require_macos, translate_tool_errors
from .device_tools import (
    DISCOVERY_TIMEOUT,
    MUTATION_TIMEOUT,
    check_output_finite,
    run_finite,
)
from .device_types import IOSDestination, PhysicalObservation
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
    if state != "Booted":
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
    with translate_tool_errors("ios", "xcrun", "install Xcode command-line tools"):
        ready = run_finite(
            ["xcrun", "simctl", "bootstatus", udid, "-b"],
            operation="simctl boot status",
            timeout=MUTATION_TIMEOUT,
            capture_output=True,
            text=True,
            check=False,
        )
    if ready.returncode != 0:
        raise DeviceError(
            f"simctl boot status failed for {udid}: {ready.stderr.strip() or ready.returncode}"
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
    except (json.JSONDecodeError, UnicodeError) as error:
        raise DeviceError(f"could not parse devicectl output: {error}") from error


def _device_properties(device: dict[str, Any], field: str) -> dict[str, Any]:
    value = device.get(field, {})
    if not isinstance(value, dict):
        raise DeviceError(f"could not parse devicectl output: invalid {field}")
    return value


def _device_property(properties: dict[str, Any], field: str) -> str:
    value = properties.get(field, "")
    if not isinstance(value, str):
        raise DeviceError(f"could not parse devicectl output: invalid {field}")
    return value


def _ios_observation(device: object) -> PhysicalObservation | None:
    if not isinstance(device, dict):
        raise DeviceError("could not parse devicectl output: invalid device record")
    properties = _device_properties(device, "properties") if "properties" in device else None
    source = properties if properties is not None else device
    hardware = _device_properties(
        source, "hardware" if properties is not None else "hardwareProperties"
    )
    reality = _device_property(hardware, "reality").lower()
    if reality == "simulated":
        return None
    platform = _device_property(hardware, "platform").lower()
    if not platform:
        raise DeviceError("could not parse devicectl output: missing device platform")
    if platform != "ios":
        return None
    udid = _device_property(hardware, "udid")
    connection = _device_properties(
        source, "connection" if properties is not None else "connectionProperties"
    )
    pairing = _device_property(connection, "pairingState").lower()
    tunnel = _device_property(
        connection, "state" if properties is not None else "tunnelState"
    ).lower()
    name = _device_property(
        _device_properties(source, "state" if properties is not None else "deviceProperties"),
        "name",
    )
    observation = PhysicalObservation(
        "ios", name or udid or "iOS device", udid or None, udid, "identity_unavailable", "", False
    )
    if reality not in {"", "physical"}:
        return replace(
            observation,
            state="device_kind_unknown",
            detail=f"Xcode reported an unrecognized device kind ({reality}). Check the device in Xcode, then rescan.",
        )
    if (
        not udid
        or re.fullmatch(r"[A-Za-z0-9-]+", udid) is None
        or udid.lower() in {"unknown", "null", "none"}
        or not any(character.isalnum() for character in udid)
        or set(udid) <= {"0", "-"}
    ):
        return replace(
            observation,
            hardware_id=None,
            detail="Xcode did not provide a usable hardware UDID. Connect and pair the phone in Xcode, then rescan.",
        )
    if pairing != "paired":
        return replace(
            observation,
            state=pairing or "pairing_unknown",
            detail="Unlock the phone, tap Trust if prompted, and finish pairing in Xcode, then rescan.",
        )
    if tunnel == "unavailable":
        return replace(
            observation,
            state="unavailable",
            detail="Xcode reports this device unavailable. Unlock and reconnect it, then rescan.",
        )
    if tunnel not in {"connected", "disconnected"}:
        return replace(
            observation,
            state="connection_unknown",
            detail=f"Xcode reported an unrecognized connection state ({tunnel or 'missing'}). Check the device in Xcode, then rescan.",
        )
    return replace(observation, state="ready", selectable=True)


def _ios_physical_observations(*, timeout: float = DISCOVERY_TIMEOUT) -> list[PhysicalObservation]:
    if timeout <= 0:
        raise DeviceError("iOS discovery timed out")
    data = _devicectl_json(["list", "devices"], timeout=timeout)
    if not isinstance(data, dict) or not isinstance(data.get("result"), dict):
        raise DeviceError("could not parse devicectl output: missing result")
    devices = data["result"].get("devices")
    if not isinstance(devices, list):
        raise DeviceError("could not parse devicectl output: missing devices list")
    observations = [item for device in devices if (item := _ios_observation(device)) is not None]
    selected: dict[str, PhysicalObservation] = {}
    unknown = [item for item in observations if item.hardware_id is None]
    for observation in observations:
        if observation.hardware_id is not None:
            existing = selected.get(observation.hardware_id)
            if existing is None or (observation.selectable and not existing.selectable):
                selected[observation.hardware_id] = observation
    return [*selected.values(), *unknown]


def _ios_physical_devices(*, timeout: float = DISCOVERY_TIMEOUT) -> list[dict[str, str]]:
    return [
        {"id": item.hardware_id, "name": item.name, "platform": "ios"}
        for item in _ios_physical_observations(timeout=timeout)
        if item.selectable and item.hardware_id is not None
    ]


def verify_ios_connection(
    destination: IOSDestination, hardware_id: str, *, timeout: float = DISCOVERY_TIMEOUT
) -> None:
    if destination.owned or not hardware_id or destination.identifier != hardware_id:
        raise DeviceError("selected iOS destination does not match the expected hardware UDID")
    try:
        observations = _ios_physical_observations(timeout=timeout)
    except DeviceError as error:
        raise DeviceError(
            f"could not verify the selected iOS phone {hardware_id}: {error}. "
            "Check its connection in Xcode and rerun the command."
        ) from error
    matching = [item for item in observations if item.hardware_id == hardware_id]
    if any(item.selectable for item in matching):
        return
    detail = matching[0].detail if matching else "Xcode no longer lists this phone"
    raise DeviceError(
        f"could not verify the selected iOS phone {hardware_id}: {detail}. "
        "Reconnect the intended phone and rerun the command."
    )
