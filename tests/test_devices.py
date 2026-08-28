from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import threading
import time
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
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: True)
    info = sd.ensure_fresh_sim(registry, checkout, "simulator", "default", {"model": "iPhone 17"})
    assert info["udid"] == "UDID-NEW"
    assert info["name"] == sd._default_sim_name(checkout, "default")
    assert created["call"][1] == "iPhone 17"
    assert created["call"][2] == "18.5"
    assert registry.get_device(str(checkout.resolve()), "simulator", "default").udid == "UDID-NEW"


def test_ensure_fresh_recreates_when_ios_stale(registry, checkout, monkeypatch):
    abspath = str(checkout.resolve())
    registry.set_device(abspath, "simulator", "default", "UDID-OLD", "iPhone 17", "17.5")
    lifecycle = []
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.devices, "_ios_latest_runtime_version", lambda: "18.5")
    monkeypatch.setattr(sd.devices, "ios_shutdown", lambda u: lifecycle.append(("shutdown", u)))
    monkeypatch.setattr(sd.devices, "ios_destroy", lambda u: lifecycle.append(("destroy", u)))
    monkeypatch.setattr(sd.devices, "ios_ensure", lambda n, m, i: ("UDID-NEW", "Shutdown"))
    sd.ensure_fresh_sim(registry, checkout, "simulator", "default", {"model": "iPhone 17"})
    assert lifecycle == [("shutdown", "UDID-OLD"), ("destroy", "UDID-OLD")]
    assert registry.get_device(abspath, "simulator", "default").udid == "UDID-NEW"


def test_ensure_fresh_keeps_when_pinned_and_current(registry, checkout, monkeypatch):
    abspath = str(checkout.resolve())
    registry.set_device(abspath, "simulator", "legacy", "UDID-X", "iPhone 12", "17.0")
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.devices, "_ios_latest_runtime_version", lambda: "18.5")
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
    lifecycle = []
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.devices, "ios_shutdown", lambda u: lifecycle.append(("shutdown", u)))
    monkeypatch.setattr(sd.devices, "ios_destroy", lambda u: lifecycle.append(("destroy", u)))
    monkeypatch.setattr(sd.devices, "ios_ensure", lambda n, m, i: ("UDID-NEW", "Shutdown"))
    monkeypatch.setattr(sd.devices, "_ios_latest_runtime_version", lambda: "18.5")
    sd.ensure_fresh_sim(
        registry,
        checkout,
        "simulator",
        "legacy",
        {"model": "iPhone 12", "ios": "17.5"},
    )
    assert lifecycle == [("shutdown", "UDID-OLD"), ("destroy", "UDID-OLD")]


def test_ensure_fresh_recreates_when_model_changed(registry, checkout, monkeypatch):
    abspath = str(checkout.resolve())
    registry.set_device(abspath, "simulator", "default", "UDID-OLD", "iPhone 17", "18.5")
    lifecycle = []
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.devices, "_ios_latest_runtime_version", lambda: "18.5")
    monkeypatch.setattr(sd.devices, "ios_shutdown", lambda u: lifecycle.append(("shutdown", u)))
    monkeypatch.setattr(sd.devices, "ios_destroy", lambda u: lifecycle.append(("destroy", u)))
    monkeypatch.setattr(sd.devices, "ios_ensure", lambda n, m, i: ("UDID-NEW", "Shutdown"))
    sd.ensure_fresh_sim(registry, checkout, "simulator", "default", {"model": "iPhone 18"})
    assert lifecycle == [("shutdown", "UDID-OLD"), ("destroy", "UDID-OLD")]


def test_ensure_fresh_emulator_destroys_old_avd_on_rename(registry, checkout, monkeypatch):
    """When the resolved AVD name changes, the old AVD (row.udid) must be destroyed,
    not the new name — otherwise the old one is orphaned on disk. Mirrors iOS."""
    abspath = str(checkout.resolve())
    registry.set_device(abspath, "emulator", "default", "old-avd", "pixel_9", "android-34")
    lifecycle: list[tuple[str, str]] = []
    # Only the OLD avd exists on disk; the freshly-resolved name does not (→ stale).
    monkeypatch.setattr(sd.devices, "_android_avd_exists", lambda n: n == "old-avd")
    monkeypatch.setattr(sd.devices, "_android_latest_image", lambda: "android-34")
    monkeypatch.setattr(sd.devices, "android_shutdown", lambda n: lifecycle.append(("shutdown", n)))
    monkeypatch.setattr(sd.devices, "android_destroy", lambda n: lifecycle.append(("destroy", n)))
    monkeypatch.setattr(sd.devices, "android_ensure", lambda n, d, i: n)
    sd.ensure_fresh_sim(
        registry, checkout, "emulator", "default", {"device": "pixel_9", "name": "new-avd"}
    )
    assert lifecycle == [("shutdown", "old-avd"), ("destroy", "old-avd")]
    assert registry.get_device(abspath, "emulator", "default").udid == "new-avd"


def test_ensure_fresh_recreates_when_udid_gone(registry, checkout, monkeypatch):
    abspath = str(checkout.resolve())
    registry.set_device(abspath, "simulator", "default", "UDID-X", "iPhone 17", "18.5")
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: False)
    monkeypatch.setattr(sd.devices, "_ios_latest_runtime_version", lambda: "18.5")
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


def test_physical_discovery_once_per_run_including_busy_result(tmp_path, registry, monkeypatch):
    (tmp_path / sd.RECIPE_NAME).write_text(
        '[targets.device.pixel]\nplatform = "android"\nid = "PXL1234"\n'
    )
    target = sd.resolve_physical_target(tmp_path, "pixel")
    owner = tmp_path / "owner"
    owner.mkdir()
    registry.attempt_claim(
        sd.PhysicalClaim(target.catalog_identity, "android", "PXL1234", "pixel", str(owner), "old")
    )
    probes: list[str] = []
    launches: list[str] = []

    def discover(platform, **_kwargs):
        probes.append(platform)
        return (_PIXEL,)

    def launch(_cwd, _recipe, destination):
        launches.append(destination["serial"])
        assert registry.all_claims()[0].hardware_id == destination["serial"]
        return 0

    monkeypatch.setattr(sd.device_claims, "discover_physical_snapshot", discover)
    monkeypatch.setattr(sd.target_commands, "validate_device_run", lambda *_args: None)
    monkeypatch.setattr(
        sd.target_commands,
        "ensure_fresh_sim",
        lambda *_args, **_kwargs: pytest.fail("physical runs must not reconcile managed devices"),
    )
    monkeypatch.setattr(sd.target_commands, "device_run", launch)

    with pytest.raises(sd.DeviceError, match="claimed by"):
        sd.cmd_run(tmp_path, registry, "device", "pixel")

    assert probes == ["android"]
    assert launches == []

    assert (
        registry.release_claim(target.catalog_identity, str(owner), force=True).status == "released"
    )
    assert sd.cmd_run(tmp_path, registry, "device", "pixel") == 0

    assert probes == ["android", "android"]
    assert launches == ["PXL1234"]


def test_global_android_variant_scopes_react_native_launch(tmp_path, registry, monkeypatch):
    (tmp_path / sd.RECIPE_NAME).write_text(
        '[project]\nframework = "react-native"\n[targets.simulator.default]\nmodel = "iPhone 17"\n'
    )
    p = sd._global_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('[targets.device.pixel]\nplatform = "android"\nname = "Pixel_9a"\n')
    selected = {"id": "192.0.2.10:42137", "name": "Pixel_9a", "platform": "android"}
    _stub_physical(monkeypatch, android=[selected, _PIXEL])
    monkeypatch.setattr(
        sd.device_claims, "discover_physical_snapshot", lambda *_args, **_kwargs: (selected, _PIXEL)
    )
    calls: list[tuple[list[str], dict]] = []

    def call(args, **kwargs):
        calls.append((args, kwargs))
        return 0

    monkeypatch.setattr(sd.runners.subprocess, "call", call)

    assert sd.cmd_run(tmp_path, registry, None, "pixel") == 0
    args, kwargs = calls[0]
    assert args == [
        "npx",
        "react-native",
        "run-android",
        "--deviceId",
        "192.0.2.10:42137",
    ]
    assert kwargs["env"]["ANDROID_SERIAL"] == "192.0.2.10:42137"


def test_ensure_physical_errors_when_none(monkeypatch):
    _stub_physical(monkeypatch)
    with pytest.raises(sd.DeviceError, match="no connected physical device"):
        sd.ensure_physical({})


def test_ensure_physical_errors_when_ambiguous(monkeypatch):
    _stub_physical(monkeypatch, ios=[_IPHONE], android=[_PIXEL])
    with pytest.raises(sd.DeviceError, match="multiple connected"):
        sd.ensure_physical({})


def test_physical_discover_tolerates_missing_ios_toolchain(monkeypatch, capsys):
    def _boom():
        raise sd.CapabilityError("ios", "iOS physical-device support requires macOS and Xcode")

    monkeypatch.setattr(sd.devices, "_ios_physical_devices", _boom)
    monkeypatch.setattr(sd.devices, "_android_physical_devices", lambda: [_PIXEL])
    assert sd.physical_discover() == [_PIXEL]
    assert "warning: skipping iOS" in capsys.readouterr().err
    with pytest.raises(sd.CapabilityError):
        sd.physical_discover("ios")


def test_physical_status_states(monkeypatch):
    _stub_physical(monkeypatch, ios=[_IPHONE])
    assert sd.physical_status({}) == "connected"
    _stub_physical(monkeypatch)
    assert sd.physical_status({}) == "absent"
    _stub_physical(monkeypatch, ios=[_IPHONE], android=[_PIXEL])
    assert sd.physical_status({}) == "ambiguous"


