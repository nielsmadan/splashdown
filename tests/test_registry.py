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


def test_registry_gc_includes_devices(registry, tmp_path, monkeypatch):
    a = tmp_path / "alive"
    a.mkdir()
    b = tmp_path / "dead"
    b.mkdir()
    registry.set_device(str(a), "simulator", "default", "UDID-A", "iPhone 17", "18.5")
    registry.set_device(str(b), "simulator", "default", "UDID-B", "iPhone 17", "18.5")
    b.rmdir()
    # gc() now also drops orphan-UDID rows; pretend both UDIDs are live in xcrun
    # so this test isolates the defunct-checkout sweep.
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda udid: True)
    monkeypatch.setattr(sd.commands, "_ios_udid_exists", lambda udid: True)
    registry.gc()
    udids = {r.udid for r in registry.all_devices()}
    assert udids == {"UDID-A"}


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
    monkeypatch.setattr(sd.commands, "_ios_udid_exists", lambda udid: False)
    removed = registry.gc()
    assert removed >= 1
    assert registry.get_device(str(a), "simulator", "default") is None


def test_registry_gc_keeps_present_device_rows(registry, tmp_path, monkeypatch):
    a = tmp_path / "alive"
    a.mkdir()
    registry.set_device(str(a), "simulator", "default", "UDID-OK", "iPhone 17", "18.5")
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda udid: True)
    monkeypatch.setattr(sd.commands, "_ios_udid_exists", lambda udid: True)
    registry.gc()
    assert registry.get_device(str(a), "simulator", "default") is not None


def test_registry_gc_drops_orphan_android_avd_rows(registry, tmp_path, monkeypatch):
    a = tmp_path / "alive"
    a.mkdir()
    registry.set_device(str(a), "emulator", "default", "AVD-NAME", "pixel_9", "android-34")
    monkeypatch.setattr(sd.devices, "_android_avd_exists", lambda name: False)
    monkeypatch.setattr(sd.commands, "_android_avd_exists", lambda name: False)
    registry.gc()
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
