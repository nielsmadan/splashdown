from __future__ import annotations

import os
import re
import stat
import sys
import tempfile
from pathlib import Path

from .catalog import PROFILES
from .constants import newline_for
from .inventory import AppInventory
from .recipe import Recipe

_GUIDANCE_START = "<!-- >>> splashdown-managed agent-guidance >>> -->"
_GUIDANCE_END = "<!-- <<< splashdown-managed agent-guidance <<< -->"
_AGENT_FILES = ("AGENTS.md", "CLAUDE.md")
_AGENTS_IMPORT_RE = re.compile(r"(?<![A-Za-z0-9_])@(?:\./)?AGENTS\.md\b")
_GENERATED_HEADER_RE = re.compile(
    r"@generated\b"
    r"|\b(?:auto[-\s]?)?generated\s+(?:by|from|with)\s+\S"
    r"|\bdo\s+not\s+edit\b"
    r"|\bdon'?t\s+edit\b",
    re.IGNORECASE,
)
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n.*?^---[ \t]*(?:\r?\n|\Z)", re.DOTALL | re.MULTILINE)
_MARKER_SUMMARY_LIMIT = 160
_ABSENT = "absent"
_PRESENT = "present"
_MALFORMED = "malformed"
_SOURCE_ACTIONS = {
    "add": "add the Splashdown guidance block to",
    "replace": "replace the Splashdown guidance block in",
    "remove": "remove the Splashdown guidance block from",
    "repair": "repair the Splashdown guidance markers in",
}
_BLOCK_START_RULE = "  ----- begin Splashdown guidance block -----"
_BLOCK_END_RULE = "  ----- end Splashdown guidance block -----"


def render_agent_guidance(cwd: Path, recipe: Recipe) -> str:
    apps: list[tuple[str, dict[str, object], list[str]]] = []
    for name, spec in recipe.apps.items():
        profile_name = str(spec["profile"])
        profile = PROFILES.get(profile_name)
        if profile is None:
            continue
        port_names = [
            resource_name
            for resource_name in spec["resources"]
            if recipe.resources[resource_name].get("type") == "port"
        ]
        if port_names:
            apps.append((name, spec, port_names))

    if not apps:
        return ""

    env_file = _markdown_code(recipe.env_file)
    lines = [
        _GUIDANCE_START,
        "## Splashdown",
        "",
        f"Splashdown assigns this checkout's ports and writes the resolved values to {env_file}.",
        "Ranges in `splashdown.toml` are allocation pools, not the assigned values.",
        "Never hardcode numeric port values or add numeric port overrides. Read one value",
        "with `splash env get KEY` and list the variable names with `splash env`.",
        f"Run `splash sync` when {env_file} is missing or out of date, and prefer the",
        "project's existing scripts when they already consume the Splashdown environment.",
        "Run any manual commands below from the checkout root.",
    ]
    for name, spec, port_names in apps:
        profile_name = str(spec["profile"])
        path = str(spec["path"])
        app = AppInventory(
            name=name,
            path=(cwd / path).resolve(),
            profile=profile_name,
            project_path=Path(path),
        )
        ports = ", ".join(f"`{port}`" for port in port_names)
        lines.extend(
            [
                "",
                f"### App {_markdown_code(name)} ({_markdown_code(path)})",
                "",
                f"Framework: `{profile_name}`. Allocated port variable"
                f"{'s' if len(port_names) != 1 else ''}: {ports}.",
            ]
        )
        specific = PROFILES[profile_name].agent_guidance(app, port_names)
        if specific:
            lines.extend(["", *specific])
    lines.extend(["", _GUIDANCE_END])
    return "\n".join(lines)


def sync_agent_guidance(cwd: Path, recipe: Recipe) -> None:
    block = render_agent_guidance(cwd, recipe)
    agents_path = cwd / "AGENTS.md"
    try:
        agents_exists = stat.S_ISREG(agents_path.lstat().st_mode)
    except OSError:
        agents_exists = False
    for name in _AGENT_FILES:
        path = cwd / name
        text = _managed_text(path)
        if text is None:
            continue
        desired = block
        if name == "CLAUDE.md" and agents_exists and _AGENTS_IMPORT_RE.search(text):
            desired = ""
        _apply_managed_block(path, text, desired)


def remove_agent_guidance(cwd: Path) -> None:
    for name in _AGENT_FILES:
        path = cwd / name
        text = _managed_text(path)
        if text is None:
            continue
        _apply_managed_block(path, text, "")


def _managed_text(path: Path) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    return _read_agent_file(path)


def _apply_managed_block(path: Path, text: str, block: str) -> None:
    state = _managed_state(text)
    if state == _MALFORMED:
        marker = _generated_marker(text)
        if marker is None:
            print(
                f"warning: {path.name} has malformed Splashdown guidance markers; left unchanged",
                file=sys.stderr,
            )
            return
        _report_generated_file(path, marker, block=block, state=state)
        return
    updated = _replace_managed_block(text, block, state)
    if updated == text:
        return
    marker = _generated_marker(text)
    if marker is not None:
        _report_generated_file(path, marker, block=block, state=state)
        return
    if _write_agent_file(path, updated):
        outcome = "updated" if block else "removed guidance from"
        print(f"{outcome} {path.name}", file=sys.stderr)


