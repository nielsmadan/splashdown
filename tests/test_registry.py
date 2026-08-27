from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from multiprocessing import get_context

import pytest

import splashdown as sd
from conftest import (
    _write_recipe,
)


def _physical_claim(owner, **changes):
    row = sd.PhysicalClaim(
        catalog_identity="recipe:/repo:pixel",
        platform="android",
        hardware_id="wifi:5555",
        target_label="pixel",
        owner_checkout=str(owner),
        claimed_at="2026-08-26T09:00:00+00:00",
    )
    return replace(row, **changes)


def _claim_notice(owner, **changes):
    row = sd.ClaimNotice(
        previous_owner=str(owner),
        catalog_identity="recipe:/repo:pixel",
        target_label="pixel",
        action="transfer",
        actor_checkout="/checkouts/new-owner",
        event_at="2026-08-26T09:00:00+00:00",
        expires_at="2026-09-25T09:00:00+00:00",
    )
    return replace(row, **changes)


def _claim_flock_worker(paths, owner, start_event, results):
    registry = sd.Registry(
        port_file=sd.Path(paths[0]),
        kv_file=sd.Path(paths[1]),
        device_file=sd.Path(paths[2]),
        claim_file=sd.Path(paths[3]),
        claim_notice_file=sd.Path(paths[4]),
    )
    start_event.wait()
    result = registry.attempt_claim(_physical_claim(owner))
    results.put(result.status)


def test_claim_round_trip_and_malformed_rows_are_skipped(registry, checkout):
    row = _physical_claim(checkout)
    registry.claim_file.write_text(
        f"recipe:/repo:pixel\tandroid\twifi:5555\tpixel\t{checkout}"
        "\t2026-08-26T09:00:00+00:00"
        + "\n"
        + "too\tfew\tclaim\tfields\n"
        + "too\tmany\tclaim\tfields\tare\there\textra\n"
    )

    assert registry.all_claims(gc=False) == (row,)


def test_claim_notice_round_trip_and_malformed_rows_are_skipped(registry, checkout):
    row = _claim_notice(checkout)
    registry.claim_notice_file.write_text(
        f"{checkout}\trecipe:/repo:pixel\tpixel\ttransfer\t/checkouts/new-owner"
        "\t2026-08-26T09:00:00+00:00\t2026-09-25T09:00:00+00:00"
        + "\n"
        + "too\tfew\tnotice\tfields\n"
        + "too\tmany\tnotice\tfields\tare\there\textra\tagain\n"
    )

    assert registry.consume_claim_notices(
        str(checkout), now=datetime(2026, 8, 26, 10, tzinfo=UTC)
    ) == (row,)


@pytest.mark.parametrize(
    "field",
    [
        "catalog_identity",
        "platform",
        "hardware_id",
        "target_label",
        "owner_checkout",
        "claimed_at",
    ],
)
@pytest.mark.parametrize("value", ["bad\tvalue", "bad\nvalue", "bad\rvalue"])
def test_claim_forbidden_field_characters_name_the_field(registry, checkout, field, value):
    with pytest.raises(ValueError, match=field):
        registry.attempt_claim(_physical_claim(checkout, **{field: value}))


@pytest.mark.parametrize(
    "field",
    [
        "previous_owner",
        "catalog_identity",
        "target_label",
        "action",
        "actor_checkout",
        "event_at",
        "expires_at",
    ],
)
@pytest.mark.parametrize("value", ["bad\tvalue", "bad\nvalue", "bad\rvalue"])
def test_claim_notice_forbidden_field_characters_name_the_field(registry, checkout, field, value):
    with pytest.raises(ValueError, match=field):
        registry.add_claim_notices([_claim_notice(checkout, **{field: value})])


def test_claim_transaction_claim_owned_busy_and_forced_transfer(registry, tmp_path):
    owner_a = tmp_path / "owner-a"
    owner_b = tmp_path / "owner-b"
    owner_a.mkdir()
    owner_b.mkdir()

    first = registry.attempt_claim(_physical_claim(owner_a))
    same = registry.attempt_claim(
        _physical_claim(
            owner_a,
            hardware_id="wifi-new",
            claimed_at="2026-08-26T10:00:00+00:00",
        )
    )
    busy = registry.attempt_claim(
        _physical_claim(
            owner_b,
            hardware_id="wifi-new",
            claimed_at="2026-08-26T11:00:00+00:00",
        )
    )
    forced = registry.attempt_claim(
        _physical_claim(
            owner_b,
            hardware_id="wifi-new",
            claimed_at="2026-08-26T12:00:00+00:00",
        ),
        force=True,
    )

    assert first.status == "claimed"
    assert same.status == "owned"
    assert same.claim is not None
    assert same.claim.hardware_id == "wifi-new"
    assert first.claim is not None
    assert same.claim.claimed_at == first.claim.claimed_at
    assert busy.status == "busy"
    assert busy.conflicts == (same.claim,)
    assert forced.status == "claimed"
    assert forced.claim is not None
    assert forced.claim.claimed_at == "2026-08-26T12:00:00+00:00"
    assert forced.displaced == (same.claim,)
    assert registry.all_claims() == (forced.claim,)


