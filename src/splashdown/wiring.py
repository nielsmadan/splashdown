from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

from . import RECIPE_NAME
from .devices import DeviceError, detect_framework, resolve_app_dir
from .hooks import _detect_hook_manager, _ensure_post_checkout_hook, _lefthook_config_path
from .recipe import Recipe

# ---------- framework wiring (doctor) ----------
#
# The wiring-check registries (_RN_WIRING_CHECKS, _HOOK_WIRING_CHECK) are the
# per-framework spec shipped with the tool. Each WiringCheck names
# a small fact about the project (e.g. "metro.config.js consumes RCT_METRO_PORT")
# that splashdown can inspect and, where safely mechanical, repair.


class WiringCheck(NamedTuple):
    id: str
    description: str
    applies: Callable[[Path], bool]
    # Returns ("ok", detail) when wired, ("problem", detail) when not.
    detect: Callable[[Path], tuple[str, str]]
    # None = manual-only check (no safe auto-fix).
    autofix: Callable[[Path], None] | None
    # Used when autofix is None or when --fix isn't requested. Returns the
    # exact change the user should apply themselves.
    manual_instructions: Callable[[Path], str] | None


# RN wiring checks accumulate here as the rn-* helper functions are defined
# below; ReactNativeProfile picks them up at registry-build time.
_RN_WIRING_CHECKS: list[WiringCheck] = []


def _resolve_doctor_framework(cwd: Path, override: str | None) -> str | None:
    """Pick the framework for doctor to check. Returns None if undetectable."""
    try:
        return _resolve_doctor_target(cwd, override)[0]
    except DeviceError:
        return None


def _resolve_doctor_target(cwd: Path, override: str | None) -> tuple[str, Path]:
    """The framework to check and the directory to check it in. Raises DeviceError
    with a specific reason when nothing resolves."""
    if override:
        return (override, cwd)
    recipe_path = cwd / RECIPE_NAME
    recipe = Recipe.load(recipe_path) if recipe_path.exists() else Recipe({}, recipe_path)
    framework = detect_framework(cwd, recipe)
    return (framework, resolve_app_dir(cwd, recipe, framework))


def _wiring_checks_for_framework(framework: str, cwd: Path) -> list[WiringCheck]:
    """Resolve the doctor's check list for a framework name. Profiles take an
    AppInventory; synthesize one rooted at cwd."""
    from .scanner import PROFILES, AppInventory  # noqa: PLC0415

    if framework in PROFILES:
        app = AppInventory(name="main", path=cwd, profile=framework)
        checks: list[WiringCheck] = PROFILES[framework].wiring_checks(app)
        return checks
    return []


def cmd_doctor(cwd: Path, *, fix: bool = False, framework_override: str | None = None) -> int:
    """Run framework-aware wiring checks. With fix=True, apply safe autofixes."""
    import sys  # noqa: PLC0415

    try:
        framework, app_dir = _resolve_doctor_target(cwd, framework_override)
    except DeviceError as e:
        print(f"doctor: {e}", file=sys.stderr)
        print("  pass --framework=NAME to check a specific framework.", file=sys.stderr)
        return 1
    if app_dir != cwd:
        print(f"doctor: checking {app_dir.relative_to(cwd)} (`{framework}`)", file=sys.stderr)
    checks = _wiring_checks_for_framework(framework, app_dir)
    if not checks:
        print(f"doctor: no wiring checks defined for framework `{framework}`.", file=sys.stderr)
        return 0

    bad = 0
    for check in checks:
        if not check.applies(app_dir):
            print(f"  -  {check.id}: not applicable", file=sys.stderr)
            continue
        status, detail = check.detect(app_dir)
        if status == "ok":
            print(f"  ✓  {check.id}: {check.description}", file=sys.stderr)
            continue
        if fix and check.autofix is not None:
            try:
                check.autofix(app_dir)
            except Exception as e:  # noqa: BLE001 - report rather than crash whole run
                print(f"  ✗  {check.id}: autofix failed: {e}", file=sys.stderr)
                bad += 1
                continue
            status_after, detail_after = check.detect(app_dir)
            if status_after == "ok":
                print(f"  ✓  {check.id}: {check.description} (fixed)", file=sys.stderr)
                continue
            print(f"  ✗  {check.id}: still problem after autofix: {detail_after}", file=sys.stderr)
            if check.manual_instructions is not None:
                for line in check.manual_instructions(app_dir).splitlines():
                    print(f"        {line}", file=sys.stderr)
            bad += 1
            continue
        print(f"  ✗  {check.id}: {detail}", file=sys.stderr)
        if check.manual_instructions is not None:
            for line in check.manual_instructions(app_dir).splitlines():
                print(f"        {line}", file=sys.stderr)
        bad += 1
    return 0 if bad == 0 else 1


