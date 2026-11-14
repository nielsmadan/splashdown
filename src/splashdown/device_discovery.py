from __future__ import annotations

import ipaddress
import re
import socket
import sys
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .device_android import _android_physical_observations
from .device_ios import _ios_physical_observations
from .device_tools import DISCOVERY_TIMEOUT
from .device_types import DiscoverySnapshot, PhysicalObservation
from .errors import DeviceError

_ADB_ROW_COLUMNS = 2
_MAX_ADB_PORT = 65535


def _discover_platform(platform: str, timeout: float) -> DiscoverySnapshot:
    try:
        devices = (
            _ios_physical_observations(timeout=timeout)
            if platform == "ios"
            else _android_physical_observations(timeout=timeout, include_names=True)
        )
        return DiscoverySnapshot(tuple(devices), ())
    except (DeviceError, OSError, UnicodeError) as error:
        return DiscoverySnapshot((), (f"{platform}: {error}",))


def discover_for_onboarding(
    platform: str | None = None, *, timeout: float = DISCOVERY_TIMEOUT
) -> DiscoverySnapshot:
    if platform not in {None, "ios", "android"}:
        raise DeviceError(f"unsupported physical-device platform: {platform}")
    platforms = (
        [platform]
        if platform
        else (["ios", "android"] if sys.platform == "darwin" else ["android"])
    )
    with ThreadPoolExecutor(max_workers=len(platforms)) as executor:
        futures = [executor.submit(_discover_platform, name, timeout) for name in platforms]
        snapshots = [future.result() for future in futures]
    return DiscoverySnapshot(
        tuple(device for snapshot in snapshots for device in snapshot.devices),
        tuple(issue for snapshot in snapshots for issue in snapshot.issues),
    )


class UnsupportedADBObservation(ValueError):
    pass


@dataclass(frozen=True)
class ExistingADB:
    devices: tuple[PhysicalObservation, ...] = ()
    idle: bool = False


def _adb_endpoint(env: Mapping[str, str]) -> tuple[int, str | tuple[str, int]]:
    configured = env.get("ADB_SERVER_SOCKET")
    if configured is not None:
        if configured.startswith("localfilesystem:"):
            path = configured.removeprefix("localfilesystem:")
            if not path.startswith("/") or "\0" in path:
                raise ValueError("ADB_SERVER_SOCKET requires an absolute filesystem socket path")
            return socket.AF_UNIX, path
        if configured.startswith("localabstract:") and sys.platform == "linux":
            name = configured.removeprefix("localabstract:")
            if not name or "\0" in name:
                raise ValueError("invalid abstract ADB server socket")
            return socket.AF_UNIX, "\0" + name
        if not configured.startswith("tcp:"):
            raise UnsupportedADBObservation(
                "unsupported ADB_SERVER_SOCKET; no default endpoint was queried"
            )
        address = configured.removeprefix("tcp:")
        if ":" in address:
            host, port = address.rsplit(":", 1)
            host = host.removeprefix("[").removesuffix("]")
        else:
            host, port = "127.0.0.1", address
    else:
        host = env.get("ANDROID_ADB_SERVER_ADDRESS", "127.0.0.1")
        port = env.get("ANDROID_ADB_SERVER_PORT", "5037")
    if host == "localhost":
        host = "127.0.0.1"
    try:
        numeric = ipaddress.ip_address(host)
    except ValueError as error:
        raise UnsupportedADBObservation(
            "ADB server hostname observation is unsupported; use a numeric endpoint to avoid unbounded DNS lookup"
        ) from error
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= _MAX_ADB_PORT:
        raise ValueError("ADB server port must be an integer from 1 to 65535")
    return (socket.AF_INET6 if isinstance(numeric, ipaddress.IPv6Address) else socket.AF_INET), (
        str(numeric),
        int(port),
    )


def _adb_timeout(connection: socket.socket, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("existing ADB server observation exceeded its deadline")
    connection.settimeout(remaining)


def _adb_receive(connection: socket.socket, size: int, deadline: float) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        _adb_timeout(connection, deadline)
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise ValueError("truncated existing ADB server response")
        chunks.extend(chunk)
    return bytes(chunks)


def _adb_payload(connection: socket.socket, deadline: float) -> bytes:
    raw_size = _adb_receive(connection, 4, deadline)
    if re.fullmatch(rb"[0-9a-fA-F]{4}", raw_size) is None:
        raise ValueError("invalid existing ADB server payload length")
    return _adb_receive(connection, int(raw_size, 16), deadline)


def _existing_adb_devices(payload: bytes) -> tuple[PhysicalObservation, ...]:
    devices = []
    for line in payload.decode("utf-8", errors="strict").split("\n"):
        if not line:
            continue
        fields = line.split()
        if len(fields) < _ADB_ROW_COLUMNS:
            raise ValueError("invalid existing ADB device row")
        serial, state = fields[:2]
        if serial.startswith("emulator-"):
            continue
        if (
            state == "no"
            and len(fields) > _ADB_ROW_COLUMNS
            and fields[2] in {"permissions", "permissions;"}
        ):
            state = "no permissions"
        elif state not in {
            "device",
            "offline",
            "unauthorized",
            "recovery",
            "bootloader",
            "sideload",
            "rescue",
            "host",
            "authorizing",
            "connecting",
            "detached",
        }:
            raise ValueError(f"unknown existing ADB device state: {state}")
        metadata = dict(field.split(":", 1) for field in fields[2:] if ":" in field)
        transport = metadata.get("transport_id")
        if transport is not None and (not transport.isascii() or not transport.isdecimal()):
            raise ValueError("invalid existing ADB transport identifier")
        usb = bool(metadata.get("usb"))
        connection_type = (
            "usb" if usb else "network" if ":" in serial or "._tcp" in serial else None
        )
        devices.append(
            PhysicalObservation(
                "android",
                metadata.get("model", serial),
                None,
                serial,
                state,
                "Hardware identity unverified; the transport endpoint is not a saved hardware ID",
                False,
                metadata.get("transport_id"),
                connection_type,
            )
        )
    return tuple(devices)


def observe_existing_adb(env: Mapping[str, str], *, timeout: float = 3) -> ExistingADB:
    family, endpoint = _adb_endpoint(env)
    deadline = time.monotonic() + timeout
    with socket.socket(family, socket.SOCK_STREAM) as connection:
        _adb_timeout(connection, deadline)
        try:
            connection.connect(endpoint)
        except ConnectionRefusedError:
            return ExistingADB(idle=True)
        except FileNotFoundError:
            if family == socket.AF_UNIX:
                return ExistingADB(idle=True)
            raise
        _adb_timeout(connection, deadline)
        connection.sendall(b"000ehost:devices-l")
        status = _adb_receive(connection, 4, deadline)
        if status == b"FAIL":
            detail = _adb_payload(connection, deadline).decode("utf-8", errors="replace")
            raise ValueError(f"existing ADB server rejected observation: {detail}")
        if status != b"OKAY":
            raise ValueError("invalid existing ADB server status")
        payload = _adb_payload(connection, deadline)
        _adb_timeout(connection, deadline)
        if connection.recv(1):
            raise ValueError("unexpected trailing existing ADB server response")
        return ExistingADB(_existing_adb_devices(payload))