def test_claim_transaction_catalog_conflicts_across_hardware_ids(registry, tmp_path):
    owner_a = tmp_path / "owner-a"
    owner_b = tmp_path / "owner-b"
    owner_a.mkdir()
    owner_b.mkdir()
    existing = registry.attempt_claim(_physical_claim(owner_a)).claim

    result = registry.attempt_claim(_physical_claim(owner_b, hardware_id="usb-new"))

    assert result.status == "busy"
    assert result.conflicts == (existing,)
    assert registry.all_claims() == (existing,)


def test_claim_transaction_hardware_conflicts_across_aliases(registry, tmp_path):
    owner_a = tmp_path / "owner-a"
    owner_b = tmp_path / "owner-b"
    owner_a.mkdir()
    owner_b.mkdir()
    existing = registry.attempt_claim(_physical_claim(owner_a)).claim

    result = registry.attempt_claim(_physical_claim(owner_b, catalog_identity="global:pixel-alias"))

    assert result.status == "busy"
    assert result.conflicts == (existing,)
    assert registry.all_claims() == (existing,)


def test_claim_transaction_hardware_identity_is_platform_scoped(registry, tmp_path):
    owner_a = tmp_path / "owner-a"
    owner_b = tmp_path / "owner-b"
    owner_a.mkdir()
    owner_b.mkdir()

    android = registry.attempt_claim(_physical_claim(owner_a, hardware_id="shared-id"))
    ios = registry.attempt_claim(
        _physical_claim(
            owner_b,
            catalog_identity="recipe:/repo:iphone",
            platform="ios",
            hardware_id="shared-id",
            target_label="iphone",
        )
    )

    assert android.status == "claimed"
    assert ios.status == "claimed"
    assert registry.all_claims() == (android.claim, ios.claim)


def test_claim_transaction_same_owner_hardware_alias_is_idempotent(registry, checkout):
    existing = registry.attempt_claim(_physical_claim(checkout)).claim

    result = registry.attempt_claim(
        _physical_claim(
            checkout,
            catalog_identity="global:pixel-alias",
            target_label="pixel-alias",
            claimed_at="2026-08-26T11:00:00+00:00",
        )
    )

    assert result.status == "owned"
    assert result.claim == existing
    assert registry.all_claims() == (existing,)


def test_claim_transaction_removes_dead_owners_lazily(registry, tmp_path):
    dead_owner = tmp_path / "dead-owner"
    live_owner = tmp_path / "live-owner"
    dead_owner.mkdir()
    live_owner.mkdir()
    registry.attempt_claim(_physical_claim(dead_owner))
    dead_owner.rmdir()

    result = registry.attempt_claim(
        _physical_claim(live_owner, claimed_at="2026-08-26T13:00:00+00:00")
    )

    assert result.status == "claimed"
    assert result.claim is not None
    assert result.claim.owner_checkout == str(live_owner)
    assert result.claim.claimed_at == "2026-08-26T13:00:00+00:00"
    assert registry.all_claims(gc=False) == (result.claim,)


def test_claim_transaction_allows_multiple_distinct_devices_per_owner(registry, checkout):
    first = registry.attempt_claim(_physical_claim(checkout))
    second = registry.attempt_claim(
        _physical_claim(
            checkout,
            catalog_identity="recipe:/repo:tablet",
            hardware_id="tablet-id",
            target_label="tablet",
            claimed_at="2026-08-26T10:00:00+00:00",
        )
    )

    assert first.status == "claimed"
    assert second.status == "claimed"
    assert registry.all_claims() == (first.claim, second.claim)


def test_physical_claim_flock_allows_exactly_one_process(registry, tmp_path):
    owners = [tmp_path / "owner-a", tmp_path / "owner-b"]
    for owner in owners:
        owner.mkdir()
    paths = tuple(
        str(path)
        for path in (
            registry.port_file,
            registry.kv_file,
            registry.device_file,
            registry.claim_file,
            registry.claim_notice_file,
        )
    )
    context = get_context("spawn")
    with context.Manager() as manager:
        start_event = manager.Event()
        results = manager.Queue()
        processes = [
            context.Process(
                target=_claim_flock_worker,
                args=(paths, str(owner), start_event, results),
            )
            for owner in owners
        ]
        for process in processes:
            process.start()
        start_event.set()
        for process in processes:
            process.join(timeout=5)

        assert [process.exitcode for process in processes] == [0, 0]
        assert sorted(results.get(timeout=1) for _ in processes) == ["busy", "claimed"]
        assert len(registry.all_claims()) == 1


