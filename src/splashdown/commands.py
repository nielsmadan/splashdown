from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, NamedTuple

from . import ENV_FILE_NAME, ENV_NAME_RE, LOCAL_NAME, RECIPE_NAME, TARGET_TYPES
from .agentdocs import remove_agent_guidance, sync_agent_guidance
from .capabilities import translate_tool_errors, warn_capability
from .devices import (
    DeviceError,
    DeviceHealth,
    _android_bin,
    _device_status_for_row,
    _ios_current_state,
    _is_orphan_device,
    _load_recipe_or_empty,
    _prepare_target_remove,
    _resolve_device_name,
    _short_path,
    _summary_string,
    _xcrun_json,
    android_boot,
    android_destroy,
    android_shutdown,
    device_destroy,
    device_destroy_row,
    device_health,
    device_needs_recreate,
    device_run,
    device_shutdown,
    device_status,
    ensure_fresh_sim,
    global_target_add,
    global_target_remove,
    ios_boot,
    ios_destroy,
    ios_shutdown,
    physical_status,
    target_add,
    validate_device_run,
)
from .errors import CapabilityError
from .hooks import (
    _ensure_gitignore,
    _ensure_post_checkout_hook,
    _remove_post_checkout_hook,
    _revert_gitignore,
)
from .loaders import LOADERS
from .provisioning import (
    clear_writer_destinations,
    provision,
    run_setup,
    write_outputs,
)
from .recipe import (
    LOCAL_SKELETON,
    GlobalConfig,
    LocalConfig,
    Recipe,
    TemplateError,
    _global_config_path,
    _slug,
    load_settings,
    merged_targets,
    resolve_variant,
)
from .registry import Registry
from .scanner import (
    ProjectInventory,
    Scanner,
    _build_resource_catalog,
    _detect_loader,
    _merge_app_targets,
    _prune_unresolvable_templates,
    _should_defer_monorepo,
)
from .wiring import (
    _resolve_doctor_framework,
    _wiring_checks_for_framework,
    cmd_doctor,
)


def _gather_resource_entries(
    co_path: Path, *, co_exists: bool, resources: dict[str, str]
) -> list[dict[str, str]]:
    """Resource rows with port liveness tagged. Port-state needs port-typed-
    resource knowledge, so read the recipe when the checkout path still exists."""
    port_keys: set[str] = set()
    if co_exists:
        recipe_path = co_path / RECIPE_NAME
        if recipe_path.exists():
            try:
                rec = Recipe.load(recipe_path)
                port_keys = {n for n, s in rec.resources.items() if s.get("type") == "port"}
            except Exception:  # noqa: BLE001, S110 — malformed recipe shouldn't kill status
                pass

    from .registry import _port_in_use  # noqa: PLC0415

    entries: list[dict[str, str]] = []
    for key, value in sorted(resources.items()):
        state = ""
        if key in port_keys:
            try:
                state = "in use" if _port_in_use(int(value)) else "free"
            except ValueError:
                state = ""
        entries.append({"key": key, "value": value, "port_state": state})
    return entries


class _StatusContext(NamedTuple):
    summary: dict[str, int]
    cache: dict[str, str]
    warned: set[str]


def _new_status_context() -> _StatusContext:
    return _StatusContext(
        summary={
            "defunct_checkouts": 0,
            "defunct_rows": 0,
            "orphan_devices": 0,
            "stale_devices": 0,
            "undeclared_devices": 0,
            "missing_devices": 0,
            "missing_hardware": 0,
        },
        cache={},
        warned=set(),
    )


def _gather_devices_all(
    registry: Registry,
    co: str,
    co_path: Path,
    *,
    check: bool,
    context: _StatusContext,
) -> list[dict[str, Any]]:
    """Device rows sourced from the registry (`--all` mode)."""
    co_exists = co_path.exists()
    entries: list[dict[str, Any]] = []
    for row in registry.devices_for(co):
        health: DeviceHealth | None = None
        try:
            try:
                status = _device_status_for_row(row)
            except CapabilityError:
                raise
            except DeviceError as error:
                status = f"error: {error}"
            if check and co_exists:
                spec = _load_variant_spec(co_path, row.dtype, row.variant)
                health = device_health(
                    registry,
                    co_path,
                    row.dtype,
                    row.variant,
                    spec,
                    cache=context.cache,
                )
        except CapabilityError as error:
            warn_capability(error, context.warned)
            status = "unavailable"
        orphan = stale = undeclared = False
        if health is not None:
            if health is DeviceHealth.ORPHAN:
                orphan = True
                context.summary["orphan_devices"] += 1
            elif health is DeviceHealth.DRIFTED:
                stale = True
                context.summary["stale_devices"] += 1
            elif health is DeviceHealth.UNDECLARED:
                undeclared = True
                context.summary["undeclared_devices"] += 1
        entries.append(
            {
                "type": row.dtype,
                "variant": row.variant,
                "source": "",
                "device_name": row.udid,
                "status": status,
                "orphan": orphan,
                "stale": stale,
                "undeclared": undeclared,
                "missing": False,
            }
        )
    return entries


def _target_source(
    dtype: str, variant: str, recipe: Recipe, local: LocalConfig, glob: GlobalConfig
) -> str:
    """Which scope a merged variant came from, annotating a project variant that
    shadows a same-named global one (the project always wins the collision)."""
    if variant in recipe.targets.get(dtype, {}):
        base = "recipe"
    elif variant in local.targets.get(dtype, {}):
        base = "local"
    else:
        return "global"
    return f"{base} (shadows global)" if variant in glob.targets.get(dtype, {}) else base


def _gather_targets_declared(
    registry: Registry,
    co: str,
    co_path: Path,
    *,
    check: bool,
    context: _StatusContext,
) -> list[dict[str, Any]]:
    """Device rows sourced from recipe + local + global catalog (default mode)."""
    recipe = _load_recipe_or_empty(co_path)
    local = LocalConfig.load(co_path / LOCAL_NAME)
    glob = GlobalConfig.load(_global_config_path())
    entries: list[dict[str, Any]] = []
    for dtype, variants in merged_targets(recipe, local, glob).items():
        for variant, spec in variants.items():
            source = _target_source(dtype, variant, recipe, local, glob)
            if dtype == "device":
                # Hardware has no created instance; show its selector + live state.
                resolved = spec.get("id") or spec.get("name") or spec.get("platform") or "auto"
            else:
                resolved = _resolve_device_name(spec, co_path, variant, dtype)
            health: DeviceHealth | None = None
            try:
                try:
                    status = (
                        physical_status(spec, warned=context.warned)
                        if dtype == "device"
                        else device_status(dtype, resolved)
                    )
                except CapabilityError:
                    raise
                except DeviceError as error:
                    status = f"error: {error}"
                if check:
                    health = device_health(
                        registry,
                        co_path,
                        dtype,
                        variant,
                        spec,
                        cache=context.cache,
                    )
            except CapabilityError as error:
                warn_capability(error, context.warned)
                status = "unavailable"
            orphan = stale = missing = False
            if health is not None:
                if dtype == "device" and status == "absent":
                    missing = True
                    context.summary["missing_hardware"] += 1
                elif health is DeviceHealth.MISSING:
                    missing = True
                    context.summary["missing_devices"] += 1
                elif health is DeviceHealth.ORPHAN:
                    orphan = True
                    context.summary["orphan_devices"] += 1
                elif health is DeviceHealth.DRIFTED:
                    stale = True
                    context.summary["stale_devices"] += 1
            entries.append(
                {
                    "type": dtype,
                    "variant": variant,
                    "source": source,
                    "device_name": resolved,
                    "status": status,
                    "orphan": orphan,
                    "stale": stale,
                    "undeclared": False,
                    "missing": missing,
                }
            )
    return entries


