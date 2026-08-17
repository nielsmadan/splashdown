from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

from .hooks import (
    _ensure_post_checkout_hook,
    post_checkout_manual_instructions,
    post_checkout_readiness,
)

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


# Detection is substring- and regex-based across every check here, which makes
# commented-out config the standing hazard: a `// server: { port: process.env.X }`
# left behind from a previous attempt reads exactly like working wiring, and the
# check then prints a ✓ claiming the project is wired. Every detect that scans a
# commentable file strips comments first. These are deliberately lexical, not
# parsers — enough to tell code from commentary, nothing more.


def _strip_hash_comments(text: str) -> str:
    """Drop `#` comments from YAML / .properties / shell text, honouring quotes.
    Indentation is preserved: YAML block structure is read off it."""
    out: list[str] = []
    for line in text.splitlines():
        kept: list[str] = []
        quote = ""
        for ch in line:
            if quote:
                if ch == quote:
                    quote = ""
            elif ch in "\"'":
                quote = ch
            elif ch == "#" and (not kept or kept[-1] in " \t"):
                break
            kept.append(ch)
        out.append("".join(kept).rstrip())
    return "\n".join(out)


def _strip_js_comments(text: str) -> str:
    """Drop `//` and `/* */` comments from JS/JSONC text, honouring quotes.
    Newlines inside block comments are kept so line-oriented checks still line up."""
    out: list[str] = []
    i, n = 0, len(text)
    quote = ""
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\":
                out.append(text[i : i + 2])
                i += 2
                continue
            # A `'` or `"` in a regex character class (`/['"]/`) opens a quote that
            # never closes, and an unstripped tail then reads commented-out wiring as
            # real. Only a template literal may span lines, so a newline closes the rest.
            if ch == quote or (ch == "\n" and quote != "`"):
                quote = ""
            out.append(ch)
        elif ch == "\\":  # an escaped `/` must not start a comment
            out.append(text[i : i + 2])
            i += 2
            continue
        elif ch in "\"'`":
            quote = ch
            out.append(ch)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            chunk = text[i:] if end == -1 else text[i : end + 2]
            out.append("\n" * chunk.count("\n"))
            i += len(chunk)
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _yaml_flow_value(text: str, start: int) -> str:
    """The flow scalar or bracketed collection at `start`, ending at the first
    top-level `,`/`}`/`]` or end of line. Quote- and depth-aware so a `,` inside
    `['5432:5432', ...]` or a `:` inside `${PORT:-5432}` doesn't terminate it."""
    depth, quote = 0, ""
    i = start
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and ch in ",\n":
            break
        i += 1
    return text[start:i]


def _yaml_key_regions(text: str, key: str, *, indent: int | None = None) -> list[str]:
    """The value text of every `<key>:` mapping entry: the flow value on the same
    line, or the indented block beneath it. Anchoring a regex on block layout is
    what silently missed every flow-style spelling, so the key is matched both at
    line start and inside a flow mapping (`db: { ports: [...] }`). Pass `indent` to
    accept only keys at that column — spring's `server:` must not match
    `management:`'s nested one. Comments are stripped here so no caller can forget."""
    text = _strip_hash_comments(text)
    pattern = re.compile(
        rf"(?:^|(?<=[{{,]))([ \t]*)(?:-[ \t]+)?{re.escape(key)}[ \t]*:", re.MULTILINE
    )
    regions: list[str] = []
    for m in pattern.finditer(text):
        at_line_start = m.start() == text.rfind("\n", 0, m.start()) + 1
        if indent is not None and not (at_line_start and len(m.group(1)) == indent):
            continue
        inline = _yaml_flow_value(text, m.end()).strip()
        if inline:
            regions.append(inline)
            continue
        if not at_line_start:
            continue
        key_indent = len(m.group(1))
        block: list[str] = []
        for line in text[m.end() :].splitlines()[1:]:
            stripped = line.lstrip()
            if stripped:
                line_indent = len(line) - len(stripped)
                # A block sequence may sit at its key's own indent, which is both
                # legal and common (`ports:` then `- "5432:5432"` at the same column).
                if line_indent < key_indent:
                    break
                if line_indent == key_indent and not stripped.startswith("-"):
                    break
            block.append(line)
        regions.append("\n".join(block))
    return regions


def _rn_hook_detect(cwd: Path) -> tuple[str, str]:
    readiness = post_checkout_readiness(cwd)
    return ("ok", readiness.detail) if readiness.ready else ("problem", readiness.detail)


def _rn_hook_manual(cwd: Path) -> str:
    return post_checkout_manual_instructions(cwd)


def _autofix_ensure_post_checkout_hook(cwd: Path) -> None:
    _ensure_post_checkout_hook(cwd)


_RN_WIRING_CHECKS.append(
    WiringCheck(
        id="hook",
        description="post-checkout forwards Git events to Splashdown",
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
    text = _strip_js_comments((cwd / "metro.config.js").read_text())
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
    text = _strip_hash_comments((cwd / "ios" / ".xcode.env").read_text())
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


_HOOK_WIRING_CHECK = WiringCheck(
    id="hook",
    description="post-checkout forwards Git events to Splashdown",
    applies=lambda cwd: True,
    detect=_rn_hook_detect,
    autofix=_autofix_ensure_post_checkout_hook,
    manual_instructions=_rn_hook_manual,
)
