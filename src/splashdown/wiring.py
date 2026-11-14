from __future__ import annotations

import json
import os
import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NamedTuple

from .constants import split_lines
from .hooks import (
    _ensure_post_checkout_hook,
    _nested_project,
    post_checkout_files,
    post_checkout_manual_instructions,
    post_checkout_readiness,
)
from .jsontext import _member_span, _object_span, _set_json_member
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


# The destination-independent RN checks accumulate as helpers are defined;
# `rn_wiring_checks` adds the ones bound to the environment output destination.
_RN_WIRING_CHECKS: list[WiringCheck] = []


# Lexical checks strip comments first so commented-out config cannot produce a false “wired” result.


def _strip_js_comments(text: str, *, preserve_offsets: bool = False) -> str:
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
                if preserve_offsets:
                    out.append(" ")
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            chunk = text[i:] if end == -1 else text[i : end + 2]
            out.append(
                re.sub(r"[^\r\n]", " ", chunk) if preserve_offsets else "\n" * chunk.count("\n")
            )
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
_METRO_CONFIG_OBJ_RE = re.compile(r"(?:const\s+config\s*=|module\.exports\s*=)\s*\{")


def _rn_metro_applies(cwd: Path) -> bool:
    return (cwd / "metro.config.js").exists()


def _rn_metro_files(cwd: Path) -> tuple[Path, ...]:
    return (cwd / "metro.config.js",)


def _rn_metro_detect(cwd: Path, port_var: str = "RCT_METRO_PORT") -> tuple[str, str]:
    text = read_editable_text(cwd / "metro.config.js", root=cwd)
    try:
        span = _server_port_span(text, metro=True)
        if span is not None:
            reads = _port_reads(_js_mask_comments(text), span, frozenset({port_var}))
            if reads:
                if reads[0].source != "process.env":
                    raise ValueError("unsupported server.port environment source")
                return "ok", f"metro.config.js server.port reads process.env.{port_var}"
        return (
            "problem",
            f"metro.config.js server.port does not consume {port_var}; recognized literal or missing port is autofixable",
        )
    except ValueError as error:
        return "incomplete", str(error)


def _rn_metro_inject(text: str, port_var: str = "RCT_METRO_PORT") -> str | None:
    code = _js_mask_comments(text)
    try:
        span = _server_port_span(code, metro=True)
        if span is not None:
            return None
        roots = list(_METRO_CONFIG_OBJ_RE.finditer(code))
        if len(roots) != 1:
            return None
        server = _js_member(code, roots[0].end() - 1, "server")
    except ValueError:
        return None
    line = f"port: Number(process.env.{port_var}) || 8081,"
    if server is not None:
        at = server[0] + 1
        return text[:at] + f"\n    {line}" + text[at:]
    at = roots[0].end()
    return text[:at] + f"\n  server: {{\n    {line}\n  }}," + text[at:]


def _rn_metro_autofix(cwd: Path, port_var: str = "RCT_METRO_PORT") -> None:
    path = cwd / "metro.config.js"
    text = read_editable_text(path, root=cwd)
    try:
        span = _server_port_span(text, metro=True)
    except ValueError:
        return
    if span is not None:
        start, end = span
        value = text[start:end]
        if re.fullmatch(r"\d+", value.strip()) is None:
            return
        new_text: str | None = (
            text[:start] + f"Number(process.env.{port_var}) || {value}" + text[end:]
        )
    else:
        new_text = _rn_metro_inject(text, port_var)
    if new_text is not None and new_text != text:
        atomic_write_text(path, new_text, root=cwd)


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
def _rn_pkg_applies(cwd: Path) -> bool:
    return (cwd / "package.json").exists()


def _rn_pkg_files(cwd: Path) -> tuple[Path, ...]:
    return (cwd / "package.json",)


