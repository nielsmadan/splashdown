from __future__ import annotations

import os
import re
import subprocess
import uuid as uuid_mod
from pathlib import Path
from typing import Any

from .registry import Registry
from .recipe import (
    Recipe, LocalConfig, TemplateError,
    _slug, _make_scope, _current_branch, render_template,
    template_refs, topo_sort, _toml_quote, _env_quote, _find_table,
)
from . import RECIPE_NAME, LOCAL_NAME, ENV_FILE_NAME


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
