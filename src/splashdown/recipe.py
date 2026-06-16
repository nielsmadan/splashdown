from __future__ import annotations

import ast
import hashlib
import operator
import re
import subprocess
import tomllib
import uuid as uuid_mod
from pathlib import Path
from typing import Any

from . import (
    ENV_NAME_RE,
    LOCAL_NAME,
    RECIPE_NAME,
    TARGET_TYPES,
    TARGET_VARIANT_RE,
)

# ---------- template engine ----------

_TEMPLATE_RE = re.compile(r"\{\{\s*(.*?)\s*\}\}")


class TemplateError(ValueError):
    pass


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


# Restricted expression evaluator for `{{ ... }}` templates. We deliberately do
# NOT use eval(): an empty-`__builtins__` eval is not a real sandbox (object-graph
# walks like `().__class__.__base__.__subclasses__()` reach `os`/`subprocess`), and
# recipes run automatically from the post-checkout hook on untrusted checkouts.
# This walks the AST and permits only literals, names bound in `scope`, calls to
# scope-provided helpers, indexing/slicing, and arithmetic — and forbids attribute
# access entirely, which is the escape hatch every eval-sandbox break relies on.
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
        return _BINOPS[type(node.op)](left, right)
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
    """Identifier-shaped names referenced from a template (for topo sort)."""
    refs: set[str] = set()
    for m in _TEMPLATE_RE.finditer(tpl):
        for ident in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", m.group(1)):
            refs.add(ident)
    return refs


# ---------- recipe ----------


def _parse_targets_section(
    data: dict[str, Any], *, source: str
) -> dict[str, dict[str, dict[str, Any]]]:
    """Parse [targets.<type>.<variant>] tables (type ∈ simulator/emulator/device)."""
    if "devices" in data and "targets" not in data:
        raise ValueError(
            f"{source}: `[devices.*]` was renamed to `[targets.*]`; rename the table "
            "(target types are simulator/emulator/device)"
        )
    raw = data.get("targets", {}) or {}
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for type_key, type_val in raw.items():
        if type_key not in TARGET_TYPES:
            raise ValueError(
                f"{source}: unknown target type `{type_key}` (known: {', '.join(TARGET_TYPES)})"
            )
        if not isinstance(type_val, dict):
            raise ValueError(f"{source}: [targets.{type_key}] must be a table of variants")
        variants: dict[str, dict[str, Any]] = {}
        for variant_name, spec in type_val.items():
            if not TARGET_VARIANT_RE.match(variant_name):
                raise ValueError(
                    f"{source}: variant name `{variant_name}` must match [A-Za-z][A-Za-z0-9_-]*"
                )
            if not isinstance(spec, dict):
                raise ValueError(f"{source}: [targets.{type_key}.{variant_name}] must be a table")
            variants[variant_name] = dict(spec)
        out[type_key] = variants
    return out


class Recipe:
    def __init__(self, data: dict[str, Any], path: Path):
        self.path = path
        self.resources: dict[str, dict[str, Any]] = dict(data.get("resources", {}) or {})
        self.setup: dict[str, dict[str, Any]] = dict(data.get("setup", {}) or {})
        self.project: dict[str, Any] = dict(data.get("project", {}) or {})
        self.targets: dict[str, dict[str, dict[str, Any]]] = _parse_targets_section(
            data,
            source=path.name or RECIPE_NAME,
        )
        for name in self.resources:
            if not ENV_NAME_RE.match(name):
                raise ValueError(f"resource name `{name}` is not a valid env var identifier")

    @classmethod
    def load(cls, path: Path) -> Recipe:
        with path.open("rb") as f:
            data = tomllib.load(f)
        return cls(data, path)


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
"""


class LocalConfig:
    """Per-checkout local config from splashdown.local.toml. Holds additional
    [targets.<type>.<variant>] variants, alongside (not replacing) the recipe's."""

    def __init__(self, data: dict[str, Any], path: Path):
        self.path = path
        self.targets: dict[str, dict[str, dict[str, Any]]] = _parse_targets_section(
            data,
            source=path.name or LOCAL_NAME,
        )

    @classmethod
    def load(cls, path: Path) -> LocalConfig:
        if not path.exists():
            return cls({}, path)
        with path.open("rb") as f:
            data = tomllib.load(f)
        return cls(data, path)


def merged_targets(recipe: Recipe, local: LocalConfig) -> dict[str, dict[str, dict[str, Any]]]:
    """Union recipe + local target catalogs. (type, variant) name collisions
    between the two files are an error — pick a different name in local."""
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
    return merged


def resolve_variant(
    catalog: dict[str, dict[str, Any]], requested: str | None
) -> tuple[str, dict[str, Any]]:
    """Pick a variant from a single type's catalog. Rules:
    - explicit name wins
    - else `default` if declared
    - else the only variant if exactly one is declared
    - else error
    """
    # Lazy import to avoid circular — devices.py imports recipe.py
    from .devices import DeviceError  # noqa: PLC0415

    if not catalog:
        raise DeviceError("no variants declared for this type")
    if requested is not None:
        if requested not in catalog:
            raise DeviceError(f"no variant `{requested}`; declared: {', '.join(sorted(catalog))}")
        return requested, catalog[requested]
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
            deps[n] = {r for r in template_refs(tpl) if r in name_set and r != n}

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


# ---------- writers ----------


def _find_table(lines: list[str], name: str) -> tuple[int | None, int]:
    """Find a `[name]` table. Returns (header_index, end_index_exclusive)."""
    start: int | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == f"[{name}]":
            start = i
            break
    if start is None:
        return None, len(lines)
    end = len(lines)
    for j in range(start + 1, len(lines)):
        s = lines[j].strip()
        if s.startswith("[") and s.endswith("]"):
            end = j
            break
    return start, end


def _toml_quote(value: str) -> str:
    # Always emit basic strings; escape minimally.
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


_ENV_SAFE_RE = re.compile(r"[A-Za-z0-9_./:@%+=,-]+")


def _env_quote(value: str) -> str:
    """Quote a dotenv value when it isn't bare-safe. Uses SINGLE quotes: this file
    is `source`d by a shell in two paths (devbox's init_hook and the no-loader
    `set -a; source` fallback), where double-quoted `$(...)`/backticks would
    EXECUTE. Single quotes neutralize them, and mise/direnv read single-quoted
    dotenv values literally too. Matches `write_envrc`'s shell-quoting."""
    if value and _ENV_SAFE_RE.fullmatch(value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"
