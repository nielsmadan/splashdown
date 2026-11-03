from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .constants import TARGET_VARIANT_RE
from .device_discovery import discover_for_onboarding
from .device_types import PhysicalObservation
from .picker import choose, read_name
from .terminal import terminal_text

ANDROID_GUIDE = "https://developer.android.com/tools/adb#Enabling"
ANDROID_WIRELESS = "https://developer.android.com/tools/adb#connect-to-a-device-over-wi-fi"
IOS_GUIDE = "https://developer.apple.com/documentation/xcode/managing-your-simulated-and-physical-devices-in-device-hub"
IOS_DEVELOPER_MODE = (
    "https://developer.apple.com/documentation/xcode/enabling-developer-mode-on-a-device"
)
_CANCEL_ACTION = 2


@dataclass(frozen=True)
class DeviceDeclaration:
    variant: str
    platform: str
    hardware_id: str


def _setup(platform: str) -> None:
    if platform == "android":
        lines = (
            "Android: enable Developer options and USB debugging on the phone.",
            "Connect a data-capable USB cable, unlock the phone and accept its debugging prompt.",
            "unauthorized: accept the phone's debugging authorization prompt.",
            "offline: check the cable and unlock/reconnect the phone, then rescan.",
            "Install Android SDK Platform-Tools if adb is missing.",
            f"USB setup: {ANDROID_GUIDE}",
            f"Wireless setup: {ANDROID_WIRELESS}",
        )
    else:
        lines = (
            "iPhone/iPad: connect a cable to your Mac, unlock the device and tap Trust if asked.",
            "In Xcode's Device Hub, select the device and follow any pairing instructions.",
            "Install/select full Xcode if devicectl is missing.",
            "Developer Mode is needed to run development apps; pairing alone does not prove",
            "signing, installation or launch readiness. Enable it when prompted for development.",
            f"Pairing and wireless setup: {IOS_GUIDE}",
            f"Developer Mode: {IOS_DEVELOPER_MODE}",
        )
    print("\n".join(lines), file=sys.stderr)


def _suggest_name(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-").lower()
    return value if value and value[0].isascii() and value[0].isalpha() else "phone"


def _name_device(
    device: PhysicalObservation, destination: Path, names: set[str]
) -> DeviceDeclaration | None:
    suggested = _suggest_name(device.name)
    while True:
        answer = read_name(f"Target name [{suggested}]: ")
        if answer is None:
            return None
        variant = answer.strip() or suggested
        if not TARGET_VARIANT_RE.fullmatch(variant):
            print(
                "Use a letter first, then letters, digits, hyphens or underscores.", file=sys.stderr
            )
            continue
        if variant in names:
            print(f"Target device.{variant} already exists. Choose another name.", file=sys.stderr)
            continue
        print(
            f"\nTarget: device.{variant}\nPlatform: {device.platform}\n"
            f"Hardware ID: {terminal_text(str(device.hardware_id))}\nSave to: {terminal_text(str(destination))}",
            file=sys.stderr,
        )
        action = choose("Save this target?", ["Save target", "Choose another name", "Cancel"])
        if action is None or action == _CANCEL_ACTION:
            return None
        if action == 0:
            if device.hardware_id is None:
                raise ValueError("selected phone has no hardware identifier")
            return DeviceDeclaration(variant, device.platform, device.hardware_id)


def onboard_device(
    platform: str | None, destination: Path, names: set[str]
) -> DeviceDeclaration | None:
    while True:
        print("Scanning for phones...", file=sys.stderr, flush=True)
        snapshot = discover_for_onboarding(platform)
        for issue in snapshot.issues:
            print(f"warning: {terminal_text(issue)}", file=sys.stderr)
        for device in snapshot.devices:
            if not device.selectable:
                print(
                    terminal_text(
                        f"{device.platform}: {device.name} [{device.transport_id}] "
                        f"{device.state}: {device.detail}"
                    ),
                    file=sys.stderr,
                )
        available = [device for device in snapshot.devices if device.selectable]
        if not available:
            print("No selectable phones found in this scan.", file=sys.stderr)
        labels = [
            f"{device.name} ({device.platform}) [{device.hardware_id}]" for device in available
        ]
        action = choose("Choose a phone", [*labels, "Set up an unlisted phone", "Rescan", "Cancel"])
        if action is None or action == len(available) + 2:
            return None
        if action < len(available):
            return _name_device(available[action], destination, names)
        if action == len(available):
            setup_platform = platform
            if setup_platform is None:
                choice = choose("Which phone?", ["Android", "iPhone / iPad", "Back"])
                if choice is None or choice == _CANCEL_ACTION:
                    continue
                setup_platform = "android" if choice == 0 else "ios"
            _setup(setup_platform)
            if choose("After completing the phone setup", ["Rescan", "Cancel"]) != 0:
                return None
