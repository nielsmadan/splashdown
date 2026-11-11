from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial
from pathlib import Path

from .capabilities import translate_tool_errors
from .constants import state_directory
from .device_tools import (
    DISCOVERY_TIMEOUT,
    MUTATION_TIMEOUT,
    check_output_finite,
    run_finite,
)
from .device_types import AndroidDestination, PhysicalObservation
from .errors import CapabilityError, DeviceError

_AVD_INVALID_RE = re.compile(r"[^A-Za-z0-9._-]")
_ADB_ROW_COLS = 2
_ADB_MDNS_ROW_RE = re.compile(r"^(.+?\._adb(?:-tls-connect)?\._tcp\.?)[ \t]+(\S.*)$")
_HARDWARE_SERIAL_RE = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_IDENTITY_PROBE_TIMEOUT = 2
_IDENTITY_PROBE_WORKERS = 4
_PHONE_LABEL_COMMAND = (
    "getprop ro.product.manufacturer; printf '\\0'; "
    "settings get global device_name; printf '\\0'; "
    "getprop ro.product.marketname; printf '\\0'; "
    "getprop ro.config.marketing_name"
)


def _sanitize_avd_name(name: str) -> str:
    """Replace characters avdmanager rejects with underscores."""
    return _AVD_INVALID_RE.sub("_", name)


def _android_home() -> Path:
    configured = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if configured and Path(configured).exists():
        return Path(configured)
    for candidate in (Path.home() / "Library/Android/sdk", Path.home() / "Android/Sdk"):
        if candidate.exists():
            return candidate
    raise CapabilityError(
        "android",
        "Android SDK not found; set ANDROID_HOME or ANDROID_SDK_ROOT",
    )