def test_release_claim_owner_removes_catalog_row(registry, checkout):
    claim = registry.attempt_claim(_physical_claim(checkout)).claim

    result = registry.release_claim("recipe:/repo:pixel", str(checkout))

    assert result == sd.ClaimRelease("released", (claim,), ())
    assert registry.all_claims() == ()


def test_release_claim_non_owner_is_busy_and_unchanged(registry, tmp_path):
    owner = tmp_path / "owner"
    requester = tmp_path / "requester"
    owner.mkdir()
    requester.mkdir()
    claim = registry.attempt_claim(_physical_claim(owner)).claim

    result = registry.release_claim("recipe:/repo:pixel", str(requester))

    assert result == sd.ClaimRelease("busy", (), (claim,))
    assert registry.all_claims() == (claim,)


def test_release_claim_force_removes_other_owners_row(registry, tmp_path):
    owner = tmp_path / "owner"
    requester = tmp_path / "requester"
    owner.mkdir()
    requester.mkdir()
    claim = registry.attempt_claim(_physical_claim(owner)).claim

    result = registry.release_claim("recipe:/repo:pixel", str(requester), force=True)

    assert result == sd.ClaimRelease("released", (claim,), ())
    assert registry.all_claims() == ()


def test_release_claim_missing_is_idempotent(registry, checkout):
    assert registry.release_claim("recipe:/repo:missing", str(checkout)) == sd.ClaimRelease(
        "missing", (), ()
    )


def test_release_claims_removes_only_requesters_rows_in_file_order(registry, tmp_path):
    owner_a = tmp_path / "owner-a"
    owner_b = tmp_path / "owner-b"
    owner_a.mkdir()
    owner_b.mkdir()
    first = registry.attempt_claim(_physical_claim(owner_a)).claim
    other = registry.attempt_claim(
        _physical_claim(
            owner_b,
            catalog_identity="recipe:/repo:iphone",
            platform="ios",
            hardware_id="iphone-id",
            target_label="iphone",
        )
    ).claim
    second = registry.attempt_claim(
        _physical_claim(
            owner_a,
            catalog_identity="recipe:/repo:tablet",
            hardware_id="tablet-id",
            target_label="tablet",
        )
    ).claim

    released = registry.release_claims(str(owner_a))

    assert released == (first, second)
    assert registry.all_claims() == (other,)


def test_claim_notice_upsert_replaces_same_owner_and_catalog(registry, checkout):
    first = _claim_notice(checkout)
    replacement = _claim_notice(
        checkout,
        action="release",
        actor_checkout="/checkouts/later-actor",
        event_at="2026-08-27T09:00:00+00:00",
        expires_at="2026-09-26T09:00:00+00:00",
    )

    registry.add_claim_notices([first])
    registry.add_claim_notices([replacement])

    assert registry.consume_claim_notices(
        str(checkout), now=datetime(2026, 8, 27, 10, tzinfo=UTC)
    ) == (replacement,)


def test_claim_notice_retains_distinct_targets_and_owners(registry, tmp_path):
    owner_a = tmp_path / "owner-a"
    owner_b = tmp_path / "owner-b"
    owner_a.mkdir()
    owner_b.mkdir()
    first = _claim_notice(owner_a)
    second = _claim_notice(
        owner_a,
        catalog_identity="recipe:/repo:tablet",
        target_label="tablet",
    )
    third = _claim_notice(owner_b)

    registry.add_claim_notices([first, second, third])

    now = datetime(2026, 8, 26, 10, tzinfo=UTC)
    assert registry.consume_claim_notices(str(owner_a), now=now) == (first, second)
    assert registry.consume_claim_notices(str(owner_b), now=now) == (third,)


def test_claim_notice_consume_atomically_removes_delivered_rows(registry, checkout):
    notice = _claim_notice(checkout)
    registry.add_claim_notices([notice])
    now = datetime(2026, 8, 26, 10, tzinfo=UTC)

    assert registry.consume_claim_notices(str(checkout), now=now) == (notice,)
    assert registry.consume_claim_notices(str(checkout), now=now) == ()
    assert registry.claim_notice_file.read_text() == ""


