"""argcomplete-backed shell completion for the `splash` CLI.

All completion logic lives here so cli.py stays focused. Completers are
read-only and fail-silent: they run on every <Tab>, so they must never raise
or print. Anything that goes wrong (malformed toml, name collisions) yields no
suggestions rather than a traceback that would corrupt the shell line.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from .constants import LOCAL_NAME, TARGET_TYPES
from .recipe import GlobalConfig, LocalConfig, _global_config_path, load_settings, merged_targets
from .targets import (
    _load_recipe_or_empty,
    _match_target_type_prefix,
    _target_types_for_variant,
)


def _catalog(
    parsed_args: argparse.Namespace, *, include_global: bool = True
) -> dict[str, dict[str, dict[str, Any]]]:
    """Load the merged target catalog for the completion cwd. With `include_global`
    (default) global config folds in; pass False for a project-only view.

    Mirrors cli._resolve_cwd: honour the top-level --cwd flag if already typed,
    else $PWD, and resolve() so the key matches what the command will act on.
    A malformed global config raises here but both callers swallow it (a completer
    must never raise), so completion just yields nothing.
    """
    cwd = Path(getattr(parsed_args, "cwd", None) or os.getcwd()).resolve()
    recipe = _load_recipe_or_empty(cwd)
    local = LocalConfig.load(cwd / LOCAL_NAME)
    glob = GlobalConfig.load(_global_config_path()) if include_global else None
    return merged_targets(recipe, local, glob)


def variant_completer(prefix: str, parsed_args: argparse.Namespace, **kwargs: object) -> list[str]:
    """Slot 2: variant names for the typed/inferred target type."""
    try:
        catalog = _catalog(parsed_args)
        dtype = getattr(parsed_args, "dtype", None)
        if dtype:
            project = [
                target_type
                for target_type, variants in _catalog(parsed_args, include_global=False).items()
                if variants
            ]
            resolved_type = dtype if dtype in TARGET_TYPES else None
            if (
                resolved_type is None
                and load_settings(
                    Path(getattr(parsed_args, "cwd", None) or os.getcwd()).resolve()
                ).prefix_match
            ):
                resolved_type = _match_target_type_prefix(dtype, project)
            if resolved_type is None:
                return []
            names = set(catalog.get(resolved_type, {}))
        else:
            non_empty = [v for v in catalog.values() if v]
            if len(non_empty) == 1:
                names = set(non_empty[0])
            else:
                names = {n for v in catalog.values() for n in v}
        return sorted(n for n in names if n.startswith(prefix))
    except Exception:  # noqa: BLE001 — a completer must never raise on <Tab>
        return []


def device_arg_completer(
    prefix: str, parsed_args: argparse.Namespace, **kwargs: object
) -> list[str]:
    """Slot 1 for run/start/stop/destroy: declared type name(s) plus variant
    names that identify exactly one type, so `splash run <TAB>` offers every
    unambiguous shorthand."""
    try:
        catalog = _catalog(parsed_args)
        declared = [t for t, v in catalog.items() if v]
        cands = set(declared)
        project = [t for t, v in _catalog(parsed_args, include_global=False).items() if v]
        prefix_match = load_settings(
            Path(getattr(parsed_args, "cwd", None) or os.getcwd()).resolve()
        ).prefix_match
        variants = {name for entries in catalog.values() for name in entries}
        cands |= {
            name
            for name in variants
            if len(_target_types_for_variant(catalog, name)) == 1
            and name not in TARGET_TYPES
            and not (prefix_match and _match_target_type_prefix(name, project))
        }
        return sorted(c for c in cands if c.startswith(prefix))
    except Exception:  # noqa: BLE001 — a completer must never raise on <Tab>
        return []


def physical_variant_completer(
    prefix: str, parsed_args: argparse.Namespace, **kwargs: object
) -> list[str]:
    """Configured physical target names only; never inspect devices or claims."""
    try:
        return sorted(
            name for name in _catalog(parsed_args).get("device", {}) if name.startswith(prefix)
        )
    except Exception:  # noqa: BLE001 — a completer must never raise on <Tab>
        return []


def available_platform_completer(
    prefix: str, parsed_args: argparse.Namespace, **kwargs: object
) -> list[str]:
    return [platform for platform in ("ios", "android", "any") if platform.startswith(prefix)]


def install(parser: argparse.ArgumentParser) -> None:
    """Enable argcomplete for `parser`. No-op (and no import) unless a shell
    completion is actively running, so the normal/hook path pays nothing."""
    if "_ARGCOMPLETE" not in os.environ:
        return
    import argcomplete  # noqa: PLC0415

    argcomplete.autocomplete(parser)
