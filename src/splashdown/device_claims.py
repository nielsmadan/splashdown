from __future__ import annotations

import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from .bootstrap import git_dirs
from .capabilities import warn_capability
from .constants import CLAIM_NOTICE_DAYS, LOCAL_NAME, RECIPE_NAME
from .device_android import _android_physical_devices
from .device_ios import _ios_physical_devices
from .device_tools import DISCOVERY_TIMEOUT
from .device_types import (
    AndroidDestination,
    ClaimNotice,
    IOSDestination,
    LaunchDestination,
    PhysicalClaim,
)
from .devices import _physical_no_match_msg, physical_match_snapshot
from .errors import CapabilityError, DeviceError
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


@dataclass(frozen=True)
class ConfiguredPhysicalTarget:
    variant: str
    source: str
    catalog_identity: str
    spec: dict[str, Any]


@dataclass(frozen=True)
class PhysicalSelection:
    target: ConfiguredPhysicalTarget
    destination: LaunchDestination
    claim: PhysicalClaim
    status: Literal["claimed", "owned"]
    displaced: tuple[PhysicalClaim, ...]


def configured_physical_targets(
    cwd: Path, *, allow_missing_recipe: bool = False
) -> tuple[ConfiguredPhysicalTarget, ...]:
    recipe_path = cwd / RECIPE_NAME
    recipe = (
        Recipe.load(recipe_path)
        if recipe_path.exists() or not allow_missing_recipe
        else Recipe({}, recipe_path)
    )
    local = LocalConfig.load(cwd / LOCAL_NAME)
    global_config = GlobalConfig.load(_global_config_path())
    merged_targets(recipe, local, global_config)
    try:
        recipe_root = git_dirs(cwd).common
    except ValueError:
        recipe_root = (cwd / RECIPE_NAME).resolve()

    catalog: list[ConfiguredPhysicalTarget] = []
    seen: set[str] = set()
    for source, identity_root, targets in (
        ("recipe", recipe_root, recipe.targets.get("device", {})),
        ("local", cwd.resolve(), local.targets.get("device", {})),
        ("global", _global_config_path().resolve(), global_config.targets.get("device", {})),
    ):
        for variant, spec in targets.items():
            if source == "global" and variant in seen:
                continue
            seen.add(variant)
            catalog.append(
                ConfiguredPhysicalTarget(
                    variant,
                    source,
                    f"{source}:{identity_root}:device:{variant}",
                    spec,
                )
            )
    return tuple(catalog)


def resolve_physical_target(cwd: Path, requested: str | None) -> ConfiguredPhysicalTarget:
    catalog = configured_physical_targets(cwd)
    variants = {target.variant: target.spec for target in catalog}
    variant, _spec = resolve_variant(variants, requested, load_settings(cwd).prefix_match)
    return next(target for target in catalog if target.variant == variant)


