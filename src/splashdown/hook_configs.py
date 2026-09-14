"""Project-owned post-checkout configuration for the hook managers splashdown
integrates with automatically. Kept below `hooks.py` so the adapters stay free of
Git and orchestration concerns."""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any, NamedTuple

from .safe_files import atomic_write_text, read_optional_editable_text
from .yamltext import _strip_hash_comments, _yaml_key_regions

HOOK_ID = "splashdown"
_UNREADABLE = object()

SPLASH_GUARD = (
    "TOP=$PWD; SPLASH=$(command -v splash) || exit 0; "
    'case "$SPLASH" in /*) ;; *) SPLASH="$TOP/$SPLASH";; esac; '
    'case "$SPLASH" in "$TOP"/*) echo "post-checkout: refusing checkout-controlled '
    'splash executable" >&2; exit 0;; esac; '
)
_ENV_FORWARD = (
    '"$SPLASH" hook post-checkout "$PRE_COMMIT_FROM_REF" "$PRE_COMMIT_TO_REF" '
    '"$PRE_COMMIT_CHECKOUT_TYPE" >&2 || true'
)
_POSITIONAL_FORWARD = '"$SPLASH" hook post-checkout "$1" "$2" "$3" >&2 || true'

PRE_COMMIT_ENTRY = f"sh -c '{SPLASH_GUARD}{_ENV_FORWARD}'"
SIMPLE_GIT_HOOKS_COMMAND = f"{SPLASH_GUARD}{_POSITIONAL_FORWARD}"

PRE_COMMIT_CONFIG_NAMES = (".pre-commit-config.yaml",)
PREK_CONFIG_NAMES = ("prek.toml",)
# prek reads the first of these that exists, so creating an earlier one silently
# retires the project's real configuration.
PREK_LOOKUP_NAMES = ("prek.toml", ".pre-commit-config.yaml", ".pre-commit-config.yml")
SIMPLE_GIT_HOOKS_DYNAMIC_NAMES = (
    ".simple-git-hooks.cjs",
    ".simple-git-hooks.js",
    ".simple-git-hooks.mjs",
    "simple-git-hooks.cjs",
    "simple-git-hooks.js",
    "simple-git-hooks.mjs",
)
SIMPLE_GIT_HOOKS_JSON_NAMES = (".simple-git-hooks.json", "simple-git-hooks.json")
SIMPLE_GIT_HOOKS_PACKAGE_KEY = "simple-git-hooks"
OVERCOMMIT_CONFIG_NAMES = (".overcommit.yml", ".overcommit.yaml")

_REPOS_RE = re.compile(r"repos:(.*)$")
_KEY_RE = re.compile(r"([A-Za-z_][\w.-]*):(.*)$")
_BLOCK_SCALAR_RE = re.compile(r"[|>][+-]?\d*")


