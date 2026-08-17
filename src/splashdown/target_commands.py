from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from .capabilities import translate_tool_errors, warn_capability
from .constants import LOCAL_NAME, RECIPE_NAME, TARGET_TYPES
from .device_types import AndroidDestination, IOSDestination, as_launch_destination
from .devices import (
    _android_bin,
    _ios_current_state,
    _is_orphan_device,
    _resolve_device_name,
    _xcrun_json,
    android_boot,
    android_destroy,
    android_shutdown,
    device_destroy_row,
    device_needs_recreate,
    device_shutdown_row,
    device_status,
    ensure_fresh_sim,
    ios_boot,
    ios_destroy,
    ios_shutdown,
    physical_status,
)
from .errors import CapabilityError, DeviceError
from .launching import device_run, validate_device_run
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
from .targets import (
    _load_recipe_or_empty,
    _prepare_target_remove,
    global_target_add,
    global_target_remove,
    target_add,
    target_source,
)

_PLATFORM_OF_DTYPE = {"simulator": "ios", "emulator": "android"}


def cmd_targets_list(cwd: Path, fmt: str) -> int:
    recipe = _load_recipe_or_empty(cwd)
    local = LocalConfig.load(cwd / LOCAL_NAME)
    glob = GlobalConfig.load(_global_config_path())
    catalog = merged_targets(recipe, local, glob)
    if not catalog:
        print(f"(no targets declared in {RECIPE_NAME} or {LOCAL_NAME})", file=sys.stderr)
        return 0
    rows: list[tuple[str, str, str, str, str]] = []
    warned: set[str] = set()
    for dtype, variants in catalog.items():
        for variant, spec in variants.items():
            source = target_source(dtype, variant, recipe, local, glob)
            if dtype == "device":
                resolved = spec.get("id") or spec.get("name") or spec.get("platform") or "auto"
            else:
                resolved = _resolve_device_name(spec, cwd, variant, dtype)
            try:
                status = (
                    physical_status(spec, warned=warned)
                    if dtype == "device"
                    else device_status(dtype, resolved)
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


def _infer_dtype(cwd: Path, dtype: str | None) -> str:
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
        dtype = _infer_dtype(cwd, dtype)
        variant, spec, recipe = _resolve_variant_for_cli(cwd, dtype, variant_arg)
        kind = _PLATFORM_OF_DTYPE.get(dtype) or spec.get("platform")
        validate_device_run(cwd, recipe, kind)
        destination = as_launch_destination(ensure_fresh_sim(registry, cwd, dtype, variant, spec))
        if destination.owned:
            if isinstance(destination, IOSDestination):
                ios_boot(destination.identifier, _ios_current_state(destination.identifier))
            elif isinstance(destination, AndroidDestination):
                destination = replace(destination, identifier=android_boot(destination.name))
    return int(device_run(cwd, recipe, destination))


def cmd_start(cwd: Path, registry: Registry, dtype: str | None, variant_arg: str | None) -> int:
    abspath = str(cwd.resolve())
    with registry.operation_lock(abspath):
        dtype = _infer_dtype(cwd, dtype)
        variant, spec, _recipe = _resolve_variant_for_cli(cwd, dtype, variant_arg)
        destination = as_launch_destination(ensure_fresh_sim(registry, cwd, dtype, variant, spec))
        if not destination.owned:
            print(f"{dtype}.{variant} connected ({destination.name})", file=sys.stderr)
            return 0
        if isinstance(destination, IOSDestination):
            ios_boot(destination.identifier, _ios_current_state(destination.identifier))
        elif isinstance(destination, AndroidDestination):
            destination = replace(destination, identifier=android_boot(destination.name))
    print(f"started {dtype}.{variant} ({destination.name})", file=sys.stderr)
    return 0


def cmd_stop(cwd: Path, registry: Registry, dtype: str | None, variant_arg: str | None) -> int:
    abspath = str(cwd.resolve())
    with registry.operation_lock(abspath):
        dtype = _infer_dtype(cwd, dtype)
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
    dtype = _infer_dtype(cwd, dtype)
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
    _spec, local_path, new_local_text = _prepare_target_remove(cwd, args.dtype, variant)
    destroyed = False
    missing = False
    if not args.keep_instance and args.dtype != "device":
        row = registry.get_device(checkout, args.dtype, variant)
        if row is not None:
            device_destroy_row(row)
        else:
            missing = True
        local_path.write_text(new_local_text)
        registry.remove_device(checkout, args.dtype, variant)
        destroyed = row is not None
    else:
        local_path.write_text(new_local_text)
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


def _target_dispatch(args: Any, cwd: Path, registry: Registry) -> int:
    if args.target_cmd is None:
        return cmd_targets_list(cwd, getattr(args, "format", None) or "text")
    if args.target_cmd == "add":
        return _target_add(args, cwd, registry)
    if args.target_cmd == "remove":
        return _target_remove(args, cwd, registry)
    if args.target_cmd == "refresh":
        return _target_refresh(args, registry)
    if args.target_cmd == "prune":
        return _target_prune(args, registry)
    print(f"splash target {args.target_cmd}: unknown action", file=sys.stderr)
    return 2
