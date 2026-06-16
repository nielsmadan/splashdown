"""Tests for splashdown.

Run with: python -m pytest tests/ -q
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import splashdown as sd


@pytest.fixture
def registry(tmp_path: Path) -> sd.Registry:
    return sd.Registry(
        port_file=tmp_path / "ports.tsv",
        kv_file=tmp_path / "kv.tsv",
        device_file=tmp_path / "devices.tsv",
    )


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    d = tmp_path / "co"
    d.mkdir()
    return d


# ---------- registry ----------


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


# ---------- device registry (devices.tsv) ----------


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


def _write_recipe(checkout: Path, body: str) -> None:
    (checkout / sd.RECIPE_NAME).write_text(body)


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
    # 2 ports, 1 kv, 1 sim, 1 emu
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


# ---------- ensure_fresh_sim ----------


def test_ensure_fresh_creates_when_missing(registry, checkout, monkeypatch):
    created = {}

    def fake_ensure(name, model, ios):
        created["call"] = (name, model, ios)
        return "UDID-NEW", "Shutdown"

    monkeypatch.setattr(sd.devices, "ios_ensure", fake_ensure)
    monkeypatch.setattr(sd.devices, "_ios_latest_runtime_version", lambda: "18.5")
    monkeypatch.setattr(sd.commands, "_ios_latest_runtime_version", lambda: "18.5")
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.commands, "_ios_udid_exists", lambda u: True)
    info = sd.ensure_fresh_sim(registry, checkout, "simulator", "default", {"model": "iPhone 17"})
    assert info["udid"] == "UDID-NEW"
    assert info["name"].endswith("/default")
    assert created["call"][1] == "iPhone 17"
    assert created["call"][2] == "18.5"
    assert registry.get_device(str(checkout.resolve()), "simulator", "default").udid == "UDID-NEW"


def test_ensure_fresh_recreates_when_ios_stale(registry, checkout, monkeypatch):
    abspath = str(checkout.resolve())
    registry.set_device(abspath, "simulator", "default", "UDID-OLD", "iPhone 17", "17.5")
    destroyed = []
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.commands, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.devices, "_ios_latest_runtime_version", lambda: "18.5")
    monkeypatch.setattr(sd.commands, "_ios_latest_runtime_version", lambda: "18.5")
    monkeypatch.setattr(sd.devices, "ios_destroy", destroyed.append)
    monkeypatch.setattr(sd.commands, "ios_destroy", destroyed.append)
    monkeypatch.setattr(sd.devices, "ios_ensure", lambda n, m, i: ("UDID-NEW", "Shutdown"))
    sd.ensure_fresh_sim(registry, checkout, "simulator", "default", {"model": "iPhone 17"})
    assert destroyed == ["UDID-OLD"]
    assert registry.get_device(abspath, "simulator", "default").udid == "UDID-NEW"


def test_ensure_fresh_keeps_when_pinned_and_current(registry, checkout, monkeypatch):
    abspath = str(checkout.resolve())
    registry.set_device(abspath, "simulator", "legacy", "UDID-X", "iPhone 12", "17.0")
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.commands, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.devices, "_ios_latest_runtime_version", lambda: "18.5")
    monkeypatch.setattr(sd.commands, "_ios_latest_runtime_version", lambda: "18.5")
    info = sd.ensure_fresh_sim(
        registry,
        checkout,
        "simulator",
        "legacy",
        {"model": "iPhone 12", "ios": "17.0"},
    )
    assert info["udid"] == "UDID-X"


def test_ensure_fresh_recreates_when_pinned_ios_mismatch(registry, checkout, monkeypatch):
    abspath = str(checkout.resolve())
    registry.set_device(abspath, "simulator", "legacy", "UDID-OLD", "iPhone 12", "17.0")
    destroyed = []
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.commands, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.devices, "ios_destroy", destroyed.append)
    monkeypatch.setattr(sd.commands, "ios_destroy", destroyed.append)
    monkeypatch.setattr(sd.devices, "ios_ensure", lambda n, m, i: ("UDID-NEW", "Shutdown"))
    monkeypatch.setattr(sd.devices, "_ios_latest_runtime_version", lambda: "18.5")
    monkeypatch.setattr(sd.commands, "_ios_latest_runtime_version", lambda: "18.5")
    sd.ensure_fresh_sim(
        registry,
        checkout,
        "simulator",
        "legacy",
        {"model": "iPhone 12", "ios": "17.5"},
    )
    assert destroyed == ["UDID-OLD"]


def test_ensure_fresh_recreates_when_model_changed(registry, checkout, monkeypatch):
    abspath = str(checkout.resolve())
    registry.set_device(abspath, "simulator", "default", "UDID-OLD", "iPhone 17", "18.5")
    destroyed = []
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.commands, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.devices, "_ios_latest_runtime_version", lambda: "18.5")
    monkeypatch.setattr(sd.commands, "_ios_latest_runtime_version", lambda: "18.5")
    monkeypatch.setattr(sd.devices, "ios_destroy", destroyed.append)
    monkeypatch.setattr(sd.commands, "ios_destroy", destroyed.append)
    monkeypatch.setattr(sd.devices, "ios_ensure", lambda n, m, i: ("UDID-NEW", "Shutdown"))
    # Recipe bumped from iPhone 17 -> iPhone 18.
    sd.ensure_fresh_sim(registry, checkout, "simulator", "default", {"model": "iPhone 18"})
    assert destroyed == ["UDID-OLD"]


def test_ensure_fresh_recreates_when_udid_gone(registry, checkout, monkeypatch):
    abspath = str(checkout.resolve())
    registry.set_device(abspath, "simulator", "default", "UDID-X", "iPhone 17", "18.5")
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: False)  # user nuked the sim
    monkeypatch.setattr(sd.commands, "_ios_udid_exists", lambda u: False)
    monkeypatch.setattr(sd.devices, "_ios_latest_runtime_version", lambda: "18.5")
    monkeypatch.setattr(sd.commands, "_ios_latest_runtime_version", lambda: "18.5")
    monkeypatch.setattr(sd.devices, "ios_ensure", lambda n, m, i: ("UDID-NEW", "Shutdown"))
    info = sd.ensure_fresh_sim(registry, checkout, "simulator", "default", {"model": "iPhone 17"})
    assert info["udid"] == "UDID-NEW"


# ---------- physical devices ----------

_IPHONE = {"id": "00008-PHONE", "name": "Niels's iPhone", "platform": "ios"}
_PIXEL = {"id": "PXL1234", "name": "Pixel_7", "platform": "android"}


def _stub_physical(monkeypatch, ios=None, android=None):
    monkeypatch.setattr(sd.devices, "_ios_physical_devices", lambda: list(ios or []))
    monkeypatch.setattr(sd.devices, "_android_physical_devices", lambda: list(android or []))


def test_ensure_physical_autopicks_lone_ios(monkeypatch):
    _stub_physical(monkeypatch, ios=[_IPHONE])
    info = sd.ensure_physical({})
    assert info == {
        "kind": "ios",
        "name": "Niels's iPhone",
        "physical": True,
        "udid": "00008-PHONE",
    }


def test_ensure_physical_autopicks_lone_android(monkeypatch):
    _stub_physical(monkeypatch, android=[_PIXEL])
    info = sd.ensure_physical({})
    assert info["kind"] == "android"
    assert info["serial"] == "PXL1234"
    assert info["physical"] is True


def test_ensure_physical_filters_by_id(monkeypatch):
    other = {"id": "OTHER", "name": "Spare", "platform": "ios"}
    _stub_physical(monkeypatch, ios=[_IPHONE, other])
    info = sd.ensure_physical({"id": "OTHER"})
    assert info["udid"] == "OTHER"


def test_ensure_physical_filters_by_name_case_insensitive(monkeypatch):
    other = {"id": "OTHER", "name": "Spare", "platform": "ios"}
    _stub_physical(monkeypatch, ios=[_IPHONE, other])
    info = sd.ensure_physical({"name": "niels"})
    assert info["udid"] == "00008-PHONE"


def test_ensure_physical_platform_scopes_autopick(monkeypatch):
    _stub_physical(monkeypatch, ios=[_IPHONE], android=[_PIXEL])
    info = sd.ensure_physical({"platform": "android"})
    assert info["serial"] == "PXL1234"


def test_ensure_physical_errors_when_none(monkeypatch):
    _stub_physical(monkeypatch)
    with pytest.raises(sd.DeviceError, match="no connected physical device"):
        sd.ensure_physical({})


def test_ensure_physical_errors_when_ambiguous(monkeypatch):
    _stub_physical(monkeypatch, ios=[_IPHONE], android=[_PIXEL])
    with pytest.raises(sd.DeviceError, match="multiple connected"):
        sd.ensure_physical({})


def test_physical_discover_tolerates_missing_ios_toolchain(monkeypatch):
    def _boom():
        raise sd.DeviceError("xcrun devicectl failed")

    monkeypatch.setattr(sd.devices, "_ios_physical_devices", _boom)
    monkeypatch.setattr(sd.devices, "_android_physical_devices", lambda: [_PIXEL])
    # Broad scan: iOS toolchain error is swallowed, android phone still found.
    assert sd.physical_discover() == [_PIXEL]
    # Explicit platform=ios: error propagates.
    with pytest.raises(sd.DeviceError):
        sd.physical_discover("ios")


def test_physical_status_states(monkeypatch):
    _stub_physical(monkeypatch, ios=[_IPHONE])
    assert sd.physical_status({}) == "connected"
    _stub_physical(monkeypatch)
    assert sd.physical_status({}) == "absent"
    _stub_physical(monkeypatch, ios=[_IPHONE], android=[_PIXEL])
    assert sd.physical_status({}) == "ambiguous"


def test_ios_physical_devices_parses_devicectl(monkeypatch):
    payload = json.dumps(
        {
            "result": {
                "devices": [
                    {  # wired & actively tunneled — included
                        "deviceProperties": {"name": "Wired iPhone"},
                        "hardwareProperties": {"udid": "WIRED", "platform": "iOS"},
                        "connectionProperties": {
                            "pairingState": "paired",
                            "tunnelState": "connected",
                        },
                    },
                    {  # wifi: paired but tunnel lazily disconnected — must still be included
                        "deviceProperties": {"name": "Wifi iPhone"},
                        "hardwareProperties": {"udid": "WIFI", "platform": "iOS"},
                        "connectionProperties": {
                            "pairingState": "paired",
                            "tunnelState": "disconnected",
                        },
                    },
                    {  # paired but gone (unavailable) — skipped
                        "deviceProperties": {"name": "Old iPad"},
                        "hardwareProperties": {"udid": "IPAD", "platform": "iOS"},
                        "connectionProperties": {
                            "pairingState": "paired",
                            "tunnelState": "unavailable",
                        },
                    },
                    {  # not paired with this Mac — skipped
                        "deviceProperties": {"name": "Stranger"},
                        "hardwareProperties": {"udid": "STRANGER", "platform": "iOS"},
                        "connectionProperties": {
                            "pairingState": "unpaired",
                            "tunnelState": "disconnected",
                        },
                    },
                ]
            }
        }
    ).encode()
    monkeypatch.setattr(sd.devices.subprocess, "check_output", lambda *a, **k: payload)
    devices = sd._ios_physical_devices()
    assert devices == [
        {"id": "WIRED", "name": "Wired iPhone", "platform": "ios"},
        {"id": "WIFI", "name": "Wifi iPhone", "platform": "ios"},
    ]


def test_android_physical_devices_excludes_emulators(monkeypatch):
    out = (
        b"List of devices attached\n"
        b"emulator-5554       device product:sdk model:sdk_gphone device:emu transport_id:1\n"
        b"PXL1234             device product:panther model:Pixel_7 device:panther transport_id:2\n"
        b"ZZZ                 unauthorized\n"
    )
    monkeypatch.setattr(sd.devices, "_android_bin", lambda name: "/fake/adb")
    monkeypatch.setattr(sd.commands, "_android_bin", lambda name: "/fake/adb")
    monkeypatch.setattr(sd.devices.subprocess, "check_output", lambda *a, **k: out)
    devices = sd._android_physical_devices()
    assert devices == [{"id": "PXL1234", "name": "Pixel_7", "platform": "android"}]


# ---------- physical: CLI run / start / stop / destroy ----------


def _write_physical_recipe(tmp_path):
    (tmp_path / "splashdown.toml").write_text("""
[targets.device.default]

[project]
framework = "flutter"
""")
    (tmp_path / "pubspec.yaml").write_text("name: demo\n")


def test_cli_run_physical_skips_boot_and_passes_id(tmp_path, monkeypatch):
    _write_physical_recipe(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _stub_physical(monkeypatch, ios=[_IPHONE])
    monkeypatch.setattr(
        sd.devices, "ios_boot", lambda u, s: pytest.fail("should not boot hardware")
    )
    monkeypatch.setattr(
        sd.commands, "ios_boot", lambda u, s: pytest.fail("should not boot hardware")
    )
    captured = {}

    def _fake_run(cwd, recipe, info):
        captured["info"] = info
        return 0

    monkeypatch.setattr(sd.devices, "device_run", _fake_run)
    monkeypatch.setattr(sd.commands, "device_run", _fake_run)
    rc = sd.main(["--cwd", str(tmp_path), "run", "device"])
    assert rc == 0
    assert captured["info"]["udid"] == "00008-PHONE"
    assert captured["info"]["physical"] is True


def test_cli_start_physical_reports_connected_without_boot(tmp_path, monkeypatch, capsys):
    _write_physical_recipe(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _stub_physical(monkeypatch, ios=[_IPHONE])
    monkeypatch.setattr(
        sd.devices, "ios_boot", lambda u, s: pytest.fail("should not boot hardware")
    )
    monkeypatch.setattr(
        sd.commands, "ios_boot", lambda u, s: pytest.fail("should not boot hardware")
    )
    rc = sd.main(["--cwd", str(tmp_path), "start", "device"])
    assert rc == 0
    assert "connected" in capsys.readouterr().err.lower()


def test_cli_stop_physical_is_noop(tmp_path, monkeypatch):
    _write_physical_recipe(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(
        sd.commands, "device_shutdown", lambda dt, name: pytest.fail("should not touch hardware")
    )
    rc = sd.main(["--cwd", str(tmp_path), "stop", "device"])
    assert rc == 0


def test_cli_destroy_physical_is_noop(tmp_path, monkeypatch):
    _write_physical_recipe(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(
        sd.commands, "device_destroy", lambda dt, name: pytest.fail("should not touch hardware")
    )
    rc = sd.main(["--cwd", str(tmp_path), "destroy", "device"])
    assert rc == 0


def test_cli_devices_lists_physical_status(tmp_path, monkeypatch, capsys):
    _write_physical_recipe(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _stub_physical(monkeypatch, ios=[_IPHONE])
    rc = sd.main(["--cwd", str(tmp_path), "target"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "device" in out
    assert "connected" in out


def test_device_add_physical_writes_id_and_platform(tmp_path):
    sd.target_add(tmp_path, "device", "my-phone", {"id": "ABC123", "platform": "ios", "name": None})
    text = (tmp_path / "splashdown.local.toml").read_text()
    assert "[targets.device.my-phone]" in text
    assert 'id = "ABC123"' in text
    assert 'platform = "ios"' in text


def test_ios_native_run_physical_uses_devicectl(tmp_path, monkeypatch):
    # Build a fake .app with an Info.plist so the run path reaches install/launch.
    app = tmp_path / "Demo.app"
    app.mkdir()
    import plistlib

    with (app / "Info.plist").open("wb") as f:
        plistlib.dump({"CFBundleIdentifier": "com.demo"}, f)

    recipe = sd.Recipe(
        {"project": {"ios": {"scheme": "Demo", "project": "Demo.xcodeproj"}}},
        tmp_path / "splashdown.toml",
    )
    calls = []
    monkeypatch.setattr(sd.profiles.subprocess, "call", lambda args, **k: calls.append(args) or 0)

    class _Done:
        stdout = json.dumps(
            [{"buildSettings": {"BUILT_PRODUCTS_DIR": str(tmp_path), "WRAPPER_NAME": "Demo.app"}}]
        )

    monkeypatch.setattr(sd.profiles.subprocess, "run", lambda *a, **k: _Done())

    info = {"kind": "ios", "udid": "00008-PHONE", "physical": True}
    rc = sd.profiles._ios_native_run(tmp_path, recipe, info)
    assert rc == 0
    flat = [" ".join(c) for c in calls]
    assert any("devicectl" in c and "install" in c for c in flat)
    assert any("devicectl" in c and "launch" in c for c in flat)
    assert not any("simctl" in c for c in flat)


# ---------- target_add / target_remove (new shape) ----------


def test_device_add_writes_nested_table(tmp_path):
    sd.target_add(tmp_path, "simulator", "repro-bug", {"model": "iPhone 16", "ios": "17.5"})
    text = (tmp_path / "splashdown.local.toml").read_text()
    assert "[targets.simulator.repro-bug]" in text
    assert 'model = "iPhone 16"' in text
    assert 'ios = "17.5"' in text


def test_device_add_rejects_collision_with_local(tmp_path):
    sd.target_add(tmp_path, "simulator", "repro", {"model": "A"})
    with pytest.raises(sd.DeviceError, match="already exists"):
        sd.target_add(tmp_path, "simulator", "repro", {"model": "B"})


def test_device_add_rejects_collision_with_recipe(tmp_path):
    (tmp_path / "splashdown.toml").write_text('[targets.simulator.default]\nmodel = "iPhone 17"\n')
    with pytest.raises(sd.DeviceError, match="recipe"):
        sd.target_add(tmp_path, "simulator", "default", {"model": "iPhone 16"})


def test_device_add_rejects_bad_type(tmp_path):
    with pytest.raises(sd.DeviceError, match="type"):
        sd.target_add(tmp_path, "not-a-type", "default", {})


def test_device_add_rejects_bad_variant(tmp_path):
    with pytest.raises(sd.DeviceError, match="variant"):
        sd.target_add(tmp_path, "simulator", "has spaces", {"model": "X"})


def test_device_remove_strips_local_variant(tmp_path):
    sd.target_add(tmp_path, "simulator", "repro", {"model": "X"})
    sd.target_add(tmp_path, "simulator", "other", {"model": "Y"})
    sd.target_remove(tmp_path, "simulator", "repro")
    lc = sd.LocalConfig.load(tmp_path / "splashdown.local.toml")
    assert "repro" not in lc.targets.get("simulator", {})
    assert "other" in lc.targets["simulator"]


def test_device_remove_refuses_recipe_variant(tmp_path):
    (tmp_path / "splashdown.toml").write_text('[targets.simulator.default]\nmodel = "iPhone 17"\n')
    with pytest.raises(sd.DeviceError, match="recipe"):
        sd.target_remove(tmp_path, "simulator", "default")


def test_device_remove_errors_when_missing(tmp_path):
    with pytest.raises(sd.DeviceError, match="no target"):
        sd.target_remove(tmp_path, "simulator", "ghost")


# ---------- splash run / boot (top-level) ----------


def _stub_ios_boot_chain(monkeypatch):
    monkeypatch.setattr(sd.devices, "_ios_latest_runtime_version", lambda: "18.5")
    monkeypatch.setattr(sd.commands, "_ios_latest_runtime_version", lambda: "18.5")
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.commands, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.devices, "_ios_current_state", lambda u: "Shutdown")
    monkeypatch.setattr(sd.commands, "_ios_current_state", lambda u: "Shutdown")
    monkeypatch.setattr(sd.devices, "ios_ensure", lambda n, m, i: ("UDID-NEW", "Shutdown"))
    monkeypatch.setattr(sd.devices, "ios_boot", lambda u, s: None)
    monkeypatch.setattr(sd.commands, "ios_boot", lambda u, s: None)


def test_cli_run_default_variant(tmp_path, monkeypatch):
    (tmp_path / "splashdown.toml").write_text("""
