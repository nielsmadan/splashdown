from __future__ import annotations

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


def target_add(cwd: Path, dtype: str, variant: str, fields: dict[str, str | None]) -> None:
    if dtype not in TARGET_TYPES:
        raise DeviceError(f"target type `{dtype}` must be one of: {', '.join(TARGET_TYPES)}")
    if not TARGET_VARIANT_RE.match(variant):
        raise DeviceError(f"variant `{variant}` must match [A-Za-z][A-Za-z0-9_-]*")
    validated_fields = _validate_target_fields(dtype, variant, fields)

    path = cwd / LOCAL_NAME
    existing_text = path.read_text() if path.exists() else LOCAL_SKELETON
    recipe = _load_recipe_or_empty(cwd)
    local = LocalConfig.load(path)
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
    path.write_text(rendered)


def _prepare_target_remove(cwd: Path, dtype: str, variant: str) -> tuple[dict[str, Any], Path, str]:
    recipe = _load_recipe_or_empty(cwd)
    if variant in recipe.targets.get(dtype, {}):
        raise DeviceError(
            f"`{dtype}.{variant}` is declared in the recipe; edit {RECIPE_NAME} to remove it"
        )
    path = cwd / LOCAL_NAME
    if not path.exists():
        raise DeviceError(f"no target `{dtype}.{variant}` in {LOCAL_NAME}")
    spec = LocalConfig.load(path).targets.get(dtype, {}).get(variant)
    if spec is None:
        raise DeviceError(f"no target `{dtype}.{variant}` in {LOCAL_NAME}")

    from .tomlio import target_remove_text  # noqa: PLC0415

    new_text = target_remove_text(path.read_text(), dtype, variant)
    if new_text is None:
        raise DeviceError(f"no target `{dtype}.{variant}` in {LOCAL_NAME}")
    return spec, path, new_text


def target_remove(cwd: Path, dtype: str, variant: str) -> None:
    _spec, path, new_text = _prepare_target_remove(cwd, dtype, variant)
    path.write_text(new_text)


def global_target_add(dtype: str, variant: str, fields: dict[str, str | None]) -> Path:
    if dtype not in TARGET_TYPES:
        raise DeviceError(f"target type `{dtype}` must be one of: {', '.join(TARGET_TYPES)}")
    if not TARGET_VARIANT_RE.match(variant):
        raise DeviceError(f"variant `{variant}` must match [A-Za-z][A-Za-z0-9_-]*")
    validated_fields = _validate_target_fields(dtype, variant, fields)

    path = _global_config_path()
    existing_text = path.read_text() if path.exists() else GLOBAL_SKELETON
    if variant in GlobalConfig.load(path).targets.get(dtype, {}):
        raise DeviceError(f"target `{dtype}.{variant}` already exists in {path}; remove it first")

    from .tomlio import target_add_text  # noqa: PLC0415

    rendered = target_add_text(existing_text, dtype, variant, validated_fields)
    GlobalConfig.parse(rendered, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered)
    return path


def global_target_remove(dtype: str, variant: str) -> Path:
    from .tomlio import target_remove_text  # noqa: PLC0415

    path = _global_config_path()
    if not path.exists() or variant not in GlobalConfig.load(path).targets.get(dtype, {}):
        raise DeviceError(f"no target `{dtype}.{variant}` in {path}")
    new_text = target_remove_text(path.read_text(), dtype, variant)
    if new_text is None:
        raise DeviceError(f"no target `{dtype}.{variant}` in {path}")
    path.write_text(new_text)
    return path
