from __future__ import annotations

import os
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from .capabilities import warn_capability
from .constants import LOCAL_NAME, RECIPE_NAME, TARGET_TYPES
from .device_android import (
    _android_avd_names,
    android_boot,
    android_destroy,
    android_shutdown,
)
from .device_claims import (
    claim_available_target,
    claim_configured_target,
    configured_physical_targets,
    discover_physical_snapshot,
    match_physical_target,
    notices_for_displaced,
    resolve_physical_target,
)
from .device_ios import _ios_current_state, _xcrun_json, ios_boot, ios_destroy, ios_shutdown
from .device_types import AndroidDestination, IOSDestination, PhysicalClaim, as_launch_destination
from .devices import (
    _is_orphan_device,
    _resolve_device_name,
    device_destroy_row,
    device_needs_recreate,
    device_shutdown_row,
    device_status,
    ensure_fresh_sim,
)
from .errors import CapabilityError, DeviceError, UsageError
from .launching import device_run, device_run_preflight, validate_device_run
from .provisioning import provision, write_outputs
from .recipe import (
    GlobalConfig,
    LocalConfig,
    Recipe,
    _global_config_path,
    load_settings,
    merged_targets,
    resolve_variant,
)
from .registry import Registry
from .safe_files import atomic_write_text, read_optional_editable_text
from .targets import (
    _load_recipe_or_empty,
    _prepare_target_remove,
    _target_types_for_variant,
    global_target_add,
    global_target_remove,
    target_add,
    target_source,
)

_PLATFORM_OF_DTYPE = {"simulator": "ios", "emulator": "android"}


def cmd_targets_list(cwd: Path, registry: Registry, fmt: str) -> int:
    from .cli_output import render_target_inventory  # noqa: PLC0415
    from .status import TargetInventoryRow  # noqa: PLC0415

    recipe = _load_recipe_or_empty(cwd)
    local = LocalConfig.load(cwd / LOCAL_NAME)
    glob = GlobalConfig.load(_global_config_path())
    catalog = merged_targets(recipe, local, glob)
    if not catalog:
        if fmt == "json":
            print("[]")
        else:
            print(f"(no targets declared in {RECIPE_NAME} or {LOCAL_NAME})", file=sys.stderr)
        return 0

    physical_targets = configured_physical_targets(cwd, allow_missing_recipe=True)
    claims = registry.all_claims()
    warned: set[str] = set()
    unavailable: set[str] = set()
    if physical_targets:
        snapshot = discover_physical_snapshot("any", warned=warned, unavailable=unavailable)
    else:
        snapshot = ()
    rows: list[TargetInventoryRow] = []
    for target in physical_targets:
        platform = str(target.spec.get("platform", "any"))
        configured_name = str(
            target.spec.get("id")
            or target.spec.get("name")
            or target.spec.get("platform")
            or "auto"
        )
        destination = None
        connection = (
            "unavailable"
            if platform in unavailable or (platform == "any" and unavailable)
            else "disconnected"
        )
        if connection != "unavailable":
            try:
                destination = match_physical_target(target, snapshot)
            except DeviceError as error:
                connection = (
                    "ambiguous"
                    if str(error).startswith("multiple connected physical devices")
                    else "disconnected"
                )
            else:
                connection = "connected"
                platform = destination.platform
        owner_claim = next(
            (claim for claim in claims if claim.catalog_identity == target.catalog_identity),
            None,
        )
        if owner_claim is None and destination is not None:
            owner_claim = next(
                (
                    claim
                    for claim in claims
                    if claim.platform == destination.platform
                    and claim.hardware_id == (destination.identifier or "")
                ),
                None,
            )
        rows.append(
            TargetInventoryRow(
                "device",
                target.variant,
                target_source("device", target.variant, recipe, local, glob),
                destination.name if destination is not None else configured_name,
                platform,
                connection,
                "claimed" if owner_claim is not None else "free",
                owner_claim.owner_checkout if owner_claim is not None else "",
            )
        )
    for dtype, variants in catalog.items():
        if dtype == "device":
            continue
        for variant, spec in variants.items():
            source = target_source(dtype, variant, recipe, local, glob)
            resolved = _resolve_device_name(spec, cwd, variant, dtype)
            try:
                connection = device_status(dtype, resolved)
            except CapabilityError as error:
                warn_capability(error, warned)
                connection = "unavailable"
            except DeviceError as error:
                connection = f"error: {error}"
            rows.append(
                TargetInventoryRow(
                    dtype,
                    variant,
                    source,
                    resolved,
                    _PLATFORM_OF_DTYPE.get(dtype, ""),
                    connection,
                    "not-applicable",
                    "",
                )
            )
    render_target_inventory(rows, fmt)
    return 0