# ---------- React Native wiring checks ----------


def _rn_hook_detect(cwd: Path) -> tuple[str, str]:
    manager = _detect_hook_manager(cwd)
    if manager == "lefthook":
        path = _lefthook_config_path(cwd)
        if path.exists():
            text = path.read_text()
            if re.search(r"post-checkout\s*:", text) and re.search(r"\brun\s*:\s*splash\b", text):
                return ("ok", "lefthook post-checkout invokes splash")
        return ("problem", "lefthook detected; post-checkout doesn't invoke splash")
    if manager == "husky":
        hook = cwd / ".husky" / "post-checkout"
        if hook.exists() and "splash" in hook.read_text():
            return ("ok", "husky .husky/post-checkout invokes splash")
        return ("problem", "husky detected; .husky/post-checkout missing or doesn't invoke splash")
    if manager == "core-hookspath-other":
        return ("problem", "core.hooksPath points to a custom dir; can't auto-wire there")
    # Clean: expect .githooks + core.hooksPath = .githooks.
    hook = cwd / ".githooks" / "post-checkout"
    if hook.exists() and "splash" in hook.read_text():
        try:
            out = (
                subprocess.check_output(
                    ["git", "config", "--get", "core.hooksPath"],
                    cwd=cwd,
                    stderr=subprocess.DEVNULL,
                )
                .decode()
                .strip()
            )
            if out == ".githooks":
                return ("ok", ".githooks/post-checkout invokes splash, core.hooksPath set")
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
        return (
            "problem",
            ".githooks/post-checkout exists but core.hooksPath isn't set to .githooks",
        )
    return ("problem", "no post-checkout hook invokes splash")


def _rn_hook_manual(cwd: Path) -> str:
    return (
        "core.hooksPath is set to a non-splashdown directory. Add a post-checkout\n"
        "hook there that runs `splash` (see examples/.githooks/post-checkout)."
    )


def _autofix_ensure_post_checkout_hook(cwd: Path) -> None:
    _ensure_post_checkout_hook(cwd)


_RN_WIRING_CHECKS.append(
    WiringCheck(
        id="rn-hook",
        description="post-checkout fires `splash`",
        applies=lambda cwd: True,
        detect=_rn_hook_detect,
        autofix=_autofix_ensure_post_checkout_hook,
        manual_instructions=_rn_hook_manual,
    ),
)


# Recognized metro.config.js shapes:
#   1. `port: <number>` (literal) — rewritten to read `process.env.RCT_METRO_PORT`
#      while keeping the literal as the fallback.
#   2. a `server: { ... }` block with no port — we add the port line to it.
#   3. a `const config = {` / `module.exports = {` object literal with no server
#      block — we inject a `server: { port: ... }` block at its top.
_METRO_LITERAL_PORT_RE = re.compile(r"\bport\s*:\s*(\d+)\b")
_METRO_SERVER_RE = re.compile(r"\bserver\s*:\s*\{")
_METRO_CONFIG_OBJ_RE = re.compile(r"(?:const\s+config\s*=|module\.exports\s*=)\s*\{")
_METRO_PORT_LINE = "port: Number(process.env.RCT_METRO_PORT) || 8081,"


def _rn_metro_applies(cwd: Path) -> bool:
    return (cwd / "metro.config.js").exists()


def _rn_metro_detect(cwd: Path) -> tuple[str, str]:
    text = (cwd / "metro.config.js").read_text()
    if "process.env.RCT_METRO_PORT" in text:
        return ("ok", "metro.config.js reads process.env.RCT_METRO_PORT")
    if _METRO_LITERAL_PORT_RE.search(text):
        return ("problem", "metro.config.js hardcodes a literal port; autofixable")
    if _rn_metro_inject(text) is not None:
        return ("problem", "metro.config.js has no server.port; autofixable")
    return ("problem", "metro.config.js doesn't reference RCT_METRO_PORT")


def _rn_metro_inject(text: str) -> str | None:
    """Wire server.port to RCT_METRO_PORT in a config that has no port literal.

    Adds the port to an existing `server: {` block, or, failing that, injects a
    `server` block at the top of the config object literal. Returns the rewritten
    text, or None if no recognizable injection point exists.
    """
    server = _METRO_SERVER_RE.search(text)
    if server:
        at = server.end()
        return text[:at] + f"\n    {_METRO_PORT_LINE}" + text[at:]
    obj = _METRO_CONFIG_OBJ_RE.search(text)
    if obj:
        at = obj.end()
        return text[:at] + f"\n  server: {{\n    {_METRO_PORT_LINE}\n  }}," + text[at:]
    return None


