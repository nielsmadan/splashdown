from __future__ import annotations

import ast
import hashlib
import operator
import os
import re
import subprocess
import tomllib
import uuid as uuid_mod
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, ClassVar, NoReturn, Self

from .catalog import PROFILES
from .constants import (
    ENV_NAME_RE,
    GLOBAL_CONFIG_NAME,
    LOCAL_NAME,
    RECIPE_NAME,
    TARGET_TYPES,
    TARGET_VARIANT_RE,
)
from .errors import DeviceError

_TEMPLATE_RE = re.compile(r"\{\{\s*(.*?)\s*\}\}")


class TemplateError(ValueError):
    pass


@dataclass(frozen=True)
class CommandSpec:
    commands: tuple[str, ...]


def _slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s or "x"


def _make_scope(cwd: Path, branch: str | None, resources: dict[str, str]) -> dict[str, Any]:
    cwd_abs = str(cwd.resolve())
    base = cwd.name
    parent = cwd.parent.name
    scope: dict[str, Any] = {
        "cwd": base,
        "cwd_abs": cwd_abs,
        "branch": branch or "",
        "repo": _repo_name(cwd),
        "parent": parent,
        "basename": lambda p: Path(p).name,
        "dirname": lambda p: str(Path(p).parent),
        "slug": _slug,
        "lower": str.lower,
        "upper": str.upper,
        "uuid": lambda: str(uuid_mod.uuid4()),
        "hash": lambda *xs: hashlib.sha256("|".join(map(str, xs)).encode()).hexdigest(),
        "port_hash": lambda *xs, lo=8000, hi=9000: (
            lo
            + (int(hashlib.sha256("|".join(map(str, xs)).encode()).hexdigest(), 16) % (hi - lo + 1))
        ),
        "truncate": lambda s, n: s[:n],
    }
    scope.update(resources)
    return scope


def _repo_name(cwd: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], cwd=cwd, stderr=subprocess.DEVNULL
        )
        return Path(out.decode().strip()).name
    except Exception:  # noqa: BLE001 — not a git repo / git absent: fall back to dir name
        return cwd.name


def _current_branch(cwd: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "symbolic-ref", "--short", "HEAD"], cwd=cwd, stderr=subprocess.DEVNULL
        )
        return out.decode().strip()
    except Exception:  # noqa: BLE001 — detached HEAD / git absent: no branch name
        return ""


