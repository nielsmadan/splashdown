#!/usr/bin/env python3
"""splashdown — per-checkout resource and device provisioner.

Reads `splashdown.toml` (committed schema), allocates ports / generates uuids /
expands templates, and writes resolved values to `splashdown.env`. Per-checkout
device config lives in `splashdown.local.toml`. Maintains a machine-local
registry so concurrent checkouts don't collide.

Stdlib-only. Python 3.11+ (uses tomllib).
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import plistlib
import re
import socket
import subprocess
import sys
import tomllib
import uuid as uuid_mod
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, NamedTuple


# ---------- paths & constants ----------

__version__ = "0.8.0"  # keep in sync with pyproject.toml

STATE_HOME = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
REGISTRY_DIR = STATE_HOME / "splashdown"
PORT_REGISTRY = REGISTRY_DIR / "ports.tsv"
KV_REGISTRY = REGISTRY_DIR / "kv.tsv"
DEVICE_REGISTRY = REGISTRY_DIR / "devices.tsv"

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEVICE_VARIANT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
DEVICE_TYPES = ("simulator", "emulator")
RECIPE_NAME = "splashdown.toml"
LOCAL_NAME = "splashdown.local.toml"
ENV_FILE_NAME = "splashdown.env"


# ---------- registry ----------

class DeviceRow(NamedTuple):
    checkout: str
    dtype: str
    variant: str
    udid: str
    model: str
    ios: str
    created_at: str


class Registry:
    """Machine-local registry. TSV files protected by flock.

    ports.tsv:    port\tabspath\tkey
    kv.tsv:       abspath\tkey\tvalue
    devices.tsv:  abspath\tdtype\tvariant\tudid\tmodel\tios\tcreated_at
    """

    def __init__(
        self,
        port_file: Path | None = None,
        kv_file: Path | None = None,
        device_file: Path | None = None,
    ):
        # Resolve defaults at instantiation time (not import time) so tests can
        # monkeypatch.setenv("XDG_STATE_HOME", ...) and have it take effect.
        state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
        registry_dir = state_home / "splashdown"
        self.port_file = port_file or (registry_dir / "ports.tsv")
        self.kv_file = kv_file or (registry_dir / "kv.tsv")
        self.device_file = device_file or (registry_dir / "devices.tsv")
        self.port_file.parent.mkdir(parents=True, exist_ok=True)
        self.port_file.touch(exist_ok=True)
        self.kv_file.touch(exist_ok=True)
        self.device_file.touch(exist_ok=True)

    @contextmanager
    def _lock(self, path: Path):
        # Lock a separate file so we can read/write the registry under the same fd.
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.touch(exist_ok=True)
        fd = os.open(str(lock_path), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    # --- ports ---

    def _read_ports(self) -> list[tuple[int, str, str]]:
        out: list[tuple[int, str, str]] = []
        for line in self.port_file.read_text().splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            try:
                port = int(parts[0])
            except ValueError:
                continue
            out.append((port, parts[1], parts[2]))
        return out

    def _write_ports(self, rows: Iterable[tuple[int, str, str]]) -> None:
        lines = [f"{p}\t{path}\t{key}" for (p, path, key) in rows]
        self.port_file.write_text("\n".join(lines) + ("\n" if lines else ""))

    def get_port(self, abspath: str, key: str) -> int | None:
        for port, path, k in self._read_ports():
            if path == abspath and k == key:
                return port
        return None

    def busy_ports(self, *, gc: bool = True) -> set[int]:
        rows = self._read_ports()
        live = set()
        kept: list[tuple[int, str, str]] = []
        for port, path, key in rows:
            if gc and not Path(path).exists():
                continue
            live.add(port)
            kept.append((port, path, key))
        if gc and len(kept) != len(rows):
            self._write_ports(kept)
        return live

    def allocate_port(self, abspath: str, key: str, lo: int, hi: int) -> int:
        with self._lock(self.port_file):
            existing = self.get_port(abspath, key)
            if existing is not None and lo <= existing <= hi:
                if not _port_in_use(existing):
                    return existing
                # Someone else grabbed it — fall through and reallocate.
                self._remove_port(abspath, key)
            busy = self.busy_ports(gc=True)
            for candidate in range(lo, hi + 1):
                if candidate in busy:
                    continue
                if _port_in_use(candidate):
                    continue
                self._append_port(candidate, abspath, key)
                return candidate
            raise RuntimeError(f"no free port in range {lo}-{hi} for {key}")

    def _append_port(self, port: int, abspath: str, key: str) -> None:
        rows = self._read_ports()
        rows.append((port, abspath, key))
        self._write_ports(rows)

    def _remove_port(self, abspath: str, key: str) -> None:
        rows = [r for r in self._read_ports() if not (r[1] == abspath and r[2] == key)]
        self._write_ports(rows)

    def release(self, abspath: str) -> int:
        """Remove all registry entries for abspath. Returns count removed."""
        removed = 0
        with self._lock(self.port_file):
            rows = self._read_ports()
            kept = [r for r in rows if r[1] != abspath]
            removed += len(rows) - len(kept)
            self._write_ports(kept)
        with self._lock(self.kv_file):
            kv_rows = self._read_kv()
            kept_kv = [r for r in kv_rows if r[0] != abspath]
            removed += len(kv_rows) - len(kept_kv)
            self._write_kv(kept_kv)
        with self._lock(self.device_file):
            dev_rows = self._read_devices()
            kept_dev = [r for r in dev_rows if r.checkout != abspath]
            removed += len(dev_rows) - len(kept_dev)
            self._write_devices(kept_dev)
        return removed

    # --- key/value (uuids, template results, set values) ---

    def _read_kv(self) -> list[tuple[str, str, str]]:
        out: list[tuple[str, str, str]] = []
        for line in self.kv_file.read_text().splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            out.append((parts[0], parts[1], parts[2]))
        return out

    def _write_kv(self, rows: Iterable[tuple[str, str, str]]) -> None:
        lines = [f"{path}\t{key}\t{value}" for (path, key, value) in rows]
        self.kv_file.write_text("\n".join(lines) + ("\n" if lines else ""))

    def get_kv(self, abspath: str, key: str) -> str | None:
        for path, k, value in self._read_kv():
            if path == abspath and k == key:
                return value
        return None

    def set_kv(self, abspath: str, key: str, value: str) -> None:
        with self._lock(self.kv_file):
            rows = [r for r in self._read_kv() if not (r[0] == abspath and r[1] == key)]
            rows.append((abspath, key, value))
            self._write_kv(rows)

    def remove_kv(self, abspath: str, key: str) -> None:
        with self._lock(self.kv_file):
            rows = [r for r in self._read_kv() if not (r[0] == abspath and r[1] == key)]
            self._write_kv(rows)

    def all_for(self, abspath: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for port, path, key in self._read_ports():
            if path == abspath:
                out[key] = str(port)
        for path, key, value in self._read_kv():
            if path == abspath:
                out[key] = value
        return out

    # --- devices (sim / AVD instances we created) ---

    def _read_devices(self) -> list[DeviceRow]:
        out: list[DeviceRow] = []
        for line in self.device_file.read_text().splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) != 7:
                continue
            out.append(DeviceRow(*parts))
        return out

    def _write_devices(self, rows: Iterable[DeviceRow]) -> None:
        lines = ["\t".join(r) for r in rows]
        self.device_file.write_text("\n".join(lines) + ("\n" if lines else ""))

    def get_device(self, abspath: str, dtype: str, variant: str) -> DeviceRow | None:
        for r in self._read_devices():
            if r.checkout == abspath and r.dtype == dtype and r.variant == variant:
                return r
        return None

    def set_device(
        self, abspath: str, dtype: str, variant: str,
        udid: str, model: str, ios: str,
    ) -> None:
        with self._lock(self.device_file):
            rows = [
                r for r in self._read_devices()
                if not (r.checkout == abspath and r.dtype == dtype and r.variant == variant)
            ]
            rows.append(DeviceRow(
                abspath, dtype, variant, udid, model, ios,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ))
            self._write_devices(rows)

    def remove_device(self, abspath: str, dtype: str, variant: str) -> None:
        with self._lock(self.device_file):
            rows = [
                r for r in self._read_devices()
                if not (r.checkout == abspath and r.dtype == dtype and r.variant == variant)
            ]
            self._write_devices(rows)

    def all_devices(self) -> list[DeviceRow]:
        return self._read_devices()

    def devices_for(self, abspath: str) -> list[DeviceRow]:
        return [r for r in self._read_devices() if r.checkout == abspath]

    def managed_udids(self) -> set[str]:
        return {r.udid for r in self._read_devices()}

    def gc_devices(self) -> int:
        """Drop entries whose checkout dir no longer exists. Returns count removed."""
        with self._lock(self.device_file):
            rows = self._read_devices()
            kept = [r for r in rows if Path(r.checkout).exists()]
            self._write_devices(kept)
            return len(rows) - len(kept)

    def gc(self) -> int:
        """Drop entries whose abspath no longer exists. Returns count removed."""
        removed = 0
        with self._lock(self.port_file):
            rows = self._read_ports()
            kept = [r for r in rows if Path(r[1]).exists()]
            removed += len(rows) - len(kept)
            self._write_ports(kept)
        with self._lock(self.kv_file):
            rows_kv = self._read_kv()
            kept_kv = [r for r in rows_kv if Path(r[0]).exists()]
            removed += len(rows_kv) - len(kept_kv)
            self._write_kv(kept_kv)
        removed += self.gc_devices()
        return removed


def _port_in_use(port: int) -> bool:
    """Best-effort live check. Tries to bind on loopback."""
    for family, addr in ((socket.AF_INET, ("127.0.0.1", port)), (socket.AF_INET6, ("::1", port))):
        try:
            s = socket.socket(family, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(addr)
            except OSError as e:
                if e.errno in (errno.EADDRINUSE, errno.EACCES):
                    return True
            finally:
                s.close()
        except OSError:
            continue
    return False


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
        "port_hash": lambda *xs, lo=8000, hi=9000: lo + (int(hashlib.sha256("|".join(map(str, xs)).encode()).hexdigest(), 16) % (hi - lo + 1)),
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
    except Exception:
        return cwd.name


def _current_branch(cwd: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "symbolic-ref", "--short", "HEAD"], cwd=cwd, stderr=subprocess.DEVNULL
        )
        return out.decode().strip()
    except Exception:
        return ""


def render_template(tpl: str, scope: dict[str, Any]) -> str:
    """Render `{{ expr }}` placeholders. expr is a restricted Python expression."""
    def replace(m: re.Match[str]) -> str:
        expr = m.group(1)
        try:
            value = eval(expr, {"__builtins__": {}}, scope)  # noqa: S307 - sandboxed
        except Exception as e:
            raise TemplateError(f"failed to render `{{{{ {expr} }}}}`: {e}") from e
        if callable(value):
            raise TemplateError(f"template expression `{expr}` resolved to a callable; did you mean to call it?")
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

def _parse_devices_section(
    data: dict[str, Any], *, source: str
) -> dict[str, dict[str, dict[str, Any]]]:
    """Parse [devices.<type>.<variant>] tables. Rejects the legacy flat shape
    [devices.<name>] with a clear pointer to the new nested form."""
    raw = data.get("devices", {}) or {}
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for type_key, type_val in raw.items():
        if type_key not in DEVICE_TYPES:
            # Detect the legacy flat shape so we can give a useful error.
            if isinstance(type_val, dict) and isinstance(type_val.get("type"), str):
                raise ValueError(
                    f"{source}: flat device shape `[devices.{type_key}]` is no "
                    f"longer supported; use `[devices.<type>.<variant>]` instead "
                    f"(e.g. [devices.{type_val['type']}.{type_key}])"
                )
            raise ValueError(
                f"{source}: unknown device type `{type_key}` "
                f"(known: {', '.join(DEVICE_TYPES)})"
            )
        if not isinstance(type_val, dict):
            raise ValueError(f"{source}: [devices.{type_key}] must be a table of variants")
        variants: dict[str, dict[str, Any]] = {}
        for variant_name, spec in type_val.items():
            if not DEVICE_VARIANT_RE.match(variant_name):
                raise ValueError(
                    f"{source}: variant name `{variant_name}` must match "
                    f"[A-Za-z][A-Za-z0-9_-]*"
                )
            if not isinstance(spec, dict):
                raise ValueError(
                    f"{source}: [devices.{type_key}.{variant_name}] must be a table"
                )
            variants[variant_name] = dict(spec)
        out[type_key] = variants
    return out


class Recipe:
    def __init__(self, data: dict[str, Any], path: Path):
        self.path = path
        self.resources: dict[str, dict[str, Any]] = dict(data.get("resources", {}) or {})
        self.setup: dict[str, dict[str, Any]] = dict(data.get("setup", {}) or {})
        self.project: dict[str, Any] = dict(data.get("project", {}) or {})
        self.devices: dict[str, dict[str, dict[str, Any]]] = _parse_devices_section(
            data, source=path.name or RECIPE_NAME,
        )
        for name in self.resources:
            if not ENV_NAME_RE.match(name):
                raise ValueError(f"resource name `{name}` is not a valid env var identifier")

    @classmethod
    def load(cls, path: Path) -> "Recipe":
        with path.open("rb") as f:
            data = tomllib.load(f)
        return cls(data, path)


LOCAL_SKELETON = """\
# splashdown.local.toml — additional, per-checkout device variants.
# Gitignored. Each checkout has its own copy.
#
# Recipe-declared variants don't go here; use this only to ADD variants on top
# of what the recipe exposes (no overrides — pick a distinct variant name).
#
# Example: a one-off iPhone 16 sim to reproduce a bug only this checkout sees:
#
# [devices.simulator.repro-bug]
# model = "iPhone 16"
# ios   = "17.5"
#
# Or, equivalently, via CLI:
#
#   splash device add simulator repro-bug --model="iPhone 16" --ios=17.5
"""


class LocalConfig:
    """Per-checkout local config from splashdown.local.toml. Holds additional
    [devices.<type>.<variant>] variants, alongside (not replacing) the recipe's."""

    def __init__(self, data: dict[str, Any], path: Path):
        self.path = path
        self.devices: dict[str, dict[str, dict[str, Any]]] = _parse_devices_section(
            data, source=path.name or LOCAL_NAME,
        )

    @classmethod
    def load(cls, path: Path) -> "LocalConfig":
        if not path.exists():
            return cls({}, path)
        with path.open("rb") as f:
            data = tomllib.load(f)
        return cls(data, path)