def _rn_metro_autofix(cwd: Path) -> None:
    import sys  # noqa: PLC0415

    path = cwd / "metro.config.js"
    text = path.read_text()
    if "process.env.RCT_METRO_PORT" in text:
        return
    m = _METRO_LITERAL_PORT_RE.search(text)
    if m:
        new_text = (
            text[: m.start()]
            + f"port: Number(process.env.RCT_METRO_PORT) || {m.group(1)}"
            + text[m.end() :]
        )
        path.write_text(new_text)
        print(f"patched metro.config.js (RCT_METRO_PORT, fallback {m.group(1)})", file=sys.stderr)
        return
    injected = _rn_metro_inject(text)
    if injected is None:
        return  # unrecognized shape — doctor will surface manual_instructions
    path.write_text(injected)
    print("patched metro.config.js (added server.port, fallback 8081)", file=sys.stderr)


def _rn_metro_manual(cwd: Path) -> str:
    return (
        "Edit metro.config.js so server.port reads RCT_METRO_PORT, keeping a fallback:\n"
        "    server: {\n"
        "      port: Number(process.env.RCT_METRO_PORT) || 8081,\n"
        "    },"
    )


_RN_WIRING_CHECKS.append(
    WiringCheck(
        id="rn-metro-config",
        description="metro.config.js consumes RCT_METRO_PORT",
        applies=_rn_metro_applies,
        detect=_rn_metro_detect,
        autofix=_rn_metro_autofix,
        manual_instructions=_rn_metro_manual,
    ),
)


# `--port 8083` or `--port=8083` in a script string — exactly the override that
# stops RCT_METRO_PORT from taking effect.
_PKG_PORT_RE = re.compile(r"\s+--port[=\s]\d+")
_PKG_RN_SCRIPTS = ("start", "ios", "android")  # default RN script names
# Only `react-native start` boots Metro. Match that specifically so we don't strip
# `--port` from unrelated tools like `react-native-test-runner --port 4000`.
_PKG_RN_START_RE = re.compile(r"\breact-native\s+start\b")


def _rn_pkg_applies(cwd: Path) -> bool:
    return (cwd / "package.json").exists()


def _pkg_scripts_with_port(data: dict[str, Any]) -> list[str]:
    """Return names of scripts that override RCT_METRO_PORT with --port."""
    scripts = data.get("scripts") or {}
    hits: list[str] = []
    for name, value in scripts.items():
        if not isinstance(value, str):
            continue
        # Target the common RN scripts, plus any script that boots Metro via
        # `react-native start` (not merely any script mentioning react-native).
        if (name in _PKG_RN_SCRIPTS or _PKG_RN_START_RE.search(value)) and _PKG_PORT_RE.search(
            value
        ):
            hits.append(name)
    return hits


def _rn_pkg_detect(cwd: Path) -> tuple[str, str]:
    try:
        data = json.loads((cwd / "package.json").read_text())
    except (json.JSONDecodeError, OSError) as e:
        return ("problem", f"could not read package.json: {e}")
    hits = _pkg_scripts_with_port(data)
    if hits:
        return ("problem", f"--port hardcoded in scripts: {', '.join(hits)}")
    return ("ok", "package.json scripts don't hardcode --port")


def _rn_pkg_autofix(cwd: Path) -> None:
    import sys  # noqa: PLC0415

    path = cwd / "package.json"
    data = json.loads(path.read_text())
    scripts = data.get("scripts") or {}
    changed = False
    for name in _pkg_scripts_with_port(data):
        new_val = _PKG_PORT_RE.sub("", scripts[name])
        if new_val != scripts[name]:
            scripts[name] = new_val
            changed = True
    if not changed:
        return
    data["scripts"] = scripts
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print("rewrote package.json (stripped --port from scripts)", file=sys.stderr)


def _rn_pkg_manual(cwd: Path) -> str:
    return (
        "Remove `--port <N>` from any react-native script in package.json so the\n"
        "RN CLI reads RCT_METRO_PORT from the environment instead."
    )


_RN_WIRING_CHECKS.append(
    WiringCheck(
        id="rn-pkg-port",
        description="package.json scripts don't override --port",
        applies=_rn_pkg_applies,
        detect=_rn_pkg_detect,
        autofix=_rn_pkg_autofix,
        manual_instructions=_rn_pkg_manual,
    ),
)


