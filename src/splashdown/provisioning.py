from __future__ import annotations

import os
import re
import subprocess
import uuid as uuid_mod
from pathlib import Path

from . import ENV_FILE_NAME, RECIPE_NAME
from .recipe import (
    Recipe,
    _current_branch,
    _env_quote,
    _make_scope,
    _slug,
    render_template,
    topo_sort,
)
from .registry import Registry

# ---------- provisioning ----------

# A port resource's `range` is a two-element [lo, hi] list.
_PORT_RANGE_LEN = 2


def provision(  # noqa: PLR0912 — one branch per resource type; this is the dispatch
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
            if not (isinstance(rng, list) and len(rng) == _PORT_RANGE_LEN):
                raise ValueError(f"`{name}` port resource needs range = [lo, hi]")
            lo, hi = int(rng[0]), int(rng[1])
            if reprovision:
                registry.remove_port(abspath, name)
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


def write_outputs(cwd: Path, recipe: Recipe, resolved: dict[str, str]) -> list[tuple[str, bool]]:
    """Dispatch resolved values to their writers. Returns (message, changed) per
    writer — `changed` is True when the writer actually touched disk (or, for the
    stdout writer, produced output)."""
    groups: dict[str, dict[str, str]] = {}
    for name, value in resolved.items():
        writer = recipe.resources[name].get("writer", "splashdown-env")
        groups.setdefault(writer, {})[name] = value

    # Truncate splashdown.env if it exists but no resources target it now (e.g. the
    # user removed the last splashdown-env resource). Without this, the old values
    # linger and silently disagree with the recipe.
    if "splashdown-env" not in groups and (cwd / ENV_FILE_NAME).exists():
        groups["splashdown-env"] = {}

    msgs: list[tuple[str, bool]] = []
    for writer, items in groups.items():
        if writer == "splashdown-env":
            target = cwd / ENV_FILE_NAME
            changed = write_splashdown_env(target, items)
            msgs.append((f"{ENV_FILE_NAME}: {len(items)} vars", changed))
        elif writer.startswith("envfile"):
            path_arg = writer.split("=", 1)[1] if "=" in writer else ".env.local"
            target = cwd / path_arg
            changed = write_envfile(target, items)
            msgs.append((f"{path_arg}: {len(items)} vars", changed))
        elif writer == "envrc":
            target = cwd / ".envrc.local"
            changed = write_envrc(target, items)
            msgs.append((f".envrc.local: {len(items)} vars", changed))
        elif writer == "stdout":
            for k, v in items.items():
                print(f"{k}={v}")
            msgs.append((f"stdout: {len(items)} vars", True))
        elif writer == "none":
            msgs.append((f"registry-only: {len(items)} vars", False))
        else:
            raise ValueError(f"unknown writer `{writer}`")
    return msgs


def _write_if_changed(path: Path, text: str) -> bool:
    """Write `text` to `path` only if it differs from the current contents. Returns
    True when the file was (re)written, False when it already matched."""
    if path.exists() and path.read_text() == text:
        return False
    path.write_text(text)
    return True


def write_splashdown_env(path: Path, items: dict[str, str]) -> bool:
    """Write the generated env file wholesale. Splashdown owns this file."""
    lines = [f"{k}={_env_quote(v)}" for k, v in items.items()]
    return _write_if_changed(path, "\n".join(lines) + ("\n" if lines else ""))


def write_envfile(path: Path, items: dict[str, str]) -> bool:
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
    new = kept + [f"{k}={_env_quote(v)}" for k, v in items.items()]
    return _write_if_changed(path, "\n".join(new) + "\n")


def write_envrc(path: Path, items: dict[str, str]) -> bool:
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

    def _shell_single_quote(v: str) -> str:
        return "'" + v.replace("'", "'\\''") + "'"

    new = kept + [f"export {k}={_shell_single_quote(v)}" for k, v in items.items()]
    return _write_if_changed(path, "\n".join(new) + "\n")


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
            subprocess.run(cmd, shell=True, cwd=cwd, env=proc_env, check=True)  # noqa: S602 — runs user-authored [setup.*] commands by design
            messages.append(f"setup.{preset}: {cmd}")
        except subprocess.CalledProcessError as e:
            messages.append(f"setup.{preset} FAILED ({cmd}): exit {e.returncode}")
    return messages
