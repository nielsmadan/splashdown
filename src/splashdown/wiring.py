from __future__ import annotations

import json
import os
import posixpath
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

from .constants import split_lines
from .hooks import (
    _ensure_post_checkout_hook,
    _nested_project,
    post_checkout_files,
    post_checkout_manual_instructions,
    post_checkout_readiness,
)
from .jsontext import _object_span, _set_json_member
from .safe_files import atomic_write_text, read_editable_text
from .yamltext import _strip_hash_comments

# The wiring-check registries (_RN_WIRING_CHECKS, _HOOK_WIRING_CHECK) are the
# per-framework spec shipped with the tool. Each WiringCheck names
# a small fact about the project (e.g. "metro.config.js consumes RCT_METRO_PORT")
# that splashdown can inspect and, where safely mechanical, repair.


def _no_wiring_files(cwd: Path) -> tuple[Path, ...]:
    return ()


def wiring_destination(check_dir: Path, project_dir: Path, env_file: str) -> str:
    """`env_file`, which the recipe states relative to the project, spelled relative
    to the directory a check inspects."""
    return os.path.relpath(project_dir / env_file, check_dir).replace(os.sep, "/")


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
    # True when the fix activates the local checkout instead of writing
    # project-owned configuration, so `splash init` skips it.
    activation: bool = False
    # The project files this check's autofix may rewrite, so a caller can report
    # what changed without parsing each fix's own message.
    files: Callable[[Path], tuple[Path, ...]] = _no_wiring_files


def run_wiring_detect(check: WiringCheck, cwd: Path) -> tuple[str, str]:
    """A detect that raises has not parsed the project, so it reports a problem
    rather than aborting the command that asked. A check reports `"ok"`, `"warning"`
    for a fact that holds but is not yet in force, or `"problem"`."""
    try:
        return check.detect(cwd)
    except Exception as error:  # noqa: BLE001
        return ("problem", f"check could not run: {error}")


# The destination-independent RN checks accumulate as helpers are defined;
# `rn_wiring_checks` adds the ones bound to the environment output destination.
_RN_WIRING_CHECKS: list[WiringCheck] = []


# Lexical checks strip comments first so commented-out config cannot produce a false “wired” result.


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


def _rn_hook_detect(cwd: Path) -> tuple[str, str]:
    """Correct project configuration still delivers no event until the manager's own
    hook is installed in this checkout, so that state is a warning, not a tick."""
    readiness = post_checkout_readiness(cwd)
    if not readiness.ready:
        return ("problem", readiness.detail)
    return ("ok", readiness.detail) if readiness.active else ("warning", readiness.detail)


def _rn_hook_manual(cwd: Path) -> str:
    return post_checkout_manual_instructions(cwd)


def _autofix_ensure_post_checkout_hook(cwd: Path) -> None:
    _ensure_post_checkout_hook(cwd)


def _rn_hook_applies(cwd: Path) -> bool:
    return not _nested_project(cwd)


def _rn_hook_files(cwd: Path) -> tuple[Path, ...]:
    return post_checkout_files(cwd)


_HOOK_WIRING_CHECK = WiringCheck(
    id="hook",
    description="post-checkout forwards Git events to Splashdown",
    applies=_rn_hook_applies,
    detect=_rn_hook_detect,
    autofix=_autofix_ensure_post_checkout_hook,
    manual_instructions=_rn_hook_manual,
    activation=True,
    files=_rn_hook_files,
)

_RN_WIRING_CHECKS.append(_HOOK_WIRING_CHECK)


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


