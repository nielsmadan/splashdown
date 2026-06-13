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

from . import LOCAL_NAME
from .devices import _load_recipe_or_empty
from .recipe import LocalConfig, merged_targets


def _catalog(parsed_args: argparse.Namespace) -> dict[str, dict[str, dict[str, Any]]]:
    """Load the merged recipe+local target catalog for the completion cwd.

    Mirrors cli._resolve_cwd: honour the top-level --cwd flag if already typed,
    else $PWD, and resolve() so the key matches what the command will act on.
    """
    cwd = Path(getattr(parsed_args, "cwd", None) or os.getcwd()).resolve()
    recipe = _load_recipe_or_empty(cwd)
    local = LocalConfig.load(cwd / LOCAL_NAME)
    return merged_targets(recipe, local)


def variant_completer(prefix: str, parsed_args: argparse.Namespace, **kwargs: object) -> list[str]:
    """Slot 2: variant names for the typed/inferred target type."""
    try:
        catalog = _catalog(parsed_args)
        dtype = getattr(parsed_args, "dtype", None)
        if dtype and dtype in catalog:
            names = set(catalog[dtype])
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
    names when exactly one type is declared, so `splash run <TAB>` offers
    variants in the common inferred-type case."""
    try:
        catalog = _catalog(parsed_args)
        declared = [t for t, v in catalog.items() if v]
        cands = set(declared)
        if len(declared) == 1:
            cands |= set(catalog[declared[0]])
        return sorted(c for c in cands if c.startswith(prefix))
    except Exception:  # noqa: BLE001 — a completer must never raise on <Tab>
        return []


def install(parser: argparse.ArgumentParser) -> None:
    """Enable argcomplete for `parser`. No-op (and no import) unless a shell
    completion is actively running, so the normal/hook path pays nothing."""
    if "_ARGCOMPLETE" not in os.environ:
        return
    import argcomplete  # noqa: PLC0415

    argcomplete.autocomplete(parser)