def _gather_status_for_checkout(
    co: str,
    registry: Registry,
    *,
    show_all: bool,
    check: bool,
    context: _StatusContext,
) -> dict[str, Any]:
    """Build the per-checkout block consumed by both JSON serialization and
    text emission. The shared context accumulates repair counts, memoized OS
    lookups, and capability warnings across checkouts."""
    co_path = Path(co)
    co_exists = co_path.exists()
    resources = registry.all_for(co)
    if check and not co_exists:
        context.summary["defunct_checkouts"] += 1
        context.summary["defunct_rows"] += len(resources) + len(registry.devices_for(co))

    res_entries = _gather_resource_entries(co_path, co_exists=co_exists, resources=resources)

    # Device entries. In --all mode, source = registry only. In default mode,
    # source = recipe + local catalog.
    if show_all:
        dev_entries = _gather_devices_all(
            registry,
            co,
            co_path,
            check=check,
            context=context,
        )
    elif co_exists:
        dev_entries = _gather_targets_declared(
            registry,
            co,
            co_path,
            check=check,
            context=context,
        )
    else:
        dev_entries = []

    return {
        "checkout": co,
        "exists": co_exists,
        "resources": res_entries,
        "targets": dev_entries,
    }


def _emit_status_block_text(block: dict[str, Any], *, show_all: bool) -> None:
    """Emit one per-checkout text block to stderr."""
    header_tag = "  [defunct]" if not block["exists"] else ""
    if show_all:
        print(f"=== {block['checkout']}{header_tag} ===", file=sys.stderr)
    else:
        print(f"checkout: {block['checkout']}{header_tag}", file=sys.stderr)
    print("resources:", file=sys.stderr)
    if not block["resources"]:
        print("  (none)", file=sys.stderr)
    for r in block["resources"]:
        suffix = f"  [{r['port_state']}]" if r["port_state"] else ""
        print(f"  {r['key']}={r['value']}{suffix}", file=sys.stderr)
    print("targets:", file=sys.stderr)
    if not block["targets"]:
        print("  (none)", file=sys.stderr)
    for d in block["targets"]:
        cols = [f"{d['type']}.{d['variant']}"]
        if d["source"]:
            cols.append(d["source"])
        cols.append(d["device_name"])
        cols.append(d["status"])
        if d["orphan"]:
            cols.append("[orphan]")
        elif d.get("stale"):
            cols.append("[stale]")
        elif d.get("undeclared"):
            cols.append("[undeclared]")
        elif d.get("missing"):
            cols.append("[missing]")
        print("  " + "\t".join(cols), file=sys.stderr)
    if show_all:
        print("", file=sys.stderr)


class _StatusRow(NamedTuple):
    path: str
    summary: str
    status: str


def _cmd_status_table(
    checkouts: list[str], registry: Registry, check: bool, context: _StatusContext
) -> int:
    """Compact one-row-per-checkout view for `splash status --all`."""
    rows: list[_StatusRow] = []
    for co in checkouts:
        counts = registry.summary_for(co)
        path_label = _short_path(co)
        summary_str = _summary_string(counts)
        co_exists = Path(co).exists()

        status_label = ""
        if not co_exists:
            status_label = "defunct"
            if check:
                context.summary["defunct_checkouts"] += 1
                context.summary["defunct_rows"] += sum(counts.values())
        elif check:
            for row in registry.devices_for(co):
                try:
                    spec = _load_variant_spec(Path(co), row.dtype, row.variant)
                    health = device_health(
                        registry,
                        Path(co),
                        row.dtype,
                        row.variant,
                        spec,
                        cache=context.cache,
                    )
                except CapabilityError as error:
                    warn_capability(error, context.warned)
                    status_label = status_label or "unavailable"
                    continue
                if health is DeviceHealth.ORPHAN:
                    context.summary["orphan_devices"] += 1
                    status_label = "orphan"
                    continue
                if health is DeviceHealth.DRIFTED:
                    context.summary["stale_devices"] += 1
                    status_label = status_label or "stale"
                elif health is DeviceHealth.UNDECLARED:
                    context.summary["undeclared_devices"] += 1
                    status_label = status_label or "undeclared"

        rows.append(_StatusRow(path_label, summary_str, status_label))

    path_width = max((len(r.path) for r in rows), default=4)
    path_width = max(path_width, len("PATH"))
    summary_width = max((len(r.summary) for r in rows), default=7)
    summary_width = max(summary_width, len("SUMMARY"))

    # ISSUE column only appears when at least one row flags something. Empty
    # cells across the board would just be dead width.
    has_issue = any(r.status for r in rows)
    if has_issue:
        fmt_row = f"{{:<{path_width}}}  {{:<{summary_width}}}  {{}}"
        print(fmt_row.format("PATH", "SUMMARY", "ISSUE").rstrip(), file=sys.stderr)
        for path_label, summary_str, status_label in rows:
            print(fmt_row.format(path_label, summary_str, status_label).rstrip(), file=sys.stderr)
    else:
        fmt_row = f"{{:<{path_width}}}  {{}}"
        print(fmt_row.format("PATH", "SUMMARY").rstrip(), file=sys.stderr)
        for path_label, summary_str, _ in rows:
            print(fmt_row.format(path_label, summary_str).rstrip(), file=sys.stderr)

    if check:
        print("", file=sys.stderr)
        _print_check_summary(context.summary)

    return 0


def _print_check_summary(summary: dict[str, int]) -> None:
    """Emit the `--check` footer used by both cmd_status branches: a counts
    block plus hints routed to whatever fixes each issue, or `all entries
    verified` when clean."""
    defunct = summary.get("defunct_checkouts", 0)
    orphan = summary.get("orphan_devices", 0)
    stale = summary.get("stale_devices", 0)
    undeclared = summary.get("undeclared_devices", 0)
    missing = summary.get("missing_devices", 0)
    missing_hw = summary.get("missing_hardware", 0)
    if not (defunct or orphan or stale or undeclared or missing or missing_hw):
        print("Summary: all entries verified.", file=sys.stderr)
        return
    print("Summary:", file=sys.stderr)
    if defunct:
        rows = summary.get("defunct_rows", 0)
        print(
            f"  {defunct} defunct checkout{'s' if defunct != 1 else ''} "
            f"({rows} registry row{'s' if rows != 1 else ''}).",
            file=sys.stderr,
        )
    if orphan:
        print(
            f"  {orphan} orphan device{'s' if orphan != 1 else ''} (underlying sim/AVD deleted).",
            file=sys.stderr,
        )
    if stale:
        print(
            f"  {stale} stale device{'s' if stale != 1 else ''} (declared target drifted).",
            file=sys.stderr,
        )
    if undeclared:
        print(
            f"  {undeclared} undeclared device row{'s' if undeclared != 1 else ''}.",
            file=sys.stderr,
        )
    if missing:
        print(
            f"  {missing} missing device{'s' if missing != 1 else ''} "
            f"(declared but not yet created).",
            file=sys.stderr,
        )
    if missing_hw:
        print(
            f"  {missing_hw} unplugged physical device{'s' if missing_hw != 1 else ''} "
            f"(declared but not connected).",
            file=sys.stderr,
        )
    # Route each hint to the command that actually fixes it. `splash gc` does NOT
    # recreate an orphan whose checkout still exists — `target refresh` does.
    if defunct:
        print("  Run `splash gc` to drop dead checkouts.", file=sys.stderr)
    if orphan or stale or undeclared:
        print("  Run `splash target refresh` to reconcile.", file=sys.stderr)
    if missing:
        print("  Run `splash run` to provision.", file=sys.stderr)
    if missing_hw:
        print(
            "  Connect the device (check pairing/USB) — splashdown can't create hardware.",
            file=sys.stderr,
        )