def _rn_command_segments(value: str) -> list[tuple[int, int]]:
    segments = _shell_segments(value)
    if not segments and re.search(r"\breact-native\s+(?:start|run-ios|run-android)\b", value):
        raise ValueError(
            "unsupported React Native shell script; verify the managed --port manually"
        )
    return [
        (start + match.end(), end)
        for start, end in segments
        if (
            match := re.match(
                r"\s*(?:npx\s+)?react-native\s+(?:start|run-ios|run-android)\b", value[start:end]
            )
        )
    ]


def _shell_segments(value: str) -> list[tuple[int, int]]:
    if "$(" in value or "`" in value or "\n" in value:
        return []
    quote = ""
    escaped = False
    start = 0
    segments = []
    for index, char in enumerate(value):
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char in ";&|":
            segments.append((start, index))
            start = index + 1
    return [] if quote or escaped else [*segments, (start, len(value))]


def _pkg_scripts_with_port(data: dict[str, Any]) -> list[str]:
    scripts = data.get("scripts") or {}
    return [
        name
        for name, value in scripts.items()
        if isinstance(value, str)
        and any(_shell_literal_port(value[start:end]) for start, end in _rn_command_segments(value))
    ]


def _shell_port_span(flags: str) -> tuple[int, int] | None:
    if not _shell_segments(flags):
        raise ValueError("unsupported shell arguments")
    words = list(re.finditer(r"""(?:[^\s"'\\]+|"(?:\\.|[^"\\])*"|'[^']*'|\\.)+""", flags))
    ports = []
    for index, word in enumerate(words):
        argument = word.group()
        if "\\" in argument or (argument[:1] in {'"', "'"} and argument[1:].startswith("--port")):
            raise ValueError("unsupported quoted or escaped shell flag")
        if argument == "--":
            break
        if argument.startswith("--port="):
            ports.append((word.start() + len("--port="), word.end()))
        elif argument == "--port":
            if index + 1 == len(words) or words[index + 1].group().startswith("--"):
                raise ValueError("--port has no value")
            ports.append((words[index + 1].start(), words[index + 1].end()))
    if len(ports) > 1:
        raise ValueError("multiple --port flags")
    return ports[0] if ports else None


def _shell_port_wired(flags: str, port_var: str, *, native: bool = False) -> bool:
    try:
        span = _shell_port_span(flags)
    except ValueError:
        return False
    if span is None:
        return native
    value = flags[span[0] : span[1]]
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return (
        re.fullmatch(
            r"\$(?:" + re.escape(port_var) + r"|\{" + re.escape(port_var) + r"(?::-(?:\d+))?\})",
            value,
        )
        is not None
    )


def _shell_literal_port(flags: str) -> bool:
    try:
        span = _shell_port_span(flags)
    except ValueError:
        return False
    return span is not None and flags[span[0] : span[1]].isdigit()


def _rn_wire_script(value: str) -> str:
    updated = value
    for start, end in reversed(_rn_command_segments(value)):
        segment = value[start:end]
        if not _shell_literal_port(segment):
            continue
        span = _shell_port_span(segment)
        if span is None:
            continue
        left, right = span
        segment = (
            segment[:left] + "${RCT_METRO_PORT:-" + segment[left:right] + "}" + segment[right:]
        )
        updated = updated[:start] + segment + updated[end:]
    return updated


def _rn_pkg_detect(cwd: Path) -> tuple[str, str]:
    try:
        data = json.loads((cwd / "package.json").read_text())
    except (json.JSONDecodeError, OSError) as e:
        return ("incomplete", f"could not read package.json: {e}")
    try:
        hits = _pkg_scripts_with_port(data)
        if hits:
            return ("problem", f"--port hardcoded in scripts: {', '.join(hits)}")
        scripts = data.get("scripts") or {}
        if any(
            not _shell_port_wired(value[start:end], "RCT_METRO_PORT", native=True)
            for value in scripts.values()
            if isinstance(value, str)
            for start, end in _rn_command_segments(value)
        ):
            return "problem", "React Native --port override does not consume RCT_METRO_PORT"
        return ("ok", "package.json scripts don't hardcode --port")
    except ValueError as error:
        return "incomplete", str(error)