def merged_devices(
    recipe: Recipe, local: LocalConfig
) -> dict[str, dict[str, dict[str, Any]]]:
    """Union recipe + local device catalogs. (type, variant) name collisions
    between the two files are an error — pick a different name in local."""
    merged: dict[str, dict[str, dict[str, Any]]] = {
        type_key: dict(variants) for type_key, variants in recipe.devices.items()
    }
    for type_key, variants in local.devices.items():
        bucket = merged.setdefault(type_key, {})
        for variant_name, spec in variants.items():
            if variant_name in bucket:
                raise ValueError(
                    f"device `{type_key}.{variant_name}` already exists in recipe; "
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
    if not catalog:
        raise DeviceError("no variants declared for this type")
    if requested is not None:
        if requested not in catalog:
            raise DeviceError(
                f"no variant `{requested}`; declared: {', '.join(sorted(catalog))}"
            )
        return requested, catalog[requested]
    if "default" in catalog:
        return "default", catalog["default"]
    if len(catalog) == 1:
        only = next(iter(catalog))
        return only, catalog[only]
    raise DeviceError(
        f"no `default` variant; pass a variant explicitly "
        f"(declared: {', '.join(sorted(catalog))})"
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


# ---------- provisioning ----------

def provision(
    cwd: Path,
    *,
    registry: Registry,
    reprovision: bool = False,
) -> dict[str, str]:
    recipe_path = cwd / RECIPE_NAME
    if not recipe_path.exists():
        raise FileNotFoundError(f"no {RECIPE_NAME} found in {cwd}")
    recipe = Recipe.load(recipe_path)
    abspath = str(cwd.resolve())
    branch = _current_branch(cwd)
    resolved: dict[str, str] = {}

    for name in topo_sort(recipe):
        spec = recipe.resources[name]
        rtype = spec.get("type")
        if rtype == "port":
            rng = spec.get("range")
            if not (isinstance(rng, list) and len(rng) == 2):
                raise ValueError(f"`{name}` port resource needs range = [lo, hi]")
            lo, hi = int(rng[0]), int(rng[1])
            if reprovision:
                registry._remove_port(abspath, name)
            value = str(registry.allocate_port(abspath, name, lo, hi))
        elif rtype == "uuid":
            existing = registry.get_kv(abspath, name) if not reprovision else None
            value = existing or str(uuid_mod.uuid4())
            registry.set_kv(abspath, name, value)
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
            existing = registry.get_kv(abspath, name) if not reprovision else None
            if existing is not None and not _template_uses_volatile(tpl):
                value = existing
            else:
                scope = _make_scope(cwd, branch, resolved)
                value = render_template(tpl, scope)
                registry.set_kv(abspath, name, value)
        elif rtype == "set":
            existing = registry.get_kv(abspath, name)
            if existing is None:
                default = spec.get("default")
                if default is None:
                    raise ValueError(
                        f"`{name}` is a set-type resource with no value yet; "
                        f"run `mise run set {name}=VALUE` or set a `default = ...`"
                    )
                value = str(default)
                registry.set_kv(abspath, name, value)
            else:
                value = existing
        else:
            raise ValueError(f"`{name}` has unknown type `{rtype}`")
        resolved[name] = value
    return resolved


def _template_uses_volatile(tpl: str) -> bool:
    """Templates that contain `uuid()` etc. should re-evaluate only on reprovision."""
    # Currently: nothing here — once persisted, keep stable. Reprovision via --reprovision.
    return False


# ---------- writers ----------

def write_outputs(cwd: Path, recipe: Recipe, resolved: dict[str, str]) -> list[str]:
    """Dispatch resolved values to their writers. Returns human-readable messages."""
    groups: dict[str, dict[str, str]] = {}
    for name, value in resolved.items():
        writer = recipe.resources[name].get("writer", "splashdown-env")
        groups.setdefault(writer, {})[name] = value

    # Truncate splashdown.env if it exists but no resources target it now (e.g. the
    # user removed the last splashdown-env resource). Without this, the old values
    # linger and silently disagree with the recipe.
    if "splashdown-env" not in groups and (cwd / ENV_FILE_NAME).exists():
        groups["splashdown-env"] = {}

    msgs: list[str] = []
    for writer, items in groups.items():
        if writer == "splashdown-env":
            target = cwd / ENV_FILE_NAME
            write_splashdown_env(target, items)
            msgs.append(f"{ENV_FILE_NAME}: {len(items)} vars")
        elif writer.startswith("envfile"):
            path_arg = writer.split("=", 1)[1] if "=" in writer else ".env.local"
            target = cwd / path_arg
            write_envfile(target, items)
            msgs.append(f"{path_arg}: {len(items)} vars")
        elif writer == "envrc":
            target = cwd / ".envrc.local"
            write_envrc(target, items)
            msgs.append(f".envrc.local: {len(items)} vars")
        elif writer == "stdout":
            for k, v in items.items():
                print(f"{k}={v}")
            msgs.append(f"stdout: {len(items)} vars")
        elif writer == "none":
            msgs.append(f"registry-only: {len(items)} vars")
        else:
            raise ValueError(f"unknown writer `{writer}`")
    return msgs


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
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{escaped}"'


_ENV_SAFE_RE = re.compile(r"[A-Za-z0-9_./:@%+=,-]+")


def _env_quote(value: str) -> str:
    """Quote a dotenv value only when it contains characters a loader could mangle."""
    if value and _ENV_SAFE_RE.fullmatch(value):
        return value
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def write_splashdown_env(path: Path, items: dict[str, str]) -> None:
    """Write the generated env file wholesale. Splashdown owns this file."""
    lines = [f"{k}={_env_quote(v)}" for k, v in items.items()]
    path.write_text("\n".join(lines) + ("\n" if lines else ""))


def write_envfile(path: Path, items: dict[str, str]) -> None:
    existing = path.read_text().splitlines() if path.exists() else []
    managed = set(items.keys())
    kept = []
    for line in existing:
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m and m.group(1) in managed:
            continue
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    new = kept + [f"{k}={v}" for k, v in items.items()]
    path.write_text("\n".join(new) + "\n")


def write_envrc(path: Path, items: dict[str, str]) -> None:
    existing = path.read_text().splitlines() if path.exists() else []
    managed = set(items.keys())
    kept = []
    for line in existing:
        m = re.match(r"\s*export\s+([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m and m.group(1) in managed:
            continue
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    # Naive shell quoting — single-quote, escape internal single quotes.
    def quote(v: str) -> str:
        return "'" + v.replace("'", "'\\''") + "'"
    new = kept + [f"export {k}={quote(v)}" for k, v in items.items()]
    path.write_text("\n".join(new) + "\n")


# ---------- lifecycle (setup hooks) ----------

def run_setup(cwd: Path, recipe: Recipe, preset: str | None, env: dict[str, str]) -> list[str]:
    if not preset:
        return []
    spec = recipe.setup.get(preset)
    if not spec:
        return []
    commands = spec.get("run") or []
    if isinstance(commands, str):
        commands = [commands]
    messages = []
    proc_env = {**os.environ, **env}
    for cmd in commands:
        try:
            subprocess.run(cmd, shell=True, cwd=cwd, env=proc_env, check=True)
            messages.append(f"setup.{preset}: {cmd}")
        except subprocess.CalledProcessError as e:
            messages.append(f"setup.{preset} FAILED ({cmd}): exit {e.returncode}")
    return messages


# ---------- devices: iOS sims + Android emulators ----------

class DeviceError(RuntimeError):
    pass


def _default_sim_name(cwd: Path, variant: str) -> str:
    """Sim instance name: '<parent>/<basename>/<variant>'. The path component
    keeps different worktrees / clones isolated; the variant suffix lets the
    same checkout host multiple sim configs (default, lowest-supported, etc.)."""
    return f"{cwd.parent.name}/{cwd.name}/{variant}"


_AVD_INVALID_RE = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize_avd_name(name: str) -> str:
    """avdmanager rejects names containing characters outside [A-Za-z0-9._-].
    Replace anything else with `_` so the default `<parent>/<basename>/<variant>`
    scheme works on Android too."""
    return _AVD_INVALID_RE.sub("_", name)


def _resolve_device_name(spec: dict[str, Any], cwd: Path, variant: str, dtype: str | None = None) -> str:
    """Sim/AVD name: explicit `name` field on the variant (string or template),
    otherwise the path-derived default. For emulator, sanitize the
    result (avdmanager allows only [A-Za-z0-9._-])."""
    raw = spec.get("name")
    if not raw:
        name = _default_sim_name(cwd, variant)
    elif isinstance(raw, str) and "{{" in raw:
        scope = _make_scope(cwd, _current_branch(cwd), {})
        name = render_template(raw, scope)
    else:
        name = str(raw)
    if dtype == "emulator":
        return _sanitize_avd_name(name)
    return name


# --- iOS simulator ---

def _xcrun_json(args: list[str]) -> Any:
    try:
        out = subprocess.check_output(["xcrun"] + args, stderr=subprocess.DEVNULL)
    except FileNotFoundError as e:
        raise DeviceError("xcrun not found; install Xcode command-line tools") from e
    except subprocess.CalledProcessError as e:
        raise DeviceError(f"xcrun {' '.join(args)} failed: exit {e.returncode}") from e
    return json.loads(out)


def _ios_find_device_by_name(name: str) -> tuple[str, str] | None:
    """Returns (udid, state) for the named simulator, or None."""
    data = _xcrun_json(["simctl", "list", "devices", "-j"])
    for _runtime, devs in (data.get("devices") or {}).items():
        for d in devs:
            if d.get("name") == name and d.get("isAvailable"):
                return d.get("udid", ""), d.get("state", "")
    return None


def _ios_latest_runtime() -> str:
    """Latest available iOS runtime identifier."""
    data = _xcrun_json(["simctl", "list", "runtimes", "-j"])
    runtimes = [r for r in (data.get("runtimes") or []) if r.get("isAvailable")]
    if not runtimes:
        raise DeviceError("no available iOS runtimes; install one in Xcode")
    runtimes.sort(key=lambda r: _version_tuple(r.get("version", "0")))
    return runtimes[-1].get("identifier", "")


def _ios_latest_runtime_version() -> str:
    """Latest available iOS runtime version string, e.g. '18.5'. Drives auto-upgrade."""
    data = _xcrun_json(["simctl", "list", "runtimes", "-j"])
    runtimes = [r for r in (data.get("runtimes") or []) if r.get("isAvailable")]
    if not runtimes:
        raise DeviceError("no available iOS runtimes; install one in Xcode")
    runtimes.sort(key=lambda r: _version_tuple(r.get("version", "0")))
    return runtimes[-1].get("version", "")


def _version_tuple(s: str) -> tuple[int, ...]:
    """Sort '18.5' / '19.0' / '17.0' as version numbers, not strings."""
    try:
        return tuple(int(p) for p in s.split("."))
    except ValueError:
        return (0,)


def _ios_udid_exists(udid: str) -> bool:
    """Is `udid` known to xcrun simctl right now?"""
    try:
        data = _xcrun_json(["simctl", "list", "devices", "-j"])
    except DeviceError:
        return False
    for devs in (data.get("devices") or {}).values():
        for d in devs:
            if d.get("udid") == udid:
                return True
    return False


def _ios_runtime_identifier(version: str) -> str:
    """`18.5` -> `com.apple.CoreSimulator.SimRuntime.iOS-18-5`."""
    return f"com.apple.CoreSimulator.SimRuntime.iOS-{version.replace('.', '-')}"


def _ios_device_type_identifier(model: str | None) -> str:
    """Match the .devrc behaviour: prefer the user's named device, else latest iPhone Pro."""
    data = _xcrun_json(["simctl", "list", "devicetypes", "-j"])
    types = data.get("devicetypes") or []
    if model:
        for t in types:
            if t.get("name") == model:
                return t.get("identifier", "")
        raise DeviceError(f"unknown iOS device model `{model}` — try `xcrun simctl list devicetypes`")
    pros = [t for t in types if re.search(r"iPhone.*Pro$", t.get("name", ""))]
    if not pros:
        raise DeviceError("no iPhone Pro device types found; specify `model = ...` explicitly")
    pros.sort(key=lambda t: t.get("name", ""))
    return pros[-1].get("identifier", "")


def ios_ensure(name: str, model: str | None, ios_version: str | None) -> tuple[str, str]:
    """Find-or-create sim. Returns (udid, state)."""
    existing = _ios_find_device_by_name(name)
    if existing:
        return existing
    runtime = _ios_runtime_identifier(ios_version) if ios_version else _ios_latest_runtime()
    device_type = _ios_device_type_identifier(model)
    print(f"creating iOS sim '{name}' ({device_type} on {runtime})", file=sys.stderr)
    try:
        udid = subprocess.check_output(
            ["xcrun", "simctl", "create", name, device_type, runtime],
            stderr=subprocess.PIPE,
        ).decode().strip()
    except subprocess.CalledProcessError as e:
        raise DeviceError(f"simctl create failed: {e.stderr.decode().strip()}") from e
    return udid, "Shutdown"


def ios_boot(udid: str, state: str) -> None:
    if state == "Booted":
        return
    subprocess.run(["xcrun", "simctl", "boot", udid], check=True)
    subprocess.run(["open", "-a", "Simulator"], check=False)


def ios_shutdown(udid: str) -> None:
    # simctl errors with code 405 if the sim is already Shutdown — noisy and
    # useless. Skip the call entirely when there's nothing to do.
    if _ios_current_state(udid) == "Shutdown":
        return
    subprocess.run(["xcrun", "simctl", "shutdown", udid], check=False)


def ios_destroy(udid: str) -> None:
    subprocess.run(["xcrun", "simctl", "delete", udid], check=False)


# --- Android emulator ---

def _android_home() -> Path:
    h = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if h and Path(h).exists():
        return Path(h)
    candidates = [
        Path.home() / "Library/Android/sdk",  # macOS default
        Path.home() / "Android/Sdk",          # Linux default (Android Studio)
    ]
    for c in candidates:
        if c.exists():
            return c
    raise DeviceError(
        f"ANDROID_HOME not set and no SDK found at {' or '.join(str(c) for c in candidates)}"
    )


def _android_bin(name: str) -> str:
    h = _android_home()
    candidates = [
        h / "cmdline-tools" / "latest" / "bin" / name,
        h / "tools" / "bin" / name,
        h / "emulator" / name,
        h / "platform-tools" / name,
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    raise DeviceError(f"{name} not found under {h}")


def _android_avd_exists(name: str) -> bool:
    try:
        out = subprocess.check_output([_android_bin("avdmanager"), "list", "avd", "-c"], stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        return False
    return name in [line.strip() for line in out.decode().splitlines()]


def _android_latest_image() -> str:
    """Pick a sensible default system image. Prefers installed; falls back to a known-good name."""
    sdkmgr = _android_bin("sdkmanager")
    try:
        out = subprocess.check_output([sdkmgr, "--list_installed"], stderr=subprocess.DEVNULL).decode()
    except subprocess.CalledProcessError:
        out = ""
    installed = re.findall(r"^\s*(system-images;android-\d+;[^\s|]+)", out, re.M)
    if installed:
        installed.sort(key=lambda s: int(re.search(r"android-(\d+)", s).group(1)), reverse=True)
        return installed[0]
    return "system-images;android-34;google_apis;arm64-v8a"


def android_ensure(name: str, device: str | None, image: str | None) -> str:
    """Find-or-create AVD. Returns AVD name (which is the identifier)."""
    if _android_avd_exists(name):
        return name
    image = image or _android_latest_image()
    device = device or "pixel_9"
    print(f"creating Android AVD '{name}' (device={device}, image={image})", file=sys.stderr)
    avdmgr = _android_bin("avdmanager")
    proc = subprocess.run(
        [avdmgr, "create", "avd", "-n", name, "-k", image, "-d", device, "--force"],
        input=b"\n",  # answer "no" to "create custom hardware profile?"
        capture_output=True,
    )
    if proc.returncode != 0:
        raise DeviceError(f"avdmanager create failed: {proc.stderr.decode().strip()}")
    return name


def _android_running_serial(avd_name: str) -> str | None:
    """Match a running emulator to an AVD via `adb -s <serial> emu avd name`."""
    adb = _android_bin("adb")
    try:
        out = subprocess.check_output([adb, "devices"], stderr=subprocess.DEVNULL).decode()
    except subprocess.CalledProcessError:
        return None
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("emulator-") and parts[1] == "device":
            serial = parts[0]
            try:
                got = subprocess.check_output(
                    [adb, "-s", serial, "emu", "avd", "name"], stderr=subprocess.DEVNULL, timeout=2,
                ).decode().splitlines()
                if got and got[0].strip() == avd_name:
                    return serial
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                continue
    return None


def android_boot(avd_name: str) -> str:
    """Start emulator in background. Returns its adb serial once it appears."""
    serial = _android_running_serial(avd_name)
    if serial:
        return serial
    emu = _android_bin("emulator")
    log = REGISTRY_DIR / f"emulator-{_slug(avd_name)}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    print(f"booting Android AVD '{avd_name}' (log: {log})", file=sys.stderr)
    with log.open("ab") as f:
        subprocess.Popen(
            [emu, "-avd", avd_name],
            stdout=f, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    # Poll for the serial to appear.
    import time
    for _ in range(60):
        time.sleep(1)
        serial = _android_running_serial(avd_name)
        if serial:
            return serial
    raise DeviceError(f"AVD '{avd_name}' did not come up within 60s; see {log}")


def android_shutdown(avd_name: str) -> None:
    serial = _android_running_serial(avd_name)
    if not serial:
        return
    subprocess.run([_android_bin("adb"), "-s", serial, "emu", "kill"], check=False)


def android_destroy(avd_name: str) -> None:
    subprocess.run([_android_bin("avdmanager"), "delete", "avd", "-n", avd_name], check=False)


# --- reconciliation (auto-upgrade on latest, pin on explicit) ---

def ensure_fresh_sim(
    registry: Registry,
    cwd: Path,
    dtype: str,
    variant: str,
    spec: dict[str, Any],
) -> dict[str, str]:
    """Reconcile a sim/AVD instance against the variant spec. Destroys + recreates
    if the OS image (or model) has drifted from what's in the registry. Pinned
    variants (`ios = "<explicit>"`) are kept on their declared version forever."""
    checkout = str(cwd.resolve())
    sim_name = _resolve_device_name(spec, cwd, variant, dtype)

    if dtype == "simulator":
        requested = spec.get("ios", "latest")
        target_ios = _ios_latest_runtime_version() if requested == "latest" else requested
        model_spec = spec.get("model", "")
        row = registry.get_device(checkout, dtype, variant)
        stale = (
            row is None
            or not _ios_udid_exists(row.udid)
            or row.ios != target_ios
            or row.model != model_spec
        )
        if not stale:
            return {"kind": "ios", "udid": row.udid, "name": sim_name}
        if row is not None and _ios_udid_exists(row.udid):
            ios_destroy(row.udid)
        udid, _state = ios_ensure(sim_name, model_spec or None, target_ios)
        registry.set_device(checkout, dtype, variant, udid, model_spec, target_ios)
        return {"kind": "ios", "udid": udid, "name": sim_name}

    if dtype == "emulator":
        requested = spec.get("image", "latest")
        target_image = _android_latest_image() if requested == "latest" else requested
        device_spec = spec.get("device", "")
        row = registry.get_device(checkout, dtype, variant)
        stale = (
            row is None
            or not _android_avd_exists(sim_name)
            or row.ios != target_image
            or row.model != device_spec
        )
        if not stale:
            return {"kind": "android", "serial": None, "name": sim_name}
        if row is not None and _android_avd_exists(sim_name):
            android_destroy(sim_name)
        android_ensure(sim_name, device_spec or None, target_image)
        registry.set_device(checkout, dtype, variant, sim_name, device_spec, target_image)
        return {"kind": "android", "serial": None, "name": sim_name}

    raise DeviceError(f"unknown device type `{dtype}`")


# --- generic device dispatch ---

def device_status(dtype: str, resolved_name: str) -> str:
    if dtype == "simulator":
        found = _ios_find_device_by_name(resolved_name)
        if not found:
            return "absent"
        return found[1].lower()  # 'booted' / 'shutdown'
    if dtype == "emulator":
        if not _android_avd_exists(resolved_name):
            return "absent"
        return "running" if _android_running_serial(resolved_name) else "stopped"
    raise DeviceError(f"unknown device type `{dtype}`")


def device_shutdown(dtype: str, resolved_name: str) -> None:
    if dtype == "simulator":
        found = _ios_find_device_by_name(resolved_name)
        if found:
            ios_shutdown(found[0])
    elif dtype == "emulator":
        android_shutdown(resolved_name)


def device_destroy(dtype: str, resolved_name: str) -> None:
    if dtype == "simulator":
        found = _ios_find_device_by_name(resolved_name)
        if found:
            ios_shutdown(found[0])
            ios_destroy(found[0])
    elif dtype == "emulator":
        android_shutdown(resolved_name)
        android_destroy(resolved_name)


def _ios_current_state(udid: str) -> str:
    """'Booted' / 'Shutdown' / 'Unknown' for the given UDID."""
    try:
        data = _xcrun_json(["simctl", "list", "devices", "-j"])
    except DeviceError:
        return "Unknown"
    for devs in (data.get("devices") or {}).values():
        for d in devs:
            if d.get("udid") == udid:
                return d.get("state", "Unknown")
    return "Unknown"


# ---------- framework detection + `device run` ----------

def detect_framework(cwd: Path, recipe: Recipe) -> str:
    override = recipe.project.get("framework")
    if override and override != "auto":
        return override
    for preset in PRESETS.values():
        if preset.framework is None or preset.detect is None:
            continue
        if preset.detect(cwd):
            return preset.framework
    frameworks = sorted({p.framework for p in PRESETS.values() if p.framework})
    raise DeviceError(
        "could not detect project framework; set `[project] framework = "
        + "|".join(f'"{f}"' for f in frameworks)
        + "` in splashdown.toml"
    )


def _preset_for_framework(framework: str) -> Preset | None:
    """Look up the registered Preset whose framework identifier matches."""
    for preset in PRESETS.values():
        if preset.framework == framework:
            return preset
    return None


def device_run(cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
    """Build + install + run the app on the given device. Returns exit code."""
    fw = detect_framework(cwd, recipe)
    preset = _preset_for_framework(fw)
    if preset is None or preset.run is None:
        raise DeviceError(f"don't know how to run framework `{fw}`")
    return preset.run(cwd, recipe, info)


def _load_recipe_or_empty(cwd: Path) -> Recipe:
    path = cwd / RECIPE_NAME
    return Recipe.load(path) if path.exists() else Recipe({}, path)


def device_add(cwd: Path, dtype: str, variant: str, fields: dict[str, str | None]) -> None:
    """Append a [devices.<type>.<variant>] table to splashdown.local.toml. Errors
    if the (type, variant) pair already exists in either the recipe or the local
    file — pick a different variant name."""
    if dtype not in DEVICE_TYPES:
        raise DeviceError(
            f"device type `{dtype}` must be one of: {', '.join(DEVICE_TYPES)}"
        )
    if not DEVICE_VARIANT_RE.match(variant):
        raise DeviceError(
            f"variant `{variant}` must match [A-Za-z][A-Za-z0-9_-]*"
        )

    path = cwd / LOCAL_NAME
    existing_text = path.read_text() if path.exists() else LOCAL_SKELETON

    recipe = _load_recipe_or_empty(cwd)
    local = LocalConfig.load(path)
    if variant in recipe.devices.get(dtype, {}):
        raise DeviceError(
            f"device `{dtype}.{variant}` is declared in the recipe; "
            f"edit {RECIPE_NAME} or pick a different variant name"
        )
    if variant in local.devices.get(dtype, {}):
        raise DeviceError(
            f"device `{dtype}.{variant}` already exists in {LOCAL_NAME}; remove it first"
        )

    block = [f"\n[devices.{dtype}.{variant}]"]
    for key, value in fields.items():
        if value is not None:
            block.append(f"{key} = {_toml_quote(value)}")
    new_text = existing_text.rstrip() + "\n" + "\n".join(block) + "\n"
    path.write_text(new_text)


def device_remove(cwd: Path, dtype: str, variant: str) -> None:
    """Delete the [devices.<type>.<variant>] table from splashdown.local.toml.
    Refuses to touch recipe-declared variants (those you remove by editing the recipe)."""
    recipe = _load_recipe_or_empty(cwd)
    if variant in recipe.devices.get(dtype, {}):
        raise DeviceError(
            f"`{dtype}.{variant}` is declared in the recipe; "
            f"edit {RECIPE_NAME} to remove it"
        )
    path = cwd / LOCAL_NAME
    if not path.exists() or variant not in LocalConfig.load(path).devices.get(dtype, {}):
        raise DeviceError(f"no device `{dtype}.{variant}` in {LOCAL_NAME}")
    lines = path.read_text().splitlines()
    start, end = _find_table(lines, f"devices.{dtype}.{variant}")
    if start is None:
        raise DeviceError(f"no device `{dtype}.{variant}` in {LOCAL_NAME}")
    kept = lines[:start] + lines[end:]
    while kept and not kept[-1].strip():
        kept.pop()
    path.write_text("\n".join(kept) + ("\n" if kept else ""))


# ---------- init / scaffolding ----------

# Scaffold TOML templates per preset. The `PRESETS` registry at the bottom of
# this file bundles each scaffold with detection logic, run(), and wiring checks.
_MINIMAL_SCAFFOLD = """\
# splashdown.toml — committed recipe. Declares per-checkout resource slots.
[resources.RUN_ID]
type = "uuid"
"""

_RN_SCAFFOLD = """\
# splashdown.toml — React Native preset.
[resources.RCT_METRO_PORT]
type  = "port"
range = [8081, 8200]

[devices.simulator.default]
model = "iPhone 17"
# ios = "latest"   # implicit; auto-recreate when a newer iOS lands. Pin to e.g.
                   # "18.5" if you want a fixed version that never upgrades.

[project]
framework = "react-native"
"""

_FLUTTER_SCAFFOLD = """\
# splashdown.toml — Flutter preset.
# Flutter's `flutter run` auto-assigns the Dart VM / DevTools port on each
# launch; there is no equivalent of RN's RCT_METRO_PORT to pin. Splashdown's
# value for Flutter is per-checkout sim/emulator naming.
[devices.simulator.default]
model = "iPhone 17"

[devices.emulator.default]
device = "pixel_9"

[project]
framework = "flutter"
"""

_SERVER_SCAFFOLD = """\
# splashdown.toml — generic web/server preset (Next.js, Django, Rails, FastAPI,
# Spring Boot, etc.). Allocates a free PORT per checkout and a unique DATABASE_URL
# so worktrees don't clobber each other's databases.
[resources.PORT]
type  = "port"
range = [3000, 3100]

[resources.DATABASE_URL]
type     = "template"
template = "postgres://localhost:5432/myapp_{{ slug(cwd) }}"

# Add extra ports as needed, e.g.:
# [resources.STORYBOOK_PORT]
# type  = "port"
# range = [6006, 6100]
"""

_ELECTRON_SCAFFOLD = """\
# splashdown.toml — Electron preset.
# Two per-checkout collisions to solve for parallel Electron dev:
#   1. PORT — the renderer dev server (Vite / Webpack / Parcel / etc.).
#   2. ELECTRON_USER_DATA_DIR — Electron's userData path. By default every
#      instance reads/writes ~/Library/Application Support/<productName>; when
#      two checkouts run side by side they clobber each other's settings,
#      IndexedDB, and SingleInstanceLock. Wire your main process to honour the
#      env var (early, before app.whenReady()):
#         if (process.env.ELECTRON_USER_DATA_DIR) {
#           app.setPath('userData', process.env.ELECTRON_USER_DATA_DIR)
#         }
[resources.PORT]
type  = "port"
range = [3000, 3100]

[resources.ELECTRON_USER_DATA_DIR]
type     = "template"
template = "{{ cwd_abs }}/.electron-userdata"
"""

_IOS_NATIVE_SCAFFOLD = """\
# splashdown.toml — Native iOS preset (Swift/Obj-C + xcodebuild).
[devices.simulator.default]
model = "iPhone 17"

[project]
framework = "ios-native"

[project.ios]
# Required: the Xcode scheme to build.
scheme = "MyApp"
# Optional, defaults shown:
# configuration = "Debug"
# workspace     = "MyApp.xcworkspace"  # auto-detected from root if absent
# project       = "MyApp.xcodeproj"    # auto-detected from root if absent
"""

_ANDROID_NATIVE_SCAFFOLD = """\
# splashdown.toml — Native Android preset (Kotlin/Java + Gradle).
[devices.emulator.default]
device = "pixel_9"

[project]
framework = "android-native"

[project.android]
# Optional, defaults shown:
# module          = "app"
# variant         = "debug"
# application_id  = "com.example.myapp"  # asked from Gradle if not set
# launch_activity = ".MainActivity"      # uses LAUNCHER intent if not set
"""


POST_CHECKOUT_HOOK = """\
#!/bin/sh
# Splashdown per-checkout provisioning. Fires on git checkout / clone / worktree add.
set -e
TOP=$(git rev-parse --show-toplevel) || exit 0
cd "$TOP"
[ -f splashdown.toml ] || exit 0
if command -v splash >/dev/null 2>&1; then
    splash >&2 || true
else
    echo "post-checkout: \\`splash\\` not on PATH — install splashdown" >&2
fi
exit 0
"""


def _ensure_gitignore(cwd: Path) -> None:
    path = cwd / ".gitignore"
    existing = path.read_text() if path.exists() else ""
    present = set(existing.splitlines())
    additions = [e for e in (ENV_FILE_NAME, LOCAL_NAME) if e not in present]
    if not additions:
        return
    prefix = existing if existing.endswith("\n") or not existing else existing + "\n"
    path.write_text(prefix + "\n".join(additions) + "\n")
    print(f"updated .gitignore (+{', '.join(additions)})", file=sys.stderr)


def _ensure_mise_file_directive(cwd: Path) -> None:
    """Ensure mise.toml has `_.file = "splashdown.env"` under [env]."""
    directive = f'_.file = "{ENV_FILE_NAME}"'
    path = cwd / "mise.toml"
    if not path.exists():
        path.write_text(f"[env]\n{directive}\n")
        print(f"created mise.toml with {directive}", file=sys.stderr)
        return
    text = path.read_text()
    if directive in text:
        return
    lines = text.splitlines()
    start, _end = _find_table(lines, "env")
    if start is None:
        new_text = text.rstrip() + f"\n\n[env]\n{directive}\n"
    else:
        lines.insert(start + 1, directive)
        new_text = "\n".join(lines) + "\n"
    path.write_text(new_text)
    print(f"updated mise.toml (+{directive})", file=sys.stderr)


def _detect_hook_manager(cwd: Path) -> str:
    """Identify the project's existing hook manager so we coexist instead of clobber.

    Returns one of: "lefthook", "husky", "core-hookspath-other", "none".
    """
    if any((cwd / n).exists() for n in ("lefthook.yml", "lefthook.yaml", ".lefthook.yml")):
        return "lefthook"
    pkg = cwd / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
        deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        if "lefthook" in deps:
            return "lefthook"
    if (cwd / ".husky").is_dir():
        return "husky"
    try:
        out = subprocess.check_output(
            ["git", "config", "--get", "core.hooksPath"],
            cwd=cwd, stderr=subprocess.DEVNULL,
        ).decode().strip()
        if out and out != ".githooks":
            return "core-hookspath-other"
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return "none"


def _lefthook_config_path(cwd: Path) -> Path:
    for name in ("lefthook.yml", "lefthook.yaml", ".lefthook.yml"):
        path = cwd / name
        if path.exists():
            return path
    return cwd / "lefthook.yml"  # default if lefthook detected only via package.json


def _wire_post_checkout_lefthook(cwd: Path) -> None:
    """Idempotently add a `post-checkout` -> `splash` entry to the lefthook config."""
    path = _lefthook_config_path(cwd)
    text = path.read_text() if path.exists() else ""
    if "splashdown" in text and "run: splash" in text:
        # Already wired.
        _run_lefthook_install(cwd)
        return
    lines = text.splitlines()
    pc_idx = next(
        (i for i, l in enumerate(lines) if re.match(r"^post-checkout:\s*$", l)),
        None,
    )
    if pc_idx is None:
        sep = "" if not text or text.endswith("\n") else "\n"
        text = text + sep + (
            "\npost-checkout:\n"
            "  commands:\n"
            "    splashdown:\n"
            "      run: splash\n"
        )
        path.write_text(text)
    else:
        # Find end of post-checkout block (next top-level key or EOF).
        end_idx = len(lines)
        for j in range(pc_idx + 1, len(lines)):
            l = lines[j]
            if l and not l[0].isspace() and not l.startswith("#"):
                end_idx = j
                break
        # If 'commands:' exists under post-checkout, insert splashdown under it;
        # otherwise inject a fresh commands: block right after the header.
        cmds_idx = next(
            (j for j in range(pc_idx + 1, end_idx)
             if re.match(r"^\s+commands:\s*$", lines[j])),
            None,
        )
        if cmds_idx is not None:
            indent = len(lines[cmds_idx]) - len(lines[cmds_idx].lstrip())
            addition = [
                " " * (indent + 2) + "splashdown:",
                " " * (indent + 4) + "run: splash",
            ]
            lines = lines[: cmds_idx + 1] + addition + lines[cmds_idx + 1 :]
        else:
            addition = ["  commands:", "    splashdown:", "      run: splash"]
            lines = lines[: pc_idx + 1] + addition + lines[pc_idx + 1 :]
        path.write_text("\n".join(lines) + ("\n" if text.endswith("\n") or text == "" else ""))
    _run_lefthook_install(cwd)
    print(f"wired post-checkout in {path.name} (lefthook)", file=sys.stderr)


def _run_lefthook_install(cwd: Path) -> None:
    """Best-effort: regenerate the lefthook-managed git hooks. Silent if unavailable."""
    candidates: list[list[str]] = []
    if (cwd / "yarn.lock").exists():
        candidates.append(["yarn", "lefthook", "install"])
    if (cwd / "package.json").exists():
        candidates.append(["npx", "--no-install", "lefthook", "install"])
    candidates.append(["lefthook", "install"])
    for cmd in candidates:
        try:
            r = subprocess.run(
                cmd, cwd=cwd, capture_output=True, timeout=30, text=True,
            )
            if r.returncode == 0:
                return
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    print(
        "note: could not run `lefthook install` automatically — run it yourself "
        "to register the post-checkout hook",
        file=sys.stderr,
    )


def _wire_post_checkout_husky(cwd: Path) -> None:
    """Drop a husky post-checkout hook invoking `splash`."""
    husky_dir = cwd / ".husky"
    husky_dir.mkdir(exist_ok=True)
    hook = husky_dir / "post-checkout"
    hook.write_text(POST_CHECKOUT_HOOK)
    hook.chmod(0o755)
    print("wrote .husky/post-checkout (husky)", file=sys.stderr)


def _wire_post_checkout_corehookspath(cwd: Path) -> None:
    """Default path: own .githooks/ and set core.hooksPath."""
    hooks_dir = cwd / ".githooks"
    hooks_dir.mkdir(exist_ok=True)
    hook = hooks_dir / "post-checkout"
    hook.write_text(POST_CHECKOUT_HOOK)
    hook.chmod(0o755)
    try:
        subprocess.run(
            ["git", "config", "core.hooksPath", ".githooks"],
            cwd=cwd, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass
    print("wrote .githooks/post-checkout, set core.hooksPath", file=sys.stderr)


def _ensure_post_checkout_hook(cwd: Path) -> None:
    """Wire `post-checkout -> splash`, coexisting with any existing hook manager."""
    manager = _detect_hook_manager(cwd)
    if manager == "lefthook":
        _wire_post_checkout_lefthook(cwd)
    elif manager == "husky":
        _wire_post_checkout_husky(cwd)
    elif manager == "core-hookspath-other":
        try:
            current = subprocess.check_output(
                ["git", "config", "--get", "core.hooksPath"],
                cwd=cwd, stderr=subprocess.DEVNULL,
            ).decode().strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            current = "?"
        print(
            f"warning: core.hooksPath is `{current}` — not wiring automatically. "
            f"Add a post-checkout hook there that runs `splash`.",
            file=sys.stderr,
        )
    else:
        _wire_post_checkout_corehookspath(cwd)


# ---------- framework wiring (doctor) ----------
#
# WIRING is the per-framework spec shipped with the tool. Each WiringCheck names
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


@dataclass
class Preset:
    """An app-type plugin: scaffold + framework identity + detect/run + wiring.

    `name` is the user-facing key. `framework` is the internal identifier used
    in detect / device_run / [project] framework overrides — may differ from
    name (e.g. preset "rn", framework "react-native"), or be None for
    scaffold-only presets (minimal, server, electron) that don't drive devices.
    `scaffold_toml` is None for entries that exist only to register a
    framework's detect/run (e.g. expo, which has no `splash init` scaffold).
    """
    name: str
    scaffold_toml: str | None = None
    framework: str | None = None
    detect: Callable[[Path], bool] | None = None
    run: Callable[[Path, "Recipe", dict[str, str]], int] | None = None
    wiring_checks: list[WiringCheck] = field(default_factory=list)


# RN wiring checks accumulate here as the rn-* helper functions are defined
# below; the ReactNativePreset instance picks them up at registry-build time.
_RN_WIRING_CHECKS: list[WiringCheck] = []


def _resolve_doctor_framework(cwd: Path, override: str | None) -> str | None:
    """Pick the framework for doctor to check. Returns None if undetectable."""
    if override:
        return override
    recipe_path = cwd / RECIPE_NAME
    recipe = Recipe.load(recipe_path) if recipe_path.exists() else Recipe({}, recipe_path)
    try:
        return detect_framework(cwd, recipe)
    except DeviceError:
        return None


def cmd_doctor(cwd: Path, *, fix: bool = False, framework_override: str | None = None) -> int:
    """Run framework-aware wiring checks. With fix=True, apply safe autofixes."""
    framework = _resolve_doctor_framework(cwd, framework_override)
    if framework is None:
        print(
            "doctor: could not detect framework. Pass --framework=NAME or "
            f"set `[project] framework = ...` in {RECIPE_NAME}.",
            file=sys.stderr,
        )
        return 1
    preset = _preset_for_framework(framework)
    checks = preset.wiring_checks if preset else []
    if not checks:
        print(f"doctor: no wiring checks defined for framework `{framework}`.", file=sys.stderr)
        return 0

    bad = 0
    for check in checks:
        if not check.applies(cwd):
            print(f"  -  {check.id}: not applicable", file=sys.stderr)
            continue
        status, detail = check.detect(cwd)
        if status == "ok":
            print(f"  ✓  {check.id}: {check.description}", file=sys.stderr)
            continue
        # Problem.
        if fix and check.autofix is not None:
            try:
                check.autofix(cwd)
            except Exception as e:  # noqa: BLE001 - report rather than crash whole run
                print(f"  ✗  {check.id}: autofix failed: {e}", file=sys.stderr)
                bad += 1
                continue
            status_after, detail_after = check.detect(cwd)
            if status_after == "ok":
                print(f"  ✓  {check.id}: {check.description} (fixed)", file=sys.stderr)
                continue
            print(f"  ✗  {check.id}: still problem after autofix: {detail_after}", file=sys.stderr)
            if check.manual_instructions is not None:
                for line in check.manual_instructions(cwd).splitlines():
                    print(f"        {line}", file=sys.stderr)
            bad += 1
            continue
        # Not fixed (or no autofix available).
        print(f"  ✗  {check.id}: {detail}", file=sys.stderr)
        if check.manual_instructions is not None:
            for line in check.manual_instructions(cwd).splitlines():
                print(f"        {line}", file=sys.stderr)
        bad += 1
    return 0 if bad == 0 else 1


def _infer_dtype(cwd: Path, dtype: str | None) -> str:
    """Resolve an unspecified TYPE arg to the only declared device type for
    this checkout, or error if there's not exactly one."""
    if dtype:
        return dtype
    recipe = _load_recipe_or_empty(cwd)
    local = LocalConfig.load(cwd / LOCAL_NAME)
    declared = [t for t, variants in merged_devices(recipe, local).items() if variants]
    if len(declared) == 1:
        return declared[0]
    if not declared:
        raise DeviceError(
            f"no devices declared in {RECIPE_NAME} or {LOCAL_NAME}"
        )
    raise DeviceError(
        f"multiple device types declared ({', '.join(sorted(declared))}); "
        "specify one: simulator | emulator"
    )


def _resolve_variant_for_cli(
    cwd: Path, dtype: str, variant_arg: str | None
) -> tuple[str, dict[str, Any], Recipe]:
    """Common prelude for `splash run`/`start`/`stop`/`destroy`: load recipe+local,
    merge, pick variant."""
    recipe = _load_recipe_or_empty(cwd)
    local = LocalConfig.load(cwd / LOCAL_NAME)
    catalog = merged_devices(recipe, local).get(dtype, {})
    variant, spec = resolve_variant(catalog, variant_arg)
    return variant, spec, recipe


def cmd_start(cwd: Path, registry: Registry, dtype: str | None, variant_arg: str | None) -> int:
    """Reconcile the sim, then boot it. No build/launch."""
    dtype = _infer_dtype(cwd, dtype)
    variant, spec, _recipe = _resolve_variant_for_cli(cwd, dtype, variant_arg)
    info = ensure_fresh_sim(registry, cwd, dtype, variant, spec)
    if info["kind"] == "ios":
        ios_boot(info["udid"], _ios_current_state(info["udid"]))
    elif info["kind"] == "android":
        info["serial"] = android_boot(info["name"])
    print(f"started {dtype}.{variant} ({info['name']})", file=sys.stderr)
    return 0


def cmd_stop(cwd: Path, dtype: str | None, variant_arg: str | None) -> int:
    """Shut down the sim/emulator (preserves it for next start)."""
    dtype = _infer_dtype(cwd, dtype)
    variant, spec, _recipe = _resolve_variant_for_cli(cwd, dtype, variant_arg)
    resolved = _resolve_device_name(spec, cwd, variant, dtype)
    device_shutdown(dtype, resolved)
    print(f"stopped {dtype}.{variant} ({resolved})", file=sys.stderr)
    return 0


def cmd_destroy(cwd: Path, dtype: str | None, variant_arg: str | None) -> int:
    """Delete the sim/emulator and its registry entry."""
    dtype = _infer_dtype(cwd, dtype)
    variant, spec, _recipe = _resolve_variant_for_cli(cwd, dtype, variant_arg)
    resolved = _resolve_device_name(spec, cwd, variant, dtype)
    device_destroy(dtype, resolved)
    Registry().remove_device(str(cwd.resolve()), dtype, variant)
    print(f"destroyed {dtype}.{variant} ({resolved})", file=sys.stderr)
    return 0


def cmd_status(cwd: Path, registry: Registry, fmt: str) -> int:
    """Show this checkout's resolved vars, declared devices, and which ports
    are currently bound by some OS process."""
    target = str(cwd.resolve())
    resources = registry.all_for(target)

    # Identify port-typed resources from the recipe so we can flag bind state.
    port_keys: set[str] = set()
    recipe_path = cwd / RECIPE_NAME
    if recipe_path.exists():
        recipe = Recipe.load(recipe_path)
        for name, spec in recipe.resources.items():
            if spec.get("type") == "port":
                port_keys.add(name)

    res_rows: list[tuple[str, str, str]] = []
    for key, value in sorted(resources.items()):
        state = ""
        if key in port_keys:
            try:
                state = "in use" if _port_in_use(int(value)) else "free"
            except ValueError:
                state = ""
        res_rows.append((key, value, state))

    # Device variants + state (mirrors cmd_devices_list output shape).
    recipe = _load_recipe_or_empty(cwd)
    local = LocalConfig.load(cwd / LOCAL_NAME)
    catalog = merged_devices(recipe, local)
    dev_rows: list[tuple[str, str, str, str, str]] = []
    for dtype, variants in catalog.items():
        for variant, spec in variants.items():
            source = "recipe" if variant in recipe.devices.get(dtype, {}) else "local"
            resolved = _resolve_device_name(spec, cwd, variant, dtype)
            try:
                status = device_status(dtype, resolved)
            except DeviceError as e:
                status = f"error: {e}"
            dev_rows.append((dtype, variant, source, resolved, status))

    # Stale-row count (rows whose checkout path no longer exists).
    stale = sum(
        1 for r in registry._read_ports() if not Path(r[1]).exists()  # noqa: SLF001
    ) + sum(
        1 for r in registry._read_kv() if not Path(r[0]).exists()  # noqa: SLF001
    )

    if fmt == "json":
        print(json.dumps({
            "checkout": target,
            "resources": [{"key": k, "value": v, "port_state": s} for k, v, s in res_rows],
            "devices": [
                dict(zip(("type", "variant", "source", "device_name", "status"), r))
                for r in dev_rows
            ],
            "stale_registry_rows": stale,
        }, indent=2))
        return 0

    print(f"checkout: {target}", file=sys.stderr)
    print("resources:", file=sys.stderr)
    if not res_rows:
        print("  (none)", file=sys.stderr)
    for key, value, state in res_rows:
        suffix = f"  [{state}]" if state else ""
        print(f"  {key}={value}{suffix}", file=sys.stderr)
    print("devices:", file=sys.stderr)
    if not dev_rows:
        print("  (none)", file=sys.stderr)
    for dtype, variant, source, resolved, status in dev_rows:
        print(f"  {dtype}.{variant}\t{source}\t{resolved}\t{status}", file=sys.stderr)
    if stale:
        print(f"stale registry rows: {stale} (run `splash gc` to clean)", file=sys.stderr)
    return 0


def _cmd_refresh(cwd: Path, registry: Registry) -> int:
    """Re-provision. Identical to bare `splash` — exists as a named verb because
    the OS-squatter auto-reallocation in Registry.allocate_port is the thing
    users reach for under that label."""
    return _cmd_provision_inner(cwd, registry, reprovision=False)


def cmd_devices_list(cwd: Path, fmt: str) -> int:
    """List declared device variants and their live instance state."""
    recipe = _load_recipe_or_empty(cwd)
    local = LocalConfig.load(cwd / LOCAL_NAME)
    catalog = merged_devices(recipe, local)
    if not catalog:
        print(f"(no devices declared in {RECIPE_NAME} or {LOCAL_NAME})", file=sys.stderr)
        return 0
    rows: list[tuple[str, str, str, str, str]] = []
    for dtype, variants in catalog.items():
        for variant, spec in variants.items():
            source = "recipe" if variant in recipe.devices.get(dtype, {}) else "local"
            resolved = _resolve_device_name(spec, cwd, variant, dtype)
            try:
                status = device_status(dtype, resolved)
            except DeviceError as e:
                status = f"error: {e}"
            rows.append((dtype, variant, source, resolved, status))
    if fmt == "json":
        print(json.dumps(
            [dict(zip(("type", "variant", "source", "device_name", "status"), r)) for r in rows],
            indent=2,
        ))
    else:
        for dtype, variant, source, resolved, status in rows:
            print(f"{dtype}\t{variant}\t{source}\t{resolved}\t{status}")
    return 0


def _load_variant_spec(cwd: Path, dtype: str, variant: str) -> dict[str, Any] | None:
    """Look up a variant's current spec from a checkout's recipe + local config.
    Returns None if the variant has been removed from both."""
    recipe = _load_recipe_or_empty(cwd)
    try:
        local = LocalConfig.load(cwd / LOCAL_NAME)
    except ValueError:
        local = LocalConfig({}, cwd / LOCAL_NAME)
    return merged_devices(recipe, local).get(dtype, {}).get(variant)


def _emit_progress(label: str, current: int, total: int) -> None:
    """Single-line progress on a TTY ('label: 3/12'). On a non-TTY (CI, pipe)
    print one line per call instead, so logs aren't a single mash of carriage
    returns. Caller should _finish_progress() after the loop to drop a newline."""
    width = len(str(total))
    msg = f"{label}: {current:>{width}}/{total}"
    if sys.stderr.isatty():
        sys.stderr.write(f"\r{msg}")
    else:
        sys.stderr.write(f"{msg}\n")
    sys.stderr.flush()


def _finish_progress() -> None:
    if sys.stderr.isatty():
        sys.stderr.write("\n")
        sys.stderr.flush()


def cmd_device_gc(registry: Registry, *, all_: bool = False) -> int:
    """Splashdown-managed sim cleanup.

    Default: drop registry entries whose checkout dir is gone, destroy their sims.
    --all: additionally destroy sims whose recipe variant uses `ios = "latest"`
    and whose registered iOS is older than the current latest. Pinned variants
    are always preserved."""
    destroyed_count = 0
    pruned_count = 0
    latest_ios: str | None = None
    rows = list(registry.all_devices())
    total = len(rows)
    for i, row in enumerate(rows, 1):
        _emit_progress("device gc", i, total)
        cwd = Path(row.checkout)
        if not cwd.exists():
            if row.dtype == "simulator" and _ios_udid_exists(row.udid):
                ios_destroy(row.udid)
            elif row.dtype == "emulator" and _android_avd_exists(row.udid):
                android_destroy(row.udid)
            registry.remove_device(row.checkout, row.dtype, row.variant)
            destroyed_count += 1
            continue
        if not all_:
            continue
        # `--all`: prune stale "latest" variants.
        spec = _load_variant_spec(cwd, row.dtype, row.variant)
        if spec is None:
            # Variant was removed from recipe + local — also destroy.
            if row.dtype == "simulator" and _ios_udid_exists(row.udid):
                ios_destroy(row.udid)
            registry.remove_device(row.checkout, row.dtype, row.variant)
            pruned_count += 1
            continue
        if row.dtype == "simulator":
            if spec.get("ios", "latest") != "latest":
                continue  # pinned — leave alone
            if latest_ios is None:
                latest_ios = _ios_latest_runtime_version()
            if row.ios == latest_ios:
                continue  # already fresh
            if _ios_udid_exists(row.udid):
                ios_destroy(row.udid)
            registry.remove_device(row.checkout, row.dtype, row.variant)
            pruned_count += 1
    _finish_progress()
    print(
        f"device gc: removed {destroyed_count} defunct + {pruned_count} stale entries",
        file=sys.stderr,
    )
    return 0


def cmd_device_prune(
    registry: Registry,
    *,
    yes: bool = False,
    dry_run: bool = False,
    platforms: tuple[str, ...] = ("ios", "android"),
) -> int:
    """Destroy every sim/AVD on this machine that splashdown did NOT create.
    Picks up the Xcode default-template pile, hand-made sims, etc.

    Splashdown-managed entries (those in the registry) are always preserved.
    Use --dry-run to preview, --yes to skip the prompt."""
    managed = registry.managed_udids()
    foreign_ios: list[tuple[str, str, str]] = []  # (udid, name, runtime)
    foreign_avd: list[str] = []

    if "ios" in platforms:
        try:
            data = _xcrun_json(["simctl", "list", "devices", "-j"])
        except DeviceError as e:
            print(f"warning: skipping iOS sims ({e})", file=sys.stderr)
        else:
            for runtime, devs in (data.get("devices") or {}).items():
                for d in devs:
                    udid = d.get("udid")
                    if not udid or udid in managed:
                        continue
                    if not d.get("isAvailable", True):
                        continue
                    foreign_ios.append((udid, d.get("name", "?"), runtime))

    if "android" in platforms:
        try:
            out = subprocess.check_output(
                [_android_bin("avdmanager"), "list", "avd", "-c"],
                stderr=subprocess.DEVNULL,
            )
        except (DeviceError, subprocess.CalledProcessError, FileNotFoundError):
            pass
        else:
            for line in out.decode().splitlines():
                name = line.strip()
                if name and name not in managed:
                    foreign_avd.append(name)

    total = len(foreign_ios) + len(foreign_avd)
    if total == 0:
        print("device prune: nothing to remove (every sim/AVD is splashdown-managed)",
              file=sys.stderr)
        return 0

    print(
        f"About to remove {total} {'/'.join(platforms)} device(s) not managed by splashdown:",
        file=sys.stderr,
    )
    for udid, name, runtime in foreign_ios:
        print(f"  simulator     {name}  ({runtime})  {udid}", file=sys.stderr)
    for name in foreign_avd:
        print(f"  android     {name}", file=sys.stderr)

    if dry_run:
        print("device prune: --dry-run, nothing destroyed", file=sys.stderr)
        return 0
    if not yes:
        print("Continue? [y/N] ", end="", file=sys.stderr, flush=True)
        if input().strip().lower() not in ("y", "yes"):
            print("device prune: aborted", file=sys.stderr)
            return 1

    done = 0
    for udid, _name, _runtime in foreign_ios:
        ios_shutdown(udid)
        ios_destroy(udid)
        done += 1
        _emit_progress("device prune", done, total)
    for name in foreign_avd:
        android_shutdown(name)
        android_destroy(name)
        done += 1
        _emit_progress("device prune", done, total)
    _finish_progress()
    print(f"device prune: removed {total} device(s)", file=sys.stderr)
    return 0


def cmd_run(cwd: Path, registry: Registry, dtype: str | None, variant_arg: str | None) -> int:
    """Reconcile the sim, boot it, then build + launch the app via the framework's CLI."""
    dtype = _infer_dtype(cwd, dtype)
    variant, spec, recipe = _resolve_variant_for_cli(cwd, dtype, variant_arg)
    info = ensure_fresh_sim(registry, cwd, dtype, variant, spec)
    if info["kind"] == "ios":
        ios_boot(info["udid"], _ios_current_state(info["udid"]))
    elif info["kind"] == "android":
        info["serial"] = android_boot(info["name"])
    return device_run(cwd, recipe, info)


def cmd_init(cwd: Path, preset: str = "minimal", force: bool = False) -> None:
    recipe_path = cwd / RECIPE_NAME
    if recipe_path.exists() and not force:
        print(f"refusing to overwrite existing {RECIPE_NAME} (use --force)", file=sys.stderr)
        sys.exit(2)
    entry = PRESETS.get(preset)
    available = [n for n, p in PRESETS.items() if p.scaffold_toml is not None]
    if entry is None or entry.scaffold_toml is None:
        print(f"unknown preset `{preset}`; available: {', '.join(available)}", file=sys.stderr)
        sys.exit(2)
    recipe_path.write_text(entry.scaffold_toml)
    print(f"wrote {RECIPE_NAME} (preset={preset})", file=sys.stderr)

    local_path = cwd / LOCAL_NAME
    if not local_path.exists():
        local_path.write_text(LOCAL_SKELETON)
        print(f"wrote {LOCAL_NAME} (skeleton)", file=sys.stderr)

    _ensure_gitignore(cwd)
    _ensure_mise_file_directive(cwd)
    _ensure_post_checkout_hook(cwd)

    # After the generic scaffolding, run framework-specific wiring (if any).
    framework = _resolve_doctor_framework(cwd, None)
    fw_preset = _preset_for_framework(framework) if framework else None
    if fw_preset and fw_preset.wiring_checks:
        print(f"running framework wiring for `{framework}`...", file=sys.stderr)
        cmd_doctor(cwd, fix=True)


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
            out = subprocess.check_output(
                ["git", "config", "--get", "core.hooksPath"],
                cwd=cwd, stderr=subprocess.DEVNULL,
            ).decode().strip()
            if out == ".githooks":
                return ("ok", ".githooks/post-checkout invokes splash, core.hooksPath set")
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
        return ("problem", ".githooks/post-checkout exists but core.hooksPath isn't set to .githooks")
    return ("problem", "no post-checkout hook invokes splash")


def _rn_hook_manual(cwd: Path) -> str:
    return (
        "core.hooksPath is set to a non-splashdown directory. Add a post-checkout\n"
        "hook there that runs `splash` (see examples/.githooks/post-checkout)."
    )


_RN_WIRING_CHECKS.append(
    WiringCheck(
        id="rn-hook",
        description="post-checkout fires `splash`",
        applies=lambda cwd: True,
        detect=_rn_hook_detect,
        autofix=_ensure_post_checkout_hook,
        manual_instructions=_rn_hook_manual,
    ),
)


# Recognized metro.config.js shape: `port: <number>` (literal). We rewrite that to
# read `process.env.RCT_METRO_PORT` while keeping the literal as the fallback.
_METRO_LITERAL_PORT_RE = re.compile(r"\bport\s*:\s*(\d+)\b")


def _rn_metro_applies(cwd: Path) -> bool:
    return (cwd / "metro.config.js").exists()


def _rn_metro_detect(cwd: Path) -> tuple[str, str]:
    text = (cwd / "metro.config.js").read_text()
    if "process.env.RCT_METRO_PORT" in text:
        return ("ok", "metro.config.js reads process.env.RCT_METRO_PORT")
    if _METRO_LITERAL_PORT_RE.search(text):
        return ("problem", "metro.config.js hardcodes a literal port; autofixable")
    return ("problem", "metro.config.js doesn't reference RCT_METRO_PORT")


def _rn_metro_autofix(cwd: Path) -> None:
    path = cwd / "metro.config.js"
    text = path.read_text()
    if "process.env.RCT_METRO_PORT" in text:
        return  # already wired
    m = _METRO_LITERAL_PORT_RE.search(text)
    if not m:
        return  # unrecognized shape — doctor will surface manual_instructions
    new_text = (
        text[: m.start()]
        + f"port: Number(process.env.RCT_METRO_PORT) || {m.group(1)}"
        + text[m.end() :]
    )
    path.write_text(new_text)
    print(f"patched metro.config.js (RCT_METRO_PORT, fallback {m.group(1)})", file=sys.stderr)


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


def _rn_pkg_applies(cwd: Path) -> bool:
    return (cwd / "package.json").exists()


def _pkg_scripts_with_port(data: dict[str, Any]) -> list[str]:
    """Return names of scripts that override RCT_METRO_PORT with --port."""
    scripts = data.get("scripts") or {}
    hits: list[str] = []
    for name, value in scripts.items():
        if not isinstance(value, str):
            continue
        # Target the common RN scripts, plus any script invoking react-native.
        if name in _PKG_RN_SCRIPTS or "react-native" in value:
            if _PKG_PORT_RE.search(value):
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
    path = cwd / "ios" / ".xcode.env"
    text = path.read_text()
    if "splashdown.env" in text:
        return  # already wired (sentinel block or hand-written equivalent)
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


# ---------- preset registry ----------
# Each Preset bundles: scaffold TOML (for `splash init`), filesystem detection
# (for auto-detecting framework), build/install/launch (for `splash run`), and
# wiring checks (for `splash doctor`). Native presets are scaffold + detect +
# run only; their wiring is intentionally minimal.


def _detect_flutter(cwd: Path) -> bool:
    return (cwd / "pubspec.yaml").exists()


def _read_pkg_deps(cwd: Path) -> dict[str, Any]:
    pkg = cwd / "package.json"
    if not pkg.exists():
        return {}
    try:
        data = json.loads(pkg.read_text())
    except json.JSONDecodeError:
        return {}
    return {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}


def _detect_expo(cwd: Path) -> bool:
    deps = _read_pkg_deps(cwd)
    return "expo" in deps and (cwd / "app.json").exists()


def _detect_rn(cwd: Path) -> bool:
    return "react-native" in _read_pkg_deps(cwd)


def _flutter_run(cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
    device_id = info.get("udid") if info["kind"] == "ios" else info.get("serial")
    return subprocess.call(["flutter", "run", "-d", device_id], cwd=cwd)


def _rn_run(cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
    if info["kind"] == "ios":
        return subprocess.call(["npx", "react-native", "run-ios", "--udid", info["udid"]], cwd=cwd)
    return subprocess.call(["npx", "react-native", "run-android", "--deviceId", info["serial"]], cwd=cwd)


def _expo_run(cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
    if info["kind"] == "ios":
        return subprocess.call(["npx", "expo", "run:ios", "--device", info["udid"]], cwd=cwd)
    return subprocess.call(["npx", "expo", "run:android", "--device", info["serial"]], cwd=cwd)


def _has_js_or_flutter(cwd: Path) -> bool:
    return _detect_flutter(cwd) or _detect_expo(cwd) or _detect_rn(cwd)


def _detect_ios_native(cwd: Path) -> bool:
    if _has_js_or_flutter(cwd):
        return False
    return any(cwd.glob("*.xcworkspace")) or any(cwd.glob("*.xcodeproj"))


def _detect_android_native(cwd: Path) -> bool:
    if _has_js_or_flutter(cwd):
        return False
    has_build = (cwd / "build.gradle").exists() or (cwd / "build.gradle.kts").exists()
    has_settings = (cwd / "settings.gradle").exists() or (cwd / "settings.gradle.kts").exists()
    return has_build and has_settings


def _ios_xcodebuild_args(cwd: Path, cfg: dict[str, Any]) -> list[str]:
    """Build the workspace/project flag for xcodebuild — explicit setting wins,
    else first match at repo root."""
    if w := cfg.get("workspace"):
        return ["-workspace", str(w)]
    if p := cfg.get("project"):
        return ["-project", str(p)]
    workspaces = sorted(cwd.glob("*.xcworkspace"))
    if workspaces:
        return ["-workspace", workspaces[0].name]
    projects = sorted(cwd.glob("*.xcodeproj"))
    if projects:
        return ["-project", projects[0].name]
    raise DeviceError(
        "ios-native: no .xcworkspace or .xcodeproj at repo root; "
        "set `[project.ios] workspace = \"...\"` or `project = \"...\"`"
    )


def _ios_native_run(cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
    cfg = recipe.project.get("ios") or {}
    scheme = cfg.get("scheme")
    if not scheme:
        raise DeviceError(
            "ios-native: set `[project.ios] scheme = \"<your-scheme>\"` in splashdown.toml"
        )
    configuration = cfg.get("configuration", "Debug")
    udid = info["udid"]
    derived = cwd / "build" / "splash-derived"
    project_flag = _ios_xcodebuild_args(cwd, cfg)

    common = [
        "xcodebuild", *project_flag,
        "-scheme", scheme,
        "-configuration", configuration,
        "-destination", f"id={udid}",
        "-derivedDataPath", str(derived),
    ]
    rc = subprocess.call(common + ["build"], cwd=cwd)
    if rc != 0:
        return rc

    settings = subprocess.run(
        common + ["-showBuildSettings", "-json"],
        cwd=cwd, capture_output=True, text=True, check=False,
    )
    try:
        entries = json.loads(settings.stdout)
        bs = entries[0]["buildSettings"]
        app_path = Path(bs["BUILT_PRODUCTS_DIR"]) / bs["WRAPPER_NAME"]
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        raise DeviceError(f"ios-native: couldn't read xcodebuild settings: {e}")
    if not app_path.exists():
        raise DeviceError(f"ios-native: built .app missing at {app_path}")

    try:
        with (app_path / "Info.plist").open("rb") as f:
            plist = plistlib.load(f)
        bundle_id = plist["CFBundleIdentifier"]
    except (FileNotFoundError, KeyError) as e:
        raise DeviceError(f"ios-native: couldn't read bundle id from {app_path}: {e}")

    rc = subprocess.call(["xcrun", "simctl", "install", udid, str(app_path)])
    if rc != 0:
        return rc
    return subprocess.call(["xcrun", "simctl", "launch", udid, bundle_id])


def _android_native_run(cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
    cfg = recipe.project.get("android") or {}
    module = cfg.get("module", "app")
    variant = cfg.get("variant", "debug")
    serial = info["serial"]
    gradlew = cwd / "gradlew"
    gradle_cmd = [f"./{gradlew.name}"] if gradlew.exists() else ["gradle"]

    install_task = f":{module}:install{variant[:1].upper()}{variant[1:]}"
    env = {**os.environ, "ANDROID_SERIAL": serial}
    rc = subprocess.call(gradle_cmd + [install_task], cwd=cwd, env=env)
    if rc != 0:
        return rc

    app_id = cfg.get("application_id")
    if not app_id:
        try:
            out = subprocess.check_output(
                gradle_cmd + [f":{module}:properties", "-q"],
                cwd=cwd, text=True, env=env,
            )
            for line in out.splitlines():
                if line.startswith("applicationId:"):
                    app_id = line.split(":", 1)[1].strip()
                    break
        except subprocess.CalledProcessError:
            pass
    if not app_id:
        raise DeviceError(
            "android-native: couldn't resolve applicationId; set "
            "`[project.android] application_id = \"...\"` in splashdown.toml"
        )

    if activity := cfg.get("launch_activity"):
        return subprocess.call(
            ["adb", "-s", serial, "shell", "am", "start", "-n", f"{app_id}/{activity}"],
        )
    return subprocess.call(
        ["adb", "-s", serial, "shell", "monkey", "-p", app_id,
         "-c", "android.intent.category.LAUNCHER", "1"],
    )


# Shared "post-checkout hook fires `splash`" wiring check — also used by native
# presets, which otherwise have no per-checkout wiring.
_HOOK_WIRING_CHECK = WiringCheck(
    id="hook",
    description="post-checkout fires `splash`",
    applies=lambda cwd: True,
    detect=_rn_hook_detect,
    autofix=_ensure_post_checkout_hook,
    manual_instructions=_rn_hook_manual,
)


# Detection order matters: Flutter and Expo are checked before plain RN because
# an Expo project's package.json also lists react-native as a dependency.
PRESETS: dict[str, Preset] = {
    "minimal": Preset(name="minimal", scaffold_toml=_MINIMAL_SCAFFOLD),
    "flutter": Preset(
        name="flutter",
        scaffold_toml=_FLUTTER_SCAFFOLD,
        framework="flutter",
        detect=_detect_flutter,
        run=_flutter_run,
    ),
    "expo": Preset(
        name="expo",
        # no `splash init expo` scaffold; framework detect/run still works
        framework="expo",
        detect=_detect_expo,
        run=_expo_run,
    ),
    "rn": Preset(
        name="rn",
        scaffold_toml=_RN_SCAFFOLD,
        framework="react-native",
        detect=_detect_rn,
        run=_rn_run,
        wiring_checks=_RN_WIRING_CHECKS,
    ),
    "ios-native": Preset(
        name="ios-native",
        scaffold_toml=_IOS_NATIVE_SCAFFOLD,
        framework="ios-native",
        detect=_detect_ios_native,
        run=_ios_native_run,
        wiring_checks=[_HOOK_WIRING_CHECK],
    ),
    "android-native": Preset(
        name="android-native",
        scaffold_toml=_ANDROID_NATIVE_SCAFFOLD,
        framework="android-native",
        detect=_detect_android_native,
        run=_android_native_run,
        wiring_checks=[_HOOK_WIRING_CHECK],
    ),
    "electron": Preset(name="electron", scaffold_toml=_ELECTRON_SCAFFOLD),
    "server": Preset(name="server", scaffold_toml=_SERVER_SCAFFOLD),
}

# `nextjs` is the historical name for the generic server scaffold — keep it as
# an alias so `splash init nextjs` still works.
PRESETS["nextjs"] = PRESETS["server"]


# ---------- CLI ----------

KNOWN_CMDS = {
    "provision", "init", "list", "get", "set", "release", "gc", "doctor",
    "status", "refresh",
    "run", "start", "stop", "destroy",
    "devices", "device",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="splash", description="Per-checkout resource provisioner")
    parser.add_argument("--cwd", default=None, help="working directory (default: $PWD)")
    parser.add_argument("--format", choices=["text", "json"], default=None)
    parser.add_argument("--version", action="version", version=f"splashdown {__version__}")
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")

    p = sub.add_parser("provision", help="provision per splashdown.toml (default if no command)")
    p.add_argument("--reprovision", action="store_true", help="force re-allocate all resources")
    p.add_argument("--setup", help="also run a [setup.NAME] block from the recipe")

    sub.add_parser("status", help="show resolved vars, declared devices, and OS-level port collisions")
    sub.add_parser("refresh", help="re-provision and reallocate any port an OS process has squatted on")

    p = sub.add_parser("init", help="scaffold a splashdown.toml")
    p.add_argument("--preset", default="minimal")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("list", help="show this checkout's resolved vars")
    p.add_argument("--checkout", default=None)

    p = sub.add_parser("get", help="echo a single resolved value")
    p.add_argument("key")
    p.add_argument("--checkout", default=None)

    p = sub.add_parser("set", help="manually set a value (for type=\"set\" resources)")
    p.add_argument("assignment", metavar="KEY=VALUE")

    p = sub.add_parser("release", help="release this checkout's registry entries (or just KEY)")
    p.add_argument("key", nargs="?")

    sub.add_parser("gc", help="garbage-collect dead registry entries")

    p = sub.add_parser("doctor", help="check framework-aware wiring of this project")
    p.add_argument("--fix", action="store_true", help="apply safe autofixes; print manual instructions for the rest")
    p.add_argument("--framework", default=None, help="override framework detection (react-native|flutter|expo)")

    for verb, helptxt in (
        ("run", "start the device + build & launch the app on it"),
        ("start", "start the device (create-if-missing); don't build/launch"),
        ("stop", "shut down the device (preserves it for next start)"),
        ("destroy", "delete the device and its registry entry"),
    ):
        p = sub.add_parser(verb, help=helptxt)
        # dtype optional: if there's exactly one declared device type for this
        # checkout, that's what's used.
        p.add_argument("dtype", choices=DEVICE_TYPES, metavar="TYPE", nargs="?")
        p.add_argument("variant", nargs="?", help="variant name (defaults to `default`)")

    sub.add_parser("devices", help="show declared variants + instance state")

    dev = sub.add_parser("device", help="manage device variants (add/remove/gc/refresh/prune)")
    devsub = dev.add_subparsers(dest="device_cmd", metavar="ACTION")

    devsub.add_parser("gc", help="prune splashdown-managed sims for defunct checkouts")
    devsub.add_parser("refresh", help="destroy + recreate stale 'latest' sims (newer iOS available)")

    prune = devsub.add_parser("prune", help="destroy every sim/AVD splashdown did NOT create")
    prune.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    prune.add_argument("--dry-run", action="store_true", dest="dry_run", help="list without deleting")
    prune.add_argument(
        "--platforms", default="ios,android",
        help="comma-separated subset of: ios,android (default: both)",
    )

    add = devsub.add_parser("add", help="declare a variant in splashdown.local.toml")
    add.add_argument("dtype", choices=DEVICE_TYPES, metavar="TYPE")
    add.add_argument("variant", help="variant name (e.g. `default`, `small-screen`)")
    add.add_argument("--model")
    add.add_argument("--ios")
    add.add_argument("--device")
    add.add_argument("--image")
    add.add_argument("--name", dest="sim_name", help="simulator/emulator name override")

    rm = devsub.add_parser("remove", help="remove a variant from splashdown.local.toml (and destroy its sim)")
    rm.add_argument("dtype", choices=DEVICE_TYPES, metavar="TYPE")
    rm.add_argument("variant")
    rm.add_argument(
        "--keep-instance", action="store_true", dest="keep_instance",
        help="leave the simulator/emulator alive; only edit the local toml",
    )

    return parser


def _resolve_cwd(args) -> Path:
    cwd = getattr(args, "cwd", None)
    return Path(cwd).resolve() if cwd else Path(os.getcwd()).resolve()


# Top-level flags whose value lives in the next argv slot (`--flag value`). Used
# by _ensure_subcommand to skip past them when deciding where to inject the
# default `provision` subcommand.
_TOP_LEVEL_VALUE_FLAGS = {"--cwd", "--format"}


def _ensure_subcommand(argv: list[str]) -> list[str]:
    """Bare `splash …` (no subcommand) defaults to `provision`. Inserts the
    `provision` token at the right slot — after any leading top-level flags
    (`--cwd PATH`, `--format json`, …) so they parse at the root level."""
    if any(a in ("-h", "--help", "--version") for a in argv):
        return argv
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in KNOWN_CMDS:
            return argv  # explicit subcommand already present
        if a in _TOP_LEVEL_VALUE_FLAGS:
            i += 2  # flag + value
            continue
        if a.startswith("--") and "=" in a:
            i += 1  # --flag=value
            continue
        break  # first non-flag, non-subcommand token: insert provision here
    return argv[:i] + ["provision"] + argv[i:]


def _resolve_format(args) -> str:
    return getattr(args, "format", None) or "text"


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    argv = _ensure_subcommand(list(argv))

    parser = _build_parser()
    args = parser.parse_args(argv)

    cwd = _resolve_cwd(args)
    registry = Registry()

    try:
        if args.cmd == "init":
            cmd_init(cwd, preset=args.preset, force=args.force)
            return 0

        if args.cmd == "gc":
            n = registry.gc()
            print(f"gc: removed {n} dead entries", file=sys.stderr)
            return 0

        if args.cmd == "doctor":
            return cmd_doctor(cwd, fix=args.fix, framework_override=args.framework)

        if args.cmd == "run":
            return cmd_run(cwd, registry, args.dtype, args.variant)

        if args.cmd == "start":
            return cmd_start(cwd, registry, args.dtype, args.variant)

        if args.cmd == "stop":
            return cmd_stop(cwd, args.dtype, args.variant)

        if args.cmd == "destroy":
            return cmd_destroy(cwd, args.dtype, args.variant)

        if args.cmd == "devices":
            return cmd_devices_list(cwd, _resolve_format(args))

        if args.cmd == "status":
            return cmd_status(cwd, registry, _resolve_format(args))

        if args.cmd == "refresh":
            return _cmd_refresh(cwd, registry)

        if args.cmd == "list":
            target = str(Path(args.checkout).resolve()) if args.checkout else str(cwd)
            data = registry.all_for(target)
            if _resolve_format(args) == "json":
                print(json.dumps(data, indent=2))
            else:
                if not data:
                    print(f"(empty) {target}", file=sys.stderr)
                for k, v in sorted(data.items()):
                    print(f"{k}={v}")
            return 0

        if args.cmd == "get":
            target = str(Path(args.checkout).resolve()) if args.checkout else str(cwd)
            value = registry.all_for(target).get(args.key)
            if value is None:
                return 1
            print(value)
            return 0

        if args.cmd == "set":
            if "=" not in args.assignment:
                print("usage: splash set KEY=VALUE", file=sys.stderr)
                return 2
            key, value = args.assignment.split("=", 1)
            registry.set_kv(str(cwd), key, value)
            print(f"set {key}={value}", file=sys.stderr)
            return 0

        if args.cmd == "release":
            if args.key:
                registry.remove_kv(str(cwd), args.key)
                registry._remove_port(str(cwd), args.key)  # noqa: SLF001
                print(f"released {args.key}", file=sys.stderr)
            else:
                n = registry.release(str(cwd))
                print(f"released {n} entries for {cwd}", file=sys.stderr)
            return 0

        if args.cmd == "device":
            return _device_dispatch(args, cwd)

        # provision (default)
        return _cmd_provision(args, cwd, registry)
    except DeviceError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _cmd_provision(args, cwd: Path, registry: Registry) -> int:
    return _cmd_provision_inner(
        cwd, registry,
        reprovision=args.reprovision,
        setup=args.setup,
        fmt=_resolve_format(args),
    )


def _cmd_provision_inner(
    cwd: Path, registry: Registry, *,
    reprovision: bool = False, setup: str | None = None, fmt: str = "text",
) -> int:
    try:
        resolved = provision(cwd, registry=registry, reprovision=reprovision)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 0
    except (ValueError, TemplateError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    recipe = Recipe.load(cwd / RECIPE_NAME)
    local_path = cwd / LOCAL_NAME
    if not local_path.exists():
        local_path.write_text(LOCAL_SKELETON)
    msgs = write_outputs(cwd, recipe, resolved)
    setup_msgs = run_setup(cwd, recipe, setup, resolved)

    if fmt == "json":
        print(json.dumps({"resolved": resolved, "writers": msgs, "setup": setup_msgs}, indent=2))
    else:
        for k, v in resolved.items():
            print(f"  {k}={v}", file=sys.stderr)
        for m in msgs + setup_msgs:
            print(f"  -> {m}", file=sys.stderr)
    return 0


def _device_dispatch(args, cwd: Path) -> int:
    # Bare `splash device` → list devices (mirrors bare `splash` → provision).
    if args.device_cmd is None:
        return cmd_devices_list(cwd, _resolve_format(args))

    if args.device_cmd == "add":
        fields = {
            "model": args.model,
            "ios": args.ios,
            "device": args.device,
            "image": args.image,
            "name": args.sim_name,
        }
        device_add(cwd, args.dtype, args.variant, fields)
        print(f"added device `{args.dtype}.{args.variant}` to {LOCAL_NAME}", file=sys.stderr)
        return 0

    if args.device_cmd == "remove":
        # Default: also destroy the instance — most users want both. Opt out
        # of state destruction with --keep-instance.
        variant_arg = args.variant
        if not args.keep_instance:
            spec = _load_variant_spec(cwd, args.dtype, variant_arg)
            if spec is not None:
                resolved = _resolve_device_name(spec, cwd, variant_arg, args.dtype)
                try:
                    device_destroy(args.dtype, resolved)
                except DeviceError:
                    pass  # sim may not exist yet; the toml edit still proceeds
                Registry().remove_device(str(cwd.resolve()), args.dtype, variant_arg)
        device_remove(cwd, args.dtype, variant_arg)
        suffix = "" if args.keep_instance else " (and destroyed the instance)"
        print(f"removed device `{args.dtype}.{variant_arg}` from {LOCAL_NAME}{suffix}", file=sys.stderr)
        return 0

    if args.device_cmd == "gc":
        return cmd_device_gc(Registry(), all_=False)

    if args.device_cmd == "refresh":
        return cmd_device_gc(Registry(), all_=True)

    if args.device_cmd == "prune":
        platforms = tuple(p.strip() for p in args.platforms.split(",") if p.strip())
        return cmd_device_prune(
            Registry(), yes=args.yes, dry_run=args.dry_run, platforms=platforms,
        )

    print(f"splash device {args.device_cmd}: unknown action", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
