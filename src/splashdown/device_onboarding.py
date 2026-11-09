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
            "\nOn the computer",
            "  Install current Android SDK Platform-Tools and make sure adb is on PATH.",
            "  Run the commands below in another terminal, leaving this guide open.",
            "\nEnable Developer options on the phone",
            "  Open Settings → About phone and tap Build number seven times.",
            "  On Samsung, Build number is inside About phone → Software information.",
            "  Menu names vary. Search Settings for Developer options after enabling it.",
            "\nUSB (recommended for the first connection)",
            "  1. Turn on USB debugging in Developer options.",
            "  2. Connect a data-capable USB cable to the computer and unlock the phone.",
            "  3. Accept Allow USB debugging? on the phone to authorize this computer.",
            "     You can select Always allow from this computer on your own test machine.",
            "  4. Run adb devices -l and look for your phone with state device.",
            "  USB authorization is enough for a cable connection. No adb pair is needed.",
            "\nWi-Fi (Android 11 or newer, no USB connection required)",
            "  1. Connect the phone and computer to the same Wi-Fi network.",
            "  2. Open Developer options → Wireless debugging. Turn it on and allow the network.",
            "  3. Tap Pair device with pairing code. Keep the dialog open.",
            "     It shows a pairing IP address, port and six-digit code.",
            "  4. Run: adb pair <pairing-ip>:<pairing-port>",
            "     Replace the placeholders with that dialog's address and port.",
            "     Enter its code when asked and wait for Successfully paired.",
            "  5. Run adb devices -l. If your phone is missing, return to the main",
            "     Wireless debugging page and use its IP address & Port with:",
            "    adb connect <ip>:<connection-port>",
            "     The connection port is different from the pairing port.",
            "  6. Run adb devices -l again. Your phone should have state device.",
            "  If Wireless debugging turns off, enable it and read the current connection port.",
            "  If this computer is still under Paired devices, try adb connect before re-pairing.",
            "\nIf the phone is missing or not ready",
            "  unauthorized: unlock the phone and accept its debugging prompt.",
            "  offline: reconnect the cable, or use the current Wi-Fi connection address.",
            "  Missing over USB: try another data cable/port and toggle USB debugging off/on.",
            "  Missing over Wi-Fi: check Wireless debugging and that both use the same network.",
            "  Then choose Phone setup complete · scan again below.",
            f"\nUSB help: {ANDROID_GUIDE}",
            f"Wi-Fi help: {ANDROID_WIRELESS}\n",
        )
    else:
        lines = (
            "\nOpen Xcode's device manager",
            "  Install and open full Xcode on the Mac, then choose:",
            "    Xcode → Open Developer Tool → Device Hub",
            "  In older Xcode versions: Window → Devices and Simulators → Devices.",
            "\nPair by USB (recommended for the first connection)",
            "  1. Connect the unlocked iPhone or iPad with a data-capable cable.",
            "     Accept any accessory connection prompt on the Mac.",
            "  2. On the phone, tap Trust in Trust This Computer and enter its passcode if asked.",
            "  3. Select the phone in Device Hub's sidebar and follow any setup instructions.",
            "     If a Pair button appears, click it. If already paired, continue below.",
            "\nEnable Developer Mode",
            "  4. On the phone, open Settings → Privacy & Security → Developer Mode and turn it on.",
            "  5. Tap Restart. After restarting, unlock the phone and confirm Developer Mode.",
            "     Enter the phone's passcode if asked, then return to Xcode to finish setup.",
            "  If Developer Mode is missing, start pairing with the Mac first.",
            "\nWi-Fi after USB pairing",
            "  Put the phone and Mac on the same Wi-Fi network, then unplug the cable.",
            "  Keep the phone unlocked for the first connection and installation.",
            "  A paired phone can show a disconnected tunnel while idle.",
            "\nPair without a cable (iOS / iPadOS 27 or newer, with Device Hub)",
            "  1. Put the phone and Mac on the same Wi-Fi network.",
            "  2. In Device Hub, choose + → Pair Nearby Device and select iPhone / iPad.",
            "  3. Follow the device-specific instructions. Allow local network access if asked.",
            "  4. Select the discovered phone, click Next and enter its displayed PIN on the Mac.",
            "  5. Tap Trust on the phone and complete Developer Mode setup as above.",
            "  On earlier iOS versions, pair by USB first. Wi-Fi use needs IPv6 on the network.",
            "\nCheck the connection",
            "  Wait for Device Hub to finish setup and show the phone under Available.",
            "  In another terminal, run: xcrun devicectl list devices",
            "  If missing, unlock/reconnect the phone and check Xcode for pending prompts.",
            "  Then choose Phone setup complete · scan again below.",
            "\nBefore running an app",
            "  In your app's Xcode project, select a Team under Signing & Capabilities.",
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