def _load_variant_spec(
    cwd: Path, dtype: str, variant: str, glob: GlobalConfig | None = None
) -> dict[str, Any] | None:
    recipe = _load_recipe_or_empty(cwd)
    local = LocalConfig.load(cwd / LOCAL_NAME)
    if glob is None:
        glob = GlobalConfig.load(_global_config_path())
    return merged_targets(recipe, local, glob).get(dtype, {}).get(variant)


def _emit_progress(label: str, current: int, total: int) -> None:
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
    destroyed_count = 0
    warning_keys = warned if warned is not None else set()
    rows = list(registry.all_devices())
    total = len(rows)
    for i, row in enumerate(rows, 1):
        _emit_progress("gc", i, total)
        try:
            with registry.operation_lock(row.checkout):
                current = registry.get_device(row.checkout, row.dtype, row.variant)
                if current is None:
                    continue
                if Path(current.checkout).exists():
                    if not _is_orphan_device(current):
                        continue
                else:
                    device_destroy_row(current)
                registry.remove_device(current.checkout, current.dtype, current.variant)
                destroyed_count += 1
        except CapabilityError as error:
            warn_capability(error, warning_keys)
    _finish_progress()
    return destroyed_count


def cmd_gc(registry: Registry) -> int:
    warned: set[str] = set()
    reconciled = cmd_target_gc(registry, warned=warned)
    removed = registry.gc(include_devices=False)
    print(
        f"gc: removed {removed} registry entries, reconciled {reconciled} device row(s)",
        file=sys.stderr,
    )
    return 0


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
    recreated = unchanged = dropped = 0
    cache: dict[str, str] = {}
    warned: set[str] = set()
    glob = GlobalConfig.load(_global_config_path())
    rows = [r for r in registry.all_devices() if _PLATFORM_OF_DTYPE.get(r.dtype) in platforms]
    for row in rows:
        if Path(row.checkout).exists():
            _load_variant_spec(Path(row.checkout), row.dtype, row.variant, glob=glob)
    total = len(rows)
    for i, row in enumerate(rows, 1):
        _emit_progress("target refresh", i, total)
        try:
            with registry.operation_lock(row.checkout):
                current = registry.get_device(row.checkout, row.dtype, row.variant)
                if current is None:
                    continue
                cwd = Path(current.checkout)
                spec = (
                    _load_variant_spec(cwd, current.dtype, current.variant, glob=glob)
                    if cwd.exists()
                    else None
                )
                if spec is None:
                    device_destroy_row(current)
                    registry.remove_device(current.checkout, current.dtype, current.variant)
                    dropped += 1
                    continue
                will_recreate = device_needs_recreate(
                    registry, cwd, current.dtype, current.variant, spec, cache=cache
                )
                ensure_fresh_sim(
                    registry,
                    cwd,
                    current.dtype,
                    current.variant,
                    spec,
                    cache=cache,
                )
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
    data = _xcrun_json(["simctl", "list", "devices", "-j"])
    foreign: list[tuple[str, str, str]] = []
    for runtime, devs in (data.get("devices") or {}).items():
        for device in devs:
            udid = device.get("udid")
            if not udid or udid in managed or not device.get("isAvailable", True):
                continue
            foreign.append((udid, device.get("name", "?"), runtime))
    return foreign


def _discover_foreign_avds(managed: set[str]) -> list[str]:
    return [name for name in _android_avd_names() if name not in managed]


def _confirm(prompt: str, *, yes: bool) -> bool:
    if yes:
        return True
    print(f"{prompt} [y/N] ", end="", file=sys.stderr, flush=True)
    return input().strip().lower() in ("y", "yes")