def cmd_status(
    cwd: Path,
    registry: Registry,
    fmt: str,
    *,
    show_all: bool = False,
    check: bool = False,
    verbose: bool = False,
) -> int:
    """Show resolved vars + declared devices.

    Default mode: just this checkout, recipe-aware device labels.
    --all: compact one-row-per-checkout table.
    --all --verbose: today's per-block view (resources + devices spelled out).
    --check: tag defunct checkouts and orphan device rows, print a `splash gc`
    cleanup hint footer. Composes with both --all variants."""
    target = str(cwd.resolve())
    checkouts = registry.all_checkouts() if show_all else [target]
    if not checkouts:
        checkouts = [target]
    context = _new_status_context()

    # JSON shape is fixed regardless of verbose — consumers want the data, not
    # a table layout. Text branches: --all without --verbose emits a table;
    # everything else falls through to the per-block emitter below.
    if show_all and not verbose and fmt != "json":
        return _cmd_status_table(checkouts, registry, check, context)

    summary = context.summary
    blocks = [
        _gather_status_for_checkout(
            co,
            registry,
            show_all=show_all,
            check=check,
            context=context,
        )
        for co in checkouts
    ]

    if fmt == "json":
        payload: dict[str, Any] = {"checkouts": blocks} if show_all else blocks[0]
        if check:
            payload["summary"] = summary
        print(json.dumps(payload, indent=2))
        return 0

    for block in blocks:
        _emit_status_block_text(block, show_all=show_all)

    if check:
        _print_check_summary(summary)
    elif not show_all:
        stale = sum(
            1
            for r in registry._read_ports()  # noqa: SLF001
            if not Path(r[1]).exists()
        ) + sum(
            1
            for r in registry._read_kv()  # noqa: SLF001
            if not Path(r[0]).exists()
        )
        if stale:
            print(f"stale registry rows: {stale} (run `splash gc` to clean)", file=sys.stderr)

        # Unfilled `set` resources never reach the registry, so they don't appear
        # above — point the user at the command that fills them.
        recipe = _load_recipe_or_empty(cwd)
        resolved_keys = registry.all_for(target)
        unfilled = [
            name
            for name, spec in recipe.resources.items()
            if spec.get("type") == "set"
            and spec.get("default") is None
            and name not in resolved_keys
        ]
        if unfilled:
            print(
                f"{len(unfilled)} resource(s) need a value "
                f"({', '.join(unfilled)}): run `splash env set NAME=VALUE`",
                file=sys.stderr,
            )

    return 0


def cmd_targets_list(cwd: Path, fmt: str) -> int:
    """List declared device variants and their live instance state."""
    _dev_status = device_status
    recipe = _load_recipe_or_empty(cwd)
    local = LocalConfig.load(cwd / LOCAL_NAME)
    glob = GlobalConfig.load(_global_config_path())
    catalog = merged_targets(recipe, local, glob)
    if not catalog:
        print(f"(no targets declared in {RECIPE_NAME} or {LOCAL_NAME})", file=sys.stderr)
        return 0
    rows: list[tuple[str, str, str, str, str]] = []
    _phys_status = physical_status
    warned: set[str] = set()
    for dtype, variants in catalog.items():
        for variant, spec in variants.items():
            source = _target_source(dtype, variant, recipe, local, glob)
            if dtype == "device":
                # Hardware has no created instance name; show its selector
                # (id/name/platform or "auto") and live connection state.
                resolved = spec.get("id") or spec.get("name") or spec.get("platform") or "auto"
            else:
                resolved = _resolve_device_name(spec, cwd, variant, dtype)
            try:
                status = (
                    _phys_status(spec, warned=warned)
                    if dtype == "device"
                    else _dev_status(dtype, resolved)
                )
            except CapabilityError as error:
                warn_capability(error, warned)
                status = "unavailable"
            except DeviceError as error:
                status = f"error: {error}"
            rows.append((dtype, variant, source, resolved, status))
    if fmt == "json":
        print(
            json.dumps(
                [
                    dict(
                        zip(("type", "variant", "source", "device_name", "status"), r, strict=False)
                    )
                    for r in rows
                ],
                indent=2,
            )
        )
    else:
        for dtype, variant, source, resolved, status in rows:
            print(f"{dtype}\t{variant}\t{source}\t{resolved}\t{status}")
    return 0


def _load_variant_spec(
    cwd: Path, dtype: str, variant: str, glob: GlobalConfig | None = None
) -> dict[str, Any] | None:
    """Look up a variant's current spec from a checkout's recipe + local + global
    config. Returns None if the variant is declared nowhere. Every existing config
    is loaded strictly so malformed input can never look like an empty catalog to a
    destructive caller. `cmd_target_refresh` passes the global config after loading
    it once for the fleet-wide sweep."""
    recipe = _load_recipe_or_empty(cwd)
    local = LocalConfig.load(cwd / LOCAL_NAME)
    if glob is None:
        glob = GlobalConfig.load(_global_config_path())
    return merged_targets(recipe, local, glob).get(dtype, {}).get(variant)


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


def cmd_target_gc(registry: Registry, *, warned: set[str] | None = None) -> int:
    """Destroy the sims/AVDs of dead checkouts (whose dir is gone) and drop their
    rows. Returns the number of device rows removed. Reconciling *live* checkouts
    against their recipes — recreating stale/missing devices — is
    `cmd_target_refresh`'s job, not gc's."""
    destroyed_count = 0
    warning_keys = warned if warned is not None else set()
    rows = list(registry.all_devices())
    total = len(rows)
    for i, row in enumerate(rows, 1):
        _emit_progress("gc", i, total)
        try:
            if Path(row.checkout).exists():
                if not _is_orphan_device(row):
                    continue
            else:
                device_destroy_row(row)
            registry.remove_device(row.checkout, row.dtype, row.variant)
            destroyed_count += 1
        except CapabilityError as error:
            warn_capability(error, warning_keys)
    _finish_progress()
    return destroyed_count


_COMPLETION_SHELLS = ("bash", "zsh")


def _detect_shell() -> str:
    """Shell basename from $SHELL, for `splash completion` with no argument."""
    return Path(os.environ.get("SHELL", "")).name or "bash"


def cmd_completion(shell: str | None) -> int:
    """Print shell-completion registration for `eval "$(splash completion)"`.

    splash bundles argcomplete, so this emits the full shellcode itself: no
    separately-installed `register-python-argcomplete`, and for zsh no
    `bashcompinit`. Autodetects the shell from $SHELL when not given."""
    shell = shell or _detect_shell()
    if shell not in _COMPLETION_SHELLS:
        print(
            f"splash completion: unsupported shell {shell!r} "
            f"(supported: {', '.join(_COMPLETION_SHELLS)})",
            file=sys.stderr,
        )
        return 2
    import argcomplete  # noqa: PLC0415

    # argcomplete doesn't export shellcode in its typed surface (autocomplete is).
    code = argcomplete.shellcode(["splash"], shell=shell)  # type: ignore[attr-defined]
    print(code)
    return 0


def cmd_gc(registry: Registry) -> int:
    """Drop every dead-checkout entry machine-wide: destroy orphaned sims/AVDs,
    then prune port/kv/device rows and reconcile live checkouts to their recipes."""
    warned: set[str] = set()
    reconciled = cmd_target_gc(registry, warned=warned)
    n = registry.gc(include_devices=False)
    print(
        f"gc: removed {n} registry entries, reconciled {reconciled} device row(s)",
        file=sys.stderr,
    )
    return 0


_PLATFORM_OF_DTYPE = {"simulator": "ios", "emulator": "android"}


def _handle_optional_capability(
    error: CapabilityError, *, skip_unavailable: bool, warned: set[str]
) -> None:
    if not skip_unavailable:
        raise error
    warn_capability(error, warned)


def cmd_target_refresh(
    registry: Registry,
    *,
    platforms: tuple[str, ...] = ("ios", "android"),
    skip_unavailable: bool = False,
) -> int:
    """Eagerly reconcile every splashdown-managed device to its declared spec.

    Recreates each sim/AVD that is stale (declared `latest`, older OS now
    available) or missing-but-declared (incl. pinned variants whose sim was
    hand-deleted). Fresh ones are left alone. Rows for defunct checkouts or
    variants no longer declared are dropped (their sim destroyed). Recreation
    leaves the new sim Shutdown — nothing is booted, so no concurrency limits
    apply."""
    _fresh_sim = ensure_fresh_sim
    recreated = unchanged = dropped = 0
    cache: dict[str, str] = {}
    warned: set[str] = set()
    # Load the global config ONCE, up front and unguarded: a malformed global
    # config must abort the whole sweep here, not make every globally-sourced
    # device look undeclared and get destroyed row-by-row below.
    glob = GlobalConfig.load(_global_config_path())
    rows = [r for r in registry.all_devices() if _PLATFORM_OF_DTYPE.get(r.dtype) in platforms]
    resolved_rows = [
        (
            row,
            _load_variant_spec(Path(row.checkout), row.dtype, row.variant, glob=glob)
            if Path(row.checkout).exists()
            else None,
        )
        for row in rows
    ]
    total = len(resolved_rows)
    for i, (row, spec) in enumerate(resolved_rows, 1):
        _emit_progress("target refresh", i, total)
        cwd = Path(row.checkout)
        try:
            if spec is None:
                device_destroy_row(row)
                registry.remove_device(row.checkout, row.dtype, row.variant)
                dropped += 1
                continue
            will_recreate = device_needs_recreate(
                registry, cwd, row.dtype, row.variant, spec, cache=cache
            )
            _fresh_sim(registry, cwd, row.dtype, row.variant, spec, cache=cache)
            if will_recreate:
                recreated += 1
            else:
                unchanged += 1
        except CapabilityError as error:
            _handle_optional_capability(error, skip_unavailable=skip_unavailable, warned=warned)
    _finish_progress()
    print(
        f"target refresh: recreated {recreated}, unchanged {unchanged}, dropped {dropped}",
        file=sys.stderr,
    )
    return 0


