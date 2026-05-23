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
import re
import socket
import subprocess
import sys
import tomllib
import uuid as uuid_mod
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, NamedTuple


# ---------- paths & constants ----------

STATE_HOME = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
REGISTRY_DIR = STATE_HOME / "splashdown"
PORT_REGISTRY = REGISTRY_DIR / "ports.tsv"
KV_REGISTRY = REGISTRY_DIR / "kv.tsv"

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEVICE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
RECIPE_NAME = "splashdown.toml"
LOCAL_NAME = "splashdown.local.toml"
ENV_FILE_NAME = "splashdown.env"


# ---------- registry ----------

class Registry:
    """Machine-local registry. TSV files protected by flock.

    ports.tsv:  port\tabspath\tkey
    kv.tsv:     abspath\tkey\tvalue
    """

    def __init__(self, port_file: Path = PORT_REGISTRY, kv_file: Path = KV_REGISTRY):
        self.port_file = port_file
        self.kv_file = kv_file
        port_file.parent.mkdir(parents=True, exist_ok=True)
        port_file.touch(exist_ok=True)
        kv_file.touch(exist_ok=True)

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

    def unpin(self, abspath: str) -> int:
        """Remove all entries for abspath. Returns count removed."""
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

class Recipe:
    def __init__(self, data: dict[str, Any], path: Path):
        self.path = path
        if "devices" in data:
            raise ValueError(
                f"[devices.*] is not allowed in {RECIPE_NAME} — the committed "
                f"recipe is schema only. Declare devices in {LOCAL_NAME} instead."
            )
        self.resources: dict[str, dict[str, Any]] = dict(data.get("resources", {}) or {})
        self.setup: dict[str, dict[str, Any]] = dict(data.get("setup", {}) or {})
        self.project: dict[str, Any] = dict(data.get("project", {}) or {})
        for name in self.resources:
            if not ENV_NAME_RE.match(name):
                raise ValueError(f"resource name `{name}` is not a valid env var identifier")

    @classmethod
    def load(cls, path: Path) -> "Recipe":
        with path.open("rb") as f:
            data = tomllib.load(f)
        return cls(data, path)


LOCAL_SKELETON = """\
# splashdown.local.toml — per-checkout device config. Gitignored, not committed.
# Declare the simulator(s) / emulator(s) you want for THIS checkout. Each
# checkout (worktree or clone) has its own copy of this file.
#
# [devices.iphone]
# type = "ios-sim"
# # model = "iPhone 16 Pro"   # optional; default = latest iPhone Pro
# # ios   = "18.5"            # optional; default = latest installed runtime
#
# [devices.android]
# type = "android-emulator"
# # device = "pixel_7"
# # image  = "system-images;android-34;google_apis;arm64-v8a"
#
# Or run:  splash device add iphone --type=ios-sim
"""


class LocalConfig:
    """Per-checkout local config from splashdown.local.toml. Holds [devices.*]."""

    def __init__(self, data: dict[str, Any], path: Path):
        self.path = path
        self.devices: dict[str, dict[str, Any]] = dict(data.get("devices", {}) or {})
        for name in self.devices:
            if not DEVICE_NAME_RE.match(name):
                raise ValueError(
                    f"device name `{name}` must match [A-Za-z][A-Za-z0-9_-]*"
                )

    @classmethod
    def load(cls, path: Path) -> "LocalConfig":
        if not path.exists():
            return cls({}, path)
        with path.open("rb") as f:
            data = tomllib.load(f)
        return cls(data, path)


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


def _default_sim_name(cwd: Path) -> str:
    """Mirror the convention from .devrc: '<parent>/<basename>'."""
    return f"{cwd.parent.name}/{cwd.name}"


def _resolve_device_name(spec: dict[str, Any], cwd: Path) -> str:
    """Device `name` field: explicit template or default to '<parent>/<basename>'."""
    raw = spec.get("name")
    if not raw:
        return _default_sim_name(cwd)
    if isinstance(raw, str) and "{{" in raw:
        scope = _make_scope(cwd, _current_branch(cwd), {})
        return render_template(raw, scope)
    return str(raw)


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
    runtimes.sort(key=lambda r: r.get("version", ""))
    return runtimes[-1].get("identifier", "")


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
    subprocess.run(["xcrun", "simctl", "shutdown", udid], check=False)


