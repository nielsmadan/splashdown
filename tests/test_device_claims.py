from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

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


def _mock_ios_revalidation(monkeypatch, devices):
    calls = []

    def probe(argv, **_kwargs):
        calls.append(argv)
        return json.dumps({"result": {"devices": devices}}).encode()

    monkeypatch.setattr(sd.device_ios, "require_macos", lambda _operation: None)
    monkeypatch.setattr(sd.device_ios, "check_output_finite", probe)
    monkeypatch.setattr(
        sd.device_claims, "verify_ios_connection", sd.device_ios.verify_ios_connection
    )
    return calls


def _write_phone_target(checkout: Path, source: str) -> Path:
    path = checkout / sd.LOCAL_NAME if source == "local" else sd._global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[targets.device.pixel]\nplatform = "android"\nid = "ANDROID-1"\n')
    return path


@pytest.mark.parametrize("source", ["local", "global"])
@pytest.mark.parametrize(
    "invocation",
    [
        (["pixel"], "text"),
        (["pixel"], "json"),
        (["--available", "android"], "text"),
        (["--available", "android"], "json"),
    ],
)
def test_claim_and_release_without_recipe_or_env(
    registry, checkout, monkeypatch, capsys, source, invocation
):
    selection, fmt = invocation
    _write_phone_target(checkout, source)
    before = {path.name: path.read_bytes() for path in checkout.iterdir()}
    monkeypatch.setattr(sd.cli, "Registry", lambda: registry)
    discovery = []

    def discover(platform, **_kwargs):
        discovery.append(platform)
        return (PIXEL,)

    monkeypatch.setattr(sd.device_claims, "discover_physical_snapshot", discover)
    assert sd.main(["--cwd", str(checkout), "target", "claim", *selection, "--format", fmt]) == 0

    (claim,) = registry.all_claims()
    assert (claim.hardware_id, claim.target_label, claim.owner_checkout) == (
        "ANDROID-1",
        "pixel",
        str(checkout.resolve()),
    )
    assert claim.catalog_identity.startswith(f"{source}:")
    output = capsys.readouterr()
    if fmt == "json":
        payload = json.loads(output.out)
        assert payload["hardware_id"] == "ANDROID-1"
        assert payload["source"] == source
    elif selection == ["pixel"]:
        assert "claimed pixel (android ANDROID-1)" in output.err
    else:
        assert output.out == "pixel\n"

    assert sd.main(["--cwd", str(checkout), "target", "release", "pixel"]) == 0
    assert capsys.readouterr().err == "released pixel\n"
    assert registry.all_claims() == ()
    assert discovery == ["android"]
    assert {path.name: path.read_bytes() for path in checkout.iterdir()} == before