[targets.simulator.default]
model = "iPhone 17"

[project]
framework = "react-native"
""")
    (tmp_path / "package.json").write_text('{"dependencies":{"react-native":"0.83"}}')
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _stub_ios_boot_chain(monkeypatch)
    captured = {}

    def _fake_run(cwd, recipe, info):
        captured["info"] = info
        return 0

    monkeypatch.setattr(sd.devices, "device_run", _fake_run)
    monkeypatch.setattr(sd.commands, "device_run", _fake_run)
    rc = sd.main(["--cwd", str(tmp_path), "run", "simulator"])
    assert rc == 0
    assert captured["info"]["udid"] == "UDID-NEW"
    assert captured["info"]["name"].endswith("/default")


def test_cli_run_explicit_variant(tmp_path, monkeypatch):
    (tmp_path / "splashdown.toml").write_text("""
[targets.simulator.default]
model = "iPhone 17"

[targets.simulator.small-screen]
model = "iPhone 13 Mini"

[project]
framework = "react-native"
""")
    (tmp_path / "package.json").write_text('{"dependencies":{"react-native":"0.83"}}')
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _stub_ios_boot_chain(monkeypatch)
    captured = {}

    def _fake_run(cwd, recipe, info):
        captured["info"] = info
        return 0

    monkeypatch.setattr(sd.devices, "device_run", _fake_run)
    monkeypatch.setattr(sd.commands, "device_run", _fake_run)
    sd.main(["--cwd", str(tmp_path), "run", "simulator", "small-screen"])
    assert captured["info"]["name"].endswith("/small-screen")


def test_cli_run_errors_when_no_default_and_no_pick(tmp_path, monkeypatch, capsys):
    (tmp_path / "splashdown.toml").write_text("""
[targets.simulator.a]
model = "X"

[targets.simulator.b]
model = "Y"

[project]
framework = "react-native"
""")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    rc = sd.main(["--cwd", str(tmp_path), "run", "simulator"])
    assert rc == 1
    assert "default" in capsys.readouterr().err.lower()


def test_cli_boot_does_not_call_device_run(tmp_path, monkeypatch):
    (tmp_path / "splashdown.toml").write_text("""
[targets.simulator.default]
model = "iPhone 17"

[project]
framework = "react-native"
""")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _stub_ios_boot_chain(monkeypatch)
    called = {"device_run": False}

    def fake_run(cwd, recipe, info):
        called["device_run"] = True
        return 0

    monkeypatch.setattr(sd.devices, "device_run", fake_run)
    monkeypatch.setattr(sd.commands, "device_run", fake_run)
    rc = sd.main(["--cwd", str(tmp_path), "start", "simulator"])
    assert rc == 0
    assert called["device_run"] is False


def test_cli_devices_shows_recipe_and_local(tmp_path, monkeypatch):
    (tmp_path / "splashdown.toml").write_text(
        '[targets.simulator.default]\nmodel = "A"\n[project]\nframework = "react-native"\n'
    )
    (tmp_path / "splashdown.local.toml").write_text('[targets.simulator.repro]\nmodel = "B"\n')
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # Stub status checks to avoid hitting xcrun.
    monkeypatch.setattr(sd.devices, "device_status", lambda dtype, name: "absent")
    monkeypatch.setattr(sd.commands, "device_status", lambda dtype, name: "absent")
    rc = sd.main(["--cwd", str(tmp_path), "target"])
    assert rc == 0


def test_cli_stop_resolves_by_type_and_variant(tmp_path, monkeypatch):
    (tmp_path / "splashdown.toml").write_text("""
[targets.simulator.default]
model = "iPhone 17"

[project]
framework = "react-native"
""")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    captured = {}

    def _shutdown(dt, name):
        captured["call"] = (dt, name)

    monkeypatch.setattr(sd.devices, "device_shutdown", _shutdown)
    monkeypatch.setattr(sd.commands, "device_shutdown", _shutdown)
    rc = sd.main(["--cwd", str(tmp_path), "stop", "simulator"])
    assert rc == 0
    assert captured["call"][0] == "simulator"
    assert captured["call"][1].endswith("/default")


def test_cli_run_infers_dtype_when_only_one_declared(tmp_path, monkeypatch):
    (tmp_path / "splashdown.toml").write_text("""
[targets.simulator.default]
model = "iPhone 17"

[project]
framework = "react-native"
""")
    (tmp_path / "package.json").write_text('{"dependencies":{"react-native":"0.83"}}')
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _stub_ios_boot_chain(monkeypatch)
    captured = {}

    def _fake_run(cwd, recipe, info):
        captured["info"] = info
        return 0

    monkeypatch.setattr(sd.devices, "device_run", _fake_run)
    monkeypatch.setattr(sd.commands, "device_run", _fake_run)
    # No TYPE given — should resolve to the only declared type (simulator).
    rc = sd.main(["--cwd", str(tmp_path), "run"])
    assert rc == 0
    assert captured["info"]["kind"] == "ios"


def test_cli_status_reports_resources_and_port_state(tmp_path, monkeypatch, capsys):
    (tmp_path / "splashdown.toml").write_text("""
[resources.MY_PORT]
type  = "port"
range = [19000, 19010]
""")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # Provision first so the registry has an entry to report on.
    rc = sd.main(["--cwd", str(tmp_path)])
    assert rc == 0
    capsys.readouterr()  # discard provision output
    rc = sd.main(["--cwd", str(tmp_path), "status"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "MY_PORT=" in err
    # The state tag must be one of `[in use]` or `[free]` (port-typed resource).
    assert "[free]" in err or "[in use]" in err


def test_cli_status_local_positional_matches_bare(tmp_path, monkeypatch, capsys):
    """`splash status local` must produce the same output as bare `splash status`."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "splashdown.toml").write_text(
        '[resources.MY_PORT]\ntype = "port"\nrange = [19030, 19040]\n'
    )
    assert sd.main(["--cwd", str(tmp_path)]) == 0
    capsys.readouterr()
    assert sd.main(["--cwd", str(tmp_path), "status"]) == 0
    bare = capsys.readouterr().err
    assert sd.main(["--cwd", str(tmp_path), "status", "local"]) == 0
    explicit = capsys.readouterr().err
    assert bare == explicit


def test_cli_status_local_json_shape(tmp_path, monkeypatch, capsys):
    """Default-mode JSON must include checkout + resources + devices keys."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "splashdown.toml").write_text(
        '[resources.J_PORT]\ntype = "port"\nrange = [19050, 19060]\n'
    )
    assert sd.main(["--cwd", str(tmp_path)]) == 0
    capsys.readouterr()
    rc = sd.main(["--cwd", str(tmp_path), "--format", "json", "status", "local"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    # Local mode emits a flat per-checkout object (not the `checkouts` list).
    assert data["checkout"] == str(tmp_path.resolve())
    assert any(r["key"] == "J_PORT" for r in data["resources"])
    assert "targets" in data


def test_cli_status_physical_device_shows_connection_state(tmp_path, monkeypatch, capsys):
    """Regression: physical `device` targets must report connected/absent via
    physical_status, not `error: unknown target type` (which `device_status` raises)."""
    _write_physical_recipe(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _stub_physical(monkeypatch, ios=[_IPHONE])
    rc = sd.main(["--cwd", str(tmp_path), "--format", "json", "status", "local"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    device_entries = [t for t in data["targets"] if t["type"] == "device"]
    assert device_entries, "expected a device target in status output"
    assert device_entries[0]["status"] == "connected"


def test_cli_status_check_physical_device_absent_marks_missing(tmp_path, monkeypatch, capsys):
    """`status --check` must flag an unplugged physical device as missing, not error."""
    _write_physical_recipe(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _stub_physical(monkeypatch)  # nothing connected
    rc = sd.main(["--cwd", str(tmp_path), "--format", "json", "status", "local", "--check"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    device_entries = [t for t in data["targets"] if t["type"] == "device"]
    assert device_entries[0]["status"] == "absent"
    assert device_entries[0]["missing"] is True


def test_cli_status_all_on_empty_registry_renders_only_cwd(tmp_path, monkeypatch, capsys):
    """`splash status all` against a fresh state still produces a usable header row."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    rc = sd.main(["--cwd", str(tmp_path), "status", "all"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "PATH" in err
    assert "SUMMARY" in err


def test_cli_init_loader_override_writes_devbox_wiring(tmp_path, monkeypatch):
    """`splash init NAME --loader=devbox` wires devbox instead of mise."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    rc = sd.main(["--cwd", str(tmp_path), "init", "minimal", "--loader=devbox"])
    assert rc == 0
    assert (tmp_path / "devbox.json").exists()
    # mise.toml must NOT be present when devbox was explicitly requested.
    assert not (tmp_path / "mise.toml").exists()


def test_cli_device_prune_rejects_invalid_platform(tmp_path, monkeypatch, capsys):
    """Unknown platform → argparse usage error, no destructive call."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    called = {"prune": False}

    def _fail(*a, **kw):
        called["prune"] = True
        return 0

    monkeypatch.setattr(sd.commands, "cmd_target_prune", _fail)
    with pytest.raises(SystemExit) as exc:
        sd.main(["--cwd", str(tmp_path), "target", "prune", "mac"])
    assert exc.value.code == 2
    assert called["prune"] is False
    err = capsys.readouterr().err
    assert "invalid choice" in err


