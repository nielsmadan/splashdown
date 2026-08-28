from __future__ import annotations

import hashlib
import subprocess as subprocess  # noqa: PLC0414
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

from .capabilities import warn_capability
from .device_android import (
    _android_avd_exists,
    _android_bin,
    _android_home,
    _android_latest_image,
    _android_physical_devices,
    _android_running_serial,
    _sanitize_avd_name,
    android_boot,
    android_destroy,
    android_ensure,
    android_shutdown,
)
from .device_ios import (
    _devicectl_json,
    _ios_current_state,
    _ios_device_type_identifier,
    _ios_find_device_by_name,
    _ios_latest_runtime,
    _ios_latest_runtime_version,
    _ios_physical_devices,
    _ios_runtime_identifier,
    _ios_runtime_models,
    _ios_runtimes,
    _ios_udid_exists,
    _version_tuple,
    _xcrun_json,
    ios_boot,
    ios_destroy,
    ios_ensure,
    ios_shutdown,
    ios_x86_64_target,
)
from .device_types import (
    AndroidDestination,
    EmulatorRecord,
    IOSDestination,
    LaunchDestination,
    ManagedDevice,
    SimulatorRecord,
)

# Public re-export for callers that import splashdown.devices.DeviceError.
from .errors import CapabilityError
from .errors import DeviceError as DeviceError  # noqa: PLC0414
from .recipe import _current_branch, _make_scope, render_template
from .registry import Registry
from .targets import global_target_add as global_target_add  # noqa: PLC0414
from .targets import global_target_remove as global_target_remove  # noqa: PLC0414
from .targets import target_add as target_add  # noqa: PLC0414
from .targets import target_remove as target_remove  # noqa: PLC0414

__all__ = [
    "_android_avd_exists",
    "_android_bin",
    "_android_home",
    "_android_latest_image",
    "_android_physical_devices",
    "_android_running_serial",
    "_devicectl_json",
    "_ios_current_state",
    "_ios_device_type_identifier",
    "_ios_find_device_by_name",
    "_ios_latest_runtime",
    "_ios_latest_runtime_version",
    "_ios_physical_devices",
    "_ios_runtime_identifier",
    "_ios_runtime_models",
    "_ios_runtimes",
    "_ios_udid_exists",
    "_sanitize_avd_name",
    "_version_tuple",
    "_xcrun_json",
    "android_boot",
    "android_destroy",
    "android_ensure",
    "android_shutdown",
    "ios_boot",
    "ios_destroy",
    "ios_ensure",
    "ios_shutdown",
    "ios_x86_64_target",
]


def _default_sim_name(cwd: Path, variant: str) -> str:
    """Return a readable, collision-resistant instance name for a checkout."""
    digest = hashlib.sha256(str(cwd.resolve()).encode()).hexdigest()[:8]
    return f"{cwd.parent.name}/{cwd.name}/{variant}-{digest}"


def _resolve_device_name(
    spec: dict[str, Any], cwd: Path, variant: str, dtype: str | None = None
) -> str:
    """Resolve an explicit or path-derived name, sanitizing emulator names."""
    raw = spec.get("name")
    if not raw:
        name = _default_sim_name(cwd, variant)
    elif isinstance(raw, str) and "{{" in raw:
        scope = _make_scope(cwd, _current_branch(cwd), {})
        name = render_template(raw, scope)
    else:
        name = str(raw)
    if name.startswith("-"):
        # simctl and avdmanager would interpret the name as a flag.
        raise DeviceError(f"device name must not start with '-': {name!r}")
    return _sanitize_avd_name(name) if dtype == "emulator" else name


def _is_orphan_device(row: ManagedDevice) -> bool:
    """Whether a registered device's underlying sim or AVD no longer exists."""
    if isinstance(row, SimulatorRecord):
        return not _ios_udid_exists(row.identifier)
    return not _android_avd_exists(row.name)


def physical_discover(
    platform: str | None = None,
    *,
    warned: set[str] | None = None,
    on_warning: Callable[[CapabilityError], None] | None = None,
) -> list[dict[str, str]]:
    """Discover physical devices, tolerating an unavailable unrequested platform."""
    ios_discover = _ios_physical_devices
    android_discover = _android_physical_devices
    devices: list[dict[str, str]] = []
    warning_keys = warned if warned is not None else set()

    def emit_warning(error: CapabilityError) -> None:
        if on_warning is not None:
            on_warning(error)
        else:
            warn_capability(error, warning_keys)

    if platform in (None, "ios"):
        try:
            devices += ios_discover()
        except CapabilityError as error:
            if platform == "ios":
                raise
            emit_warning(error)
    if platform in (None, "android"):
        try:
            devices += android_discover()
        except CapabilityError as error:
            if platform == "android":
                raise
            emit_warning(error)
    return devices


def _physical_match(
    spec: dict[str, Any],
    *,
    warned: set[str] | None = None,
    on_warning: Callable[[CapabilityError], None] | None = None,
) -> list[dict[str, str]]:
    """Discover and filter physical devices by a variant spec's platform/id/name."""
    devices = physical_discover(spec.get("platform"), warned=warned, on_warning=on_warning)
    want_id = spec.get("id")
    want_name = spec.get("name")
    if want_id:
        devices = [device for device in devices if device["id"] == want_id]
    elif want_name:
        needle = str(want_name).lower()
        devices = [device for device in devices if needle in device["name"].lower()]
    return devices