def cmd_target_prune(
    registry: Registry,
    *,
    yes: bool = False,
    dry_run: bool = False,
    platforms: tuple[str, ...] = ("ios", "android"),
    skip_unavailable: bool = False,
) -> int:
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
            ios_shutdown(udid)
            ios_destroy(udid)
            done += 1
            _emit_progress("target prune", done, total)
        except CapabilityError as error:
            _handle_optional_capability(error, skip_unavailable=skip_unavailable, warned=warned)
    for name in foreign_avd:
        try:
            android_shutdown(name)
            android_destroy(name)
            done += 1
            _emit_progress("target prune", done, total)
        except CapabilityError as error:
            _handle_optional_capability(error, skip_unavailable=skip_unavailable, warned=warned)
    _finish_progress()
    print(f"target prune: removed {done} device(s)", file=sys.stderr)
    return 0


def _declared_target_types(cwd: Path, *, include_global: bool = True) -> list[str]:
    recipe = _load_recipe_or_empty(cwd)
    local = LocalConfig.load(cwd / LOCAL_NAME)
    glob = GlobalConfig.load(_global_config_path()) if include_global else None
    return [dtype for dtype, variants in merged_targets(recipe, local, glob).items() if variants]


def _infer_dtype(cwd: Path, dtype: str | None, variant_arg: str | None = None) -> str:
    if dtype:
        return dtype
    if variant_arg is not None:
        recipe = _load_recipe_or_empty(cwd)
        local = LocalConfig.load(cwd / LOCAL_NAME)
        glob = GlobalConfig.load(_global_config_path())
        catalog = merged_targets(recipe, local, glob)
        matches = _target_types_for_variant(catalog, variant_arg)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise DeviceError(
                f"variant `{variant_arg}` exists under multiple target types "
                f"({', '.join(sorted(matches))}); specify the type"
            )
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
    recipe = _load_recipe_or_empty(cwd)
    local = LocalConfig.load(cwd / LOCAL_NAME)
    glob = GlobalConfig.load(_global_config_path())
    catalog = merged_targets(recipe, local, glob).get(dtype, {})
    variant, spec = resolve_variant(
        catalog, variant_arg, prefix_match=load_settings(cwd).prefix_match
    )
    return variant, spec, recipe


def cmd_run(cwd: Path, registry: Registry, dtype: str | None, variant_arg: str | None) -> int:
    abspath = str(cwd.resolve())
    with registry.operation_lock(abspath):
        dtype = _infer_dtype(cwd, dtype, variant_arg)
        variant, spec, recipe = _resolve_variant_for_cli(cwd, dtype, variant_arg)
        kind = _PLATFORM_OF_DTYPE.get(dtype) or spec.get("platform")
        validate_device_run(cwd, recipe, kind)
        resolved = {}
        if (cwd / RECIPE_NAME).exists():
            resolved = provision(cwd, registry=registry, recipe=recipe)
            write_outputs(cwd, recipe, resolved)
        env = {**os.environ, **resolved}
        if dtype == "device":
            target = resolve_physical_target(cwd, variant)
            selection = claim_configured_target(registry, cwd, target)
            destination = selection.destination
        else:
            destination = as_launch_destination(
                ensure_fresh_sim(registry, cwd, dtype, variant, spec)
            )
        device_run_preflight(cwd, recipe, destination, resolved)
        if destination.owned:
            if isinstance(destination, IOSDestination):
                ios_boot(destination.identifier, _ios_current_state(destination.identifier))
            elif isinstance(destination, AndroidDestination):
                destination = replace(
                    destination,
                    identifier=android_boot(destination.name, state_dir=registry.state_dir),
                )
    return int(device_run(cwd, recipe, destination, env))


def cmd_start(cwd: Path, registry: Registry, dtype: str | None, variant_arg: str | None) -> int:
    abspath = str(cwd.resolve())
    with registry.operation_lock(abspath):
        dtype = _infer_dtype(cwd, dtype, variant_arg)
        variant, spec, _recipe = _resolve_variant_for_cli(cwd, dtype, variant_arg)
        destination = as_launch_destination(ensure_fresh_sim(registry, cwd, dtype, variant, spec))
        if not destination.owned:
            print(f"{dtype}.{variant} connected ({destination.name})", file=sys.stderr)
            return 0
        if isinstance(destination, IOSDestination):
            ios_boot(destination.identifier, _ios_current_state(destination.identifier))
        elif isinstance(destination, AndroidDestination):
            destination = replace(
                destination,
                identifier=android_boot(destination.name, state_dir=registry.state_dir),
            )
    print(f"started {dtype}.{variant} ({destination.name})", file=sys.stderr)
    return 0