# Recipes run automatically after checkout, so evaluate an allowlisted AST; eval() permits object-graph escapes even without builtins.
_BINOPS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_UNARYOPS: dict[type[ast.unaryop], Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

# Bound sequence repetition because untrusted checkout templates could otherwise exhaust memory during automatic provisioning.
_MAX_SEQ_REPEAT = 100_000


def _apply_binop(op_type: type[ast.operator], left: Any, right: Any, expr: str) -> Any:
    if op_type is ast.Mult:
        for seq, n in ((left, right), (right, left)):
            if isinstance(seq, (str, bytes, list, tuple)) and isinstance(n, int):
                if n > 0 and len(seq) * n > _MAX_SEQ_REPEAT:
                    raise TemplateError(
                        f"sequence repetition in `{expr}` exceeds {_MAX_SEQ_REPEAT} elements"
                    )
                break
    return _BINOPS[op_type](left, right)


def _eval_node(node: ast.AST, scope: dict[str, Any], expr: str) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in scope:
            raise TemplateError(f"unknown name `{node.id}`")
        return scope[node.id]
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left = _eval_node(node.left, scope, expr)
        right = _eval_node(node.right, scope, expr)
        return _apply_binop(type(node.op), left, right, expr)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARYOPS:
        return _UNARYOPS[type(node.op)](_eval_node(node.operand, scope, expr))
    if isinstance(node, ast.Call):
        func = _eval_node(node.func, scope, expr)
        if any(kw.arg is None for kw in node.keywords) or any(
            isinstance(a, ast.Starred) for a in node.args
        ):
            raise TemplateError(f"argument unpacking is not allowed in `{expr}`")
        if not callable(func):
            raise TemplateError(f"`{ast.unparse(node.func)}` is not callable")
        args = [_eval_node(a, scope, expr) for a in node.args]
        kwargs = {
            kw.arg: _eval_node(kw.value, scope, expr) for kw in node.keywords if kw.arg is not None
        }
        return func(*args, **kwargs)
    if isinstance(node, ast.Subscript):
        value = _eval_node(node.value, scope, expr)
        return value[_eval_subscript(node.slice, scope, expr)]
    raise TemplateError(f"disallowed expression element `{type(node).__name__}` in `{expr}`")


def _eval_subscript(node: ast.AST, scope: dict[str, Any], expr: str) -> Any:
    if isinstance(node, ast.Slice):
        lo = _eval_node(node.lower, scope, expr) if node.lower else None
        hi = _eval_node(node.upper, scope, expr) if node.upper else None
        step = _eval_node(node.step, scope, expr) if node.step else None
        return slice(lo, hi, step)
    return _eval_node(node, scope, expr)


def _safe_eval(expr: str, scope: dict[str, Any]) -> Any:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise TemplateError(f"invalid expression `{expr}`: {e}") from e
    return _eval_node(tree.body, scope, expr)


def render_template(tpl: str, scope: dict[str, Any]) -> str:
    """Render `{{ expr }}` placeholders. expr is a restricted expression subset."""

    def replace(m: re.Match[str]) -> str:
        expr = m.group(1)
        try:
            value = _safe_eval(expr, scope)
        except TemplateError:
            raise
        except Exception as e:
            raise TemplateError(f"failed to render `{{{{ {expr} }}}}`: {e}") from e
        if callable(value):
            raise TemplateError(
                f"template expression `{expr}` resolved to a callable; did you mean to call it?"
            )
        return str(value)

    return _TEMPLATE_RE.sub(replace, tpl)


def template_refs(tpl: str) -> set[str]:
    """Names referenced from a template's `{{ expr }}` placeholders (for topo sort).

    Collects `ast.Name` identifiers from each parsed expression, so a name that
    only appears inside a string literal (e.g. `{{ "PORT" }}`) is not treated as
    a dependency and cannot fabricate a false cycle in topo_sort.
    """
    refs: set[str] = set()
    for m in _TEMPLATE_RE.finditer(tpl):
        expr = m.group(1)
        try:
            tree = ast.parse(expr, mode="eval")
        except SyntaxError:
            # Malformed expression: fall back to a lenient identifier scan;
            # render_template surfaces the real error when the value is resolved.
            refs.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr))
            continue
        refs.update(node.id for node in ast.walk(tree) if isinstance(node, ast.Name))
    return refs


_WORKSPACES = ("single", "pnpm", "yarn", "npm", "cargo", "gradle")
_RECIPE_SECTIONS = {"project", "apps", "resources", "targets", "setup", "bootstrap"}
_CONFIG_SECTIONS = {"settings", "targets"}
_PROJECT_FIELDS = {"workspace", "loader", "framework", "run", "ios", "android", "worktree"}
_PROJECT_IOS_FIELDS = {"scheme", "mode", "configuration", "workspace", "project"}
_PROJECT_ANDROID_FIELDS = {
    "mode",
    "module",
    "variant",
    "application_id",
    "launch_activity",
}
_APP_FIELDS = {"path", "profile", "resources"}
_RESOURCE_TYPES = {"port", "template", "set", "uuid", "cwd", "cwd-slug"}
_RESOURCE_FIELDS = {
    "port": {"type", "range", "writer"},
    "template": {"type", "template", "writer"},
    "set": {"type", "default", "writer"},
    "uuid": {"type", "writer"},
    "cwd": {"type", "writer"},
    "cwd-slug": {"type", "writer"},
}
_TARGET_FIELDS = {
    "simulator": {"model", "ios", "name"},
    "emulator": {"device", "image", "name"},
    "device": {"id", "name", "platform"},
}
_TEMPLATE_NAMES = {
    "cwd",
    "cwd_abs",
    "branch",
    "repo",
    "parent",
    "basename",
    "dirname",
    "slug",
    "lower",
    "upper",
    "truncate",
    "uuid",
    "hash",
    "port_hash",
}
_WRITERS = {"splashdown-env", "envrc", "stdout", "none"}
_PORT_RANGE_LENGTH = 2
_MAX_PORT = 65535


def _schema_error(
    source: str,
    path: str,
    *,
    problem: str,
    expected: str,
) -> NoReturn:
    raise ValueError(f"{source}: [{path}] {problem}; expected {expected}")


def _source(path: Path, fallback: str) -> str:
    return path.name or fallback


def _load_toml(path: Path, fallback: str) -> dict[str, Any]:
    try:
        with path.open("rb") as file:
            return tomllib.load(file)
    except tomllib.TOMLDecodeError as error:
        _schema_error(
            _source(path, fallback),
            "document",
            problem=f"invalid TOML: {error}",
            expected="valid TOML",
        )