@pytest.mark.parametrize("hardware_id", ["192.0.2.10:5555", "adb-phone._adb-tls-connect._tcp"])
@pytest.mark.parametrize("selection", [["xiaomi"], ["--available", "android"]])
def test_free_android_target_can_be_claimed_despite_unrelated_network_claim(
    registry, checkout, monkeypatch, capsys, hardware_id, selection
):
    path = _write_phone_target(checkout, "global")
    path.write_text(
        path.read_text() + '\n[targets.device.xiaomi]\nplatform = "android"\nid = "ANDROID-2"\n'
    )
    owner = checkout.parent / "pixel-owner"
    owner.mkdir()
    old = sd.PhysicalClaim(
        f"global:{path.resolve()}:device:pixel",
        "android",
        hardware_id,
        "pixel",
        str(owner.resolve()),
        "2026-08-26T09:00:00+00:00",
    )
    registry._write_claims([old])
    monkeypatch.setattr(sd.cli, "Registry", lambda: registry)
    for module in (sd.device_claims, sd.target_commands):
        monkeypatch.setattr(
            module, "discover_physical_snapshot", lambda *_a, **_k: (PIXEL, PIXEL_OTHER)
        )
    command = ["--cwd", str(checkout), "target"]

    assert sd.main([*command, "--format", "json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(row["variant"], row["claim"]) for row in rows] == [
        ("pixel", "claimed"),
        ("xiaomi", "free"),
    ]

    assert sd.main([*command, "claim", *selection, "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert (result["target"], result["hardware_id"], result["status"]) == (
        "xiaomi",
        "ANDROID-2",
        "claimed",
    )
    preserved, claimed = registry.all_claims()
    assert preserved == old
    assert claimed.target_label == "xiaomi"
    assert claimed.owner_checkout == str(checkout.resolve())


@pytest.mark.parametrize("source", ["recipe", "local", "global"])
@pytest.mark.parametrize(
    "action", [["claim", "pixel"], ["claim", "--available", "android"], ["release", "pixel"]]
)
def test_claim_commands_preserve_ownership_with_invalid_config(
    registry, checkout, monkeypatch, capsys, source, action
):
    global_path = _write_phone_target(checkout, "global")
    paths = {
        "recipe": checkout / sd.RECIPE_NAME,
        "local": checkout / sd.LOCAL_NAME,
        "global": global_path,
    }
    path = paths[source]
    path.write_text("[targets\n")
    registry.attempt_claim(
        sd.PhysicalClaim(
            f"global:{global_path.resolve()}:device:pixel",
            "android",
            "ANDROID-1",
            "pixel",
            str(checkout.resolve()),
            "2026-11-02T12:00:00+00:00",
        )
    )
    before = registry.claim_file.read_bytes()
    monkeypatch.setattr(sd.cli, "Registry", lambda: registry)
    monkeypatch.setattr(
        sd.device_claims,
        "discover_physical_snapshot",
        lambda *_a, **_k: pytest.fail("invalid configuration reached discovery"),
    )

    assert sd.main(["--cwd", str(checkout), "target", *action]) == 1
    assert f"error: {path.name}: [document] invalid TOML" in capsys.readouterr().err
    assert registry.claim_file.read_bytes() == before
    assert path.read_text() == "[targets\n"


@pytest.mark.parametrize("failure", ["unreadable", "broken-symlink"])
def test_claim_rejects_unreadable_recipe(registry, checkout, monkeypatch, capsys, failure):
    _write_phone_target(checkout, "global")
    recipe = checkout / sd.RECIPE_NAME
    if failure == "broken-symlink":
        recipe.symlink_to(checkout / "missing.toml")
        expected = "No such file or directory"
    else:
        recipe.write_text("")
        original_open = Path.open

        def open_path(path, *args, **kwargs):
            if path == recipe:
                raise PermissionError(13, "Permission denied", str(path))
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", open_path)
        expected = "Permission denied"
    before = registry.claim_file.read_bytes()
    monkeypatch.setattr(sd.cli, "Registry", lambda: registry)
    monkeypatch.setattr(
        sd.device_claims,
        "discover_physical_snapshot",
        lambda *_a, **_k: pytest.fail("unreadable recipe reached discovery"),
    )

    assert sd.main(["--cwd", str(checkout), "target", "claim", "pixel"]) == 1
    error = capsys.readouterr().err
    assert error.startswith("error: ")
    assert expected in error
    assert sd.RECIPE_NAME in error
    assert registry.claim_file.read_bytes() == before


def test_configured_physical_targets_preserves_source_and_declaration_order(tmp_path):
    _write_recipe(
        tmp_path,
        """\
[targets.device.recipe-first]
platform = "ios"
id = "IOS-1"
[targets.device.recipe-second]
platform = "android"
id = "ANDROID-1"
""",
    )
    (tmp_path / sd.LOCAL_NAME).write_text(
        """\
[targets.device.local-first]
platform = "android"
id = "ANDROID-2"
"""
    )
    global_path = sd._global_config_path()
    global_path.parent.mkdir(parents=True)
    global_path.write_text(
        """\
[targets.device.global-first]
platform = "ios"
id = "IOS-2"
[targets.device.global-second]
platform = "android"
id = "ANDROID-3"
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
        _write_recipe(cwd, '[targets.device.phone]\nplatform = "ios"\nid = "IOS-1"\n')
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
        _write_recipe(cwd, '[targets.device.phone]\nplatform = "ios"\nid = "IOS-1"\n')
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
        (cwd / sd.LOCAL_NAME).write_text('[targets.device.phone]\nplatform = "ios"\nid = "IOS-1"\n')

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
    global_path.write_text('[targets.device.phone]\nplatform = "ios"\nid = "IOS-1"\n')

    assert sd.configured_physical_targets(first)[0].catalog_identity == (
        sd.configured_physical_targets(second)[0].catalog_identity
    )


def test_recipe_target_identity_falls_back_to_resolved_recipe_path(tmp_path, monkeypatch):
    _write_recipe(tmp_path, '[targets.device.phone]\nplatform = "ios"\nid = "IOS-1"\n')

    def no_git(_cwd: Path) -> sd.GitDirs:
        raise ValueError("not Git")

    monkeypatch.setattr(sd.device_claims, "git_dirs", no_git)

    assert sd.configured_physical_targets(tmp_path)[0].catalog_identity == (
        f"recipe:{(tmp_path / sd.RECIPE_NAME).resolve()}:device:phone"
    )


def test_global_variant_shadowed_by_project_uses_project_identity(tmp_path):
    _write_recipe(tmp_path, '[targets.device.phone]\nplatform = "ios"\nid = "IOS-1"\n')
    global_path = sd._global_config_path()
    global_path.parent.mkdir(parents=True)
    global_path.write_text('[targets.device.phone]\nplatform = "android"\nid = "ANDROID-1"\n')

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


@pytest.mark.parametrize(
    "snapshot,message",
    [
        ((), "no connected physical device"),
        (
            (PIXEL, {**PIXEL, "name": "Second connection"}),
            "multiple connected physical devices",
        ),
    ],
)
def test_claim_configured_target_rejects_unavailable_or_ambiguous_without_writing(
    registry, checkout, monkeypatch, snapshot, message
):
    target = _physical_target("phone", {"platform": "android", "id": "ANDROID-1"})
    monkeypatch.setattr(sd.device_claims, "discover_physical_snapshot", lambda *_a, **_k: snapshot)
    before = registry.claim_file.read_bytes()

    with pytest.raises(sd.DeviceError, match=message):
        sd.claim_configured_target(registry, checkout, target)

    assert registry.all_claims() == ()
    assert registry.claim_file.read_bytes() == before


@pytest.mark.parametrize("selection", ["named", "available"])
def test_ios_claim_revalidates_before_acquiring(registry, checkout, monkeypatch, selection):
    target = _physical_target("iphone", {"platform": "ios", "id": "IOS-1"})
    monkeypatch.setattr(sd.device_claims, "discover_physical_snapshot", lambda *_a, **_k: (IPHONE,))
    monkeypatch.setattr(
        sd.device_claims, "configured_physical_targets", lambda *_a, **_k: (target,)
    )
    probes = _mock_ios_revalidation(monkeypatch, [])
    before = registry.claim_file.read_bytes()

    with pytest.raises(sd.DeviceError, match=r"could not verify.*IOS-1.*no longer lists"):
        if selection == "named":
            sd.claim_configured_target(registry, checkout, target)
        else:
            sd.claim_available_target(registry, checkout, "ios")

    assert probes == [["xcrun", "devicectl", "list", "devices", "--json-output", "-"]]
    assert registry.all_claims() == ()
    assert registry.claim_file.read_bytes() == before


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
        sd.device_claims, "configured_physical_targets", lambda _cwd, **_kwargs: (first, second)
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
        sd.device_claims, "configured_physical_targets", lambda _cwd, **_kwargs: (first, second)
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
        sd.device_claims, "configured_physical_targets", lambda _cwd, **_kwargs: (first, second)
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
        sd.device_claims,
        "configured_physical_targets",
        lambda _cwd, **_kwargs: (taken, alias, fallback),
    )
    monkeypatch.setattr(
        sd.device_claims, "discover_physical_snapshot", lambda *_a, **_k: (PIXEL, IPHONE)
    )

    assert sd.claim_available_target(registry, checkout, "any").target == fallback


def test_claim_available_target_skips_ambiguous_target(registry, checkout, monkeypatch):
    ambiguous = _physical_target("ambiguous", {"platform": "android", "id": "ANDROID-1"})
    fallback = _physical_target("fallback", {"platform": "ios", "id": "IOS-1"})
    monkeypatch.setattr(
        sd.device_claims,
        "configured_physical_targets",
        lambda _cwd, **_kwargs: (ambiguous, fallback),
    )
    monkeypatch.setattr(
        sd.device_claims,
        "discover_physical_snapshot",
        lambda *_a, **_k: ({**PIXEL, "error": "ambiguous connections for ANDROID-1"}, IPHONE),
    )

    assert sd.claim_available_target(registry, checkout, "any").target == fallback


def test_claim_available_target_filters_requested_platform(registry, checkout, monkeypatch):
    ios = _physical_target("ios", {"platform": "ios", "id": "IOS-1"})
    android = _physical_target("android", {"platform": "android", "id": "ANDROID-1"})
    monkeypatch.setattr(
        sd.device_claims, "configured_physical_targets", lambda _cwd, **_kwargs: (ios, android)
    )
    monkeypatch.setattr(
        sd.device_claims, "discover_physical_snapshot", lambda *_a, **_k: (PIXEL, IPHONE)
    )

    assert sd.claim_available_target(registry, checkout, "android").target == android


def test_claim_available_target_never_selects_undeclared_hardware(registry, checkout, monkeypatch):
    target = _physical_target("iphone", {"platform": "ios", "id": "IOS-1"})
    monkeypatch.setattr(
        sd.device_claims, "configured_physical_targets", lambda _cwd, **_kwargs: (target,)
    )
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


def test_rebound_connection_is_rejected_before_claim(tmp_path, registry, monkeypatch):
    (tmp_path / "splashdown.toml").write_text(
        '[targets.device.phone]\nplatform="android"\nid="PHONE-A"\n'
    )
    target = sd.resolve_physical_target(tmp_path, "phone")
    observed = {
        "id": "PHONE-A",
        "platform": "android",
        "name": "Phone",
        "transport_id": "192.0.2.10:5555",
        "adb_transport_id": "7",
    }

    def verify(destination, expected, **_kwargs):
        assert destination.adb_transport_id == "7"
        assert expected == "PHONE-A"
        raise sd.DeviceError("connection changed")

    monkeypatch.setattr(sd.device_claims, "verify_android_connection", verify)
    with pytest.raises(sd.DeviceError, match="connection changed"):
        sd.claim_configured_target(registry, tmp_path, target, snapshot=[observed])
    assert registry.all_claims() == ()


def test_claim_revalidation_keeps_the_remaining_discovery_budget(tmp_path, registry, monkeypatch):
    target = _physical_target("pixel", {"platform": "android", "id": "ANDROID-1"})
    times = iter([100.0, 102.0])
    budgets = []
    with monkeypatch.context() as patch:
        patch.setattr(sd.device_claims.time, "monotonic", lambda: next(times))
        patch.setattr(
            sd.device_claims,
            "verify_android_connection",
            lambda _destination, _id, *, timeout: budgets.append(timeout),
        )
        result = sd.claim_configured_target(registry, tmp_path, target, snapshot=[PIXEL], timeout=5)
    assert result.status == "claimed"
    assert budgets == [3.0]


def test_run_rechecks_connection_after_preflight_and_preserves_claim(
    tmp_path, registry, monkeypatch
):
    (tmp_path / "splashdown.toml").write_text(
        '[targets.device.phone]\nplatform="android"\nid="PHONE-A"\n'
    )
    observed = {
        "id": "PHONE-A",
        "platform": "android",
        "name": "Phone",
        "transport_id": "192.0.2.10:5555",
        "adb_transport_id": "7",
    }
    current = ["PHONE-A"]
    checks = []

    def verify(destination, expected, **_kwargs):
        checks.append((destination.serial, destination.adb_transport_id, expected))
        if current[0] != expected:
            raise sd.DeviceError("connection changed")

    monkeypatch.setattr(sd.device_claims, "verify_android_connection", verify)
    monkeypatch.setattr(
        sd.device_claims, "discover_physical_snapshot", lambda *_a, **_k: [observed]
    )
    monkeypatch.setattr(sd.target_commands, "validate_device_run", lambda *_a: None)
    monkeypatch.setattr(
        sd.target_commands, "device_run_preflight", lambda *_a: current.__setitem__(0, "PHONE-B")
    )
    monkeypatch.setattr(
        sd.target_commands,
        "device_run",
        lambda *_a: pytest.fail("must not dispatch to replacement phone"),
    )
    with pytest.raises(sd.DeviceError, match="connection changed"):
        sd.cmd_run(tmp_path, registry, "device", "phone")
    assert checks == [("192.0.2.10:5555", "7", "PHONE-A")] * 2
    assert registry.all_claims()[0].hardware_id == "PHONE-A"


def test_ios_run_revalidates_after_preflight_and_preserves_existing_claim(
    registry, checkout, monkeypatch
):
    _write_recipe(checkout, '[targets.device.iphone]\nplatform="ios"\nid="IOS-1"\n')
    target = sd.resolve_physical_target(checkout, "iphone")
    previous = sd.PhysicalClaim(
        target.catalog_identity,
        "ios",
        "IOS-1",
        "iphone",
        str(checkout.resolve()),
        "2026-08-26T10:00:00+00:00",
    )
    registry.attempt_claim(previous)
    before = registry.claim_file.read_bytes()
    device = {
        "hardwareProperties": {"platform": "iOS", "udid": "IOS-1"},
        "deviceProperties": {"name": "Niels iPhone"},
        "connectionProperties": {"pairingState": "paired", "tunnelState": "disconnected"},
    }
    probes = _mock_ios_revalidation(monkeypatch, [device])
    monkeypatch.setattr(sd.device_claims, "discover_physical_snapshot", lambda *_a, **_k: (IPHONE,))
    monkeypatch.setattr(sd.target_commands, "validate_device_run", lambda *_a: None)

    def preflight(*_args):
        device["connectionProperties"]["pairingState"] = "unpaired"

    monkeypatch.setattr(sd.target_commands, "device_run_preflight", preflight)
    launch = Mock(return_value=0)
    monkeypatch.setattr(sd.target_commands, "device_run", launch)

    with pytest.raises(sd.DeviceError, match=r"could not verify.*IOS-1.*finish pairing"):
        sd.cmd_run(checkout, registry, "device", "iphone")

    assert probes == [["xcrun", "devicectl", "list", "devices", "--json-output", "-"]] * 2
    assert registry.all_claims() == (previous,)
    assert registry.claim_file.read_bytes() == before
    launch.assert_not_called()