def cmd_stop(cwd: Path, registry: Registry, dtype: str | None, variant_arg: str | None) -> int:
    abspath = str(cwd.resolve())
    with registry.operation_lock(abspath):
        dtype = _infer_dtype(cwd, dtype, variant_arg)
        variant, _spec, _recipe = _resolve_variant_for_cli(cwd, dtype, variant_arg)
        if dtype == "device":
            print(
                f"{dtype}.{variant} is hardware splashdown doesn't own; nothing to stop",
                file=sys.stderr,
            )
            return 0
        row = registry.get_device(abspath, dtype, variant)
        if row is None:
            print(f"{dtype}.{variant} has no managed instance; nothing to stop", file=sys.stderr)
            return 0
        device_shutdown_row(row)
    print(f"stopped {dtype}.{variant} ({row.identifier})", file=sys.stderr)
    return 0


def cmd_destroy(
    cwd: Path,
    registry: Registry,
    dtype: str | None,
    variant_arg: str | None,
    *,
    yes: bool = False,
) -> int:
    dtype = _infer_dtype(cwd, dtype, variant_arg)
    variant, _spec, _recipe = _resolve_variant_for_cli(cwd, dtype, variant_arg)
    if dtype == "device":
        print(
            f"{dtype}.{variant} is hardware splashdown doesn't own; nothing to destroy",
            file=sys.stderr,
        )
        return 0
    if not _confirm(f"Destroy {dtype}.{variant}?", yes=yes):
        print(f"destroy {dtype}.{variant}: aborted", file=sys.stderr)
        return 1
    abspath = str(cwd.resolve())
    with registry.operation_lock(abspath):
        variant, _spec, _recipe = _resolve_variant_for_cli(cwd, dtype, variant)
        row = registry.get_device(abspath, dtype, variant)
        if row is None:
            print(f"{dtype}.{variant} has no managed instance; nothing to destroy", file=sys.stderr)
            return 0
        device_destroy_row(row)
        registry.remove_device(abspath, dtype, variant)
    print(f"destroyed {dtype}.{variant} ({row.identifier})", file=sys.stderr)
    return 0


def _target_add(args: Any, cwd: Path, registry: Registry) -> int:
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
    with registry.operation_lock(str(cwd.resolve())):
        target_add(cwd, args.dtype, args.variant, fields)
    print(f"added target `{args.dtype}.{args.variant}` to {LOCAL_NAME}", file=sys.stderr)
    return 0


def _target_remove_locked(args: Any, cwd: Path, registry: Registry, checkout: str) -> int:
    variant = args.variant
    recipe = _load_recipe_or_empty(cwd)
    local_path = cwd / LOCAL_NAME
    local_text = read_optional_editable_text(local_path, root=cwd)
    if local_text is None:
        local = LocalConfig({}, local_path)
    else:
        try:
            local = LocalConfig.parse(local_text, local_path)
        except ValueError:
            local = LocalConfig({}, local_path)
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
    _spec, local_path, new_local_text = _prepare_target_remove(cwd, args.dtype, variant)
    destroyed = False
    missing = False
    if not args.keep_instance and args.dtype != "device":
        row = registry.get_device(checkout, args.dtype, variant)
        if row is not None:
            device_destroy_row(row)
        else:
            missing = True
        atomic_write_text(local_path, new_local_text, root=cwd)
        registry.remove_device(checkout, args.dtype, variant)
        destroyed = row is not None
    else:
        atomic_write_text(local_path, new_local_text, root=cwd)
    if destroyed:
        suffix = " (and destroyed the instance)"
    elif missing:
        suffix = " (no managed instance found)"
    else:
        suffix = ""
    print(f"removed target `{args.dtype}.{variant}` from {LOCAL_NAME}{suffix}", file=sys.stderr)
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
    checkout = str(cwd.resolve())
    with registry.operation_lock(checkout):
        return _target_remove_locked(args, cwd, registry, checkout)


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


def _add_claim_notices(
    registry: Registry,
    displaced: tuple[PhysicalClaim, ...],
    *,
    action: Literal["transfer", "release"],
    actor_checkout: str,
) -> None:
    if not displaced:
        return
    notices = notices_for_displaced(
        displaced,
        action=action,
        actor_checkout=actor_checkout,
        event_at=datetime.now(UTC),
    )
    try:
        registry.add_claim_notices(notices)
    except OSError as error:
        print(f"warning: could not record claim notice: {error}", file=sys.stderr)