def ensure_physical(
    spec: dict[str, Any],
    *,
    warned: set[str] | None = None,
    on_warning: Callable[[CapabilityError], None] | None = None,
) -> LaunchDestination:
    """Resolve a physical target to exactly one connected launch destination."""
    devices = _physical_match(spec, warned=warned, on_warning=on_warning)
    if not devices:
        raise DeviceError(_physical_no_match_msg(spec))
    if len(devices) > 1:
        listing = ", ".join(f"{device['name']} ({device['id']})" for device in devices)
        raise DeviceError(
            f"multiple connected physical devices ({listing}); narrow with "
            "`id`/`name`/`platform` on the variant"
        )
    device = devices[0]
    if device["platform"] == "ios":
        return IOSDestination(device["name"], device["id"], owned=False)
    return AndroidDestination(device["name"], device["id"], owned=False)


def _physical_no_match_msg(spec: dict[str, Any]) -> str:
    bits = [f"{key}={spec[key]!r}" for key in ("platform", "id", "name") if spec.get(key)]
    qualifier = f" matching {', '.join(bits)}" if bits else ""
    return (
        f"no connected physical device{qualifier}; plug one in and unlock it "
        "(iOS: trust this computer; Android: enable USB debugging)"
    )


def physical_status(
    spec: dict[str, Any],
    *,
    warned: set[str] | None = None,
    on_warning: Callable[[CapabilityError], None] | None = None,
) -> str:
    """Liveness for a physical target: connected, absent, or ambiguous."""
    devices = _physical_match(spec, warned=warned, on_warning=on_warning)
    if not devices:
        return "absent"
    if len(devices) > 1:
        return "ambiguous"
    return "connected"


def _latest_os_for(dtype: str, cache: dict[str, str] | None) -> str:
    """Resolve the latest platform image at most once per command cache."""
    key = "ios" if dtype == "simulator" else "android"
    if cache is not None and key in cache:
        return cache[key]
    value = _ios_latest_runtime_version() if dtype == "simulator" else _android_latest_image()
    if cache is not None:
        cache[key] = value
    return value


def _target_os_for(dtype: str, spec: dict[str, Any], cache: dict[str, str] | None) -> str:
    """Resolve a variant's pinned platform image or the current latest."""
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
    if isinstance(row, SimulatorRecord):
        drifted = row.runtime != target or row.model != spec.get("model", "")
    elif isinstance(row, EmulatorRecord):
        resolved_name = _resolve_device_name(spec, cwd, variant, dtype)
        drifted = (
            row.name != resolved_name or row.image != target or row.device != spec.get("device", "")
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
    """Whether reconciliation would need to create or replace the managed device."""
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
) -> LaunchDestination:
    """Reconcile a managed sim/AVD against the selected target variant."""
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
            if not isinstance(row, SimulatorRecord):
                raise DeviceError("internal: simulator row vanished mid-check")
            return IOSDestination(sim_name, row.identifier, owned=True)
        if row is not None:
            device_destroy_row(row)
        udid, _state = ios_ensure(sim_name, model_spec or None, target_ios)
        registry.record_simulator(checkout, variant, udid, model_spec, target_ios)
        return IOSDestination(sim_name, udid, owned=True)

    if dtype == "emulator":
        target_image = _target_os_for(dtype, spec, cache)
        device_spec = spec.get("device", "")
        if not stale:
            return AndroidDestination(sim_name, None, owned=True)
        if row is not None:
            device_destroy_row(row)
        android_ensure(sim_name, device_spec or None, target_image)
        registry.record_emulator(checkout, variant, sim_name, device_spec, target_image)
        return AndroidDestination(sim_name, None, owned=True)

    raise DeviceError(f"unknown target type `{dtype}`")


def device_status(dtype: str, resolved_name: str) -> str:
    if dtype == "simulator":
        found = _ios_find_device_by_name(resolved_name)
        if not found:
            return "absent"
        return found[1].lower()
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


def device_shutdown_row(row: ManagedDevice) -> None:
    if isinstance(row, SimulatorRecord):
        if _ios_udid_exists(row.identifier):
            ios_shutdown(row.identifier)
    elif _android_avd_exists(row.name):
        android_shutdown(row.name)


def device_destroy_row(row: ManagedDevice) -> None:
    """Destroy only the external instance identified by a managed registry row."""
    if isinstance(row, SimulatorRecord):
        if not _ios_udid_exists(row.identifier):
            return
        ios_shutdown(row.identifier)
        ios_destroy(row.identifier)
    elif isinstance(row, EmulatorRecord):
        if not _android_avd_exists(row.name):
            return
        android_shutdown(row.name)
        android_destroy(row.name)


def _device_status_for_row(row: ManagedDevice) -> str:
    """Read liveness using the identifier persisted in a managed registry row."""
    if isinstance(row, SimulatorRecord):
        if not _ios_udid_exists(row.identifier):
            return "absent"
        return _ios_current_state(row.identifier).lower()
    if isinstance(row, EmulatorRecord):
        if not _android_avd_exists(row.name):
            return "absent"
        return "running" if _android_running_serial(row.name) else "stopped"
    return "unknown"
