from __future__ import annotations

import os
import re
import subprocess
import uuid as uuid_mod
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import NamedTuple

from .constants import RECIPE_NAME, newline_for, normalized_env_reference, split_lines
from .errors import SetupError
from .recipe import (
    CommandSpec,
    Recipe,
    _current_branch,
    _env_quote,
    _make_scope,
    _slug,
    render_template,
    topo_sort,
)
from .registry import Registry
from .safe_files import atomic_write_text, read_optional_editable_text

_PORT_RANGE_LEN = 2


@dataclass(frozen=True)
class WriterResult:
    writer: str
    message: str
    changed: bool
    stdout_values: dict[str, str] = field(default_factory=dict)


def _required_set_default(name: str, default: object) -> str:
    if default is None:
        raise ValueError(
            f"`{name}` is a set-type resource with no value yet; "
            f"run `splash env set {name}=VALUE` or set a `default = ...`"
        )
    return str(default)


def provision(
    cwd: Path,
    *,
    registry: Registry,
    reprovision: bool = False,
    recipe: Recipe | None = None,
) -> dict[str, str]:
    recipe_path = cwd / RECIPE_NAME
    if not recipe_path.exists():
        raise FileNotFoundError(f"no {RECIPE_NAME} in {cwd}; run `splash init`")
    recipe = recipe or Recipe.load(recipe_path)
    abspath = str(cwd.resolve())
    branch = _current_branch(cwd)
    resolved: dict[str, str] = {}

    for name in topo_sort(recipe):
        spec = recipe.resources[name]
        rtype = spec.get("type")
        if rtype == "port":
            rng = spec.get("range")
            if not (isinstance(rng, list) and len(rng) == _PORT_RANGE_LEN):
                raise ValueError(f"`{name}` port resource needs range = [lo, hi]")
            lo, hi = int(rng[0]), int(rng[1])
            if reprovision:
                registry.remove_port(abspath, name)
            value = str(registry.allocate_port(abspath, name, lo, hi))
        elif rtype == "uuid":
            if reprovision:
                value = str(uuid_mod.uuid4())
                registry.set_kv(abspath, name, value)
            else:
                value = registry.get_or_create_kv(abspath, name, lambda: str(uuid_mod.uuid4()))
        elif rtype == "cwd":
            value = cwd.name
            registry.set_kv(abspath, name, value)
        elif rtype == "cwd-slug":
            value = _slug(cwd.name)
            registry.set_kv(abspath, name, value)
        elif rtype == "template":
            tpl = spec.get("template")
            if not isinstance(tpl, str):
                raise ValueError(f"`{name}` template resource needs `template = ...`")
            scope = _make_scope(cwd, branch, resolved)
            value = render_template(tpl, scope)
            registry.set_kv(abspath, name, value)
        elif rtype == "set":
            value = registry.get_or_create_kv(
                abspath,
                name,
                partial(_required_set_default, name, spec.get("default")),
            )
        else:
            raise ValueError(f"`{name}` has unknown type `{rtype}`")
        resolved[name] = value
    return resolved


DEFAULT_WRITER = "splashdown-env"
_CREATE_MODE = 0o600
_NON_FILE_WRITERS = ("stdout", "none")


def resolve_writer(writer: str, env_file: str) -> str:
    """The destination a declared writer names, in one canonical spelling so two
    ways of naming the same file land in one group. `splashdown-env` follows the
    recipe's configured default so one filename never gets two ownership models."""
    if writer == DEFAULT_WRITER:
        return f"envfile={env_file}"
    if writer.startswith("envfile="):
        return f"envfile={normalized_env_reference(writer.removeprefix('envfile='))}"
    return writer


def writer_output_path(writer: str) -> str | None:
    """The checkout-relative file a resolved writer delivers to, or None when it
    delivers no file at all."""
    if writer.startswith("envfile="):
        return writer.removeprefix("envfile=")
    return ".envrc.local" if writer == "envrc" else None


def env_output_paths(recipe: Recipe) -> list[str]:
    """Every checkout-relative file this recipe's writers deliver values to. A
    recipe whose resources all route elsewhere never produces the default
    destination, so it does not appear here."""
    writers = [
        resolve_writer(spec.get("writer", DEFAULT_WRITER), recipe.env_file)
        for spec in recipe.resources.values()
    ] or [resolve_writer(DEFAULT_WRITER, recipe.env_file)]
    paths = [writer_output_path(writer) for writer in writers]
    return list(dict.fromkeys(path for path in paths if path is not None))


def _undelivered_keys(recipe: Recipe) -> set[str]:
    """Declared resources whose writer produces no file, so no destination ever
    held a line splashdown wrote for them and none may be removed on their behalf."""
    return {
        name
        for name, spec in recipe.resources.items()
        if resolve_writer(spec.get("writer", DEFAULT_WRITER), recipe.env_file) in _NON_FILE_WRITERS
    }