def _discover_foreign_ios(managed: set[str]) -> list[tuple[str, str, str]]:
    """Available simulators not in the registry, as (udid, name, runtime)."""
    _xcrun = _xcrun_json
    data = _xcrun(["simctl", "list", "devices", "-j"])
    foreign: list[tuple[str, str, str]] = []
    for runtime, devs in (data.get("devices") or {}).items():
        for d in devs:
            udid = d.get("udid")
            if not udid or udid in managed:
                continue
            if not d.get("isAvailable", True):
                continue
            foreign.append((udid, d.get("name", "?"), runtime))
    return foreign


def _discover_foreign_avds(managed: set[str]) -> list[str]:
    """AVD names not in the registry."""
    try:
        with translate_tool_errors(
            "android", "avdmanager", "install Android SDK command-line tools"
        ):
            out = subprocess.check_output(
                [_android_bin("avdmanager"), "list", "avd", "-c"],
                stderr=subprocess.DEVNULL,
            )
    except subprocess.CalledProcessError as error:
        raise DeviceError(f"avdmanager list failed: exit {error.returncode}") from error
    return [
        name for line in out.decode().splitlines() if (name := line.strip()) and name not in managed
    ]


def cmd_target_prune(
    registry: Registry,
    *,
    yes: bool = False,
    dry_run: bool = False,
    platforms: tuple[str, ...] = ("ios", "android"),
    skip_unavailable: bool = False,
) -> int:
    """Destroy every sim/AVD on this machine that splashdown did NOT create.
    Picks up the Xcode default-template pile, hand-made sims, etc.

    Splashdown-managed entries (those in the registry) are always preserved.
    Use --dry-run to preview, --yes to skip the prompt."""
    _ios_shut = ios_shutdown
    _ios_del = ios_destroy
    _avd_shut = android_shutdown
    _avd_del = android_destroy
    managed = registry.managed_udids()
    warned: set[str] = set()
    foreign_ios: list[tuple[str, str, str]] = []
    foreign_avd: list[str] = []
    if "ios" in platforms:
        try:
            foreign_ios = _discover_foreign_ios(managed)
        except CapabilityError as error:
            _handle_optional_capability(error, skip_unavailable=skip_unavailable, warned=warned)
    if "android" in platforms:
        try:
            foreign_avd = _discover_foreign_avds(managed)
        except CapabilityError as error:
            _handle_optional_capability(error, skip_unavailable=skip_unavailable, warned=warned)

    total = len(foreign_ios) + len(foreign_avd)
    if total == 0:
        print(
            "target prune: nothing to remove (every sim/AVD is splashdown-managed)", file=sys.stderr
        )
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
        print("target prune: --dry-run, nothing destroyed", file=sys.stderr)
        return 0
    if not _confirm("Continue?", yes=yes):
        print("target prune: aborted", file=sys.stderr)
        return 1

    done = 0
    for udid, _name, _runtime in foreign_ios:
        try:
            _ios_shut(udid)
            _ios_del(udid)
            done += 1
            _emit_progress("target prune", done, total)
        except CapabilityError as error:
            _handle_optional_capability(error, skip_unavailable=skip_unavailable, warned=warned)
    for name in foreign_avd:
        try:
            _avd_shut(name)
            _avd_del(name)
            done += 1
            _emit_progress("target prune", done, total)
        except CapabilityError as error:
            _handle_optional_capability(error, skip_unavailable=skip_unavailable, warned=warned)
    _finish_progress()
    print(f"target prune: removed {done} device(s)", file=sys.stderr)
    return 0


def cmd_run(cwd: Path, registry: Registry, dtype: str | None, variant_arg: str | None) -> int:
    """Reconcile the sim, boot it, then build + launch the app via the framework's CLI."""
    _fresh_sim = ensure_fresh_sim
    _boot_ios = ios_boot
    _ios_state = _ios_current_state
    _boot_android = android_boot
    _dev_run = device_run
    dtype = _infer_dtype(cwd, dtype)
    variant, spec, recipe = _resolve_variant_for_cli(cwd, dtype, variant_arg)
    kind = _PLATFORM_OF_DTYPE.get(dtype) or spec.get("platform")
    validate_device_run(cwd, recipe, kind)
    info = _fresh_sim(registry, cwd, dtype, variant, spec)
    # Physical devices are already live (discovery returns the running id); only
    # splashdown-owned sims/emulators need booting.
    if not info.get("physical"):
        if info["kind"] == "ios":
            _boot_ios(info["udid"], _ios_state(info["udid"]))
        elif info["kind"] == "android":
            info["serial"] = _boot_android(info["name"])
    return int(_dev_run(cwd, recipe, info))


def cmd_start(cwd: Path, registry: Registry, dtype: str | None, variant_arg: str | None) -> int:
    """Reconcile the sim, then boot it. No build/launch."""
    _fresh_sim = ensure_fresh_sim
    _boot_ios = ios_boot
    _ios_state = _ios_current_state
    _boot_android = android_boot
    dtype = _infer_dtype(cwd, dtype)
    variant, spec, _recipe = _resolve_variant_for_cli(cwd, dtype, variant_arg)
    info = _fresh_sim(registry, cwd, dtype, variant, spec)
    if info.get("physical"):
        print(f"{dtype}.{variant} connected ({info['name']})", file=sys.stderr)
        return 0
    if info["kind"] == "ios":
        _boot_ios(info["udid"], _ios_state(info["udid"]))
    elif info["kind"] == "android":
        info["serial"] = _boot_android(info["name"])
    print(f"started {dtype}.{variant} ({info['name']})", file=sys.stderr)
    return 0


def cmd_stop(cwd: Path, dtype: str | None, variant_arg: str | None) -> int:
    """Shut down the sim/emulator (preserves it for next start)."""
    _dev_shutdown = device_shutdown
    dtype = _infer_dtype(cwd, dtype)
    variant, spec, _recipe = _resolve_variant_for_cli(cwd, dtype, variant_arg)
    if dtype == "device":
        print(
            f"{dtype}.{variant} is hardware splashdown doesn't own; nothing to stop",
            file=sys.stderr,
        )
        return 0
    resolved = _resolve_device_name(spec, cwd, variant, dtype)
    _dev_shutdown(dtype, resolved)
    print(f"stopped {dtype}.{variant} ({resolved})", file=sys.stderr)
    return 0


def _confirm(prompt: str, *, yes: bool) -> bool:
    """Interactive [y/N] gate for destructive ops. `yes=True` skips the prompt."""
    if yes:
        return True
    print(f"{prompt} [y/N] ", end="", file=sys.stderr, flush=True)
    return input().strip().lower() in ("y", "yes")