def test_ios_physical_devices_parses_devicectl(monkeypatch):
    monkeypatch.setattr(sd.capabilities.sys, "platform", "darwin")

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
    monkeypatch.setattr(sd.device_android, "_android_bin", lambda name: "/fake/adb")
    monkeypatch.setattr(sd.devices.subprocess, "check_output", lambda *a, **k: out)
    devices = sd._android_physical_devices()
    assert devices == [{"id": "PXL1234", "name": "Pixel_7", "platform": "android"}]


def test_ios_physical_devices_forwards_timeout_budget(monkeypatch):
    monkeypatch.setattr(sd.capabilities.sys, "platform", "darwin")
    seen: dict[str, object] = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        seen["timeout"] = kwargs["timeout"]
        return b'{"result": {"devices": []}}'

    monkeypatch.setattr(sd.device_ios, "check_output_finite", run)

    assert sd._ios_physical_devices(timeout=5) == []
    assert seen == {
        "argv": ["xcrun", "devicectl", "list", "devices", "--json-output", "-"],
        "timeout": 5,
    }


def test_android_physical_devices_forwards_timeout_budget(monkeypatch):
    monkeypatch.setattr(sd.device_android, "_android_bin", lambda name: "/fake/adb")
    seen: dict[str, object] = {}

    def run(argv, **kwargs):
        seen["argv"] = argv
        seen["timeout"] = kwargs["timeout"]
        return b"List of devices attached\n"

    monkeypatch.setattr(sd.device_android, "check_output_finite", run)

    assert sd._android_physical_devices(timeout=5) == []
    assert seen == {"argv": ["/fake/adb", "devices", "-l"], "timeout": 5}


def test_physical_snapshot_any_is_concurrent_and_platform_ordered(monkeypatch):
    calls: list[str] = []
    budgets: list[float] = []

    def ios(*, timeout):
        budgets.append(timeout)
        time.sleep(0.02)
        calls.append("ios")
        return [_IPHONE]

    def android(*, timeout):
        budgets.append(timeout)
        calls.append("android")
        return [_PIXEL]

    monkeypatch.setattr(sd.device_claims, "_ios_physical_devices", ios)
    monkeypatch.setattr(sd.device_claims, "_android_physical_devices", android)

    assert sd.discover_physical_snapshot("any", timeout=5) == (_IPHONE, _PIXEL)
    assert calls == ["android", "ios"]
    assert len(budgets) == 2
    assert all(4.9 < budget <= 5 for budget in budgets)


def test_physical_snapshot_any_raises_device_error_at_shared_deadline(monkeypatch):
    def ios(**_kwargs):
        return [_IPHONE]

    def android(**_kwargs):
        time.sleep(1.5)
        return [_PIXEL]

    monkeypatch.setattr(sd.device_claims, "_ios_physical_devices", ios)
    monkeypatch.setattr(sd.device_claims, "_android_physical_devices", android)

    started = time.monotonic()
    with pytest.raises(sd.DeviceError, match="physical device discovery timed out"):
        sd.discover_physical_snapshot("any", timeout=1)

    assert time.monotonic() - started < 1.3


def test_physical_snapshot_any_warns_once_and_keeps_supported_platform(monkeypatch, capsys):
    def ios(**_kwargs):
        raise sd.CapabilityError("ios", "iOS requires Xcode")

    monkeypatch.setattr(sd.device_claims, "_ios_physical_devices", ios)
    monkeypatch.setattr(sd.device_claims, "_android_physical_devices", lambda **_kwargs: [_PIXEL])

    warned: set[str] = set()
    assert sd.discover_physical_snapshot("any", warned=warned) == (_PIXEL,)
    assert sd.discover_physical_snapshot("any", warned=warned) == (_PIXEL,)
    assert capsys.readouterr().err.count("warning: skipping iOS") == 1


def test_physical_snapshot_single_platform_propagates_capability_error(monkeypatch):
    def ios(**_kwargs):
        raise sd.CapabilityError("ios", "iOS requires Xcode")

    monkeypatch.setattr(sd.device_claims, "_ios_physical_devices", ios)

    with pytest.raises(sd.CapabilityError, match="iOS requires Xcode"):
        sd.discover_physical_snapshot("ios")


def test_match_physical_target_filters_supplied_snapshot_without_discovery(monkeypatch):
    target = sd.ConfiguredPhysicalTarget(
        variant="pixel",
        source="recipe",
        catalog_identity="recipe:test:device:pixel",
        spec={"platform": "android", "name": "pixel"},
    )
    monkeypatch.setattr(
        sd.device_claims,
        "discover_physical_snapshot",
        lambda *_args, **_kwargs: pytest.fail("unexpected discovery"),
    )

    destination = sd.match_physical_target(target, (_IPHONE, _PIXEL))

    assert destination == sd.AndroidDestination("Pixel_7", "PXL1234", owned=False)


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

    monkeypatch.setattr(sd.target_commands, "device_run", _fake_run)
    rc = sd.main(["--cwd", str(tmp_path), "run", "simulator"])
    assert rc == 0
    assert captured["info"]["udid"] == "UDID-NEW"
    assert captured["info"]["name"] == sd._default_sim_name(tmp_path, "default")


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

    monkeypatch.setattr(sd.target_commands, "device_run", _fake_run)
    sd.main(["--cwd", str(tmp_path), "run", "simulator", "small-screen"])
    assert captured["info"]["name"] == sd._default_sim_name(tmp_path, "small-screen")


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

    monkeypatch.setattr(sd.target_commands, "device_run", fake_run)
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
    monkeypatch.setattr(sd.target_commands, "device_status", lambda dtype, name: "absent")
    rc = sd.main(["--cwd", str(tmp_path), "target"])
    assert rc == 0


def test_cli_stop_uses_registered_identifier(tmp_path, monkeypatch):
    (tmp_path / "splashdown.toml").write_text("""
[targets.simulator.default]
model = "iPhone 17"

[project]
framework = "react-native"
""")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    registry = sd.Registry()
    registry.set_device(
        str(tmp_path.resolve()), "simulator", "default", "UDID-STORED", "iPhone 17", "18.5"
    )
    captured = []
    monkeypatch.setattr(sd.target_commands, "device_shutdown_row", captured.append)
    rc = sd.main(["--cwd", str(tmp_path), "stop", "simulator"])
    assert rc == 0
    assert captured[0].identifier == "UDID-STORED"


def test_cli_stop_without_registry_row_does_not_resolve_external_name(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / "splashdown.toml").write_text('[targets.emulator.default]\nname = "unowned-avd"\n')
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(
        sd.target_commands, "device_shutdown_row", lambda row: pytest.fail(f"shut down {row}")
    )

    assert sd.main(["--cwd", str(tmp_path), "stop", "emulator"]) == 0
    assert "no managed instance" in capsys.readouterr().err


