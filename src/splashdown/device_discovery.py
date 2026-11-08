from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor

from .device_android import _android_physical_observations
from .device_ios import _ios_physical_observations
from .device_tools import DISCOVERY_TIMEOUT
from .device_types import DiscoverySnapshot
from .errors import DeviceError


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