def _rn_pkg_autofix(cwd: Path) -> None:
    """Replace literal RN port flags with managed-value fallbacks."""
    import sys  # noqa: PLC0415

    path = cwd / "package.json"
    text = read_editable_text(path, root=cwd)
    data = json.loads(text)
    scripts = data.get("scripts") or {}
    stripped = {
        name: _rn_wire_script(scripts[name])
        for name in _pkg_scripts_with_port(data)
        if _rn_wire_script(scripts[name]) != scripts[name]
    }
    if not stripped:
        return
    updated = text
    for name, value in stripped.items():
        root = _object_span(updated, data, None)
        member = _member_span(updated, root, "scripts") if root is not None else None
        if member is None:
            return
        updated = _set_json_member(
            updated, (member[1], member[2] - 1), name, json.dumps(value, ensure_ascii=False)
        )
        scripts[name] = value
    data["scripts"] = scripts
    if json.loads(updated) != data:
        return
    atomic_write_text(path, updated, root=cwd)
    print("updated package.json (managed port with existing fallback)", file=sys.stderr)


def _rn_pkg_manual(cwd: Path) -> str:
    return "Pass --port ${RCT_METRO_PORT:-8081} to the React Native command, preserving your existing fallback."


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


def _xcode_destination_supported(env_file: str) -> bool:
    return all(char.isalnum() or char in " ./_-" for char in env_file)