def cmd_destroy(cwd: Path, dtype: str | None, variant_arg: str | None, *, yes: bool = False) -> int:
    """Delete the sim/emulator and its registry entry."""
    _dev_destroy = device_destroy
    dtype = _infer_dtype(cwd, dtype)
    variant, spec, _recipe = _resolve_variant_for_cli(cwd, dtype, variant_arg)
    if dtype == "device":
        print(
            f"{dtype}.{variant} is hardware splashdown doesn't own; nothing to destroy",
            file=sys.stderr,
        )
        return 0
    if not _confirm(f"Destroy {dtype}.{variant}?", yes=yes):
        print(f"destroy {dtype}.{variant}: aborted", file=sys.stderr)
        return 1
    resolved = _resolve_device_name(spec, cwd, variant, dtype)
    _dev_destroy(dtype, resolved)
    Registry().remove_device(str(cwd.resolve()), dtype, variant)
    print(f"destroyed {dtype}.{variant} ({resolved})", file=sys.stderr)
    return 0


def _declared_target_types(cwd: Path, *, include_global: bool = True) -> list[str]:
    """The target types this checkout has variants for. With `include_global` (the
    default) global config folds in — used where a global device should be
    resolvable. Pass `include_global=False` for TYPE inference and type-prefix
    matching, which must stay scoped to the project's *own* types: an
    always-available global `device` must not make bare `splash run` ambiguous in
    every mobile project, nor claim a short token (`splash run d`) in a sim-only
    repo."""
    recipe = _load_recipe_or_empty(cwd)
    local = LocalConfig.load(cwd / LOCAL_NAME)
    glob = GlobalConfig.load(_global_config_path()) if include_global else None
    return [t for t, variants in merged_targets(recipe, local, glob).items() if variants]


def _infer_dtype(cwd: Path, dtype: str | None) -> str:
    """Resolve an unspecified TYPE arg to the single device type to act on. Scoped
    to the project's own declared types first; only when the project declares none
    do globally-available types count (so bare `splash run` still resolves a lone
    global physical device in an otherwise target-less repo)."""
    if dtype:
        return dtype
    declared = _declared_target_types(cwd, include_global=False) or _declared_target_types(cwd)
    if len(declared) == 1:
        return declared[0]
    if not declared:
        raise DeviceError(f"no targets declared in {RECIPE_NAME} or {LOCAL_NAME}")
    raise DeviceError(
        f"multiple target types declared ({', '.join(sorted(declared))}); "
        f"specify one: {' | '.join(TARGET_TYPES)}"
    )


def _resolve_variant_for_cli(
    cwd: Path, dtype: str, variant_arg: str | None
) -> tuple[str, dict[str, Any], Recipe]:
    """Common prelude for `splash run`/`start`/`stop`/`destroy`: load recipe+local,
    merge, pick variant."""
    recipe = _load_recipe_or_empty(cwd)
    local = LocalConfig.load(cwd / LOCAL_NAME)
    glob = GlobalConfig.load(_global_config_path())
    catalog = merged_targets(recipe, local, glob).get(dtype, {})
    prefix_match = load_settings(cwd).prefix_match
    variant, spec = resolve_variant(catalog, variant_arg, prefix_match=prefix_match)
    return variant, spec, recipe


_NO_LOADER_INSTRUCTIONS = (
    "no shell loader detected — wrote splashdown.env but nothing sources it.\n"
    "  install mise/direnv/devbox and re-run `splash init`, or source it "
    "yourself (e.g. `set -a; . ./splashdown.env; set +a`)"
)


def _path_git_ignored(cwd: Path, name: str) -> bool:
    """True if `name` is gitignored in `cwd`. Best-effort: any git error counts
    as ignored so we never nag spuriously (e.g. outside a repo)."""
    try:
        r = subprocess.run(
            ["git", "check-ignore", "-q", "--", name],
            cwd=cwd,
            capture_output=True,
            check=False,
        )
    except OSError:
        return True
    # check-ignore: 0 = ignored, 1 = not ignored, 128 = fatal (e.g. not a repo).
    # Only the explicit "not ignored" answer should let the warning fire; treat
    # everything else (ignored, or any error) as "don't nag".
    return r.returncode != 1


def _resolve_no_loader_delivery(cwd: Path, inv: ProjectInventory) -> tuple[str | None, str]:
    """Decide how to deliver values when no shell-env loader is detected.

    Returns `(writer, message)`. `writer` is an `envfile=<name>` string to apply
    to the generated resources — chosen by `.env` → `.env.local` precedence and
    only when at least one app actually reads a dotenv file. Otherwise it is
    None, meaning: keep generating `splashdown.env` and tell the user how to make
    it reach their processes. `message` is always printed.
    """
    from .scanner import PROFILES  # noqa: PLC0415

    def reads_dotenv(profile: str) -> bool:
        if profile == "unknown":
            return True  # give the benefit of the doubt; the caveat note covers it
        prof = PROFILES.get(profile)
        return bool(prof and prof.reads_dotenv)

    target = None
    if (cwd / ".env").exists():
        target = ".env"
    elif (cwd / ".env.local").exists():
        target = ".env.local"

    proc_only = [
        app for app in inv.apps if not reads_dotenv(app.profile) or "electron" in app.capabilities
    ]
    file_capable = any(reads_dotenv(app.profile) for app in inv.apps) or not inv.apps

    if target and file_capable:
        msg = f"no shell loader detected — routing values into {target}"
        if proc_only:
            names = ", ".join(a.name for a in proc_only)
            msg += (
                f"\n  note: {names} read env from the process, not {target}; "
                "install mise/direnv/devbox so those pick up values"
            )
        if not _path_git_ignored(cwd, target):
            msg += (
                f"\n  warning: {target} is not gitignored — per-checkout values "
                "will show up as local changes"
            )
        return f"envfile={target}", msg

    return None, _NO_LOADER_INSTRUCTIONS


def _apply_no_loader_fallback(
    cwd: Path, inv: ProjectInventory, merged_resources: dict[str, dict[str, Any]]
) -> str | None:
    """When no loader is detected, route generated resources into a dotenv file
    (where one fits) and return the message to print. Returns None when a loader
    is present — nothing to do."""
    if inv.loader != "none":
        return None
    writer, msg = _resolve_no_loader_delivery(cwd, inv)
    if writer:
        for spec in merged_resources.values():
            spec.setdefault("writer", writer)
    return msg


def _write_minimal_monorepo_recipe(cwd: Path, inv: ProjectInventory) -> None:
    """Defer path: write a structural-only recipe ([project] + [apps.*], no
    resources/targets) plus loader/hook wiring, and tell the user where to look.
    Used when init detects an ambiguous monorepo it should not auto-configure."""
    from .tomlio import render_scanned_recipe  # noqa: PLC0415

    recipe_path = cwd / RECIPE_NAME
    rendered = render_scanned_recipe(inv, {}, {}, cwd)
    Recipe.parse(rendered, recipe_path)
    recipe_path.write_text(rendered)
    print(f"wrote {RECIPE_NAME} (structure only)", file=sys.stderr)
    print(
        f"monorepo detected ({len(inv.apps)} apps) — resources not auto-configured; "
        "see https://splashdown.dev/monorepos/",
        file=sys.stderr,
    )
    local_path = cwd / LOCAL_NAME
    if not local_path.exists():
        local_path.write_text(LOCAL_SKELETON)
        print(f"wrote {LOCAL_NAME} (skeleton)", file=sys.stderr)
    _ensure_gitignore(cwd)
    loader = LOADERS[inv.loader]
    if loader.wire(cwd):
        loader.approve(cwd, announce=True)
    _ensure_post_checkout_hook(cwd)
    sync_agent_guidance(cwd, Recipe.load(recipe_path))


_ELECTRON_PROFILE_RESOURCE = "ELECTRON_PROFILE_ID"


def _add_electron_resources(
    _cwd: Path,
    inv: ProjectInventory,
    res_by_app: dict[str, dict[str, dict[str, Any]]],
    choice: str | None = None,
) -> bool:
    electron_apps = [app for app in inv.apps if "electron" in app.capabilities]
    if choice not in (None, "isolated", "shared"):
        raise ValueError("electron profile choice must be `isolated` or `shared`")
    if not electron_apps:
        if choice is not None:
            raise ValueError("--electron-profile requires a scanner-detected Electron app")
        return False
    if choice == "shared":
        return False
    if choice is None:
        if not sys.stdin.isatty():
            return False
        if len(electron_apps) == 1:
            prompt = "Set up an independent Electron profile for this checkout?"
        else:
            names = ", ".join(app.name for app in electron_apps)
            prompt = f"Set up independent Electron profiles for these checkouts ({names})?"
        print(f"{prompt} [y/N] ", end="", file=sys.stderr, flush=True)
        try:
            answer = input()
        except EOFError:
            return False
        if answer.strip().lower() not in ("y", "yes"):
            return False
    multiple = len(electron_apps) > 1
    for app in electron_apps:
        template = "splashdown-{{ truncate(hash(cwd_abs), 12) }}"
        if multiple:
            template = f"{template}-{_slug(app.name)}"
        res_by_app[app.name][_ELECTRON_PROFILE_RESOURCE] = {
            "type": "template",
            "template": template,
            "writer": "splashdown-env",
        }
    return True