def test_claim_notice_expires_at_thirty_day_boundary(registry, checkout):
    event = datetime(2026, 8, 1, 9, tzinfo=UTC)
    boundary = event + timedelta(days=sd.CLAIM_NOTICE_DAYS)
    expired = _claim_notice(
        checkout,
        expires_at=boundary.isoformat(),
    )
    live = _claim_notice(
        checkout,
        catalog_identity="recipe:/repo:tablet",
        target_label="tablet",
        expires_at=(boundary + timedelta(seconds=1)).isoformat(),
    )
    registry.add_claim_notices([expired, live])

    assert registry.consume_claim_notices(str(checkout), now=boundary) == (live,)


def test_claim_notice_consume_prunes_dead_owners(registry, tmp_path):
    dead_owner = tmp_path / "dead-owner"
    live_owner = tmp_path / "live-owner"
    dead_owner.mkdir()
    live_owner.mkdir()
    dead = _claim_notice(dead_owner)
    live = _claim_notice(live_owner)
    registry.add_claim_notices([dead, live])
    dead_owner.rmdir()

    now = datetime(2026, 8, 26, 10, tzinfo=UTC)
    assert registry.consume_claim_notices(str(live_owner), now=now) == (live,)
    assert registry.consume_claim_notices(str(dead_owner), now=now) == ()
    assert registry.claim_notice_file.read_text() == ""


@pytest.mark.parametrize("action", ["transfer", "release"])
def test_claim_notice_action_round_trip(registry, checkout, action):
    notice = _claim_notice(checkout, action=action)
    registry.add_claim_notices([notice])

    assert registry.consume_claim_notices(
        str(checkout), now=datetime(2026, 8, 26, 10, tzinfo=UTC)
    ) == (notice,)


@pytest.mark.parametrize(
    "expires_at",
    ["not-a-timestamp", "2026-09-25T09:00:00"],
    ids=["malformed", "timezone-naive"],
)
def test_claim_notice_invalid_expiry_write_is_rejected_without_rewrite(
    registry, checkout, expires_at
):
    registry.add_claim_notices([_claim_notice(checkout)])
    original = registry.claim_notice_file.read_text()

    with pytest.raises(ValueError, match="expires_at"):
        registry.add_claim_notices([_claim_notice(checkout, expires_at=expires_at)])

    assert registry.claim_notice_file.read_text() == original