def _rn_metro_files(cwd: Path) -> tuple[Path, ...]:
    return (cwd / "metro.config.js",)


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
    text = read_editable_text(path, root=cwd)
    if "process.env.RCT_METRO_PORT" in text:
        return
    m = _METRO_LITERAL_PORT_RE.search(text)
    if m:
        new_text = (
            text[: m.start()]
            + f"port: Number(process.env.RCT_METRO_PORT) || {m.group(1)}"
            + text[m.end() :]
        )
        atomic_write_text(path, new_text, root=cwd)
        print(f"patched metro.config.js (RCT_METRO_PORT, fallback {m.group(1)})", file=sys.stderr)
        return
    injected = _rn_metro_inject(text)
    if injected is None:
        return  # unrecognized shape — doctor will surface manual_instructions
    atomic_write_text(path, injected, root=cwd)
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
        files=_rn_metro_files,
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


def _rn_pkg_files(cwd: Path) -> tuple[Path, ...]:
    return (cwd / "package.json",)


def _pkg_scripts_with_port(data: dict[str, Any]) -> list[str]:
    """Return names of scripts that override RCT_METRO_PORT with --port."""
    scripts = data.get("scripts") or {}
    hits: list[str] = []
    for name, value in scripts.items():
        if not isinstance(value, str):
            continue
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
    """Strip `--port` from the RN scripts that hardcode it, splicing each script's
    own bytes. A shape this editor cannot place exactly is left for the manual
    instructions rather than reflowed into splashdown's own formatting."""
    import sys  # noqa: PLC0415

    path = cwd / "package.json"
    text = read_editable_text(path, root=cwd)
    data = json.loads(text)
    scripts = data.get("scripts") or {}
    stripped = {
        name: _PKG_PORT_RE.sub("", scripts[name])
        for name in _pkg_scripts_with_port(data)
        if _PKG_PORT_RE.sub("", scripts[name]) != scripts[name]
    }
    if not stripped:
        return
    updated = text
    for name, value in stripped.items():
        span = _object_span(updated, scripts, "scripts")
        if span is None:
            return
        updated = _set_json_member(updated, span, name, json.dumps(value, ensure_ascii=False))
        scripts[name] = value
    data["scripts"] = scripts
    if json.loads(updated) != data:
        return
    atomic_write_text(path, updated, root=cwd)
    print("updated package.json (stripped --port from scripts)", file=sys.stderr)


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
        files=_rn_pkg_files,
    ),
)


# Sentinel-wrapped block written into ios/.xcode.env. Sentinels make autofix
# idempotent (find by sentinel pair, replace contents) and let the user identify
# what's tool-managed vs hand-edited.
_XCODE_BEGIN = "# >>> splashdown-managed RCT_METRO_PORT >>>"
_XCODE_END = "# <<< splashdown-managed RCT_METRO_PORT <<<"


def _xcode_block(env_file: str) -> str:
    return f"""{_XCODE_BEGIN}
# splashdown ships this block. RCT_METRO_PORT is baked into the iOS binary via
# GCC_PREPROCESSOR_DEFINITIONS (RCTBundleURLProvider's defaultPort), so the app
# must be rebuilt after a port change. Honour a value set by `react-native
# run-ios`; else read this checkout's {env_file}; else fall back to 8083.
if [ -z "${{RCT_METRO_PORT:-}}" ] && [ -f "${{SRCROOT}}/../{env_file}" ]; then
  export RCT_METRO_PORT="$(grep '^RCT_METRO_PORT=' "${{SRCROOT}}/../{env_file}" | cut -d= -f2)"
fi
export RCT_METRO_PORT="${{RCT_METRO_PORT:-8083}}"
{_XCODE_END}
"""