def _print_electron_integration(resource_names: list[str]) -> None:
    print(
        "  Electron: in each main process, before requestSingleInstanceLock():",
        file=sys.stderr,
    )
    print('    import { mkdirSync } from "node:fs"', file=sys.stderr)
    for resource in resource_names:
        print(f"    const profileId = process.env.{resource}", file=sys.stderr)
        print("    if (profileId) {", file=sys.stderr)
        print('      const userData = `${app.getPath("userData")}-${profileId}`', file=sys.stderr)
        print("      mkdirSync(userData, { recursive: true })", file=sys.stderr)
        print('      app.setPath("userData", userData)', file=sys.stderr)
        print("    }", file=sys.stderr)


def _ios_native_schemes(cwd: Path) -> list[str]:
    from .runners import _ios_native_schemes as discover  # noqa: PLC0415

    return discover(cwd)


def _resolve_init_ios_scheme(inv: ProjectInventory, explicit: str | None) -> str | None:
    ios_apps = [app for app in inv.apps if app.profile == "ios-native"]
    if not ios_apps:
        if explicit is not None:
            raise ValueError("--ios-scheme requires a scanner-detected native iOS app")
        return None
    if len(ios_apps) != 1:
        raise DeviceError("ios-native: select a single app before choosing its Xcode scheme")
    if explicit is not None:
        scheme = explicit.strip()
        if not scheme:
            raise ValueError("--ios-scheme must not be empty")
        if scheme.startswith("-"):
            raise ValueError("--ios-scheme must not start with `-`")
        return scheme

    schemes = _ios_native_schemes(ios_apps[0].path)
    if len(schemes) == 1:
        return schemes[0]
    if not schemes:
        raise DeviceError(
            "ios-native: no shared Xcode schemes found; rerun `splash init --ios-scheme NAME`"
        )

    choices = ", ".join(schemes)
    if not sys.stdin.isatty():
        raise DeviceError(
            f"ios-native: multiple shared Xcode schemes found ({choices}); "
            "rerun `splash init --ios-scheme NAME`"
        )
    print(f"Select native iOS scheme ({choices}): ", end="", file=sys.stderr, flush=True)
    try:
        selected = input().strip()
    except EOFError as exc:
        raise DeviceError(
            "ios-native: no Xcode scheme selected; rerun `splash init --ios-scheme NAME`"
        ) from exc
    if selected not in schemes:
        raise DeviceError(
            f"ios-native: unknown Xcode scheme `{selected}`; choose one of: {choices}"
        )
    return selected


def cmd_init(  # noqa: PLR0912 — init orchestrator; one branch per optional integration
    cwd: Path,
    preset: str | None = None,
    force: bool = False,
    loader_override: str | None = None,
    electron_profile: str | None = None,
    ios_scheme: str | None = None,
) -> None:
    """Scaffold splashdown.toml from a project scan (default) or from a named
    intent preset (`splash init <preset>`)."""

    recipe_path = cwd / RECIPE_NAME
    if recipe_path.exists() and not force:
        print(f"refusing to overwrite existing {RECIPE_NAME} (use --overwrite)", file=sys.stderr)
        sys.exit(2)

    if preset is not None:
        if electron_profile is not None:
            raise ValueError("--electron-profile is only valid with scanner-driven `splash init`")
        if ios_scheme is not None:
            raise ValueError("--ios-scheme is only valid with scanner-driven `splash init`")
        return _cmd_init_preset(cwd, preset, loader_override=loader_override)

    # Scanner-driven path.
    from .scanner import PROFILES  # noqa: PLC0415

    inv = Scanner().scan(cwd)
    if loader_override:
        inv = ProjectInventory(workspace=inv.workspace, apps=inv.apps, loader=loader_override)

    print("scanning project…", file=sys.stderr)
    print(
        f"  detected: {inv.workspace} ({'/'.join(a.name for a in inv.apps) or 'no apps'})",
        file=sys.stderr,
    )
    for app in inv.apps:
        rel = app.path.relative_to(cwd) if app.path != cwd else Path(".")
        print(f"  {rel}\t→ {app.profile}", file=sys.stderr)
    print(f"  shell loader\t→ {inv.loader}", file=sys.stderr)
    # Collect per-app resources, then merge with collision-mangling.
    res_by_app: dict[str, dict[str, dict[str, Any]]] = {}
    for app in inv.apps:
        if app.profile == "unknown":
            res_by_app[app.name] = {}
            continue
        res_by_app[app.name] = PROFILES[app.profile].resources(app)
    if _should_defer_monorepo(cwd, res_by_app, inv.apps):
        _write_minimal_monorepo_recipe(cwd, inv)
        return
    electron_isolated = _add_electron_resources(cwd, inv, res_by_app, electron_profile)
    merged_resources, app_resource_names = _build_resource_catalog(res_by_app)
    # Compose is project-level infrastructure, so its resources are merged in after
    # the per-app pass rather than claimed by any one app.
    from .profiles import compose_project_resources  # noqa: PLC0415

    for name, spec in compose_project_resources(cwd).items():
        merged_resources.setdefault(name, spec)
    merged_targets = _merge_app_targets(inv.apps)
    for name in _prune_unresolvable_templates(merged_resources, app_resource_names):
        print(f"  skipped {name}: template references a resource no app declares", file=sys.stderr)

    no_loader_msg = _apply_no_loader_fallback(cwd, inv, merged_resources)
    resolved_ios_scheme = _resolve_init_ios_scheme(inv, ios_scheme)

    from .tomlio import render_scanned_recipe  # noqa: PLC0415

    project_metadata = {"ios": {"scheme": resolved_ios_scheme}} if resolved_ios_scheme else None
    rendered = render_scanned_recipe(
        inv,
        merged_resources,
        app_resource_names,
        cwd,
        merged_targets,
        project_metadata,
    )
    Recipe.parse(rendered, recipe_path)
    recipe_path.write_text(rendered)
    print(f"wrote {RECIPE_NAME}", file=sys.stderr)

    local_path = cwd / LOCAL_NAME
    if not local_path.exists():
        local_path.write_text(LOCAL_SKELETON)
        print(f"wrote {LOCAL_NAME} (skeleton)", file=sys.stderr)

    _ensure_gitignore(cwd)
    loader = LOADERS[inv.loader]
    if loader.wire(cwd):
        loader.approve(cwd, announce=True)
    if no_loader_msg:
        print(f"  {no_loader_msg}", file=sys.stderr)
    _ensure_post_checkout_hook(cwd)
    if electron_isolated:
        resource_names = [
            name
            for app in inv.apps
            if "electron" in app.capabilities
            for name in app_resource_names[app.name]
            if name.startswith(_ELECTRON_PROFILE_RESOURCE)
        ]
        _print_electron_integration(resource_names)

    if any(app.profile != "unknown" for app in inv.apps):
        _apply_init_wiring_checks(inv)
    sync_agent_guidance(cwd, Recipe.load(recipe_path))


def _apply_init_wiring_checks(inv: ProjectInventory) -> None:
    """Apply autofix wiring checks for every known-profile app found during init."""
    from .scanner import PROFILES  # noqa: PLC0415

    for app in inv.apps:
        if app.profile == "unknown":
            continue
        checks = PROFILES[app.profile].wiring_checks(app)
        for check in checks:
            if not check.applies(app.path):
                continue
            status, _ = check.detect(app.path)
            if status != "ok" and check.autofix is not None:
                try:
                    check.autofix(app.path)
                except Exception as e:  # noqa: BLE001
                    print(f"  ✗ {check.id}: autofix failed: {e}", file=sys.stderr)