def cmd_target_claim(
    cwd: Path,
    registry: Registry,
    variant: str | None,
    *,
    available: str | None,
    force: bool,
    fmt: str,
) -> int:
    from .cli_output import render_claim_selection  # noqa: PLC0415

    checkout = str(cwd.resolve())
    with registry.operation_lock(checkout):
        if available is None:
            target = resolve_physical_target(cwd, variant)
            selection = claim_configured_target(registry, cwd, target, force=force)
        else:
            selection = claim_available_target(
                registry, cwd, cast(Literal["ios", "android", "any"], available)
            )
        if force:
            _add_claim_notices(
                registry,
                selection.displaced,
                action="transfer",
                actor_checkout=checkout,
            )
        render_claim_selection(selection, fmt, available=available is not None)
    return 0


def _release_busy_error(variant: str, conflict: PhysicalClaim) -> DeviceError:
    return DeviceError(
        f"physical target `{variant}` is claimed by {conflict.owner_checkout} "
        f"since {conflict.claimed_at}; inspect with `splash target claims` or release with "
        f"`splash target release {variant} --force`"
    )


def cmd_target_release(
    cwd: Path,
    registry: Registry,
    variant: str | None,
    *,
    all_owned: bool,
    force: bool,
) -> int:
    checkout = str(cwd.resolve())
    if all_owned:
        with registry.operation_lock(checkout):
            released = registry.release_claims(checkout)
            print(f"released {len(released)} physical claim(s)", file=sys.stderr)
        return 0

    with registry.operation_lock(checkout):
        target = resolve_physical_target(cwd, variant)
        result = registry.release_claim(target.catalog_identity, checkout, force=force)
        if result.status == "busy":
            raise _release_busy_error(target.variant, result.conflicts[0])
        displaced = tuple(row for row in result.released if row.owner_checkout != checkout)
        if force:
            _add_claim_notices(registry, displaced, action="release", actor_checkout=checkout)
        if result.status == "missing":
            print(f"no claim for {target.variant}; nothing to release", file=sys.stderr)
        else:
            print(f"released {target.variant}", file=sys.stderr)
    return 0


def cmd_target_claims(registry: Registry, fmt: str) -> int:
    from .cli_output import render_claim_rows  # noqa: PLC0415
    from .status import ClaimListRow  # noqa: PLC0415

    rows = tuple(
        ClaimListRow(
            claim.target_label,
            claim.catalog_identity.split(":", 1)[0],
            claim.platform,
            claim.hardware_id,
            claim.owner_checkout,
            claim.claimed_at,
        )
        for claim in registry.all_claims()
    )
    render_claim_rows(rows, fmt)
    return 0


def _target_dispatch(args: Any, cwd: Path, registry: Registry) -> int:  # noqa: PLR0911
    if args.target_cmd is None:
        return cmd_targets_list(cwd, registry, getattr(args, "format", None) or "text")
    if args.target_cmd == "add":
        return _target_add(args, cwd, registry)
    if args.target_cmd == "remove":
        return _target_remove(args, cwd, registry)
    if args.target_cmd == "refresh":
        return _target_refresh(args, registry)
    if args.target_cmd == "prune":
        return _target_prune(args, registry)
    fmt = getattr(args, "target_format", None) or getattr(args, "format", None) or "text"
    if args.target_cmd == "claims":
        return cmd_target_claims(registry, fmt)
    if args.target_cmd == "claim":
        if bool(args.variant) == bool(args.available):
            raise UsageError("splash target claim requires exactly one of VARIANT or --available")
        if args.force and args.available:
            raise UsageError("splash target claim --force requires VARIANT")
        return cmd_target_claim(
            cwd,
            registry,
            args.variant,
            available=args.available,
            force=args.force,
            fmt=fmt,
        )
    if args.target_cmd == "release":
        if bool(args.variant) == bool(args.all_owned):
            raise UsageError("splash target release requires exactly one of VARIANT or --all")
        if args.force and args.all_owned:
            raise UsageError("splash target release --force requires VARIANT")
        return cmd_target_release(
            cwd,
            registry,
            args.variant,
            all_owned=args.all_owned,
            force=args.force,
        )
    print(f"splash target {args.target_cmd}: unknown action", file=sys.stderr)
    return 2
