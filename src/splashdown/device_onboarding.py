from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .constants import TARGET_VARIANT_RE
from .device_discovery import discover_for_onboarding
from .device_types import DiscoverySnapshot, PhysicalObservation
from .picker import choose, read_name
from .terminal import terminal_text

ANDROID_GUIDE = "https://developer.android.com/tools/adb#Enabling"
ANDROID_WIRELESS = "https://developer.android.com/tools/adb#connect-to-a-device-over-wi-fi"
IOS_GUIDE = "https://developer.apple.com/documentation/xcode/managing-your-simulated-and-physical-devices-in-device-hub"
IOS_DEVELOPER_MODE = (
    "https://developer.apple.com/documentation/xcode/enabling-developer-mode-on-a-device"
)
_CANCEL_ACTION = 2
_SETUP_SUMMARIES = {
    "android": "Set up an unlisted Android phone",
    "ios": "Set up an unlisted iPhone / iPad",
}


@dataclass(frozen=True)
class DeviceDeclaration:
    variant: str
    platform: str
    hardware_id: str


def _setup(platform: str) -> None:
    lines: tuple[str, ...]
    if platform == "android":
        lines = (
            "\nUSB (recommended for the first connection)",
            "  1. Enable Developer options and USB debugging on the phone.",
            "  2. Connect a data-capable USB cable and unlock the phone.",
            "  3. Accept the phone's debugging authorization prompt.",
            "\nWi-Fi",
            "  Keep the phone and computer on the same Wi-Fi network.",
            "  Open Developer options → Wireless debugging → Pair device with pairing code.",
            "  In another terminal, run: adb pair <pairing-ip>:<pairing-port>",
            "  Enter the code. If the phone does not connect automatically, run:",
            "    adb connect <ip>:<connection-port>",
            "  The connection port is on the main Wireless debugging page, not the pairing dialog.",
            "\nCheck the connection with: adb devices -l",
            "  device: ready to scan · unauthorized: accept the prompt · offline: reconnect",
            "  If adb is missing, install Android SDK Platform-Tools.",
            f"\nUSB help: {ANDROID_GUIDE}",
            f"Wi-Fi help: {ANDROID_WIRELESS}\n",
        )
    else:
        lines = (
            "\nConnect and pair",
            "  1. Connect the unlocked iPhone or iPad to your Mac by cable.",
            "  2. Tap Trust if asked, then finish pairing in Xcode's Device Hub.",
            "  3. For Wi-Fi, keep the phone and Mac on the same network.",
            "\nBefore running an app",
            "  Enable Developer Mode on the phone. App signing is configured in Xcode.",
            "  Pairing makes the phone discoverable; it does not verify app signing or installation.",
            "  If devicectl is missing, install and select full Xcode.",
            f"\nPairing and Wi-Fi help: {IOS_GUIDE}",
            f"Developer Mode: {IOS_DEVELOPER_MODE}\n",
        )
    print("\n".join(lines), file=sys.stderr)


def _phone_label(device: PhysicalObservation) -> str:
    name = device.name.replace("_", " ") if device.platform == "android" else device.name
    platform = "Android" if device.platform == "android" else "iOS"
    return f"{name} · {platform}"


def _show_scan(snapshot: DiscoverySnapshot) -> None:
    if snapshot.issues:
        print("\nDiscovery problems", file=sys.stderr)
        for issue in snapshot.issues:
            print(f"  {terminal_text(issue)}", file=sys.stderr)
    blocked = [device for device in snapshot.devices if not device.selectable]
    if blocked:
        print("\nPhones needing attention", file=sys.stderr)
        for device in blocked:
            print(
                f"  {terminal_text(_phone_label(device))} ({terminal_text(device.state)})\n"
                f"    {terminal_text(device.detail)}",
                file=sys.stderr,
            )
    if not any(device.selectable for device in snapshot.devices):
        print(
            "\nNo phones are ready. Choose setup help below, or reconnect and scan again.",
            file=sys.stderr,
        )
    print(file=sys.stderr)


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
            f"\nReview phone target\n  Name: {variant}\n  Phone: {terminal_text(_phone_label(device))}\n"
            f"  Hardware ID: {terminal_text(str(device.hardware_id))}\n  Save to: {terminal_text(str(destination))}\n",
            file=sys.stderr,
        )
        action = choose(
            "Save this phone?",
            ["Save target", "Choose another name", "Cancel"],
            summaries=[f"Save target: {variant}", "Choose another name", "Cancelled"],
        )
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
        print("Scanning for phones...", end="\r", file=sys.stderr, flush=True)
        snapshot = discover_for_onboarding(platform)
        print("\x1b[2K", end="", file=sys.stderr)
        _show_scan(snapshot)
        available = [device for device in snapshot.devices if device.selectable]
        labels = [f"{_phone_label(device)} · {device.hardware_id}" for device in available]
        setup_summary = _SETUP_SUMMARIES[platform] if platform else "Set up an unlisted phone"
        action = choose(
            "Choose a phone",
            [*labels, "Set up an unlisted phone", "Scan again", "Cancel"],
            summaries=[
                *[f"Selected phone: {label}" for label in labels],
                setup_summary,
                "Scan again",
                "Cancelled",
            ],
        )
        if action is None or action == len(available) + 2:
            return None
        if action < len(available):
            return _name_device(available[action], destination, names)
        if action == len(available):
            setup_platform = platform
            if setup_platform is None:
                choice = choose(
                    "Which kind of phone?",
                    ["Android", "iPhone / iPad", "Back"],
                    summaries=[
                        _SETUP_SUMMARIES["android"],
                        _SETUP_SUMMARIES["ios"],
                        "Back to phones",
                    ],
                )
                if choice is None or choice == _CANCEL_ACTION:
                    continue
                setup_platform = "android" if choice == 0 else "ios"
            _setup(setup_platform)
            action = choose(
                "Ready to look for the phone?",
                ["Phone setup complete · scan again", "Back to phones", "Cancel"],
                summaries=["Phone setup complete", "Back to phones", "Cancelled"],
            )
            if action is None or action == _CANCEL_ACTION:
                return None