def _cmd_init_preset(cwd: Path, preset: str, *, loader_override: str | None = None) -> None:
    """`splash init NAME` path: write the intent preset, then wire the
    detected (or overridden) shell-env loader and the post-checkout hook."""
    from .scaffolds import SCAFFOLDS  # noqa: PLC0415

    scaffold = SCAFFOLDS.get(preset)
    if scaffold is None:
        available = sorted(SCAFFOLDS)
        print(f"unknown preset `{preset}`; available: {', '.join(available)}", file=sys.stderr)
        sys.exit(2)
    loader_name = loader_override or _detect_loader(cwd)
    recipe_path = cwd / RECIPE_NAME
    rendered = scaffold.replace("__SPLASH_LOADER__", loader_name)
    Recipe.parse(rendered, recipe_path)
    recipe_path.write_text(rendered)
    print(f"wrote {RECIPE_NAME} (preset={preset})", file=sys.stderr)

    local_path = cwd / LOCAL_NAME
    if not local_path.exists():
        local_path.write_text(LOCAL_SKELETON)
        print(f"wrote {LOCAL_NAME} (skeleton)", file=sys.stderr)

    _ensure_gitignore(cwd)
    loader = LOADERS[loader_name]
    if loader.wire(cwd):
        loader.approve(cwd, announce=True)
    if loader_name == "none":
        # Preset scaffolds are written verbatim, so we can't re-route resources to
        # a dotenv file here — but we must not leave the user with a silent no-op.
        print(f"  {_NO_LOADER_INSTRUCTIONS}", file=sys.stderr)
    _ensure_post_checkout_hook(cwd)
    if preset == "electron":
        _print_electron_integration([_ELECTRON_PROFILE_RESOURCE])

    framework = _resolve_doctor_framework(cwd, None)
    if framework and _wiring_checks_for_framework(framework, cwd):
        print(f"running framework wiring for `{framework}`...", file=sys.stderr)
        cmd_doctor(cwd, fix=True)
    sync_agent_guidance(cwd, Recipe.load(recipe_path))


def cmd_deinit(cwd: Path, registry: Registry) -> int:
    """Remove splashdown from this checkout: reverse `init`'s edits and clear the
    machine-wide state that `sync`/`run` created. Surgical — user-modified files
    are kept with a note rather than clobbered. Framework config patches from
    `doctor --fix` are out of scope (no sentinels, originals not recoverable)."""
    abspath = str(cwd.resolve())

    # Loader name lives in the recipe; read it before we delete the recipe. A
    # broken/legacy recipe must never abort the one command meant to clean it up,
    # so a failed read just degrades to "loader unknown".
    try:
        recipe = _load_recipe_or_empty(cwd)
        loader_name = recipe.project.get("loader")
    except Exception as e:  # noqa: BLE001 — a recipe we can't parse must not block teardown
        print(
            f"warning: could not read {RECIPE_NAME} ({e}); skipping loader un-wiring",
            file=sys.stderr,
        )
        recipe = None
        loader_name = None

    # Iterate registry rows (not recipe variants) so orphaned instances get cleaned
    # up too, destroying each by the identifier its row stores (UDID for sims, AVD
    # name for emulators).
    for row in registry.devices_for(abspath):
        if row.dtype == "device":
            continue  # hardware splashdown doesn't own
        try:
            device_destroy_row(row)
            print(f"destroyed {row.dtype}.{row.variant} ({row.udid})", file=sys.stderr)
        except DeviceError as e:
            print(f"warning: could not destroy {row.dtype}.{row.variant}: {e}", file=sys.stderr)

    removed = registry.release(abspath)
    if removed:
        print(f"released {removed} registry entr{'y' if removed == 1 else 'ies'}", file=sys.stderr)

    # splashdown owns splashdown.env wholesale, so it goes unconditionally.
    env_path = cwd / ENV_FILE_NAME
    if env_path.exists():
        env_path.unlink()
        print(f"removed {ENV_FILE_NAME}", file=sys.stderr)

    # Per-resource `envfile=`/`envrc` writer destinations (e.g. per-app .env files
    # in a monorepo) are user-owned, unlike splashdown.env — remove only our keys
    # and delete the file only if nothing else remains.
    if recipe is not None:
        for relpath, action in clear_writer_destinations(cwd, recipe):
            print(f"{action} {relpath}", file=sys.stderr)

    # `.get` guards an absent/unknown loader name; the "none" loader resolves to a
    # no-op unwire.
    loader = LOADERS.get(loader_name) if loader_name else None
    if loader is not None:
        loader.unwire(cwd)

    _remove_post_checkout_hook(cwd)

    _revert_gitignore(cwd)
    remove_agent_guidance(cwd)

    # Only remove splashdown.local.toml when it's still the untouched skeleton.
    local_path = cwd / LOCAL_NAME
    if local_path.exists():
        if local_path.read_text() == LOCAL_SKELETON:
            local_path.unlink()
            print(f"removed {LOCAL_NAME}", file=sys.stderr)
        else:
            print(f"note: {LOCAL_NAME} was modified — left in place", file=sys.stderr)

    recipe_path = cwd / RECIPE_NAME
    if recipe_path.exists():
        recipe_path.unlink()
        print(f"removed {RECIPE_NAME}", file=sys.stderr)

    print("splashdown removed from this checkout", file=sys.stderr)
    return 0


def cmd_refresh_inventory(cwd: Path) -> int:
    """Re-scan and rewrite [project] / [apps.*] in splashdown.toml; preserve
    [resources.*] sections verbatim. Used both for picking up new apps and for
    upgrading legacy recipes to the new shape."""
    from .scanner import PROFILES  # noqa: PLC0415

    recipe_path = cwd / RECIPE_NAME
    if not recipe_path.exists():
        print(f"no {RECIPE_NAME} in {cwd}; run `splash init` instead", file=sys.stderr)
        return 1
    existing = Recipe.load(recipe_path)
    inv = Scanner().scan(cwd)

    res_by_app: dict[str, dict[str, dict[str, Any]]] = {}
    for app in inv.apps:
        if app.profile == "unknown":
            res_by_app[app.name] = {}
            continue
        res_by_app[app.name] = PROFILES[app.profile].resources(app)
    if any("electron" in app.capabilities for app in inv.apps) and any(
        name.startswith(_ELECTRON_PROFILE_RESOURCE) for name in existing.resources
    ):
        _add_electron_resources(cwd, inv, res_by_app, "isolated")
    profile_emitted, app_resource_names = _build_resource_catalog(res_by_app)
    # Names already in the recipe stay resolvable — refresh_recipe keeps them.
    _prune_unresolvable_templates(profile_emitted, app_resource_names, set(existing.resources))

    from .tomlio import refresh_recipe  # noqa: PLC0415

    rebuilt = refresh_recipe(recipe_path.read_text(), inv, profile_emitted, app_resource_names, cwd)
    Recipe.parse(rebuilt, recipe_path)
    recipe_path.write_text(rebuilt)
    n_resources = len(tomllib.loads(rebuilt).get("resources", {}))
    print(
        f"refreshed {RECIPE_NAME}: {len(inv.apps)} app(s), {n_resources} resource(s)",
        file=sys.stderr,
    )
    sync_agent_guidance(cwd, Recipe.parse(rebuilt, recipe_path))
    return 0


def _cmd_provision(args: Any, cwd: Path, registry: Registry) -> int:
    return _cmd_provision_inner(
        cwd,
        registry,
        reprovision=args.force,
        setup=args.setup,
        fmt=_resolve_format_arg(args),
    )


def _resolve_format_arg(args: Any) -> str:
    return getattr(args, "format", None) or "text"


