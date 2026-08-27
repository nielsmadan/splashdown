from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import splashdown as sd

IPHONE = {"id": "IOS-1", "name": "Niels iPhone", "platform": "ios"}
PIXEL = {"id": "ANDROID-1", "name": "Pixel 9", "platform": "android"}
PIXEL_OTHER = {"id": "ANDROID-2", "name": "Pixel 8", "platform": "android"}


def _write_recipe(cwd: Path, text: str) -> None:
    (cwd / sd.RECIPE_NAME).write_text(text)


def _physical_target(variant: str, spec: dict[str, str]) -> sd.ConfiguredPhysicalTarget:
    return sd.ConfiguredPhysicalTarget(
        variant=variant,
        source="recipe",
        catalog_identity=f"recipe:test:device:{variant}",
        spec=spec,
    )


def test_configured_physical_targets_preserves_source_and_declaration_order(tmp_path):
    _write_recipe(
        tmp_path,
        """\
[targets.device.recipe-first]
platform = "ios"
[targets.device.recipe-second]
platform = "android"
""",
    )
    (tmp_path / sd.LOCAL_NAME).write_text(
        """\
[targets.device.local-first]
platform = "android"
"""
    )
    global_path = sd._global_config_path()
    global_path.parent.mkdir(parents=True)
    global_path.write_text(
        """\
[targets.device.global-first]
platform = "ios"
[targets.device.global-second]
platform = "android"
"""
    )

    targets = sd.configured_physical_targets(tmp_path)

    assert [(target.variant, target.source) for target in targets] == [
        ("recipe-first", "recipe"),
        ("recipe-second", "recipe"),
        ("local-first", "local"),
        ("global-first", "global"),
        ("global-second", "global"),
    ]


def test_recipe_target_identity_is_shared_by_linked_worktrees(tmp_path, monkeypatch):
    common = tmp_path / "repo" / ".git"
    common.mkdir(parents=True)
    first = tmp_path / "repo"
    second = tmp_path / "worktree"
    second.mkdir()
    for cwd in (first, second):
        _write_recipe(cwd, '[targets.device.phone]\nplatform = "ios"\n')
    monkeypatch.setattr(sd.device_claims, "git_dirs", lambda _cwd: sd.GitDirs(common, common))

    assert sd.configured_physical_targets(first)[0].catalog_identity == (
        sd.configured_physical_targets(second)[0].catalog_identity
    )