def _confined_target(cwd: Path, relpath: str) -> Path:
    target = cwd / relpath
    # Recipes run automatically after checkout; confine env writes to the checkout.
    # The parent, not the file: a symlinked destination is the writer's to refuse.
    if not target.parent.resolve().is_relative_to(cwd.resolve()):
        raise ValueError(
            f"writer `envfile={relpath}` resolves outside the checkout; "
            "envfile paths must stay within the project directory"
        )
    return target


def write_outputs(
    cwd: Path,
    recipe: Recipe,
    resolved: dict[str, str],
    *,
    known_keys: set[str],
) -> list[WriterResult]:
    """Write every writer group. `known_keys` names the keys splashdown has
    already written for this checkout (the registry rows), so the default
    destination also loses a key the recipe no longer declares."""
    default_writer = resolve_writer(DEFAULT_WRITER, recipe.env_file)
    undelivered = _undelivered_keys(recipe)
    groups: dict[str, dict[str, str]] = {}
    for name, value in resolved.items():
        writer = resolve_writer(
            recipe.resources[name].get("writer", DEFAULT_WRITER), recipe.env_file
        )
        groups.setdefault(writer, {})[name] = value

    if default_writer not in groups and (cwd / recipe.env_file).exists():
        groups[default_writer] = {}

    results: list[WriterResult] = []
    for writer, items in groups.items():
        if writer.startswith("envfile="):
            path_arg = writer.removeprefix("envfile=")
            target = _confined_target(cwd, path_arg)
            drop = (
                (set(resolved) | known_keys) - undelivered - set(items)
                if writer == default_writer
                else set()
            )
            changed = write_envfile(target, items, drop=drop, root=cwd)
            results.append(WriterResult(writer, f"{path_arg}: {len(items)} vars", changed))
        elif writer == "envrc":
            changed = write_envrc(cwd / ".envrc.local", items, root=cwd)
            results.append(WriterResult("envrc", f".envrc.local: {len(items)} vars", changed))
        elif writer == "stdout":
            results.append(WriterResult("stdout", f"stdout: {len(items)} vars", True, dict(items)))
        elif writer == "none":
            results.append(WriterResult("none", f"registry-only: {len(items)} vars", False))
        else:
            raise ValueError(f"unknown writer `{writer}`")
    return results


_ASSIGNMENT_RE = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=(.*)$")
_EXPORT_ASSIGNMENT_RE = re.compile(r"^[ \t]*export[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*=(.*)$")
# `KEY: value` and `KEY += value` name a key in a shape splashdown does not rewrite,
# so appending its own `KEY=` would leave two competing definitions behind.
_NEAR_ASSIGNMENT_RE = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*(?::|\+=)")
_UNTERMINATED_QUOTE = "an unterminated quoted value"


@dataclass(frozen=True)
class _Assignment:
    key: str
    start: int
    end: int


def _closes_quote(text: str, quote: str) -> bool:
    index = 0
    while index < len(text):
        character = text[index]
        if quote == '"' and character == "\\":
            index += 2
            continue
        if character == quote:
            return True
        index += 1
    return False


def _opening_quote(value: str) -> str | None:
    text = value.lstrip(" \t")
    quote = text[:1]
    if quote not in ("'", '"'):
        return None
    return None if _closes_quote(text[1:], quote) else quote


