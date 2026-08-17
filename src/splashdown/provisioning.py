from __future__ import annotations

import os
import re
import stat
import subprocess
import uuid as uuid_mod
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from .constants import ENV_FILE_NAME, RECIPE_NAME
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

# A port resource's `range` is a two-element [lo, hi] list.
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


def write_outputs(cwd: Path, recipe: Recipe, resolved: dict[str, str]) -> list[WriterResult]:
    groups: dict[str, dict[str, str]] = {}
    for name, value in resolved.items():
        writer = recipe.resources[name].get("writer", "splashdown-env")
        groups.setdefault(writer, {})[name] = value

    # Truncate splashdown.env if it exists but no resources target it now (e.g. the
    # user removed the last splashdown-env resource). Without this, the old values
    # linger and silently disagree with the recipe.
    if "splashdown-env" not in groups and (cwd / ENV_FILE_NAME).exists():
        groups["splashdown-env"] = {}

    results: list[WriterResult] = []
    for writer, items in groups.items():
        if writer == "splashdown-env":
            target = cwd / ENV_FILE_NAME
            changed = write_splashdown_env(target, items)
            results.append(
                WriterResult("splashdown-env", f"{ENV_FILE_NAME}: {len(items)} vars", changed)
            )
        elif writer.startswith("envfile="):
            path_arg = writer.removeprefix("envfile=")
            target = cwd / path_arg
            # The recipe is auto-run by the post-checkout hook, so a committed
            # `envfile=` value is untrusted input. Reject absolute paths and any
            # `..` that escapes the checkout — otherwise it is an arbitrary-file
            # write primitive for any cloned repo.
            if not target.resolve().is_relative_to(cwd.resolve()):
                raise ValueError(
                    f"writer `envfile={path_arg}` resolves outside the checkout; "
                    "envfile paths must stay within the project directory"
                )
            changed = write_envfile(target, items)
            results.append(WriterResult(writer, f"{path_arg}: {len(items)} vars", changed))
        elif writer == "envrc":
            target = cwd / ".envrc.local"
            changed = write_envrc(target, items)
            results.append(WriterResult("envrc", f".envrc.local: {len(items)} vars", changed))
        elif writer == "stdout":
            results.append(WriterResult("stdout", f"stdout: {len(items)} vars", True, dict(items)))
        elif writer == "none":
            results.append(WriterResult("none", f"registry-only: {len(items)} vars", False))
        else:
            raise ValueError(f"unknown writer `{writer}`")
    return results


def _read_output_file(path: Path) -> tuple[str, int] | None:
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError(f"could not inspect output file `{path}`: {error}") from error
    if stat.S_ISLNK(entry.st_mode):
        raise ValueError(f"refusing to write `{path}`: destination is a symlink")
    if not stat.S_ISREG(entry.st_mode):
        raise ValueError(f"refusing to write `{path}`: destination is not a regular file")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, encoding="utf-8") as file:
            opened = os.fstat(file.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError(f"refusing to write `{path}`: destination is not a regular file")
            text = file.read()
    except OSError as error:
        raise ValueError(f"could not safely access output file `{path}`: {error}") from error
    return text, stat.S_IMODE(opened.st_mode)


def _create_output_temp(path: Path) -> tuple[int, Path]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for _ in range(10):
        temp_path = path.with_name(f".{path.name}.{uuid_mod.uuid4().hex}.tmp")
        try:
            return os.open(temp_path, flags, 0o666), temp_path
        except FileExistsError:
            continue
        except OSError as error:
            raise ValueError(f"could not create output file beside `{path}`: {error}") from error
    raise ValueError(f"could not create a unique output file beside `{path}`")


def _write_if_changed(path: Path, text: str, *, mode: int | None = None) -> bool:
    """Safely replace a regular output file when its contents or required mode differ."""
    current = _read_output_file(path)
    if current is not None and current[0] == text and (mode is None or current[1] == mode):
        return False

    output_mode = mode if mode is not None else (current[1] if current is not None else None)
    fd, temp_path = _create_output_temp(path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as file:
            if output_mode is not None:
                os.fchmod(file.fileno(), output_mode)
            file.write(text)
        os.replace(temp_path, path)
    except OSError as error:
        raise ValueError(f"could not safely write output file `{path}`: {error}") from error
    finally:
        temp_path.unlink(missing_ok=True)
    return True


def write_splashdown_env(path: Path, items: dict[str, str]) -> bool:
    """Write the generated env file wholesale. Splashdown owns this file."""
    lines = [f"{k}={_env_quote(v)}" for k, v in items.items()]
    return _write_if_changed(path, "\n".join(lines) + ("\n" if lines else ""), mode=0o600)


def write_envfile(path: Path, items: dict[str, str]) -> bool:
    try:
        current = _read_output_file(path)
        existing = current[0].splitlines() if current is not None else []
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
        path.parent.mkdir(parents=True, exist_ok=True)
        return _write_if_changed(path, "\n".join(new) + "\n")
    except (OSError, ValueError) as error:
        raise ValueError(f"could not write envfile `{path}`: {error}") from error


def write_envrc(path: Path, items: dict[str, str]) -> bool:
    current = _read_output_file(path)
    existing = current[0].splitlines() if current is not None else []
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


def _strip_managed_keys(text: str, keys: set[str], *, export: bool) -> str | None:
    """Drop `KEY=` (or `export KEY=`) lines for `keys` from `text`, the inverse of
    `write_envfile`/`write_envrc`. Returns the remaining text, or `None` when only
    blank lines remain (the caller then deletes the now-empty file)."""
    prefix = r"\s*export\s+" if export else r"\s*"
    pat = re.compile(prefix + r"([A-Za-z_][A-Za-z0-9_]*)\s*=")
    kept = [ln for ln in text.splitlines() if not ((m := pat.match(ln)) and m.group(1) in keys)]
    while kept and not kept[-1].strip():
        kept.pop()
    if not any(ln.strip() for ln in kept):
        return None
    return "\n".join(kept) + "\n"


def clear_writer_destinations(cwd: Path, recipe: Recipe) -> list[tuple[str, str]]:
    """Remove splashdown's injected keys from every per-resource `envfile=`/`envrc`
    writer destination (splashdown co-owns specific keys in these user files; it
    does not own them wholesale like `splashdown.env`). Deletes a destination that
    ends up empty. Returns `[(relpath, "cleaned" | "removed")]` for what changed."""
    groups: dict[str, set[str]] = {}
    for name, spec in recipe.resources.items():
        writer = spec.get("writer", "splashdown-env")
        if writer.startswith("envfile=") or writer == "envrc":
            groups.setdefault(writer, set()).add(name)

    changed: list[tuple[str, str]] = []
    for writer, keys in groups.items():
        if writer == "envrc":
            relpath, export = ".envrc.local", True
        else:
            relpath = writer.removeprefix("envfile=")
            export = False
        target = cwd / relpath
        # Mirror write_outputs' containment guard: never touch a path a committed
        # recipe points outside the checkout.
        if not target.resolve().is_relative_to(cwd.resolve()):
            continue
        try:
            current = _read_output_file(target)
        except ValueError:
            continue
        if current is None:
            continue
        remaining = _strip_managed_keys(current[0], keys, export=export)
        if remaining is None:
            target.unlink()
            changed.append((relpath, "removed"))
        elif _write_if_changed(target, remaining):
            changed.append((relpath, "cleaned"))
    return changed


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
