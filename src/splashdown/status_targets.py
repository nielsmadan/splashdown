from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar, cast

from .device_android import _android_bin
from .device_ios import _devicectl_json, _xcrun_json
from .device_tools import DISCOVERY_TIMEOUT, check_output_finite
from .device_types import EmulatorRecord, ManagedDevice, SimulatorRecord
from .devices import _resolve_device_name, physical_match_snapshot
from .errors import DeviceError
from .recipe import _make_scope

T = TypeVar("T")
_ADB_ROW_COLUMNS = 2


class TargetObservations:
    def __init__(self) -> None:
        self.cache: dict[str, object] = {}
        self.unavailable: set[str] = set()

    def once(self, key: str, probe: Callable[[], T]) -> T:
        if key not in self.cache:
            try:
                self.cache[key] = probe()
            except (OSError, ValueError, DeviceError, subprocess.SubprocessError) as error:
                self.cache[key] = error
        result = self.cache[key]
        if isinstance(result, Exception):
            raise result
        return cast(T, result)

    def simulators(self) -> list[dict[str, Any]]:
        def discover() -> list[dict[str, Any]]:
            data = _xcrun_json(["simctl", "list", "devices", "-j"])
            if not isinstance(data, dict) or not isinstance(data.get("devices"), dict):
                raise DeviceError("invalid simulator inventory")
            groups = data["devices"].values()
            if any(not isinstance(group, list) for group in groups):
                raise DeviceError("invalid simulator inventory")
            rows = [row for group in groups for row in group]
            if any(
                not isinstance(row, dict)
                or not isinstance(row.get("udid"), str)
                or not row["udid"]
                or type(row.get("isAvailable")) is not bool
                for row in rows
            ):
                raise DeviceError("invalid simulator inventory")
            return rows

        return self.once("simulators", discover)

    def avds(self) -> set[str]:
        def discover() -> set[str]:
            raw = check_output_finite(
                [_android_bin("avdmanager"), "list", "avd", "-c"],
                operation="avdmanager list",
                timeout=DISCOVERY_TIMEOUT,
                stderr=subprocess.DEVNULL,
            ).decode()
            names = [line.strip() for line in raw.splitlines() if line.strip()]
            if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", name) for name in names):
                raise DeviceError("invalid AVD inventory")
            return set(names)

        return self.once("avds", discover)

    def running_emulators(self) -> set[str]:
        def discover() -> set[str]:
            adb = _android_bin("adb")
            raw = check_output_finite(
                [adb, "devices"],
                operation="adb devices",
                timeout=DISCOVERY_TIMEOUT,
                stderr=subprocess.DEVNULL,
            ).decode()
            rows = _adb_rows(raw)
            names = set()
            for row in rows:
                if len(row) < _ADB_ROW_COLUMNS or not row[0].startswith("emulator-"):
                    continue
                if row[1] != "device":
                    raise DeviceError("emulator connection unavailable")
                output = (
                    check_output_finite(
                        [adb, "-s", row[0], "emu", "avd", "name"],
                        operation="emulator name",
                        timeout=2,
                        stderr=subprocess.DEVNULL,
                    )
                    .decode()
                    .splitlines()
                )
                if not output or not output[0].strip():
                    raise DeviceError("emulator name unavailable")
                names.add(output[0].strip())
            return names

        return self.once("running_emulators", discover)

    def latest(self, dtype: str) -> str:
        def android_image() -> str:
            raw = check_output_finite(
                [_android_bin("sdkmanager"), "--list_installed"],
                operation="installed Android images",
                timeout=DISCOVERY_TIMEOUT,
                stderr=subprocess.DEVNULL,
            ).decode()
            images = re.findall(r"^\s*(system-images;android-(\d+);[^\s|]+)", raw, re.MULTILINE)
            if not images:
                raise DeviceError("no installed Android images")
            return str(max(images, key=lambda item: int(item[1]))[0])

        def ios_runtime() -> str:
            data = _xcrun_json(["simctl", "list", "runtimes", "-j"])
            if not isinstance(data, dict) or not isinstance(data.get("runtimes"), list):
                raise DeviceError("invalid runtime inventory")
            versions = []
            for runtime in data["runtimes"]:
                if not isinstance(runtime, dict):
                    raise DeviceError("invalid runtime inventory")
                if runtime.get("isAvailable"):
                    version = runtime.get("version")
                    if not isinstance(version, str) or not re.fullmatch(r"\d+(?:\.\d+)*", version):
                        raise DeviceError("invalid runtime version")
                    versions.append(version)
            if not versions:
                raise DeviceError("no available runtime")
            return max(
                versions, key=lambda version: tuple(int(part) for part in version.split("."))
            )

        return self.once(f"latest:{dtype}", ios_runtime if dtype == "simulator" else android_image)

    def name(self, cwd: Path, dtype: str, variant: str, spec: dict[str, Any]) -> str:
        scope = None
        if "{{" in str(spec.get("name", "")):
            scope = self.once(f"name_scope:{cwd}", lambda: _name_scope(cwd))
        return _resolve_device_name(spec, cwd, variant, dtype, scope=scope)

    def observe(
        self,
        cwd: Path,
        dtype: str,
        variant: str,
        spec: dict[str, Any] | None,
        row: ManagedDevice | None,
    ) -> tuple[str, str, str]:
        if dtype == "device":
            return self.physical(spec or {})
        resolved = self.name(cwd, dtype, variant, spec or {})
        name = row.identifier if row else resolved
        if dtype == "simulator":
            devices = self.simulators()
            found = next(
                (
                    device
                    for device in devices
                    if (
                        device["udid"] == name
                        if row
                        else device.get("name") == name and device["isAvailable"]
                    )
                ),
                None,
            )
            if found and not found["isAvailable"]:
                return name, "runtime unavailable", "unavailable"
            state = str(found.get("state", "unknown")).lower() if found else "absent"
            if found and state not in {
                "booted",
                "shutdown",
                "booting",
                "shutting down",
                "creating",
            }:
                raise DeviceError("unknown simulator state")
        else:
            exists = name in self.avds()
            state = (
                ("running" if name in self.running_emulators() else "stopped")
                if exists
                else "absent"
            )
        if row and state == "absent":
            return name, state, "orphan"
        if spec is None:
            return name, state, "undeclared"
        if row is None:
            return name, state, "not_created" if state == "absent" else "unregistered"
        requested = spec.get("ios" if dtype == "simulator" else "image", "latest")
        desired = self.latest(dtype) if requested == "latest" else requested
        drifted = (
            row.runtime != desired or row.model != spec.get("model", "")
            if isinstance(row, SimulatorRecord)
            else isinstance(row, EmulatorRecord)
            and (
                row.image != desired or row.device != spec.get("device", "") or row.name != resolved
            )
        )
        return name, state, "drifted" if drifted else "healthy"

    def physical(self, spec: dict[str, Any]) -> tuple[str, str, str]:
        self.unavailable = set()
        platforms = [spec["platform"]] if spec.get("platform") else ["ios", "android"]
        devices = []
        failed = False
        for platform in platforms:
            try:
                devices.extend(
                    self.once(
                        f"physical:{platform}",
                        _physical_ios if platform == "ios" else _physical_android,
                    )
                )
            except (OSError, ValueError, DeviceError, subprocess.SubprocessError):
                failed = True
                self.unavailable.add(platform)
        matches = physical_match_snapshot(spec, devices)
        name = str(spec.get("id") or spec.get("name") or spec.get("platform") or "auto")
        if len(matches) > 1:
            return name, "ambiguous", "ambiguous"
        if matches:
            return name, "connected", "unavailable" if failed else "healthy"
        return name, "unavailable" if failed else "absent", "unavailable" if failed else "missing"