# A block whose closing sentinel the user deleted still ends at the first blank
# line: splashdown writes the block as one unbroken paragraph.
_XCODE_BLOCK_RE = re.compile(
    re.escape(_XCODE_BEGIN)
    + r"(?:.*?"
    + re.escape(_XCODE_END)
    + r"|[^\n]*(?:\n[^\n\S]*\S[^\n]*)*)"
    + r"\n?",
    re.DOTALL,
)
# A *static literal* export — `export RCT_METRO_PORT=8083`, no variable
# references. The intentionally narrow match keeps autofix from mangling
# user-written conditional / shell-substitution-based wirings.
_XCODE_LITERAL_EXPORT_RE = re.compile(
    r"^[ \t]*export[ \t]+RCT_METRO_PORT[ \t]*=[ \t]*\d+[ \t]*\n?",
    re.MULTILINE,
)
# Every dotenv path the file names, however it is spelled.
_XCODE_ENV_TOKEN_RE = re.compile(r"[\w$./{}~-]*\.env[\w.-]*")
# A statement that puts a value into RCT_METRO_PORT. Only such a statement ties a
# dotenv path to the Metro port; every other mention wires nothing.
_XCODE_PORT_ASSIGN_RE = re.compile(r"(?:export[ \t]+)?RCT_METRO_PORT[ \t]*=")
# Xcode build variables that all resolve to the directory holding the project,
# which for a React Native app is `ios/` — the directory `.xcode.env` sits in.
_XCODE_SRCROOT_VARS = (
    "${SRCROOT}",
    "$SRCROOT",
    "${PROJECT_DIR}",
    "$PROJECT_DIR",
    "${SOURCE_ROOT}",
    "$SOURCE_ROOT",
)


def _rn_xcode_applies(cwd: Path) -> bool:
    return (cwd / "ios" / ".xcode.env").exists()


def _rn_xcode_files(cwd: Path) -> tuple[Path, ...]:
    return (cwd / "ios" / ".xcode.env",)


def _xcode_ref_paths(token: str) -> set[str]:
    """Every app-relative path a dotenv reference in `ios/.xcode.env` may name.
    A reference through an unrecognized shell variable names nothing this can
    compare, so it resolves to no path and the wiring reads as unparsed."""
    for var in _XCODE_SRCROOT_VARS:
        if token.startswith(f"{var}/"):
            return {posixpath.normpath(posixpath.join("ios", token[len(var) + 1 :]))}
    if token.startswith("$"):
        return set()
    return {posixpath.normpath(token), posixpath.normpath(posixpath.join("ios", token))}