def _cmd_provision_inner(
    cwd: Path,
    registry: Registry,
    *,
    reprovision: bool = False,
    setup: str | None = None,
    fmt: str = "text",
) -> int:
    abspath = str(cwd.resolve())
    before = registry.all_for(abspath)
    try:
        resolved = provision(cwd, registry=registry, reprovision=reprovision)
        recipe = Recipe.load(cwd / RECIPE_NAME)
        local_path = cwd / LOCAL_NAME
        if not local_path.exists():
            local_path.write_text(LOCAL_SKELETON)
        writer_results = write_outputs(cwd, recipe, resolved)
        setup_msgs = run_setup(cwd, recipe, setup, resolved)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 0
    except (ValueError, TemplateError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    changed_vars = {k: v for k, v in resolved.items() if before.get(k) != v}
    anything_changed = bool(changed_vars) or any(c for _, c in writer_results) or bool(setup_msgs)

    if fmt == "json":
        print(
            json.dumps(
                {
                    "resolved": resolved,
                    "writers": [m for m, _ in writer_results],
                    "setup": setup_msgs,
                    "changed": anything_changed,
                    "changed_keys": sorted(changed_vars),
                },
                indent=2,
            )
        )
        return 0

    if not anything_changed:
        files = sum(1 for m, _ in writer_results if not m.startswith(("stdout:", "registry-only:")))
        print(
            f"splashdown: up to date ({len(resolved)} vars, {files} files)",
            file=sys.stderr,
        )
        return 0

    for key in changed_vars:
        print(f"  {key} (changed)", file=sys.stderr)
    for m, changed in writer_results:
        if changed:
            print(f"  -> {m} (changed)", file=sys.stderr)
    for m in setup_msgs:
        print(f"  -> {m}", file=sys.stderr)
    return 0


def _target_add(args: Any, cwd: Path) -> int:
    fields = {
        "model": args.model,
        "ios": args.ios,
        "device": args.device,
        "image": args.image,
        "name": args.sim_name,
        "id": args.device_id,
        "platform": args.platform,
    }
    if getattr(args, "global_scope", False):
        path = global_target_add(args.dtype, args.variant, fields)
        print(f"added target `{args.dtype}.{args.variant}` to {path}", file=sys.stderr)
        return 0
    target_add(cwd, args.dtype, args.variant, fields)
    print(f"added target `{args.dtype}.{args.variant}` to {LOCAL_NAME}", file=sys.stderr)
    return 0


def _target_remove(args: Any, cwd: Path, registry: Registry) -> int:
    if getattr(args, "global_scope", False):
        path = global_target_remove(args.dtype, args.variant)
        print(
            f"removed target `{args.dtype}.{args.variant}` from {path}; run "
            "`splash target refresh` to reap any now-undeclared instances",
            file=sys.stderr,
        )
        return 0
    variant = args.variant
    recipe = _load_recipe_or_empty(cwd)
    try:
        local = LocalConfig.load(cwd / LOCAL_NAME)
    except ValueError:
        local = LocalConfig({}, cwd / LOCAL_NAME)
    in_project = variant in recipe.targets.get(args.dtype, {}) or variant in local.targets.get(
        args.dtype, {}
    )
    if not in_project and variant in GlobalConfig.load(_global_config_path()).targets.get(
        args.dtype, {}
    ):
        raise DeviceError(
            f"`{args.dtype}.{variant}` is a global variant; "
            "remove it with `splash target remove … --global`"
        )
    spec, local_path, new_local_text = _prepare_target_remove(cwd, args.dtype, variant)
    destroyed = False
    if not args.keep_instance and args.dtype != "device":
        checkout = str(cwd.resolve())
        row = registry.get_device(checkout, args.dtype, variant)
        if row is not None:
            device_destroy_row(row)
        else:
            resolved = _resolve_device_name(spec, cwd, variant, args.dtype)
            device_destroy(args.dtype, resolved)
        local_path.write_text(new_local_text)
        registry.remove_device(checkout, args.dtype, variant)
        destroyed = True
    else:
        local_path.write_text(new_local_text)
    suffix = " (and destroyed the instance)" if destroyed else ""
    print(f"removed target `{args.dtype}.{variant}` from {LOCAL_NAME}{suffix}", file=sys.stderr)
    return 0


def _target_refresh(args: Any, registry: Registry) -> int:
    platforms = ("ios", "android") if args.platform == "all" else (args.platform,)
    return cmd_target_refresh(
        registry,
        platforms=platforms,
        skip_unavailable=args.platform == "all",
    )


def _target_prune(args: Any, registry: Registry) -> int:
    platforms = ("ios", "android") if args.platform == "all" else (args.platform,)
    return cmd_target_prune(
        registry,
        yes=args.yes,
        dry_run=args.dry_run,
        platforms=platforms,
        skip_unavailable=args.platform == "all",
    )


def _target_dispatch(args: Any, cwd: Path, registry: Registry) -> int:
    if args.target_cmd is None:
        return cmd_targets_list(cwd, _resolve_format_arg(args))
    if args.target_cmd == "add":
        return _target_add(args, cwd)
    if args.target_cmd == "remove":
        return _target_remove(args, cwd, registry)
    if args.target_cmd == "refresh":
        return _target_refresh(args, registry)
    if args.target_cmd == "prune":
        return _target_prune(args, registry)
    print(f"splash target {args.target_cmd}: unknown action", file=sys.stderr)
    return 2


def _env_set(assignment: str, target: str, registry: Registry) -> int:
    """`splash env set KEY=VALUE` — persist a value into the registry kv store."""
    if "=" not in assignment:
        print("usage: splash env set KEY=VALUE", file=sys.stderr)
        return 2
    key, value = assignment.split("=", 1)
    if not ENV_NAME_RE.match(key):
        print(f"invalid env name `{key}` (must match {ENV_NAME_RE.pattern})", file=sys.stderr)
        return 2
    recipe_path = Path(target) / RECIPE_NAME
    if not recipe_path.exists():
        print(
            f'no {RECIPE_NAME} in {target}; declare `{key}` as a type="set" resource',
            file=sys.stderr,
        )
        return 2
    try:
        resources = Recipe.load(recipe_path).resources
    except (OSError, ValueError) as e:
        print(f"could not read {recipe_path}: {e}", file=sys.stderr)
        return 2
    spec = resources.get(key)
    if spec is None:
        print(
            f"`{key}` is not a resource in {RECIPE_NAME}; declare it as "
            f'`[resources.{key}]` with type = "set" before setting it',
            file=sys.stderr,
        )
        return 2
    if not isinstance(spec, dict):
        print(f"`{key}` in {RECIPE_NAME} must be a resource table", file=sys.stderr)
        return 2
    rtype = spec.get("type")
    if rtype != "set":
        print(
            f'`{key}` is type `{rtype}`; only type="set" resources accept manual values',
            file=sys.stderr,
        )
        return 2
    registry.remove_port(target, key)
    registry.set_kv(target, key, value)
    print(f"set {key}={value}", file=sys.stderr)
    return 0


def _env_dispatch(args: Any, cwd: Path, registry: Registry) -> int:
    """`splash env …` — this checkout's resolved values. Bare = list."""
    fmt = _resolve_format_arg(args)
    if args.env_cmd is None:  # bare `splash env` → list
        target = str(Path(args.checkout).resolve()) if args.checkout else str(cwd)
        data = registry.all_for(target)
        if fmt == "json":
            print(json.dumps(data, indent=2))
        else:
            if not data:
                print(f"(empty) {target}", file=sys.stderr)
            for k, v in sorted(data.items()):
                print(f"{k}={v}")
        return 0
    # Normalize the same way provision() keys the registry (str(cwd.resolve())),
    # or get/set/release silently miss each other on symlinked/relative invocations.
    # --checkout targets another checkout's entries (default: this one).
    target = str(Path(args.checkout).resolve()) if args.checkout else str(cwd.resolve())
    if args.env_cmd == "get":
        value = registry.all_for(target).get(args.key)
        if value is None:
            return 1
        print(value)
        return 0
    if args.env_cmd == "set":
        return _env_set(args.assignment, target, registry)
    if args.env_cmd == "release":
        if args.key:
            registry.remove_kv(target, args.key)
            registry.remove_port(target, args.key)
            print(f"released {args.key}", file=sys.stderr)
        else:
            n = registry.release(target)
            print(f"released {n} entries for {target}", file=sys.stderr)
        return 0
    print(f"splash env {args.env_cmd}: unknown action", file=sys.stderr)
    return 2
