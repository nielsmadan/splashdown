"""Tests for splashdown devices behavior."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

import splashdown as sd
from conftest import (
    _IPHONE,
    _PIXEL,
    _stub_ios_boot_chain,
    _stub_ios_devices,
    _stub_physical,
    _write_physical_recipe,
)


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


def test_ensure_fresh_emulator_destroys_old_avd_on_rename(registry, checkout, monkeypatch):
    """When the resolved AVD name changes, the old AVD (row.udid) must be destroyed,
    not the new name — otherwise the old one is orphaned on disk. Mirrors iOS."""
    abspath = str(checkout.resolve())
    # Emulator rows store the AVD name in the udid column.
    registry.set_device(abspath, "emulator", "default", "old-avd", "pixel_9", "android-34")
    destroyed: list[str] = []
    # Only the OLD avd exists on disk; the freshly-resolved name does not (→ stale).
    monkeypatch.setattr(sd.devices, "_android_avd_exists", lambda n: n == "old-avd")
    monkeypatch.setattr(sd.commands, "_android_avd_exists", lambda n: n == "old-avd")
    monkeypatch.setattr(sd.devices, "_android_latest_image", lambda: "android-34")
    monkeypatch.setattr(sd.commands, "_android_latest_image", lambda: "android-34")
    monkeypatch.setattr(sd.devices, "android_destroy", destroyed.append)
    monkeypatch.setattr(sd.commands, "android_destroy", destroyed.append)
    monkeypatch.setattr(sd.devices, "android_ensure", lambda n, d, i: n)
    sd.ensure_fresh_sim(
        registry, checkout, "emulator", "default", {"device": "pixel_9", "name": "new-avd"}
    )
    assert destroyed == ["old-avd"]
    assert registry.get_device(abspath, "emulator", "default").udid == "new-avd"


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


def test_cli_sync_keeps_pinned_port_when_bound(tmp_path, monkeypatch, capsys):
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
    # Bind the assigned port — simulating this checkout's own dev server running.
    port_str = first.split("=", 1)[1]
    import socket as _sock

    squatter = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
    squatter.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
    squatter.bind(("127.0.0.1", int(port_str)))
    squatter.listen(1)
    try:
        # Plain sync must KEEP the pin even though the port is bound — otherwise
        # it would move the port out from under the running dev server.
        rc = sd.main(["--cwd", str(tmp_path), "sync"])
        assert rc == 0
        kept = (tmp_path / "splashdown.env").read_text().strip()
        assert kept == first
        # `sync --force` is the explicit reallocation path: it drops the pin
        # first, so the now-bound port is skipped and a fresh one is chosen.
        rc = sd.main(["--cwd", str(tmp_path), "sync", "--force"])
        assert rc == 0
    finally:
        squatter.close()
    forced = (tmp_path / "splashdown.env").read_text().strip()
    assert forced != first


def test_allocate_port_keeps_existing_pin_when_in_use(registry, checkout, monkeypatch):
    first = registry.allocate_port(str(checkout), "PORT", 18600, 18610)
    # Force the bind probe to report every port busy: a plain re-allocation must
    # still return the existing in-range pin rather than trying to move it.
    monkeypatch.setattr(sd.registry, "_port_in_use", lambda port: True)
    again = registry.allocate_port(str(checkout), "PORT", 18600, 18610)
    assert again == first


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
    (tmp_path / sd.RECIPE_NAME).write_text('[resources.K]\ntype = "set"\n')
    assert sd.main(["--cwd", str(tmp_path), "env", "set", "K=v1"]) == 0
    assert sd.main(["--cwd", str(tmp_path), "env", "get", "K"]) == 0
    assert capsys.readouterr().out.strip() == "v1"


def test_cli_env_set_rejects_undeclared_key_when_recipe_present(tmp_path, monkeypatch, capsys):
    """A key the recipe doesn't declare would be silently dropped by the next
    `splash gc` reconcile — reject it up front instead of losing the value."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / sd.RECIPE_NAME).write_text('[resources.KNOWN]\ntype = "set"\n')
    assert sd.main(["--cwd", str(tmp_path), "env", "set", "UNKNOWN=x"]) == 2
    assert "not a resource" in capsys.readouterr().err
    # A declared key still works.
    assert sd.main(["--cwd", str(tmp_path), "env", "set", "KNOWN=y"]) == 0


def test_cli_env_set_rejects_invalid_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # A key that isn't a valid env name would write a malformed dotenv line.
    assert sd.main(["--cwd", str(tmp_path), "env", "set", "FOO BAR=x"]) == 2
    assert "invalid env name" in capsys.readouterr().err


