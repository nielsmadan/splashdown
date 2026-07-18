"""Tests for splashdown profiles behavior."""

from __future__ import annotations

import json

import pytest

import splashdown as sd
from conftest import (
    _capture_profile_calls,
)


def test_flutter_run_builds_argv(tmp_path, monkeypatch):
    calls = _capture_profile_calls(monkeypatch)
    rc = sd.profiles._flutter_run(
        tmp_path, sd.Recipe({}, tmp_path / "x.toml"), {"kind": "ios", "udid": "U1"}
    )
    assert rc == 0
    assert ["flutter", "run", "-d", "U1"] in calls


def test_rn_run_ios_and_android(tmp_path, monkeypatch):
    calls = _capture_profile_calls(monkeypatch)
    r = sd.Recipe({}, tmp_path / "x.toml")
    sd.profiles._rn_run(tmp_path, r, {"kind": "ios", "udid": "U1"})
    sd.profiles._rn_run(tmp_path, r, {"kind": "android", "serial": "S1"})
    flat = [" ".join(c) for c in calls]
    assert any("run-ios" in c and "--udid U1" in c for c in flat)
    assert any("run-android" in c and "--deviceId S1" in c for c in flat)
    # Bare recipe forwards no scheme/mode.
    assert not any("--scheme" in c or "--mode" in c for c in flat)


def test_rn_run_forwards_scheme_and_mode(tmp_path, monkeypatch):
    calls = _capture_profile_calls(monkeypatch)
    r = sd.Recipe(
        {
            "project": {
                "ios": {"scheme": "DreamHackDev", "mode": "Debug"},
                "android": {"mode": "developmentDebug"},
            }
        },
        tmp_path / "x.toml",
    )
    sd.profiles._rn_run(tmp_path, r, {"kind": "ios", "udid": "U1"})
    sd.profiles._rn_run(tmp_path, r, {"kind": "android", "serial": "S1"})
    flat = [" ".join(c) for c in calls]
    assert any(
        "run-ios" in c and "--scheme DreamHackDev" in c and "--mode Debug" in c for c in flat
    )
    assert any("run-android" in c and "--mode developmentDebug" in c for c in flat)


def test_rn_run_rejects_flaglike_scheme(tmp_path, monkeypatch):
    _capture_profile_calls(monkeypatch)
    r = sd.Recipe({"project": {"ios": {"scheme": "-evil"}}}, tmp_path / "x.toml")
    try:
        sd.profiles._rn_run(tmp_path, r, {"kind": "ios", "udid": "U1"})
    except sd.DeviceError:
        return
    raise AssertionError("expected DeviceError for flag-like scheme")


def _write_pods_excluded_arch(tmp_path):
    cfg = tmp_path / "ios" / "Pods" / "Target Support Files" / "MLKit" / "MLKit.debug.xcconfig"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("EXCLUDED_ARCHS[sdk=iphonesimulator*] = arm64\n")


def test_rn_run_prints_arch_hint_on_failure(tmp_path, monkeypatch, capsys):
    _write_pods_excluded_arch(tmp_path)
    monkeypatch.setattr(sd.profiles.subprocess, "call", lambda args, **k: 70)
    rc = sd.profiles._rn_run(
        tmp_path, sd.Recipe({}, tmp_path / "x.toml"), {"kind": "ios", "udid": "U1"}
    )
    assert rc == 70
    assert 'ios = "18.5"' in capsys.readouterr().err


def test_rn_run_no_arch_hint_on_success_or_missing_pods(tmp_path, monkeypatch, capsys):
    # Success → no hint even when the exclusion is present.
    _write_pods_excluded_arch(tmp_path)
    monkeypatch.setattr(sd.profiles.subprocess, "call", lambda args, **k: 0)
    sd.profiles._rn_run(tmp_path, sd.Recipe({}, tmp_path / "x.toml"), {"kind": "ios", "udid": "U1"})
    assert "18.5" not in capsys.readouterr().err
    # Failure but no Pods → no hint.
    other = tmp_path / "no_pods"
    other.mkdir()
    monkeypatch.setattr(sd.profiles.subprocess, "call", lambda args, **k: 70)
    sd.profiles._rn_run(other, sd.Recipe({}, other / "x.toml"), {"kind": "ios", "udid": "U1"})
    assert "18.5" not in capsys.readouterr().err


def test_expo_run_ios_and_android(tmp_path, monkeypatch):
    calls = _capture_profile_calls(monkeypatch)
    r = sd.Recipe({}, tmp_path / "x.toml")
    sd.profiles._expo_run(tmp_path, r, {"kind": "ios", "udid": "U1"})
    sd.profiles._expo_run(tmp_path, r, {"kind": "android", "serial": "S1"})
    flat = [" ".join(c) for c in calls]
    assert any("run:ios" in c and "--device U1" in c for c in flat)
    assert any("run:android" in c and "--device S1" in c for c in flat)


def _app(tmp_path, profile):
    return sd.AppInventory(name="main", path=tmp_path, profile=profile)


