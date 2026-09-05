from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from . import bootstrap
from .constants import LOCAL_NAME, RECIPE_NAME
from .device_types import ManagedDevice
from .devices import (
    DeviceError,
    DeviceHealth,
    _device_status_for_row,
    _resolve_device_name,
    device_health,
    device_status,
    physical_status,
)
from .errors import CapabilityError
from .port_inspection import PortOwner, listening_processes
from .recipe import GlobalConfig, LocalConfig, Recipe, _global_config_path, merged_targets
from .registry import Registry, _port_in_use
from .targets import _load_recipe_or_empty, target_source


@dataclass(frozen=True)
class ResourceStatus:
    key: str
    value: str
    port_state: str
    owners: tuple[PortOwner, ...] | None = None


@dataclass(frozen=True)
class TargetStatus:
    type: str
    variant: str
    source: str
    device_name: str
    status: str
    orphan: bool = False
    stale: bool = False
    undeclared: bool = False
    missing: bool = False


@dataclass(frozen=True)
class AutomationStatus:
    sync_trusted: bool
    bootstrap_trusted: bool
    bootstrap_declared: bool | None
    bootstrap_completion: Literal["not-declared", "pending", "complete", "invalid", "unavailable"]


@dataclass(frozen=True)
class ClaimListRow:
    target: str
    source: str
    platform: str
    hardware_id: str
    owner: str
    claimed_at: str


@dataclass(frozen=True)
class TargetInventoryRow:
    type: str
    variant: str
    source: str
    device_name: str
    platform: str
    connection: str
    claim: str
    owner: str


def _load_variant_spec(cwd: Path, dtype: str, variant: str) -> dict[str, Any] | None:
    recipe = _load_recipe_or_empty(cwd)
    local = LocalConfig.load(cwd / LOCAL_NAME)
    glob = GlobalConfig.load(_global_config_path())
    return merged_targets(recipe, local, glob).get(dtype, {}).get(variant)


@dataclass(frozen=True)
class CheckoutStatus:
    checkout: str
    exists: bool
    resources: tuple[ResourceStatus, ...]
    targets: tuple[TargetStatus, ...]
    automation: AutomationStatus | None


@dataclass(frozen=True)
class StatusTableRow:
    checkout: str
    counts: dict[str, int]
    status: str


@dataclass
class StatusSummary:
    defunct_checkouts: int = 0
    defunct_rows: int = 0
    orphan_devices: int = 0
    stale_devices: int = 0
    undeclared_devices: int = 0
    missing_devices: int = 0
    missing_hardware: int = 0


@dataclass(frozen=True)
class StatusReport:
    show_all: bool
    check: bool
    checkouts: tuple[CheckoutStatus, ...]
    rows: tuple[StatusTableRow, ...]
    summary: StatusSummary
    warnings: tuple[str, ...]
    stale_registry_rows: int = 0
    unfilled_resources: tuple[str, ...] = ()


@dataclass
class _StatusContext:
    summary: StatusSummary = field(default_factory=StatusSummary)
    cache: dict[str, str] = field(default_factory=dict)
    warned: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)
    listeners: dict[int, tuple[PortOwner, ...]] | None = None
    listeners_queried: bool = False

    def warn(self, error: CapabilityError) -> None:
        if error.capability in self.warned:
            return
        self.warned.add(error.capability)
        label = {"ios": "iOS", "android": "Android"}.get(error.capability, error.capability)
        self.warnings.append(f"warning: skipping {label}: {error}")


def _gather_resource_entries(
    co_path: Path, *, co_exists: bool, resources: dict[str, str], context: _StatusContext
) -> tuple[ResourceStatus, ...]:
    port_keys: set[str] = set()
    if co_exists:
        recipe_path = co_path / RECIPE_NAME
        if recipe_path.exists():
            try:
                recipe = Recipe.load(recipe_path)
                port_keys = {
                    name for name, spec in recipe.resources.items() if spec.get("type") == "port"
                }
            except Exception:  # noqa: BLE001, S110
                pass

    entries: list[ResourceStatus] = []
    for key, value in sorted(resources.items()):
        state = ""
        owners: tuple[PortOwner, ...] | None = None
        if key in port_keys:
            with suppress(ValueError):
                state = "in use" if _port_in_use(int(value)) else "free"
                if state == "free":
                    owners = ()
                else:
                    if not context.listeners_queried:
                        context.listeners = listening_processes()
                        context.listeners_queried = True
                    if context.listeners is not None:
                        owners = context.listeners.get(int(value))
        entries.append(ResourceStatus(key, value, state, owners))
    return tuple(entries)