def test_recipe_target_identity_differs_between_clones(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    for cwd in (first, second):
        _write_recipe(cwd, '[targets.device.phone]\nplatform = "ios"\n')
    monkeypatch.setattr(
        sd.device_claims,
        "git_dirs",
        lambda cwd: sd.GitDirs(cwd / ".git", cwd / ".git"),
    )

    assert sd.configured_physical_targets(first)[0].catalog_identity != (
        sd.configured_physical_targets(second)[0].catalog_identity
    )


def test_local_target_identity_is_checkout_scoped(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    for cwd in (first, second):
        _write_recipe(cwd, "")
        (cwd / sd.LOCAL_NAME).write_text('[targets.device.phone]\nplatform = "ios"\n')

    assert sd.configured_physical_targets(first)[0].catalog_identity != (
        sd.configured_physical_targets(second)[0].catalog_identity
    )


def test_global_target_identity_is_shared_between_projects(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _write_recipe(first, "")
    _write_recipe(second, "")
    global_path = sd._global_config_path()
    global_path.parent.mkdir(parents=True)
    global_path.write_text('[targets.device.phone]\nplatform = "ios"\n')

    assert sd.configured_physical_targets(first)[0].catalog_identity == (
        sd.configured_physical_targets(second)[0].catalog_identity
    )


def test_recipe_target_identity_falls_back_to_resolved_recipe_path(tmp_path, monkeypatch):
    _write_recipe(tmp_path, '[targets.device.phone]\nplatform = "ios"\n')

    def no_git(_cwd: Path) -> sd.GitDirs:
        raise ValueError("not Git")

    monkeypatch.setattr(sd.device_claims, "git_dirs", no_git)

    assert sd.configured_physical_targets(tmp_path)[0].catalog_identity == (
        f"recipe:{(tmp_path / sd.RECIPE_NAME).resolve()}:device:phone"
    )


def test_global_variant_shadowed_by_project_uses_project_identity(tmp_path):
    _write_recipe(tmp_path, '[targets.device.phone]\nplatform = "ios"\n')
    global_path = sd._global_config_path()
    global_path.parent.mkdir(parents=True)
    global_path.write_text('[targets.device.phone]\nplatform = "android"\n')

    targets = sd.configured_physical_targets(tmp_path)

    assert len(targets) == 1
    assert targets[0].source == "recipe"
    assert targets[0].catalog_identity.startswith("recipe:")


def test_claim_configured_target_claims_connected_target(registry, checkout, monkeypatch):
    target = _physical_target("pixel", {"platform": "android", "id": "ANDROID-1"})
    monkeypatch.setattr(sd.device_claims, "discover_physical_snapshot", lambda *_a, **_k: (PIXEL,))

    result = sd.claim_configured_target(registry, checkout, target)

    assert result.status == "claimed"
    assert result.destination == sd.AndroidDestination("Pixel 9", "ANDROID-1", owned=False)
    assert registry.all_claims() == (result.claim,)


def test_claim_configured_target_is_idempotent_for_owner(registry, checkout, monkeypatch):
    target = _physical_target("pixel", {"platform": "android", "id": "ANDROID-1"})
    monkeypatch.setattr(sd.device_claims, "discover_physical_snapshot", lambda *_a, **_k: (PIXEL,))
    sd.claim_configured_target(registry, checkout, target)

    result = sd.claim_configured_target(registry, checkout, target)

    assert result.status == "owned"
    assert len(registry.all_claims()) == 1


@pytest.mark.parametrize("snapshot", [(), (PIXEL, IPHONE)])
def test_claim_configured_target_rejects_unavailable_or_ambiguous_without_writing(
    registry, checkout, monkeypatch, snapshot
):
    target = _physical_target("phone", {})
    monkeypatch.setattr(sd.device_claims, "discover_physical_snapshot", lambda *_a, **_k: snapshot)

    with pytest.raises(sd.DeviceError):
        sd.claim_configured_target(registry, checkout, target)

    assert registry.all_claims() == ()


def test_claim_configured_target_reports_busy_owner_and_time(registry, checkout, monkeypatch):
    target = _physical_target("pixel", {"platform": "android", "id": "ANDROID-1"})
    owner = checkout.parent / "owner"
    owner.mkdir()
    registry.attempt_claim(
        sd.PhysicalClaim(
            target.catalog_identity,
            "android",
            "ANDROID-1",
            "pixel",
            str(owner.resolve()),
            "2026-08-26T10:00:00+00:00",
        )
    )
    monkeypatch.setattr(sd.device_claims, "discover_physical_snapshot", lambda *_a, **_k: (PIXEL,))

    with pytest.raises(sd.DeviceError) as error:
        sd.claim_configured_target(registry, checkout, target)

    assert str(owner.resolve()) in str(error.value)
    assert "2026-08-26T10:00:00+00:00" in str(error.value)
    assert "splash target claims" in str(error.value)
    assert "splash target claim pixel --force" in str(error.value)


def test_claim_configured_target_force_returns_displaced_claim(registry, checkout, monkeypatch):
    target = _physical_target("pixel", {"platform": "android", "id": "ANDROID-1"})
    owner = checkout.parent / "owner"
    owner.mkdir()
    previous = sd.PhysicalClaim(
        target.catalog_identity,
        "android",
        "ANDROID-1",
        "pixel",
        str(owner.resolve()),
        "2026-08-26T10:00:00+00:00",
    )
    registry.attempt_claim(previous)
    monkeypatch.setattr(sd.device_claims, "discover_physical_snapshot", lambda *_a, **_k: (PIXEL,))

    result = sd.claim_configured_target(registry, checkout, target, force=True)

    assert result.status == "claimed"
    assert result.displaced == (previous,)


def test_claim_available_target_uses_catalog_order_and_skips_unavailable_targets(
    registry, checkout, monkeypatch
):
    first = _physical_target("first", {"platform": "android", "id": "MISSING"})
    second = _physical_target("second", {"platform": "android", "id": "ANDROID-1"})
    monkeypatch.setattr(
        sd.device_claims, "configured_physical_targets", lambda _cwd: (first, second)
    )
    monkeypatch.setattr(sd.device_claims, "discover_physical_snapshot", lambda *_a, **_k: (PIXEL,))

    result = sd.claim_available_target(registry, checkout, "android")

    assert result.target == second


def test_claim_available_target_retries_after_atomic_race_loss(registry, checkout, monkeypatch):
    first = _physical_target("first", {"platform": "android", "id": "ANDROID-1"})
    second = _physical_target("second", {"platform": "ios", "id": "IOS-1"})
    competitor = checkout.parent / "competitor"
    competitor.mkdir()
    monkeypatch.setattr(
        sd.device_claims, "configured_physical_targets", lambda _cwd: (first, second)
    )
    monkeypatch.setattr(
        sd.device_claims, "discover_physical_snapshot", lambda *_a, **_k: (PIXEL, IPHONE)
    )
    original = registry.attempt_claim
    calls = 0

    def race(claim, *, force=False):
        nonlocal calls
        calls += 1
        if calls == 1:
            registry.attempt_claim(
                sd.PhysicalClaim(
                    claim.catalog_identity,
                    claim.platform,
                    claim.hardware_id,
                    claim.target_label,
                    str(competitor.resolve()),
                    claim.claimed_at,
                )
            )
        return original(claim, force=force)

    monkeypatch.setattr(registry, "attempt_claim", race)

    assert sd.claim_available_target(registry, checkout, "any").target == second


def test_claim_available_target_skips_same_owner_claim_created_during_race(
    registry, checkout, monkeypatch
):
    first = _physical_target("first", {"platform": "android", "id": "ANDROID-1"})
    second = _physical_target("second", {"platform": "ios", "id": "IOS-1"})
    monkeypatch.setattr(
        sd.device_claims, "configured_physical_targets", lambda _cwd: (first, second)
    )
    monkeypatch.setattr(
        sd.device_claims, "discover_physical_snapshot", lambda *_a, **_k: (PIXEL, IPHONE)
    )
    original = registry.attempt_claim
    calls = 0

    def race(claim, *, force=False):
        nonlocal calls
        calls += 1
        if calls == 1:
            original(
                sd.PhysicalClaim(
                    claim.catalog_identity,
                    claim.platform,
                    claim.hardware_id,
                    claim.target_label,
                    str(checkout.resolve()),
                    claim.claimed_at,
                )
            )
        return original(claim, force=force)

    monkeypatch.setattr(registry, "attempt_claim", race)

    assert sd.claim_available_target(registry, checkout, "any").target == second


def test_claim_available_target_skips_busy_hardware_alias(registry, checkout, monkeypatch):
    taken = _physical_target("taken", {"platform": "android", "id": "ANDROID-1"})
    alias = _physical_target("alias", {"platform": "android", "id": "ANDROID-1"})
    fallback = _physical_target("fallback", {"platform": "ios", "id": "IOS-1"})
    owner = checkout.parent / "owner"
    owner.mkdir()
    registry.attempt_claim(
        sd.PhysicalClaim(
            taken.catalog_identity,
            "android",
            "ANDROID-1",
            "taken",
            str(owner.resolve()),
            "2026-08-26T10:00:00+00:00",
        )
    )
    monkeypatch.setattr(
        sd.device_claims, "configured_physical_targets", lambda _cwd: (taken, alias, fallback)
    )
    monkeypatch.setattr(
        sd.device_claims, "discover_physical_snapshot", lambda *_a, **_k: (PIXEL, IPHONE)
    )

    assert sd.claim_available_target(registry, checkout, "any").target == fallback


def test_claim_available_target_skips_ambiguous_target(registry, checkout, monkeypatch):
    ambiguous = _physical_target("ambiguous", {"platform": "android"})
    fallback = _physical_target("fallback", {"platform": "ios", "id": "IOS-1"})
    monkeypatch.setattr(
        sd.device_claims, "configured_physical_targets", lambda _cwd: (ambiguous, fallback)
    )
    monkeypatch.setattr(
        sd.device_claims,
        "discover_physical_snapshot",
        lambda *_a, **_k: (PIXEL, PIXEL_OTHER, IPHONE),
    )

    assert sd.claim_available_target(registry, checkout, "any").target == fallback


def test_claim_available_target_filters_requested_platform(registry, checkout, monkeypatch):
    ios = _physical_target("ios", {"platform": "ios", "id": "IOS-1"})
    android = _physical_target("android", {"platform": "android", "id": "ANDROID-1"})
    monkeypatch.setattr(
        sd.device_claims, "configured_physical_targets", lambda _cwd: (ios, android)
    )
    monkeypatch.setattr(
        sd.device_claims, "discover_physical_snapshot", lambda *_a, **_k: (PIXEL, IPHONE)
    )

    assert sd.claim_available_target(registry, checkout, "android").target == android


def test_claim_available_target_never_selects_undeclared_hardware(registry, checkout, monkeypatch):
    target = _physical_target("iphone", {"platform": "ios", "id": "IOS-1"})
    monkeypatch.setattr(sd.device_claims, "configured_physical_targets", lambda _cwd: (target,))
    monkeypatch.setattr(sd.device_claims, "discover_physical_snapshot", lambda *_a, **_k: (PIXEL,))

    with pytest.raises(sd.DeviceError, match="no configured, connected, free any physical target"):
        sd.claim_available_target(registry, checkout, "any")


def test_notices_for_displaced_expire_thirty_days_after_event():
    displaced = sd.PhysicalClaim("catalog", "ios", "IOS-1", "phone", "/old", "then")
    event_at = datetime(2026, 8, 26, 10, 0, tzinfo=UTC)

    notices = sd.notices_for_displaced(
        (displaced,), action="transfer", actor_checkout="/new", event_at=event_at
    )

    assert notices == (
        sd.ClaimNotice(
            "/old",
            "catalog",
            "phone",
            "transfer",
            "/new",
            "2026-08-26T10:00:00+00:00",
            "2026-09-25T10:00:00+00:00",
        ),
    )