def test_react_native_profile_declares_sim_and_emulator_targets(tmp_path):
    targets = sd.PROFILES["react-native"].targets(_app(tmp_path, "react-native"))
    assert targets["simulator"]["default"]["model"]
    assert targets["emulator"]["default"]["device"]


def test_flutter_profile_declares_sim_and_emulator_targets(tmp_path):
    targets = sd.PROFILES["flutter"].targets(_app(tmp_path, "flutter"))
    assert "default" in targets["simulator"]
    assert "default" in targets["emulator"]


def test_expo_profile_declares_sim_and_emulator_targets(tmp_path):
    targets = sd.PROFILES["expo"].targets(_app(tmp_path, "expo"))
    assert "default" in targets["simulator"]
    assert "default" in targets["emulator"]


def test_ios_native_profile_declares_only_simulator_target(tmp_path):
    targets = sd.PROFILES["ios-native"].targets(_app(tmp_path, "ios-native"))
    assert "default" in targets["simulator"]
    assert "emulator" not in targets


def test_android_native_profile_declares_only_emulator_target(tmp_path):
    targets = sd.PROFILES["android-native"].targets(_app(tmp_path, "android-native"))
    assert "default" in targets["emulator"]
    assert "simulator" not in targets


def test_non_mobile_profile_declares_no_targets(tmp_path):
    assert sd.PROFILES["vite"].targets(_app(tmp_path, "vite")) == {}


def test_expo_profile_emits_metro_port(tmp_path):
    res = sd.PROFILES["expo"].resources(_app(tmp_path, "expo"))
    assert res["RCT_METRO_PORT"]["type"] == "port"
    assert res["RCT_METRO_PORT"]["range"] == [8081, 8200]


def test_ios_native_run_simulator_uses_simctl(tmp_path, monkeypatch):
    app = tmp_path / "Demo.app"
    app.mkdir()
    import plistlib

    with (app / "Info.plist").open("wb") as f:
        plistlib.dump({"CFBundleIdentifier": "com.demo"}, f)
    recipe = sd.Recipe(
        {"project": {"ios": {"scheme": "Demo", "project": "Demo.xcodeproj"}}},
        tmp_path / "splashdown.toml",
    )
    calls = _capture_profile_calls(monkeypatch)

    class _Done:
        stdout = json.dumps(
            [{"buildSettings": {"BUILT_PRODUCTS_DIR": str(tmp_path), "WRAPPER_NAME": "Demo.app"}}]
        )

    monkeypatch.setattr(sd.profiles.subprocess, "run", lambda *a, **k: _Done())
    rc = sd.profiles._ios_native_run(tmp_path, recipe, {"kind": "ios", "udid": "SIM-1"})
    assert rc == 0
    flat = [" ".join(c) for c in calls]
    assert any("simctl install SIM-1" in c for c in flat)
    assert any("simctl launch SIM-1 com.demo" in c for c in flat)
    assert not any("devicectl" in c for c in flat)


def test_android_native_run_monkey_launcher(tmp_path, monkeypatch):
    recipe = sd.Recipe({"project": {"android": {}}}, tmp_path / "splashdown.toml")
    calls = _capture_profile_calls(monkeypatch)
    monkeypatch.setattr(
        sd.profiles.subprocess, "check_output", lambda *a, **k: "applicationId: com.example.app\n"
    )
    rc = sd.profiles._android_native_run(
        tmp_path, recipe, {"kind": "android", "serial": "emulator-5554"}
    )
    assert rc == 0
    flat = [" ".join(c) for c in calls]
    assert any(":app:installDebug" in c for c in flat)  # variant casing
    assert any("monkey" in c and "com.example.app" in c for c in flat)


def test_android_native_run_launch_activity(tmp_path, monkeypatch):
    recipe = sd.Recipe(
        {"project": {"android": {"application_id": "com.x", "launch_activity": ".Main"}}},
        tmp_path / "splashdown.toml",
    )
    calls = _capture_profile_calls(monkeypatch)
    rc = sd.profiles._android_native_run(tmp_path, recipe, {"kind": "android", "serial": "S1"})
    assert rc == 0
    flat = [" ".join(c) for c in calls]
    assert any("am start -n com.x/.Main" in c for c in flat)


@pytest.mark.parametrize(
    "field,value",
    [
        ("launch_activity", ".Main; rm -rf /sdcard"),
        ("launch_activity", ".Main$(reboot)"),
        ("application_id", "com.x`id`"),
    ],
)
def test_android_native_run_rejects_shell_injection(tmp_path, monkeypatch, field, value):
    """`adb shell` re-parses argv through the device sh, so recipe-supplied package
    /activity names with shell metacharacters must be rejected, not passed through."""
    android = {"application_id": "com.x", "launch_activity": ".Main", field: value}
    recipe = sd.Recipe({"project": {"android": android}}, tmp_path / "splashdown.toml")
    _capture_profile_calls(monkeypatch)
    with pytest.raises(sd.DeviceError):
        sd.profiles._android_native_run(tmp_path, recipe, {"kind": "android", "serial": "S1"})