@pytest.mark.parametrize(
    "expires_at",
    ["not-a-timestamp", "2026-09-25T09:00:00"],
    ids=["malformed", "timezone-naive"],
)
def test_claim_notice_invalid_expiry_disk_row_is_pruned_without_blocking_valid_notice(
    registry, checkout, expires_at
):
    valid = _claim_notice(
        checkout,
        catalog_identity="recipe:/repo:valid",
        target_label="valid",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    registry.claim_notice_file.write_text(
        f"{checkout}\trecipe:/repo:bad\tbad\ttransfer\t/checkouts/actor"
        f"\t2026-08-26T09:00:00+00:00\t{expires_at}\n"
        f"{checkout}\trecipe:/repo:valid\tvalid\ttransfer\t/checkouts/new-owner"
        "\t2026-08-26T09:00:00+00:00\t2099-01-01T00:00:00+00:00\n"
    )

    registry.gc()

    assert registry.claim_notice_file.read_text() == (
        f"{checkout}\trecipe:/repo:valid\tvalid\ttransfer\t/checkouts/new-owner"
        "\t2026-08-26T09:00:00+00:00\t2099-01-01T00:00:00+00:00\n"
    )
    assert registry.consume_claim_notices(
        str(checkout), now=datetime(2026, 8, 26, 10, tzinfo=UTC)
    ) == (valid,)
    assert registry.claim_notice_file.read_text() == ""


def test_port_allocate_persists(registry, checkout):
    p1 = registry.allocate_port(str(checkout), "METRO", 18081, 18100)
    p2 = registry.allocate_port(str(checkout), "METRO", 18081, 18100)
    assert p1 == p2


def test_allocate_port_replaces_out_of_range_pin_without_leaking_row(
    registry, checkout, monkeypatch
):
    """When a recipe's range changes under a live pin, the stale out-of-range row
    must be dropped, not left to shadow the new value and accrue duplicates."""
    monkeypatch.setattr(sd.registry, "_port_in_use", lambda port: False)
    old = registry.allocate_port(str(checkout), "PORT", 3000, 3100)
    assert 3000 <= old <= 3100
    new = registry.allocate_port(str(checkout), "PORT", 4000, 4100)
    assert 4000 <= new <= 4100
    rows = [r for r in registry._read_ports() if r[1] == str(checkout) and r[2] == "PORT"]
    assert len(rows) == 1
    assert registry.get_port(str(checkout), "PORT") == new


def test_registry_files_are_owner_only(registry):
    """kv/ports/devices TSVs can hold secrets — they must not be world-readable."""
    for f in (
        registry.port_file,
        registry.kv_file,
        registry.device_file,
        registry.claim_file,
        registry.claim_notice_file,
    ):
        assert (f.stat().st_mode & 0o077) == 0, f"{f} is group/other-accessible"


def test_registry_resolves_state_directory_at_construction(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"

    monkeypatch.setenv("XDG_STATE_HOME", str(first))
    first_registry = sd.Registry()
    monkeypatch.setenv("XDG_STATE_HOME", str(second))
    second_registry = sd.Registry()

    assert first_registry.state_dir == first / "splashdown"
    assert second_registry.state_dir == second / "splashdown"
    assert sd.state_directory() == second / "splashdown"


def test_allocate_port_is_lock_serialized_under_thread_contention(registry, tmp_path, monkeypatch):
    import threading

    monkeypatch.setattr(sd.registry, "_port_in_use", lambda port: False)
    n = 16
    checkouts = []
    for i in range(n):
        d = tmp_path / f"co{i}"
        d.mkdir()
        checkouts.append(str(d))
    results: dict[str, int] = {}
    barrier = threading.Barrier(n)

    def worker(path: str) -> None:
        barrier.wait()  # maximize the race
        results[path] = registry.allocate_port(path, "PORT", 9000, 9000 + n - 1)

    threads = [threading.Thread(target=worker, args=(c,)) for c in checkouts]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ports = sorted(results.values())
    assert len(set(ports)) == n, "two checkouts double-allocated the same port"
    assert ports == list(range(9000, 9000 + n))
    assert len(registry._read_ports()) == n


def test_two_checkouts_get_different_ports(registry, tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    pa = registry.allocate_port(str(a), "METRO", 18081, 18100)
    pb = registry.allocate_port(str(b), "METRO", 18081, 18100)
    assert pa != pb


def test_gc_frees_dead_checkout(registry, tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    pa = registry.allocate_port(str(a), "X", 18101, 18110)
    a.rmdir()  # simulate worktree removal
    pb = registry.allocate_port(str(b), "X", 18101, 18110)
    assert pb == pa


def test_kv_set_get(registry, checkout):
    registry.set_kv(str(checkout), "K", "v1")
    assert registry.get_kv(str(checkout), "K") == "v1"
    registry.set_kv(str(checkout), "K", "v2")
    assert registry.get_kv(str(checkout), "K") == "v2"


def test_get_or_create_kv_is_lock_serialized_under_thread_contention(registry, checkout):
    import threading

    count = 12
    barrier = threading.Barrier(count)
    factory_calls: list[int] = []
    results: list[str] = []
    errors: list[BaseException] = []
    registries = [
        sd.Registry(
            port_file=registry.port_file,
            kv_file=registry.kv_file,
            device_file=registry.device_file,
        )
        for _ in range(count)
    ]

    def worker(index: int) -> None:
        try:
            barrier.wait()

            def factory() -> str:
                factory_calls.append(index)
                return f"value-{index}"

            results.append(registries[index].get_or_create_kv(str(checkout), "RUN_ID", factory))
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(factory_calls) == 1
    assert len(set(results)) == 1
    assert registry.get_kv(str(checkout), "RUN_ID") == results[0]


@pytest.mark.parametrize("kind", ["port", "kv", "device", "claim", "claim_notice"])
def test_registry_mutations_atomically_replace_tsv(registry, checkout, monkeypatch, kind):
    replacements: list[tuple[str, str, int]] = []
    real_replace = sd.registry.os.replace

    def observe_replace(source, destination):
        source_path = sd.Path(source)
        destination_path = sd.Path(destination)
        assert source_path.parent == destination_path.parent
        replacements.append(
            (
                destination_path.read_text(),
                source_path.read_text(),
                source_path.stat().st_mode & 0o777,
            )
        )
        real_replace(source, destination)

    monkeypatch.setattr(sd.registry.os, "replace", observe_replace)
    monkeypatch.setattr(sd.registry, "_port_in_use", lambda _port: False)

    if kind == "port":
        registry.allocate_port(str(checkout), "PORT", 18400, 18410)
    elif kind == "kv":
        registry.set_kv(str(checkout), "KEY", "value")
    elif kind == "device":
        registry.set_device(str(checkout), "simulator", "default", "UDID-X", "iPhone 17", "18.5")
    elif kind == "claim":
        registry.attempt_claim(_physical_claim(checkout))
    else:
        registry.add_claim_notices([_claim_notice(checkout)])

    assert len(replacements) == 1
    old_text, replacement_text, replacement_mode = replacements[0]
    assert old_text == ""
    assert replacement_text
    assert replacement_mode == 0o600


def test_operation_locks_use_a_bounded_shard_set(registry, monkeypatch):
    from contextlib import contextmanager

    targets = set()

    @contextmanager
    def record_lock(path):
        targets.add(path)
        yield

    monkeypatch.setattr(registry, "_lock", record_lock)
    for index in range(2048):
        with registry.operation_lock(f"/checkout/{index}"):
            pass

    assert 1 < len(targets) <= sd.registry._OPERATION_LOCK_SHARDS
    assert {path.parent for path in targets} == {registry.kv_file.parent}


def test_release_clears_entries(registry, checkout):
    registry.allocate_port(str(checkout), "P", 18200, 18210)
    registry.set_kv(str(checkout), "K", "v")
    n = registry.release(str(checkout))
    assert n == 2
    assert registry.all_for(str(checkout)) == {}


def test_release_clears_claims_and_addressed_notices(registry, checkout):
    registry.attempt_claim(_physical_claim(checkout))
    registry.add_claim_notices([_claim_notice(checkout)])

    assert registry.release(str(checkout)) == 2
    assert registry.all_claims() == ()
    assert (
        registry.consume_claim_notices(str(checkout), now=datetime(2026, 8, 26, 10, tzinfo=UTC))
        == ()
    )


def test_all_for_returns_combined(registry, checkout):
    registry.allocate_port(str(checkout), "PORT", 18300, 18310)
    registry.set_kv(str(checkout), "ID", "abc")
    assert set(registry.all_for(str(checkout))) == {"PORT", "ID"}


def test_device_registry_set_and_get(registry, checkout):
    registry.set_device(str(checkout), "simulator", "default", "UDID-X", "iPhone 17", "18.5")
    row = registry.get_device(str(checkout), "simulator", "default")
    assert row.udid == "UDID-X"
    assert row.model == "iPhone 17"
    assert row.ios == "18.5"
    assert row.created_at


def test_device_registry_set_overwrites(registry, checkout):
    registry.set_device(str(checkout), "simulator", "default", "UDID-X", "iPhone 17", "18.5")
    registry.set_device(str(checkout), "simulator", "default", "UDID-Y", "iPhone 17", "19.0")
    row = registry.get_device(str(checkout), "simulator", "default")
    assert row.udid == "UDID-Y"
    assert row.ios == "19.0"


def test_device_registry_remove(registry, checkout):
    registry.set_device(str(checkout), "simulator", "default", "UDID-X", "iPhone 17", "18.5")
    registry.remove_device(str(checkout), "simulator", "default")
    assert registry.get_device(str(checkout), "simulator", "default") is None


def test_device_registry_managed_udids(registry, tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    registry.set_device(str(a), "simulator", "default", "UDID-A", "iPhone 17", "18.5")
    registry.set_device(str(b), "simulator", "default", "UDID-B", "iPhone 17", "18.5")
    assert registry.managed_udids() == {"UDID-A", "UDID-B"}


def test_device_registry_devices_for(registry, tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    registry.set_device(str(a), "simulator", "default", "UDID-A", "iPhone 17", "18.5")
    registry.set_device(str(a), "simulator", "small", "UDID-S", "iPhone 13 Mini", "18.5")
    registry.set_device(str(b), "simulator", "default", "UDID-B", "iPhone 17", "18.5")
    rows = registry.devices_for(str(a))
    assert {r.variant for r in rows} == {"default", "small"}


def test_device_registry_gc_drops_defunct(registry, tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    registry.set_device(str(a), "simulator", "default", "UDID-A", "iPhone 17", "18.5")
    a.rmdir()
    n = registry.gc_devices()
    assert n == 1
    assert registry.all_devices() == []


def test_device_registry_release_clears_devices_too(registry, checkout):
    registry.allocate_port(str(checkout), "PORT", 18900, 18910)
    registry.set_device(str(checkout), "simulator", "default", "UDID-X", "iPhone 17", "18.5")
    registry.release(str(checkout))
    assert registry.get_device(str(checkout), "simulator", "default") is None


def test_registry_gc_includes_devices(registry, tmp_path):
    a = tmp_path / "alive"
    a.mkdir()
    b = tmp_path / "dead"
    b.mkdir()
    registry.set_device(str(a), "simulator", "default", "UDID-A", "iPhone 17", "18.5")
    registry.set_device(str(b), "simulator", "default", "UDID-B", "iPhone 17", "18.5")
    b.rmdir()
    registry.gc()
    udids = {r.udid for r in registry.all_devices()}
    assert udids == {"UDID-A"}


def test_registry_gc_removes_dead_claims_and_expired_or_dead_notices(registry, tmp_path):
    dead_owner = tmp_path / "dead-owner"
    live_owner = tmp_path / "live-owner"
    dead_owner.mkdir()
    live_owner.mkdir()
    registry.attempt_claim(_physical_claim(dead_owner))
    registry.add_claim_notices(
        [
            _claim_notice(dead_owner, expires_at="2099-01-01T00:00:00+00:00"),
            _claim_notice(
                live_owner,
                catalog_identity="recipe:/repo:expired",
                target_label="expired",
                expires_at="2000-01-01T00:00:00+00:00",
            ),
            _claim_notice(
                live_owner,
                catalog_identity="recipe:/repo:live",
                target_label="live",
                expires_at="2099-01-01T00:00:00+00:00",
            ),
        ]
    )
    dead_owner.rmdir()

    assert registry.gc() == 3
    assert registry.all_claims() == ()
    assert registry.consume_claim_notices(
        str(live_owner), now=datetime(2026, 8, 26, 10, tzinfo=UTC)
    ) == (
        _claim_notice(
            live_owner,
            catalog_identity="recipe:/repo:live",
            target_label="live",
            expires_at="2099-01-01T00:00:00+00:00",
        ),
    )


def test_registry_gc_can_preserve_device_rows(registry, tmp_path):
    dead = tmp_path / "dead"
    registry.allocate_port(str(dead), "PORT", 18920, 18930)
    registry.set_kv(str(dead), "TOKEN", "value")
    registry.set_device(str(dead), "simulator", "default", "UDID-DEAD", "iPhone 17", "18.5")

    assert registry.gc(include_devices=False) == 2
    assert registry.all_for(str(dead)) == {}
    assert registry.get_device(str(dead), "simulator", "default") is not None


def test_registry_all_checkouts_aggregates_claims_with_existing_files(registry, tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    c = tmp_path / "c"
    c.mkdir()
    d = tmp_path / "d"
    d.mkdir()
    registry.allocate_port(str(a), "PORT", 18100, 18110)
    registry.set_kv(str(b), "KEY", "v")
    registry.set_device(str(c), "simulator", "default", "UDID-C", "iPhone 17", "18.5")
    registry.attempt_claim(_physical_claim(d))
    out = registry.all_checkouts()
    assert out == sorted([str(a), str(b), str(c), str(d)])


def test_registry_all_checkouts_dedupes_when_same_path_in_multiple_files(registry, checkout):
    registry.allocate_port(str(checkout), "PORT", 18200, 18210)
    registry.set_kv(str(checkout), "KEY", "v")
    registry.set_device(str(checkout), "simulator", "default", "UDID-X", "iPhone 17", "18.5")
    out = registry.all_checkouts()
    assert out == [str(checkout)]


def test_registry_all_checkouts_empty_returns_empty_list(registry):
    assert registry.all_checkouts() == []


def test_registry_gc_drops_orphan_device_rows(registry, tmp_path, monkeypatch):
    a = tmp_path / "alive"
    a.mkdir()
    registry.set_device(str(a), "simulator", "default", "UDID-GONE", "iPhone 17", "18.5")
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda udid: False)
    removed = registry.gc(device_orphan_check=sd.devices._is_orphan_device)
    assert removed >= 1
    assert registry.get_device(str(a), "simulator", "default") is None


def test_registry_gc_keeps_present_device_rows(registry, tmp_path, monkeypatch):
    a = tmp_path / "alive"
    a.mkdir()
    registry.set_device(str(a), "simulator", "default", "UDID-OK", "iPhone 17", "18.5")
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda udid: True)
    registry.gc(device_orphan_check=sd.devices._is_orphan_device)
    assert registry.get_device(str(a), "simulator", "default") is not None


def test_registry_gc_drops_orphan_android_avd_rows(registry, tmp_path, monkeypatch):
    a = tmp_path / "alive"
    a.mkdir()
    registry.set_device(str(a), "emulator", "default", "AVD-NAME", "pixel_9", "android-34")
    monkeypatch.setattr(sd.devices, "_android_avd_exists", lambda name: False)
    registry.gc(device_orphan_check=sd.devices._is_orphan_device)
    assert registry.get_device(str(a), "emulator", "default") is None


def test_registry_gc_drops_port_not_in_recipe(registry, tmp_path):
    a = tmp_path / "alive"
    a.mkdir()
    _write_recipe(a, '[resources.PORT]\ntype = "port"\nrange = [3000, 3100]\n')
    registry.allocate_port(str(a), "PORT", 3000, 3100)
    registry.allocate_port(str(a), "DART_PORT", 9100, 9200)
    registry.gc()
    keys = set(registry.all_for(str(a)))
    assert keys == {"PORT"}


def test_registry_gc_drops_kv_not_in_recipe(registry, tmp_path):
    a = tmp_path / "alive"
    a.mkdir()
    _write_recipe(a, '[resources.NAME]\ntype = "set"\n')
    registry.set_kv(str(a), "NAME", "kept")
    registry.set_kv(str(a), "STALE", "gone")
    registry.gc()
    assert registry.all_for(str(a)) == {"NAME": "kept"}


def test_registry_gc_keeps_entries_when_recipe_missing(registry, tmp_path):
    # Dir exists but no recipe — don't read that as "zero declared resources".
    a = tmp_path / "alive"
    a.mkdir()
    registry.allocate_port(str(a), "DART_PORT", 9100, 9200)
    registry.gc()
    assert set(registry.all_for(str(a))) == {"DART_PORT"}


def test_registry_gc_keeps_entries_when_recipe_unparseable(registry, tmp_path):
    a = tmp_path / "alive"
    a.mkdir()
    _write_recipe(a, "this is not = valid toml [[[")
    registry.allocate_port(str(a), "DART_PORT", 9100, 9200)
    registry.gc()
    assert set(registry.all_for(str(a))) == {"DART_PORT"}


def test_registry_summary_for_counts_by_source(registry, tmp_path):
    a = tmp_path / "co"
    a.mkdir()
    registry.allocate_port(str(a), "P1", 19700, 19710)
    registry.allocate_port(str(a), "P2", 19711, 19720)
    registry.set_kv(str(a), "K", "v")
    registry.set_device(str(a), "simulator", "default", "UDID-X", "iPhone 17", "18.5")
    registry.set_device(str(a), "emulator", "default", "AVD-X", "pixel_9", "android-34")
    registry.attempt_claim(_physical_claim(a))
    s = registry.summary_for(str(a))
    assert s == {"port": 2, "kv": 1, "simulator": 1, "emulator": 1, "claim": 1}


def test_device_tsv_decodes_platform_specific_records(registry, tmp_path):
    checkout = str(tmp_path)
    registry.set_device(checkout, "simulator", "default", "UDID", "iPhone 17", "18.5")
    registry.set_device(checkout, "emulator", "pixel", "my-avd", "pixel_9", "android-34")

    simulator = registry.get_device(checkout, "simulator", "default")
    emulator = registry.get_device(checkout, "emulator", "pixel")

    assert isinstance(simulator, sd.SimulatorRecord)
    assert simulator.identifier == "UDID"
    assert simulator.runtime == "18.5"
    assert isinstance(emulator, sd.EmulatorRecord)
    assert emulator.name == "my-avd"
    assert emulator.image == "android-34"
    assert all(len(line.split("\t")) == 7 for line in registry.device_file.read_text().splitlines())


def test_registry_summary_for_unknown_checkout_returns_zeros(registry, tmp_path):
    assert registry.summary_for(str(tmp_path / "never-tracked")) == {
        "port": 0,
        "kv": 0,
        "simulator": 0,
        "emulator": 0,
        "claim": 0,
    }


def test_short_path_uses_home_prefix(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sd.Path, "home", classmethod(lambda cls: tmp_path))
    assert sd._short_path(str(tmp_path / "wrksp" / "x")) == "~/wrksp/x"
    assert sd._short_path(str(tmp_path)) == "~"
    assert sd._short_path("/etc/foo") == "/etc/foo"


def test_summary_string_format():
    assert (
        sd._summary_string({"port": 2, "kv": 1, "simulator": 1, "emulator": 0})
        == "2 ports, 1 var, 1 sim"
    )
    assert sd._summary_string({"port": 1, "kv": 0, "simulator": 0, "emulator": 0}) == "1 port"
    assert sd._summary_string({"port": 0, "kv": 0, "simulator": 0, "emulator": 0}) == "—"
    assert (
        sd._summary_string({"port": 0, "kv": 0, "simulator": 2, "emulator": 2}) == "2 sims, 2 emus"
    )


def test_allocate_port_exhaustion_raises(registry, checkout, monkeypatch):
    monkeypatch.setattr(sd.registry, "_port_in_use", lambda port: False)
    assert registry.allocate_port(str(checkout), "A", 8000, 8000) == 8000
    with pytest.raises(RuntimeError):
        registry.allocate_port(str(checkout), "B", 8000, 8000)


def test_reconcile_with_recipes_drops_stale_key(registry, tmp_path):
    a = tmp_path / "alive"
    a.mkdir()
    _write_recipe(a, '[resources.PORT]\ntype = "port"\nrange = [3000, 3100]\n')
    registry.allocate_port(str(a), "PORT", 3000, 3100)
    registry.allocate_port(str(a), "STALE", 9100, 9200)
    registry.reconcile_with_recipes()
    keys = set(registry.all_for(str(a)))
    assert "PORT" in keys and "STALE" not in keys
