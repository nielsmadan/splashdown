"""Tests for splashdown registry behavior."""

from __future__ import annotations

import pytest

import splashdown as sd
from conftest import (
    _write_recipe,
)


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
    # Exactly one row for (checkout, PORT), and get_port returns the new value.
    rows = [r for r in registry._read_ports() if r[1] == str(checkout) and r[2] == "PORT"]
    assert len(rows) == 1
    assert registry.get_port(str(checkout), "PORT") == new


def test_registry_files_are_owner_only(registry):
    """kv/ports/devices TSVs can hold secrets — they must not be world-readable."""
    for f in (registry.port_file, registry.kv_file, registry.device_file):
        assert (f.stat().st_mode & 0o077) == 0, f"{f} is group/other-accessible"


def test_allocate_port_is_lock_serialized_under_thread_contention(registry, tmp_path, monkeypatch):
    """Many checkouts racing for the same range must each get a distinct port with
    no lost/duplicated rows — the flock is the module's whole reason to exist."""
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
    # One row per checkout, no corruption/duplication.
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
    # b should be allowed to take a's port now
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


@pytest.mark.parametrize("kind", ["port", "kv", "device"])
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
    else:
        registry.set_device(str(checkout), "simulator", "default", "UDID-X", "iPhone 17", "18.5")

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
    assert row.created_at  # ISO-ish, non-empty


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


def test_registry_gc_can_preserve_device_rows(registry, tmp_path):
    dead = tmp_path / "dead"
    registry.allocate_port(str(dead), "PORT", 18920, 18930)
    registry.set_kv(str(dead), "TOKEN", "value")
    registry.set_device(str(dead), "simulator", "default", "UDID-DEAD", "iPhone 17", "18.5")

    assert registry.gc(include_devices=False) == 2
    assert registry.all_for(str(dead)) == {}
    assert registry.get_device(str(dead), "simulator", "default") is not None


def test_registry_all_checkouts_aggregates_three_files(registry, tmp_path):
    # Distinct paths across ports.tsv, kv.tsv, devices.tsv.
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    c = tmp_path / "c"
    c.mkdir()
    registry.allocate_port(str(a), "PORT", 18100, 18110)
    registry.set_kv(str(b), "KEY", "v")
    registry.set_device(str(c), "simulator", "default", "UDID-C", "iPhone 17", "18.5")
    out = registry.all_checkouts()
    assert out == sorted([str(a), str(b), str(c)])


def test_registry_all_checkouts_dedupes_when_same_path_in_multiple_files(registry, checkout):
    registry.allocate_port(str(checkout), "PORT", 18200, 18210)
    registry.set_kv(str(checkout), "KEY", "v")
    registry.set_device(str(checkout), "simulator", "default", "UDID-X", "iPhone 17", "18.5")
    out = registry.all_checkouts()
    assert out == [str(checkout)]


def test_registry_all_checkouts_empty_returns_empty_list(registry):
    assert registry.all_checkouts() == []


def test_registry_gc_drops_orphan_device_rows(registry, tmp_path, monkeypatch):
    # Checkout path EXISTS but the registered sim's UDID is gone from xcrun.
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
    # Checkout exists; recipe declares PORT but not DART_PORT. The leftover
    # DART_PORT row (from an older recipe) should be reconciled away.
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
    s = registry.summary_for(str(a))
    assert s == {"port": 2, "kv": 1, "simulator": 1, "emulator": 1}


def test_registry_summary_for_unknown_checkout_returns_zeros(registry, tmp_path):
    assert registry.summary_for(str(tmp_path / "never-tracked")) == {
        "port": 0,
        "kv": 0,
        "simulator": 0,
        "emulator": 0,
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
