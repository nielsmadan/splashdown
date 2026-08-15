from __future__ import annotations

import os
import re
import stat
import sys
import tempfile
from pathlib import Path

from .catalog import PROFILES
from .inventory import AppInventory
from .recipe import Recipe

_GUIDANCE_START = "<!-- >>> splashdown-managed agent-guidance >>> -->"
_GUIDANCE_END = "<!-- <<< splashdown-managed agent-guidance <<< -->"
_AGENT_FILES = ("AGENTS.md", "CLAUDE.md")
_AGENTS_IMPORT_RE = re.compile(r"(?<![A-Za-z0-9_])@(?:\./)?AGENTS\.md\b")


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

    lines = [
        _GUIDANCE_START,
        "## Splashdown",
        "",
        "Splashdown assigns this checkout's ports. Ranges in `splashdown.toml` are",
        "allocation pools, not the assigned values. Never hardcode numeric port values or",
        "add numeric port overrides. Prefer the project's existing scripts when they already",
        "consume the Splashdown environment.",
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
        if not path.exists() and not path.is_symlink():
            continue
        text = _read_agent_file(path)
        if text is None:
            continue
        if name == "CLAUDE.md" and agents_exists and _AGENTS_IMPORT_RE.search(text):
            updated = _replace_managed_block(path, text, "")
            if updated is not None and updated != text and _write_agent_file(path, updated):
                print(f"removed guidance from {name}", file=sys.stderr)
            continue
        updated = _replace_managed_block(path, text, block)
        if updated is not None and updated != text and _write_agent_file(path, updated):
            action = "updated" if block else "removed guidance from"
            print(f"{action} {name}", file=sys.stderr)


def remove_agent_guidance(cwd: Path) -> None:
    for name in _AGENT_FILES:
        path = cwd / name
        if not path.exists() and not path.is_symlink():
            continue
        text = _read_agent_file(path)
        if text is None:
            continue
        updated = _replace_managed_block(path, text, "")
        if updated is not None and updated != text and _write_agent_file(path, updated):
            print(f"removed guidance from {name}", file=sys.stderr)


def _replace_managed_block(path: Path, text: str, block: str) -> str | None:
    starts = [match.start() for match in re.finditer(re.escape(_GUIDANCE_START), text)]
    ends = [match.end() for match in re.finditer(re.escape(_GUIDANCE_END), text)]
    if not starts and not ends:
        if not block:
            return text
        newline = _newline_for(text)
        rendered = block.replace("\n", newline)
        separator = "" if not text or text.endswith(newline) else newline
        return f"{text}{separator}{rendered}{newline}"
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        print(
            f"warning: {path.name} has malformed Splashdown guidance markers; left unchanged",
            file=sys.stderr,
        )
        return None
    newline = _newline_for(text)
    before = text[: starts[0]]
    after = text[ends[0] :]
    if not block and after.startswith(newline):
        after = after[len(newline) :]
    rendered = block.replace("\n", newline)
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


def _newline_for(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    if "\r" in text and "\n" not in text:
        return "\r"
    return "\n"