def ios_destroy(udid: str) -> None:
    subprocess.run(["xcrun", "simctl", "delete", udid], check=False)


# --- Android emulator ---

def _android_home() -> Path:
    h = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if h and Path(h).exists():
        return Path(h)
    default = Path.home() / "Library/Android/sdk"
    if default.exists():
        return default
    raise DeviceError("ANDROID_HOME not set and ~/Library/Android/sdk not found")


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
    device = device or "pixel_7"
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


# --- generic device dispatch ---

def device_status(spec: dict[str, Any], resolved_name: str) -> str:
    dtype = spec.get("type")
    if dtype == "ios-sim":
        found = _ios_find_device_by_name(resolved_name)
        if not found:
            return "absent"
        return found[1].lower()  # 'booted' / 'shutdown'
    if dtype == "android-emulator":
        if not _android_avd_exists(resolved_name):
            return "absent"
        return "running" if _android_running_serial(resolved_name) else "stopped"
    raise DeviceError(f"unknown device type `{dtype}`")


def device_boot(spec: dict[str, Any], resolved_name: str) -> dict[str, str]:
    """Returns info dict (udid/serial, kind)."""
    dtype = spec.get("type")
    if dtype == "ios-sim":
        udid, state = ios_ensure(resolved_name, spec.get("model"), spec.get("ios"))
        ios_boot(udid, state)
        return {"kind": "ios", "udid": udid, "name": resolved_name}
    if dtype == "android-emulator":
        avd = android_ensure(resolved_name, spec.get("device"), spec.get("image"))
        serial = android_boot(avd)
        return {"kind": "android", "serial": serial, "name": resolved_name}
    raise DeviceError(f"unknown device type `{dtype}`")


def device_shutdown(spec: dict[str, Any], resolved_name: str) -> None:
    dtype = spec.get("type")
    if dtype == "ios-sim":
        found = _ios_find_device_by_name(resolved_name)
        if found:
            ios_shutdown(found[0])
    elif dtype == "android-emulator":
        android_shutdown(resolved_name)


def device_destroy(spec: dict[str, Any], resolved_name: str) -> None:
    dtype = spec.get("type")
    if dtype == "ios-sim":
        found = _ios_find_device_by_name(resolved_name)
        if found:
            ios_shutdown(found[0])
            ios_destroy(found[0])
    elif dtype == "android-emulator":
        android_shutdown(resolved_name)
        android_destroy(resolved_name)


# ---------- framework detection + `device run` ----------

def detect_framework(cwd: Path, recipe: Recipe) -> str:
    override = recipe.project.get("framework")
    if override and override != "auto":
        return override
    if (cwd / "pubspec.yaml").exists():
        return "flutter"
    pkg = cwd / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
        except json.JSONDecodeError:
            data = {}
        deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        if "expo" in deps and (cwd / "app.json").exists():
            return "expo"
        if "react-native" in deps:
            return "react-native"
    raise DeviceError(
        "could not detect project framework; set `[project] framework = \"flutter\"|\"react-native\"|\"expo\"` in splashdown.toml"
    )


def device_run(cwd: Path, recipe: Recipe, info: dict[str, str]) -> int:
    """Build + install + run the app on the given device. Returns exit code."""
    fw = detect_framework(cwd, recipe)
    kind = info["kind"]
    if fw == "flutter":
        device_id = info.get("udid") if kind == "ios" else info.get("serial")
        return subprocess.call(["flutter", "run", "-d", device_id], cwd=cwd)
    if fw == "react-native":
        if kind == "ios":
            return subprocess.call(["npx", "react-native", "run-ios", "--udid", info["udid"]], cwd=cwd)
        return subprocess.call(["npx", "react-native", "run-android", "--deviceId", info["serial"]], cwd=cwd)
    if fw == "expo":
        if kind == "ios":
            return subprocess.call(["npx", "expo", "run:ios", "--device", info["udid"]], cwd=cwd)
        return subprocess.call(["npx", "expo", "run:android", "--device", info["serial"]], cwd=cwd)
    raise DeviceError(f"don't know how to run framework `{fw}`")


