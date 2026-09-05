from __future__ import annotations

import json
import plistlib
import re
import shutil
import subprocess
from pathlib import Path
from xml.parsers.expat import ExpatError

from .wiring import WiringCheck

_LOOPBACK = re.compile(
    r"(?<![\w.:-])(?:localhost\.?|127(?:\.\d{1,3}){3}|\[?::1\]?)(?![\w.-])",
    re.IGNORECASE,
)
_CHECK_TIMEOUT = 3


def loopback_warnings(resolved: dict[str, str]) -> list[str]:
    keys = sorted(key for key, value in resolved.items() if _LOOPBACK.search(value))
    if not keys:
        return []
    subject = "Resource" if len(keys) == 1 else "Resources"
    verb = "contains" if len(keys) == 1 else "contain"
    return [
        f"{subject} {', '.join(keys)} {verb} loopback addresses. On a physical device these refer to "
        "the device itself. Use the development machine's reachable LAN address for host "
        "services, or verify that you configured port forwarding."
    ]


def _local_network_description(data: object, source: str) -> list[str]:
    if not isinstance(data, dict):
        return [f"{source}: expected a plist dictionary; local-network description is unverified."]
    description = data.get("NSLocalNetworkUsageDescription")
    if not isinstance(description, str) or not description.strip():
        return [
            f"{source}: add a nonempty NSLocalNetworkUsageDescription explaining the app's "
            "connection to Metro and local services, then rebuild and allow Local Network "
            "access on the iPhone."
        ]
    if "$(" in description or "${" in description:
        return [f"{source}: local-network description uses build variables; verify the built app."]
    return []


def local_network_warnings(app_dir: Path, framework: str) -> list[str]:
    candidates = sorted((app_dir / "ios").glob("*/Info.plist"))
    candidates = [path for path in candidates if path.parent.name not in {"Pods", "build"}]
    if (app_dir / "ios" / "Info.plist").exists():
        candidates.append(app_dir / "ios" / "Info.plist")
    warnings: list[str] = []
    app_plists = 0
    for path in candidates:
        source = str(path.relative_to(app_dir))
        try:
            data = plistlib.loads(path.read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException, ExpatError) as error:
            warnings.append(
                f"{source}: could not read plist ({error}); local network is unverified."
            )
        else:
            if isinstance(data, dict) and (
                "NSExtension" in data or data.get("CFBundlePackageType") in ("BNDL", "FMWK")
            ):
                continue
            app_plists += 1
            warnings.extend(_local_network_description(data, source))
    if app_plists or warnings:
        return warnings
    if framework == "expo":
        if any(app_dir.glob("app.config.*")):
            return [
                "Expo uses dynamic app config; verify ios.infoPlist.NSLocalNetworkUsageDescription "
                "in the resolved config and generated iOS app."
            ]
        try:
            data = json.loads((app_dir / "app.json").read_text())
            plist = data.get("expo", {}).get("ios", {}).get("infoPlist", {})
        except (OSError, ValueError, AttributeError):
            return [
                "Could not read Expo app.json; the iOS local-network description is unverified."
            ]
        return _local_network_description(plist, "app.json expo.ios.infoPlist")
    return [
        "No app Info.plist found under ios/; verify NSLocalNetworkUsageDescription in the "
        "built iOS app and allow Local Network access on the iPhone."
    ]


def watchman_available() -> bool:
    return shutil.which("watchman") is not None


def watchman_watch_root(cwd: Path) -> tuple[str, str]:
    try:
        result = subprocess.run(
            ["watchman", "--no-spawn", "--no-local", "watch-list"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_CHECK_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ("problem", "Watchman query timed out after 3 seconds; watch roots are unverified")
    except OSError:
        return ("problem", "Watchman query could not start; watch roots are unverified")
    if result.returncode:
        return ("problem", "Watchman query failed; check that its daemon is available")
    try:
        data = json.loads(result.stdout)
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get("error"):
        return ("problem", "Watchman returned an error or invalid data; watch roots are unverified")
    roots = data.get("roots")
    if not isinstance(roots, list) or any(
        not isinstance(root, str) or not Path(root).is_absolute() for root in roots
    ):
        return ("problem", "Watchman returned invalid roots; watch roots are unverified")
    checkout = cwd.resolve()
    ancestors = sorted(
        {str(Path(root).resolve()) for root in roots if Path(root).resolve() in checkout.parents}
    )
    if ancestors:
        return (
            "problem",
            f"Watchman watches an ancestor of this checkout: {', '.join(ancestors)}. "
            "Review that shared watch before removing it with `watchman watch-del PATH`, "
            "then restart Metro from this checkout.",
        )
    return ("ok", "Watchman has no ancestor watch for this checkout")


WATCHMAN_CHECK = WiringCheck(
    id="watchman-root",
    description="Watchman watches this checkout or has no watch for it",
    applies=lambda _cwd: watchman_available(),
    detect=watchman_watch_root,
    autofix=None,
    manual_instructions=None,
)