def _android_bin(name: str) -> str:
    home = _android_home()
    candidates = [
        home / "cmdline-tools" / "latest" / "bin" / name,
        home / "tools" / "bin" / name,
        home / "emulator" / name,
        home / "platform-tools" / name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise CapabilityError(
        "android",
        f"{name} not found under {home}; install Android SDK command-line tools",
    )


def _read_android_avd_names() -> list[str]:
    with translate_tool_errors("android", "avdmanager", "install Android SDK command-line tools"):
        out = check_output_finite(
            [_android_bin("avdmanager"), "list", "avd", "-c"],
            operation="avdmanager list",
            timeout=DISCOVERY_TIMEOUT,
            stderr=subprocess.DEVNULL,
        )
    return [line.strip() for line in out.decode().splitlines() if line.strip()]


def _android_avd_names() -> list[str]:
    try:
        return _read_android_avd_names()
    except subprocess.CalledProcessError as error:
        raise DeviceError(f"avdmanager list failed: exit {error.returncode}") from error


def _android_avd_exists(name: str) -> bool:
    try:
        return name in _read_android_avd_names()
    except subprocess.CalledProcessError:
        return False


def _android_latest_image() -> str:
    """Prefer the newest installed system image, with a stable fallback."""
    sdkmanager = _android_bin("sdkmanager")
    try:
        with translate_tool_errors(
            "android", "sdkmanager", "install Android SDK command-line tools"
        ):
            out = check_output_finite(
                [sdkmanager, "--list_installed"],
                operation="sdkmanager list installed",
                timeout=DISCOVERY_TIMEOUT,
                stderr=subprocess.DEVNULL,
            ).decode()
    except subprocess.CalledProcessError:
        out = ""
    installed = re.findall(r"^\s*(system-images;android-\d+;[^\s|]+)", out, re.MULTILINE)
    if installed:

        def api_level(image: str) -> int:
            match = re.search(r"android-(\d+)", image)
            return int(match.group(1)) if match else 0

        installed.sort(key=api_level, reverse=True)
        return str(installed[0])
    return "system-images;android-34;google_apis;arm64-v8a"


def android_ensure(name: str, device: str | None, image: str | None) -> str:
    """Find or create an AVD and return its name."""
    if _android_avd_exists(name):
        return name
    image = image or _android_latest_image()
    device = device or "pixel_9"
    print(f"creating Android AVD '{name}' (device={device}, image={image})", file=sys.stderr)
    with translate_tool_errors("android", "avdmanager", "install Android SDK command-line tools"):
        proc = run_finite(
            [
                _android_bin("avdmanager"),
                "create",
                "avd",
                "-n",
                name,
                "-k",
                image,
                "-d",
                device,
                "--force",
            ],
            operation="avdmanager create",
            timeout=MUTATION_TIMEOUT,
            input=b"\n",
            capture_output=True,
            check=False,
        )
    if proc.returncode != 0:
        raise DeviceError(f"avdmanager create failed: {proc.stderr.decode().strip()}")
    return name


def _android_running_serial(avd_name: str, *, timeout: float = DISCOVERY_TIMEOUT) -> str | None:
    """Match a running emulator to an AVD through adb."""
    if timeout <= 0:
        return None
    deadline = time.monotonic() + timeout
    adb = _android_bin("adb")
    try:
        with translate_tool_errors("android", "adb", "install Android SDK platform-tools"):
            out = check_output_finite(
                [adb, "devices"],
                operation="adb devices",
                timeout=min(DISCOVERY_TIMEOUT, timeout),
                stderr=subprocess.DEVNULL,
            ).decode()
    except subprocess.CalledProcessError:
        return None
    for line in out.splitlines()[1:]:
        parts = line.split()
        if (
            len(parts) < _ADB_ROW_COLS
            or not parts[0].startswith("emulator-")
            or parts[1] != "device"
        ):
            continue
        serial = parts[0]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            with translate_tool_errors("android", "adb", "install Android SDK platform-tools"):
                names = (
                    subprocess.check_output(
                        [adb, "-s", serial, "emu", "avd", "name"],
                        stderr=subprocess.DEVNULL,
                        timeout=min(2, remaining),
                    )
                    .decode()
                    .splitlines()
                )
            if names and names[0].strip() == avd_name:
                return serial
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
    return None


def _android_boot_ready(serial: str, *, timeout: float = 4) -> bool:
    if timeout <= 0:
        return False
    deadline = time.monotonic() + timeout
    adb = _android_bin("adb")
    with translate_tool_errors("android", "adb", "install Android SDK platform-tools"):
        booted = check_output_finite(
            [adb, "-s", serial, "shell", "getprop", "sys.boot_completed"],
            operation="adb read boot status",
            timeout=min(2, timeout),
            stderr=subprocess.DEVNULL,
        )
        if booted.strip() != b"1":
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        package_service = check_output_finite(
            [adb, "-s", serial, "shell", "service", "check", "package"],
            operation="adb check package service",
            timeout=min(2, remaining),
            stderr=subprocess.DEVNULL,
        )
    return package_service.strip().endswith(b": found")


def android_boot(avd_name: str, *, state_dir: Path | None = None) -> str:
    """Start an emulator in the background and return its adb serial."""
    deadline = time.monotonic() + 60
    serial = _android_running_serial(
        avd_name,
        timeout=min(DISCOVERY_TIMEOUT, deadline - time.monotonic()),
    )
    should_spawn = serial is None
    if serial:
        try:
            if _android_boot_ready(serial, timeout=deadline - time.monotonic()):
                return serial
        except subprocess.CalledProcessError:
            serial = None
    from .recipe import _slug as recipe_slug  # noqa: PLC0415

    log = (state_dir or state_directory()) / f"emulator-{recipe_slug(avd_name)}.log"
    if should_spawn:
        emulator = _android_bin("emulator")
        log.parent.mkdir(parents=True, exist_ok=True)
        print(f"booting Android AVD '{avd_name}' (log: {log})", file=sys.stderr)
        with (
            log.open("ab") as file,
            translate_tool_errors("android", "emulator", "install the Android SDK emulator"),
        ):
            subprocess.Popen(
                [emulator, "-avd", avd_name],
                stdout=file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
    while (remaining := deadline - time.monotonic()) > 0:
        time.sleep(min(1, remaining))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if serial is None:
            serial = _android_running_serial(
                avd_name,
                timeout=min(DISCOVERY_TIMEOUT, remaining),
            )
            remaining = deadline - time.monotonic()
        if serial and remaining > 0:
            try:
                if _android_boot_ready(serial, timeout=remaining):
                    return serial
            except subprocess.CalledProcessError:
                serial = None
    log_hint = f"; see {log}" if should_spawn else ""
    raise DeviceError(f"AVD '{avd_name}' did not become ready within 60s{log_hint}")


def android_shutdown(avd_name: str) -> None:
    serial = _android_running_serial(avd_name)
    if not serial:
        return
    with translate_tool_errors("android", "adb", "install Android SDK platform-tools"):
        run_finite(
            [_android_bin("adb"), "-s", serial, "emu", "kill"],
            operation="adb emulator shutdown",
            timeout=MUTATION_TIMEOUT,
            check=False,
        )


def android_destroy(avd_name: str) -> None:
    with translate_tool_errors("android", "avdmanager", "install Android SDK command-line tools"):
        proc = run_finite(
            [_android_bin("avdmanager"), "delete", "avd", "-n", avd_name],
            operation="avdmanager delete",
            timeout=MUTATION_TIMEOUT,
            capture_output=True,
            text=True,
            check=False,
        )
    if proc.returncode != 0:
        raise DeviceError(
            f"avdmanager delete failed for {avd_name}: {proc.stderr.strip() or proc.returncode}"
        )


def _adb_device_rows(output: str) -> list[list[str]]:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines or lines[0] != "List of devices attached":
        raise DeviceError("could not parse adb devices output: missing device-list header")
    rows = [
        [match[1], *match[2].split()]
        if (match := _ADB_MDNS_ROW_RE.fullmatch(line))
        else line.split()
        for line in lines[1:]
    ]
    if any(len(row) < _ADB_ROW_COLS for row in rows):
        raise DeviceError("could not parse adb devices output: incomplete device row")
    return [row for row in rows if not row[0].startswith("emulator-")]


def _valid_hardware_serial(serial: str) -> bool:
    return bool(
        _HARDWARE_SERIAL_RE.fullmatch(serial)
        and any(character.isalnum() for character in serial)
        and serial.lower() not in {"unknown", "null", "none", "default", "0123456789abcdef"}
        and set(serial) != {"0"}
    )


def _android_hardware_serial(adb: str, selector: list[str], deadline: float) -> str:
    for prop in ("ro.serialno", "ro.boot.serialno"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DeviceError("Android discovery timed out before reading the hardware serial")
        with translate_tool_errors("android", "adb", "install Android SDK platform-tools"):
            try:
                output = check_output_finite(
                    [adb, *selector, "shell", "getprop", prop],
                    operation=f"adb read {prop}",
                    timeout=min(_IDENTITY_PROBE_TIMEOUT, remaining),
                    stderr=subprocess.PIPE,
                )
            except subprocess.CalledProcessError as error:
                detail = (error.stderr or b"").decode("utf-8", errors="replace").strip()
                raise DeviceError(
                    f"adb could not read {prop}: {detail or f'exit {error.returncode}'}"
                ) from error
        serial = output.decode("utf-8", errors="replace").strip()
        if _valid_hardware_serial(serial):
            return serial
    raise DeviceError("device returned an empty, placeholder or malformed hardware serial")


def _android_connection_type(endpoint: str, *, usb: bool) -> str | None:
    if usb:
        return "usb"
    if endpoint.rstrip(".").endswith("._tcp") or re.fullmatch(
        r"(?:\[[^]]+\]|[^: ]+):[0-9]+", endpoint
    ):
        return "network"
    return None


def _android_observe_row(row: list[str], adb: str, deadline: float) -> PhysicalObservation:
    endpoint, state = row[:2]
    metadata = dict(part.split(":", 1) for part in row[2:] if ":" in part)
    transport = metadata.get("transport_id")
    if transport and not (transport.isascii() and transport.isdecimal()):
        raise DeviceError(f"invalid adb transport_id for {endpoint}")
    observation = PhysicalObservation(
        "android",
        metadata.get("model", endpoint),
        None,
        endpoint,
        state,
        "",
        False,
        adb_transport_id=transport,
        connection_type=_android_connection_type(endpoint, usb=bool(metadata.get("usb"))),
    )
    if state == "unauthorized":
        return replace(
            observation,
            detail="Unlock the phone and accept its USB debugging authorization prompt, then rescan.",
        )
    if state == "offline":
        return replace(
            observation,
            detail="ADB reports this connection offline. Unlock and reconnect the phone, then rescan.",
        )
    if state == "no" and len(row) > _ADB_ROW_COLS and row[2] in {"permissions", "permissions;"}:
        return replace(
            observation,
            state="no_permissions",
            detail="The host cannot access this device. Check Android USB permissions, then reconnect and rescan.",
        )
    if state != "device":
        return replace(
            observation,
            detail=f"ADB reports {state}; reconnect the phone in normal Android mode and rescan.",
        )
    selector = ["-t", transport] if transport else ["-s", endpoint]
    try:
        hardware_id = _android_hardware_serial(adb, selector, deadline)
    except DeviceError as error:
        return replace(
            observation,
            state="identity_unavailable",
            detail=f"{error}. Connect and authorize the phone over USB, then rescan.",
        )
    return replace(observation, hardware_id=hardware_id, state="ready", selectable=True)


def _select_android_connections(
    observations: list[PhysicalObservation],
) -> list[PhysicalObservation]:
    grouped: dict[str, list[PhysicalObservation]] = {}
    for observation in observations:
        if observation.hardware_id is not None:
            grouped.setdefault(observation.hardware_id, []).append(observation)
    selected = [observation for observation in observations if observation.hardware_id is None]
    for hardware_id, matches in grouped.items():
        usb_matches = [match for match in matches if match.connection_type == "usb"]
        models = {match.name for match in matches if match.name != match.transport_id}
        wifi_only = all(match.connection_type == "network" for match in matches)
        if len(models) > 1 or (len(matches) > 1 and len(usb_matches) != 1 and not wifi_only):
            endpoints = ", ".join(match.transport_id for match in matches)
            detail = f"Hardware ID {hardware_id} has conflicting or ambiguous connections ({endpoints}). Disconnect extra connections, then rescan."
            selected.append(replace(matches[0], state="ambiguous", detail=detail, selectable=False))
        else:
            selected.append(
                usb_matches[0]
                if usb_matches
                else min(
                    matches, key=lambda match: (match.transport_id, match.adb_transport_id or "")
                )
            )
    return selected


def _android_physical_observations(
    *, timeout: float = DISCOVERY_TIMEOUT, include_names: bool = False
) -> list[PhysicalObservation]:
    if timeout <= 0:
        raise DeviceError("Android discovery timed out")
    deadline = time.monotonic() + timeout
    adb = _android_bin("adb")
    try:
        with translate_tool_errors("android", "adb", "install Android SDK platform-tools"):
            out = check_output_finite(
                [adb, "devices", "-l"],
                operation="adb devices",
                timeout=timeout,
                stderr=subprocess.PIPE,
            ).decode()
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or b"").decode("utf-8", errors="replace").strip()
        raise DeviceError(f"adb devices failed: {detail or f'exit {error.returncode}'}") from error
    except UnicodeError as error:
        raise DeviceError("could not parse adb devices output: invalid text encoding") from error
    with ThreadPoolExecutor(max_workers=_IDENTITY_PROBE_WORKERS) as executor:
        observations = list(
            executor.map(
                partial(_android_observe_row, adb=adb, deadline=deadline), _adb_device_rows(out)
            )
        )
    selected = _select_android_connections(observations)
    if not include_names:
        return selected
    with ThreadPoolExecutor(max_workers=_IDENTITY_PROBE_WORKERS) as executor:
        return list(
            executor.map(partial(_android_phone_name, adb=adb, deadline=deadline), selected)
        )


def _android_phone_name(
    observation: PhysicalObservation, *, adb: str, deadline: float
) -> PhysicalObservation:
    remaining = deadline - time.monotonic()
    if not observation.selectable or remaining <= 0:
        return observation
    selector = (
        ["-t", observation.adb_transport_id]
        if observation.adb_transport_id
        else ["-s", observation.transport_id]
    )
    try:
        output = check_output_finite(
            [adb, *selector, "shell", _PHONE_LABEL_COMMAND],
            operation="adb read phone name",
            timeout=min(_IDENTITY_PROBE_TIMEOUT, remaining),
            stderr=subprocess.PIPE,
        ).decode()
    except (DeviceError, OSError, subprocess.CalledProcessError, UnicodeError):
        return observation
    fields = [value.strip() for value in output.split("\0")]
    try:
        manufacturer, device_name, market_name, marketing_name = (
            "" if value.lower() in {"null", "unknown", "none"} else value for value in fields
        )
    except ValueError:
        return observation
    names = (device_name, market_name, marketing_name)
    name = next((value for value in names if value), observation.name.replace("_", " "))
    if manufacturer and not name.casefold().startswith(manufacturer.casefold()):
        name = f"{manufacturer.capitalize() if manufacturer.islower() else manufacturer} {name}"
    return replace(observation, name=name)


def _android_physical_devices(*, timeout: float = DISCOVERY_TIMEOUT) -> list[dict[str, str]]:
    observations = _android_physical_observations(timeout=timeout)
    return [
        {
            "id": item.hardware_id,
            "name": item.name,
            "platform": "android",
            **({"connection_type": item.connection_type} if item.connection_type else {}),
            **({"error": item.detail} if item.state == "ambiguous" else {}),
            **(
                {"adb_transport_id": item.adb_transport_id}
                if item.selectable and item.adb_transport_id is not None
                else {}
            ),
            **(
                {"transport_id": item.transport_id}
                if item.selectable and item.transport_id != item.hardware_id
                else {}
            ),
        }
        for item in observations
        if item.hardware_id is not None and (item.selectable or item.state == "ambiguous")
    ]


def verify_android_connection(
    destination: AndroidDestination, hardware_id: str, *, timeout: float = DISCOVERY_TIMEOUT
) -> None:
    if destination.owned or not destination.serial or not hardware_id:
        raise DeviceError(
            "cannot verify an Android physical connection without its endpoint and hardware ID"
        )
    if timeout <= 0:
        raise DeviceError("Android connection verification timed out; rerun the command")
    deadline = time.monotonic() + timeout
    token = destination.adb_transport_id
    if token is not None and not (token.isascii() and token.isdecimal()):
        raise DeviceError(
            "invalid selected adb transport ID; rerun the command to select the phone again"
        )
    selectors = [["-s", destination.serial]]
    if token is not None:
        selectors.insert(0, ["-t", token])
    try:
        adb = _android_bin("adb")
        for selector in selectors:
            observed_id = _android_hardware_serial(adb, selector, deadline)
            if observed_id != hardware_id:
                raise DeviceError(
                    f"selected connection {' '.join(selector)} identifies {observed_id}, expected {hardware_id}"
                )
    except DeviceError as error:
        raise DeviceError(
            f"could not verify the selected Android phone: {error}. "
            "Reconnect the intended phone and rerun the command."
        ) from error