def pick_device(local: "LocalConfig", requested: str | None) -> tuple[str, dict[str, Any]]:
    if not local.devices:
        raise DeviceError(f"no [devices.*] declared in {LOCAL_NAME}")
    if requested:
        if requested not in local.devices:
            raise DeviceError(
                f"no device `{requested}`; declared: {', '.join(local.devices)}"
            )
        return requested, local.devices[requested]
    if len(local.devices) == 1:
        only = next(iter(local.devices))
        return only, local.devices[only]
    raise DeviceError(
        f"multiple devices declared ({', '.join(local.devices)}); pass NAME explicitly"
    )


DEVICE_TYPES = ("ios-sim", "android-emulator")


def device_add(cwd: Path, name: str, dtype: str, fields: dict[str, str | None]) -> None:
    """Append a [devices.NAME] table to splashdown.local.toml."""
    if not DEVICE_NAME_RE.match(name):
        raise DeviceError(f"device name `{name}` must match [A-Za-z][A-Za-z0-9_-]*")
    if dtype not in DEVICE_TYPES:
        raise DeviceError(f"device type `{dtype}` must be one of: {', '.join(DEVICE_TYPES)}")

    path = cwd / LOCAL_NAME
    existing = path.read_text() if path.exists() else LOCAL_SKELETON
    if LocalConfig.load(path).devices.get(name) is not None:
        raise DeviceError(f"device `{name}` already exists in {LOCAL_NAME}; remove it first")

    block = [f"\n[devices.{name}]", f'type = {_toml_quote(dtype)}']
    for key, value in fields.items():
        if value is not None:
            block.append(f"{key} = {_toml_quote(value)}")
    new_text = existing.rstrip() + "\n" + "\n".join(block) + "\n"
    path.write_text(new_text)


def device_remove(cwd: Path, name: str) -> None:
    """Delete the [devices.NAME] table from splashdown.local.toml."""
    path = cwd / LOCAL_NAME
    if not path.exists() or LocalConfig.load(path).devices.get(name) is None:
        raise DeviceError(f"no device `{name}` in {LOCAL_NAME}")
    lines = path.read_text().splitlines()
    start, end = _find_table(lines, f"devices.{name}")
    if start is None:
        raise DeviceError(f"no device `{name}` in {LOCAL_NAME}")
    kept = lines[:start] + lines[end:]
    while kept and not kept[-1].strip():
        kept.pop()
    path.write_text("\n".join(kept) + ("\n" if kept else ""))


# ---------- init / scaffolding ----------

PRESETS: dict[str, str] = {
    "minimal": """\
# splashdown.toml — committed recipe. Declares per-checkout resource slots.
[resources.RUN_ID]
type = "uuid"
""",
    "rn": """\
# splashdown.toml — React Native preset.
[resources.RCT_METRO_PORT]
type  = "port"
range = [8081, 8200]

[resources.SIM_NAME]
type     = "template"
template = "{{ basename(parent) }}/{{ cwd }}"

[resources.TEST_DB]
type     = "template"
template = "myapp_{{ slug(cwd) }}"

[project]
framework = "react-native"
""",
    "flutter": """\
# splashdown.toml — Flutter preset.
[resources.DART_PORT]
type  = "port"
range = [9100, 9200]

[resources.SIM_NAME]
type     = "template"
template = "{{ basename(parent) }}/{{ cwd }}"

[project]
framework = "flutter"
""",
    "nextjs": """\
# splashdown.toml — Next.js preset.
[resources.PORT]
type  = "port"
range = [3000, 3100]

[resources.STORYBOOK_PORT]
type  = "port"
range = [6006, 6100]

[resources.DATABASE_URL]
type     = "template"
template = "postgres://localhost:5432/myapp_{{ slug(cwd) }}"
""",
}


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


WIRING: dict[str, list[WiringCheck]] = {
    # Framework -> ordered list of checks. Populated below by the rn-* / etc.
    # entries. Frameworks without an entry here simply have no doctor checks.
}


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
    checks = WIRING.get(framework, [])
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