def test_cli_status_all_emits_compact_table(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    a = tmp_path / "co-a"
    a.mkdir()
    b = tmp_path / "co-b"
    b.mkdir()
    (a / "splashdown.toml").write_text('[resources.P_A]\ntype = "port"\nrange = [19100, 19110]\n')
    (b / "splashdown.toml").write_text('[resources.P_B]\ntype = "port"\nrange = [19200, 19210]\n')
    assert sd.main(["--cwd", str(a)]) == 0
    assert sd.main(["--cwd", str(b)]) == 0
    capsys.readouterr()
    rc = sd.main(["--cwd", str(a), "status", "all"])
    assert rc == 0
    err = capsys.readouterr().err
    # Header columns present (ISSUE only appears when at least one row has one).
    assert "PATH" in err
    assert "SUMMARY" in err
    assert "ISSUE" not in err  # healthy registry: column dropped
    # Both paths appear; resource counts (not names) appear.
    assert str(a) in err
    assert str(b) in err
    assert "1 port" in err
    # Resource names from the recipe must NOT appear in compact mode.
    assert "P_A=" not in err
    assert "P_B=" not in err


def test_cli_status_all_shows_issue_column_when_a_row_has_one(tmp_path, monkeypatch, capsys):
    """ISSUE column appears the moment any row needs to flag something.
    Without --check we already detect defunct paths; that's enough."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    alive = tmp_path / "alive"
    alive.mkdir()
    dead = tmp_path / "dead"
    dead.mkdir()
    (alive / "splashdown.toml").write_text('[resources.P]\ntype = "port"\nrange = [19340, 19350]\n')
    (dead / "splashdown.toml").write_text('[resources.Q]\ntype = "port"\nrange = [19440, 19450]\n')
    assert sd.main(["--cwd", str(alive)]) == 0
    assert sd.main(["--cwd", str(dead)]) == 0
    capsys.readouterr()
    (dead / "splashdown.toml").unlink()
    (dead / "splashdown.env").unlink()
    (dead / "splashdown.local.toml").unlink()
    dead.rmdir()
    rc = sd.main(["--cwd", str(alive), "status", "all"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "ISSUE" in err
    assert "defunct" in err


def test_cli_status_all_rows_sorted_alphabetically(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # Provision in non-alphabetical order on purpose.
    z = tmp_path / "zeta"
    z.mkdir()
    a = tmp_path / "alpha"
    a.mkdir()
    m = tmp_path / "mike"
    m.mkdir()
    for d in (z, a, m):
        (d / "splashdown.toml").write_text('[resources.P]\ntype = "port"\nrange = [19800, 19810]\n')
        assert sd.main(["--cwd", str(d)]) == 0
    capsys.readouterr()
    assert sd.main(["--cwd", str(a), "status", "all"]) == 0
    err = capsys.readouterr().err
    # Strip header line; verify the path-bearing rows appear in alpha order.
    body = err.split("\n", 1)[1]
    pos_a = body.index(str(a))
    pos_m = body.index(str(m))
    pos_z = body.index(str(z))
    assert pos_a < pos_m < pos_z


def test_cli_status_all_verbose_uses_block_view(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "splashdown.toml").write_text(
        '[resources.P_VERBOSE]\ntype = "port"\nrange = [19900, 19910]\n'
    )
    assert sd.main(["--cwd", str(tmp_path)]) == 0
    capsys.readouterr()
    rc = sd.main(["--cwd", str(tmp_path), "status", "all", "--verbose"])
    assert rc == 0
    err = capsys.readouterr().err
    # Verbose mode brings back resource names + the === path === block header.
    assert "P_VERBOSE=" in err
    assert "===" in err


def test_cli_status_check_table_status_column_flags_defunct(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    alive = tmp_path / "alive"
    alive.mkdir()
    dead = tmp_path / "dead"
    dead.mkdir()
    (alive / "splashdown.toml").write_text('[resources.P]\ntype = "port"\nrange = [19300, 19310]\n')
    (dead / "splashdown.toml").write_text('[resources.Q]\ntype = "port"\nrange = [19400, 19410]\n')
    assert sd.main(["--cwd", str(alive)]) == 0
    assert sd.main(["--cwd", str(dead)]) == 0
    capsys.readouterr()
    (dead / "splashdown.toml").unlink()
    (dead / "splashdown.env").unlink()
    (dead / "splashdown.local.toml").unlink()
    dead.rmdir()
    rc = sd.main(["--cwd", str(alive), "status", "all", "--check"])
    assert rc == 0
    err = capsys.readouterr().err
    # In the table, status column reads `defunct` (no brackets).
    assert "defunct" in err
    assert "defunct checkout" in err
    assert "`splash gc`" in err


def test_cli_status_check_verbose_keeps_bracket_tag(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    alive = tmp_path / "alive"
    alive.mkdir()
    dead = tmp_path / "dead"
    dead.mkdir()
    (alive / "splashdown.toml").write_text('[resources.P]\ntype = "port"\nrange = [19320, 19330]\n')
    (dead / "splashdown.toml").write_text('[resources.Q]\ntype = "port"\nrange = [19420, 19430]\n')
    assert sd.main(["--cwd", str(alive)]) == 0
    assert sd.main(["--cwd", str(dead)]) == 0
    capsys.readouterr()
    (dead / "splashdown.toml").unlink()
    (dead / "splashdown.env").unlink()
    (dead / "splashdown.local.toml").unlink()
    dead.rmdir()
    rc = sd.main(["--cwd", str(alive), "status", "all", "--check", "--verbose"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "[defunct]" in err


def test_cli_status_check_table_status_column_flags_orphan(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    a = tmp_path / "co"
    a.mkdir()
    (a / "splashdown.toml").write_text("")
    assert sd.main(["--cwd", str(a)]) == 0
    state_home = tmp_path / "state"
    reg = sd.Registry(
        port_file=state_home / "splashdown" / "ports.tsv",
        kv_file=state_home / "splashdown" / "kv.tsv",
        device_file=state_home / "splashdown" / "devices.tsv",
    )
    reg.set_device(str(a), "simulator", "default", "UDID-GHOST", "iPhone 17", "18.5")
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda udid: False)
    monkeypatch.setattr(sd.commands, "_ios_udid_exists", lambda udid: False)
    monkeypatch.setattr(sd.devices, "device_status", lambda dt, name: "absent")
    monkeypatch.setattr(sd.commands, "device_status", lambda dt, name: "absent")
    capsys.readouterr()
    rc = sd.main(["--cwd", str(a), "status", "all", "--check"])
    assert rc == 0
    err = capsys.readouterr().err
    # In the table, status column reads `orphan` (no brackets).
    assert "orphan" in err
    assert "orphan device" in err
    # Orphans are recreated by `device refresh` (plain `gc` won't touch an orphan
    # whose checkout still exists).
    assert "`splash target refresh`" in err


def test_cli_status_check_says_clean_when_nothing_stale(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "splashdown.toml").write_text(
        '[resources.P]\ntype = "port"\nrange = [19500, 19510]\n'
    )
    assert sd.main(["--cwd", str(tmp_path)]) == 0
    capsys.readouterr()
    rc = sd.main(["--cwd", str(tmp_path), "status", "all", "--check"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "all entries verified" in err


def test_cli_status_all_json_shape(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "splashdown.toml").write_text(
        '[resources.P]\ntype = "port"\nrange = [19600, 19610]\n'
    )
    assert sd.main(["--cwd", str(tmp_path)]) == 0
    capsys.readouterr()
    rc = sd.main(["--cwd", str(tmp_path), "--format", "json", "status", "all", "--check"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "checkouts" in data
    assert len(data["checkouts"]) == 1
    assert data["checkouts"][0]["checkout"] == str(tmp_path.resolve())
    assert "summary" in data
    assert data["summary"]["defunct_checkouts"] == 0


def test_cli_sync_reallocates_squatted_port(tmp_path, monkeypatch, capsys):
    (tmp_path / "splashdown.toml").write_text("""
[resources.HOT_PORT]
type  = "port"
range = [19500, 19510]
""")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    rc = sd.main(["--cwd", str(tmp_path)])
    assert rc == 0
    first = (tmp_path / "splashdown.env").read_text().strip()
    capsys.readouterr()
    # Squat the assigned port from a different socket.
    port_str = first.split("=", 1)[1]
    import socket as _sock

    squatter = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
    squatter.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
    squatter.bind(("127.0.0.1", int(port_str)))
    squatter.listen(1)
    try:
        rc = sd.main(["--cwd", str(tmp_path), "sync"])
        assert rc == 0
    finally:
        squatter.close()
    second = (tmp_path / "splashdown.env").read_text().strip()
    assert second != first  # reallocated to a new port


def test_cli_release_clears_one_key(tmp_path, monkeypatch, capsys):
    (tmp_path / "splashdown.toml").write_text("""
[resources.GONE]
type  = "port"
range = [19600, 19610]
""")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    sd.main(["--cwd", str(tmp_path)])
    capsys.readouterr()
    rc = sd.main(["--cwd", str(tmp_path), "env", "release", "GONE"])
    assert rc == 0
    rc = sd.main(["--cwd", str(tmp_path), "env", "get", "GONE"])
    assert rc == 1  # key gone


def test_cli_env_list_and_get(tmp_path, monkeypatch, capsys):
    (tmp_path / "splashdown.toml").write_text(
        '[resources.PORT]\ntype = "port"\nrange = [19700, 19710]\n'
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    sd.main(["--cwd", str(tmp_path)])  # sync → allocate PORT
    capsys.readouterr()
    assert sd.main(["--cwd", str(tmp_path), "env", "get", "PORT"]) == 0
    assert capsys.readouterr().out.strip().isdigit()
    assert sd.main(["--cwd", str(tmp_path), "env"]) == 0  # bare list
    assert "PORT=" in capsys.readouterr().out


def test_cli_env_set(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert sd.main(["--cwd", str(tmp_path), "env", "set", "K=v1"]) == 0
    assert sd.main(["--cwd", str(tmp_path), "env", "get", "K"]) == 0
    assert capsys.readouterr().out.strip() == "v1"


def test_cli_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        sd.main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "splashdown" in out
    assert re.search(r"\d+\.\d+\.\d+", out)


def test_cli_bare_device_lists(tmp_path, monkeypatch):
    (tmp_path / "splashdown.toml").write_text(
        '[targets.simulator.default]\nmodel = "X"\n[project]\nframework = "react-native"\n'
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(sd.devices, "device_status", lambda dtype, name: "absent")
    monkeypatch.setattr(sd.commands, "device_status", lambda dtype, name: "absent")
    rc = sd.main(["--cwd", str(tmp_path), "target"])
    assert rc == 0


def test_cli_device_remove_destroys_instance_by_default(tmp_path, monkeypatch):
    (tmp_path / "splashdown.toml").write_text('[project]\nframework = "react-native"\n')
    (tmp_path / "splashdown.local.toml").write_text(
        '[targets.simulator.repro]\nmodel = "iPhone 17"\n'
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    destroyed: list[tuple[str, str]] = []
    monkeypatch.setattr(sd.devices, "device_destroy", lambda dt, name: destroyed.append((dt, name)))
    monkeypatch.setattr(
        sd.commands, "device_destroy", lambda dt, name: destroyed.append((dt, name))
    )
    rc = sd.main(["--cwd", str(tmp_path), "target", "remove", "simulator", "repro"])
    assert rc == 0
    assert destroyed and destroyed[0][0] == "simulator"
    assert "[targets.simulator.repro]" not in (tmp_path / "splashdown.local.toml").read_text()


def test_cli_device_remove_keep_instance_skips_destroy(tmp_path, monkeypatch):
    (tmp_path / "splashdown.toml").write_text('[project]\nframework = "react-native"\n')
    (tmp_path / "splashdown.local.toml").write_text(
        '[targets.simulator.repro]\nmodel = "iPhone 17"\n'
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    destroyed: list[tuple[str, str]] = []
    monkeypatch.setattr(sd.devices, "device_destroy", lambda dt, name: destroyed.append((dt, name)))
    monkeypatch.setattr(
        sd.commands, "device_destroy", lambda dt, name: destroyed.append((dt, name))
    )
    rc = sd.main(
        [
            "--cwd",
            str(tmp_path),
            "target",
            "remove",
            "simulator",
            "repro",
            "--keep-instance",
        ]
    )
    assert rc == 0
    assert destroyed == []
    assert "[targets.simulator.repro]" not in (tmp_path / "splashdown.local.toml").read_text()


# ---------- device gc / prune ----------


def test_device_gc_drops_defunct_checkouts(registry, tmp_path, monkeypatch):
    a = tmp_path / "gone"
    a.mkdir()
    b = tmp_path / "live"
    b.mkdir()
    registry.set_device(str(a), "simulator", "default", "UDID-A", "iPhone 17", "18.5")
    registry.set_device(str(b), "simulator", "default", "UDID-B", "iPhone 17", "18.5")
    a.rmdir()
    destroyed = []
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.commands, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.devices, "ios_destroy", destroyed.append)
    monkeypatch.setattr(sd.commands, "ios_destroy", destroyed.append)
    destroyed_count, pruned_count = sd.cmd_target_gc(registry, all_=False)
    assert destroyed_count == 1
    assert pruned_count == 0
    assert destroyed == ["UDID-A"]
    assert {r.udid for r in registry.all_devices()} == {"UDID-B"}


def test_device_gc_all_drops_stale_latest_but_keeps_pinned(registry, tmp_path, monkeypatch):
    checkout = tmp_path / "co"
    checkout.mkdir()
    (checkout / "splashdown.toml").write_text("""
[targets.simulator.default]
model = "iPhone 17"

[targets.simulator.legacy]
model = "iPhone 12"
ios   = "17.0"
""")
    abspath = str(checkout.resolve())
    registry.set_device(
        abspath, "simulator", "default", "UDID-DEFAULT", "iPhone 17", "17.5"
    )  # stale latest
    registry.set_device(
        abspath, "simulator", "legacy", "UDID-LEGACY", "iPhone 12", "17.0"
    )  # pinned, current
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.commands, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.devices, "_ios_latest_runtime_version", lambda: "18.5")
    monkeypatch.setattr(sd.commands, "_ios_latest_runtime_version", lambda: "18.5")
    destroyed = []
    monkeypatch.setattr(sd.devices, "ios_destroy", destroyed.append)
    monkeypatch.setattr(sd.commands, "ios_destroy", destroyed.append)
    destroyed_count, pruned_count = sd.cmd_target_gc(registry, all_=True)
    assert destroyed_count == 0  # no defunct checkouts
    assert pruned_count == 1  # one stale "latest" variant pruned
    assert destroyed == ["UDID-DEFAULT"]
    assert {r.udid for r in registry.all_devices()} == {"UDID-LEGACY"}


def test_cli_gc_destroys_orphan_sims_and_prunes_rows(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    dead = tmp_path / "dead"  # a checkout dir that won't exist at gc time
    reg = sd.Registry()
    reg.allocate_port(str(dead), "PORT", 19800, 19810)
    reg.set_device(str(dead), "simulator", "default", "UDID-DEAD", "iPhone 17", "18.5")
    destroyed = []
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.commands, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.devices, "ios_destroy", destroyed.append)
    monkeypatch.setattr(sd.commands, "ios_destroy", destroyed.append)
    # dead/ never created on disk → it's a defunct checkout
    rc = sd.main(["--cwd", str(tmp_path), "gc"])
    assert rc == 0
    assert "UDID-DEAD" in destroyed  # sim torn down
    assert reg.get_device(str(dead), "simulator", "default") is None
    assert str(dead) not in reg.all_checkouts()  # port row pruned too


# ---------- device refresh ----------


def test_device_refresh_recreates_stale_latest(registry, tmp_path, monkeypatch):
    checkout = tmp_path / "co"
    checkout.mkdir()
    (checkout / "splashdown.toml").write_text('[targets.simulator.default]\nmodel = "iPhone 17"\n')
    abspath = str(checkout.resolve())
    registry.set_device(abspath, "simulator", "default", "UDID-OLD", "iPhone 17", "17.5")
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.commands, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.devices, "_ios_latest_runtime_version", lambda: "18.5")
    monkeypatch.setattr(sd.commands, "_ios_latest_runtime_version", lambda: "18.5")
    destroyed = []
    monkeypatch.setattr(sd.devices, "ios_destroy", destroyed.append)
    monkeypatch.setattr(sd.commands, "ios_destroy", destroyed.append)
    monkeypatch.setattr(sd.devices, "ios_ensure", lambda n, m, i: ("UDID-NEW", "Shutdown"))
    monkeypatch.setattr(
        sd.devices, "ios_boot", lambda *a, **k: pytest.fail("refresh must not boot")
    )
    monkeypatch.setattr(
        sd.commands, "ios_boot", lambda *a, **k: pytest.fail("refresh must not boot")
    )
    rc = sd.cmd_target_refresh(registry)
    assert rc == 0
    assert destroyed == ["UDID-OLD"]
    assert registry.get_device(abspath, "simulator", "default").udid == "UDID-NEW"


def test_device_refresh_leaves_fresh_and_pinned_untouched(registry, tmp_path, monkeypatch):
    checkout = tmp_path / "co"
    checkout.mkdir()
    (checkout / "splashdown.toml").write_text(
        '[targets.simulator.default]\nmodel = "iPhone 17"\n\n'
        '[targets.simulator.legacy]\nmodel = "iPhone 12"\nios   = "17.0"\n'
    )
    abspath = str(checkout.resolve())
    registry.set_device(
        abspath, "simulator", "default", "UDID-DEFAULT", "iPhone 17", "18.5"
    )  # fresh latest
    registry.set_device(
        abspath, "simulator", "legacy", "UDID-LEGACY", "iPhone 12", "17.0"
    )  # pinned current
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.commands, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.devices, "_ios_latest_runtime_version", lambda: "18.5")
    monkeypatch.setattr(sd.commands, "_ios_latest_runtime_version", lambda: "18.5")
    destroyed = []
    monkeypatch.setattr(sd.devices, "ios_destroy", destroyed.append)
    monkeypatch.setattr(sd.commands, "ios_destroy", destroyed.append)
    monkeypatch.setattr(
        sd.devices, "ios_ensure", lambda *a: pytest.fail("nothing should be recreated")
    )
    rc = sd.cmd_target_refresh(registry)
    assert rc == 0
    assert destroyed == []
    assert {r.udid for r in registry.all_devices()} == {"UDID-DEFAULT", "UDID-LEGACY"}


def test_device_refresh_ios_skips_emulator(registry, tmp_path, monkeypatch):
    checkout = tmp_path / "co"
    checkout.mkdir()
    (checkout / "splashdown.toml").write_text('[targets.emulator.default]\ndevice = "pixel_9"\n')
    abspath = str(checkout.resolve())
    registry.set_device(
        abspath,
        "emulator",
        "default",
        "avd-name",
        "pixel_9",
        "system-images;android-33;google_apis;arm64-v8a",  # stale vs latest below
    )
    touched = []
    monkeypatch.setattr(sd.devices, "_android_avd_exists", lambda n: True)
    monkeypatch.setattr(sd.commands, "_android_avd_exists", lambda n: True)
    monkeypatch.setattr(
        sd.devices,
        "_android_latest_image",
        lambda: "system-images;android-34;google_apis;arm64-v8a",
    )
    monkeypatch.setattr(
        sd.commands,
        "_android_latest_image",
        lambda: "system-images;android-34;google_apis;arm64-v8a",
    )
    monkeypatch.setattr(sd.devices, "android_destroy", touched.append)
    monkeypatch.setattr(sd.commands, "android_destroy", touched.append)
    monkeypatch.setattr(sd.devices, "android_ensure", lambda *a: touched.append("ensure"))
    rc = sd.cmd_target_refresh(registry, platforms=("ios",))
    assert rc == 0
    assert touched == []  # emulator row skipped entirely
    assert {r.udid for r in registry.all_devices()} == {"avd-name"}


def test_device_refresh_drops_defunct_and_undeclared(registry, tmp_path, monkeypatch):
    gone = tmp_path / "gone"
    gone.mkdir()
    live = tmp_path / "live"
    live.mkdir()
    (live / "splashdown.toml").write_text("")  # variant `old` is no longer declared
    registry.set_device(str(gone), "simulator", "default", "UDID-GONE", "iPhone 17", "18.5")
    registry.set_device(
        str(live.resolve()), "simulator", "old", "UDID-UNDECLARED", "iPhone 17", "18.5"
    )
    gone.rmdir()
    destroyed = []
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.commands, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.devices, "ios_destroy", destroyed.append)
    monkeypatch.setattr(sd.commands, "ios_destroy", destroyed.append)
    monkeypatch.setattr(
        sd.devices, "ios_ensure", lambda *a: pytest.fail("nothing should be recreated")
    )
    rc = sd.cmd_target_refresh(registry)
    assert rc == 0
    assert set(destroyed) == {"UDID-GONE", "UDID-UNDECLARED"}
    assert list(registry.all_devices()) == []


def test_cli_status_check_flags_stale_device(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    co = tmp_path / "co"
    co.mkdir()
    (co / "splashdown.toml").write_text('[targets.simulator.default]\nmodel = "iPhone 17"\n')
    state_home = tmp_path / "state"
    reg = sd.Registry(
        port_file=state_home / "splashdown" / "ports.tsv",
        kv_file=state_home / "splashdown" / "kv.tsv",
        device_file=state_home / "splashdown" / "devices.tsv",
    )
    reg.set_device(str(co.resolve()), "simulator", "default", "UDID-OLD", "iPhone 17", "17.5")
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.commands, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.devices, "_ios_latest_runtime_version", lambda: "18.5")
    monkeypatch.setattr(sd.commands, "_ios_latest_runtime_version", lambda: "18.5")
    monkeypatch.setattr(sd.commands, "device_status", lambda dt, name: "shutdown")
    capsys.readouterr()
    rc = sd.main(["--cwd", str(co), "status", "--check"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "[stale]" in err
    assert "stale device" in err
    assert "`splash target refresh`" in err


def test_cli_status_check_flags_missing_device(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    co = tmp_path / "co"
    co.mkdir()
    (co / "splashdown.toml").write_text('[targets.simulator.default]\nmodel = "iPhone 17"\n')
    # Declared but never provisioned: no registry row, sim absent.
    monkeypatch.setattr(sd.commands, "device_status", lambda dt, name: "absent")
    capsys.readouterr()
    rc = sd.main(["--cwd", str(co), "status", "--check"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "[missing]" in err
    assert "missing device" in err
    assert "`splash run`" in err


def test_device_prune_lists_only_unmanaged(registry, monkeypatch, capsys):
    fake_devices = {
        "iOS 18.5": [
            {
                "name": "myapp/feat-x/default",
                "udid": "MANAGED",
                "isAvailable": True,
                "state": "Shutdown",
            },
            {"name": "iPhone 17", "udid": "FOREIGN-1", "isAvailable": True, "state": "Shutdown"},
            {"name": "iPad Air", "udid": "FOREIGN-2", "isAvailable": True, "state": "Shutdown"},
        ]
    }
    monkeypatch.setattr(sd.devices, "_xcrun_json", lambda args: {"devices": fake_devices})
    monkeypatch.setattr(sd.commands, "_xcrun_json", lambda args: {"devices": fake_devices})
    registry.set_device("/tmp/something", "simulator", "default", "MANAGED", "iPhone 17", "18.5")
    rc = sd.cmd_target_prune(registry, yes=False, dry_run=True, platforms=("ios",))
    assert rc == 0
    err = capsys.readouterr().err
    assert "FOREIGN-1" in err
    assert "FOREIGN-2" in err
    assert "MANAGED" not in err
    assert "--dry-run" in err


def test_device_prune_yes_destroys_unmanaged(registry, monkeypatch):
    fake_devices = {
        "iOS 18.5": [
            {"name": "iPhone 17", "udid": "FOREIGN", "isAvailable": True, "state": "Shutdown"},
        ]
    }
    monkeypatch.setattr(sd.devices, "_xcrun_json", lambda args: {"devices": fake_devices})
    monkeypatch.setattr(sd.commands, "_xcrun_json", lambda args: {"devices": fake_devices})
    destroyed: list[str] = []
    shut: list[str] = []
    monkeypatch.setattr(sd.devices, "ios_destroy", destroyed.append)
    monkeypatch.setattr(sd.commands, "ios_destroy", destroyed.append)
    monkeypatch.setattr(sd.devices, "ios_shutdown", shut.append)
    monkeypatch.setattr(sd.commands, "ios_shutdown", shut.append)
    rc = sd.cmd_target_prune(registry, yes=True, dry_run=False, platforms=("ios",))
    assert rc == 0
    assert destroyed == ["FOREIGN"]
    assert shut == ["FOREIGN"]


def test_device_prune_noop_when_nothing_unmanaged(registry, monkeypatch, capsys):
    monkeypatch.setattr(sd.devices, "_xcrun_json", lambda args: {"devices": {}})
    monkeypatch.setattr(sd.commands, "_xcrun_json", lambda args: {"devices": {}})
    rc = sd.cmd_target_prune(registry, yes=True, dry_run=False, platforms=("ios",))
    assert rc == 0
    assert "nothing" in capsys.readouterr().err.lower()


def test_cli_device_prune_platform_positional_ios(tmp_path, monkeypatch):
    """`splash device prune ios` should pass platforms=("ios",) only."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    captured = {}

    def _fake_prune(reg, *, yes, dry_run, platforms):
        captured["platforms"] = platforms
        return 0

    monkeypatch.setattr(sd.commands, "cmd_target_prune", _fake_prune)
    rc = sd.main(["--cwd", str(tmp_path), "target", "prune", "ios", "--yes", "--dry-run"])
    assert rc == 0
    assert captured["platforms"] == ("ios",)


def test_cli_device_prune_default_is_both(tmp_path, monkeypatch):
    """No positional → both platforms."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    captured = {}

    def _fake_prune(reg, *, yes, dry_run, platforms):
        captured["platforms"] = platforms
        return 0

    monkeypatch.setattr(sd.commands, "cmd_target_prune", _fake_prune)
    rc = sd.main(["--cwd", str(tmp_path), "target", "prune", "--yes", "--dry-run"])
    assert rc == 0
    assert captured["platforms"] == ("ios", "android")


def test_cli_device_prune_all_is_both(tmp_path, monkeypatch):
    """Explicit `all` matches the no-arg default."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    captured = {}

    def _fake_prune(reg, *, yes, dry_run, platforms):
        captured["platforms"] = platforms
        return 0

    monkeypatch.setattr(sd.commands, "cmd_target_prune", _fake_prune)
    rc = sd.main(["--cwd", str(tmp_path), "target", "prune", "all", "--yes", "--dry-run"])
    assert rc == 0
    assert captured["platforms"] == ("ios", "android")


# ---------- presets ----------


def test_rn_preset_declares_default_ios_variant(tmp_path):
    sd.cmd_init(tmp_path, preset="rn")
    recipe = sd.Recipe.load(tmp_path / "splashdown.toml")
    assert "default" in recipe.targets.get("simulator", {})
    assert recipe.targets["simulator"]["default"]["model"]
    assert "SIM_NAME" not in recipe.resources


def test_flutter_preset_declares_both_defaults(tmp_path):
    sd.cmd_init(tmp_path, preset="flutter")
    recipe = sd.Recipe.load(tmp_path / "splashdown.toml")
    assert "default" in recipe.targets.get("simulator", {})
    assert "default" in recipe.targets.get("emulator", {})
    assert "SIM_NAME" not in recipe.resources


def test_local_skeleton_documents_additions(tmp_path):
    sd.cmd_init(tmp_path, preset="rn")
    text = (tmp_path / "splashdown.local.toml").read_text()
    assert "additional" in text.lower() or "additions" in text.lower()
    assert "simulator" in text
    assert "splash target add" in text


# ---------- templates ----------


def test_template_basic_vars(tmp_path):
    cwd = tmp_path / "myrepo.feat"
    cwd.mkdir()
    scope = sd._make_scope(cwd, "feat", {})
    assert sd.render_template("{{ cwd }}", scope) == "myrepo.feat"
    assert sd.render_template("port-{{ basename(cwd_abs) }}", scope) == "port-myrepo.feat"
    assert sd.render_template("{{ slug(cwd) }}", scope) == "myrepo-feat"


def test_template_cross_resource(tmp_path):
    cwd = tmp_path / "x"
    cwd.mkdir()
    scope = sd._make_scope(cwd, "", {"PORT": "8081"})
    assert sd.render_template("http://localhost:{{ PORT }}", scope) == "http://localhost:8081"


def test_template_refs():
    refs = sd.template_refs("http://x:{{ PORT }}/{{ basename(cwd) }}")
    assert "PORT" in refs
    assert "basename" in refs


def test_template_error_on_bad_expr(tmp_path):
    cwd = tmp_path / "x"
    cwd.mkdir()
    scope = sd._make_scope(cwd, "", {})
    with pytest.raises(sd.TemplateError):
        sd.render_template("{{ no_such_var }}", scope)


@pytest.mark.parametrize(
    "payload",
    [
        "{{ ().__class__ }}",
        "{{ cwd.__class__ }}",
        "{{ [].__class__.__base__.__subclasses__() }}",
        '{{ __import__("os") }}',
        "{{ ().__class__.__bases__[0].__subclasses__() }}",
        "{{ lambda: 1 }}",
        "{{ [x for x in (1, 2)] }}",
    ],
)
def test_template_rejects_sandbox_escapes(tmp_path, payload):
    # The restricted evaluator must reject attribute access, dunders, lambdas,
    # and comprehensions — the building blocks of every eval-sandbox escape.
    cwd = tmp_path / "x"
    cwd.mkdir()
    scope = sd._make_scope(cwd, "main", {})
    with pytest.raises(sd.TemplateError):
        sd.render_template(payload, scope)


def test_template_allows_slicing_and_nested_calls(tmp_path):
    cwd = tmp_path / "foo" / "bar"
    cwd.mkdir(parents=True)
    scope = sd._make_scope(cwd, "main", {})
    assert len(sd.render_template("{{ uuid()[:8] }}", scope)) == 8
    assert sd.render_template("{{ basename(dirname(cwd_abs)) }}", scope) == "foo"


@pytest.mark.parametrize("bad", ["has\ttab", "has\nnewline", "has\rcarriage"])
def test_registry_rejects_control_chars_in_value(registry, bad):
    with pytest.raises(ValueError, match="tab or newline"):
        registry.set_kv("/checkout/a", "KEY", bad)


def test_registry_rejects_control_chars_in_checkout_path(registry):
    with pytest.raises(ValueError, match="tab or newline"):
        registry.set_kv("/checkout\twith-tab", "KEY", "value")


def test_registry_rejects_control_chars_in_device_field(registry):
    with pytest.raises(ValueError, match="tab or newline"):
        registry.set_device("/co", "simulator", "default", "udid\ninjected", "iPhone 17", "18.5")


def test_ios_boot_raises_deviceerror_on_boot_failure(monkeypatch):
    def fake_run(cmd, *a, **k):
        if "boot" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boot failed: no space")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(sd.devices.subprocess, "run", fake_run)
    with pytest.raises(sd.DeviceError, match="simctl boot failed"):
        sd.devices.ios_boot("UDID-X", "Shutdown")


def test_ios_boot_tolerates_already_booted_race(monkeypatch):
    def fake_run(cmd, *a, **k):
        if "boot" in cmd:
            return subprocess.CompletedProcess(
                cmd, 149, stdout="", stderr="Unable to boot device in current state: Booted"
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(sd.devices.subprocess, "run", fake_run)
    sd.devices.ios_boot("UDID-X", "Shutdown")  # benign race must not raise


# ---------- recipe / topo ----------


def test_recipe_loads(tmp_path):
    p = tmp_path / "splashdown.toml"
    p.write_text("""
[resources.A]
type = "uuid"
[resources.B]
type = "template"
template = "x-{{ A }}"
""")
    r = sd.Recipe.load(p)
    assert "A" in r.resources and "B" in r.resources


def test_topo_sort_orders_refs(tmp_path):
    p = tmp_path / "splashdown.toml"
    p.write_text("""
[resources.B]
type = "template"
template = "x-{{ A }}"

[resources.A]
type = "uuid"
""")
    r = sd.Recipe.load(p)
    order = sd.topo_sort(r)
    assert order.index("A") < order.index("B")


def test_topo_sort_detects_cycle(tmp_path):
    p = tmp_path / "splashdown.toml"
    p.write_text("""
[resources.A]
type = "template"
template = "{{ B }}"
[resources.B]
type = "template"
template = "{{ A }}"
""")
    r = sd.Recipe.load(p)
    with pytest.raises(ValueError):
        sd.topo_sort(r)


def test_recipe_rejects_bad_name(tmp_path):
    p = tmp_path / "splashdown.toml"
    p.write_text("""
[resources."BAD-NAME"]
type = "uuid"
""")
    with pytest.raises(ValueError):
        sd.Recipe.load(p)


# ---------- end-to-end provision ----------


def test_provision_writes_splashdown_env(registry, checkout):
    _write_recipe(
        checkout,
        """
[resources.PORT]
type  = "port"
range = [18400, 18410]

[resources.RUN_ID]
type = "uuid"

[resources.URL]
type     = "template"
template = "http://localhost:{{ PORT }}"
""",
    )
    resolved = sd.provision(checkout, registry=registry)
    assert 18400 <= int(resolved["PORT"]) <= 18410
    assert resolved["URL"] == f"http://localhost:{resolved['PORT']}"
    assert len(resolved["RUN_ID"]) == 36  # uuid string length

    recipe = sd.Recipe.load(checkout / "splashdown.toml")
    sd.write_outputs(checkout, recipe, resolved)
    text = (checkout / "splashdown.env").read_text()
    assert f"PORT={resolved['PORT']}" in text
    assert "URL=" in text


def test_provision_idempotent(registry, checkout):
    _write_recipe(
        checkout,
        """
[resources.RUN_ID]
type = "uuid"
[resources.PORT]
type  = "port"
range = [18500, 18510]
""",
    )
    r1 = sd.provision(checkout, registry=registry)
    r2 = sd.provision(checkout, registry=registry)
    assert r1 == r2


def test_provision_reprovision_regenerates_uuid(registry, checkout):
    _write_recipe(
        checkout,
        """
[resources.RUN_ID]
type = "uuid"
""",
    )
    r1 = sd.provision(checkout, registry=registry)
    r2 = sd.provision(checkout, registry=registry, reprovision=True)
    assert r1["RUN_ID"] != r2["RUN_ID"]


def test_splashdown_env_writer_basic(tmp_path):
    target = tmp_path / "splashdown.env"
    sd.write_splashdown_env(target, {"PORT": "8082", "RUN_ID": "abc-def"})
    text = target.read_text()
    assert "PORT=8082" in text
    assert "RUN_ID=abc-def" in text


def test_splashdown_env_writer_quotes_specials(tmp_path):
    target = tmp_path / "splashdown.env"
    sd.write_splashdown_env(target, {"URL": "http://localhost:8082", "MSG": "has spaces"})
    text = target.read_text()
    assert 'MSG="has spaces"' in text
    # A plain URL has no spaces; ':' and '/' are allowed unquoted.
    assert "URL=http://localhost:8082" in text


def test_splashdown_env_writer_overwrites_wholesale(tmp_path):
    target = tmp_path / "splashdown.env"
    target.write_text("STALE=1\nOLD=2\n")
    sd.write_splashdown_env(target, {"PORT": "8082"})
    text = target.read_text()
    assert "STALE" not in text
    assert text.strip() == "PORT=8082"


def test_envfile_writer(registry, checkout):
    _write_recipe(
        checkout,
        """
[resources.MY_VAR]
type     = "template"
template = "hello"
writer   = "envfile=.env.local"
""",
    )
    resolved = sd.provision(checkout, registry=registry)
    recipe = sd.Recipe.load(checkout / "splashdown.toml")
    sd.write_outputs(checkout, recipe, resolved)
    text = (checkout / ".env.local").read_text()
    assert "MY_VAR=hello" in text


def test_writer_reports_changed_then_unchanged(tmp_path):
    target = tmp_path / "splashdown.env"
    assert sd.write_splashdown_env(target, {"PORT": "8082"}) is True  # created
    mtime = target.stat().st_mtime_ns
    assert sd.write_splashdown_env(target, {"PORT": "8082"}) is False  # identical
    assert target.stat().st_mtime_ns == mtime  # untouched
    assert sd.write_splashdown_env(target, {"PORT": "9000"}) is True  # value changed


def test_envfile_writer_reports_changed(tmp_path):
    target = tmp_path / ".env.local"
    assert sd.write_envfile(target, {"MY_VAR": "hello"}) is True
    assert sd.write_envfile(target, {"MY_VAR": "hello"}) is False


def test_envfile_writer_quotes_unsafe_values(tmp_path):
    # A value with a space must be quoted so the dotenv line stays parseable;
    # a safe value stays bare (consistent with write_splashdown_env).
    target = tmp_path / ".env.local"
    sd.write_envfile(target, {"MSG": "hello world", "PORT": "8082"})
    text = target.read_text()
    assert 'MSG="hello world"' in text
    assert "PORT=8082" in text


def test_provision_noop_prints_up_to_date(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "splashdown.toml").write_text("""
[resources.PORT]
type  = "port"
range = [19100, 19110]
[resources.RUN_ID]
type = "uuid"
""")
    assert sd.main(["--cwd", str(tmp_path)]) == 0
    capsys.readouterr()  # discard first-run (changed) output
    assert sd.main(["--cwd", str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert err.strip() == "splashdown: up to date (2 vars, 1 files)"


def test_provision_changed_prints_only_changes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "splashdown.toml").write_text("""
[resources.RUN_ID]
type = "uuid"
""")
    assert sd.main(["--cwd", str(tmp_path)]) == 0
    capsys.readouterr()
    # --force regenerates RUN_ID, so exactly that var + its writer report.
    assert sd.main(["--cwd", str(tmp_path), "sync", "--force"]) == 0
    err = capsys.readouterr().err
    assert "RUN_ID=" in err
    assert f"-> {sd.ENV_FILE_NAME}: 1 vars (changed)" in err
    assert "up to date" not in err


def test_cwd_resource_type(registry, tmp_path):
    cwd = tmp_path / "mybranch"
    cwd.mkdir()
    _write_recipe(
        cwd,
        """
[resources.NAME]
type = "cwd"
""",
    )
    resolved = sd.provision(cwd, registry=registry)
    assert resolved["NAME"] == "mybranch"


def test_set_type_uses_default(registry, checkout):
    _write_recipe(
        checkout,
        """
[resources.MODE]
type    = "set"
default = "dev"
""",
    )
    resolved = sd.provision(checkout, registry=registry)
    assert resolved["MODE"] == "dev"


def test_set_type_persists_user_value(registry, checkout):
    _write_recipe(
        checkout,
        """
[resources.MODE]
type = "set"
""",
    )
    registry.set_kv(str(checkout.resolve()), "MODE", "prod")
    resolved = sd.provision(checkout, registry=registry)
    assert resolved["MODE"] == "prod"


def test_toml_quoting_escapes_specials():
    assert sd._toml_quote("a\\b") == r'"a\\b"'
    assert sd._toml_quote('he said "hi"') == r'"he said \"hi\""'
    assert sd._toml_quote("line\nbreak") == r'"line\nbreak"'


# ---------- writers helper ----------


def test_find_table_locates_env(tmp_path):
    lines = ["[tools]", 'node = "20"', "", "[env]", 'X = "1"', 'Y = "2"', "", "[other]", "k = 1"]
    s, e = sd._find_table(lines, "env")
    assert s == 3
    assert e == 7


def test_find_table_missing(tmp_path):
    lines = ["[tools]", 'node = "20"']
    s, _e = sd._find_table(lines, "env")
    assert s is None


# ---------- recipe: devices + project ----------


def test_recipe_parses_project(tmp_path):
    p = tmp_path / "splashdown.toml"
    p.write_text('[project]\nframework = "flutter"\n')
    r = sd.Recipe.load(p)
    assert r.project["framework"] == "flutter"


def test_resolve_device_name_template(tmp_path):
    cwd = tmp_path / "feat-y"
    cwd.mkdir()
    spec = {"name": "{{ basename(parent) }}-{{ cwd }}"}
    out = sd._resolve_device_name(spec, cwd, "default")
    assert out == f"{tmp_path.name}-feat-y"


def test_resolve_device_name_default_uses_variant_suffix(tmp_path):
    cwd = tmp_path / "feat-z"
    cwd.mkdir()
    out = sd._resolve_device_name({}, cwd, "small-screen")
    assert out == f"{tmp_path.name}/feat-z/small-screen"


def test_resolve_device_name_sanitized_for_android(tmp_path):
    """avdmanager rejects '/' in names; the default path-derived name has two slashes."""
    cwd = tmp_path / "feat-z"
    cwd.mkdir()
    out = sd._resolve_device_name({}, cwd, "default", dtype="emulator")
    assert "/" not in out
    assert out == f"{tmp_path.name}_feat-z_default"


def test_resolve_device_name_ios_keeps_slashes(tmp_path):
    """iOS sims accept '/' so we preserve the human-readable separators."""
    cwd = tmp_path / "feat-z"
    cwd.mkdir()
    out = sd._resolve_device_name({}, cwd, "default", dtype="simulator")
    assert out == f"{tmp_path.name}/feat-z/default"


# ---------- framework detection ----------


def test_detect_framework_flutter(tmp_path):
    (tmp_path / "pubspec.yaml").write_text("name: x\n")
    r = sd.Recipe({"project": {}}, tmp_path / "splashdown.toml")
    assert sd.detect_framework(tmp_path, r) == "flutter"


def test_detect_framework_rn(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies": {"react-native": "0.74"}}')
    r = sd.Recipe({"project": {}}, tmp_path / "splashdown.toml")
    assert sd.detect_framework(tmp_path, r) == "react-native"


def test_detect_framework_expo(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies": {"expo": "50"}}')
    (tmp_path / "app.json").write_text("{}")
    r = sd.Recipe({"project": {}}, tmp_path / "splashdown.toml")
    assert sd.detect_framework(tmp_path, r) == "expo"


def test_detect_framework_override_wins(tmp_path):
    (tmp_path / "pubspec.yaml").write_text("name: x\n")
    r = sd.Recipe({"project": {"framework": "react-native"}}, tmp_path / "splashdown.toml")
    assert sd.detect_framework(tmp_path, r) == "react-native"


def test_detect_framework_errors_when_unknown(tmp_path):
    r = sd.Recipe({"project": {}}, tmp_path / "splashdown.toml")
    with pytest.raises(sd.DeviceError):
        sd.detect_framework(tmp_path, r)


def test_detect_framework_flutter_wins_over_react_native(tmp_path):
    """Conflicting signals: pubspec.yaml AND react-native dep both present.
    Flutter is registered first in PROFILES, so it wins."""
    (tmp_path / "pubspec.yaml").write_text("name: x\n")
    (tmp_path / "package.json").write_text('{"dependencies": {"react-native": "0.83"}}')
    r = sd.Recipe({"project": {}}, tmp_path / "splashdown.toml")
    assert sd.detect_framework(tmp_path, r) == "flutter"


def test_cmd_doctor_runs_vite_wiring_check_in_legacy_recipe(tmp_path):
    """`[project] framework = "vite"` + a vite.config that uses loadEnv → doctor
    must surface the vite-config-process-env check (not "no wiring defined")."""
    (tmp_path / "splashdown.toml").write_text('[project]\nframework = "vite"\n')
    (tmp_path / "vite.config.ts").write_text("""\
import { defineConfig, loadEnv } from "vite";
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, import.meta.dirname, "");
  return { server: { port: Number(env.WEB_DEV_PORT ?? 5173) } };
});
""")
    rc = sd.cmd_doctor(tmp_path)
    assert rc != 0  # the check flags `problem` and we didn't pass --fix
    # No assertion on exact stderr — the meaningful contract is "the check ran",
    # which we infer from rc != 0 instead of rc == 0 with "no wiring defined".


def test_extract_resource_blocks_handles_underscore_resource_names(tmp_path):
    """Underscores in resource names must round-trip through refresh-inventory.
    (Quoted/hyphenated TOML names are not currently supported; this test pins
    what IS supported, so we don't accidentally regress.)"""
    blocks = sd._extract_resource_blocks("""\
[resources.MY_PORT_WITH_UNDERSCORES]
type = "port"
range = [9000, 9010]
""")
    assert "MY_PORT_WITH_UNDERSCORES" in blocks


def test_enumerate_apps_handles_pnpm_workspace_with_comments(tmp_path):
    """pnpm-workspace.yaml with comments + missing packages key shouldn't crash."""
    (tmp_path / "pnpm-workspace.yaml").write_text("""\
# top comment
packages:
  # only one block
  - apps/*
# trailing
""")
    (tmp_path / "apps").mkdir()
    (tmp_path / "apps" / "api").mkdir()
    apps = sd._enumerate_apps(tmp_path, "pnpm")
    assert [n for n, _ in apps] == ["api"]


def test_enumerate_apps_handles_pnpm_workspace_with_no_packages_key(tmp_path):
    """A pnpm-workspace.yaml that's missing `packages:` returns no apps,
    doesn't raise."""
    (tmp_path / "pnpm-workspace.yaml").write_text("catalogMode: manual\n")
    apps = sd._enumerate_apps(tmp_path, "pnpm")
    assert apps == []


def test_detect_framework_ios_native_xcodeproj(tmp_path):
    (tmp_path / "MyApp.xcodeproj").mkdir()
    r = sd.Recipe({"project": {}}, tmp_path / "splashdown.toml")
    assert sd.detect_framework(tmp_path, r) == "ios-native"


def test_detect_framework_ios_native_xcworkspace(tmp_path):
    (tmp_path / "MyApp.xcworkspace").mkdir()
    r = sd.Recipe({"project": {}}, tmp_path / "splashdown.toml")
    assert sd.detect_framework(tmp_path, r) == "ios-native"


def test_detect_framework_ios_native_skipped_when_rn_present(tmp_path):
    # RN's iOS subproject lives under cwd/ios/*.xcodeproj so won't match the
    # root glob, but defend against unusual layouts where a stray .xcodeproj
    # at root could mis-trigger ios-native.
    (tmp_path / "MyApp.xcodeproj").mkdir()
    (tmp_path / "package.json").write_text('{"dependencies": {"react-native": "0.74"}}')
    r = sd.Recipe({"project": {}}, tmp_path / "splashdown.toml")
    assert sd.detect_framework(tmp_path, r) == "react-native"


def test_detect_framework_android_native(tmp_path):
    (tmp_path / "build.gradle.kts").write_text("")
    (tmp_path / "settings.gradle.kts").write_text("")
    r = sd.Recipe({"project": {}}, tmp_path / "splashdown.toml")
    assert sd.detect_framework(tmp_path, r) == "android-native"


def test_detect_framework_android_native_needs_settings(tmp_path):
    # build.gradle alone (no settings.gradle) is too weak a signal — many
    # non-Android Gradle projects ship just a build.gradle.
    (tmp_path / "build.gradle").write_text("")
    r = sd.Recipe({"project": {}}, tmp_path / "splashdown.toml")
    with pytest.raises(sd.DeviceError):
        sd.detect_framework(tmp_path, r)


def test_detect_framework_android_native_skipped_when_flutter_present(tmp_path):
    # Flutter projects have an android/ subdir with gradle files but should
    # still detect as flutter via pubspec.yaml at root.
    (tmp_path / "pubspec.yaml").write_text("name: x\n")
    (tmp_path / "build.gradle").write_text("")
    (tmp_path / "settings.gradle").write_text("")
    r = sd.Recipe({"project": {}}, tmp_path / "splashdown.toml")
    assert sd.detect_framework(tmp_path, r) == "flutter"


def test_detect_framework_native_override_wins(tmp_path):
    # No filesystem signals at all — explicit override carries it.
    r = sd.Recipe({"project": {"framework": "ios-native"}}, tmp_path / "splashdown.toml")
    assert sd.detect_framework(tmp_path, r) == "ios-native"


# ---------- variant resolution ----------


def test_default_sim_name_includes_variant(tmp_path):
    cwd = tmp_path / "myapp.feat-x"
    cwd.mkdir()
    assert sd._default_sim_name(cwd, "default") == f"{tmp_path.name}/myapp.feat-x/default"
    assert sd._default_sim_name(cwd, "small-screen") == f"{tmp_path.name}/myapp.feat-x/small-screen"


def test_resolve_variant_explicit_wins():
    catalog = {"default": {"model": "A"}, "small-screen": {"model": "B"}}
    name, spec = sd.resolve_variant(catalog, "small-screen")
    assert name == "small-screen"
    assert spec["model"] == "B"


def test_resolve_variant_picks_default_when_unspecified():
    catalog = {"default": {"model": "A"}, "small-screen": {"model": "B"}}
    name, _ = sd.resolve_variant(catalog, None)
    assert name == "default"


def test_resolve_variant_picks_single_when_no_default():
    catalog = {"lonely": {"model": "X"}}
    name, _ = sd.resolve_variant(catalog, None)
    assert name == "lonely"


def test_resolve_variant_errors_when_multiple_no_default():
    with pytest.raises(sd.DeviceError, match="default"):
        sd.resolve_variant({"a": {}, "b": {}}, None)


def test_resolve_variant_errors_when_unknown_variant():
    with pytest.raises(sd.DeviceError, match="no variant `ghost`"):
        sd.resolve_variant({"default": {}}, "ghost")


def test_resolve_variant_errors_when_empty_catalog():
    with pytest.raises(sd.DeviceError, match="no variants"):
        sd.resolve_variant({}, None)


# ---------- merged_targets ----------


def test_merged_devices_unions_recipe_and_local(tmp_path):
    r = sd.Recipe(
        {"targets": {"simulator": {"default": {"model": "iPhone 17"}}}},
        tmp_path / "splashdown.toml",
    )
    lc = sd.LocalConfig(
        {"targets": {"simulator": {"repro-bug": {"model": "iPhone 16"}}}},
        tmp_path / "splashdown.local.toml",
    )
    merged = sd.merged_targets(r, lc)
    assert set(merged["simulator"]) == {"default", "repro-bug"}


def test_merged_devices_collision_errors(tmp_path):
    r = sd.Recipe(
        {"targets": {"simulator": {"default": {"model": "A"}}}},
        tmp_path / "splashdown.toml",
    )
    lc = sd.LocalConfig(
        {"targets": {"simulator": {"default": {"model": "B"}}}},
        tmp_path / "splashdown.local.toml",
    )
    with pytest.raises(ValueError, match="already exists in recipe"):
        sd.merged_targets(r, lc)


def test_recipe_accepts_nested_device_variants(tmp_path):
    p = tmp_path / "splashdown.toml"
    p.write_text("""
[targets.simulator.default]
model = "iPhone 17"

[targets.simulator.lowest-supported]
model = "iPhone 12"
""")
    r = sd.Recipe.load(p)
    assert set(r.targets["simulator"]) == {"default", "lowest-supported"}
    assert r.targets["simulator"]["default"]["model"] == "iPhone 17"


def test_recipe_rejects_legacy_devices_table(tmp_path):
    p = tmp_path / "splashdown.toml"
    p.write_text('[devices.simulator.default]\nmodel = "iPhone 17"\n')
    with pytest.raises(ValueError, match=r"renamed to `\[targets"):
        sd.Recipe.load(p)


def test_cli_surfaces_recipe_errors_cleanly(tmp_path, monkeypatch, capsys):
    # Recipe validation (ValueError) should print `error: …` and exit 1, not
    # dump a traceback — notably the [devices.*]→[targets.*] migration error.
    (tmp_path / "splashdown.toml").write_text('[devices.simulator.default]\nmodel = "X"\n')
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    rc = sd.main(["--cwd", str(tmp_path), "target"])
    assert rc == 1
    assert "renamed to `[targets" in capsys.readouterr().err


def test_recipe_rejects_unknown_device_type(tmp_path):
    p = tmp_path / "splashdown.toml"
    p.write_text('[targets.cardboard-vr.default]\nmodel = "Pixel"\n')
    with pytest.raises(ValueError, match="unknown target type"):
        sd.Recipe.load(p)


def test_localconfig_accepts_nested_device_variants(tmp_path):
    p = tmp_path / "splashdown.local.toml"
    p.write_text("""
[targets.simulator.repro-bug]
model = "iPhone 16"
ios   = "17.5"
""")
    lc = sd.LocalConfig.load(p)
    assert lc.targets["simulator"]["repro-bug"]["ios"] == "17.5"


# ---------- CLI ----------


def test_file_name_constants():
    assert sd.RECIPE_NAME == "splashdown.toml"
    assert sd.LOCAL_NAME == "splashdown.local.toml"
    assert sd.ENV_FILE_NAME == "splashdown.env"


def test_cli_prog_name_is_splash():
    assert sd._build_parser().prog == "splash"


def test_cli_help_shows_tiers(capsys):
    with pytest.raises(SystemExit):
        sd.main(["--help"])
    out = capsys.readouterr().out
    for token in ("run", "sync", "status", "init", "target", "env"):
        assert token in out
    assert "provision" not in out  # old word is gone


def test_localconfig_missing_file_is_empty(tmp_path):
    lc = sd.LocalConfig.load(tmp_path / "splashdown.local.toml")
    assert lc.targets == {}


def test_localconfig_rejects_bad_variant_name(tmp_path):
    p = tmp_path / "splashdown.local.toml"
    p.write_text('[targets.simulator."has spaces"]\nmodel = "iPhone"\n')
    with pytest.raises(ValueError, match="variant name"):
        sd.LocalConfig.load(p)


def test_init_writes_recipe_and_local_skeleton(tmp_path):
    sd.cmd_init(tmp_path, preset="rn")
    recipe = (tmp_path / "splashdown.toml").read_text()
    assert "[resources." in recipe
    assert (tmp_path / "splashdown.local.toml").exists()


def test_init_does_not_clobber_existing_local(tmp_path):
    (tmp_path / "splashdown.local.toml").write_text('[targets.mine]\ntype = "simulator"\n')
    sd.cmd_init(tmp_path, preset="rn")
    assert "targets.mine" in (tmp_path / "splashdown.local.toml").read_text()


def test_cli_init_preset_is_positional(tmp_path, monkeypatch):
    """`splash init rn` (no --preset flag) writes the rn scaffold."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    rc = sd.main(["--cwd", str(tmp_path), "init", "rn"])
    assert rc == 0
    recipe = (tmp_path / "splashdown.toml").read_text()
    # rn preset declares the React Native profile + Metro port resource.
    assert 'profile = "react-native"' in recipe
    assert "RCT_METRO_PORT" in recipe


def test_cli_init_no_arg_runs_scanner(tmp_path, monkeypatch):
    """`splash init` with no positional kicks off the Scanner-driven flow."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "vite.config.ts").write_text("export default {}")
    rc = sd.main(["--cwd", str(tmp_path), "init"])
    assert rc == 0
    recipe = (tmp_path / "splashdown.toml").read_text()
    # Scanner emits the new shape with [apps.*] + a detected profile.
    assert "[apps." in recipe
    assert 'profile = "vite"' in recipe


def test_cli_init_no_arg_emits_rn_metro_port(tmp_path, monkeypatch):
    """Scanner-driven `splash init` on a react-native project emits the
    RCT_METRO_PORT port resource, just like the `rn` preset does."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "package.json").write_text('{"dependencies":{"react-native":"0.83"}}')
    rc = sd.main(["--cwd", str(tmp_path), "init"])
    assert rc == 0
    recipe = (tmp_path / "splashdown.toml").read_text()
    assert 'profile = "react-native"' in recipe
    assert "[resources.RCT_METRO_PORT]" in recipe
    assert 'resources = ["RCT_METRO_PORT"]' in recipe


def test_cli_init_rescan_updates_inventory(tmp_path, monkeypatch):
    # `init --rescan` re-detects apps in an existing recipe instead of scaffolding.
    (tmp_path / "splashdown.toml").write_text('[project]\nworkspace = "single"\nloader = "mise"\n')
    (tmp_path / "pubspec.yaml").write_text("name: demo\n")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    called = {}

    def _fake(cwd):
        called["cwd"] = cwd
        return 0

    monkeypatch.setattr(sd.cli, "cmd_refresh_inventory", _fake)
    rc = sd.main(["--cwd", str(tmp_path), "init", "--rescan"])
    assert rc == 0
    assert called["cwd"] == tmp_path


def test_init_server_preset_writes_generic_scaffold(tmp_path):
    sd.cmd_init(tmp_path, preset="server")
    recipe = (tmp_path / "splashdown.toml").read_text()
    assert "[resources.PORT]" in recipe
    assert "[resources.DATABASE_URL]" in recipe
    # Generic — should not name a specific framework.
    assert "Next.js preset" not in recipe


def test_init_nextjs_alias_still_works(tmp_path):
    # `nextjs` is kept as a backward-compat alias for `server`.
    sd.cmd_init(tmp_path, preset="nextjs")
    recipe = (tmp_path / "splashdown.toml").read_text()
    assert "[resources.PORT]" in recipe
    assert "[resources.DATABASE_URL]" in recipe


def test_init_electron_preset_includes_user_data_dir(tmp_path):
    sd.cmd_init(tmp_path, preset="electron")
    recipe = (tmp_path / "splashdown.toml").read_text()
    assert "[resources.PORT]" in recipe
    assert "[resources.ELECTRON_USER_DATA_DIR]" in recipe
    # Per-checkout — must reference cwd_abs so each worktree gets its own dir.
    assert "cwd_abs" in recipe


def test_cli_provision_is_default(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cwd = tmp_path / "co"
    cwd.mkdir()
    (cwd / "splashdown.toml").write_text("""
[resources.PORT]
type = "port"
range = [18900, 18910]
""")
    code = sd.main(["--cwd", str(cwd)])
    assert code == 0
    assert (cwd / "splashdown.env").exists()


def test_cli_provision_drops_local_skeleton(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cwd = tmp_path / "co"
    cwd.mkdir()
    (cwd / "splashdown.toml").write_text("""
[resources.PORT]
type  = "port"
range = [18900, 18910]
""")
    sd.main(["--cwd", str(cwd)])
    assert (cwd / "splashdown.local.toml").exists()
    assert "targets.simulator" in (cwd / "splashdown.local.toml").read_text()


def test_cli_provision_preserves_existing_local(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cwd = tmp_path / "co"
    cwd.mkdir()
    (cwd / "splashdown.toml").write_text("""
[resources.PORT]
type  = "port"
range = [18920, 18930]
""")
    (cwd / "splashdown.local.toml").write_text('[targets.mine]\ntype = "simulator"\n')
    sd.main(["--cwd", str(cwd)])
    assert "targets.mine" in (cwd / "splashdown.local.toml").read_text()


POST_CHECKOUT_SENTINEL = "splash"


def test_init_appends_gitignore(tmp_path):
    sd.cmd_init(tmp_path, preset="minimal")
    gi = (tmp_path / ".gitignore").read_text()
    assert "splashdown.env" in gi
    assert "splashdown.local.toml" in gi


def test_init_gitignore_no_duplicates(tmp_path):
    (tmp_path / ".gitignore").write_text("splashdown.env\n")
    sd.cmd_init(tmp_path, preset="minimal")
    gi = (tmp_path / ".gitignore").read_text()
    assert gi.count("splashdown.env") == 1


def test_init_adds_mise_file_directive_new_file(tmp_path):
    sd.cmd_init(tmp_path, preset="minimal", loader_override="mise")
    mise = (tmp_path / "mise.toml").read_text()
    assert '_.file = "splashdown.env"' in mise
    assert "[env]" in mise


def test_init_adds_mise_file_directive_existing_env_table(tmp_path):
    (tmp_path / "mise.toml").write_text('[env]\nFOO = "bar"\n\n[tools]\nnode = "20"\n')
    sd.cmd_init(tmp_path, preset="minimal")
    mise = (tmp_path / "mise.toml").read_text()
    assert '_.file = "splashdown.env"' in mise
    assert 'FOO = "bar"' in mise
    assert 'node = "20"' in mise
    assert mise.count('_.file = "splashdown.env"') == 1


def test_init_mise_directive_idempotent(tmp_path):
    sd.cmd_init(tmp_path, preset="minimal", loader_override="mise")
    sd.cmd_init(tmp_path, preset="minimal", force=True, loader_override="mise")
    mise = (tmp_path / "mise.toml").read_text()
    assert mise.count('_.file = "splashdown.env"') == 1


def test_init_writes_post_checkout_hook(tmp_path):
    sd.cmd_init(tmp_path, preset="minimal")
    hook = tmp_path / ".githooks" / "post-checkout"
    assert hook.exists()
    assert os.access(hook, os.X_OK)
    assert POST_CHECKOUT_SENTINEL in hook.read_text()


# ---------- hook-manager detection and wiring ----------


def test_detect_hook_manager_clean(tmp_path):
    assert sd._detect_hook_manager(tmp_path) == "none"


def test_detect_hook_manager_lefthook_yml(tmp_path):
    (tmp_path / "lefthook.yml").write_text("pre-commit:\n  commands: {}\n")
    assert sd._detect_hook_manager(tmp_path) == "lefthook"


def test_detect_hook_manager_lefthook_yaml(tmp_path):
    (tmp_path / "lefthook.yaml").write_text("")
    assert sd._detect_hook_manager(tmp_path) == "lefthook"


def test_detect_hook_manager_lefthook_dotted(tmp_path):
    (tmp_path / ".lefthook.yml").write_text("")
    assert sd._detect_hook_manager(tmp_path) == "lefthook"


def test_detect_hook_manager_lefthook_via_pkg(tmp_path):
    (tmp_path / "package.json").write_text('{"devDependencies": {"lefthook": "^1.0"}}')
    assert sd._detect_hook_manager(tmp_path) == "lefthook"


def test_detect_hook_manager_husky(tmp_path):
    (tmp_path / ".husky").mkdir()
    assert sd._detect_hook_manager(tmp_path) == "husky"


def test_wire_lefthook_appends_block_when_absent(tmp_path):
    (tmp_path / "lefthook.yml").write_text(
        "pre-commit:\n  commands:\n    lint:\n      run: echo lint\n"
    )
    sd._wire_post_checkout_lefthook(tmp_path)
    text = (tmp_path / "lefthook.yml").read_text()
    assert "pre-commit:" in text
    assert "post-checkout:" in text
    assert "splashdown:" in text
    assert "run: splash" in text


def test_wire_lefthook_inserts_under_existing_post_checkout(tmp_path):
    (tmp_path / "lefthook.yml").write_text(
        "post-checkout:\n  commands:\n    notify:\n      run: echo hi\n"
    )
    sd._wire_post_checkout_lefthook(tmp_path)
    text = (tmp_path / "lefthook.yml").read_text()
    assert "notify:" in text  # existing command preserved
    assert "splashdown:" in text  # ours added
    assert text.count("post-checkout:") == 1  # not duplicated


def test_wire_lefthook_idempotent(tmp_path):
    (tmp_path / "lefthook.yml").write_text("")
    sd._wire_post_checkout_lefthook(tmp_path)
    once = (tmp_path / "lefthook.yml").read_text()
    sd._wire_post_checkout_lefthook(tmp_path)
    twice = (tmp_path / "lefthook.yml").read_text()
    assert once == twice
    assert twice.count("splashdown:") == 1


def test_wire_lefthook_creates_config_if_only_pkg_dep(tmp_path):
    # Detected via package.json but no lefthook.yml yet.
    (tmp_path / "package.json").write_text('{"devDependencies": {"lefthook": "^1.0"}}')
    sd._wire_post_checkout_lefthook(tmp_path)
    assert (tmp_path / "lefthook.yml").exists()
    assert "splashdown:" in (tmp_path / "lefthook.yml").read_text()


def test_wire_husky_creates_executable_hook(tmp_path):
    sd._wire_post_checkout_husky(tmp_path)
    hook = tmp_path / ".husky" / "post-checkout"
    assert hook.exists()
    assert os.access(hook, os.X_OK)
    assert "splash" in hook.read_text()


def test_ensure_hook_chooses_lefthook(tmp_path):
    (tmp_path / "lefthook.yml").write_text("")
    sd._ensure_post_checkout_hook(tmp_path)
    # lefthook wiring happened; no .githooks dir created.
    assert "splashdown:" in (tmp_path / "lefthook.yml").read_text()
    assert not (tmp_path / ".githooks").exists()


def test_ensure_hook_chooses_husky(tmp_path):
    (tmp_path / ".husky").mkdir()
    sd._ensure_post_checkout_hook(tmp_path)
    assert (tmp_path / ".husky" / "post-checkout").exists()
    assert not (tmp_path / ".githooks").exists()


def test_ensure_hook_clean_falls_back_to_corehookspath(tmp_path):
    sd._ensure_post_checkout_hook(tmp_path)
    hook = tmp_path / ".githooks" / "post-checkout"
    assert hook.exists()
    assert os.access(hook, os.X_OK)


# ---------- doctor (no framework wiring entries yet) ----------


def test_doctor_help_in_cli(capsys):
    with pytest.raises(SystemExit):
        sd.main(["doctor", "--help"])
    out = capsys.readouterr().out
    assert "--fix" in out
    assert "--framework" in out


def test_doctor_no_framework_returns_1(tmp_path, capsys):
    # No recipe, no package.json, no pubspec — detect_framework fails.
    rc = sd.cmd_doctor(tmp_path)
    assert rc == 1
    err = capsys.readouterr().err
    assert "framework" in err.lower()


def test_doctor_unknown_framework_no_checks_returns_0(tmp_path, capsys):
    # Override to a framework that has no WIRING entries.
    rc = sd.cmd_doctor(tmp_path, framework_override="nonesuch")
    assert rc == 0
    err = capsys.readouterr().err
    assert "no wiring checks" in err.lower()


def test_doctor_detects_framework_from_recipe(tmp_path):
    (tmp_path / "splashdown.toml").write_text('[project]\nframework = "nonesuch"\n')
    # nonesuch has no WIRING entries → returns 0 without erroring.
    assert sd.cmd_doctor(tmp_path) == 0


def test_doctor_uses_filesystem_when_no_recipe(tmp_path):
    # package.json with react-native → detect_framework returns "react-native".
    (tmp_path / "package.json").write_text('{"dependencies":{"react-native":"0.83"}}')
    # WIRING["react-native"] now exists (rn-hook). A clean RN dir is missing the
    # hook → doctor reports a problem.
    assert sd.cmd_doctor(tmp_path) == 1


# ---------- rn-hook check ----------

import subprocess as _subprocess


def _git_init(path):
    _subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def test_rn_hook_clean_detect_problem(tmp_path):
    status, _ = sd._rn_hook_detect(tmp_path)
    assert status == "problem"


def test_rn_hook_clean_autofix_then_ok(tmp_path):
    _git_init(tmp_path)
    sd._ensure_post_checkout_hook(tmp_path)
    status, _ = sd._rn_hook_detect(tmp_path)
    assert status == "ok"


def test_rn_hook_lefthook_detect_problem(tmp_path):
    (tmp_path / "lefthook.yml").write_text("pre-commit:\n  commands: {}\n")
    status, detail = sd._rn_hook_detect(tmp_path)
    assert status == "problem"
    assert "lefthook" in detail


def test_rn_hook_lefthook_autofix_then_ok(tmp_path):
    (tmp_path / "lefthook.yml").write_text(
        "pre-commit:\n  commands:\n    lint:\n      run: echo lint\n"
    )
    sd._ensure_post_checkout_hook(tmp_path)
    status, _ = sd._rn_hook_detect(tmp_path)
    assert status == "ok"
    text = (tmp_path / "lefthook.yml").read_text()
    assert "lint" in text  # existing preserved
    assert "splashdown:" in text  # ours added


def test_rn_hook_husky_detect_problem(tmp_path):
    (tmp_path / ".husky").mkdir()
    status, _ = sd._rn_hook_detect(tmp_path)
    assert status == "problem"


def test_rn_hook_husky_autofix_then_ok(tmp_path):
    (tmp_path / ".husky").mkdir()
    sd._ensure_post_checkout_hook(tmp_path)
    status, _ = sd._rn_hook_detect(tmp_path)
    assert status == "ok"


def test_doctor_fix_wires_hook_in_clean_rn_dir(tmp_path):
    _git_init(tmp_path)
    (tmp_path / "package.json").write_text('{"dependencies":{"react-native":"0.83"}}')
    # Place an empty metro.config.js to satisfy the rn-metro-config "applies" check;
    # detect will still report problem. We're only checking the hook here.
    (tmp_path / "metro.config.js").write_text(
        "module.exports = { server: { port: Number(process.env.RCT_METRO_PORT) || 8081 } };\n"
    )
    assert sd.cmd_doctor(tmp_path) == 1  # not wired
    assert sd.cmd_doctor(tmp_path, fix=True) == 0  # now wired
    assert sd.cmd_doctor(tmp_path) == 0  # idempotent re-check
    assert (tmp_path / ".githooks" / "post-checkout").exists()


# ---------- rn-metro-config check ----------


def test_rn_metro_not_applicable_without_config(tmp_path):
    assert sd._rn_metro_applies(tmp_path) is False


def test_rn_metro_detect_ok_when_env_present(tmp_path):
    (tmp_path / "metro.config.js").write_text(
        "module.exports = { server: { port: Number(process.env.RCT_METRO_PORT) || 8081 } };\n"
    )
    assert sd._rn_metro_detect(tmp_path)[0] == "ok"


def test_rn_metro_detect_problem_for_literal(tmp_path):
    (tmp_path / "metro.config.js").write_text("module.exports = { server: { port: 8083 } };\n")
    status, detail = sd._rn_metro_detect(tmp_path)
    assert status == "problem"
    assert "autofixable" in detail


def test_rn_metro_autofix_replaces_literal(tmp_path):
    (tmp_path / "metro.config.js").write_text(
        "const config = {\n  server: {\n    port: 8083,\n  },\n};\n"
    )
    sd._rn_metro_autofix(tmp_path)
    text = (tmp_path / "metro.config.js").read_text()
    assert "process.env.RCT_METRO_PORT" in text
    assert "|| 8083" in text
    # Re-detect now ok.
    assert sd._rn_metro_detect(tmp_path)[0] == "ok"


def test_rn_metro_autofix_idempotent(tmp_path):
    (tmp_path / "metro.config.js").write_text("const config = { server: { port: 8083 } };\n")
    sd._rn_metro_autofix(tmp_path)
    once = (tmp_path / "metro.config.js").read_text()
    sd._rn_metro_autofix(tmp_path)
    twice = (tmp_path / "metro.config.js").read_text()
    assert once == twice
    assert twice.count("process.env.RCT_METRO_PORT") == 1


def test_rn_metro_autofix_injects_server_block_when_absent(tmp_path):
    # The common RN template shape: a config object with no server block at all.
    (tmp_path / "metro.config.js").write_text(
        "const config = {\n"
        "  transformer: { babelTransformerPath: 'x' },\n"
        "  resolver: { sourceExts: ['svg'] },\n"
        "};\n"
        "module.exports = mergeConfig(defaultConfig, config);\n"
    )
    status, detail = sd._rn_metro_detect(tmp_path)
    assert status == "problem"
    assert "autofixable" in detail
    sd._rn_metro_autofix(tmp_path)
    text = (tmp_path / "metro.config.js").read_text()
    assert "server: {" in text
    assert "process.env.RCT_METRO_PORT" in text
    assert "|| 8081" in text
    assert sd._rn_metro_detect(tmp_path)[0] == "ok"


def test_rn_metro_autofix_adds_port_to_existing_server_block(tmp_path):
    (tmp_path / "metro.config.js").write_text(
        "module.exports = { server: { someOtherThing: 1 } };\n"
    )
    status, detail = sd._rn_metro_detect(tmp_path)
    assert status == "problem"
    assert "autofixable" in detail
    sd._rn_metro_autofix(tmp_path)
    text = (tmp_path / "metro.config.js").read_text()
    assert "process.env.RCT_METRO_PORT" in text
    assert "someOtherThing" in text
    assert sd._rn_metro_detect(tmp_path)[0] == "ok"


def test_rn_metro_autofix_noop_for_unrecognized_shape(tmp_path):
    # No port literal, no server block, no config object literal to inject into.
    text = "module.exports = makeMetroConfig(__dirname);\n"
    (tmp_path / "metro.config.js").write_text(text)
    sd._rn_metro_autofix(tmp_path)
    assert (tmp_path / "metro.config.js").read_text() == text
    # Detect still reports problem; manual instructions will be printed by doctor.
    assert sd._rn_metro_detect(tmp_path)[0] == "problem"


# ---------- rn-pkg-port check ----------


def test_rn_pkg_not_applicable_without_pkg(tmp_path):
    assert sd._rn_pkg_applies(tmp_path) is False


def test_rn_pkg_detect_ok_when_clean(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"start": "react-native start", "ios": "react-native run-ios"}})
    )
    assert sd._rn_pkg_detect(tmp_path)[0] == "ok"


def test_rn_pkg_detect_problem_with_space_form(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"start": "react-native start --port 8083"}})
    )
    status, detail = sd._rn_pkg_detect(tmp_path)
    assert status == "problem"
    assert "start" in detail


def test_rn_pkg_detect_problem_with_equals_form(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"ios": "react-native run-ios --port=8083"}})
    )
    assert sd._rn_pkg_detect(tmp_path)[0] == "problem"


def test_rn_pkg_autofix_strips_port_flag(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "x",
                "scripts": {
                    "android": "react-native run-android --port 8083",
                    "ios": "react-native run-ios --port 8083",
                    "start": "react-native start --port 8083",
                    "test": "jest",
                },
                "dependencies": {"react-native": "0.83"},
            },
            indent=2,
        )
    )
    sd._rn_pkg_autofix(tmp_path)
    data = json.loads((tmp_path / "package.json").read_text())
    assert data["scripts"]["start"] == "react-native start"
    assert data["scripts"]["ios"] == "react-native run-ios"
    assert data["scripts"]["android"] == "react-native run-android"
    assert data["scripts"]["test"] == "jest"  # unrelated script preserved
    assert data["dependencies"]["react-native"] == "0.83"  # rest of file preserved
    assert sd._rn_pkg_detect(tmp_path)[0] == "ok"


def test_rn_pkg_autofix_idempotent(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"start": "react-native start --port 8083"}})
    )
    sd._rn_pkg_autofix(tmp_path)
    once = (tmp_path / "package.json").read_text()
    sd._rn_pkg_autofix(tmp_path)
    twice = (tmp_path / "package.json").read_text()
    assert once == twice


def test_rn_pkg_targets_react_native_scripts_by_command(tmp_path):
    # An unconventional script name that still invokes react-native should be caught.
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"dev": "react-native start --port 8083"}})
    )
    assert sd._rn_pkg_detect(tmp_path)[0] == "problem"
    sd._rn_pkg_autofix(tmp_path)
    assert (
        json.loads((tmp_path / "package.json").read_text())["scripts"]["dev"]
        == "react-native start"
    )


# ---------- rn-xcode-env check ----------


def _make_ios(tmp_path: Path, xcode_env_content: str) -> None:
    (tmp_path / "ios").mkdir()
    (tmp_path / "ios" / ".xcode.env").write_text(xcode_env_content)


def test_rn_xcode_not_applicable_without_file(tmp_path):
    assert sd._rn_xcode_applies(tmp_path) is False


def test_rn_xcode_detect_problem_for_static_export(tmp_path):
    _make_ios(tmp_path, "export NODE_BINARY=node\nexport RCT_METRO_PORT=8083\n")
    status, detail = sd._rn_xcode_detect(tmp_path)
    assert status == "problem"
    assert "statically" in detail.lower()


def test_rn_xcode_detect_problem_when_missing(tmp_path):
    _make_ios(tmp_path, "export NODE_BINARY=node\n")
    assert sd._rn_xcode_detect(tmp_path)[0] == "problem"


def test_rn_xcode_detect_ok_with_block(tmp_path):
    _make_ios(tmp_path, "export NODE_BINARY=node\n" + sd._XCODE_BLOCK)
    assert sd._rn_xcode_detect(tmp_path)[0] == "ok"


def test_rn_xcode_autofix_replaces_static(tmp_path):
    _make_ios(
        tmp_path,
        "# header\nexport NODE_BINARY=node\n\n# Pin Metro port\nexport RCT_METRO_PORT=8083\n",
    )
    sd._rn_xcode_autofix(tmp_path)
    text = (tmp_path / "ios" / ".xcode.env").read_text()
    # Old static export gone.
    assert "export RCT_METRO_PORT=8083" not in text
    # NODE_BINARY preserved.
    assert "export NODE_BINARY=node" in text
    # Splashdown block present.
    assert sd._XCODE_BEGIN in text
    assert sd._XCODE_END in text
    assert "splashdown.env" in text
    # Now wired.
    assert sd._rn_xcode_detect(tmp_path)[0] == "ok"


def test_rn_xcode_autofix_appends_when_missing(tmp_path):
    _make_ios(tmp_path, "export NODE_BINARY=node\n")
    sd._rn_xcode_autofix(tmp_path)
    text = (tmp_path / "ios" / ".xcode.env").read_text()
    assert "export NODE_BINARY=node" in text
    assert sd._XCODE_BEGIN in text


def test_rn_xcode_autofix_idempotent(tmp_path):
    _make_ios(tmp_path, "export NODE_BINARY=node\nexport RCT_METRO_PORT=8083\n")
    sd._rn_xcode_autofix(tmp_path)
    once = (tmp_path / "ios" / ".xcode.env").read_text()
    sd._rn_xcode_autofix(tmp_path)
    twice = (tmp_path / "ios" / ".xcode.env").read_text()
    assert once == twice
    # Sentinels should appear exactly once.
    assert twice.count(sd._XCODE_BEGIN) == 1
    assert twice.count(sd._XCODE_END) == 1


def test_rn_xcode_detect_ok_for_handwritten_splashdown_wiring(tmp_path):
    """A user-written, non-sentinel block that reads splashdown.env counts as ok."""
    _make_ios(
        tmp_path,
        (
            "export NODE_BINARY=node\n"
            'if [ -z "${RCT_METRO_PORT:-}" ] && [ -f "${SRCROOT}/../splashdown.env" ]; then\n'
            '  export RCT_METRO_PORT="$(grep \'^RCT_METRO_PORT=\' "${SRCROOT}/../splashdown.env" | cut -d= -f2)"\n'
            "fi\n"
            'export RCT_METRO_PORT="${RCT_METRO_PORT:-8083}"\n'
        ),
    )
    assert sd._rn_xcode_detect(tmp_path)[0] == "ok"


def test_rn_xcode_autofix_noop_when_already_referencing_splashdown(tmp_path):
    content = (
        "export NODE_BINARY=node\n"
        'if [ -f "${SRCROOT}/../splashdown.env" ]; then\n'
        '  . "${SRCROOT}/../splashdown.env"\n'
        "fi\n"
    )
    _make_ios(tmp_path, content)
    sd._rn_xcode_autofix(tmp_path)
    assert (tmp_path / "ios" / ".xcode.env").read_text() == content


def test_cmd_init_rn_preset_wires_everything(tmp_path):
    """`splash init --preset=rn` in an RN-shaped repo scaffolds AND wires."""
    _git_init(tmp_path)
    # RN-shaped repo before splashdown.
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "start": "react-native start --port 8083",
                    "ios": "react-native run-ios --port 8083",
                },
                "dependencies": {"react-native": "0.83"},
                "devDependencies": {"lefthook": "^1.0"},
            },
            indent=2,
        )
    )
    (tmp_path / "metro.config.js").write_text(
        "const config = { server: { port: 8083 } };\nmodule.exports = config;\n"
    )
    (tmp_path / "ios").mkdir()
    (tmp_path / "ios" / ".xcode.env").write_text(
        "export NODE_BINARY=node\nexport RCT_METRO_PORT=8083\n"
    )
    (tmp_path / "lefthook.yml").write_text(
        "pre-commit:\n  commands:\n    lint:\n      run: echo lint\n"
    )
    # Run init — should scaffold + wire. Force mise so the loader-wiring leg is
    # exercised (the repo has no loader config, so detection now yields "none").
    sd.cmd_init(tmp_path, preset="rn", loader_override="mise")
    # Scaffolding present.
    assert (tmp_path / "splashdown.toml").exists()
    assert (tmp_path / "splashdown.local.toml").exists()
    assert (tmp_path / "mise.toml").exists()
    # All four wirings applied.
    pkg = json.loads((tmp_path / "package.json").read_text())
    assert "--port" not in pkg["scripts"]["start"]
    assert "process.env.RCT_METRO_PORT" in (tmp_path / "metro.config.js").read_text()
    assert sd._XCODE_BEGIN in (tmp_path / "ios" / ".xcode.env").read_text()
    assert "post-checkout:" in (tmp_path / "lefthook.yml").read_text()
    # core.hooksPath NOT set (lefthook owns hooks).
    import subprocess as _sp

    r = _sp.run(
        ["git", "-C", str(tmp_path), "config", "--get", "core.hooksPath"], capture_output=True
    )
    assert r.returncode != 0 or not r.stdout.strip()
    # Doctor confirms green.
    assert sd.cmd_doctor(tmp_path) == 0


def test_cmd_init_minimal_preset_skips_doctor(tmp_path, capsys):
    """No `[project] framework` → no framework wiring run."""
    sd.cmd_init(tmp_path, preset="minimal")
    err = capsys.readouterr().err
    assert "running framework wiring" not in err


def test_doctor_fix_full_rn_project(tmp_path):
    _git_init(tmp_path)
    # An RN-shaped tmp dir mirroring FlowLab's pre-wiring state.
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "start": "react-native start --port 8083",
                    "ios": "react-native run-ios --port 8083",
                    "android": "react-native run-android --port 8083",
                },
                "dependencies": {"react-native": "0.83"},
                "devDependencies": {"lefthook": "^1.0"},
            },
            indent=2,
        )
    )
    (tmp_path / "metro.config.js").write_text(
        "const config = {\n  server: {\n    port: 8083,\n  },\n};\nmodule.exports = config;\n"
    )
    (tmp_path / "ios").mkdir()
    (tmp_path / "ios" / ".xcode.env").write_text(
        "export NODE_BINARY=node\nexport RCT_METRO_PORT=8083\n"
    )
    (tmp_path / "lefthook.yml").write_text(
        "pre-commit:\n  commands:\n    lint:\n      run: echo lint\n"
    )
    # Initial state: all four checks problem.
    assert sd.cmd_doctor(tmp_path) == 1
    # Fix.
    assert sd.cmd_doctor(tmp_path, fix=True) == 0
    # All green now.
    assert sd.cmd_doctor(tmp_path) == 0
    # Verify the concrete file states.
    pkg = json.loads((tmp_path / "package.json").read_text())
    for name in ("start", "ios", "android"):
        assert "--port" not in pkg["scripts"][name]
    assert "process.env.RCT_METRO_PORT" in (tmp_path / "metro.config.js").read_text()
    assert sd._XCODE_BEGIN in (tmp_path / "ios" / ".xcode.env").read_text()
    lh = (tmp_path / "lefthook.yml").read_text()
    assert "post-checkout:" in lh
    assert "splashdown:" in lh
    assert "lint:" in lh  # original entry preserved


def test_app_inventory_is_dataclass_with_name_path_profile(tmp_path):
    app = sd.AppInventory(name="api", path=tmp_path / "apps" / "api", profile="node-backend")
    assert app.name == "api"
    assert app.profile == "node-backend"
    assert app.path == tmp_path / "apps" / "api"


def test_project_inventory_collects_apps_and_loader(tmp_path):
    inv = sd.ProjectInventory(
        workspace="pnpm",
        apps=[sd.AppInventory(name="api", path=tmp_path / "apps/api", profile="node-backend")],
        loader="mise",
    )
    assert inv.workspace == "pnpm"
    assert inv.loader == "mise"
    assert len(inv.apps) == 1


def test_detect_workspace_pnpm(tmp_path):
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - apps/*\n")
    assert sd._detect_workspace(tmp_path) == "pnpm"


def test_detect_workspace_yarn(tmp_path):
    (tmp_path / "package.json").write_text('{"workspaces": ["apps/*"]}')
    (tmp_path / "yarn.lock").write_text("")
    assert sd._detect_workspace(tmp_path) == "yarn"


def test_detect_workspace_npm(tmp_path):
    (tmp_path / "package.json").write_text('{"workspaces": ["apps/*"]}')
    (tmp_path / "package-lock.json").write_text("")
    assert sd._detect_workspace(tmp_path) == "npm"


def test_detect_workspace_cargo(tmp_path):
    (tmp_path / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/*"]\n')
    assert sd._detect_workspace(tmp_path) == "cargo"


def test_detect_workspace_gradle(tmp_path):
    (tmp_path / "settings.gradle.kts").write_text('include("api", "web")\n')
    assert sd._detect_workspace(tmp_path) == "gradle"


def test_detect_workspace_single(tmp_path):
    assert sd._detect_workspace(tmp_path) == "single"


def test_scanner_single_app_no_workspace(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "single"}')
    inv = sd.Scanner().scan(tmp_path)
    assert inv.workspace == "single"
    assert len(inv.apps) == 1
    assert inv.apps[0].name == "main"
    assert inv.apps[0].path == tmp_path
    assert inv.apps[0].profile == "unknown"  # no profiles registered yet


def test_scanner_pnpm_monorepo_enumerates_apps(tmp_path):
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - apps/*\n")
    (tmp_path / "apps").mkdir()
    (tmp_path / "apps" / "api").mkdir()
    (tmp_path / "apps" / "api" / "package.json").write_text('{"name": "api"}')
    (tmp_path / "apps" / "web").mkdir()
    (tmp_path / "apps" / "web" / "package.json").write_text('{"name": "web"}')
    inv = sd.Scanner().scan(tmp_path)
    assert inv.workspace == "pnpm"
    names = sorted(a.name for a in inv.apps)
    assert names == ["api", "web"]


def test_scanner_loader_defaults_to_none(tmp_path):
    inv = sd.Scanner().scan(tmp_path)
    assert inv.loader == "none"


def test_scanner_detects_mise_loader(tmp_path):
    (tmp_path / "mise.toml").write_text("")
    inv = sd.Scanner().scan(tmp_path)
    assert inv.loader == "mise"


def test_scanner_detects_direnv_loader(tmp_path):
    (tmp_path / ".envrc").write_text("")
    inv = sd.Scanner().scan(tmp_path)
    assert inv.loader == "direnv"


def test_scanner_detects_devbox_loader(tmp_path):
    (tmp_path / "devbox.json").write_text("{}")
    inv = sd.Scanner().scan(tmp_path)
    assert inv.loader == "devbox"


def test_scanner_loader_precedence_mise_over_direnv(tmp_path):
    (tmp_path / "mise.toml").write_text("")
    (tmp_path / ".envrc").write_text("")
    inv = sd.Scanner().scan(tmp_path)
    assert inv.loader == "mise"


# ---------- no-loader delivery fallback ----------


def _inv_none(tmp_path, *profiles):
    apps = [
        sd.AppInventory(name=f"app{i}", path=tmp_path, profile=p) for i, p in enumerate(profiles)
    ]
    return sd.ProjectInventory(workspace="single", apps=apps, loader="none")


def test_no_loader_delivery_prefers_env_for_dotenv_app(tmp_path):
    (tmp_path / ".env").write_text("")
    writer, msg = sd._resolve_no_loader_delivery(tmp_path, _inv_none(tmp_path, "nextjs"))
    assert writer == "envfile=.env"
    assert ".env" in msg


def test_no_loader_delivery_falls_back_to_env_local(tmp_path):
    (tmp_path / ".env.local").write_text("")
    writer, _msg = sd._resolve_no_loader_delivery(tmp_path, _inv_none(tmp_path, "nextjs"))
    assert writer == "envfile=.env.local"


def test_no_loader_delivery_prefers_env_over_env_local(tmp_path):
    # Both files present → .env wins (documented precedence).
    (tmp_path / ".env").write_text("")
    (tmp_path / ".env.local").write_text("")
    writer, _msg = sd._resolve_no_loader_delivery(tmp_path, _inv_none(tmp_path, "nextjs"))
    assert writer == "envfile=.env"


def test_no_loader_delivery_no_apps_routes_to_file(tmp_path):
    # The `not inv.apps` guard: an empty repo with a .env is still file-capable.
    (tmp_path / ".env").write_text("")
    writer, _msg = sd._resolve_no_loader_delivery(tmp_path, _inv_none(tmp_path))
    assert writer == "envfile=.env"


def test_no_loader_delivery_unknown_profile_routes_to_file(tmp_path):
    # `unknown` apps get the benefit of the doubt (treated as dotenv-capable).
    (tmp_path / ".env").write_text("")
    writer, _msg = sd._resolve_no_loader_delivery(tmp_path, _inv_none(tmp_path, "unknown"))
    assert writer == "envfile=.env"


def test_no_loader_delivery_no_dotenv_file_returns_none(tmp_path):
    writer, msg = sd._resolve_no_loader_delivery(tmp_path, _inv_none(tmp_path, "nextjs"))
    assert writer is None
    assert "splashdown.env" in msg


def test_no_loader_delivery_process_env_only_app_returns_none(tmp_path):
    # A dotenv file exists, but the only app (vite) reads from process.env — a
    # plain .env would reach nothing, so fall to instructions.
    (tmp_path / ".env").write_text("")
    writer, msg = sd._resolve_no_loader_delivery(tmp_path, _inv_none(tmp_path, "vite"))
    assert writer is None
    assert "install mise/direnv/devbox" in msg


def test_no_loader_delivery_mixed_routes_to_file_with_caveat(tmp_path):
    (tmp_path / ".env").write_text("")
    writer, msg = sd._resolve_no_loader_delivery(tmp_path, _inv_none(tmp_path, "nextjs", "vite"))
    assert writer == "envfile=.env"
    assert "app1" in msg  # the vite app is named in the caveat
    assert "read env from the process" in msg


def test_no_loader_delivery_warns_when_target_tracked(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".env").write_text("")
    writer, msg = sd._resolve_no_loader_delivery(tmp_path, _inv_none(tmp_path, "nextjs"))
    assert writer == "envfile=.env"
    assert "not gitignored" in msg


def test_no_loader_delivery_no_warning_when_target_ignored(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(".env\n")
    (tmp_path / ".env").write_text("")
    _writer, msg = sd._resolve_no_loader_delivery(tmp_path, _inv_none(tmp_path, "nextjs"))
    assert "not gitignored" not in msg


def test_no_loader_delivery_no_warning_outside_git_repo(tmp_path):
    # No repo → `git check-ignore` exits 128; we must not nag spuriously.
    (tmp_path / ".env").write_text("")
    _writer, msg = sd._resolve_no_loader_delivery(tmp_path, _inv_none(tmp_path, "nextjs"))
    assert "not gitignored" not in msg


def test_none_loader_wire_is_noop(tmp_path):
    assert sd.LOADERS["none"].detect(tmp_path) is False
    sd.LOADERS["none"].wire(tmp_path)
    assert not (tmp_path / "mise.toml").exists()
    assert not (tmp_path / ".envrc").exists()


def test_profile_registry_exists_and_is_dict_of_str_to_profile():
    assert isinstance(sd.PROFILES, dict)
    for name, p in sd.PROFILES.items():
        assert isinstance(name, str)
        assert isinstance(p, sd.Profile)


def test_scanner_falls_back_to_unknown_when_no_profile_matches(tmp_path):
    # A directory with nothing recognizable.
    inv = sd.Scanner().scan(tmp_path)
    assert all(app.profile == "unknown" for app in inv.apps)


def test_vite_profile_detects_vite_config_ts(tmp_path):
    (tmp_path / "vite.config.ts").write_text("export default {}")
    p = sd.PROFILES["vite"]
    assert p.detect(tmp_path) is True


def test_vite_profile_detects_vite_config_js(tmp_path):
    (tmp_path / "vite.config.js").write_text("module.exports = {}")
    assert sd.PROFILES["vite"].detect(tmp_path) is True


def test_vite_profile_does_not_detect_without_config(tmp_path):
    assert sd.PROFILES["vite"].detect(tmp_path) is False


def test_vite_profile_emits_web_dev_port_resource(tmp_path):
    (tmp_path / "vite.config.ts").write_text("export default {}")
    app = sd.AppInventory(name="web", path=tmp_path, profile="vite")
    res = sd.PROFILES["vite"].resources(app)
    assert "WEB_DEV_PORT" in res
    assert res["WEB_DEV_PORT"]["type"] == "port"
    assert res["WEB_DEV_PORT"]["range"] == [5174, 5200]


def test_vite_profile_emits_api_dev_port_when_proxy_present(tmp_path):
    (tmp_path / "vite.config.ts").write_text(
        'export default { server: { proxy: { "/api": "http://localhost:9081" } } }'
    )
    app = sd.AppInventory(name="web", path=tmp_path, profile="vite")
    res = sd.PROFILES["vite"].resources(app)
    assert "API_DEV_PORT" in res
    assert res["API_DEV_PORT"]["type"] == "template"
    assert res["API_DEV_PORT"]["template"] == "{{ PORT }}"


def test_vite_profile_skips_api_dev_port_when_no_proxy(tmp_path):
    (tmp_path / "vite.config.ts").write_text("export default {}")
    app = sd.AppInventory(name="web", path=tmp_path, profile="vite")
    res = sd.PROFILES["vite"].resources(app)
    assert "API_DEV_PORT" not in res


def test_vite_wiring_check_detects_loadenv_pattern(tmp_path):
    (tmp_path / "vite.config.ts").write_text("""
import { defineConfig, loadEnv } from "vite";
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, import.meta.dirname, "");
  return { server: { port: Number(env.WEB_DEV_PORT ?? 5173) } };
});
""")
    app = sd.AppInventory(name="web", path=tmp_path, profile="vite")
    checks = sd.PROFILES["vite"].wiring_checks(app)
    check = next(c for c in checks if c.id == "vite-config-process-env")
    status, _ = check.detect(tmp_path)
    assert status == "problem"


def test_vite_wiring_check_autofix_swaps_loadenv_for_process_env(tmp_path):
    (tmp_path / "vite.config.ts").write_text("""\
import { defineConfig, loadEnv } from "vite";
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, import.meta.dirname, "");
  const webPort = Number(env.WEB_DEV_PORT ?? 5173);
  return { server: { port: webPort } };
});
""")
    app = sd.AppInventory(name="web", path=tmp_path, profile="vite")
    check = next(
        c for c in sd.PROFILES["vite"].wiring_checks(app) if c.id == "vite-config-process-env"
    )
    check.autofix(tmp_path)
    text = (tmp_path / "vite.config.ts").read_text()
    assert "process.env.WEB_DEV_PORT" in text
    status, _ = check.detect(tmp_path)
    assert status == "ok"


def test_vite_wiring_check_idempotent(tmp_path):
    (tmp_path / "vite.config.ts").write_text(
        "export default { server: { port: Number(process.env.WEB_DEV_PORT ?? 5173) } };\n"
    )
    app = sd.AppInventory(name="web", path=tmp_path, profile="vite")
    check = next(
        c for c in sd.PROFILES["vite"].wiring_checks(app) if c.id == "vite-config-process-env"
    )
    status, _ = check.detect(tmp_path)
    assert status == "ok"


def test_loader_registry_exists_with_mise():
    assert isinstance(sd.LOADERS, dict)
    assert "mise" in sd.LOADERS
    assert isinstance(sd.LOADERS["mise"], sd.Loader)


def test_mise_loader_wire_creates_mise_toml(tmp_path):
    sd.LOADERS["mise"].wire(tmp_path)
    assert (tmp_path / "mise.toml").exists()
    assert "splashdown.env" in (tmp_path / "mise.toml").read_text()


def test_mise_loader_wire_is_idempotent(tmp_path):
    sd.LOADERS["mise"].wire(tmp_path)
    first = (tmp_path / "mise.toml").read_text()
    sd.LOADERS["mise"].wire(tmp_path)
    assert (tmp_path / "mise.toml").read_text() == first


# ---------- resource-name app-scoping ----------


def test_resource_name_scoping_single_instance_keeps_canonical_name():
    apps = [sd.AppInventory(name="api", path=Path("."), profile="node-backend")]
    res_by_app = {"api": {"PORT": {"type": "port", "range": [9081, 9100]}}}
    merged = sd._merge_app_resources(apps, res_by_app)
    assert "PORT" in merged
    assert "api" not in merged.get("PORT", {}).get("__owners", [])  # canonical, single owner


def test_resource_name_scoping_multi_instance_mangles_with_app_name():
    apps = [
        sd.AppInventory(name="admin", path=Path("/a"), profile="vite"),
        sd.AppInventory(name="customer", path=Path("/b"), profile="vite"),
    ]
    res_by_app = {
        "admin": {"WEB_DEV_PORT": {"type": "port", "range": [5174, 5200]}},
        "customer": {"WEB_DEV_PORT": {"type": "port", "range": [5174, 5200]}},
    }
    merged = sd._merge_app_resources(apps, res_by_app)
    assert "WEB_DEV_PORT" not in merged  # original collided, both mangled
    assert "WEB_DEV_PORT_ADMIN" in merged
    assert "WEB_DEV_PORT_CUSTOMER" in merged


def test_resource_name_scoping_preserves_per_app_resources_list():
    apps = [
        sd.AppInventory(name="admin", path=Path("/a"), profile="vite"),
        sd.AppInventory(name="customer", path=Path("/b"), profile="vite"),
    ]
    res_by_app = {
        "admin": {"WEB_DEV_PORT": {"type": "port", "range": [5174, 5200]}},
        "customer": {"WEB_DEV_PORT": {"type": "port", "range": [5174, 5200]}},
    }
    sd._merge_app_resources(apps, res_by_app)
    # The helper also reports which names each app should consume:
    consumed = sd._app_resource_names(apps, res_by_app)
    assert consumed["admin"] == ["WEB_DEV_PORT_ADMIN"]
    assert consumed["customer"] == ["WEB_DEV_PORT_CUSTOMER"]


def test_cmd_init_scans_single_vite_app(tmp_path):
    (tmp_path / "vite.config.ts").write_text(
        "export default { server: { port: Number(process.env.WEB_DEV_PORT ?? 5173) } };\n"
    )
    (tmp_path / "package.json").write_text('{"name": "web"}')
    sd.cmd_init(tmp_path)
    recipe_text = (tmp_path / "splashdown.toml").read_text()
    assert "[project]" in recipe_text
    assert 'workspace = "single"' in recipe_text
    assert 'loader = "none"' in recipe_text  # no loader config present → no imposition
    assert "[apps.main]" in recipe_text
    assert 'profile = "vite"' in recipe_text
    assert "[resources.WEB_DEV_PORT]" in recipe_text


def test_cmd_init_emits_mise_loader_wiring(tmp_path):
    (tmp_path / "vite.config.ts").write_text("export default {}")
    sd.cmd_init(tmp_path, loader_override="mise")
    # mise.toml created and points at splashdown.env
    assert (tmp_path / "mise.toml").exists()
    assert "splashdown.env" in (tmp_path / "mise.toml").read_text()


def test_cmd_init_no_loader_routes_dotenv_app_to_env_file(tmp_path):
    # Next.js app, no shell loader, existing .env → values route into .env.
    (tmp_path / "next.config.js").write_text("module.exports = {}")
    (tmp_path / "package.json").write_text('{"dependencies": {"next": "15"}}')
    (tmp_path / ".env").write_text("")
    sd.cmd_init(tmp_path)
    recipe_text = (tmp_path / "splashdown.toml").read_text()
    assert 'loader = "none"' in recipe_text
    assert 'writer = "envfile=.env"' in recipe_text


def test_cmd_init_no_loader_no_dotenv_file_omits_writer_and_prints_instructions(tmp_path, capsys):
    # Vite app (process.env only), no shell loader, no .env → no writer routing,
    # and the user is told nothing sources splashdown.env.
    (tmp_path / "vite.config.ts").write_text("export default {}")
    sd.cmd_init(tmp_path)
    recipe_text = (tmp_path / "splashdown.toml").read_text()
    assert 'loader = "none"' in recipe_text
    assert "writer =" not in recipe_text
    assert "nothing sources it" in capsys.readouterr().err


def test_cmd_init_legacy_preset_path_still_works(tmp_path):
    sd.cmd_init(tmp_path, preset="minimal")
    recipe_text = (tmp_path / "splashdown.toml").read_text()
    # Legacy: writes the minimal scaffold verbatim, no [apps.*] / [project] tables.
    assert "[resources.RUN_ID]" in recipe_text
    assert "[apps.main]" not in recipe_text


def test_cmd_init_legacy_preset_no_loader_prints_instructions(tmp_path, capsys):
    # No loader config → the preset path can't route to a dotenv file, but it must
    # not leave the user with a silent no-op.
    sd.cmd_init(tmp_path, preset="minimal")
    err = capsys.readouterr().err
    assert "nothing sources it" in err


def test_cmd_init_unknown_framework_app_gets_unknown_profile(tmp_path):
    # No detectable framework signals — single-app with bare directory.
    sd.cmd_init(tmp_path)
    recipe_text = (tmp_path / "splashdown.toml").read_text()
    assert 'profile = "unknown"' in recipe_text
    # No resources for unknown profiles → [resources.*] section should be absent.
    assert "[resources." not in recipe_text


def test_cmd_init_writes_post_checkout_hook(tmp_path):
    # The hook wiring is independent of the Scanner-driven flow and must persist.
    (tmp_path / "vite.config.ts").write_text("export default {}")
    sd.cmd_init(tmp_path)
    # Either .githooks/post-checkout exists or one of the hook-manager configs
    # was wired — same contract as today.
    hook_exists = (tmp_path / ".githooks" / "post-checkout").exists()
    assert hook_exists


def test_refresh_inventory_updates_project_and_apps(tmp_path):
    # Start with a recipe that knows about the api app only.
    (tmp_path / "splashdown.toml").write_text("""\