def _parse_toml(text: str, path: Path, fallback: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        _schema_error(
            _source(path, fallback),
            "document",
            problem=f"invalid TOML: {error}",
            expected="valid TOML",
        )


def _table(
    value: Any,
    *,
    source: str,
    path: str,
    expected: str = "a table",
) -> dict[str, Any]:
    if not isinstance(value, dict):
        _schema_error(
            source,
            path,
            problem=f"got {type(value).__name__}",
            expected=expected,
        )
    return value


def _allowed_keys(value: dict[str, Any], allowed: set[str], *, source: str, path: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        _schema_error(
            source,
            path,
            problem=f"unknown field `{unknown[0]}`",
            expected="only " + ", ".join(sorted(allowed)),
        )


def _non_empty_string(value: Any, *, source: str, path: str) -> str:
    if type(value) is not str or not value.strip():
        _schema_error(
            source,
            path,
            problem=f"got {type(value).__name__}",
            expected="a non-empty string",
        )
    return value


def _enum(value: Any, choices: set[str], *, source: str, path: str) -> str:
    parsed = _non_empty_string(value, source=source, path=path)
    if parsed not in choices:
        _schema_error(
            source,
            path,
            problem=f"unknown value `{parsed}`",
            expected="one of " + ", ".join(sorted(choices)),
        )
    return parsed


def _known_profiles() -> set[str]:
    return set(PROFILES)


def _known_loaders() -> set[str]:
    from .loaders import LOADERS  # noqa: PLC0415

    return set(LOADERS)


def _validate_project(data: dict[str, Any], *, source: str) -> dict[str, Any]:
    raw = data.get("project", {})
    project = _table(raw, source=source, path="project")
    _allowed_keys(project, _PROJECT_FIELDS, source=source, path="project")
    if "workspace" in project:
        _enum(
            project["workspace"],
            set(_WORKSPACES),
            source=source,
            path="project.workspace",
        )
    if "loader" in project:
        _enum(project["loader"], _known_loaders(), source=source, path="project.loader")
    if "framework" in project:
        _enum(
            project["framework"],
            _known_profiles() | {"auto"},
            source=source,
            path="project.framework",
        )
    if "run" in project:
        run = project["run"]
        if isinstance(run, dict):
            _allowed_keys(run, {"ios", "android"}, source=source, path="project.run")
            if not run:
                _schema_error(
                    source,
                    "project.run",
                    problem="table is empty",
                    expected="one or both of `ios` and `android`",
                )
            for platform, command in run.items():
                _non_empty_string(command, source=source, path=f"project.run.{platform}")
        else:
            _non_empty_string(run, source=source, path="project.run")
    if "worktree" in project:
        shape = 'exactly `claim_device = "ios" | "android" | "any"`'
        worktree = _table(
            project["worktree"],
            source=source,
            path="project.worktree",
            expected=f"a table containing {shape}",
        )
        if set(worktree) != {"claim_device"}:
            unknown = sorted(set(worktree) - {"claim_device"})
            problem = (
                f"unknown field `{unknown[0]}`"
                if unknown
                else "missing required field `claim_device`"
            )
            _schema_error(source, "project.worktree", problem=problem, expected=shape)
        _enum(
            worktree["claim_device"],
            {"ios", "android", "any"},
            source=source,
            path="project.worktree.claim_device",
        )
    for key, allowed in (("ios", _PROJECT_IOS_FIELDS), ("android", _PROJECT_ANDROID_FIELDS)):
        if key not in project:
            continue
        nested = _table(project[key], source=source, path=f"project.{key}")
        _allowed_keys(nested, allowed, source=source, path=f"project.{key}")
        for field, value in nested.items():
            _non_empty_string(value, source=source, path=f"project.{key}.{field}")
    return dict(project)


def _validate_writer(value: Any, *, source: str, path: str, base_dir: Path) -> str:
    writer = _non_empty_string(value, source=source, path=path)
    if writer in _WRITERS:
        return writer
    if not writer.startswith("envfile="):
        _schema_error(
            source,
            path,
            problem=f"unknown writer `{writer}`",
            expected="splashdown-env, envrc, stdout, none, or envfile=RELATIVE_PATH",
        )
    path_arg = writer.removeprefix("envfile=")
    candidate = Path(path_arg)
    if (
        not path_arg
        or candidate.is_absolute()
        or PureWindowsPath(path_arg).is_absolute()
        or candidate == Path(".")
        or ".." in candidate.parts
        or not (base_dir / candidate).resolve().is_relative_to(base_dir.resolve())
    ):
        _schema_error(
            source,
            path,
            problem=f"invalid envfile path `{path_arg}`",
            expected="a non-empty relative path that stays inside the checkout",
        )
    return writer


def _validate_template_node(
    node: ast.AST, names: set[str], *, source: str, path: str, expr: str
) -> None:
    if isinstance(node, ast.Constant):
        return
    if isinstance(node, ast.Name):
        if node.id not in names:
            _schema_error(
                source,
                path,
                problem=f"unknown template name `{node.id}`",
                expected="a context name, helper, or declared resource",
            )
        return
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        _validate_template_node(node.left, names, source=source, path=path, expr=expr)
        _validate_template_node(node.right, names, source=source, path=path, expr=expr)
        return
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARYOPS:
        _validate_template_node(node.operand, names, source=source, path=path, expr=expr)
        return
    if isinstance(node, ast.Call):
        if any(kw.arg is None for kw in node.keywords) or any(
            isinstance(arg, ast.Starred) for arg in node.args
        ):
            _schema_error(
                source,
                path,
                problem="template uses argument unpacking",
                expected="explicit arguments",
            )
        _validate_template_node(node.func, names, source=source, path=path, expr=expr)
        for arg in node.args:
            _validate_template_node(arg, names, source=source, path=path, expr=expr)
        for keyword in node.keywords:
            _validate_template_node(keyword.value, names, source=source, path=path, expr=expr)
        return
    if isinstance(node, ast.Subscript):
        _validate_template_node(node.value, names, source=source, path=path, expr=expr)
        _validate_template_node(node.slice, names, source=source, path=path, expr=expr)
        return
    if isinstance(node, ast.Slice):
        for bound in (node.lower, node.upper, node.step):
            if bound is not None:
                _validate_template_node(bound, names, source=source, path=path, expr=expr)
        return
    _schema_error(
        source,
        path,
        problem=f"disallowed template syntax `{type(node).__name__}` in `{expr}`",
        expected="the documented restricted expression syntax",
    )


def _validate_template(template: str, resource_names: set[str], *, source: str, path: str) -> None:
    names = _TEMPLATE_NAMES | resource_names
    matches = list(_TEMPLATE_RE.finditer(template))
    remainder = _TEMPLATE_RE.sub("", template)
    if "{{" in remainder or "}}" in remainder:
        _schema_error(
            source,
            path,
            problem="unmatched template delimiter",
            expected="balanced `{{ expression }}` placeholders",
        )
    for match in matches:
        expr = match.group(1)
        try:
            tree = ast.parse(expr, mode="eval")
        except SyntaxError:
            _schema_error(
                source,
                path,
                problem=f"invalid template expression `{expr}`",
                expected="valid restricted expression syntax",
            )
        _validate_template_node(tree.body, names, source=source, path=path, expr=expr)


def _validate_template_cycles(resources: dict[str, dict[str, Any]], *, source: str) -> None:
    names = set(resources)
    deps = {
        name: template_refs(spec.get("template", "")) & names
        for name, spec in resources.items()
        if spec["type"] == "template"
    }
    seen: set[str] = set()
    active: set[str] = set()

    def visit(name: str) -> None:
        if name in seen:
            return
        if name in active:
            _schema_error(
                source,
                f"resources.{name}.template",
                problem=f"dependency cycle involving `{name}`",
                expected="acyclic resource references",
            )
        active.add(name)
        for dependency in deps.get(name, set()):
            visit(dependency)
        active.remove(name)
        seen.add(name)

    for name in resources:
        visit(name)


def _validate_resources(
    data: dict[str, Any], *, source: str, base_dir: Path
) -> dict[str, dict[str, Any]]:
    raw = _table(data.get("resources", {}), source=source, path="resources")
    resources: dict[str, dict[str, Any]] = {}
    resource_names = set(raw)
    for name, raw_spec in raw.items():
        path = f"resources.{name}"
        if not ENV_NAME_RE.match(name):
            _schema_error(
                source,
                path,
                problem=f"invalid resource name `{name}`",
                expected="an environment identifier matching [A-Za-z_][A-Za-z0-9_]*",
            )
        spec = _table(raw_spec, source=source, path=path)
        if "type" not in spec:
            _schema_error(
                source,
                path,
                problem="missing field `type`",
                expected="a resource type",
            )
        resource_type = _enum(spec["type"], _RESOURCE_TYPES, source=source, path=f"{path}.type")
        _allowed_keys(spec, _RESOURCE_FIELDS[resource_type], source=source, path=path)
        if "writer" in spec:
            _validate_writer(
                spec["writer"],
                source=source,
                path=f"{path}.writer",
                base_dir=base_dir,
            )
        if resource_type == "port":
            if "range" not in spec:
                _schema_error(
                    source,
                    path,
                    problem="missing field `range`",
                    expected="range = [LO, HI]",
                )
            port_range = spec["range"]
            if (
                not isinstance(port_range, list)
                or len(port_range) != _PORT_RANGE_LENGTH
                or any(type(value) is not int for value in port_range)
            ):
                _schema_error(
                    source,
                    f"{path}.range",
                    problem=f"got {port_range!r}",
                    expected="exactly two integers [LO, HI]",
                )
            lo, hi = port_range
            if not (1 <= lo <= hi <= _MAX_PORT):
                _schema_error(
                    source,
                    f"{path}.range",
                    problem=f"got [{lo}, {hi}]",
                    expected="1 <= LO <= HI <= 65535",
                )
        elif resource_type == "template":
            if "template" not in spec:
                _schema_error(
                    source,
                    path,
                    problem="missing field `template`",
                    expected="a string template",
                )
            if type(spec["template"]) is not str:
                _schema_error(
                    source,
                    f"{path}.template",
                    problem=f"got {type(spec['template']).__name__}",
                    expected="a string",
                )
        elif resource_type == "set" and "default" in spec and type(spec["default"]) is not str:
            _schema_error(
                source,
                f"{path}.default",
                problem=f"got {type(spec['default']).__name__}",
                expected="a string",
            )
        resources[name] = dict(spec)
    for name, spec in resources.items():
        if spec["type"] == "template":
            _validate_template(
                spec["template"],
                resource_names,
                source=source,
                path=f"resources.{name}.template",
            )
    _validate_template_cycles(resources, source=source)
    return resources


def _validate_apps(
    data: dict[str, Any],
    resources: dict[str, dict[str, Any]],
    *,
    source: str,
) -> dict[str, dict[str, Any]]:
    raw = _table(data.get("apps", {}), source=source, path="apps")
    profiles = _known_profiles() | {"unknown"}
    apps: dict[str, dict[str, Any]] = {}
    for name, raw_spec in raw.items():
        path = f"apps.{name}"
        spec = _table(raw_spec, source=source, path=path)
        _allowed_keys(spec, _APP_FIELDS, source=source, path=path)
        for required in sorted(_APP_FIELDS):
            if required not in spec:
                _schema_error(
                    source,
                    path,
                    problem=f"missing field `{required}`",
                    expected="path, profile, resources",
                )
        _non_empty_string(spec["path"], source=source, path=f"{path}.path")
        _enum(spec["profile"], profiles, source=source, path=f"{path}.profile")
        refs = spec["resources"]
        if not isinstance(refs, list) or any(type(ref) is not str for ref in refs):
            _schema_error(
                source,
                f"{path}.resources",
                problem=f"got {type(refs).__name__}",
                expected="an array of resource-name strings",
            )
        if len(refs) != len(set(refs)):
            _schema_error(
                source,
                f"{path}.resources",
                problem="contains duplicate resource names",
                expected="a unique list",
            )
        for ref in refs:
            if not ENV_NAME_RE.match(ref):
                _schema_error(
                    source,
                    f"{path}.resources",
                    problem=f"invalid resource name `{ref}`",
                    expected="environment identifiers",
                )
            if ref not in resources:
                _schema_error(
                    source,
                    f"{path}.resources",
                    problem=f"references undeclared resource `{ref}`",
                    expected="names declared under [resources]",
                )
        apps[name] = dict(spec)
    return apps


def _validate_command_table(
    raw_spec: Any,
    *,
    source: str,
    path: str,
) -> CommandSpec:
    spec = _table(raw_spec, source=source, path=path)
    _allowed_keys(spec, {"run"}, source=source, path=path)
    if "run" not in spec:
        _schema_error(
            source,
            path,
            problem="missing field `run`",
            expected="a non-empty string or non-empty array of strings",
        )
    commands = spec["run"]
    if isinstance(commands, str):
        _non_empty_string(commands, source=source, path=f"{path}.run")
        normalized = (commands,)
    elif isinstance(commands, list) and commands:
        for index, command in enumerate(commands):
            _non_empty_string(command, source=source, path=f"{path}.run.{index}")
        normalized = tuple(commands)
    else:
        _schema_error(
            source,
            f"{path}.run",
            problem=f"got {type(commands).__name__}",
            expected="a non-empty string or non-empty array of strings",
        )
    return CommandSpec(normalized)


def _validate_setup(data: dict[str, Any], *, source: str) -> dict[str, CommandSpec]:
    raw = _table(data.get("setup", {}), source=source, path="setup")
    return {
        name: _validate_command_table(spec, source=source, path=f"setup.{name}")
        for name, spec in raw.items()
    }


def _validate_bootstrap(data: dict[str, Any], *, source: str) -> CommandSpec | None:
    if "bootstrap" not in data:
        return None
    return _validate_command_table(data["bootstrap"], source=source, path="bootstrap")


def validate_target_spec(
    dtype: str,
    spec: dict[str, Any],
    *,
    source: str,
    path: str,
) -> dict[str, str]:
    if dtype not in TARGET_TYPES:
        _schema_error(
            source,
            path,
            problem=f"unknown target type `{dtype}`",
            expected="one of " + ", ".join(TARGET_TYPES),
        )
    filtered = {key: value for key, value in spec.items() if value is not None}
    _allowed_keys(filtered, _TARGET_FIELDS[dtype], source=source, path=path)
    for field, value in filtered.items():
        if field == "platform":
            _enum(value, {"ios", "android"}, source=source, path=f"{path}.{field}")
        else:
            _non_empty_string(value, source=source, path=f"{path}.{field}")
    return filtered


def _reject_legacy_devices(data: dict[str, Any], *, source: str) -> None:
    if "devices" in data:
        _schema_error(
            source,
            "devices",
            problem="`[devices.*]` was renamed to `[targets.*]`",
            expected="[targets.simulator|emulator|device.VARIANT]",
        )


def _parse_targets_section(
    data: dict[str, Any], *, source: str
) -> dict[str, dict[str, dict[str, Any]]]:
    """Parse [targets.<type>.<variant>] tables (type ∈ simulator/emulator/device)."""
    raw = _table(data.get("targets", {}), source=source, path="targets")
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for type_key, raw_type in raw.items():
        if type_key not in TARGET_TYPES:
            _schema_error(
                source,
                f"targets.{type_key}",
                problem=f"unknown target type `{type_key}`",
                expected="one of " + ", ".join(TARGET_TYPES),
            )
        type_table = _table(raw_type, source=source, path=f"targets.{type_key}")
        variants: dict[str, dict[str, Any]] = {}
        for variant_name, raw_spec in type_table.items():
            if not TARGET_VARIANT_RE.match(variant_name):
                _schema_error(
                    source,
                    f"targets.{type_key}.{variant_name}",
                    problem=f"invalid variant name `{variant_name}`",
                    expected="a name matching [A-Za-z][A-Za-z0-9_-]*",
                )
            spec = _table(
                raw_spec,
                source=source,
                path=f"targets.{type_key}.{variant_name}",
            )
            variants[variant_name] = validate_target_spec(
                type_key,
                spec,
                source=source,
                path=f"targets.{type_key}.{variant_name}",
            )
        out[type_key] = variants
    return out


class Recipe:
    def __init__(self, data: dict[str, Any], path: Path):
        self.path = path
        source = _source(path, RECIPE_NAME)
        _reject_legacy_devices(data, source=source)
        _allowed_keys(data, _RECIPE_SECTIONS, source=source, path="document")
        self.resources = _validate_resources(data, source=source, base_dir=path.parent)
        self.setup = _validate_setup(data, source=source)
        self.bootstrap = _validate_bootstrap(data, source=source)
        self.project = _validate_project(data, source=source)
        self.apps = _validate_apps(data, self.resources, source=source)
        self.targets: dict[str, dict[str, dict[str, Any]]] = _parse_targets_section(
            data,
            source=source,
        )

    @classmethod
    def load(cls, path: Path) -> Recipe:
        return cls(_load_toml(path, RECIPE_NAME), path)

    @classmethod
    def parse(cls, text: str, path: Path) -> Recipe:
        return cls(_parse_toml(text, path, RECIPE_NAME), path)


LOCAL_SKELETON = """\
# splashdown.local.toml — additional, per-checkout target variants.
# Gitignored. Each checkout has its own copy.
#
# Recipe-declared variants don't go here; use this only to ADD variants on top
# of what the recipe exposes (no overrides — pick a distinct variant name).
#
# Example: a one-off iPhone 16 sim to reproduce a bug only this checkout sees:
#
# [targets.simulator.repro-bug]
# model = "iPhone 16"
# ios   = "17.5"
#
# Or, equivalently, via CLI:
#
#   splash target add simulator repro-bug --model="iPhone 16" --ios=17.5
#
# Per-checkout settings (optional) — overrides the global config
# (~/.config/splashdown/config.toml):
#
# [settings]
# prefix_match = false
"""


class _TargetConfig:
    _source_name: ClassVar[str]

    def __init__(self, data: dict[str, Any], path: Path):
        self.path = path
        source = _source(path, self._source_name)
        _reject_legacy_devices(data, source=source)
        _allowed_keys(data, _CONFIG_SECTIONS, source=source, path="document")
        self.settings = _parse_settings(data, source=source)
        self.targets: dict[str, dict[str, dict[str, Any]]] = _parse_targets_section(
            data,
            source=source,
        )

    @classmethod
    def load(cls, path: Path) -> Self:
        if not path.exists():
            return cls({}, path)
        return cls(_load_toml(path, cls._source_name), path)

    @classmethod
    def parse(cls, text: str, path: Path) -> Self:
        return cls(_parse_toml(text, path, cls._source_name), path)


class LocalConfig(_TargetConfig):
    """Per-checkout local config from splashdown.local.toml. Holds additional
    [targets.<type>.<variant>] variants, alongside (not replacing) the recipe's."""

    _source_name = LOCAL_NAME


GLOBAL_SKELETON = """\
# splashdown config.toml — machine-wide config (~/.config/splashdown/config.toml).
# Shared across every project on this machine.
#
# [targets.*] variants here are available in every checkout without re-declaring
# them per repo:
#   - physical `device` targets show up everywhere (they match connected hardware);
#   - `simulator`/`emulator` variants only surface in projects that already declare
#     that target type.
# A project's own recipe/local variant of the same name always wins.
#
# Example: your usual test phones, available in every project.
#
# [targets.device.my-iphone]
# platform = "ios"
# name     = "Niels's iPhone"
#
# [targets.device.my-pixel]
# platform = "android"
#
# Or via CLI:
#
#   splash target add device my-iphone --platform=ios --name="Niels's iPhone" --global
#
# Machine-wide settings (optional):
#
# [settings]
# prefix_match = false
"""


class GlobalConfig(_TargetConfig):
    """Machine-wide config from ~/.config/splashdown/config.toml. Holds
    [targets.<type>.<variant>] variants shared across every project (the
    [settings] table in the same file is read separately by load_settings)."""

    _source_name = GLOBAL_CONFIG_NAME


def _global_config_path() -> Path:
    """Path to the machine-wide config.toml. Resolved from XDG_CONFIG_HOME at call
    time (like Registry) so tests can monkeypatch the env."""
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config_home / "splashdown" / GLOBAL_CONFIG_NAME


@dataclass
class Settings:
    """Resolved splashdown settings. Precedence (highest first): per-checkout
    `[settings]` in splashdown.local.toml, then the global config, then defaults."""

    prefix_match: bool = True


_SETTINGS_SCHEMA: dict[str, type] = {"prefix_match": bool}


def _parse_settings(data: dict[str, Any], source: str) -> dict[str, Any]:
    """Read and strictly validate a `[settings]` table. Unknown keys and wrong
    value types are errors (a silently-ignored typo'd toggle is worse than loud)."""
    raw = _table(data.get("settings", {}), source=source, path="settings")
    parsed: dict[str, Any] = {}
    for key, value in raw.items():
        expected = _SETTINGS_SCHEMA.get(key)
        if expected is None:
            _schema_error(
                source,
                "settings",
                problem=f"unknown field `{key}`",
                expected="only " + ", ".join(sorted(_SETTINGS_SCHEMA)),
            )
        # bool is a subclass of int, so a plain isinstance check would let `1` pass
        # for a bool key and `true` pass for a (future) int key. The second clause
        # rejects a bool wherever a non-bool type is expected.
        if not isinstance(value, expected) or (expected is not bool and isinstance(value, bool)):
            _schema_error(
                source,
                f"settings.{key}",
                problem=f"got {type(value).__name__}",
                expected=expected.__name__,
            )
        parsed[key] = value
    return parsed


def load_settings(cwd: Path) -> Settings:
    """Merge global config + per-checkout `[settings]` into a `Settings`. The global
    path is resolved from XDG_CONFIG_HOME at call time (like Registry) so tests can
    monkeypatch the env. Both files are read with stdlib tomllib — the read path
    stays dependency-free."""
    merged: dict[str, Any] = {}
    for path in (_global_config_path(), cwd / LOCAL_NAME):
        if not path.exists():
            continue
        config = (
            GlobalConfig.load(path) if path.name == GLOBAL_CONFIG_NAME else LocalConfig.load(path)
        )
        merged.update(config.settings)
    # _parse_settings only ever returns validated, whitelisted keys, so they map
    # 1:1 onto Settings fields; absent keys fall back to the dataclass defaults.
    return Settings(**merged)


def merged_targets(
    recipe: Recipe,
    local: LocalConfig,
    global_config: GlobalConfig | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Union the recipe + local + global target catalogs.

    recipe vs local: a (type, variant) collision between the two is an error —
    pick a different name in local.

    Global variants (from ~/.config/splashdown/config.toml) then fold in on top:
    - `device` (physical) targets are added to *every* project — they create
      nothing and just match connected hardware.
    - `simulator`/`emulator` targets only surface for types the project already
      declares; a global sim never conjures device support into a backend repo.
    On a name collision the project's own variant wins *silently* — a shared
    global file must never break unrelated repos on a coincidental clash (the
    winning variant's `source` surfaces the shadow; see _target_source)."""
    merged: dict[str, dict[str, dict[str, Any]]] = {
        type_key: dict(variants) for type_key, variants in recipe.targets.items()
    }
    for type_key, variants in local.targets.items():
        bucket = merged.setdefault(type_key, {})
        for variant_name, spec in variants.items():
            if variant_name in bucket:
                raise ValueError(
                    f"target `{type_key}.{variant_name}` already exists in recipe; "
                    f"pick a different name in {LOCAL_NAME}"
                )
            bucket[variant_name] = spec
    if global_config is not None:
        for type_key, variants in global_config.targets.items():
            if type_key != "device" and type_key not in merged:
                continue
            bucket = merged.setdefault(type_key, {})
            for variant_name, spec in variants.items():
                bucket.setdefault(variant_name, spec)
    return merged


def resolve_variant(
    catalog: dict[str, dict[str, Any]], requested: str | None, prefix_match: bool = False
) -> tuple[str, dict[str, Any]]:
    """Pick a variant from a single type's catalog. Rules:
    - explicit name wins (exact, or a unique prefix when `prefix_match`)
    - else `default` if declared
    - else the only variant if exactly one is declared
    - else error
    """
    if not catalog:
        raise DeviceError("no variants declared for this type")
    if requested is not None:
        if requested in catalog:
            return requested, catalog[requested]
        # `requested` must be truthy: "".startswith() matches every variant, which
        # would spuriously report "ambiguous" (or silently pick the sole variant).
        if prefix_match and requested:
            matches = [name for name in catalog if name.startswith(requested)]
            if len(matches) == 1:
                return matches[0], catalog[matches[0]]
            if len(matches) > 1:
                raise DeviceError(
                    f"ambiguous variant `{requested}`; matches: {', '.join(sorted(matches))}"
                )
        raise DeviceError(f"no variant `{requested}`; declared: {', '.join(sorted(catalog))}")
    if "default" in catalog:
        return "default", catalog["default"]
    if len(catalog) == 1:
        only = next(iter(catalog))
        return only, catalog[only]
    raise DeviceError(
        f"no `default` variant; pass a variant explicitly (declared: {', '.join(sorted(catalog))})"
    )


def topo_sort(recipe: Recipe) -> list[str]:
    """Sort resources so referents come before referrers."""
    names = list(recipe.resources.keys())
    name_set = set(names)
    deps: dict[str, set[str]] = {n: set() for n in names}
    for n, spec in recipe.resources.items():
        tpl = spec.get("template")
        if isinstance(tpl, str):
            deps[n] = {r for r in template_refs(tpl) if r in name_set}

    out: list[str] = []
    seen: set[str] = set()
    temp: set[str] = set()

    def visit(node: str) -> None:
        if node in seen:
            return
        if node in temp:
            raise ValueError(f"cycle in template references involving `{node}`")
        temp.add(node)
        for d in deps[node]:
            visit(d)
        temp.remove(node)
        seen.add(node)
        out.append(node)

    for n in names:
        visit(n)
    return out


_ENV_SAFE_RE = re.compile(r"[A-Za-z0-9_./:@%+=,-]+")


def _env_quote(value: str) -> str:
    """Single-quote unsafe values because devbox and no-loader paths source the file as shell code."""
    if value and _ENV_SAFE_RE.fullmatch(value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"