def test_cli_env_set_rejects_non_set_resource(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / sd.RECIPE_NAME).write_text(
        '[resources.PORT]\ntype = "port"\nrange = [19700, 19710]\n'
    )
    assert sd.main(["--cwd", str(tmp_path), "env", "set", "PORT=garbage"]) == 2
    assert 'only type="set"' in capsys.readouterr().err
    registry = sd.Registry()
    assert registry.get_kv(str(tmp_path.resolve()), "PORT") is None


def test_cli_env_set_requires_recipe(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert sd.main(["--cwd", str(tmp_path), "env", "set", "K=v1"]) == 2
    assert sd.RECIPE_NAME in capsys.readouterr().err


def test_cli_env_set_release_honor_checkout(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    other = tmp_path / "other"
    other.mkdir()
    (other / sd.RECIPE_NAME).write_text('[resources.K]\ntype = "set"\n')
    # set/release can target another checkout, the same way get already can.
    assert sd.main(["--cwd", str(tmp_path), "env", "set", "K=v1", "--checkout", str(other)]) == 0
    assert sd.main(["--cwd", str(tmp_path), "env", "get", "K", "--checkout", str(other)]) == 0
    assert capsys.readouterr().out.strip() == "v1"
    # ...and it's scoped: this checkout doesn't see it.
    assert sd.main(["--cwd", str(tmp_path), "env", "get", "K"]) == 1
    assert sd.main(["--cwd", str(tmp_path), "env", "release", "K", "--checkout", str(other)]) == 0
    assert sd.main(["--cwd", str(tmp_path), "env", "get", "K", "--checkout", str(other)]) == 1


def test_resolve_device_name_rejects_leading_dash(tmp_path):
    with pytest.raises(sd.DeviceError):
        sd._resolve_device_name({"name": "-rf"}, tmp_path, "default")


@pytest.mark.parametrize(
    ("destroy", "destroy_args", "message"),
    [
        (sd.devices.ios_destroy, ("UDID",), "simctl delete failed"),
        (sd.devices.android_destroy, ("avd",), "avdmanager delete failed"),
    ],
)
def test_device_destroy_reports_command_failure(monkeypatch, destroy, destroy_args, message):
    monkeypatch.setattr(sd.devices, "_android_bin", lambda name: name)
    monkeypatch.setattr(
        sd.devices.subprocess,
        "run",
        lambda *call_args, **kwargs: subprocess.CompletedProcess(
            call_args[0], 1, "", "delete failed"
        ),
    )
    with pytest.raises(sd.DeviceError, match=message):
        destroy(*destroy_args)


def test_cli_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        sd.main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "splashdown" in out
    assert re.search(r"\d+\.\d+\.\d+", out)


def test_tomlkit_not_imported_on_hot_path():
    """The git-hook hot path only READS TOML (stdlib tomllib). tomlkit is a
    write-only dep and must stay lazy — importing the package or the read modules
    must not pull it in. Checked in a fresh subprocess for a clean module table."""
    code = (
        "import sys, splashdown, splashdown.recipe, splashdown.provisioning, splashdown.scanner; "
        "assert 'tomlkit' not in sys.modules, "
        "sorted(m for m in sys.modules if 'toml' in m)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


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


def test_cli_device_remove_recipe_variant_does_not_destroy(tmp_path, monkeypatch):
    (tmp_path / "splashdown.toml").write_text('[targets.simulator.default]\nmodel = "iPhone 17"\n')
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    destroyed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        sd.commands, "device_destroy", lambda dtype, name: destroyed.append((dtype, name))
    )
    rc = sd.main(["--cwd", str(tmp_path), "target", "remove", "simulator", "default"])
    assert rc == 1
    assert destroyed == []
    assert "[targets.simulator.default]" in (tmp_path / "splashdown.toml").read_text()


def test_cli_device_remove_failure_keeps_local_variant(tmp_path, monkeypatch, capsys):
    (tmp_path / "splashdown.toml").write_text("")
    local = tmp_path / "splashdown.local.toml"
    local.write_text('[targets.simulator.repro]\nmodel = "iPhone 17"\n')
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    registry = sd.Registry()
    registry.set_device(
        str(tmp_path.resolve()),
        "simulator",
        "repro",
        "UDID",
        "iPhone 17",
        "18.5",
    )

    def fail_destroy(row):
        raise sd.DeviceError(f"could not destroy {row.dtype} {row.udid}")

    monkeypatch.setattr(sd.commands, "device_destroy_row", fail_destroy)
    rc = sd.main(["--cwd", str(tmp_path), "target", "remove", "simulator", "repro"])
    assert rc == 1
    assert "[targets.simulator.repro]" in local.read_text()
    assert registry.get_device(str(tmp_path.resolve()), "simulator", "repro") is not None
    assert "and destroyed the instance" not in capsys.readouterr().err


def test_cli_device_remove_destroys_registered_instance(tmp_path, monkeypatch):
    (tmp_path / "splashdown.toml").write_text("")
    local = tmp_path / "splashdown.local.toml"
    local.write_text('[targets.simulator.repro]\nname = "new-name"\n')
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    registry = sd.Registry()
    registry.set_device(
        str(tmp_path.resolve()),
        "simulator",
        "repro",
        "UDID-OLD",
        "iPhone 17",
        "18.5",
    )
    destroyed = []
    monkeypatch.setattr(sd.commands, "device_destroy_row", destroyed.append)
    monkeypatch.setattr(
        sd.commands,
        "device_destroy",
        lambda *args: pytest.fail(f"resolved-name fallback used: {args}"),
    )

    rc = sd.main(["--cwd", str(tmp_path), "target", "remove", "simulator", "repro"])

    assert rc == 0
    assert destroyed[0].udid == "UDID-OLD"
    assert registry.get_device(str(tmp_path.resolve()), "simulator", "repro") is None
    assert "[targets.simulator.repro]" not in local.read_text()


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
    destroyed_count = sd.cmd_target_gc(registry)
    assert destroyed_count == 1
    assert destroyed == ["UDID-A"]
    assert {r.udid for r in registry.all_devices()} == {"UDID-B"}


def test_device_gc_drops_defunct_emulator_and_destroys_avd(registry, tmp_path, monkeypatch):
    """gc destroys the AVD (not just the sim) of a dead checkout's emulator row."""
    gone = tmp_path / "gone"
    gone.mkdir()
    registry.set_device(str(gone), "emulator", "default", "AVD-GONE", "pixel_9", "android-34")
    gone.rmdir()
    destroyed: list[str] = []
    monkeypatch.setattr(sd.devices, "_android_avd_exists", lambda n: True)
    monkeypatch.setattr(sd.commands, "_android_avd_exists", lambda n: True)
    monkeypatch.setattr(sd.devices, "android_destroy", destroyed.append)
    monkeypatch.setattr(sd.commands, "android_destroy", destroyed.append)
    assert sd.cmd_target_gc(registry) == 1
    assert destroyed == ["AVD-GONE"]
    assert list(registry.all_devices()) == []


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


def test_device_refresh_resolves_latest_os_once_across_rows(registry, tmp_path, monkeypatch):
    """The latest-OS lookup shells out; with a shared cache it must fire at most
    once per platform even across many rows (perf: no per-row subprocess)."""
    checkout = tmp_path / "co"
    checkout.mkdir()
    (checkout / "splashdown.toml").write_text(
        '[targets.simulator.a]\nmodel = "iPhone 17"\n[targets.simulator.b]\nmodel = "iPhone 17"\n'
    )
    abspath = str(checkout.resolve())
    registry.set_device(abspath, "simulator", "a", "UDID-A", "iPhone 17", "18.5")
    registry.set_device(abspath, "simulator", "b", "UDID-B", "iPhone 17", "18.5")
    calls = {"n": 0}

    def counting_latest() -> str:
        calls["n"] += 1
        return "18.5"

    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.devices, "_ios_latest_runtime_version", counting_latest)
    monkeypatch.setattr(sd.commands, "_ios_latest_runtime_version", counting_latest)
    sd.cmd_target_refresh(registry, platforms=("ios",))
    assert calls["n"] == 1, f"latest-OS resolved {calls['n']}x; cache not shared"


def test_device_needs_recreate_agrees_with_actuator_on_emulator_rename(
    registry, checkout, monkeypatch
):
    """Counter and actuator must agree: a renamed emulator is a recreate, not
    'unchanged' (the count-divergence bug)."""
    abspath = str(checkout.resolve())
    registry.set_device(abspath, "emulator", "default", "old-avd", "pixel_9", "android-34")
    monkeypatch.setattr(sd.devices, "_android_avd_exists", lambda n: n == "old-avd")
    monkeypatch.setattr(sd.devices, "_android_latest_image", lambda: "android-34")
    spec = {"device": "pixel_9", "name": "new-avd"}
    assert sd.device_needs_recreate(registry, checkout, "emulator", "default", spec) is True


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


def test_version_tuple_parses_and_falls_back():
    assert sd.devices._version_tuple("18.5") == (18, 5)
    assert sd.devices._version_tuple("17.0.1") == (17, 0, 1)
    assert sd.devices._version_tuple("nope") == (0,)


def test_ios_latest_runtime_sorts_numerically(monkeypatch):
    monkeypatch.setattr(
        sd.devices,
        "_xcrun_json",
        lambda args: {
            "runtimes": [
                {"identifier": "x.iOS-17-0", "version": "17.0", "isAvailable": True},
                {"identifier": "x.iOS-9-0", "version": "9.0", "isAvailable": True},
                {"identifier": "x.iOS-18-5", "version": "18.5", "isAvailable": True},
                {"identifier": "x.iOS-19-0", "version": "19.0", "isAvailable": False},  # filtered
            ]
        },
    )
    assert sd.devices._ios_latest_runtime_version() == "18.5"
    assert sd.devices._ios_latest_runtime().endswith("iOS-18-5")


def test_ios_device_type_identifier_selection(monkeypatch):
    monkeypatch.setattr(
        sd.devices,
        "_xcrun_json",
        lambda args: {
            "devicetypes": [
                {"identifier": "t.iPhone-16", "name": "iPhone 16"},
                {"identifier": "t.iPhone-16-Pro", "name": "iPhone 16 Pro"},
                {"identifier": "t.iPhone-17-Pro", "name": "iPhone 17 Pro"},
            ]
        },
    )
    assert sd.devices._ios_device_type_identifier(None).endswith("iPhone-17-Pro")  # latest Pro
    assert sd.devices._ios_device_type_identifier("iPhone 16").endswith("iPhone-16")
    with pytest.raises(sd.DeviceError):
        sd.devices._ios_device_type_identifier("iPhone 99")


def test_android_latest_image_picks_highest_api(monkeypatch):
    monkeypatch.setattr(sd.devices, "_android_bin", lambda name: "/fake/" + name)
    monkeypatch.setattr(
        sd.devices.subprocess,
        "check_output",
        lambda *a, **k: (
            b"system-images;android-33;google_apis;arm64-v8a\n"
            b"system-images;android-34;google_apis;arm64-v8a\n"
        ),
    )
    assert sd.devices._android_latest_image() == "system-images;android-34;google_apis;arm64-v8a"


def test_android_running_serial_matches_avd(monkeypatch):
    monkeypatch.setattr(sd.devices, "_android_bin", lambda name: "/fake/" + name)
    outputs = [b"List of devices attached\nemulator-5554\tdevice\n", b"my_avd\n"]
    monkeypatch.setattr(sd.devices.subprocess, "check_output", lambda *a, **k: outputs.pop(0))
    assert sd.devices._android_running_serial("my_avd") == "emulator-5554"


def test_android_running_serial_no_match(monkeypatch):
    monkeypatch.setattr(sd.devices, "_android_bin", lambda name: "/fake/" + name)
    outputs = [b"List of devices attached\nemulator-5554\tdevice\n", b"other_avd\n"]
    monkeypatch.setattr(sd.devices.subprocess, "check_output", lambda *a, **k: outputs.pop(0))
    assert sd.devices._android_running_serial("my_avd") is None


def test_default_sim_name():
    assert sd.devices._default_sim_name(Path("/work/myapp/co"), "default") == "myapp/co/default"


def test_sanitize_avd_name():
    assert sd.devices._sanitize_avd_name("My App/v1.2") == "My_App_v1.2"


def test_ios_find_device_by_name(monkeypatch):
    _stub_ios_devices(
        monkeypatch,
        {
            "iOS-18-5": [
                {"name": "MySim", "udid": "U1", "state": "Booted", "isAvailable": True},
                {"name": "Other", "udid": "U2", "state": "Shutdown", "isAvailable": True},
            ]
        },
    )
    assert sd.devices._ios_find_device_by_name("MySim") == ("U1", "Booted")
    assert sd.devices._ios_find_device_by_name("Nope") is None


def test_ios_find_device_by_name_skips_unavailable(monkeypatch):
    _stub_ios_devices(
        monkeypatch,
        {"iOS-18-5": [{"name": "MySim", "udid": "U1", "state": "Shutdown", "isAvailable": False}]},
    )
    assert sd.devices._ios_find_device_by_name("MySim") is None


def test_ios_current_state_and_udid_exists(monkeypatch):
    _stub_ios_devices(monkeypatch, {"iOS-18-5": [{"udid": "U1", "state": "Booted"}]})
    assert sd.devices._ios_current_state("U1") == "Booted"
    assert sd.devices._ios_current_state("MISSING") == "Unknown"
    assert sd.devices._ios_udid_exists("U1") is True
    assert sd.devices._ios_udid_exists("U2") is False


def test_android_avd_exists(monkeypatch):
    monkeypatch.setattr(sd.devices, "_android_bin", lambda name: "/fake/" + name)
    monkeypatch.setattr(sd.devices.subprocess, "check_output", lambda *a, **k: b"pixel_9\nmy_avd\n")
    assert sd.devices._android_avd_exists("my_avd") is True
    assert sd.devices._android_avd_exists("ghost") is False


def test_detect_framework_override_and_autodetect(tmp_path):
    vite = sd.detect_framework(tmp_path, sd.Recipe({"project": {"framework": "vite"}}, tmp_path))
    assert vite == "vite"
    (tmp_path / "pubspec.yaml").write_text("name: app\n")  # auto-detect → flutter
    assert sd.detect_framework(tmp_path, sd.Recipe({}, tmp_path)) == "flutter"


def test_detect_framework_auto_sentinel_loads_and_autodetects(tmp_path):
    # `framework = "auto"` must survive recipe validation and mean auto-detect.
    (tmp_path / "pubspec.yaml").write_text("name: app\n")
    recipe = sd.Recipe.parse('[project]\nframework = "auto"\n', tmp_path / "splashdown.toml")
    assert sd.detect_framework(tmp_path, recipe) == "flutter"


def test_detect_framework_unknown_raises(tmp_path):
    with pytest.raises(sd.DeviceError):
        sd.detect_framework(tmp_path, sd.Recipe({}, tmp_path))


def test_device_run_custom_command_bypasses_framework_detection(tmp_path, monkeypatch):
    # No framework markers in the dir — detect_framework would raise. A custom
    # run command must run instead, never reaching detection.
    captured = {}

    def _fake_call(cmd, **k):
        captured["cmd"] = cmd
        return 7

    monkeypatch.setattr(sd.profiles.subprocess, "call", _fake_call)
    recipe = sd.Recipe({"project": {"run": "echo ran {device_id}"}}, tmp_path / "splashdown.toml")
    rc = sd.device_run(tmp_path, recipe, {"kind": "ios", "udid": "ABCD"})
    assert rc == 7  # returns the custom command's exit code
    assert captured["cmd"] == "echo ran ABCD"


def test_device_run_no_custom_command_uses_framework(tmp_path, monkeypatch):
    # With no [project] run, device_run must fall through to framework detection
    # and launch via the profile — a regression that returned 0/"" instead of
    # None from run_custom_command would silently break every normal run.
    recipe = sd.Recipe({}, tmp_path / "splashdown.toml")
    monkeypatch.setattr(sd.devices, "detect_framework", lambda cwd, r: "flutter")
    called = {}

    def _fw_run(cwd, r, info):
        called["fw"] = True
        return 3

    monkeypatch.setattr(sd.PROFILES["flutter"], "run", _fw_run)
    rc = sd.device_run(tmp_path, recipe, {"kind": "ios", "udid": "U"})
    assert called.get("fw") is True
    assert rc == 3


def test_global_target_add_and_remove(tmp_path):
    path = sd.global_target_add("device", "my-iphone", {"platform": "ios", "name": "Niels iPhone"})
    assert path == sd._global_config_path()
    gc = sd.GlobalConfig.load(path)
    assert gc.targets["device"]["my-iphone"]["platform"] == "ios"
    sd.global_target_remove("device", "my-iphone")
    assert "my-iphone" not in sd.GlobalConfig.load(path).targets.get("device", {})


def test_global_target_add_rejects_duplicate(tmp_path):
    sd.global_target_add("device", "my-iphone", {"platform": "ios"})
    with pytest.raises(sd.DeviceError, match="already exists"):
        sd.global_target_add("device", "my-iphone", {"platform": "ios"})


def test_global_target_add_rejects_incompatible_fields():
    with pytest.raises(sd.DeviceError, match="unknown field"):
        sd.global_target_add("simulator", "default", {"device": "pixel_9"})


def test_global_target_remove_errors_when_missing(tmp_path):
    with pytest.raises(sd.DeviceError, match="no target"):
        sd.global_target_remove("device", "ghost")