def test_cli_destroy_uses_registered_emulator_name(tmp_path, monkeypatch):
    (tmp_path / "splashdown.toml").write_text(
        '[targets.emulator.default]\nname = "renamed-in-recipe"\n'
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    registry = sd.Registry()
    registry.set_device(
        str(tmp_path.resolve()),
        "emulator",
        "default",
        "stored-avd",
        "pixel_9",
        "android-34",
    )
    destroyed = []
    monkeypatch.setattr(sd.target_commands, "device_destroy_row", destroyed.append)

    assert sd.main(["--cwd", str(tmp_path), "destroy", "emulator", "--yes"]) == 0
    assert isinstance(destroyed[0], sd.EmulatorRecord)
    assert destroyed[0].name == "stored-avd"


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

    monkeypatch.setattr(sd.target_commands, "device_run", _fake_run)
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
    rc = sd.main(["--cwd", str(tmp_path)])
    assert rc == 0
    capsys.readouterr()  # discard provision output
    rc = sd.main(["--cwd", str(tmp_path), "status"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "MY_PORT  [" in err
    # The state tag must be one of `[in use]` or `[free]` (port-typed resource).
    assert "[free]" in err or "[in use]" in err

    assert sd.main(["--cwd", str(tmp_path), "--show-values", "status"]) == 0
    assert "MY_PORT=" in capsys.readouterr().err


def test_cli_status_local_positional_matches_bare(tmp_path, monkeypatch, capsys):
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
    resource = next(resource for resource in data["resources"] if resource["key"] == "J_PORT")
    assert "value" not in resource
    assert "targets" in data

    assert sd.main(["--cwd", str(tmp_path), "--format", "json", "--show-values", "status"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert next(resource for resource in shown["resources"] if resource["key"] == "J_PORT")[
        "value"
    ].isdigit()


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
    _write_physical_recipe(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _stub_physical(monkeypatch)
    rc = sd.main(["--cwd", str(tmp_path), "--format", "json", "status", "local", "--check"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    device_entries = [t for t in data["targets"] if t["type"] == "device"]
    assert device_entries[0]["status"] == "absent"
    assert device_entries[0]["missing"] is True


def test_cli_status_all_on_empty_registry_renders_only_cwd(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    rc = sd.main(["--cwd", str(tmp_path), "status", "all"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "PATH" in err
    assert "SUMMARY" in err


def test_cli_init_loader_override_writes_devbox_wiring(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    rc = sd.main(["--cwd", str(tmp_path), "init", "minimal", "--loader=devbox"])
    assert rc == 0
    assert (tmp_path / "devbox.json").exists()
    assert not (tmp_path / "mise.toml").exists()


def test_cli_device_prune_rejects_invalid_platform(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    called = {"prune": False}

    def _fail(*a, **kw):
        called["prune"] = True
        return 0

    monkeypatch.setattr(sd.target_commands, "cmd_target_prune", _fail)
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
    assert "P_VERBOSE  [" in err
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
    monkeypatch.setattr(sd.devices, "device_status", lambda dt, name: "absent")
    monkeypatch.setattr(sd.status, "device_status", lambda dt, name: "absent")
    capsys.readouterr()
    rc = sd.main(["--cwd", str(a), "status", "all", "--check"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "orphan" in err
    assert "orphan device" in err
    # Orphans require `splash target refresh`; `gc` leaves rows for live checkouts untouched.
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
    assert rc == 1


def test_cli_env_list_and_get(tmp_path, monkeypatch, capsys):
    (tmp_path / "splashdown.toml").write_text(
        '[resources.PORT]\ntype = "port"\nrange = [19700, 19710]\n'
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    sd.main(["--cwd", str(tmp_path)])
    capsys.readouterr()
    assert sd.main(["--cwd", str(tmp_path), "env", "get", "PORT"]) == 0
    assert capsys.readouterr().out.strip().isdigit()
    assert sd.main(["--cwd", str(tmp_path), "env"]) == 0
    assert capsys.readouterr().out.strip() == "PORT"
    assert sd.main(["--cwd", str(tmp_path), "--show-values", "env"]) == 0
    assert "PORT=" in capsys.readouterr().out
    assert sd.main(["--cwd", str(tmp_path), "--format", "json", "env"]) == 0
    assert json.loads(capsys.readouterr().out) == ["PORT"]
    assert sd.main(["--cwd", str(tmp_path), "--format", "json", "--show-values", "env"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert list(shown) == ["PORT"]
    assert shown["PORT"].isdigit()


def test_cli_env_set(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / sd.RECIPE_NAME).write_text('[resources.K]\ntype = "set"\n')
    assert sd.main(["--cwd", str(tmp_path), "env", "set", "K=v1"]) == 0
    assert capsys.readouterr().err.strip() == "set K"
    assert sd.main(["--cwd", str(tmp_path), "env", "get", "K"]) == 0
    assert capsys.readouterr().out.strip() == "v1"


def test_cli_env_set_rejects_undeclared_key_when_recipe_present(tmp_path, monkeypatch, capsys):
    """A key the recipe doesn't declare would be silently dropped by the next
    `splash gc` reconcile — reject it up front instead of losing the value."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / sd.RECIPE_NAME).write_text('[resources.KNOWN]\ntype = "set"\n')
    assert sd.main(["--cwd", str(tmp_path), "env", "set", "UNKNOWN=x"]) == 2
    assert "not a resource" in capsys.readouterr().err
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


@pytest.mark.parametrize("selector_before_action", [True, False])
def test_cli_env_set_release_honor_checkout(tmp_path, monkeypatch, capsys, selector_before_action):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    other = tmp_path / "other"
    other.mkdir()
    (other / sd.RECIPE_NAME).write_text('[resources.K]\ntype = "set"\n')

    def env_args(action, *values):
        selector = ["--checkout", str(other)]
        nested = [action, *values]
        nested = [*selector, *nested] if selector_before_action else [*nested, *selector]
        return ["--cwd", str(tmp_path), "env", *nested]

    assert sd.main(env_args("set", "K=v1")) == 0
    capsys.readouterr()
    assert sd.main(["--cwd", str(tmp_path), "env", "--checkout", str(other)]) == 0
    assert capsys.readouterr().out.strip() == "K"
    assert sd.main(env_args("get", "K")) == 0
    assert capsys.readouterr().out.strip() == "v1"
    assert sd.main(["--cwd", str(tmp_path), "env", "get", "K"]) == 1
    assert sd.main(env_args("release", "K")) == 0
    assert sd.main(env_args("get", "K")) == 1


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
    monkeypatch.setattr(sd.capabilities.sys, "platform", "darwin")

    monkeypatch.setattr(sd.device_android, "_android_bin", lambda name: name)
    monkeypatch.setattr(
        sd.devices.subprocess,
        "run",
        lambda *call_args, **kwargs: subprocess.CompletedProcess(
            call_args[0], 1, "", "delete failed"
        ),
    )
    with pytest.raises(sd.DeviceError, match=message):
        destroy(*destroy_args)


def test_ios_status_requires_macos_before_launch(monkeypatch):
    def fail_if_launched(*args, **kwargs):
        raise AssertionError(f"unexpected subprocess launch: {args}")

    monkeypatch.setattr(sd.capabilities.sys, "platform", "linux")
    monkeypatch.setattr(sd.devices.subprocess, "check_output", fail_if_launched)

    with pytest.raises(
        sd.CapabilityError, match="iOS simulator support requires macOS and Xcode"
    ) as raised:
        sd.device_status("simulator", "checkout/default")

    assert raised.value.capability == "ios"


def test_cli_ios_start_reports_unsupported_platform(tmp_path, monkeypatch, capsys):
    (tmp_path / "splashdown.toml").write_text(
        '[targets.simulator.default]\nmodel = "iPhone 17"\n[project]\nframework = "react-native"\n'
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(sd.capabilities.sys, "platform", "linux")

    rc = sd.main(["--cwd", str(tmp_path), "start", "simulator"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "iOS simulator support requires macOS and Xcode" in err
    assert "Traceback" not in err


def test_android_home_missing_is_capability_error(tmp_path, monkeypatch):
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.setattr(sd.device_android.Path, "home", lambda: tmp_path)

    with pytest.raises(sd.CapabilityError, match="ANDROID_HOME") as raised:
        sd._android_home()

    assert raised.value.capability == "android"


def test_ios_tool_permission_error_is_capability_error(monkeypatch):
    monkeypatch.setattr(sd.capabilities.sys, "platform", "darwin")

    monkeypatch.setattr(
        sd.devices.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("denied")),
    )

    with pytest.raises(sd.CapabilityError, match="xcrun is unavailable") as raised:
        sd.ios_destroy("UDID")

    assert raised.value.capability == "ios"


def test_android_tool_permission_error_is_capability_error(monkeypatch):
    monkeypatch.setattr(sd.device_android, "_android_bin", lambda name: name)
    monkeypatch.setattr(
        sd.devices.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("denied")),
    )

    with pytest.raises(sd.CapabilityError, match="avdmanager is unavailable") as raised:
        sd.android_destroy("avd")

    assert raised.value.capability == "android"


def test_ios_discovery_timeout_is_device_error(monkeypatch):
    monkeypatch.setattr(sd.capabilities.sys, "platform", "darwin")

    def timeout(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(sd.device_tools.subprocess, "check_output", timeout)

    with pytest.raises(sd.DeviceError, match="xcrun simctl list devices -j timed out after 30s"):
        sd.device_ios._xcrun_json(["simctl", "list", "devices", "-j"])


def test_android_mutation_timeout_is_device_error(monkeypatch):
    monkeypatch.setattr(sd.device_android, "_android_bin", lambda name: name)

    def timeout(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(sd.device_tools.subprocess, "run", timeout)

    with pytest.raises(sd.DeviceError, match="avdmanager delete timed out after 120s"):
        sd.device_android.android_destroy("demo")


def test_android_boot_writes_log_under_supplied_state_directory(tmp_path, monkeypatch):
    serials = iter((None, "emulator-5554"))
    monkeypatch.setattr(
        sd.device_android,
        "_android_running_serial",
        lambda _name, **_kwargs: next(serials),
    )
    monkeypatch.setattr(sd.device_android, "_android_bin", lambda name: name)
    monkeypatch.setattr(sd.device_android.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(sd.device_android.subprocess, "Popen", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        sd.device_android,
        "check_output_finite",
        lambda argv, **_kwargs: (
            b"1\n" if "sys.boot_completed" in argv else b"Service package: found\n"
        ),
    )

    state_dir = tmp_path / "state" / "splashdown"
    assert sd.device_android.android_boot("demo", state_dir=state_dir) == "emulator-5554"
    assert (state_dir / "emulator-demo.log").exists()


def test_android_boot_waits_for_system_and_package_service(monkeypatch):
    serial = "emulator-5554"
    discoveries: list[str] = []

    def running(name, **_kwargs):
        discoveries.append(name)
        return serial

    monkeypatch.setattr(sd.device_android, "_android_running_serial", running)
    monkeypatch.setattr(sd.device_android, "_android_bin", lambda name: name)
    responses = iter(
        (
            b"0\n",
            b"1\n",
            b"Service package: not found\n",
            b"1\n",
            b"Service package: found\n",
        )
    )
    commands: list[list[str]] = []

    def output(argv, **_kwargs):
        commands.append(argv)
        return next(responses)

    sleeps: list[int] = []
    monkeypatch.setattr(sd.device_android, "check_output_finite", output)
    monkeypatch.setattr(sd.device_android.time, "sleep", sleeps.append)

    assert sd.device_android.android_boot("demo") == serial
    assert commands == [
        ["adb", "-s", serial, "shell", "getprop", "sys.boot_completed"],
        ["adb", "-s", serial, "shell", "getprop", "sys.boot_completed"],
        ["adb", "-s", serial, "shell", "service", "check", "package"],
        ["adb", "-s", serial, "shell", "getprop", "sys.boot_completed"],
        ["adb", "-s", serial, "shell", "service", "check", "package"],
    ]
    assert discoveries == ["demo"]
    assert sleeps == [1, 1]


def test_android_boot_ready_preserves_probe_failure(monkeypatch):
    monkeypatch.setattr(sd.device_android, "_android_bin", lambda name: name)

    def fail(*_args, **_kwargs):
        raise sd.DeviceError("adb read boot status timed out after 2s")

    monkeypatch.setattr(sd.device_android, "check_output_finite", fail)

    with pytest.raises(sd.DeviceError, match="adb read boot status timed out after 2s"):
        sd.device_android._android_boot_ready("emulator-5554")


def test_android_boot_rediscovers_after_transport_error_without_respawning(monkeypatch):
    serial = "emulator-5554"
    discoveries = []

    def running(name, **_kwargs):
        discoveries.append(name)
        return serial

    readiness_checks = []

    def ready(found_serial, **_kwargs):
        readiness_checks.append(found_serial)
        if len(readiness_checks) == 1:
            raise subprocess.CalledProcessError(1, ["adb", "shell"])
        return True

    monkeypatch.setattr(sd.device_android, "_android_running_serial", running)
    monkeypatch.setattr(sd.device_android, "_android_boot_ready", ready)
    monkeypatch.setattr(sd.device_android.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        sd.device_android.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("an existing AVD must not be started again"),
    )

    assert sd.device_android.android_boot("demo") == serial
    assert discoveries == ["demo", "demo"]
    assert readiness_checks == [serial, serial]


def test_android_boot_timeout_is_wall_clock_bounded_for_existing_avd(monkeypatch):
    serial = "emulator-5554"
    clock = [0.0]

    def running(_name, **kwargs):
        clock[0] += min(30, kwargs.get("timeout", 30))
        return serial

    def ready(_serial, **kwargs):
        clock[0] += min(4, kwargs.get("timeout", 4))
        return False

    monkeypatch.setattr(sd.device_android, "_android_running_serial", running)
    monkeypatch.setattr(sd.device_android, "_android_boot_ready", ready)
    monkeypatch.setattr(sd.device_android.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        sd.device_android.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    monkeypatch.setattr(
        sd.device_android.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("an existing AVD must not be started again"),
    )

    with pytest.raises(sd.DeviceError, match="did not become ready within 60s") as error:
        sd.device_android.android_boot("demo")

    assert clock[0] <= 60
    assert "; see " not in str(error.value)


def test_android_boot_timeout_references_log_for_spawned_avd(tmp_path, monkeypatch):
    clock = [0.0]

    def running(_name, **kwargs):
        clock[0] += min(30, kwargs.get("timeout", 30))

    monkeypatch.setattr(sd.device_android, "_android_running_serial", running)
    monkeypatch.setattr(sd.device_android, "_android_bin", lambda name: name)
    monkeypatch.setattr(sd.device_android.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        sd.device_android.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    monkeypatch.setattr(sd.device_android.subprocess, "Popen", lambda *args, **kwargs: None)
    state_dir = tmp_path / "state"

    with pytest.raises(sd.DeviceError, match="did not become ready within 60s") as error:
        sd.device_android.android_boot("demo", state_dir=state_dir)

    log = state_dir / "emulator-demo.log"
    assert log.exists()
    assert str(error.value).endswith(f"; see {log}")


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
    monkeypatch.setattr(sd.target_commands, "device_status", lambda dtype, name: "absent")
    rc = sd.main(["--cwd", str(tmp_path), "target"])
    assert rc == 0


def test_cli_device_remove_without_registry_row_is_safe_noop(tmp_path, monkeypatch, capsys):
    (tmp_path / "splashdown.toml").write_text('[project]\nframework = "react-native"\n')
    (tmp_path / "splashdown.local.toml").write_text(
        '[targets.simulator.repro]\nmodel = "iPhone 17"\n'
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(
        sd.target_commands, "device_destroy_row", lambda row: pytest.fail(f"destroyed {row}")
    )
    rc = sd.main(["--cwd", str(tmp_path), "target", "remove", "simulator", "repro"])
    assert rc == 0
    assert "no managed instance found" in capsys.readouterr().err
    assert "[targets.simulator.repro]" not in (tmp_path / "splashdown.local.toml").read_text()


def test_cli_device_remove_keep_instance_skips_destroy(tmp_path, monkeypatch):
    (tmp_path / "splashdown.toml").write_text('[project]\nframework = "react-native"\n')
    (tmp_path / "splashdown.local.toml").write_text(
        '[targets.simulator.repro]\nmodel = "iPhone 17"\n'
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    destroyed = []
    monkeypatch.setattr(sd.target_commands, "device_destroy_row", destroyed.append)
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
    destroyed = []
    monkeypatch.setattr(sd.target_commands, "device_destroy_row", destroyed.append)
    rc = sd.main(["--cwd", str(tmp_path), "target", "remove", "simulator", "default"])
    assert rc == 1
    assert destroyed == []
    assert "[targets.simulator.default]" in (tmp_path / "splashdown.toml").read_text()


def test_cli_device_remove_refuses_symlink_before_destroy(tmp_path, monkeypatch, capsys):
    (tmp_path / "splashdown.toml").write_text("")
    outside = tmp_path.parent / f"{tmp_path.name}-outside-remove.toml"
    original = '[targets.simulator.repro]\nmodel = "iPhone 17"\n'
    outside.write_text(original)
    (tmp_path / sd.LOCAL_NAME).symlink_to(outside)
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
    destroyed = []
    monkeypatch.setattr(sd.target_commands, "device_destroy_row", destroyed.append)

    rc = sd.main(["--cwd", str(tmp_path), "target", "remove", "simulator", "repro"])

    assert rc == 1
    assert destroyed == []
    assert outside.read_text() == original
    assert registry.get_device(str(tmp_path.resolve()), "simulator", "repro") is not None
    assert "symlink" in capsys.readouterr().err


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

    monkeypatch.setattr(sd.target_commands, "device_destroy_row", fail_destroy)
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
    monkeypatch.setattr(sd.target_commands, "device_destroy_row", destroyed.append)

    rc = sd.main(["--cwd", str(tmp_path), "target", "remove", "simulator", "repro"])

    assert rc == 0
    assert destroyed[0].udid == "UDID-OLD"
    assert registry.get_device(str(tmp_path.resolve()), "simulator", "repro") is None
    assert "[targets.simulator.repro]" not in local.read_text()


def test_cli_target_remove_uses_composition_root_registry(tmp_path, monkeypatch):
    (tmp_path / "splashdown.toml").write_text("")
    (tmp_path / "splashdown.local.toml").write_text(
        '[targets.simulator.repro]\nmodel = "iPhone 17"\n'
    )
    state = tmp_path / "injected"
    registry = sd.Registry(
        state / "ports.tsv",
        state / "kv.tsv",
        state / "devices.tsv",
    )
    checkout = str(tmp_path.resolve())
    registry.set_device(checkout, "simulator", "repro", "UDID", "iPhone 17", "18.5")
    monkeypatch.setattr(sd.cli, "Registry", lambda: registry)
    monkeypatch.setattr(sd.target_commands, "device_destroy_row", lambda row: None)

    assert sd.main(["--cwd", str(tmp_path), "target", "remove", "simulator", "repro"]) == 0
    assert registry.get_device(checkout, "simulator", "repro") is None


def test_device_gc_drops_defunct_checkouts(registry, tmp_path, monkeypatch):
    a = tmp_path / "gone"
    a.mkdir()
    b = tmp_path / "live"
    b.mkdir()
    registry.set_device(str(a), "simulator", "default", "UDID-A", "iPhone 17", "18.5")
    registry.set_device(str(b), "simulator", "default", "UDID-B", "iPhone 17", "18.5")
    a.rmdir()
    lifecycle = []
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.devices, "ios_shutdown", lambda u: lifecycle.append(("shutdown", u)))
    monkeypatch.setattr(sd.devices, "ios_destroy", lambda u: lifecycle.append(("destroy", u)))
    destroyed_count = sd.cmd_target_gc(registry)
    assert destroyed_count == 1
    assert lifecycle == [("shutdown", "UDID-A"), ("destroy", "UDID-A")]
    assert {r.udid for r in registry.all_devices()} == {"UDID-B"}


def test_device_gc_rereads_row_after_checkout_lock(registry, tmp_path, monkeypatch):
    from contextlib import contextmanager

    gone = tmp_path / "gone"
    registry.set_device(str(gone), "simulator", "default", "UDID-OLD", "iPhone 17", "18.5")
    destroyed = []

    @contextmanager
    def replace_before_entry(_target):
        registry.set_device(str(gone), "simulator", "default", "UDID-NEW", "iPhone 17", "18.5")
        yield

    monkeypatch.setattr(registry, "operation_lock", replace_before_entry)
    monkeypatch.setattr(
        sd.target_commands, "device_destroy_row", lambda row: destroyed.append(row.udid)
    )

    assert sd.cmd_target_gc(registry) == 1
    assert destroyed == ["UDID-NEW"]


def test_device_gc_drops_defunct_emulator_and_destroys_avd(registry, tmp_path, monkeypatch):
    gone = tmp_path / "gone"
    gone.mkdir()
    registry.set_device(str(gone), "emulator", "default", "AVD-GONE", "pixel_9", "android-34")
    gone.rmdir()
    lifecycle: list[tuple[str, str]] = []
    monkeypatch.setattr(sd.devices, "_android_avd_exists", lambda n: True)
    monkeypatch.setattr(sd.devices, "android_shutdown", lambda n: lifecycle.append(("shutdown", n)))
    monkeypatch.setattr(sd.devices, "android_destroy", lambda n: lifecycle.append(("destroy", n)))
    assert sd.cmd_target_gc(registry) == 1
    assert lifecycle == [("shutdown", "AVD-GONE"), ("destroy", "AVD-GONE")]
    assert list(registry.all_devices()) == []


def test_device_gc_drops_live_orphan_without_destroying(registry, checkout, monkeypatch):
    registry.set_device(str(checkout), "simulator", "default", "UDID-GONE", "iPhone 17", "18.5")
    monkeypatch.setattr(sd.target_commands, "_is_orphan_device", lambda row: True)
    monkeypatch.setattr(
        sd.target_commands,
        "device_destroy_row",
        lambda row: pytest.fail(f"orphan row was destroyed: {row}"),
    )

    assert sd.cmd_target_gc(registry) == 1
    assert registry.get_device(str(checkout), "simulator", "default") is None


def test_device_gc_preserves_live_row_when_capability_unavailable(
    registry, checkout, monkeypatch, capsys
):
    registry.set_device(str(checkout), "simulator", "default", "UDID", "iPhone 17", "18.5")
    monkeypatch.setattr(
        sd.target_commands,
        "_is_orphan_device",
        lambda row: (_ for _ in ()).throw(
            sd.CapabilityError("ios", "iOS simulator support requires macOS and Xcode")
        ),
    )

    assert sd.cmd_target_gc(registry) == 0
    assert registry.get_device(str(checkout), "simulator", "default") is not None
    assert "warning: skipping iOS" in capsys.readouterr().err


def test_cli_gc_destroys_orphan_sims_and_prunes_rows(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    dead = tmp_path / "dead"  # a checkout dir that won't exist at gc time
    reg = sd.Registry()
    reg.allocate_port(str(dead), "PORT", 19800, 19810)
    reg.set_device(str(dead), "simulator", "default", "UDID-DEAD", "iPhone 17", "18.5")
    lifecycle = []
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.devices, "ios_shutdown", lambda u: lifecycle.append(("shutdown", u)))
    monkeypatch.setattr(sd.devices, "ios_destroy", lambda u: lifecycle.append(("destroy", u)))
    rc = sd.main(["--cwd", str(tmp_path), "gc"])
    assert rc == 0
    assert lifecycle == [("shutdown", "UDID-DEAD"), ("destroy", "UDID-DEAD")]
    assert reg.get_device(str(dead), "simulator", "default") is None
    assert str(dead) not in reg.all_checkouts()


def test_gc_preserves_unavailable_device_rows(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    ios = tmp_path / "dead-ios"
    android = tmp_path / "dead-android"
    registry = sd.Registry(state / "ports.tsv", state / "kv.tsv", state / "devices.tsv")
    registry.allocate_port(str(ios), "PORT", 19820, 19830)
    registry.set_kv(str(android), "TOKEN", "value")
    registry.set_device(str(ios), "simulator", "default", "UDID-DEAD", "iPhone 17", "18.5")
    registry.set_device(str(android), "emulator", "default", "AVD-DEAD", "pixel_9", "android-34")
    destroyed: list[str] = []

    def destroy(row):
        if row.dtype == "simulator":
            raise sd.CapabilityError("ios", "iOS simulator support requires macOS and Xcode")
        destroyed.append(row.udid)

    monkeypatch.setattr(sd.target_commands, "device_destroy_row", destroy)

    assert sd.cmd_gc(registry) == 0
    assert destroyed == ["AVD-DEAD"]
    assert registry.get_device(str(ios), "simulator", "default") is not None
    assert registry.get_device(str(android), "emulator", "default") is None
    assert registry.all_for(str(ios)) == {}
    assert registry.all_for(str(android)) == {}
    assert "warning: skipping iOS" in capsys.readouterr().err


def test_device_refresh_recreates_stale_latest(registry, tmp_path, monkeypatch):
    checkout = tmp_path / "co"
    checkout.mkdir()
    (checkout / "splashdown.toml").write_text('[targets.simulator.default]\nmodel = "iPhone 17"\n')
    abspath = str(checkout.resolve())
    registry.set_device(abspath, "simulator", "default", "UDID-OLD", "iPhone 17", "17.5")
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.devices, "_ios_latest_runtime_version", lambda: "18.5")
    lifecycle = []
    monkeypatch.setattr(sd.devices, "ios_shutdown", lambda u: lifecycle.append(("shutdown", u)))
    monkeypatch.setattr(sd.devices, "ios_destroy", lambda u: lifecycle.append(("destroy", u)))
    monkeypatch.setattr(sd.devices, "ios_ensure", lambda n, m, i: ("UDID-NEW", "Shutdown"))
    monkeypatch.setattr(
        sd.devices, "ios_boot", lambda *a, **k: pytest.fail("refresh must not boot")
    )
    monkeypatch.setattr(
        sd.target_commands, "ios_boot", lambda *a, **k: pytest.fail("refresh must not boot")
    )
    rc = sd.cmd_target_refresh(registry)
    assert rc == 0
    assert lifecycle == [("shutdown", "UDID-OLD"), ("destroy", "UDID-OLD")]
    assert registry.get_device(abspath, "simulator", "default").udid == "UDID-NEW"


def test_device_refresh_rereads_row_after_checkout_lock(registry, tmp_path, monkeypatch):
    from contextlib import contextmanager

    gone = tmp_path / "gone"
    registry.set_device(str(gone), "simulator", "default", "UDID-OLD", "iPhone 17", "18.5")
    destroyed = []

    @contextmanager
    def replace_before_entry(_target):
        registry.set_device(str(gone), "simulator", "default", "UDID-NEW", "iPhone 17", "18.5")
        yield

    monkeypatch.setattr(registry, "operation_lock", replace_before_entry)
    monkeypatch.setattr(
        sd.target_commands, "device_destroy_row", lambda row: destroyed.append(row.udid)
    )

    assert sd.cmd_target_refresh(registry, platforms=("ios",)) == 0
    assert destroyed == ["UDID-NEW"]


def test_device_refresh_skips_unavailable_platform(registry, tmp_path, monkeypatch, capsys):
    ios = tmp_path / "ios"
    android = tmp_path / "android"
    ios.mkdir()
    android.mkdir()
    (ios / sd.RECIPE_NAME).write_text(
        '[targets.simulator.default]\nmodel = "iPhone 17"\nios = "18.5"\n'
    )
    (android / sd.RECIPE_NAME).write_text(
        '[targets.emulator.default]\ndevice = "pixel_9"\nimage = "android-34"\n'
    )
    registry.set_device(str(ios), "simulator", "default", "UDID", "iPhone 17", "18.5")
    registry.set_device(str(android), "emulator", "default", "AVD", "pixel_9", "android-34")
    reconciled: list[str] = []

    def needs_recreate(reg, cwd, dtype, variant, spec, *, cache):
        if dtype == "simulator":
            raise sd.CapabilityError("ios", "iOS simulator support requires macOS and Xcode")
        return True

    monkeypatch.setattr(sd.target_commands, "device_needs_recreate", needs_recreate)
    monkeypatch.setattr(
        sd.target_commands,
        "ensure_fresh_sim",
        lambda reg, cwd, dtype, variant, spec, *, cache: reconciled.append(f"{dtype}/{variant}"),
    )

    assert sd.cmd_target_refresh(registry, skip_unavailable=True) == 0
    assert reconciled == ["emulator/default"]
    assert registry.get_device(str(ios), "simulator", "default") is not None
    assert "warning: skipping iOS" in capsys.readouterr().err


def test_device_refresh_explicit_unavailable_platform_fails(registry, checkout, monkeypatch):
    (checkout / sd.RECIPE_NAME).write_text(
        '[targets.simulator.default]\nmodel = "iPhone 17"\nios = "18.5"\n'
    )
    registry.set_device(str(checkout), "simulator", "default", "UDID", "iPhone 17", "18.5")
    monkeypatch.setattr(
        sd.target_commands,
        "device_needs_recreate",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            sd.CapabilityError("ios", "iOS simulator support requires macOS and Xcode")
        ),
    )

    with pytest.raises(sd.CapabilityError):
        sd.cmd_target_refresh(registry, platforms=("ios",), skip_unavailable=False)


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
    monkeypatch.setattr(sd.devices, "_ios_latest_runtime_version", lambda: "18.5")
    destroyed = []
    monkeypatch.setattr(sd.devices, "ios_destroy", destroyed.append)
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
    monkeypatch.setattr(
        sd.devices,
        "_android_latest_image",
        lambda: "system-images;android-34;google_apis;arm64-v8a",
    )
    monkeypatch.setattr(sd.devices, "android_destroy", touched.append)
    monkeypatch.setattr(sd.devices, "android_ensure", lambda *a: touched.append("ensure"))
    rc = sd.cmd_target_refresh(registry, platforms=("ios",))
    assert rc == 0
    assert touched == []
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
    lifecycle = []
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.devices, "ios_shutdown", lambda u: lifecycle.append(("shutdown", u)))
    monkeypatch.setattr(sd.devices, "ios_destroy", lambda u: lifecycle.append(("destroy", u)))
    monkeypatch.setattr(
        sd.devices, "ios_ensure", lambda *a: pytest.fail("nothing should be recreated")
    )
    rc = sd.cmd_target_refresh(registry)
    assert rc == 0
    assert lifecycle == [
        ("shutdown", "UDID-GONE"),
        ("destroy", "UDID-GONE"),
        ("shutdown", "UDID-UNDECLARED"),
        ("destroy", "UDID-UNDECLARED"),
    ]
    assert list(registry.all_devices()) == []


def test_cli_target_refresh_uses_composition_root_registry(tmp_path, monkeypatch):
    state = tmp_path / "injected"
    registry = sd.Registry(
        state / "ports.tsv",
        state / "kv.tsv",
        state / "devices.tsv",
    )
    captured = {}

    def fake_refresh(received, *, platforms, skip_unavailable):
        captured["registry"] = received
        captured["platforms"] = platforms
        captured["skip_unavailable"] = skip_unavailable
        return 0

    monkeypatch.setattr(sd.cli, "Registry", lambda: registry)
    monkeypatch.setattr(sd.target_commands, "cmd_target_refresh", fake_refresh)

    assert sd.main(["--cwd", str(tmp_path), "target", "refresh", "android"]) == 0
    assert captured == {
        "registry": registry,
        "platforms": ("android",),
        "skip_unavailable": False,
    }


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
    monkeypatch.setattr(sd.devices, "_ios_latest_runtime_version", lambda: "18.5")
    monkeypatch.setattr(sd.status, "device_status", lambda dt, name: "shutdown")
    capsys.readouterr()
    rc = sd.main(["--cwd", str(co), "status", "--check"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "[stale]" in err
    assert "stale device" in err
    assert "`splash target refresh`" in err


def test_cli_status_check_flags_model_drift(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    co = tmp_path / "co"
    co.mkdir()
    (co / "splashdown.toml").write_text(
        '[targets.simulator.default]\nmodel = "iPhone 18"\nios = "18.5"\n'
    )
    reg = sd.Registry()
    reg.set_device(str(co.resolve()), "simulator", "default", "UDID", "iPhone 17", "18.5")
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda udid: True)
    monkeypatch.setattr(sd.status, "device_status", lambda dtype, name: "shutdown")

    assert sd.main(["--cwd", str(co), "status", "--check"]) == 0

    err = capsys.readouterr().err
    assert "[stale]" in err
    assert "declared target drifted" in err


def test_cli_status_all_check_flags_undeclared_device_row(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    co = tmp_path / "co"
    co.mkdir()
    (co / "splashdown.toml").write_text("")
    reg = sd.Registry()
    reg.set_device(str(co.resolve()), "simulator", "old", "UDID", "iPhone 17", "18.5")
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda udid: True)
    monkeypatch.setattr(sd.status, "_device_status_for_row", lambda row: "shutdown")

    assert sd.main(["--cwd", str(co), "status", "all", "--check"]) == 0

    err = capsys.readouterr().err
    assert "undeclared" in err
    assert "`splash target refresh`" in err


def test_cli_status_check_flags_missing_device(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    co = tmp_path / "co"
    co.mkdir()
    (co / "splashdown.toml").write_text('[targets.simulator.default]\nmodel = "iPhone 17"\n')
    # Declared but never provisioned: no registry row, sim absent.
    monkeypatch.setattr(sd.status, "device_status", lambda dt, name: "absent")
    capsys.readouterr()
    rc = sd.main(["--cwd", str(co), "status", "--check"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "[missing]" in err
    assert "missing device" in err
    assert "`splash run`" in err


def test_status_unavailable_does_not_increment_repair_counters(
    registry, checkout, monkeypatch, capsys
):
    (checkout / sd.RECIPE_NAME).write_text(
        '[targets.simulator.a]\nmodel = "iPhone 17"\n[targets.simulator.b]\nmodel = "iPhone 17"\n'
    )

    def unavailable(*args, **kwargs):
        raise sd.CapabilityError("ios", "iOS simulator support requires macOS and Xcode")

    monkeypatch.setattr(sd.status, "device_status", unavailable)
    monkeypatch.setattr(sd.status, "device_health", unavailable)

    assert sd.cmd_status(checkout, registry, "json", check=True) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert [row["status"] for row in payload["targets"]] == [
        "unavailable",
        "unavailable",
    ]
    assert payload["summary"]["orphan_devices"] == 0
    assert payload["summary"]["stale_devices"] == 0
    assert payload["summary"]["missing_devices"] == 0
    assert captured.err.count("warning: skipping iOS") == 1


def test_status_text_and_target_list_render_unavailable(registry, checkout, monkeypatch, capsys):
    (checkout / sd.RECIPE_NAME).write_text('[targets.simulator.default]\nmodel = "iPhone 17"\n')
    monkeypatch.setattr(
        sd.status,
        "device_status",
        lambda *args: (_ for _ in ()).throw(
            sd.CapabilityError("ios", "iOS simulator support requires macOS and Xcode")
        ),
    )
    monkeypatch.setattr(
        sd.target_commands,
        "device_status",
        lambda *args: (_ for _ in ()).throw(
            sd.CapabilityError("ios", "iOS simulator support requires macOS and Xcode")
        ),
    )

    assert sd.cmd_status(checkout, registry, "text") == 0
    captured = capsys.readouterr()
    assert "simulator.default" in captured.err
    assert "unavailable" in captured.err

    assert sd.cmd_targets_list(checkout, registry, "json") == 0
    assert json.loads(capsys.readouterr().out)[0]["connection"] == "unavailable"


def test_target_inventory_uses_one_snapshot_and_keeps_connection_and_claim_independent(
    registry, checkout, monkeypatch, capsys
):
    (checkout / sd.RECIPE_NAME).write_text(
        """
[targets.device.pixel]
platform = "android"
id = "PXL1234"

[targets.device.xiaomi]
platform = "android"
id = "XIAOMI"

[targets.device.iphone17]
platform = "ios"
id = "00008-PHONE"

[targets.device.pixel-alias]
platform = "android"
id = "PXL1234"

[targets.simulator.ios-dev]
name = "ios-dev"

[targets.emulator.android-dev]
name = "android-dev"
"""
    )
    pixel, _xiaomi, iphone, _alias = sd.configured_physical_targets(checkout)
    pixel_owner = checkout / "feature-pixel"
    iphone_owner = checkout / "feature-iphone"
    pixel_owner.mkdir()
    iphone_owner.mkdir()
    registry.attempt_claim(
        sd.PhysicalClaim(
            pixel.catalog_identity,
            "android",
            "PXL1234",
            "pixel",
            str(pixel_owner.resolve()),
            "2026-08-26T10:00:00+00:00",
        )
    )
    registry.attempt_claim(
        sd.PhysicalClaim(
            iphone.catalog_identity,
            "ios",
            "00008-PHONE",
            "iphone17",
            str(iphone_owner.resolve()),
            "2026-08-26T10:00:00+00:00",
        )
    )
    calls: list[str] = []

    def discover_ios(**_kwargs):
        calls.append("ios")
        return []

    def discover_android(**_kwargs):
        calls.append("android")
        return [_PIXEL, {"id": "XIAOMI", "name": "Xiaomi", "platform": "android"}]

    monkeypatch.setattr(sd.device_claims, "_ios_physical_devices", discover_ios)
    monkeypatch.setattr(sd.device_claims, "_android_physical_devices", discover_android)
    monkeypatch.setattr(
        sd.target_commands,
        "device_status",
        lambda dtype, _name: {"simulator": "shutdown", "emulator": "running"}[dtype],
    )

    assert sd.cmd_targets_list(checkout, registry, "json") == 0

    rows = json.loads(capsys.readouterr().out)
    assert sorted(calls) == ["android", "ios"]
    assert rows == [
        {
            "type": "device",
            "variant": "pixel",
            "source": "recipe",
            "device_name": "Pixel_7",
            "platform": "android",
            "connection": "connected",
            "claim": "claimed",
            "owner": str(pixel_owner.resolve()),
        },
        {
            "type": "device",
            "variant": "xiaomi",
            "source": "recipe",
            "device_name": "Xiaomi",
            "platform": "android",
            "connection": "connected",
            "claim": "free",
            "owner": "",
        },
        {
            "type": "device",
            "variant": "iphone17",
            "source": "recipe",
            "device_name": "00008-PHONE",
            "platform": "ios",
            "connection": "disconnected",
            "claim": "claimed",
            "owner": str(iphone_owner.resolve()),
        },
        {
            "type": "device",
            "variant": "pixel-alias",
            "source": "recipe",
            "device_name": "Pixel_7",
            "platform": "android",
            "connection": "connected",
            "claim": "claimed",
            "owner": str(pixel_owner.resolve()),
        },
        {
            "type": "simulator",
            "variant": "ios-dev",
            "source": "recipe",
            "device_name": "ios-dev",
            "platform": "ios",
            "connection": "shutdown",
            "claim": "not-applicable",
            "owner": "",
        },
        {
            "type": "emulator",
            "variant": "android-dev",
            "source": "recipe",
            "device_name": "android-dev",
            "platform": "android",
            "connection": "running",
            "claim": "not-applicable",
            "owner": "",
        },
    ]


def test_target_inventory_discovers_platforms_concurrently_under_shared_budget(
    registry, checkout, monkeypatch, capsys
):
    (checkout / sd.RECIPE_NAME).write_text(
        '[targets.device.iphone]\nplatform = "ios"\n[targets.device.pixel]\nplatform = "android"\n'
    )
    ios_started = threading.Event()
    android_started = threading.Event()
    budgets: dict[str, float] = {}

    def ios(*, timeout):
        budgets["ios"] = timeout
        ios_started.set()
        assert android_started.wait(0.5)
        return [_IPHONE]

    def android(*, timeout):
        budgets["android"] = timeout
        android_started.set()
        assert ios_started.wait(0.5)
        return [_PIXEL]

    monkeypatch.setattr(sd.device_claims, "_ios_physical_devices", ios)
    monkeypatch.setattr(sd.device_claims, "_android_physical_devices", android)

    started = time.monotonic()
    assert sd.cmd_targets_list(checkout, registry, "json") == 0
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert set(budgets) == {"ios", "android"}
    assert all(29.9 < budget <= 30 for budget in budgets.values())
    assert [row["connection"] for row in json.loads(capsys.readouterr().out)] == [
        "connected",
        "connected",
    ]


def test_target_inventory_keeps_claims_when_connection_is_ambiguous_or_unavailable(
    registry, checkout, monkeypatch, capsys
):
    (checkout / sd.RECIPE_NAME).write_text(
        """
[targets.device.ambiguous]
platform = "android"

[targets.device.iphone]
platform = "ios"
id = "00008-PHONE"
"""
    )
    _ambiguous, iphone = sd.configured_physical_targets(checkout)
    owner = checkout / "feature-iphone"
    owner.mkdir()
    registry.attempt_claim(
        sd.PhysicalClaim(
            iphone.catalog_identity,
            "ios",
            "00008-PHONE",
            "iphone",
            str(owner.resolve()),
            "2026-08-26T10:00:00+00:00",
        )
    )
    monkeypatch.setattr(
        sd.device_claims,
        "_ios_physical_devices",
        lambda **_kwargs: (_ for _ in ()).throw(
            sd.CapabilityError("ios", "iOS physical-device support requires macOS and Xcode")
        ),
    )
    monkeypatch.setattr(
        sd.device_claims,
        "_android_physical_devices",
        lambda **_kwargs: [_PIXEL, {"id": "OTHER", "name": "Other", "platform": "android"}],
    )

    assert sd.cmd_targets_list(checkout, registry, "json") == 0

    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["connection"] == "ambiguous"
    assert rows[0]["claim"] == "free"
    assert rows[1]["connection"] == "unavailable"
    assert rows[1]["claim"] == "claimed"
    assert rows[1]["owner"] == str(owner.resolve())


def test_target_inventory_marks_platform_agnostic_target_unavailable_for_partial_snapshot(
    registry, checkout, monkeypatch, capsys
):
    (checkout / sd.RECIPE_NAME).write_text('[targets.device.pixel]\nid = "PXL1234"\n')
    target = sd.configured_physical_targets(checkout)[0]
    owner = checkout / "feature-pixel"
    owner.mkdir()
    registry.attempt_claim(
        sd.PhysicalClaim(
            target.catalog_identity,
            "android",
            "PXL1234",
            "pixel",
            str(owner.resolve()),
            "2026-08-26T10:00:00+00:00",
        )
    )

    monkeypatch.setattr(
        sd.device_claims,
        "_ios_physical_devices",
        lambda **_kwargs: (_ for _ in ()).throw(
            sd.CapabilityError("ios", "iOS physical-device support requires macOS and Xcode")
        ),
    )
    monkeypatch.setattr(sd.device_claims, "_android_physical_devices", lambda **_kwargs: [_PIXEL])

    assert sd.cmd_targets_list(checkout, registry, "json") == 0

    row = json.loads(capsys.readouterr().out)[0]
    assert row["connection"] == "unavailable"
    assert row["claim"] == "claimed"
    assert row["owner"] == str(owner.resolve())


def test_cli_target_empty_catalog_json_is_valid(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    assert sd.main(["--cwd", str(tmp_path), "--format", "json", "target"]) == 0
    assert json.loads(capsys.readouterr().out) == []


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
    monkeypatch.setattr(sd.device_ios, "_xcrun_json", lambda args: {"devices": fake_devices})
    monkeypatch.setattr(sd.target_commands, "_xcrun_json", lambda args: {"devices": fake_devices})
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
    monkeypatch.setattr(sd.device_ios, "_xcrun_json", lambda args: {"devices": fake_devices})
    monkeypatch.setattr(sd.target_commands, "_xcrun_json", lambda args: {"devices": fake_devices})
    destroyed: list[str] = []
    shut: list[str] = []
    monkeypatch.setattr(sd.devices, "ios_destroy", destroyed.append)
    monkeypatch.setattr(sd.target_commands, "ios_destroy", destroyed.append)
    monkeypatch.setattr(sd.devices, "ios_shutdown", shut.append)
    monkeypatch.setattr(sd.target_commands, "ios_shutdown", shut.append)
    rc = sd.cmd_target_prune(registry, yes=True, dry_run=False, platforms=("ios",))
    assert rc == 0
    assert destroyed == ["FOREIGN"]
    assert shut == ["FOREIGN"]


def test_device_prune_noop_when_nothing_unmanaged(registry, monkeypatch, capsys):
    monkeypatch.setattr(sd.device_ios, "_xcrun_json", lambda args: {"devices": {}})
    monkeypatch.setattr(sd.target_commands, "_xcrun_json", lambda args: {"devices": {}})
    rc = sd.cmd_target_prune(registry, yes=True, dry_run=False, platforms=("ios",))
    assert rc == 0
    assert "nothing" in capsys.readouterr().err.lower()


def test_device_prune_skips_unavailable_platform(registry, monkeypatch, capsys):
    monkeypatch.setattr(
        sd.target_commands,
        "_discover_foreign_ios",
        lambda managed: (_ for _ in ()).throw(
            sd.CapabilityError("ios", "iOS simulator support requires macOS and Xcode")
        ),
    )
    monkeypatch.setattr(
        sd.target_commands, "_discover_foreign_avds", lambda managed: ["foreign-avd"]
    )
    lifecycle: list[tuple[str, str]] = []
    monkeypatch.setattr(
        sd.target_commands,
        "android_shutdown",
        lambda name: lifecycle.append(("shutdown", name)),
    )
    monkeypatch.setattr(
        sd.target_commands,
        "android_destroy",
        lambda name: lifecycle.append(("destroy", name)),
    )

    assert sd.cmd_target_prune(registry, yes=True, skip_unavailable=True) == 0
    assert lifecycle == [("shutdown", "foreign-avd"), ("destroy", "foreign-avd")]
    assert "warning: skipping iOS" in capsys.readouterr().err


def test_device_prune_explicit_unavailable_platform_fails(registry, monkeypatch):
    monkeypatch.setattr(
        sd.target_commands,
        "_discover_foreign_ios",
        lambda managed: (_ for _ in ()).throw(
            sd.CapabilityError("ios", "iOS simulator support requires macOS and Xcode")
        ),
    )

    with pytest.raises(sd.CapabilityError):
        sd.cmd_target_prune(
            registry,
            yes=True,
            platforms=("ios",),
            skip_unavailable=False,
        )


def test_cli_device_prune_platform_positional_ios(tmp_path, monkeypatch):
    """`splash target prune ios` passes only the iOS platform."""
    state = tmp_path / "injected"
    registry = sd.Registry(
        state / "ports.tsv",
        state / "kv.tsv",
        state / "devices.tsv",
    )
    captured = {}

    def _fake_prune(reg, *, yes, dry_run, platforms, skip_unavailable):
        captured["registry"] = reg
        captured["platforms"] = platforms
        captured["skip_unavailable"] = skip_unavailable
        return 0

    monkeypatch.setattr(sd.cli, "Registry", lambda: registry)
    monkeypatch.setattr(sd.target_commands, "cmd_target_prune", _fake_prune)
    rc = sd.main(["--cwd", str(tmp_path), "target", "prune", "ios", "--yes", "--dry-run"])
    assert rc == 0
    assert captured["registry"] is registry
    assert captured["platforms"] == ("ios",)
    assert captured["skip_unavailable"] is False


def test_cli_device_prune_default_is_both(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    captured = {}

    def _fake_prune(reg, *, yes, dry_run, platforms, skip_unavailable):
        captured["platforms"] = platforms
        captured["skip_unavailable"] = skip_unavailable
        return 0

    monkeypatch.setattr(sd.target_commands, "cmd_target_prune", _fake_prune)
    rc = sd.main(["--cwd", str(tmp_path), "target", "prune", "--yes", "--dry-run"])
    assert rc == 0
    assert captured["platforms"] == ("ios", "android")
    assert captured["skip_unavailable"] is True


def test_cli_device_prune_all_is_both(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    captured = {}

    def _fake_prune(reg, *, yes, dry_run, platforms, skip_unavailable):
        captured["platforms"] = platforms
        captured["skip_unavailable"] = skip_unavailable
        return 0

    monkeypatch.setattr(sd.target_commands, "cmd_target_prune", _fake_prune)
    rc = sd.main(["--cwd", str(tmp_path), "target", "prune", "all", "--yes", "--dry-run"])
    assert rc == 0
    assert captured["platforms"] == ("ios", "android")
    assert captured["skip_unavailable"] is True


def test_version_tuple_parses_and_falls_back():
    assert sd.devices._version_tuple("18.5") == (18, 5)
    assert sd.devices._version_tuple("17.0.1") == (17, 0, 1)
    assert sd.devices._version_tuple("nope") == (0,)


def test_ios_latest_runtime_sorts_numerically(monkeypatch):
    monkeypatch.setattr(
        sd.device_ios,
        "_xcrun_json",
        lambda args: {
            "runtimes": [
                {"identifier": "x.iOS-17-0", "version": "17.0", "isAvailable": True},
                {"identifier": "x.iOS-9-0", "version": "9.0", "isAvailable": True},
                {"identifier": "x.iOS-18-5", "version": "18.5", "isAvailable": True},
                {"identifier": "x.iOS-19-0", "version": "19.0", "isAvailable": False},
            ]
        },
    )
    assert sd.devices._ios_latest_runtime_version() == "18.5"
    assert sd.devices._ios_latest_runtime().endswith("iOS-18-5")


def test_ios_device_type_identifier_selection(monkeypatch):
    monkeypatch.setattr(
        sd.device_ios,
        "_xcrun_json",
        lambda args: {
            "devicetypes": [
                {"identifier": "t.iPhone-16", "name": "iPhone 16"},
                {"identifier": "t.iPhone-16-Pro", "name": "iPhone 16 Pro"},
                {"identifier": "t.iPhone-17-Pro", "name": "iPhone 17 Pro"},
            ]
        },
    )
    assert sd.devices._ios_device_type_identifier(None).endswith("iPhone-17-Pro")
    assert sd.devices._ios_device_type_identifier("iPhone 16").endswith("iPhone-16")
    with pytest.raises(sd.DeviceError):
        sd.devices._ios_device_type_identifier("iPhone 99")


def test_android_latest_image_picks_highest_api(monkeypatch):
    monkeypatch.setattr(sd.device_android, "_android_bin", lambda name: "/fake/" + name)
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
    monkeypatch.setattr(sd.device_android, "_android_bin", lambda name: "/fake/" + name)
    outputs = [b"List of devices attached\nemulator-5554\tdevice\n", b"my_avd\n"]
    monkeypatch.setattr(sd.devices.subprocess, "check_output", lambda *a, **k: outputs.pop(0))
    assert sd.devices._android_running_serial("my_avd") == "emulator-5554"


def test_android_running_serial_no_match(monkeypatch):
    monkeypatch.setattr(sd.device_android, "_android_bin", lambda name: "/fake/" + name)
    outputs = [b"List of devices attached\nemulator-5554\tdevice\n", b"other_avd\n"]
    monkeypatch.setattr(sd.devices.subprocess, "check_output", lambda *a, **k: outputs.pop(0))
    assert sd.devices._android_running_serial("my_avd") is None


def test_default_sim_name():
    cwd = Path("/work/myapp/co")
    digest = hashlib.sha256(str(cwd.resolve()).encode()).hexdigest()[:8]
    assert sd.devices._default_sim_name(cwd, "default") == f"myapp/co/default-{digest}"


def test_default_sim_name_distinguishes_matching_path_tails(tmp_path):
    one = tmp_path / "one" / "work" / "checkout"
    two = tmp_path / "two" / "work" / "checkout"
    one.mkdir(parents=True)
    two.mkdir(parents=True)

    one_name = sd.devices._default_sim_name(one, "default")
    two_name = sd.devices._default_sim_name(two, "default")

    assert one_name.startswith("work/checkout/default-")
    assert two_name.startswith("work/checkout/default-")
    assert one_name != two_name


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
    monkeypatch.setattr(sd.device_android, "_android_bin", lambda name: "/fake/" + name)
    monkeypatch.setattr(sd.devices.subprocess, "check_output", lambda *a, **k: b"pixel_9\nmy_avd\n")
    assert sd.devices._android_avd_exists("my_avd") is True
    assert sd.devices._android_avd_exists("ghost") is False


def test_detect_framework_override_and_autodetect(tmp_path):
    vite = sd.detect_framework(tmp_path, sd.Recipe({"project": {"framework": "vite"}}, tmp_path))
    assert vite == "vite"
    (tmp_path / "pubspec.yaml").write_text("name: app\n")
    assert sd.detect_framework(tmp_path, sd.Recipe({}, tmp_path)) == "flutter"


def test_detect_framework_auto_sentinel_loads_and_autodetects(tmp_path):
    # `framework = "auto"` must survive recipe validation and mean auto-detect.
    (tmp_path / "pubspec.yaml").write_text("name: app\n")
    recipe = sd.Recipe.parse('[project]\nframework = "auto"\n', tmp_path / "splashdown.toml")
    assert sd.detect_framework(tmp_path, recipe) == "flutter"


def test_detect_framework_falls_back_to_single_app_profile(tmp_path):
    # Root detection finds nothing (the app lives in a subdirectory), but the
    # recipe already records the resolved profile.
    recipe = sd.Recipe.parse(
        '[apps.api]\npath = "apps/api"\nprofile = "node-backend"\nresources = []\n',
        tmp_path / "splashdown.toml",
    )
    assert sd.detect_framework(tmp_path, recipe) == "node-backend"


def test_detect_framework_ignores_unknown_app_profiles(tmp_path):
    recipe = sd.Recipe.parse(
        '[apps.api]\npath = "apps/api"\nprofile = "node-backend"\nresources = []\n'
        '[apps.shared]\npath = "packages/shared"\nprofile = "unknown"\nresources = []\n',
        tmp_path / "splashdown.toml",
    )
    assert sd.detect_framework(tmp_path, recipe) == "node-backend"


def test_detect_framework_multi_app_raises_ambiguous(tmp_path):
    recipe = sd.Recipe.parse(
        '[apps.api]\npath = "apps/api"\nprofile = "node-backend"\nresources = []\n'
        '[apps.web]\npath = "apps/web"\nprofile = "vite"\nresources = []\n',
        tmp_path / "splashdown.toml",
    )
    with pytest.raises(sd.DeviceError, match="ambiguous") as e:
        sd.detect_framework(tmp_path, recipe)
    # The remediation must not name a flag that only `doctor` accepts.
    assert "--framework=NAME" not in str(e.value)


def test_detect_framework_two_apps_same_profile_still_ambiguous(tmp_path):
    # Deduping by profile collapsed these into one "unambiguous" app.
    recipe = sd.Recipe.parse(
        '[apps.web]\npath = "apps/web"\nprofile = "vite"\nresources = []\n'
        '[apps.admin]\npath = "apps/admin"\nprofile = "vite"\nresources = []\n',
        tmp_path / "splashdown.toml",
    )
    with pytest.raises(sd.DeviceError, match="ambiguous"):
        sd.detect_framework(tmp_path, recipe)


def test_resolve_app_dir_points_at_declared_subdirectory(tmp_path):
    (tmp_path / "apps" / "web").mkdir(parents=True)
    recipe = sd.Recipe.parse(
        '[apps.web]\npath = "apps/web"\nprofile = "vite"\nresources = []\n',
        tmp_path / "splashdown.toml",
    )
    assert sd.resolve_app_dir(tmp_path, recipe, "vite") == tmp_path / "apps" / "web"


def test_resolve_app_dir_prefers_cwd_when_root_itself_matches(tmp_path):
    (tmp_path / "vite.config.ts").write_text("export default {}")
    (tmp_path / "apps" / "web").mkdir(parents=True)
    recipe = sd.Recipe.parse(
        '[apps.web]\npath = "apps/web"\nprofile = "vite"\nresources = []\n',
        tmp_path / "splashdown.toml",
    )
    assert sd.resolve_app_dir(tmp_path, recipe, "vite") == tmp_path


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

    monkeypatch.setattr(sd.runners.subprocess, "call", _fake_call)
    recipe = sd.Recipe({"project": {"run": "echo ran {device_id}"}}, tmp_path / "splashdown.toml")
    rc = sd.device_run(tmp_path, recipe, {"kind": "ios", "udid": "ABCD"})
    assert rc == 7
    assert captured["cmd"] == "echo ran ABCD"


def test_cli_run_rejects_non_runnable_profile_before_device_mutation(tmp_path, monkeypatch, capsys):
    (tmp_path / "splashdown.toml").write_text(
        '[project]\nframework = "vite"\n\n[targets.simulator.default]\nmodel = "iPhone 17"\n'
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(
        sd.target_commands,
        "ensure_fresh_sim",
        lambda *args, **kwargs: pytest.fail("device mutation must not start"),
    )

    assert sd.main(["--cwd", str(tmp_path), "run", "simulator"]) == 1
    assert "does not support `splash run`" in capsys.readouterr().err
    assert sd.Registry().all_devices() == []


def test_run_preflight_accepts_custom_command_without_runnable_profile(tmp_path):
    recipe = sd.Recipe(
        {"project": {"framework": "vite", "run": {"ios": "echo custom"}}},
        tmp_path / "splashdown.toml",
    )

    sd.validate_device_run(tmp_path, recipe, "ios")


def test_device_run_no_custom_command_uses_framework(tmp_path, monkeypatch):
    # With no [project] run, device_run must fall through to framework detection
    # and launch via the profile — a regression that returned 0/"" instead of
    # None from run_custom_command would silently break every normal run.
    recipe = sd.Recipe({}, tmp_path / "splashdown.toml")
    monkeypatch.setattr(sd.launching, "detect_framework", lambda cwd, r: "flutter")
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


_RUNTIMES = {
    "runtimes": [
        {
            "identifier": "com.apple.CoreSimulator.SimRuntime.iOS-18-5",
            "version": "18.5",
            "isAvailable": True,
            "supportedArchitectures": ["x86_64", "arm64"],
            "supportedDeviceTypes": [
                {"name": "iPhone 16 Pro"},
                {"name": "iPhone 16"},
                {"name": "iPad Pro 13-inch"},
            ],
        },
        {
            "identifier": "com.apple.CoreSimulator.SimRuntime.iOS-26-5",
            "version": "26.5",
            "isAvailable": True,
            "supportedArchitectures": ["arm64"],
            "supportedDeviceTypes": [{"name": "iPhone 17"}, {"name": "iPhone 17 Pro"}],
        },
    ]
}


def _fake_runtimes(monkeypatch, data=_RUNTIMES):
    monkeypatch.setattr(sd.device_ios, "_xcrun_json", lambda args: data)


def test_ios_x86_64_target_picks_newest_runtime_with_an_x86_64_slice(monkeypatch):
    _fake_runtimes(monkeypatch)
    # 26.5 is newer but arm64-only, so it must be skipped rather than returned.
    assert sd.ios_x86_64_target() == ("18.5", "iPhone 16 Pro")


def test_ios_x86_64_target_none_when_every_runtime_is_arm64_only(monkeypatch):
    arm_only = {"runtimes": [_RUNTIMES["runtimes"][1]]}
    _fake_runtimes(monkeypatch, arm_only)
    assert sd.ios_x86_64_target() is None


def test_ios_x86_64_target_none_when_no_runtimes_installed(monkeypatch):
    _fake_runtimes(monkeypatch, {"runtimes": []})
    assert sd.ios_x86_64_target() is None


def test_ios_runtime_models_excludes_non_iphone_types(monkeypatch):
    _fake_runtimes(monkeypatch)
    models = sd.devices._ios_runtime_models("com.apple.CoreSimulator.SimRuntime.iOS-18-5")
    assert models == ["iPhone 16 Pro", "iPhone 16"]


def test_ios_create_failure_explains_incompatible_model(monkeypatch):
    monkeypatch.setattr(sd.capabilities.sys, "platform", "darwin")

    _fake_runtimes(monkeypatch)
    monkeypatch.setattr(sd.device_ios, "_ios_find_device_by_name", lambda name: None)
    monkeypatch.setattr(
        sd.device_ios, "_ios_device_type_identifier", lambda m: "com.apple.x.iPhone-17"
    )

    def boom(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, stderr=b"Incompatible device (code=403)")

    monkeypatch.setattr(sd.devices.subprocess, "check_output", boom)
    with pytest.raises(sd.DeviceError) as exc:
        sd.devices.ios_ensure("co/app/default", "iPhone 17", "18.5")
    msg = str(exc.value)
    assert "Incompatible device" in msg
    assert "`iPhone 17` is not a device type iOS 18.5 can create" in msg
    assert "iPhone 16 Pro" in msg


def test_ios_create_failure_stays_bare_when_model_is_compatible(monkeypatch):
    _fake_runtimes(monkeypatch)
    monkeypatch.setattr(sd.device_ios, "_ios_find_device_by_name", lambda name: None)
    monkeypatch.setattr(
        sd.device_ios, "_ios_device_type_identifier", lambda m: "com.apple.x.iPhone-16-Pro"
    )

    def boom(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, stderr=b"disk full")

    monkeypatch.setattr(sd.devices.subprocess, "check_output", boom)
    with pytest.raises(sd.DeviceError) as exc:
        sd.devices.ios_ensure("co/app/default", "iPhone 16 Pro", "18.5")
    # An unrelated failure must not be given a misleading model explanation.
    assert "is not a device type" not in str(exc.value)