[project]
workspace = "single"
loader = "mise"

[apps.api]
path = "."
profile = "node-backend"
resources = ["PORT"]

[resources.PORT]
type  = "port"
range = [9081, 9100]
""")
    # User adds vite alongside.
    (tmp_path / "vite.config.ts").write_text("export default {}")
    rc = sd.cmd_refresh_inventory(tmp_path)
    assert rc == 0
    text = (tmp_path / "splashdown.toml").read_text()
    assert 'profile = "vite"' in text
    # Original resource block preserved verbatim.
    assert "[resources.PORT]" in text
    assert "range = [9081, 9100]" in text


def test_refresh_inventory_on_legacy_recipe_upgrades_in_place(tmp_path):
    # A legacy single-resource recipe (no [project] / [apps.*]).
    (tmp_path / "splashdown.toml").write_text('[resources.RUN_ID]\ntype = "uuid"\n')
    (tmp_path / "vite.config.ts").write_text("export default {}")
    rc = sd.cmd_refresh_inventory(tmp_path)
    assert rc == 0
    text = (tmp_path / "splashdown.toml").read_text()
    assert "[project]" in text
    assert "[apps." in text
    # Existing resources are kept verbatim.
    assert "[resources.RUN_ID]" in text


def test_node_backend_profile_detects_hono(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies": {"hono": "^4.0.0"}}')
    assert sd.PROFILES["node-backend"].detect(tmp_path) is True


def test_node_backend_profile_detects_express(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies": {"express": "^4.0.0"}}')
    assert sd.PROFILES["node-backend"].detect(tmp_path) is True


def test_node_backend_profile_does_not_detect_without_framework(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies": {"react": "^18.0.0"}}')
    assert sd.PROFILES["node-backend"].detect(tmp_path) is False


def test_node_backend_profile_emits_port_resource(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies": {"hono": "^4.0.0"}}')
    app = sd.AppInventory(name="api", path=tmp_path, profile="node-backend")
    res = sd.PROFILES["node-backend"].resources(app)
    assert res == {"PORT": {"type": "port", "range": [9081, 9100]}}


def test_nextjs_profile_detects_next_config_js(tmp_path):
    (tmp_path / "next.config.js").write_text("module.exports = {}")
    assert sd.PROFILES["nextjs"].detect(tmp_path) is True


def test_nextjs_profile_detects_next_dep(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies": {"next": "^15.0.0"}}')
    assert sd.PROFILES["nextjs"].detect(tmp_path) is True


def test_nextjs_profile_emits_port_resource(tmp_path):
    (tmp_path / "next.config.js").write_text("")
    app = sd.AppInventory(name="web", path=tmp_path, profile="nextjs")
    res = sd.PROFILES["nextjs"].resources(app)
    assert res == {"PORT": {"type": "port", "range": [3000, 3100]}}


def test_django_profile_detects_manage_py(tmp_path):
    (tmp_path / "manage.py").write_text("import django\n")
    assert sd.PROFILES["django"].detect(tmp_path) is True


def test_django_profile_does_not_detect_without_manage_py(tmp_path):
    assert sd.PROFILES["django"].detect(tmp_path) is False


def test_django_profile_emits_port_resource(tmp_path):
    (tmp_path / "manage.py").write_text("import django\n")
    app = sd.AppInventory(name="api", path=tmp_path, profile="django")
    assert sd.PROFILES["django"].resources(app) == {"PORT": {"type": "port", "range": [8000, 8100]}}


def test_fastapi_profile_detects_via_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["fastapi==0.115.0", "uvicorn"]\n'
    )
    assert sd.PROFILES["fastapi"].detect(tmp_path) is True


def test_fastapi_profile_detects_via_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi==0.115.0\nuvicorn==0.30.0\n")
    assert sd.PROFILES["fastapi"].detect(tmp_path) is True


def test_fastapi_profile_does_not_detect_without_fastapi(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask==3.0.0\n")
    assert sd.PROFILES["fastapi"].detect(tmp_path) is False


def test_fastapi_profile_emits_port_resource(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    app = sd.AppInventory(name="api", path=tmp_path, profile="fastapi")
    assert sd.PROFILES["fastapi"].resources(app) == {
        "PORT": {"type": "port", "range": [8000, 8100]}
    }


def test_springboot_profile_detects_pom_xml(tmp_path):
    (tmp_path / "pom.xml").write_text(
        "<project><dependencies><dependency><artifactId>spring-boot-starter-web</artifactId></dependency></dependencies></project>"
    )
    assert sd.PROFILES["springboot"].detect(tmp_path) is True


def test_springboot_profile_detects_build_gradle(tmp_path):
    (tmp_path / "build.gradle.kts").write_text('plugins { id("org.springframework.boot") }')
    assert sd.PROFILES["springboot"].detect(tmp_path) is True


def test_springboot_profile_does_not_detect_plain_gradle(tmp_path):
    (tmp_path / "build.gradle").write_text('plugins { id("java") }')
    assert sd.PROFILES["springboot"].detect(tmp_path) is False


def test_springboot_profile_emits_port_resource(tmp_path):
    (tmp_path / "pom.xml").write_text("spring-boot-starter")
    app = sd.AppInventory(name="api", path=tmp_path, profile="springboot")
    assert sd.PROFILES["springboot"].resources(app) == {
        "PORT": {"type": "port", "range": [8080, 8180]}
    }


def test_springboot_wiring_check_flags_missing_port_placeholder(tmp_path):
    (tmp_path / "pom.xml").write_text("spring-boot-starter")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main").mkdir()
    (tmp_path / "src" / "main" / "resources").mkdir()
    (tmp_path / "src" / "main" / "resources" / "application.properties").write_text(
        "server.port=8080\n"
    )
    app = sd.AppInventory(name="api", path=tmp_path, profile="springboot")
    check = next(
        c
        for c in sd.PROFILES["springboot"].wiring_checks(app)
        if c.id == "springboot-application-properties"
    )
    status, _ = check.detect(tmp_path)
    assert status == "problem"


def test_springboot_wiring_check_accepts_env_placeholder(tmp_path):
    (tmp_path / "pom.xml").write_text("spring-boot-starter")
    (tmp_path / "src" / "main" / "resources").mkdir(parents=True)
    (tmp_path / "src" / "main" / "resources" / "application.properties").write_text(
        "server.port=${PORT:8080}\n"
    )
    app = sd.AppInventory(name="api", path=tmp_path, profile="springboot")
    check = next(
        c
        for c in sd.PROFILES["springboot"].wiring_checks(app)
        if c.id == "springboot-application-properties"
    )
    status, _ = check.detect(tmp_path)
    assert status == "ok"


# ---------- DirenvLoader ----------


def test_direnv_loader_wire_appends_sentinel_block(tmp_path):
    sd.LOADERS["direnv"].wire(tmp_path)
    text = (tmp_path / ".envrc").read_text()
    assert "# >>> splashdown-managed dotenv >>>" in text
    assert "dotenv_if_exists splashdown.env" in text
    assert "# <<< splashdown-managed dotenv <<<" in text


def test_direnv_loader_wire_idempotent(tmp_path):
    sd.LOADERS["direnv"].wire(tmp_path)
    first = (tmp_path / ".envrc").read_text()
    sd.LOADERS["direnv"].wire(tmp_path)
    assert (tmp_path / ".envrc").read_text() == first


def test_direnv_loader_wire_upgrades_legacy_dotenv_block(tmp_path):
    (tmp_path / ".envrc").write_text(
        "# >>> splashdown-managed dotenv >>>\n"
        "dotenv splashdown.env\n"
        "# <<< splashdown-managed dotenv <<<\n"
    )
    sd.LOADERS["direnv"].wire(tmp_path)
    text = (tmp_path / ".envrc").read_text()
    assert "dotenv_if_exists splashdown.env" in text
    assert "\ndotenv splashdown.env\n" not in text


def test_direnv_loader_wire_preserves_existing_envrc(tmp_path):
    (tmp_path / ".envrc").write_text("use nix\nlayout python\n")
    sd.LOADERS["direnv"].wire(tmp_path)
    text = (tmp_path / ".envrc").read_text()
    assert "use nix" in text
    assert "layout python" in text
    assert "dotenv_if_exists splashdown.env" in text


# ---------- DevboxLoader ----------


def test_devbox_loader_wire_adds_init_hook(tmp_path):
    (tmp_path / "devbox.json").write_text('{"packages": ["nodejs@22"]}')
    sd.LOADERS["devbox"].wire(tmp_path)
    data = json.loads((tmp_path / "devbox.json").read_text())
    hooks = data.get("shell", {}).get("init_hook", [])
    assert any("splashdown.env" in h for h in hooks)


def test_devbox_loader_wire_preserves_existing_packages(tmp_path):
    (tmp_path / "devbox.json").write_text('{"packages": ["nodejs@22", "pnpm@9"]}')
    sd.LOADERS["devbox"].wire(tmp_path)
    data = json.loads((tmp_path / "devbox.json").read_text())
    assert data["packages"] == ["nodejs@22", "pnpm@9"]


def test_devbox_loader_wire_idempotent(tmp_path):
    (tmp_path / "devbox.json").write_text("{}")
    sd.LOADERS["devbox"].wire(tmp_path)
    first = (tmp_path / "devbox.json").read_text()
    sd.LOADERS["devbox"].wire(tmp_path)
    assert (tmp_path / "devbox.json").read_text() == first


def test_react_native_profile_detects_via_package_json(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies": {"react-native": "0.83"}}')
    assert sd.PROFILES["react-native"].detect(tmp_path) is True


def test_expo_profile_detects_via_expo_dep_and_app_json(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies": {"expo": "50"}}')
    (tmp_path / "app.json").write_text("{}")
    assert sd.PROFILES["expo"].detect(tmp_path) is True


def test_flutter_profile_detects_via_pubspec(tmp_path):
    (tmp_path / "pubspec.yaml").write_text("name: x\n")
    assert sd.PROFILES["flutter"].detect(tmp_path) is True


def test_ios_native_profile_detects_via_xcworkspace(tmp_path):
    (tmp_path / "MyApp.xcworkspace").mkdir()
    assert sd.PROFILES["ios-native"].detect(tmp_path) is True


def test_android_native_profile_detects_via_gradle(tmp_path):
    (tmp_path / "build.gradle.kts").write_text("")
    (tmp_path / "settings.gradle.kts").write_text("")
    assert sd.PROFILES["android-native"].detect(tmp_path) is True


def test_react_native_profile_inherits_existing_wiring_checks(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies": {"react-native": "0.83"}}')
    app = sd.AppInventory(name="main", path=tmp_path, profile="react-native")
    checks = sd.PROFILES["react-native"].wiring_checks(app)
    ids = {c.id for c in checks}
    assert "rn-hook" in ids
    assert "rn-metro-config" in ids
    assert "rn-pkg-port" in ids
    assert "rn-xcode-env" in ids


# ---------- completion: packaging ----------


def test_argcomplete_marker_present_in_cli():
    cli_src = (ROOT / "src" / "splashdown" / "cli.py").read_text()
    # Marker must be within the first 1 KB so argcomplete's wrapper-follow finds it.
    assert "# PYTHON_ARGCOMPLETE_OK" in cli_src[:1024]


def test_argcomplete_importable():
    import argcomplete  # noqa: F401


# ---------- completion: completers ----------

from argparse import Namespace

from splashdown.completion import device_arg_completer, variant_completer


def _write_recipe(d: Path, body: str) -> None:
    (d / "splashdown.toml").write_text(body)


def test_variant_completer_lists_variants_for_typed_dtype(checkout):
    _write_recipe(
        checkout,
        '[targets.simulator.default]\nmodel = "A"\n[targets.simulator.small-screen]\nmodel = "B"\n',
    )
    args = Namespace(cwd=str(checkout), dtype="simulator")
    assert variant_completer("", args) == ["default", "small-screen"]


def test_variant_completer_prefix_filters(checkout):
    _write_recipe(
        checkout,
        '[targets.simulator.default]\nmodel = "A"\n[targets.simulator.small-screen]\nmodel = "B"\n',
    )
    args = Namespace(cwd=str(checkout), dtype="simulator")
    assert variant_completer("sm", args) == ["small-screen"]


def test_variant_completer_infers_single_type_when_dtype_none(checkout):
    _write_recipe(
        checkout,
        '[targets.simulator.default]\nmodel = "A"\n[targets.simulator.tablet]\nmodel = "B"\n',
    )
    args = Namespace(cwd=str(checkout), dtype=None)
    assert variant_completer("", args) == ["default", "tablet"]


def test_variant_completer_dedupes_across_types(checkout):
    _write_recipe(
        checkout,
        '[targets.simulator.default]\nmodel = "A"\n[targets.emulator.default]\nimage = "X"\n',
    )
    args = Namespace(cwd=str(checkout), dtype=None)
    # `default` declared under both types must appear once.
    assert variant_completer("", args) == ["default"]


def test_device_arg_completer_offers_variants_for_single_type(checkout):
    _write_recipe(
        checkout,
        '[targets.simulator.default]\nmodel = "A"\n[targets.simulator.tablet]\nmodel = "B"\n',
    )
    args = Namespace(cwd=str(checkout), dtype=None)
    # type name + variant names, sorted, deduped.
    assert device_arg_completer("", args) == ["default", "simulator", "tablet"]


def test_device_arg_completer_offers_only_type_names_for_multi_type(checkout):
    _write_recipe(
        checkout,
        '[targets.simulator.default]\nmodel = "A"\n[targets.emulator.default]\nimage = "X"\n',
    )
    args = Namespace(cwd=str(checkout), dtype=None)
    # Two declared types: offer only type names, no variants.
    assert device_arg_completer("", args) == ["emulator", "simulator"]


def test_completer_fail_silent_on_malformed_toml(checkout):
    _write_recipe(checkout, "this is not = valid toml [[[")
    args = Namespace(cwd=str(checkout), dtype="simulator")
    assert variant_completer("", args) == []
    assert device_arg_completer("", args) == []


# ---------- completion: arg normalization ----------

from splashdown.cli import _normalize_device_args
from splashdown.devices import DeviceError


def test_normalize_leaves_explicit_type_and_variant():
    args = Namespace(dtype="simulator", variant="small-screen")
    _normalize_device_args(args)
    assert (args.dtype, args.variant) == ("simulator", "small-screen")


def test_normalize_leaves_bare_type():
    args = Namespace(dtype="simulator", variant=None)
    _normalize_device_args(args)
    assert (args.dtype, args.variant) == ("simulator", None)


def test_normalize_reinterprets_lone_variant():
    args = Namespace(dtype="small-screen", variant=None)
    _normalize_device_args(args)
    assert (args.dtype, args.variant) == (None, "small-screen")


def test_normalize_leaves_nothing():
    args = Namespace(dtype=None, variant=None)
    _normalize_device_args(args)
    assert (args.dtype, args.variant) == (None, None)


def test_normalize_rejects_nontype_with_variant():
    args = Namespace(dtype="foo", variant="bar")
    with pytest.raises(DeviceError):
        _normalize_device_args(args)


def test_normalize_type_name_wins_as_type():
    args = Namespace(dtype="simulator", variant=None)
    _normalize_device_args(args)
    assert (args.dtype, args.variant) == ("simulator", None)


def test_run_accepts_lone_variant(tmp_path, monkeypatch):
    (tmp_path / "splashdown.toml").write_text(
        '[targets.simulator.small-screen]\nmodel = "iPhone SE"\n'
    )
    captured = {}

    def fake_cmd_run(cwd, registry, dtype, variant):
        captured["dtype"] = dtype
        captured["variant"] = variant
        return 0

    # main() resolves `cmd_run` in the cli module's namespace (it does
    # `from .commands import cmd_run`), so patch it there, not on the package.
    monkeypatch.setattr("splashdown.cli.cmd_run", fake_cmd_run)
    rc = sd.main(["--cwd", str(tmp_path), "run", "small-screen"])
    assert rc == 0
    assert captured == {"dtype": None, "variant": "small-screen"}


def test_run_rejects_nontype_with_variant_via_main(tmp_path):
    (tmp_path / "splashdown.toml").write_text('[targets.simulator.default]\nmodel = "A"\n')
    # `foo` is not a device type and a variant is already given -> DeviceError,
    # which main()'s try/except turns into exit code 1 (not an uncaught crash).
    rc = sd.main(["--cwd", str(tmp_path), "run", "foo", "bar"])
    assert rc == 1


# ---------- completion: protocol + wiring ----------

import io


def _argcomplete_completions(parser, comp_line, cwd):
    """Drive argcomplete in-process via its env protocol and return the list of
    completion strings it would emit for `comp_line`."""
    import contextlib

    import argcomplete

    env = {
        "_ARGCOMPLETE": "1",
        "_ARGCOMPLETE_IFS": "\013",
        "_ARGCOMPLETE_SUPPRESS_SPACE": "1",
        "COMP_LINE": comp_line,
        "COMP_POINT": str(len(comp_line)),
    }
    out = io.StringIO()
    saved = dict(os.environ)
    saved_cwd = os.getcwd()
    os.environ.update(env)
    os.chdir(cwd)
    try:
        with contextlib.suppress(SystemExit):
            argcomplete.autocomplete(parser, exit_method=sys.exit, output_stream=out)
    finally:
        os.environ.clear()
        os.environ.update(saved)
        os.chdir(saved_cwd)
    return out.getvalue().split("\013")


def test_comp_line_offers_variants_for_run_single_type(tmp_path):
    (tmp_path / "splashdown.toml").write_text(
        '[targets.simulator.default]\nmodel = "A"\n[targets.simulator.small-screen]\nmodel = "B"\n'
    )
    parser = sd._build_parser()
    out = _argcomplete_completions(parser, "splash run ", tmp_path)
    assert "small-screen" in out
    assert "default" in out


def test_install_is_noop_without_argcomplete_env(monkeypatch):
    from splashdown.completion import install

    # No _ARGCOMPLETE in env -> returns without importing/inspecting.
    monkeypatch.delenv("_ARGCOMPLETE", raising=False)
    assert install(sd._build_parser()) is None
