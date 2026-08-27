from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .capabilities import translate_tool_errors
from .constants import state_directory
from .device_tools import (
    DISCOVERY_TIMEOUT,
    MUTATION_TIMEOUT,
    check_output_finite,
    run_finite,
)
from .errors import CapabilityError, DeviceError

_AVD_INVALID_RE = re.compile(r"[^A-Za-z0-9._-]")
_ADB_ROW_COLS = 2


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
    installed = re.findall(r"^\s*(system-images;android-\d+;[^\s|]+)", out, re.M)
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


def _android_running_serial(avd_name: str) -> str | None:
    """Match a running emulator to an AVD through adb."""
    adb = _android_bin("adb")
    try:
        with translate_tool_errors("android", "adb", "install Android SDK platform-tools"):
            out = check_output_finite(
                [adb, "devices"],
                operation="adb devices",
                timeout=DISCOVERY_TIMEOUT,
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
        try:
            with translate_tool_errors("android", "adb", "install Android SDK platform-tools"):
                names = (
                    subprocess.check_output(
                        [adb, "-s", serial, "emu", "avd", "name"],
                        stderr=subprocess.DEVNULL,
                        timeout=2,
                    )
                    .decode()
                    .splitlines()
                )
            if names and names[0].strip() == avd_name:
                return serial
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
    return None


def android_boot(avd_name: str, *, state_dir: Path | None = None) -> str:
    """Start an emulator in the background and return its adb serial."""
    serial = _android_running_serial(avd_name)
    if serial:
        return serial
    emulator = _android_bin("emulator")
    from .recipe import _slug as recipe_slug  # noqa: PLC0415

    log = (state_dir or state_directory()) / f"emulator-{recipe_slug(avd_name)}.log"
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
    for _ in range(60):
        time.sleep(1)
        serial = _android_running_serial(avd_name)
        if serial:
            return serial
    raise DeviceError(f"AVD '{avd_name}' did not come up within 60s; see {log}")


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


def _android_physical_devices(*, timeout: float = DISCOVERY_TIMEOUT) -> list[dict[str, str]]:
    """Return connected Android hardware, excluding emulator serials."""
    try:
        with translate_tool_errors("android", "adb", "install Android SDK platform-tools"):
            out = check_output_finite(
                [_android_bin("adb"), "devices", "-l"],
                operation="adb devices",
                timeout=timeout,
                stderr=subprocess.DEVNULL,
            ).decode()
    except subprocess.CalledProcessError as error:
        raise DeviceError(f"adb devices failed: exit {error.returncode}") from error
    devices: list[dict[str, str]] = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < _ADB_ROW_COLS or parts[1] != "device":
            continue
        serial = parts[0]
        if serial.startswith("emulator-"):
            continue
        model = next(
            (part.split(":", 1)[1] for part in parts[2:] if part.startswith("model:")), serial
        )
        devices.append({"id": serial, "name": model, "platform": "android"})
    return devices