def _remaining_timeout(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def discover_physical_snapshot(
    platform: Literal["ios", "android", "any"],
    *,
    timeout: float = DISCOVERY_TIMEOUT,
    warned: set[str] | None = None,
    unavailable: set[str] | None = None,
) -> tuple[dict[str, str], ...]:
    warning_keys = warned if warned is not None else set()
    unavailable_platforms = unavailable if unavailable is not None else set()
    if platform == "ios":
        return tuple(_ios_physical_devices(timeout=timeout))
    if platform == "android":
        return tuple(_android_physical_devices(timeout=timeout))

    deadline = time.monotonic() + timeout
    executor = ThreadPoolExecutor(max_workers=2)
    futures = {}
    results: dict[str, list[dict[str, str]]] = {}
    try:
        for name, discover in (
            ("ios", _ios_physical_devices),
            ("android", _android_physical_devices),
        ):
            remaining = _remaining_timeout(deadline)
            if remaining == 0:
                raise DeviceError(f"physical device discovery timed out after {timeout}s")
            futures[name] = executor.submit(discover, timeout=remaining)
        for name in ("ios", "android"):
            try:
                results[name] = futures[name].result(timeout=_remaining_timeout(deadline))
            except CapabilityError as error:
                unavailable_platforms.add(name)
                warn_capability(error, warning_keys)
            except FutureTimeoutError as error:
                raise DeviceError(
                    f"physical device discovery timed out after {timeout}s"
                ) from error
    finally:
        for future in futures.values():
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
    return tuple(results.get("ios", []) + results.get("android", []))


def match_physical_target(
    target: ConfiguredPhysicalTarget, snapshot: Sequence[dict[str, str]]
) -> LaunchDestination:
    matches = physical_match_snapshot(target.spec, snapshot)
    if not matches:
        raise DeviceError(_physical_no_match_msg(target.spec))
    if len(matches) > 1:
        listing = ", ".join(f"{device['name']} ({device['id']})" for device in matches)
        raise DeviceError(
            f"multiple connected physical devices ({listing}); narrow with "
            "`id`/`name`/`platform` on the variant"
        )
    device = matches[0]
    if device["platform"] == "ios":
        return IOSDestination(device["name"], device["id"], owned=False)
    return AndroidDestination(device["name"], device["id"], owned=False)


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _claim_row(
    cwd: Path, target: ConfiguredPhysicalTarget, destination: LaunchDestination
) -> PhysicalClaim:
    return PhysicalClaim(
        target.catalog_identity,
        destination.platform,
        destination.identifier or "",
        target.variant,
        str(cwd.resolve()),
        _now().isoformat(),
    )


def _busy_error(
    target: ConfiguredPhysicalTarget, conflicts: Sequence[PhysicalClaim]
) -> DeviceError:
    owner = conflicts[0]
    return DeviceError(
        f"physical target `{target.variant}` is claimed by {owner.owner_checkout} "
        f"since {owner.claimed_at}; inspect with `splash target claims` or transfer with "
        f"`splash target claim {target.variant} --force`"
    )


def claim_configured_target(
    registry: Registry,
    cwd: Path,
    target: ConfiguredPhysicalTarget,
    *,
    force: bool = False,
    timeout: float = DISCOVERY_TIMEOUT,
    snapshot: Sequence[dict[str, str]] | None = None,
) -> PhysicalSelection:
    selected_snapshot = (
        tuple(snapshot)
        if snapshot is not None
        else discover_physical_snapshot(target.spec.get("platform", "any"), timeout=timeout)
    )
    destination = match_physical_target(target, selected_snapshot)
    attempt = registry.attempt_claim(_claim_row(cwd, target, destination), force=force)
    if attempt.status == "busy":
        raise _busy_error(target, attempt.conflicts)
    if attempt.claim is None:
        raise DeviceError("internal: claim attempt did not return a claim")
    return PhysicalSelection(target, destination, attempt.claim, attempt.status, attempt.displaced)


def claim_available_target(
    registry: Registry,
    cwd: Path,
    platform: Literal["ios", "android", "any"],
    *,
    timeout: float = DISCOVERY_TIMEOUT,
) -> PhysicalSelection:
    snapshot = discover_physical_snapshot(platform, timeout=timeout)
    claims = registry.all_claims()
    for target in configured_physical_targets(cwd):
        if platform != "any" and target.spec.get("platform") not in (None, platform):
            continue
        try:
            destination = match_physical_target(target, snapshot)
        except DeviceError:
            continue
        matching_claims = [
            claim
            for claim in claims
            if claim.catalog_identity == target.catalog_identity
            or (
                claim.platform == destination.platform
                and claim.hardware_id == (destination.identifier or "")
            )
        ]
        if matching_claims:
            continue
        attempt = registry.attempt_claim(_claim_row(cwd, target, destination))
        if attempt.status == "busy":
            continue
        if attempt.status == "owned":
            continue
        if attempt.claim is None:
            raise DeviceError("internal: claim attempt did not return a claim")
        return PhysicalSelection(
            target, destination, attempt.claim, attempt.status, attempt.displaced
        )
    raise DeviceError(f"no configured, connected, free {platform} physical target")


def notices_for_displaced(
    displaced: Sequence[PhysicalClaim],
    *,
    action: Literal["transfer", "release"],
    actor_checkout: str,
    event_at: datetime,
) -> tuple[ClaimNotice, ...]:
    expiry = event_at + timedelta(days=CLAIM_NOTICE_DAYS)
    return tuple(
        ClaimNotice(
            claim.owner_checkout,
            claim.catalog_identity,
            claim.target_label,
            action,
            actor_checkout,
            event_at.isoformat(),
            expiry.isoformat(),
        )
        for claim in displaced
    )