def _registered_target_status(
    row: ManagedDevice,
    registry: Registry,
    checkout_path: Path,
    *,
    check: bool,
    context: _StatusContext,
) -> TargetStatus:
    health: DeviceHealth | None = None
    try:
        try:
            current = _device_status_for_row(row)
        except CapabilityError:
            raise
        except DeviceError as error:
            current = f"error: {error}"
        if check and checkout_path.exists():
            spec = _load_variant_spec(checkout_path, row.dtype, row.variant)
            health = device_health(
                registry,
                checkout_path,
                row.dtype,
                row.variant,
                spec,
                cache=context.cache,
            )
    except CapabilityError as error:
        context.warn(error)
        current = "unavailable"

    orphan = health is DeviceHealth.ORPHAN
    stale = health is DeviceHealth.DRIFTED
    undeclared = health is DeviceHealth.UNDECLARED
    if orphan:
        context.summary.orphan_devices += 1
    elif stale:
        context.summary.stale_devices += 1
    elif undeclared:
        context.summary.undeclared_devices += 1
    return TargetStatus(
        row.dtype,
        row.variant,
        "",
        row.identifier,
        current,
        orphan=orphan,
        stale=stale,
        undeclared=undeclared,
    )


def _gather_devices_all(
    registry: Registry,
    checkout: str,
    checkout_path: Path,
    *,
    check: bool,
    context: _StatusContext,
) -> tuple[TargetStatus, ...]:
    return tuple(
        _registered_target_status(
            row,
            registry,
            checkout_path,
            check=check,
            context=context,
        )
        for row in registry.devices_for(checkout)
    )


def _declared_target_status(  # noqa: PLR0913 — target identity and shared context are independent inputs
    registry: Registry,
    checkout_path: Path,
    dtype: str,
    variant: str,
    spec: dict[str, Any],
    source: str,
    *,
    check: bool,
    context: _StatusContext,
) -> TargetStatus:
    resolved = (
        spec.get("id") or spec.get("name") or spec.get("platform") or "auto"
        if dtype == "device"
        else _resolve_device_name(spec, checkout_path, variant, dtype)
    )
    health: DeviceHealth | None = None
    try:
        try:
            current = (
                physical_status(spec, warned=context.warned, on_warning=context.warn)
                if dtype == "device"
                else device_status(dtype, resolved)
            )
        except CapabilityError:
            raise
        except DeviceError as error:
            current = f"error: {error}"
        if check:
            health = device_health(
                registry,
                checkout_path,
                dtype,
                variant,
                spec,
                cache=context.cache,
            )
    except CapabilityError as error:
        context.warn(error)
        current = "unavailable"

    missing = False
    orphan = health is DeviceHealth.ORPHAN
    stale = health is DeviceHealth.DRIFTED
    if dtype == "device" and current == "absent":
        missing = check
        context.summary.missing_hardware += int(check)
    elif health is DeviceHealth.MISSING:
        missing = True
        context.summary.missing_devices += 1
    elif orphan:
        context.summary.orphan_devices += 1
    elif stale:
        context.summary.stale_devices += 1
    return TargetStatus(
        dtype,
        variant,
        source,
        str(resolved),
        current,
        orphan=orphan,
        stale=stale,
        missing=missing,
    )


def _gather_targets_declared(
    registry: Registry,
    checkout_path: Path,
    *,
    check: bool,
    context: _StatusContext,
) -> tuple[TargetStatus, ...]:
    recipe = _load_recipe_or_empty(checkout_path)
    local = LocalConfig.load(checkout_path / LOCAL_NAME)
    global_config = GlobalConfig.load(_global_config_path())
    entries: list[TargetStatus] = []
    for dtype, variants in merged_targets(recipe, local, global_config).items():
        for variant, spec in variants.items():
            source = target_source(dtype, variant, recipe, local, global_config)
            entries.append(
                _declared_target_status(
                    registry,
                    checkout_path,
                    dtype,
                    variant,
                    spec,
                    source,
                    check=check,
                    context=context,
                )
            )
    return tuple(entries)