def _generated_marker(text: str) -> str | None:
    """The leading HTML comment by which another tool claims ownership of this file.

    Recognized only in the comments that open the file, after any leading YAML
    frontmatter, and only when the comment attributes the file to a generator or
    forbids editing it. Prose that merely mentions generated content, frontmatter
    fields, and a marker further down the file, do not match."""
    remainder = _skip_frontmatter(text.lstrip("\ufeff").lstrip())
    while True:
        remainder = remainder.lstrip()
        if not remainder.startswith("<!--"):
            return None
        end = remainder.find("-->")
        if end < 0:
            return None
        if _GENERATED_HEADER_RE.search(remainder[len("<!--") : end]):
            return _marker_summary(remainder[: end + len("-->")])
        remainder = remainder[end + len("-->") :]


def _skip_frontmatter(text: str) -> str:
    match = _FRONTMATTER_RE.match(text)
    return text[match.end() :] if match else text


def _marker_summary(marker: str) -> str:
    collapsed = "".join(char for char in " ".join(marker.split()) if char.isprintable())
    if len(collapsed) <= _MARKER_SUMMARY_LIMIT:
        return collapsed
    return f"{collapsed[: _MARKER_SUMMARY_LIMIT - 3]}..."


def _source_action(block: str, state: str) -> str:
    if not block:
        return "remove"
    if state == _MALFORMED:
        return "repair"
    return "replace" if state == _PRESENT else "add"


def _report_generated_file(path: Path, marker: str, *, block: str, state: str) -> None:
    action = _source_action(block, state)
    condition = (
        "has malformed Splashdown guidance markers and is generated by another tool"
        if state == _MALFORMED
        else "is generated by another tool"
    )
    print(f"warning: {path.name} {condition}; left unchanged", file=sys.stderr)
    print(f"  marker: {marker}", file=sys.stderr)
    print(
        f"  {_SOURCE_ACTIONS[action]} the source that generates {path.name},"
        " then re-run that generator",
        file=sys.stderr,
    )
    if block:
        print(_BLOCK_START_RULE, file=sys.stderr)
        print(block, file=sys.stderr)
        print(_BLOCK_END_RULE, file=sys.stderr)


def _managed_state(text: str) -> str:
    starts = [match.start() for match in re.finditer(re.escape(_GUIDANCE_START), text)]
    ends = [match.end() for match in re.finditer(re.escape(_GUIDANCE_END), text)]
    if not starts and not ends:
        return _ABSENT
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        return _MALFORMED
    return _PRESENT


def _replace_managed_block(text: str, block: str, state: str) -> str:
    newline = newline_for(text)
    rendered = block.replace("\n", newline)
    if state == _ABSENT:
        if not block:
            return text
        separator = "" if not text or text.endswith(newline) else newline
        return f"{text}{separator}{rendered}{newline}"
    before = text[: text.index(_GUIDANCE_START)]
    after = text[text.index(_GUIDANCE_END) + len(_GUIDANCE_END) :]
    if not block and after.startswith(newline):
        after = after[len(newline) :]
    return f"{before}{rendered}{after}"


def _read_agent_file(path: Path) -> str | None:
    try:
        current = path.lstat()
        if stat.S_ISLNK(current.st_mode):
            print(
                f"warning: {path.name} is a symlink or unreadable; left unchanged",
                file=sys.stderr,
            )
            return None
        if not stat.S_ISREG(current.st_mode):
            print(
                f"warning: {path.name} is not a regular file; left unchanged",
                file=sys.stderr,
            )
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                print(
                    f"warning: {path.name} is not a regular file; left unchanged",
                    file=sys.stderr,
                )
                return None
            data = file.read()
    except OSError:
        print(f"warning: {path.name} is a symlink or unreadable; left unchanged", file=sys.stderr)
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        print(f"warning: {path.name} is not UTF-8; left unchanged", file=sys.stderr)
        return None


def _write_agent_file(path: Path, text: str) -> bool:
    try:
        current = path.lstat()
        if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
            print(
                f"warning: {path.name} is not a regular file; left unchanged",
                file=sys.stderr,
            )
            return False
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(text.encode("utf-8"))
            temp_path.chmod(stat.S_IMODE(current.st_mode))
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)
    except OSError:
        print(f"warning: {path.name} could not be updated; left unchanged", file=sys.stderr)
        return False
    return True


def _markdown_code(value: str) -> str:
    escaped = (
        value.replace("<", "&lt;").replace(">", "&gt;").replace("\r", r"\r").replace("\n", r"\n")
    )
    longest = max((len(match.group()) for match in re.finditer(r"`+", escaped)), default=0)
    fence = "`" * max(1, longest + 1)
    padding = " " if escaped.startswith("`") or escaped.endswith("`") else ""
    return f"{fence}{padding}{escaped}{padding}{fence}"