def _adb_rows(raw: str) -> list[list[str]]:
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "List of devices attached":
        raise DeviceError("invalid adb inventory")
    rows = [line.split() for line in lines[1:] if line.strip()]
    if any(len(row) < _ADB_ROW_COLUMNS for row in rows):
        raise DeviceError("invalid adb device row")
    return rows


def _physical_android() -> list[dict[str, str]]:
    raw = check_output_finite(
        [_android_bin("adb"), "devices", "-l"],
        operation="physical Android discovery",
        timeout=DISCOVERY_TIMEOUT,
        stderr=subprocess.DEVNULL,
    ).decode()
    rows = _adb_rows(raw)
    if any(row[1] not in {"device", "offline", "unauthorized"} for row in rows):
        raise DeviceError("unknown Android connection")
    return [
        {
            "id": row[0],
            "platform": "android",
            "name": next(
                (part.split(":", 1)[1] for part in row[2:] if part.startswith("model:")), row[0]
            ),
        }
        for row in rows
        if row[1] == "device" and not row[0].startswith("emulator-")
    ]


def _physical_ios() -> list[dict[str, str]]:
    data = _devicectl_json(["list", "devices"])
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("result"), dict)
        or not isinstance(data["result"].get("devices"), list)
    ):
        raise DeviceError("invalid physical iOS inventory")
    result = []
    for device in data["result"]["devices"]:
        if not isinstance(device, dict) or any(
            not isinstance(device.get(key), dict)
            for key in ("hardwareProperties", "connectionProperties", "deviceProperties")
        ):
            raise DeviceError("invalid physical iOS device")
        hardware = device["hardwareProperties"]
        connection = device["connectionProperties"]
        if str(hardware.get("platform", "")).lower() != "ios":
            continue
        if (
            str(connection.get("pairingState", "")).lower() != "paired"
            or str(connection.get("tunnelState", "")).lower() == "unavailable"
        ):
            continue
        identifier = hardware.get("udid")
        if not isinstance(identifier, str) or not identifier:
            raise DeviceError("invalid physical iOS identity")
        result.append(
            {
                "id": identifier,
                "platform": "ios",
                "name": str(device["deviceProperties"].get("name") or identifier),
            }
        )
    return result


def _name_scope(cwd: Path) -> dict[str, Any]:
    import os  # noqa: PLC0415

    root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
        env={**os.environ, "LC_ALL": "C"},
    )
    if root.returncode:
        if "not a git repository" not in root.stderr:
            raise DeviceError("target Git metadata unavailable")
        repo, branch = cwd.name, ""
    else:
        repo = Path(root.stdout.strip()).name
        current = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        if current.returncode not in {0, 1}:
            raise DeviceError("target branch unavailable")
        branch = current.stdout.strip() if current.returncode == 0 else ""
    return _make_scope(cwd, branch, {}, repo_name=repo)
