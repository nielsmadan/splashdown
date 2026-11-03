from __future__ import annotations

import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .constants import LOCAL_NAME, RECIPE_NAME, TARGET_TYPES, TARGET_VARIANT_RE
from .errors import DeviceError
from .recipe import (
    GLOBAL_SKELETON,
    LOCAL_SKELETON,
    GlobalConfig,
    LocalConfig,
    Recipe,
    _global_config_path,
    validate_target_spec,
)
from .safe_files import atomic_write_text, read_optional_editable_text


def _load_recipe_or_empty(cwd: Path) -> Recipe:
    path = cwd / RECIPE_NAME
    return Recipe.load(path) if path.exists() else Recipe({}, path)


def _match_target_type_prefix(token: str, candidates: list[str]) -> str | None:
    matches = [dtype for dtype in candidates if dtype.startswith(token)]
    return matches[0] if len(matches) == 1 else None


def _target_types_for_variant(catalog: dict[str, dict[str, Any]], variant: str) -> list[str]:
    return [dtype for dtype, variants in catalog.items() if variant in variants]


def target_source(
    dtype: str, variant: str, recipe: Recipe, local: LocalConfig, glob: GlobalConfig
) -> str:
    if variant in recipe.targets.get(dtype, {}):
        base = "recipe"
    elif variant in local.targets.get(dtype, {}):
        base = "local"
    else:
        return "global"
    return f"{base} (shadows global)" if variant in glob.targets.get(dtype, {}) else base


def _validate_target_fields(
    dtype: str,
    variant: str,
    fields: dict[str, str | None],
) -> dict[str, str]:
    try:
        return validate_target_spec(
            dtype,
            fields,
            source="command line",
            path=f"targets.{dtype}.{variant}",
        )
    except ValueError as error:
        raise DeviceError(str(error)) from error


def _required_recipe(cwd: Path) -> Recipe:
    path = cwd / RECIPE_NAME
    try:
        return Recipe.load(path)
    except FileNotFoundError as error:
        raise DeviceError(
            f"No {RECIPE_NAME} in {cwd}; run `splash init` first or use --global."
        ) from error


def target_add_destination(cwd: Path, *, global_scope: bool) -> tuple[Path, set[str]]:
    if global_scope:
        path = _global_config_path()
        text = read_optional_editable_text(path)
        config = GlobalConfig.parse(text if text is not None else GLOBAL_SKELETON, path)
        return path, set(config.targets.get("device", {}))
    recipe = _required_recipe(cwd)
    destination = cwd / LOCAL_NAME
    text = read_optional_editable_text(destination, root=cwd)
    local = LocalConfig.parse(text if text is not None else LOCAL_SKELETON, destination)
    return destination, set(recipe.targets.get("device", {})) | set(local.targets.get("device", {}))


@contextmanager
def _global_target_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path.with_suffix(".toml.lock"), flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise DeviceError("global target lock must be a regular file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def target_add(cwd: Path, dtype: str, variant: str, fields: dict[str, str | None]) -> None:
    if dtype not in TARGET_TYPES:
        raise DeviceError(f"target type `{dtype}` must be one of: {', '.join(TARGET_TYPES)}")
    if not TARGET_VARIANT_RE.match(variant):
        raise DeviceError(f"variant `{variant}` must match [A-Za-z][A-Za-z0-9_-]*")
    validated_fields = _validate_target_fields(dtype, variant, fields)

    recipe = _required_recipe(cwd) if dtype == "device" else _load_recipe_or_empty(cwd)
    path = cwd / LOCAL_NAME
    existing_text = read_optional_editable_text(path, root=cwd)
    if existing_text is None:
        existing_text = LOCAL_SKELETON
    local = LocalConfig.parse(existing_text, path)
    if variant in recipe.targets.get(dtype, {}):
        raise DeviceError(
            f"target `{dtype}.{variant}` is declared in the recipe; "
            f"edit {RECIPE_NAME} or pick a different variant name"
        )
    if variant in local.targets.get(dtype, {}):
        raise DeviceError(
            f"target `{dtype}.{variant}` already exists in {LOCAL_NAME}; remove it first"
        )

    from .tomlio import target_add_text  # noqa: PLC0415

    rendered = target_add_text(existing_text, dtype, variant, validated_fields)
    LocalConfig.parse(rendered, path)
    atomic_write_text(path, rendered, root=cwd, create=True)


def _prepare_target_remove(cwd: Path, dtype: str, variant: str) -> tuple[dict[str, Any], Path, str]:
    recipe = _load_recipe_or_empty(cwd)
    if variant in recipe.targets.get(dtype, {}):
        raise DeviceError(
            f"`{dtype}.{variant}` is declared in the recipe; edit {RECIPE_NAME} to remove it"
        )
    path = cwd / LOCAL_NAME
    existing_text = read_optional_editable_text(path, root=cwd)
    if existing_text is None:
        raise DeviceError(f"no target `{dtype}.{variant}` in {LOCAL_NAME}")
    spec = LocalConfig.parse(existing_text, path).targets.get(dtype, {}).get(variant)
    if spec is None:
        raise DeviceError(f"no target `{dtype}.{variant}` in {LOCAL_NAME}")

    from .tomlio import target_remove_text  # noqa: PLC0415

    new_text = target_remove_text(existing_text, dtype, variant)
    if new_text is None:
        raise DeviceError(f"no target `{dtype}.{variant}` in {LOCAL_NAME}")
    return spec, path, new_text


def target_remove(cwd: Path, dtype: str, variant: str) -> None:
    _spec, path, new_text = _prepare_target_remove(cwd, dtype, variant)
    atomic_write_text(path, new_text, root=cwd)


def global_target_add(dtype: str, variant: str, fields: dict[str, str | None]) -> Path:
    if dtype not in TARGET_TYPES:
        raise DeviceError(f"target type `{dtype}` must be one of: {', '.join(TARGET_TYPES)}")
    if not TARGET_VARIANT_RE.match(variant):
        raise DeviceError(f"variant `{variant}` must match [A-Za-z][A-Za-z0-9_-]*")
    validated_fields = _validate_target_fields(dtype, variant, fields)

    from .tomlio import target_add_text  # noqa: PLC0415

    path = _global_config_path()
    with _global_target_lock(path):
        text = read_optional_editable_text(path)
        existing_text = text if text is not None else GLOBAL_SKELETON
        if variant in GlobalConfig.parse(existing_text, path).targets.get(dtype, {}):
            raise DeviceError(
                f"target `{dtype}.{variant}` already exists in {path}; remove it first"
            )
        rendered = target_add_text(existing_text, dtype, variant, validated_fields)
        GlobalConfig.parse(rendered, path)
        atomic_write_text(path, rendered, create=True)
    return path


def global_target_remove(dtype: str, variant: str) -> Path:
    from .tomlio import target_remove_text  # noqa: PLC0415

    path = _global_config_path()
    with _global_target_lock(path):
        text = read_optional_editable_text(path)
        if text is None or variant not in GlobalConfig.parse(text, path).targets.get(dtype, {}):
            raise DeviceError(f"no target `{dtype}.{variant}` in {path}")
        new_text = target_remove_text(text, dtype, variant)
        if new_text is None:
            raise DeviceError(f"no target `{dtype}.{variant}` in {path}")
        atomic_write_text(path, new_text)
    return path