# Sentinel-wrapped block written into ios/.xcode.env. Sentinels make autofix
# idempotent (find by sentinel pair, replace contents) and let the user identify
# what's tool-managed vs hand-edited.
_XCODE_BEGIN = "# >>> splashdown-managed RCT_METRO_PORT >>>"
_XCODE_END = "# <<< splashdown-managed RCT_METRO_PORT <<<"
_XCODE_BLOCK = f"""{_XCODE_BEGIN}
# splashdown ships this block. RCT_METRO_PORT is baked into the iOS binary via
# GCC_PREPROCESSOR_DEFINITIONS (RCTBundleURLProvider's defaultPort), so the app
# must be rebuilt after a port change. Honour a value set by `react-native
# run-ios`; else read this checkout's splashdown.env; else fall back to 8083.
if [ -z "${{RCT_METRO_PORT:-}}" ] && [ -f "${{SRCROOT}}/../splashdown.env" ]; then
  export RCT_METRO_PORT="$(grep '^RCT_METRO_PORT=' "${{SRCROOT}}/../splashdown.env" | cut -d= -f2)"
fi
export RCT_METRO_PORT="${{RCT_METRO_PORT:-8083}}"
{_XCODE_END}
"""

_XCODE_BLOCK_RE = re.compile(
    re.escape(_XCODE_BEGIN) + r".*?" + re.escape(_XCODE_END) + r"\n?",
    re.DOTALL,
)
# A *static literal* export — `export RCT_METRO_PORT=8083`, no variable
# references. The intentionally narrow match keeps autofix from mangling
# user-written conditional / shell-substitution-based wirings.
_XCODE_LITERAL_EXPORT_RE = re.compile(
    r"^[ \t]*export[ \t]+RCT_METRO_PORT[ \t]*=[ \t]*\d+[ \t]*\n?",
    re.MULTILINE,
)


def _rn_xcode_applies(cwd: Path) -> bool:
    return (cwd / "ios" / ".xcode.env").exists()


def _rn_xcode_detect(cwd: Path) -> tuple[str, str]:
    text = (cwd / "ios" / ".xcode.env").read_text()
    # A reference to splashdown.env means *somebody* wired it to the per-checkout
    # env file — sentinel block, hand-written conditional, etc. All fine.
    if "splashdown.env" in text:
        return ("ok", "ios/.xcode.env reads RCT_METRO_PORT from splashdown.env")
    if _XCODE_LITERAL_EXPORT_RE.search(text):
        return ("problem", "ios/.xcode.env statically exports a literal RCT_METRO_PORT")
    return ("problem", "ios/.xcode.env doesn't wire RCT_METRO_PORT to splashdown")


def _rn_xcode_autofix(cwd: Path) -> None:
    import sys  # noqa: PLC0415

    path = cwd / "ios" / ".xcode.env"
    text = path.read_text()
    if "splashdown.env" in text:
        return
    # Strip any literal-digit export so the file has one source of truth.
    text = _XCODE_LITERAL_EXPORT_RE.sub("", text)
    # Strip any prior sentinel block (only reachable if sentinels existed but no
    # splashdown.env reference — defensive).
    text = _XCODE_BLOCK_RE.sub("", text)
    # Append our block at the end. Ensure exactly one separating newline.
    text = text.rstrip() + ("\n\n" if text.strip() else "")
    text += _XCODE_BLOCK
    path.write_text(text)
    print("rewrote ios/.xcode.env (splashdown-managed RCT_METRO_PORT block)", file=sys.stderr)


def _rn_xcode_manual(cwd: Path) -> str:
    return (
        "Edit ios/.xcode.env so RCT_METRO_PORT is honoured-if-set, else read from\n"
        "splashdown.env, else fall back to 8083. See README ('Framework wiring')."
    )


_RN_WIRING_CHECKS.append(
    WiringCheck(
        id="rn-xcode-env",
        description="ios/.xcode.env wires RCT_METRO_PORT to splashdown.env",
        applies=_rn_xcode_applies,
        detect=_rn_xcode_detect,
        autofix=_rn_xcode_autofix,
        manual_instructions=_rn_xcode_manual,
    ),
)


# Shared "post-checkout hook fires `splash`" wiring check — also used by native
# presets, which otherwise have no per-checkout wiring.
_HOOK_WIRING_CHECK = WiringCheck(
    id="hook",
    description="post-checkout fires `splash`",
    applies=lambda cwd: True,
    detect=_rn_hook_detect,
    autofix=_autofix_ensure_post_checkout_hook,
    manual_instructions=_rn_hook_manual,
)