def _gather_automation(
    checkout_path: Path, *, exists: bool, context: _StatusContext
) -> AutomationStatus | None:
    if not exists:
        return None
    try:
        dirs = bootstrap.git_dirs(checkout_path)
    except ValueError:
        return None

    trust = bootstrap.trust_state(dirs)
    try:
        declared = _load_recipe_or_empty(checkout_path).bootstrap is not None
    except (OSError, ValueError) as error:
        context.warnings.append(f"warning: {checkout_path}: automation recipe unavailable: {error}")
        return AutomationStatus(
            sync_trusted=trust.sync,
            bootstrap_trusted=trust.bootstrap,
            bootstrap_declared=None,
            bootstrap_completion="unavailable",
        )
    completion: Literal["not-declared", "pending", "complete", "invalid"] = "not-declared"
    if declared:
        try:
            completion = "complete" if bootstrap.bootstrap_complete(dirs) else "pending"
        except ValueError:
            completion = "invalid"
    return AutomationStatus(
        sync_trusted=trust.sync,
        bootstrap_trusted=trust.bootstrap,
        bootstrap_declared=declared,
        bootstrap_completion=completion,
    )


def _gather_checkout(
    checkout: str,
    registry: Registry,
    *,
    show_all: bool,
    check: bool,
    context: _StatusContext,
) -> CheckoutStatus:
    checkout_path = Path(checkout)
    exists = checkout_path.exists()
    resources = registry.all_for(checkout)
    if check and not exists:
        context.summary.defunct_checkouts += 1
        context.summary.defunct_rows += (
            len(resources)
            + len(registry.devices_for(checkout))
            + registry.summary_for(checkout)["claim"]
        )

    targets: tuple[TargetStatus, ...]
    if show_all:
        targets = _gather_devices_all(
            registry,
            checkout,
            checkout_path,
            check=check,
            context=context,
        )
    elif exists:
        targets = _gather_targets_declared(
            registry,
            checkout_path,
            check=check,
            context=context,
        )
    else:
        targets = ()
    return CheckoutStatus(
        checkout,
        exists,
        _gather_resource_entries(
            checkout_path, co_exists=exists, resources=resources, context=context
        ),
        targets,
        _gather_automation(checkout_path, exists=exists, context=context),
    )


def _table_rows(
    checkouts: list[str], registry: Registry, *, check: bool, context: _StatusContext
) -> tuple[StatusTableRow, ...]:
    rows: list[StatusTableRow] = []
    for checkout in checkouts:
        counts = registry.summary_for(checkout)
        status = ""
        if not Path(checkout).exists():
            status = "defunct"
            if check:
                context.summary.defunct_checkouts += 1
                context.summary.defunct_rows += sum(counts.values())
        elif check:
            for row in registry.devices_for(checkout):
                try:
                    spec = _load_variant_spec(Path(checkout), row.dtype, row.variant)
                    health = device_health(
                        registry,
                        Path(checkout),
                        row.dtype,
                        row.variant,
                        spec,
                        cache=context.cache,
                    )
                except CapabilityError as error:
                    context.warn(error)
                    status = status or "unavailable"
                    continue
                if health is DeviceHealth.ORPHAN:
                    context.summary.orphan_devices += 1
                    status = "orphan"
                elif health is DeviceHealth.DRIFTED:
                    context.summary.stale_devices += 1
                    status = status or "stale"
                elif health is DeviceHealth.UNDECLARED:
                    context.summary.undeclared_devices += 1
                    status = status or "undeclared"
        rows.append(StatusTableRow(checkout, counts, status))
    return tuple(rows)


def build_status_report(
    cwd: Path,
    registry: Registry,
    *,
    show_all: bool = False,
    check: bool = False,
    detailed: bool = True,
) -> StatusReport:
    target = str(cwd.resolve())
    checkouts = registry.all_checkouts() if show_all else [target]
    if not checkouts:
        checkouts = [target]
    context = _StatusContext()

    if show_all and not detailed:
        rows = _table_rows(checkouts, registry, check=check, context=context)
        return StatusReport(
            show_all,
            check,
            (),
            rows,
            context.summary,
            tuple(context.warnings),
        )

    checkout_reports = tuple(
        _gather_checkout(
            checkout,
            registry,
            show_all=show_all,
            check=check,
            context=context,
        )
        for checkout in checkouts
    )
    stale_rows = 0
    unfilled: tuple[str, ...] = ()
    if not check and not show_all:
        stale_rows = sum(1 for row in registry._read_ports() if not Path(row[1]).exists()) + sum(  # noqa: SLF001
            1
            for row in registry._read_kv()  # noqa: SLF001
            if not Path(row[0]).exists()
        )
        recipe = _load_recipe_or_empty(cwd)
        resolved_keys = registry.all_for(target)
        unfilled = tuple(
            name
            for name, spec in recipe.resources.items()
            if spec.get("type") == "set"
            and spec.get("default") is None
            and name not in resolved_keys
        )
    return StatusReport(
        show_all,
        check,
        checkout_reports,
        (),
        context.summary,
        tuple(context.warnings),
        stale_rows,
        unfilled,
    )