def cmd_init(cwd: Path, preset: str = "minimal", force: bool = False) -> None:
    recipe_path = cwd / RECIPE_NAME
    if recipe_path.exists() and not force:
        print(f"refusing to overwrite existing {RECIPE_NAME} (use --force)", file=sys.stderr)
        sys.exit(2)
    body = PRESETS.get(preset)
    if body is None:
        print(f"unknown preset `{preset}`; available: {', '.join(PRESETS)}", file=sys.stderr)
        sys.exit(2)
    recipe_path.write_text(body)
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
    if framework and WIRING.get(framework):
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


WIRING["react-native"] = [
    WiringCheck(
        id="rn-hook",
        description="post-checkout fires `splash`",
        applies=lambda cwd: True,
        detect=_rn_hook_detect,
        autofix=_ensure_post_checkout_hook,
        manual_instructions=_rn_hook_manual,
    ),
]


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


WIRING["react-native"].append(
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


WIRING["react-native"].append(
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


WIRING["react-native"].append(
    WiringCheck(
        id="rn-xcode-env",
        description="ios/.xcode.env wires RCT_METRO_PORT to splashdown.env",
        applies=_rn_xcode_applies,
        detect=_rn_xcode_detect,
        autofix=_rn_xcode_autofix,
        manual_instructions=_rn_xcode_manual,
    ),
)


# ---------- CLI ----------

KNOWN_CMDS = {"provision", "init", "list", "get", "set", "unpin", "gc", "device", "doctor"}


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS defaults so a top-level value isn't clobbered when the subparser
    # (which also inherits `common` via `parents=`) parses without these flags.
    common.add_argument("--cwd", default=argparse.SUPPRESS, help="working directory (default: $PWD)")
    common.add_argument("--format", choices=["text", "json"], default=argparse.SUPPRESS)

    parser = argparse.ArgumentParser(prog="splash", description="Per-checkout resource provisioner", parents=[common])
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")

    p = sub.add_parser("provision", parents=[common], help="provision per splashdown.toml (default if no command)")
    p.add_argument("--reprovision", action="store_true", help="force re-allocate all resources")
    p.add_argument("--setup", help="also run a [setup.NAME] block from the recipe")

    p = sub.add_parser("init", parents=[common], help="scaffold a splashdown.toml")
    p.add_argument("--preset", default="minimal")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("list", parents=[common], help="show this checkout's resolved vars")
    p.add_argument("--checkout", default=None)

    p = sub.add_parser("get", parents=[common], help="echo a single resolved value")
    p.add_argument("key")
    p.add_argument("--checkout", default=None)

    p = sub.add_parser("set", parents=[common], help="manually set a value (for type=\"set\" resources)")
    p.add_argument("assignment", metavar="KEY=VALUE")

    p = sub.add_parser("unpin", parents=[common], help="release this checkout's entries (or just KEY)")
    p.add_argument("key", nargs="?")

    sub.add_parser("gc", parents=[common], help="garbage-collect dead registry entries")

    p = sub.add_parser("doctor", parents=[common], help="check framework-aware wiring of this project")
    p.add_argument("--fix", action="store_true", help="apply safe autofixes; print manual instructions for the rest")
    p.add_argument("--framework", default=None, help="override framework detection (react-native|flutter|expo)")

    dev = sub.add_parser("device", parents=[common], help="manage iOS sims / Android emulators")
    devsub = dev.add_subparsers(dest="device_cmd", metavar="ACTION", required=True)
    for action in ("list", "boot", "run", "shutdown", "destroy"):
        sp = devsub.add_parser(action, parents=[common])
        if action != "list":
            sp.add_argument("name", nargs="?", help="device name (optional if only one declared)")

    add = devsub.add_parser("add", parents=[common], help="declare a device in splashdown.local.toml")
    add.add_argument("name")
    add.add_argument("--type", required=True, choices=DEVICE_TYPES, dest="dtype")
    add.add_argument("--model")
    add.add_argument("--ios")
    add.add_argument("--device")
    add.add_argument("--image")
    add.add_argument("--name", dest="sim_name", help="simulator/emulator name override")

    rm = devsub.add_parser("remove", parents=[common], help="remove a device from splashdown.local.toml")
    rm.add_argument("name")

    return parser


def _resolve_cwd(args) -> Path:
    cwd = getattr(args, "cwd", None)
    return Path(cwd).resolve() if cwd else Path(os.getcwd()).resolve()


def _resolve_format(args) -> str:
    return getattr(args, "format", "text")


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    argv = list(argv)
    # Bare `splash ...` with no subcommand defaults to `provision`, unless the
    # user is asking for top-level help.
    if not any(a in KNOWN_CMDS for a in argv) and not any(a in ("-h", "--help") for a in argv):
        argv = ["provision"] + argv

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

        if args.cmd == "unpin":
            if args.key:
                registry.remove_kv(str(cwd), args.key)
                registry._remove_port(str(cwd), args.key)  # noqa: SLF001
                print(f"unpinned {args.key}", file=sys.stderr)
            else:
                n = registry.unpin(str(cwd))
                print(f"unpinned {n} entries for {cwd}", file=sys.stderr)
            return 0

        if args.cmd == "device":
            return _device_dispatch(args, cwd)

        # provision (default)
        return _cmd_provision(args, cwd, registry)
    except DeviceError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _cmd_provision(args, cwd: Path, registry: Registry) -> int:
    try:
        resolved = provision(cwd, registry=registry, reprovision=args.reprovision)
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
    setup_msgs = run_setup(cwd, recipe, args.setup, resolved)

    if _resolve_format(args) == "json":
        print(json.dumps({"resolved": resolved, "writers": msgs, "setup": setup_msgs}, indent=2))
    else:
        for k, v in resolved.items():
            print(f"  {k}={v}", file=sys.stderr)
        for m in msgs + setup_msgs:
            print(f"  -> {m}", file=sys.stderr)
    return 0


def _device_dispatch(args, cwd: Path) -> int:
    local = LocalConfig.load(cwd / LOCAL_NAME)

    if args.device_cmd == "add":
        fields = {
            "model": args.model,
            "ios": args.ios,
            "device": args.device,
            "image": args.image,
            "name": args.sim_name,
        }
        device_add(cwd, args.name, args.dtype, fields)
        print(f"added device `{args.name}` ({args.dtype}) to {LOCAL_NAME}", file=sys.stderr)
        return 0

    if args.device_cmd == "remove":
        device_remove(cwd, args.name)
        print(f"removed device `{args.name}` from {LOCAL_NAME}", file=sys.stderr)
        return 0

    if args.device_cmd == "list":
        if not local.devices:
            print(f"(no devices declared in {LOCAL_NAME})", file=sys.stderr)
            return 0
        rows = []
        for name, spec in local.devices.items():
            resolved = _resolve_device_name(spec, cwd)
            try:
                status = device_status(spec, resolved)
            except DeviceError as e:
                status = f"error: {e}"
            rows.append((name, spec.get("type", "?"), resolved, status))
        if _resolve_format(args) == "json":
            print(json.dumps(
                [dict(zip(("name", "type", "device_name", "status"), r)) for r in rows],
                indent=2,
            ))
        else:
            for name, dtype, resolved, status in rows:
                print(f"{name}\t{dtype}\t{resolved}\t{status}")
        return 0

    name, spec = pick_device(local, args.name)
    resolved = _resolve_device_name(spec, cwd)

    if args.device_cmd == "boot":
        info = device_boot(spec, resolved)
        print(f"booted {name} ({info})", file=sys.stderr)
        return 0
    if args.device_cmd == "run":
        recipe_path = cwd / RECIPE_NAME
        recipe = Recipe.load(recipe_path) if recipe_path.exists() else Recipe({}, recipe_path)
        info = device_boot(spec, resolved)
        return device_run(cwd, recipe, info)
    if args.device_cmd == "shutdown":
        device_shutdown(spec, resolved)
        print(f"shutdown {name} ({resolved})", file=sys.stderr)
        return 0
    if args.device_cmd == "destroy":
        device_destroy(spec, resolved)
        print(f"destroyed {name} ({resolved})", file=sys.stderr)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