def _scan_assignments(
    lines: list[str], *, export: bool
) -> tuple[list[_Assignment], list[tuple[int, str, str]]]:
    """Logical assignments in an env destination, plus `(line, key, problem)` for
    each shape that names a key without being safely rewritable."""
    pattern = _EXPORT_ASSIGNMENT_RE if export else _ASSIGNMENT_RE
    assignments: list[_Assignment] = []
    problems: list[tuple[int, str, str]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        match = pattern.match(line)
        if match is None:
            near = _NEAR_ASSIGNMENT_RE.match(line)
            if near is not None:
                problems.append((index, near.group(1), "an unrecognized assignment syntax"))
            index += 1
            continue
        end = index
        quote = _opening_quote(match.group(2))
        if quote is not None:
            end = next(
                (
                    candidate
                    for candidate in range(index + 1, len(lines))
                    if _closes_quote(lines[candidate], quote)
                ),
                -1,
            )
            if end < 0:
                problems.append((index, match.group(1), _UNTERMINATED_QUOTE))
                end = index
        assignments.append(_Assignment(match.group(1), index, end))
        index = end + 1
    return assignments, problems


def _reject_ambiguous(
    path: Path,
    managed: set[str],
    assignments: list[_Assignment],
    problems: list[tuple[int, str, str]],
) -> None:
    for line, key, problem in problems:
        if problem == _UNTERMINATED_QUOTE:
            # Whoever opened it, every line below is of unknown shape, so splashdown
            # can no longer tell an assignment it manages from ordinary text.
            raise ValueError(
                f"line {line + 1} of `{path}` opens a quoted value for `{key}` that is "
                "never closed, so splashdown cannot tell where the assignments below it "
                "begin; close the quote and re-run"
            )
        if key in managed:
            raise ValueError(
                f"line {line + 1} of `{path}` uses {problem} for `{key}`, which splashdown "
                f"manages; rewrite that line as `{key}=VALUE`, or route the resource "
                'elsewhere with `writer = "envfile=PATH"`'
            )
    seen: set[str] = set()
    for assignment in assignments:
        if assignment.key not in managed:
            continue
        if assignment.key in seen:
            raise ValueError(
                f"`{path}` assigns `{assignment.key}` more than once (line "
                f"{assignment.start + 1}), and splashdown manages that key; leave a single "
                "assignment so its value is unambiguous"
            )
        seen.add(assignment.key)


def existing_managed_keys(path: Path, keys: set[str], *, root: Path | None) -> list[str]:
    """Declared keys a destination already assigns. Raises when one of them is
    assigned in a shape splashdown cannot rewrite."""
    if not path.parent.is_dir():
        return []
    current = read_optional_editable_text(path, root=root)
    if current is None:
        return []
    assignments, problems = _scan_assignments(_destination_lines(current), export=False)
    _reject_ambiguous(path, keys, assignments, problems)
    return sorted({item.key for item in assignments if item.key in keys})


def _replaced_lines(assignments: list[_Assignment], managed: set[str]) -> set[int]:
    return {
        index
        for assignment in assignments
        if assignment.key in managed
        for index in range(assignment.start, assignment.end + 1)
    }


def _destination_lines(text: str) -> list[str]:
    """`split_lines` without the trailing empty element a final newline produces,
    so an appended assignment lands on its own line rather than after a blank."""
    lines = split_lines(text)
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _merged_lines(
    lines: list[str],
    assignments: list[_Assignment],
    managed: set[str],
    rendered: dict[str, str],
) -> list[str]:
    """`lines` with each managed assignment replaced where it already stands, and
    the remaining rendered keys appended."""
    replacements: dict[int, str] = {}
    removed = _replaced_lines(assignments, managed)
    placed: set[str] = set()
    for assignment in assignments:
        line = rendered.get(assignment.key)
        if assignment.key not in managed or line is None or assignment.key in placed:
            continue
        replacements[assignment.start] = line
        placed.add(assignment.key)
    merged = [
        replacements.get(index, line)
        for index, line in enumerate(lines)
        if index in replacements or index not in removed
    ]
    merged.extend(line for key, line in rendered.items() if key not in placed)
    return merged


def _rewrite(
    path: Path,
    *,
    managed: set[str],
    rendered: dict[str, str],
    export: bool,
    root: Path | None,
) -> bool:
    current = read_optional_editable_text(path, root=root)
    newline = newline_for(current or "")
    lines = _destination_lines(current) if current is not None else []
    assignments, problems = _scan_assignments(lines, export=export)
    _reject_ambiguous(path, managed, assignments, problems)
    new = _merged_lines(lines, assignments, managed, rendered)
    if current is not None and not any(line.strip() for line in new):
        path.unlink()
        return True
    text = newline.join(new) + (newline if new else "")
    if current == text:
        return False
    # An existing destination keeps the mode its owner chose; only a file
    # splashdown creates is made owner-only.
    atomic_write_text(
        path,
        text,
        root=root,
        create=True,
        mode=_CREATE_MODE if current is None else None,
    )
    return True


def write_envfile(
    path: Path,
    items: dict[str, str],
    *,
    drop: set[str] | None = None,
    root: Path | None,
) -> bool:
    """Write `items` into a dotenv destination, replacing splashdown's own keys and
    leaving every other line in place. `drop` names keys to remove without rewriting."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return _rewrite(
            path,
            managed=set(items) | (drop or set()),
            rendered={k: f"{k}={_env_quote(v)}" for k, v in items.items()},
            export=False,
            root=root,
        )
    except (OSError, ValueError) as error:
        raise ValueError(f"could not write envfile `{path}`: {error}") from error


def _shell_single_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def write_envrc(path: Path, items: dict[str, str], *, root: Path | None) -> bool:
    return _rewrite(
        path,
        managed=set(items),
        rendered={k: f"export {k}={_shell_single_quote(v)}" for k, v in items.items()},
        export=True,
        root=root,
    )


def _strip_destination(
    path: Path, keys: set[str], *, export: bool, root: Path | None
) -> tuple[str, frozenset[str]] | None:
    """`(action, keys actually removed)`, or `None` when the file carried none."""
    current = read_optional_editable_text(path, root=root)
    if current is None:
        return None
    newline = newline_for(current)
    lines = _destination_lines(current)
    assignments, problems = _scan_assignments(lines, export=export)
    if any(problem == _UNTERMINATED_QUOTE for _line, _key, problem in problems):
        # Below an unclosed quote the scanner matches inside the quoted region, so an
        # edit here could delete part of a value rather than an assignment.
        return "unparsed", frozenset()
    replaced = _replaced_lines(assignments, keys)
    if not replaced:
        return None
    cleared = frozenset(item.key for item in assignments if item.key in keys)
    kept = [line for index, line in enumerate(lines) if index not in replaced]
    if not any(line.strip() for line in kept):
        path.unlink()
        return "removed", cleared
    atomic_write_text(path, newline.join(kept) + newline, root=root)
    return "cleaned", cleared


class TeardownResult(NamedTuple):
    """What teardown changed, and the keys the registry still held that no
    destination this recipe declares carried, so the caller can say which values
    it could not chase instead of leaving them behind in silence."""

    changed: list[tuple[str, str]]
    uncleaned: frozenset[str]


def clear_writer_destinations(cwd: Path, recipe: Recipe, *, known_keys: set[str]) -> TeardownResult:
    """Remove splashdown's keys from every writer destination, the default one
    included: splashdown co-owns specific keys in these files rather than owning
    any of them wholesale. `known_keys` names what the registry still holds for
    this checkout, so a key the recipe stopped declaring goes from the default
    destination too; a resource whose writer delivers no file is never removed on
    that basis. Deletes a destination left with nothing else."""
    default_writer = resolve_writer(DEFAULT_WRITER, recipe.env_file)
    default_keys = (set(recipe.resources) | known_keys) - _undelivered_keys(recipe)
    groups: dict[str, set[str]] = {default_writer: default_keys}
    for name, spec in recipe.resources.items():
        writer = resolve_writer(spec.get("writer", DEFAULT_WRITER), recipe.env_file)
        if writer.startswith("envfile=") or writer == "envrc":
            groups.setdefault(writer, set()).add(name)

    changed: list[tuple[str, str]] = []
    cleared: set[str] = set()
    for writer, keys in groups.items():
        if writer == "envrc":
            relpath, export = ".envrc.local", True
        else:
            relpath = writer.removeprefix("envfile=")
            export = False
        target = cwd / relpath
        if not target.resolve().is_relative_to(cwd.resolve()):
            continue
        try:
            outcome = _strip_destination(target, keys, export=export, root=cwd)
        except (OSError, ValueError):
            continue
        if outcome is not None:
            changed.append((relpath, outcome[0]))
            cleared |= outcome[1]
    return TeardownResult(changed, frozenset(known_keys - _undelivered_keys(recipe) - cleared))


def _run_commands(
    cwd: Path,
    spec: CommandSpec,
    env: dict[str, str],
    *,
    label: str,
    extra_env: dict[str, str] | None = None,
) -> list[str]:
    proc_env = {**os.environ, **env, **(extra_env or {})}
    messages: list[str] = []
    for command in spec.commands:
        try:
            subprocess.run(command, shell=True, cwd=cwd, env=proc_env, check=True)  # noqa: S602 — runs user-authorized recipe commands by design
            messages.append(f"{label}: {command}")
        except subprocess.CalledProcessError as error:
            raise SetupError(f"{label} failed ({command}): exit {error.returncode}") from error
    return messages


def run_setup(
    cwd: Path,
    recipe: Recipe,
    preset: str | None,
    env: dict[str, str],
    *,
    extra_env: dict[str, str] | None = None,
) -> list[str]:
    if preset is None:
        return []
    if preset not in recipe.setup:
        available = ", ".join(sorted(recipe.setup)) or "(none)"
        raise ValueError(f"unknown setup `{preset}`; declared setups: {available}")
    spec = recipe.setup[preset]
    label = f"setup.{preset}"
    return _run_commands(
        cwd,
        spec,
        env,
        label=label,
        extra_env=extra_env,
    )


def run_bootstrap(
    cwd: Path,
    recipe: Recipe,
    env: dict[str, str],
    *,
    extra_env: dict[str, str],
) -> list[str]:
    if recipe.bootstrap is None:
        raise ValueError("recipe has no [bootstrap] section")
    return _run_commands(
        cwd,
        recipe.bootstrap,
        env,
        label="bootstrap",
        extra_env=extra_env,
    )