def _split_unquoted(line: str, sep: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quote = ""
    for ch in line:
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == sep:
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    parts.append("".join(current))
    return parts


def _xcode_statements(text: str) -> list[tuple[str, tuple[str, ...]]]:
    """Each shell statement in `ios/.xcode.env` with the conditions guarding it."""
    statements: list[tuple[str, tuple[str, ...]]] = []
    guards: list[str] = []
    for line in split_lines(text):
        for segment in _split_unquoted(line, ";"):
            word = segment.strip()
            while word and word.split()[0] in {"then", "else", "do"}:
                word = word.split(None, 1)[1] if " " in word else ""
            if not word:
                continue
            head = word.split()[0]
            if head in {"if", "elif"}:
                condition = word.split(None, 1)[1] if " " in word else ""
                if head == "elif" and guards:
                    guards[-1] = condition
                else:
                    guards.append(condition)
                continue
            if head in {"fi", "done"}:
                if guards:
                    guards.pop()
                continue
            statements.append((word, tuple(guards)))
    return statements


def _xcode_guard_applies(condition: str) -> bool:
    """A guard that names neither RCT_METRO_PORT nor a dotenv path is testing
    something else — a build configuration, say — so a Debug build may never
    reach what it wraps."""
    return "RCT_METRO_PORT" in condition or bool(_XCODE_ENV_TOKEN_RE.search(condition))


def _xcode_port_refs(text: str) -> list[str]:
    """Dotenv paths named by a statement that assigns RCT_METRO_PORT, in file
    order. A path named for any other purpose wires nothing, so it is not a
    reference at all."""
    found: list[str] = []
    for statement, guards in _xcode_statements(text):
        if not _XCODE_PORT_ASSIGN_RE.match(statement):
            continue
        if not all(_xcode_guard_applies(guard) for guard in guards):
            continue
        for token in _XCODE_ENV_TOKEN_RE.findall(" ".join((*guards, statement))):
            if token not in found:
                found.append(token)
    return found


def _xcode_reads(text: str, env_file: str) -> bool:
    wanted = posixpath.normpath(env_file)
    return any(wanted in _xcode_ref_paths(token) for token in _xcode_port_refs(text))


def _xcode_foreign_refs(raw: str) -> list[str]:
    """Dotenv paths the RCT_METRO_PORT wiring reads outside splashdown's own block,
    in file order. The block is removed before comments are stripped: its
    sentinels are comment lines."""
    return _xcode_port_refs(_strip_hash_comments(_XCODE_BLOCK_RE.sub("", raw)))


def _xcode_status(raw: str, env_file: str) -> tuple[str, str]:
    if _xcode_reads(_strip_hash_comments(raw), env_file):
        return ("ok", f"ios/.xcode.env reads RCT_METRO_PORT from {env_file}")
    foreign = _xcode_foreign_refs(raw)
    if foreign:
        return (
            "problem",
            f"ios/.xcode.env reads {', '.join(foreign)}, not the configured {env_file}",
        )
    if _XCODE_LITERAL_EXPORT_RE.search(raw):
        return ("problem", "ios/.xcode.env statically exports a literal RCT_METRO_PORT")
    return ("problem", f"ios/.xcode.env doesn't wire RCT_METRO_PORT to {env_file}")


def _rn_xcode_detect(cwd: Path, env_file: str) -> tuple[str, str]:
    return _xcode_status((cwd / "ios" / ".xcode.env").read_text(), env_file)


def _rn_xcode_autofix(cwd: Path, env_file: str) -> None:
    import sys  # noqa: PLC0415

    path = cwd / "ios" / ".xcode.env"
    text = read_editable_text(path, root=cwd)
    if _xcode_reads(_strip_hash_comments(text), env_file) or _xcode_foreign_refs(text):
        return
    text = _XCODE_LITERAL_EXPORT_RE.sub("", text)
    text = _XCODE_BLOCK_RE.sub("", text)
    text = text.rstrip() + ("\n\n" if text.strip() else "")
    text += _xcode_block(env_file)
    atomic_write_text(path, text, root=cwd)
    print(f"rewrote ios/.xcode.env (RCT_METRO_PORT read from {env_file})", file=sys.stderr)


def _rn_xcode_manual(cwd: Path, env_file: str) -> str:
    return (
        f"Edit ios/.xcode.env so RCT_METRO_PORT is honoured-if-set, else read from\n"
        f"{env_file}, else fall back to 8083:\n"
        f'    if [ -z "${{RCT_METRO_PORT:-}}" ] && [ -f "${{SRCROOT}}/../{env_file}" ]; then\n'
        f"      export RCT_METRO_PORT=\"$(grep '^RCT_METRO_PORT=' "
        f'"${{SRCROOT}}/../{env_file}" | cut -d= -f2)"\n'
        f"    fi\n"
        f'    export RCT_METRO_PORT="${{RCT_METRO_PORT:-8083}}"'
    )


def _rn_xcode_check(env_file: str) -> WiringCheck:
    return WiringCheck(
        id="rn-xcode-env",
        description=f"ios/.xcode.env wires RCT_METRO_PORT to {env_file}",
        applies=_rn_xcode_applies,
        detect=lambda cwd: _rn_xcode_detect(cwd, env_file),
        autofix=lambda cwd: _rn_xcode_autofix(cwd, env_file),
        manual_instructions=lambda cwd: _rn_xcode_manual(cwd, env_file),
        files=_rn_xcode_files,
    )


def rn_wiring_checks(env_file: str) -> list[WiringCheck]:
    """The React Native checks, with the destination-dependent ones bound to the
    environment output this checkout writes."""
    return [*_RN_WIRING_CHECKS, _rn_xcode_check(env_file)]