def _yaml_double_quoted(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


PRE_COMMIT_ENTRY_LITERAL = _yaml_double_quoted(PRE_COMMIT_ENTRY)


def existing_config(cwd: Path, names: tuple[str, ...]) -> Path | None:
    return next((cwd / name for name in names if (cwd / name).exists()), None)


def _is_content(line: str) -> bool:
    return bool(line.strip())


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def _last_content_before(lines: list[str], end: int, start: int) -> int:
    for index in range(end - 1, start, -1):
        if _is_content(lines[index]):
            return index
    return start


def _item_key_column(lines: list[str], item_start: int) -> int:
    line = lines[item_start]
    body = line.lstrip()[1:]
    if body.strip():
        return _indent_of(line) + 1 + (len(body) - len(body.lstrip()))
    for index in range(item_start + 1, len(lines)):
        if _is_content(lines[index]):
            return _indent_of(lines[index])
    return _indent_of(line) + 2


def _repos_key(lines: list[str]) -> int | None:
    """The line of the one top-level `repos:` whose value is a block, or `None`."""
    found = [
        (index, _REPOS_RE.fullmatch(line))
        for index, line in enumerate(lines)
        if _is_content(line) and _indent_of(line) == 0
    ]
    keys = [(index, match) for index, match in found if match is not None]
    if len(keys) != 1 or keys[0][1].group(1).strip():
        return None
    return keys[0][0]


def _block_sequence(
    lines: list[str], key: int, key_indent: int, limit: int
) -> tuple[list[tuple[int, int]], int, int] | None:
    """`(item spans, item indent, block end)` for the block sequence under the key at
    `key`, or `None` for a shape this editor cannot read back exactly. An item indent
    equal to the key's own column is legal YAML and is accepted."""
    starts: list[int] = []
    item_indent: int | None = None
    end = limit
    for index in range(key + 1, limit):
        line = lines[index]
        if not _is_content(line):
            continue
        indent = _indent_of(line)
        stripped = line.lstrip()
        if indent < key_indent or (indent == key_indent and not stripped.startswith("-")):
            end = index
            break
        if item_indent is None:
            if not stripped.startswith("-"):
                return None
            item_indent = indent
        if indent < item_indent:
            return None
        if indent == item_indent:
            if not stripped.startswith("-"):
                return None
            starts.append(index)
    if item_indent is None:
        return [], key_indent + 2, end
    spans = [
        (start, starts[position + 1] if position + 1 < len(starts) else end)
        for position, start in enumerate(starts)
    ]
    return spans, item_indent, end


def _item_fields(lines: list[str], start: int, end: int) -> dict[str, tuple[int, str]] | None:
    """`{name: (line, inline value)}` for a sequence item written as a block mapping,
    or `None` when any line in the item is not one this editor can place exactly."""
    column = _item_key_column(lines, start)
    if column <= _indent_of(lines[start]):
        return None
    fields: dict[str, tuple[int, str]] = {}
    pending, nested, sequence = "", False, False
    for index in range(start, end):
        line = lines[index]
        if not _is_content(line):
            continue
        indent = column if index == start else _indent_of(line)
        if index == start and not line[_indent_of(line) + 1 :].strip():
            continue
        if indent > column:
            if not (sequence or (not pending) or _BLOCK_SCALAR_RE.fullmatch(pending)):
                return None
            if not sequence:
                nested = True
            continue
        if indent < column:
            return None
        if line[column:].startswith("-"):
            if pending or nested:
                return None
            sequence = True
            continue
        match = _KEY_RE.fullmatch(line[column:])
        if match is None or match.group(1) in fields:
            return None
        pending, nested, sequence = match.group(2).strip(), False, False
        fields[match.group(1)] = (index, pending)
    return fields


class _Hooks(NamedTuple):
    """Where a repo item's hook list is, and where a new hook item belongs in it."""

    spans: list[tuple[int, int]]
    indent: int
    anchor: int
    key_line: int | None


def _repo_hooks(
    lines: list[str], start: int, end: int, fields: dict[str, tuple[int, str]]
) -> _Hooks | None:
    column = _item_key_column(lines, start)
    slot = fields.get("hooks")
    if slot is None:
        return _Hooks([], column + 2, _last_content_before(lines, end, start) + 1, None)
    key_line, value = slot
    if value:
        return None
    parsed = _block_sequence(lines, key_line, column, end)
    if parsed is None:
        return None
    spans, indent, block_end = parsed
    anchor = _last_content_before(lines, block_end, key_line) + 1 if spans else key_line + 1
    return _Hooks(spans, indent, anchor, key_line)


def _plain(value: str) -> str:
    return value.strip("\"'")


def _hook_state(lines: list[str], hooks: _Hooks) -> str | None:
    """`"ok"` / `"modified"` for splashdown's own hook item, `None` when this hook
    list does not carry one. An unreadable item aborts with `"unrecognized"`."""
    for start, end in hooks.spans:
        fields = _item_fields(lines, start, end)
        if fields is None:
            return "unrecognized"
        if "id" not in fields or _plain(fields["id"][1]) != HOOK_ID:
            continue
        entry = fields.get("entry")
        if entry is None:
            return "modified"
        return "ok" if entry[1] == PRE_COMMIT_ENTRY_LITERAL else "modified"
    return None


def _pre_commit_insertion(lines: list[str], repos: int) -> tuple[int, list[str]] | str:
    """`(anchor line, block to insert)`, or the state name when the document carries
    splashdown's hook already or is in a shape this editor will not touch."""
    if len(_yaml_key_regions("\n".join(lines), "repos", indent=0)) != 1:
        return "unrecognized"
    parsed = _block_sequence(lines, repos, 0, len(lines))
    if parsed is None:
        return "unrecognized"
    spans, indent, end = parsed
    local: _Hooks | None = None
    for start, stop in spans:
        fields = _item_fields(lines, start, stop)
        if fields is None:
            return "unrecognized"
        hooks = _repo_hooks(lines, start, stop, fields)
        if hooks is None:
            return "unrecognized"
        state = _hook_state(lines, hooks)
        if state is not None:
            return state
        if local is None and "repo" in fields and _plain(fields["repo"][1]) == "local":
            local = hooks
    if local is None:
        block = [
            f"{' ' * indent}- repo: local",
            f"{' ' * (indent + 2)}hooks:",
            *_pre_commit_hook_lines(indent + 4),
        ]
        anchor = _last_content_before(lines, end, repos) + 1 if spans else repos + 1
        return anchor, block
    block = _pre_commit_hook_lines(local.indent)
    if local.key_line is None:
        block = [f"{' ' * (local.indent - 2)}hooks:", *block]
    return local.anchor, block


def _pre_commit_analysis(text: str) -> tuple[str, str | None]:
    """`(state, updated text)`. The update is returned only for `"missing"`, and only
    when the rewritten document reads back with splashdown's hook in place."""
    raw = text.splitlines()
    lines = _strip_hash_comments(text).split("\n")
    repos = _repos_key(lines)
    if repos is None:
        return "unrecognized", None
    plan = _pre_commit_insertion(lines, repos)
    if isinstance(plan, str):
        return plan, None
    anchor, block = plan
    newline = "\r\n" if "\r\n" in text else "\n"
    updated = newline.join(raw[:anchor] + block + raw[anchor:])
    updated += newline if text.endswith("\n") else ""
    if _pre_commit_analysis(updated)[0] != "ok":
        return "unrecognized", None
    return "missing", updated


def _pre_commit_hook_lines(indent: int) -> list[str]:
    pad = " " * indent
    inner = " " * (indent + 2)
    return [
        f"{pad}- id: {HOOK_ID}",
        f"{inner}name: splashdown post-checkout",
        f"{inner}language: system",
        f"{inner}entry: {PRE_COMMIT_ENTRY_LITERAL}",
        f"{inner}always_run: true",
        f"{inner}pass_filenames: false",
        f"{inner}stages: [post-checkout]",
    ]


def _pre_commit_document() -> str:
    body = "\n".join(_pre_commit_hook_lines(6))
    return f"repos:\n  - repo: local\n    hooks:\n{body}\n"


def pre_commit_config_path(cwd: Path) -> Path:
    return existing_config(cwd, PRE_COMMIT_CONFIG_NAMES) or cwd / PRE_COMMIT_CONFIG_NAMES[0]


def pre_commit_state(cwd: Path, *, path: Path | None = None) -> str:
    """`"ok"`, `"missing"`, `"modified"`, or `"unrecognized"`. Only a document this
    editor can read back structurally can report anything but `"unrecognized"`."""
    path = pre_commit_config_path(cwd) if path is None else path
    try:
        text = read_optional_editable_text(path, root=cwd)
    except ValueError:
        return "unrecognized"
    if text is None or not text.strip():
        return "missing"
    return _pre_commit_analysis(text)[0]


def _report_unrecognized(path: Path, manager: str) -> None:
    print(
        f"{path.name} is not in a shape splashdown can edit safely ({manager}) — "
        "leaving it untouched",
        file=sys.stderr,
    )


def wire_pre_commit(cwd: Path, *, manager: str = "pre-commit", path: Path | None = None) -> bool:
    """Add splashdown's local hook to the pre-commit YAML configuration prek also
    reads. Returns True when the configuration carries the current entry."""
    path = pre_commit_config_path(cwd) if path is None else path
    try:
        text = read_optional_editable_text(path, root=cwd)
    except ValueError:
        _report_unrecognized(path, manager)
        return False
    if text is None or not text.strip():
        atomic_write_text(path, _pre_commit_document(), root=cwd, create=True)
        print(f"wired post-checkout in {path.name} ({manager})", file=sys.stderr)
        return True
    state, updated = _pre_commit_analysis(text)
    if state == "ok":
        return True
    if state == "modified":
        print(
            f"existing splashdown hook in {path.name} was modified — leaving it untouched",
            file=sys.stderr,
        )
        return False
    if updated is None:
        _report_unrecognized(path, manager)
        return False
    atomic_write_text(path, updated, root=cwd, create=True)
    print(f"wired post-checkout in {path.name} ({manager})", file=sys.stderr)
    return True


def prek_config_path(cwd: Path) -> Path:
    """The configuration prek itself would load, which is a pre-commit YAML whenever
    the project has one and no `prek.toml`."""
    return existing_config(cwd, PREK_LOOKUP_NAMES) or cwd / PREK_CONFIG_NAMES[0]


def _prek_local_hooks(data: Any) -> list[Any] | None:
    repos = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(repos, list):
        return None
    for repo in repos:
        if not isinstance(repo, dict) or repo.get("repo") != "local":
            continue
        hooks = repo.get("hooks")
        return hooks if isinstance(hooks, list) else None
    return None


def prek_state(cwd: Path) -> str:
    path = prek_config_path(cwd)
    if path.suffix != ".toml":
        return pre_commit_state(cwd, path=path)
    try:
        text = read_optional_editable_text(path, root=cwd)
    except ValueError:
        return "unrecognized"
    if text is None or not text.strip():
        return "missing"
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return "unrecognized"
    hooks = _prek_local_hooks(data)
    if hooks is None:
        return "missing"
    ours = next(
        (h for h in hooks if isinstance(h, dict) and h.get("id") == HOOK_ID),
        None,
    )
    if ours is None:
        return "missing"
    return "ok" if ours.get("entry") == PRE_COMMIT_ENTRY else "modified"


def _prek_hook_item() -> Any:
    import tomlkit  # noqa: PLC0415

    hook = tomlkit.inline_table()
    hook.update(
        {
            "id": HOOK_ID,
            "name": "splashdown post-checkout",
            "language": "system",
            "entry": PRE_COMMIT_ENTRY,
            "always_run": True,
            "pass_filenames": False,
            "stages": ["post-checkout"],
        }
    )
    return hook


def _prek_document_text(text: str | None) -> str | None:
    import tomlkit  # noqa: PLC0415

    document = tomlkit.parse(text) if text else tomlkit.document()
    repos = document.get("repos")
    if repos is None:
        repos = tomlkit.aot()
        document["repos"] = repos
    if not isinstance(repos, list):
        return None
    local = next(
        (repo for repo in repos if isinstance(repo, dict) and repo.get("repo") == "local"),
        None,
    )
    if local is None:
        local = tomlkit.table()
        local["repo"] = "local"
        repos.append(local)
    hooks: Any = local.get("hooks")
    if hooks is None:
        hooks = tomlkit.array()
        hooks.multiline(True)
        local["hooks"] = hooks
    if not isinstance(hooks, list):
        return None
    hooks.append(_prek_hook_item())
    return tomlkit.dumps(document)


def wire_prek(cwd: Path) -> bool:
    """Add splashdown's local hook to the configuration prek itself loads: its native
    TOML, or the pre-commit YAML that outranks a `prek.toml` splashdown would create."""
    path = prek_config_path(cwd)
    if path.suffix != ".toml":
        return wire_pre_commit(cwd, manager="prek", path=path)
    try:
        text = read_optional_editable_text(path, root=cwd)
    except ValueError:
        _report_unrecognized(path, "prek")
        return False
    state = prek_state(cwd)
    if state == "ok":
        return True
    if state in {"modified", "unrecognized"}:
        print(
            f"existing splashdown hook in {path.name} was modified — leaving it untouched",
            file=sys.stderr,
        )
        return False
    updated = _prek_document_text(text if text and text.strip() else None)
    if updated is None:
        _report_unrecognized(path, "prek")
        return False
    atomic_write_text(path, updated, root=cwd, create=True)
    print(f"wired post-checkout in {path.name} (prek)", file=sys.stderr)
    return True


def _package_json_config(cwd: Path) -> Any:
    """The `simple-git-hooks` value package.json declares, `None` when it declares
    none, or `_UNREADABLE` when splashdown may not read the file back exactly."""
    try:
        text = read_optional_editable_text(cwd / "package.json", root=cwd)
    except ValueError:
        return _UNREADABLE
    if text is None:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return _UNREADABLE
    return data.get(SIMPLE_GIT_HOOKS_PACKAGE_KEY) if isinstance(data, dict) else None


def simple_git_hooks_target(cwd: Path) -> tuple[str, Path]:
    """`(kind, path)` where kind is `"dynamic"`, `"json"`, or `"package"`. The
    order mirrors simple-git-hooks' own configuration lookup, so splashdown never
    writes into a file the tool would not read."""
    dynamic = existing_config(cwd, SIMPLE_GIT_HOOKS_DYNAMIC_NAMES)
    if dynamic is not None:
        return "dynamic", dynamic
    existing_json = existing_config(cwd, SIMPLE_GIT_HOOKS_JSON_NAMES)
    if existing_json is not None:
        return "json", existing_json
    declared = _package_json_config(cwd)
    if declared is _UNREADABLE:
        return "unreadable", cwd / "package.json"
    if isinstance(declared, str):
        return "pointer", cwd / "package.json"
    if isinstance(declared, dict):
        return "package", cwd / "package.json"
    return "json", cwd / SIMPLE_GIT_HOOKS_JSON_NAMES[0]


def _simple_git_hooks_settings(cwd: Path, kind: str, path: Path) -> dict[str, Any] | None:
    if kind == "package":
        declared = _package_json_config(cwd)
        return declared if isinstance(declared, dict) else None
    try:
        text = read_optional_editable_text(path, root=cwd)
    except ValueError:
        return None
    if text is None or not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def simple_git_hooks_state(cwd: Path) -> str:
    kind, path = simple_git_hooks_target(cwd)
    if kind in {"dynamic", "pointer", "unreadable"}:
        return "unrecognized"
    settings = _simple_git_hooks_settings(cwd, kind, path)
    if settings is None:
        return "unrecognized"
    current = settings.get("post-checkout")
    if current is None:
        return "missing"
    return "ok" if current == SIMPLE_GIT_HOOKS_COMMAND else "modified"


def _json_indent(text: str) -> str | int:
    match = re.search(r"\n([ \t]+)\S", text)
    if match is None:
        return 2
    return "\t" if match.group(1).startswith("\t") else len(match.group(1))


def _line_indent(text: str, index: int) -> str:
    line = text[text.rfind("\n", 0, index) + 1 : index]
    return line[: len(line) - len(line.lstrip())]


def _object_span(text: str, value: Any, key: str | None) -> tuple[int, int] | None:
    """Offsets of the `{` and `}` of the object holding `value`: the document's own
    outermost object when `key` is None, otherwise the one that member names."""
    if key is None:
        starts = [text.find("{")]
    else:
        starts = [m.end() for m in re.finditer(rf'"{re.escape(key)}"[ \t]*:\s*', text)]
    for start in starts:
        if start < 0 or start >= len(text) or text[start] != "{":
            continue
        close = _matching_brace(text, start)
        if close is None:
            continue
        try:
            if json.loads(text[start : close + 1]) == value:
                return start, close
        except json.JSONDecodeError:
            continue
    return None


def _matching_brace(text: str, start: int) -> int | None:
    depth, quote, index = 0, False, start
    while index < len(text):
        character = text[index]
        if quote:
            if character == "\\":
                index += 2
                continue
            if character == '"':
                quote = False
        elif character == '"':
            quote = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _member_text(name: str, value: str) -> str:
    return f"{json.dumps(name, ensure_ascii=False)}: {json.dumps(value, ensure_ascii=False)}"


def _insert_json_member(text: str, span: tuple[int, int], name: str, value: str) -> str:
    """Splice one member into an existing JSON object, touching nothing else in the
    file. `json.dumps` over the whole document would reflow every nested literal the
    project hand-formatted, so only the inserted bytes are new."""
    open_index, close_index = span
    member = _member_text(name, value)
    body = text[open_index + 1 : close_index]
    newline = "\r\n" if "\r\n" in text else "\n"
    if not body.strip():
        step = _json_indent(text)
        pad = "\t" if step == "\t" else " " * int(step)
        base = _line_indent(text, open_index)
        return f"{text[: open_index + 1]}{newline}{base}{pad}{member}{newline}{base}{text[close_index:]}"
    cut = close_index - (len(body) - len(body.rstrip()))
    if "\n" not in body:
        return f"{text[:cut]}, {member}{text[cut:]}"
    return f"{text[:cut]},{newline}{_line_indent(text, cut)}{member}{text[cut:]}"


def _new_json_document(name: str, value: str) -> str:
    return json.dumps({name: value}, indent=2, ensure_ascii=False) + "\n"


def _simple_git_hooks_update(text: str | None, settings: dict[str, Any], kind: str) -> str | None:
    """The configuration text carrying splashdown's command, or `None` when the file
    cannot be edited by splicing and read back as the same data."""
    if text is None or not text.strip():
        return _new_json_document("post-checkout", SIMPLE_GIT_HOOKS_COMMAND)
    key = SIMPLE_GIT_HOOKS_PACKAGE_KEY if kind == "package" else None
    span = _object_span(text, settings, key)
    if span is None:
        return None
    updated = _insert_json_member(text, span, "post-checkout", SIMPLE_GIT_HOOKS_COMMAND)
    expected = json.loads(text)
    target = expected[SIMPLE_GIT_HOOKS_PACKAGE_KEY] if kind == "package" else expected
    target["post-checkout"] = SIMPLE_GIT_HOOKS_COMMAND
    try:
        if json.loads(updated) != expected:
            return None
    except json.JSONDecodeError:
        return None
    return updated


def wire_simple_git_hooks(cwd: Path) -> bool:
    """Add splashdown's command to the simple-git-hooks configuration the tool
    would actually read. Dynamic JavaScript configuration is never executed or
    edited, so it gets manual instructions instead."""
    kind, path = simple_git_hooks_target(cwd)
    state = simple_git_hooks_state(cwd)
    if state == "ok":
        return True
    if kind == "dynamic":
        print(
            f"{path.name} configures simple-git-hooks dynamically — leaving it untouched",
            file=sys.stderr,
        )
        return False
    if kind == "pointer":
        print(
            f"{path.name} points simple-git-hooks at another configuration file — "
            "leaving it untouched",
            file=sys.stderr,
        )
        return False
    if state == "unrecognized":
        _report_unrecognized(path, "simple-git-hooks")
        return False
    if state == "modified":
        print(
            f"existing splashdown post-checkout in {path.name} was modified — leaving it untouched",
            file=sys.stderr,
        )
        return False
    try:
        text = read_optional_editable_text(path, root=cwd)
    except ValueError:
        _report_unrecognized(path, "simple-git-hooks")
        return False
    settings = _simple_git_hooks_settings(cwd, kind, path)
    updated = None if settings is None else _simple_git_hooks_update(text, settings, kind)
    if updated is None:
        _report_unrecognized(path, "simple-git-hooks")
        return False
    atomic_write_text(path, updated, root=cwd, create=True)
    print(f"wired post-checkout in {path.name} (simple-git-hooks)", file=sys.stderr)
    return True