def _xcode_block(env_file: str, fallback: str = "8083") -> str:
    if not _xcode_destination_supported(env_file):
        raise ValueError("unsupported Xcode environment destination")
    return f"""{_XCODE_BEGIN}
# splashdown ships this block. RCT_METRO_PORT is baked into the iOS binary via
# GCC_PREPROCESSOR_DEFINITIONS (RCTBundleURLProvider's defaultPort), so the app
# must be rebuilt after a port change. Honour a value set by `react-native
# run-ios`; else read this checkout's {env_file}; else fall back to {fallback}.
if [ -z "${{RCT_METRO_PORT:-}}" ] && [ -f "${{SRCROOT}}/../{env_file}" ]; then
  export RCT_METRO_PORT="$(grep '^RCT_METRO_PORT=' "${{SRCROOT}}/../{env_file}" | cut -d= -f2)"
fi
export RCT_METRO_PORT="${{RCT_METRO_PORT:-{fallback}}}"
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
    r"^[ \t]*export[ \t]+RCT_METRO_PORT[ \t]*=[ \t]*(\d+)[ \t]*\n?",
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


def _xcode_configured_refs(text: str, env_file: str) -> list[str]:
    wanted = posixpath.normpath(env_file)
    relative = posixpath.relpath(wanted, "ios")
    paths = {wanted, relative, *(f"{var}/{relative}" for var in _XCODE_SRCROOT_VARS)}
    pattern = (
        r"(?P<quote>[\"'])(?P<path>"
        + "|".join(re.escape(path) for path in sorted(paths))
        + r")(?P=quote)"
    )
    return [match.group("path") for match in re.finditer(pattern, text)] + [
        token for token in re.findall(r"[\w$./{}~-]+", text) if wanted in _xcode_ref_paths(token)
    ]


def _xcode_guard_applies(condition: str, env_file: str | None = None) -> bool:
    """A guard that names neither RCT_METRO_PORT nor a dotenv path is testing
    something else — a build configuration, say — so a Debug build may never
    reach what it wraps."""
    return (
        "RCT_METRO_PORT" in condition
        or bool(_XCODE_ENV_TOKEN_RE.search(condition))
        or (env_file is not None and bool(_xcode_configured_refs(condition, env_file)))
    )


def _xcode_port_refs(text: str, env_file: str | None = None) -> list[str]:
    """Dotenv paths named by a statement that assigns RCT_METRO_PORT, in file
    order. A path named for any other purpose wires nothing, so it is not a
    reference at all."""
    found: list[str] = []
    for statement, guards in _xcode_statements(text):
        if not _XCODE_PORT_ASSIGN_RE.match(statement):
            continue
        if not all(_xcode_guard_applies(guard, env_file) for guard in guards):
            continue
        context = " ".join((*guards, statement))
        tokens = _XCODE_ENV_TOKEN_RE.findall(context)
        if env_file is not None:
            tokens += _xcode_configured_refs(context, env_file)
        for token in tokens:
            if token not in found:
                found.append(token)
    return found


def _xcode_reads(text: str, env_file: str) -> bool:
    wanted = posixpath.normpath(env_file)
    return any(wanted in _xcode_ref_paths(token) for token in _xcode_port_refs(text, env_file))


def _xcode_foreign_refs(raw: str) -> list[str]:
    """Dotenv paths the RCT_METRO_PORT wiring reads outside splashdown's own block,
    in file order. The block is removed before comments are stripped: its
    sentinels are comment lines."""
    return _xcode_port_refs(_strip_hash_comments(_XCODE_BLOCK_RE.sub("", raw)))


def _xcode_status(raw: str, env_file: str) -> tuple[str, str]:
    if not _xcode_destination_supported(env_file):
        return "incomplete", "Xcode environment destination contains unsupported shell characters"
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

    if not _xcode_destination_supported(env_file):
        return
    path = cwd / "ios" / ".xcode.env"
    text = read_editable_text(path, root=cwd)
    outside = _XCODE_BLOCK_RE.sub("", text)
    unknown_assignments = _XCODE_PORT_ASSIGN_RE.search(
        _strip_hash_comments(_XCODE_LITERAL_EXPORT_RE.sub("", outside))
    )
    if (
        _xcode_reads(_strip_hash_comments(text), env_file)
        or _xcode_foreign_refs(text)
        or unknown_assignments
    ):
        return
    literal = _XCODE_LITERAL_EXPORT_RE.search(text)
    old_block = _XCODE_BLOCK_RE.search(text)
    block = _xcode_block(env_file, literal.group(1) if literal else "8083")
    if old_block:
        text = text[: old_block.start()] + block + text[old_block.end() :]
    elif literal:
        text = text[: literal.start()] + block + text[literal.end() :]
    else:
        text += ("" if not text or text.endswith("\n") else "\n") + block
    atomic_write_text(path, text, root=cwd)
    print(f"rewrote ios/.xcode.env (RCT_METRO_PORT read from {env_file})", file=sys.stderr)


def _rn_xcode_manual(cwd: Path, env_file: str) -> str:
    if not _xcode_destination_supported(env_file):
        return "Choose an envfile destination containing only letters, digits, spaces, dots, slashes, underscores or hyphens, or wire RCT_METRO_PORT manually with literal shell quoting."
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
        autofix=(lambda cwd: _rn_xcode_autofix(cwd, env_file))
        if _xcode_destination_supported(env_file)
        else None,
        manual_instructions=lambda cwd: _rn_xcode_manual(cwd, env_file),
        files=_rn_xcode_files,
    )


def rn_wiring_checks(env_file: str | None, port_var: str = "RCT_METRO_PORT") -> list[WiringCheck]:
    metro = _RN_WIRING_CHECKS[1]._replace(
        description=f"metro.config.js server.port consumes {port_var}",
        detect=lambda cwd: _rn_metro_detect(cwd, port_var),
        autofix=lambda cwd: _rn_metro_autofix(cwd, port_var),
        manual_instructions=lambda cwd: _rn_metro_manual(cwd).replace("RCT_METRO_PORT", port_var),
    )
    package = (
        _RN_WIRING_CHECKS[2] if port_var == "RCT_METRO_PORT" else _rn_custom_package_check(port_var)
    )
    xcode = (
        _rn_xcode_check(env_file)
        if env_file is not None and port_var == "RCT_METRO_PORT"
        else _rn_custom_xcode_check(port_var)
    )
    return [_HOOK_WIRING_CHECK, metro, package, xcode]


def _rn_custom_package_check(port_var: str) -> WiringCheck:
    def detect(cwd: Path) -> tuple[str, str]:
        data = json.loads(read_editable_text(cwd / "package.json", root=cwd))
        scripts = data.get("scripts", {})
        try:
            relevant = [
                value[start:end]
                for value in scripts.values()
                if isinstance(value, str)
                for start, end in _rn_command_segments(value)
            ]
        except ValueError as error:
            return "incomplete", str(error)
        if relevant and all(_shell_port_wired(value, port_var) for value in relevant):
            return "ok", f"React Native launch scripts pass {port_var}"
        return "problem", f"React Native launch scripts must pass --port ${port_var}"

    return WiringCheck(
        "rn-pkg-port",
        f"React Native CLI consumes {port_var}",
        _rn_pkg_applies,
        detect,
        None,
        lambda _: (
            f"Pass --port ${port_var} to each React Native launch command, or export RCT_METRO_PORT from {port_var}."
        ),
    )


def _rn_custom_xcode_check(port_var: str) -> WiringCheck:
    def detect(cwd: Path) -> tuple[str, str]:
        text = _strip_hash_comments(read_editable_text(cwd / "ios" / ".xcode.env", root=cwd))
        pattern = (
            r"^export\s+RCT_METRO_PORT=[\"']?\$\{?" + re.escape(port_var) + r"(?:\}|(?=[\"'\s]|$))"
        )
        if re.search(pattern, text, re.MULTILINE):
            return "ok", f"ios/.xcode.env maps inherited {port_var} to RCT_METRO_PORT"
        return (
            "incomplete",
            f"Verify ios/.xcode.env receives {port_var}; no dotenv source established",
        )

    return WiringCheck(
        "rn-xcode-env",
        f"Xcode consumes {port_var}",
        _rn_xcode_applies,
        detect,
        None,
        lambda _: (
            f'Arrange for {port_var} in the Xcode build environment and export RCT_METRO_PORT="${{{port_var}}}". Shell envrc and non-file writers are not dotenv sources.'
        ),
    )


def _js_mask_comments(text: str) -> str:
    return _strip_js_comments(text, preserve_offsets=True)


def _js_mask_literals(text: str) -> str:
    return re.sub(
        r"\"(?:\\.|[^\"\\\r\n])*\"|'(?:\\.|[^'\\\r\n])*'|`(?:\\.|[^`\\])*`",
        lambda match: " " * len(match.group()),
        text,
    )


def _js_value_end(text: str, start: int) -> int:
    stack: list[str] = []
    quote = ""
    index = start
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = ""
        elif char in "\"'`":
            quote = char
        elif char in "([{":
            stack.append({"(": ")", "[": "]", "{": "}"}[char])
        elif char in ")]}" and stack:
            if stack.pop() != char:
                raise ValueError("unbalanced config expression")
        elif not stack and char in ",;\n}":
            return index
        index += 1
    if stack or quote:
        raise ValueError("unbalanced config expression")
    return index


def _js_member(text: str, start: int, key: str) -> tuple[int, int] | None:
    index = start + 1
    found = None
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index == len(text) or text[index] == "}":
            return found
        match = re.match(r"""(?:([A-Za-z_$][\w$]*)|["']([\w$]+)["'])\s*:\s*""", text[index:])
        if match is None:
            raise ValueError("unsupported config object member")
        value_start = index + match.end()
        value_end = _js_value_end(text, value_start)
        if (match.group(1) or match.group(2)) == key:
            if found is not None:
                raise ValueError(f"duplicate {key} member")
            found = (value_start, value_end)
        index = value_end
        while index < len(text) and text[index].isspace():
            index += 1
        if index < len(text) and text[index] == ",":
            index += 1
        elif index == len(text) or text[index] == "}":
            return found
        else:
            raise ValueError("unsupported config object separator")
    raise ValueError("unterminated config object")


def _server_port_span(text: str, *, metro: bool = False) -> tuple[int, int] | None:
    code = _js_mask_comments(text)
    pattern = (
        r"(?:module\.exports\s*=\s*|const\s+config\s*=\s*)\{"
        if metro
        else r"(?:export\s+default\s+(?:defineConfig\s*\(\s*)?|\breturn\s+)\{"
    )
    roots = list(re.finditer(pattern, _js_mask_literals(code)))
    if len(roots) != 1:
        raise ValueError("cannot establish the exported config object")
    if (
        metro
        and code[roots[0].start() :].startswith("const")
        and not re.search(
            r"module\.exports\s*=\s*(?:config\s*;|mergeConfig\(\s*\w+\s*,\s*config\s*\))", code
        )
    ):
        raise ValueError("cannot establish that config reaches module.exports")
    if (
        not metro
        and code[roots[0].start() :].startswith("return")
        and not re.search(r"export\s+default\s+defineConfig\s*\(", code[: roots[0].start()])
    ):
        raise ValueError("cannot establish that returned config reaches the export")
    if not metro and code[roots[0].start() :].startswith("return"):
        prefix = _js_mask_literals(code[: roots[0].start()])
        exported = prefix[prefix.rfind("export") :]
        if (
            exported.count("=>") != 1
            or exported.count("{") - exported.count("}") != 1
            or re.search(r"\b(?:function|if|switch|for|while)\b", exported)
            or len(re.findall(r"\breturn\b", _js_mask_literals(code))) != 1
        ):
            raise ValueError("nested or conditional config flow requires manual verification")
    if metro:
        end = _js_value_end(code, roots[0].end() - 1)
        tail = _js_mask_literals(code[end:])
        if code[roots[0].start() :].startswith("const"):
            tail = re.sub(
                r"module\.exports\s*=\s*(?:config\s*;|mergeConfig\(\s*\w+\s*,\s*config\s*\)\s*;?)",
                "",
                tail,
                count=1,
            )
        if re.search(r"\bconfig\b|\bmodule\s*(?:\.|\[)", tail):
            raise ValueError("later Metro config use requires manual verification of mutations")
    server = _js_member(code, roots[0].end() - 1, "server")
    if server is None:
        return None
    if code[server[0]] != "{":
        raise ValueError("server configuration is dynamic")
    return _js_member(code, server[0], "port")


@dataclass(frozen=True)
class _PortRead:
    name: str
    source: str
    start: int
    end: int


@dataclass(frozen=True)
class _PortExpression:
    leading: _PortRead | None
    reads: tuple[_PortRead, ...]
    fallback: Literal["nullish", "falsy", "zero-string", "none"]


def _validate_port_expression(expression: str, values: list[_PortExpression]) -> _PortExpression:
    parts = re.findall(r"""\s*(\?\?|\|\||"\d+"|'\d+'|\d+|[A-Za-z_$][\w$]*|[^\s])""", expression)
    index = 0

    def consume(expected: str) -> None:
        nonlocal index
        if index == len(parts) or parts[index] != expected:
            raise ValueError("unsupported server.port expression")
        index += 1

    def primary() -> _PortExpression:
        nonlocal index
        if index == len(parts):
            raise ValueError("unsupported server.port expression")
        part = parts[index]
        index += 1
        if part == "@":
            value = values[int(parts[index])]
            index += 1
            return value
        if part.isdigit():
            return _PortExpression(None, (), "falsy" if int(part) == 0 else "none")
        if part[0] in "\"'" and part[1:-1].isdigit():
            return _PortExpression(None, (), "zero-string" if int(part[1:-1]) == 0 else "none")
        if part == "(":
            reads = expression_value()
            consume(")")
            return reads
        if part in {"Number", "parseInt"}:
            consume("(")
            reads = expression_value()
            if part == "parseInt" and index < len(parts) and parts[index] == ",":
                consume(",")
                consume("10")
            consume(")")
            return _PortExpression(
                reads.leading, reads.reads, "falsy" if reads.fallback != "none" else "none"
            )
        raise ValueError("unsupported server.port expression")

    def expression_value() -> _PortExpression:
        nonlocal index
        value = primary()
        operator = ""
        while index < len(parts) and parts[index] in {"||", "??"}:
            if value.leading is None:
                raise ValueError("port fallback does not lead with a managed read")
            if operator and operator != parts[index]:
                raise ValueError("mixed port fallback operators require parentheses")
            operator = parts[index]
            index += 1
            right = primary()
            if value.fallback == "nullish" or (operator == "||" and value.fallback == "falsy"):
                value = _PortExpression(value.leading, value.reads + right.reads, right.fallback)
        return value

    value = expression_value()
    if index != len(parts):
        raise ValueError("unsupported server.port expression")
    return value


def _port_reads(code: str, span: tuple[int, int], managed_names: frozenset[str]) -> list[_PortRead]:
    value = _port_expression(code, span)
    if value.leading is not None and value.leading.name not in managed_names:
        raise ValueError("server.port does not lead with a declared managed port")
    return list(value.reads)


def _port_expression(
    code: str, span: tuple[int, int], seen: frozenset[str] = frozenset()
) -> _PortExpression:
    start, end = span
    expr = code[start:end]
    if "@" in expr:
        raise ValueError("unsupported server.port expression")
    if re.search(r"\b(?:const|let|var|function)\s+(?:process|Number|parseInt)\b", code):
        raise ValueError("port expression shadows a built-in binding")
    access = re.compile(
        r"\b([A-Za-z_$][\w$]*(?:\.env)?)\s*(?:\.\s*([A-Z][A-Z0-9_]*)|\[\s*[\"']([A-Z][A-Z0-9_]*)[\"']\s*\])"
    )
    visible = _js_mask_literals(expr)
    matches = [
        match
        for match in access.finditer(expr)
        if visible[match.start() : match.start() + 1].strip()
    ]
    reads = [
        _PortRead(
            match.group(2) or match.group(3),
            match.group(1),
            start + match.start(),
            start + match.end(),
        )
        for match in matches
    ]
    values = [_PortExpression(read, (read,), "nullish") for read in reads]
    remaining = expr
    for index, match in reversed(list(enumerate(matches))):
        remaining = remaining[: match.start()] + f"@{index}" + remaining[match.end() :]
    for name in set(re.findall(r"[A-Za-z_$][\w$]*", remaining)):
        if name in {"Number", "parseInt"}:
            continue
        if name in seen:
            raise ValueError(f"cyclic or shadowed port binding {name}")
        bindings = list(re.finditer(rf"\bconst\s+{re.escape(name)}\s*=\s*", code))
        if len(bindings) == 1:
            if len(re.findall(rf"\b{re.escape(name)}\s*=(?!=)", code)) != 1:
                raise ValueError(f"port binding {name} is reassigned")
            bound = bindings[0].end()
            value = _port_expression(code, (bound, _js_value_end(code, bound)), seen | {name})
        else:
            destructured = list(
                re.finditer(rf"\bconst\s*\{{\s*{re.escape(name)}\s*\}}\s*=\s*process\.env\b", code)
            )
            if len(destructured) != 1:
                raise ValueError(f"unsupported port expression or binding {name}")
            read = _PortRead(name, "process.env", start, end)
            value = _PortExpression(read, (read,), "nullish")
        remaining = re.sub(rf"\b{re.escape(name)}\b", f"@{len(values)}", remaining)
        values.append(value)
    return _validate_port_expression(remaining, values)
