"""Tests for splashdown.

Run with: python -m pytest tests/ -q
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import splashdown as sd  # noqa: E402


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
    a.mkdir(); b.mkdir()
    pa = registry.allocate_port(str(a), "METRO", 18081, 18100)
    pb = registry.allocate_port(str(b), "METRO", 18081, 18100)
    assert pa != pb


def test_gc_frees_dead_checkout(registry, tmp_path):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
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
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    registry.set_device(str(a), "simulator", "default", "UDID-A", "iPhone 17", "18.5")
    registry.set_device(str(b), "simulator", "default", "UDID-B", "iPhone 17", "18.5")
    assert registry.managed_udids() == {"UDID-A", "UDID-B"}


def test_device_registry_devices_for(registry, tmp_path):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    registry.set_device(str(a), "simulator", "default", "UDID-A", "iPhone 17", "18.5")
    registry.set_device(str(a), "simulator", "small", "UDID-S", "iPhone 13 Mini", "18.5")
    registry.set_device(str(b), "simulator", "default", "UDID-B", "iPhone 17", "18.5")
    rows = registry.devices_for(str(a))
    assert {r.variant for r in rows} == {"default", "small"}


def test_device_registry_gc_drops_defunct(registry, tmp_path):
    a = tmp_path / "a"; a.mkdir()
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
    a = tmp_path / "alive"; a.mkdir()
    b = tmp_path / "dead"; b.mkdir()
    registry.set_device(str(a), "simulator", "default", "UDID-A", "iPhone 17", "18.5")
    registry.set_device(str(b), "simulator", "default", "UDID-B", "iPhone 17", "18.5")
    b.rmdir()
    registry.gc()
    udids = {r.udid for r in registry.all_devices()}
    assert udids == {"UDID-A"}


# ---------- ensure_fresh_sim ----------

def test_ensure_fresh_creates_when_missing(registry, checkout, monkeypatch):
    created = {}

    def fake_ensure(name, model, ios):
        created["call"] = (name, model, ios)
        return "UDID-NEW", "Shutdown"

    monkeypatch.setattr(sd, "ios_ensure", fake_ensure)
    monkeypatch.setattr(sd, "_ios_latest_runtime_version", lambda: "18.5")
    monkeypatch.setattr(sd, "_ios_udid_exists", lambda u: True)
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
    monkeypatch.setattr(sd, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd, "_ios_latest_runtime_version", lambda: "18.5")
    monkeypatch.setattr(sd, "ios_destroy", lambda u: destroyed.append(u))
    monkeypatch.setattr(sd, "ios_ensure", lambda n, m, i: ("UDID-NEW", "Shutdown"))
    sd.ensure_fresh_sim(registry, checkout, "simulator", "default", {"model": "iPhone 17"})
    assert destroyed == ["UDID-OLD"]
    assert registry.get_device(abspath, "simulator", "default").udid == "UDID-NEW"


def test_ensure_fresh_keeps_when_pinned_and_current(registry, checkout, monkeypatch):
    abspath = str(checkout.resolve())
    registry.set_device(abspath, "simulator", "legacy", "UDID-X", "iPhone 12", "17.0")
    monkeypatch.setattr(sd, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd, "_ios_latest_runtime_version", lambda: "18.5")
    info = sd.ensure_fresh_sim(
        registry, checkout, "simulator", "legacy",
        {"model": "iPhone 12", "ios": "17.0"},
    )
    assert info["udid"] == "UDID-X"


def test_ensure_fresh_recreates_when_pinned_ios_mismatch(registry, checkout, monkeypatch):
    abspath = str(checkout.resolve())
    registry.set_device(abspath, "simulator", "legacy", "UDID-OLD", "iPhone 12", "17.0")
    destroyed = []
    monkeypatch.setattr(sd, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd, "ios_destroy", lambda u: destroyed.append(u))
    monkeypatch.setattr(sd, "ios_ensure", lambda n, m, i: ("UDID-NEW", "Shutdown"))
    monkeypatch.setattr(sd, "_ios_latest_runtime_version", lambda: "18.5")
    sd.ensure_fresh_sim(
        registry, checkout, "simulator", "legacy",
        {"model": "iPhone 12", "ios": "17.5"},
    )
    assert destroyed == ["UDID-OLD"]


def test_ensure_fresh_recreates_when_model_changed(registry, checkout, monkeypatch):
    abspath = str(checkout.resolve())
    registry.set_device(abspath, "simulator", "default", "UDID-OLD", "iPhone 17", "18.5")
    destroyed = []
    monkeypatch.setattr(sd, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd, "_ios_latest_runtime_version", lambda: "18.5")
    monkeypatch.setattr(sd, "ios_destroy", lambda u: destroyed.append(u))
    monkeypatch.setattr(sd, "ios_ensure", lambda n, m, i: ("UDID-NEW", "Shutdown"))
    # Recipe bumped from iPhone 17 -> iPhone 18.
    sd.ensure_fresh_sim(registry, checkout, "simulator", "default", {"model": "iPhone 18"})
    assert destroyed == ["UDID-OLD"]


def test_ensure_fresh_recreates_when_udid_gone(registry, checkout, monkeypatch):
    abspath = str(checkout.resolve())
    registry.set_device(abspath, "simulator", "default", "UDID-X", "iPhone 17", "18.5")
    monkeypatch.setattr(sd, "_ios_udid_exists", lambda u: False)  # user nuked the sim
    monkeypatch.setattr(sd, "_ios_latest_runtime_version", lambda: "18.5")
    monkeypatch.setattr(sd, "ios_ensure", lambda n, m, i: ("UDID-NEW", "Shutdown"))
    info = sd.ensure_fresh_sim(registry, checkout, "simulator", "default", {"model": "iPhone 17"})
    assert info["udid"] == "UDID-NEW"


# ---------- device_add / device_remove (new shape) ----------

def test_device_add_writes_nested_table(tmp_path):
    sd.device_add(tmp_path, "simulator", "repro-bug", {"model": "iPhone 16", "ios": "17.5"})
    text = (tmp_path / "splashdown.local.toml").read_text()
    assert "[devices.simulator.repro-bug]" in text
    assert 'model = "iPhone 16"' in text
    assert 'ios = "17.5"' in text


def test_device_add_rejects_collision_with_local(tmp_path):
    sd.device_add(tmp_path, "simulator", "repro", {"model": "A"})
    with pytest.raises(sd.DeviceError, match="already exists"):
        sd.device_add(tmp_path, "simulator", "repro", {"model": "B"})


def test_device_add_rejects_collision_with_recipe(tmp_path):
    (tmp_path / "splashdown.toml").write_text(
        '[devices.simulator.default]\nmodel = "iPhone 17"\n'
    )
    with pytest.raises(sd.DeviceError, match="recipe"):
        sd.device_add(tmp_path, "simulator", "default", {"model": "iPhone 16"})


def test_device_add_rejects_bad_type(tmp_path):
    with pytest.raises(sd.DeviceError, match="type"):
        sd.device_add(tmp_path, "not-a-type", "default", {})


def test_device_add_rejects_bad_variant(tmp_path):
    with pytest.raises(sd.DeviceError, match="variant"):
        sd.device_add(tmp_path, "simulator", "has spaces", {"model": "X"})


def test_device_remove_strips_local_variant(tmp_path):
    sd.device_add(tmp_path, "simulator", "repro", {"model": "X"})
    sd.device_add(tmp_path, "simulator", "other", {"model": "Y"})
    sd.device_remove(tmp_path, "simulator", "repro")
    lc = sd.LocalConfig.load(tmp_path / "splashdown.local.toml")
    assert "repro" not in lc.devices.get("simulator", {})
    assert "other" in lc.devices["simulator"]


def test_device_remove_refuses_recipe_variant(tmp_path):
    (tmp_path / "splashdown.toml").write_text(
        '[devices.simulator.default]\nmodel = "iPhone 17"\n'
    )
    with pytest.raises(sd.DeviceError, match="recipe"):
        sd.device_remove(tmp_path, "simulator", "default")


def test_device_remove_errors_when_missing(tmp_path):
    with pytest.raises(sd.DeviceError, match="no device"):
        sd.device_remove(tmp_path, "simulator", "ghost")


# ---------- splash run / boot (top-level) ----------

def _stub_ios_boot_chain(monkeypatch):
    monkeypatch.setattr(sd, "_ios_latest_runtime_version", lambda: "18.5")
    monkeypatch.setattr(sd, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd, "_ios_current_state", lambda u: "Shutdown")
    monkeypatch.setattr(sd, "ios_ensure", lambda n, m, i: ("UDID-NEW", "Shutdown"))
    monkeypatch.setattr(sd, "ios_boot", lambda u, s: None)


def test_cli_run_default_variant(tmp_path, monkeypatch):
    (tmp_path / "splashdown.toml").write_text("""
[devices.simulator.default]
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
    monkeypatch.setattr(sd, "device_run", _fake_run)
    rc = sd.main(["--cwd", str(tmp_path), "run", "simulator"])
    assert rc == 0
    assert captured["info"]["udid"] == "UDID-NEW"
    assert captured["info"]["name"].endswith("/default")


def test_cli_run_explicit_variant(tmp_path, monkeypatch):
    (tmp_path / "splashdown.toml").write_text("""
[devices.simulator.default]
model = "iPhone 17"

[devices.simulator.small-screen]
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
    monkeypatch.setattr(sd, "device_run", _fake_run)
    sd.main(["--cwd", str(tmp_path), "run", "simulator", "small-screen"])
    assert captured["info"]["name"].endswith("/small-screen")


def test_cli_run_errors_when_no_default_and_no_pick(tmp_path, monkeypatch, capsys):
    (tmp_path / "splashdown.toml").write_text("""
[devices.simulator.a]
model = "X"

[devices.simulator.b]
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
[devices.simulator.default]
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

    monkeypatch.setattr(sd, "device_run", fake_run)
    rc = sd.main(["--cwd", str(tmp_path), "start", "simulator"])
    assert rc == 0
    assert called["device_run"] is False


def test_cli_devices_shows_recipe_and_local(tmp_path, monkeypatch):
    (tmp_path / "splashdown.toml").write_text(
        '[devices.simulator.default]\nmodel = "A"\n[project]\nframework = "react-native"\n'
    )
    (tmp_path / "splashdown.local.toml").write_text(
        '[devices.simulator.repro]\nmodel = "B"\n'
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # Stub status checks to avoid hitting xcrun.
    monkeypatch.setattr(sd, "device_status", lambda dtype, name: "absent")
    rc = sd.main(["--cwd", str(tmp_path), "devices"])
    assert rc == 0


def test_cli_stop_resolves_by_type_and_variant(tmp_path, monkeypatch):
    (tmp_path / "splashdown.toml").write_text("""
[devices.simulator.default]
model = "iPhone 17"

[project]
framework = "react-native"
""")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    captured = {}
    def _shutdown(dt, name):
        captured["call"] = (dt, name)
    monkeypatch.setattr(sd, "device_shutdown", _shutdown)
    rc = sd.main(["--cwd", str(tmp_path), "stop", "simulator"])
    assert rc == 0
    assert captured["call"][0] == "simulator"
    assert captured["call"][1].endswith("/default")


def test_cli_run_infers_dtype_when_only_one_declared(tmp_path, monkeypatch):
    (tmp_path / "splashdown.toml").write_text("""
[devices.simulator.default]
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
    monkeypatch.setattr(sd, "device_run", _fake_run)
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


def test_cli_refresh_reallocates_on_squatter(tmp_path, monkeypatch, capsys):
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
        rc = sd.main(["--cwd", str(tmp_path), "refresh"])
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
    rc = sd.main(["--cwd", str(tmp_path), "release", "GONE"])
    assert rc == 0
    rc = sd.main(["--cwd", str(tmp_path), "get", "GONE"])
    assert rc == 1  # key gone


def test_cli_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        sd.main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "splashdown" in out
    assert re.search(r"\d+\.\d+\.\d+", out)


def test_cli_bare_device_lists(tmp_path, monkeypatch):
    (tmp_path / "splashdown.toml").write_text(
        '[devices.simulator.default]\nmodel = "X"\n[project]\nframework = "react-native"\n'
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(sd, "device_status", lambda dtype, name: "absent")
    rc = sd.main(["--cwd", str(tmp_path), "device"])
    assert rc == 0


def test_cli_device_remove_destroys_instance_by_default(tmp_path, monkeypatch):
    (tmp_path / "splashdown.toml").write_text(
        '[project]\nframework = "react-native"\n'
    )
    (tmp_path / "splashdown.local.toml").write_text(
        '[devices.simulator.repro]\nmodel = "iPhone 17"\n'
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    destroyed: list[tuple[str, str]] = []
    monkeypatch.setattr(sd, "device_destroy", lambda dt, name: destroyed.append((dt, name)))
    rc = sd.main(["--cwd", str(tmp_path), "device", "remove", "simulator", "repro"])
    assert rc == 0
    assert destroyed and destroyed[0][0] == "simulator"
    assert "[devices.simulator.repro]" not in (tmp_path / "splashdown.local.toml").read_text()


def test_cli_device_remove_keep_instance_skips_destroy(tmp_path, monkeypatch):
    (tmp_path / "splashdown.toml").write_text(
        '[project]\nframework = "react-native"\n'
    )
    (tmp_path / "splashdown.local.toml").write_text(
        '[devices.simulator.repro]\nmodel = "iPhone 17"\n'
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    destroyed: list[tuple[str, str]] = []
    monkeypatch.setattr(sd, "device_destroy", lambda dt, name: destroyed.append((dt, name)))
    rc = sd.main([
        "--cwd", str(tmp_path), "device", "remove",
        "simulator", "repro", "--keep-instance",
    ])
    assert rc == 0
    assert destroyed == []  # sim left alone
    assert "[devices.simulator.repro]" not in (tmp_path / "splashdown.local.toml").read_text()


# ---------- device gc / prune ----------

def test_device_gc_drops_defunct_checkouts(registry, tmp_path, monkeypatch):
    a = tmp_path / "gone"; a.mkdir()
    b = tmp_path / "live"; b.mkdir()
    registry.set_device(str(a), "simulator", "default", "UDID-A", "iPhone 17", "18.5")
    registry.set_device(str(b), "simulator", "default", "UDID-B", "iPhone 17", "18.5")
    a.rmdir()
    destroyed = []
    monkeypatch.setattr(sd, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd, "ios_destroy", lambda u: destroyed.append(u))
    rc = sd.cmd_device_gc(registry, all_=False)
    assert rc == 0
    assert destroyed == ["UDID-A"]
    assert {r.udid for r in registry.all_devices()} == {"UDID-B"}


def test_device_gc_all_drops_stale_latest_but_keeps_pinned(registry, tmp_path, monkeypatch):
    checkout = tmp_path / "co"; checkout.mkdir()
    (checkout / "splashdown.toml").write_text("""
[devices.simulator.default]
model = "iPhone 17"

[devices.simulator.legacy]
model = "iPhone 12"
ios   = "17.0"
""")
    abspath = str(checkout.resolve())
    registry.set_device(abspath, "simulator", "default", "UDID-DEFAULT", "iPhone 17", "17.5")  # stale latest
    registry.set_device(abspath, "simulator", "legacy", "UDID-LEGACY", "iPhone 12", "17.0")    # pinned, current
    monkeypatch.setattr(sd, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd, "_ios_latest_runtime_version", lambda: "18.5")
    destroyed = []
    monkeypatch.setattr(sd, "ios_destroy", lambda u: destroyed.append(u))
    sd.cmd_device_gc(registry, all_=True)
    assert destroyed == ["UDID-DEFAULT"]
    assert {r.udid for r in registry.all_devices()} == {"UDID-LEGACY"}


def test_device_prune_lists_only_unmanaged(registry, monkeypatch, capsys):
    fake_devices = {
        "iOS 18.5": [
            {"name": "myapp/feat-x/default", "udid": "MANAGED", "isAvailable": True, "state": "Shutdown"},
            {"name": "iPhone 17", "udid": "FOREIGN-1", "isAvailable": True, "state": "Shutdown"},
            {"name": "iPad Air", "udid": "FOREIGN-2", "isAvailable": True, "state": "Shutdown"},
        ]
    }
    monkeypatch.setattr(sd, "_xcrun_json", lambda args: {"devices": fake_devices})
    registry.set_device("/tmp/something", "simulator", "default", "MANAGED", "iPhone 17", "18.5")
    rc = sd.cmd_device_prune(registry, yes=False, dry_run=True, platforms=("ios",))
    assert rc == 0
    err = capsys.readouterr().err
    assert "FOREIGN-1" in err
    assert "FOREIGN-2" in err
    assert "MANAGED" not in err
    assert "--dry-run" in err


def test_device_prune_yes_destroys_unmanaged(registry, monkeypatch):
    fake_devices = {"iOS 18.5": [
        {"name": "iPhone 17", "udid": "FOREIGN", "isAvailable": True, "state": "Shutdown"},
    ]}
    monkeypatch.setattr(sd, "_xcrun_json", lambda args: {"devices": fake_devices})
    destroyed: list[str] = []
    shut: list[str] = []
    monkeypatch.setattr(sd, "ios_destroy", lambda u: destroyed.append(u))
    monkeypatch.setattr(sd, "ios_shutdown", lambda u: shut.append(u))
    rc = sd.cmd_device_prune(registry, yes=True, dry_run=False, platforms=("ios",))
    assert rc == 0
    assert destroyed == ["FOREIGN"]
    assert shut == ["FOREIGN"]


def test_device_prune_noop_when_nothing_unmanaged(registry, monkeypatch, capsys):
    monkeypatch.setattr(sd, "_xcrun_json", lambda args: {"devices": {}})
    rc = sd.cmd_device_prune(registry, yes=True, dry_run=False, platforms=("ios",))
    assert rc == 0
    assert "nothing" in capsys.readouterr().err.lower()


# ---------- presets ----------

def test_rn_preset_declares_default_ios_variant(tmp_path):
    sd.cmd_init(tmp_path, preset="rn")
    recipe = sd.Recipe.load(tmp_path / "splashdown.toml")
    assert "default" in recipe.devices.get("simulator", {})
    assert recipe.devices["simulator"]["default"]["model"]
    assert "SIM_NAME" not in recipe.resources


def test_flutter_preset_declares_both_defaults(tmp_path):
    sd.cmd_init(tmp_path, preset="flutter")
    recipe = sd.Recipe.load(tmp_path / "splashdown.toml")
    assert "default" in recipe.devices.get("simulator", {})
    assert "default" in recipe.devices.get("emulator", {})
    assert "SIM_NAME" not in recipe.resources


def test_local_skeleton_documents_additions(tmp_path):
    sd.cmd_init(tmp_path, preset="rn")
    text = (tmp_path / "splashdown.local.toml").read_text()
    assert "additional" in text.lower() or "additions" in text.lower()
    assert "simulator" in text
    assert "splash device add" in text


# ---------- templates ----------

def test_template_basic_vars(tmp_path):
    cwd = tmp_path / "myrepo.feat"
    cwd.mkdir()
    scope = sd._make_scope(cwd, "feat", {})
    assert sd.render_template("{{ cwd }}", scope) == "myrepo.feat"
    assert sd.render_template("port-{{ basename(cwd_abs) }}", scope) == "port-myrepo.feat"
    assert sd.render_template("{{ slug(cwd) }}", scope) == "myrepo-feat"


def test_template_cross_resource(tmp_path):
    cwd = tmp_path / "x"; cwd.mkdir()
    scope = sd._make_scope(cwd, "", {"PORT": "8081"})
    assert sd.render_template("http://localhost:{{ PORT }}", scope) == "http://localhost:8081"


def test_template_refs():
    refs = sd.template_refs("http://x:{{ PORT }}/{{ basename(cwd) }}")
    assert "PORT" in refs
    assert "basename" in refs


def test_template_error_on_bad_expr(tmp_path):
    cwd = tmp_path / "x"; cwd.mkdir()
    scope = sd._make_scope(cwd, "", {})
    with pytest.raises(sd.TemplateError):
        sd.render_template("{{ no_such_var }}", scope)


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

def _write_recipe(cwd: Path, body: str) -> None:
    (cwd / "splashdown.toml").write_text(body)


def test_provision_writes_splashdown_env(registry, checkout):
    _write_recipe(checkout, """
[resources.PORT]
type  = "port"
range = [18400, 18410]

[resources.RUN_ID]
type = "uuid"

[resources.URL]
type     = "template"
template = "http://localhost:{{ PORT }}"
""")
    resolved = sd.provision(checkout, registry=registry)
    assert 18400 <= int(resolved["PORT"]) <= 18410
    assert resolved["URL"] == f"http://localhost:{resolved['PORT']}"
    assert len(resolved["RUN_ID"]) == 36  # uuid string length

    recipe = sd.Recipe.load(checkout / "splashdown.toml")
    sd.write_outputs(checkout, recipe, resolved)
    text = (checkout / "splashdown.env").read_text()
    assert f'PORT={resolved["PORT"]}' in text
    assert "URL=" in text


def test_provision_idempotent(registry, checkout):
    _write_recipe(checkout, """
[resources.RUN_ID]
type = "uuid"
[resources.PORT]
type  = "port"
range = [18500, 18510]
""")
    r1 = sd.provision(checkout, registry=registry)
    r2 = sd.provision(checkout, registry=registry)
    assert r1 == r2


def test_provision_reprovision_regenerates_uuid(registry, checkout):
    _write_recipe(checkout, """
[resources.RUN_ID]
type = "uuid"
""")
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
    _write_recipe(checkout, """
[resources.MY_VAR]
type     = "template"
template = "hello"
writer   = "envfile=.env.local"
""")
    resolved = sd.provision(checkout, registry=registry)
    recipe = sd.Recipe.load(checkout / "splashdown.toml")
    sd.write_outputs(checkout, recipe, resolved)
    text = (checkout / ".env.local").read_text()
    assert "MY_VAR=hello" in text


def test_cwd_resource_type(registry, tmp_path):
    cwd = tmp_path / "mybranch"; cwd.mkdir()
    _write_recipe(cwd, """
[resources.NAME]
type = "cwd"
""")
    resolved = sd.provision(cwd, registry=registry)
    assert resolved["NAME"] == "mybranch"


def test_set_type_uses_default(registry, checkout):
    _write_recipe(checkout, """
[resources.MODE]
type    = "set"
default = "dev"
""")
    resolved = sd.provision(checkout, registry=registry)
    assert resolved["MODE"] == "dev"


def test_set_type_persists_user_value(registry, checkout):
    _write_recipe(checkout, """
[resources.MODE]
type = "set"
""")
    registry.set_kv(str(checkout.resolve()), "MODE", "prod")
    resolved = sd.provision(checkout, registry=registry)
    assert resolved["MODE"] == "prod"


def test_toml_quoting_escapes_specials():
    assert sd._toml_quote("a\\b") == r'"a\\b"'
    assert sd._toml_quote('he said "hi"') == r'"he said \"hi\""'
    assert sd._toml_quote("line\nbreak") == r'"line\nbreak"'


# ---------- writers helper ----------

def test_find_table_locates_env(tmp_path):
    lines = ["[tools]", "node = \"20\"", "", "[env]", "X = \"1\"", "Y = \"2\"", "", "[other]", "k = 1"]
    s, e = sd._find_table(lines, "env")
    assert s == 3
    assert e == 7


def test_find_table_missing(tmp_path):
    lines = ["[tools]", "node = \"20\""]
    s, e = sd._find_table(lines, "env")
    assert s is None


# ---------- recipe: devices + project ----------

def test_recipe_parses_project(tmp_path):
    p = tmp_path / "splashdown.toml"
    p.write_text('[project]\nframework = "flutter"\n')
    r = sd.Recipe.load(p)
    assert r.project["framework"] == "flutter"


def test_resolve_device_name_template(tmp_path):
    cwd = tmp_path / "feat-y"; cwd.mkdir()
    spec = {"name": "{{ basename(parent) }}-{{ cwd }}"}
    out = sd._resolve_device_name(spec, cwd, "default")
    assert out == f"{tmp_path.name}-feat-y"


def test_resolve_device_name_default_uses_variant_suffix(tmp_path):
    cwd = tmp_path / "feat-z"; cwd.mkdir()
    out = sd._resolve_device_name({}, cwd, "small-screen")
    assert out == f"{tmp_path.name}/feat-z/small-screen"


def test_resolve_device_name_sanitized_for_android(tmp_path):
    """avdmanager rejects '/' in names; the default path-derived name has two slashes."""
    cwd = tmp_path / "feat-z"; cwd.mkdir()
    out = sd._resolve_device_name({}, cwd, "default", dtype="emulator")
    assert "/" not in out
    assert out == f"{tmp_path.name}_feat-z_default"


def test_resolve_device_name_ios_keeps_slashes(tmp_path):
    """iOS sims accept '/' so we preserve the human-readable separators."""
    cwd = tmp_path / "feat-z"; cwd.mkdir()
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
    cwd = tmp_path / "myapp.feat-x"; cwd.mkdir()
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


# ---------- merged_devices ----------

def test_merged_devices_unions_recipe_and_local(tmp_path):
    r = sd.Recipe(
        {"devices": {"simulator": {"default": {"model": "iPhone 17"}}}},
        tmp_path / "splashdown.toml",
    )
    lc = sd.LocalConfig(
        {"devices": {"simulator": {"repro-bug": {"model": "iPhone 16"}}}},
        tmp_path / "splashdown.local.toml",
    )
    merged = sd.merged_devices(r, lc)
    assert set(merged["simulator"]) == {"default", "repro-bug"}


def test_merged_devices_collision_errors(tmp_path):
    r = sd.Recipe(
        {"devices": {"simulator": {"default": {"model": "A"}}}},
        tmp_path / "splashdown.toml",
    )
    lc = sd.LocalConfig(
        {"devices": {"simulator": {"default": {"model": "B"}}}},
        tmp_path / "splashdown.local.toml",
    )
    with pytest.raises(ValueError, match="already exists in recipe"):
        sd.merged_devices(r, lc)


def test_recipe_accepts_nested_device_variants(tmp_path):
    p = tmp_path / "splashdown.toml"
    p.write_text("""
[devices.simulator.default]
model = "iPhone 17"

[devices.simulator.lowest-supported]
model = "iPhone 12"
""")
    r = sd.Recipe.load(p)
    assert set(r.devices["simulator"]) == {"default", "lowest-supported"}
    assert r.devices["simulator"]["default"]["model"] == "iPhone 17"


def test_recipe_rejects_flat_device_shape(tmp_path):
    p = tmp_path / "splashdown.toml"
    p.write_text('[devices.iphone]\ntype = "simulator"\n')
    with pytest.raises(ValueError, match=r"flat device shape"):
        sd.Recipe.load(p)


def test_recipe_rejects_unknown_device_type(tmp_path):
    p = tmp_path / "splashdown.toml"
    p.write_text('[devices.cardboard-vr.default]\nmodel = "Pixel"\n')
    with pytest.raises(ValueError, match="unknown device type"):
        sd.Recipe.load(p)


def test_localconfig_accepts_nested_device_variants(tmp_path):
    p = tmp_path / "splashdown.local.toml"
    p.write_text("""
[devices.simulator.repro-bug]
model = "iPhone 16"
ios   = "17.5"
""")
    lc = sd.LocalConfig.load(p)
    assert lc.devices["simulator"]["repro-bug"]["ios"] == "17.5"


# ---------- CLI ----------

def test_file_name_constants():
    assert sd.RECIPE_NAME == "splashdown.toml"
    assert sd.LOCAL_NAME == "splashdown.local.toml"
    assert sd.ENV_FILE_NAME == "splashdown.env"


def test_cli_prog_name_is_splash():
    assert sd._build_parser().prog == "splash"


def test_cli_help_shows_subcommands(capsys):
    with pytest.raises(SystemExit):
        sd.main(["--help"])
    out = capsys.readouterr().out
    assert "provision" in out
    assert "device" in out
    assert "init" in out


def test_localconfig_missing_file_is_empty(tmp_path):
    lc = sd.LocalConfig.load(tmp_path / "splashdown.local.toml")
    assert lc.devices == {}


def test_localconfig_rejects_bad_variant_name(tmp_path):
    p = tmp_path / "splashdown.local.toml"
    p.write_text('[devices.simulator."has spaces"]\nmodel = "iPhone"\n')
    with pytest.raises(ValueError, match="variant name"):
        sd.LocalConfig.load(p)


def test_init_writes_recipe_and_local_skeleton(tmp_path):
    sd.cmd_init(tmp_path, preset="rn")
    recipe = (tmp_path / "splashdown.toml").read_text()
    assert "[resources." in recipe
    assert (tmp_path / "splashdown.local.toml").exists()


def test_init_does_not_clobber_existing_local(tmp_path):
    (tmp_path / "splashdown.local.toml").write_text("[devices.mine]\ntype = \"simulator\"\n")
    sd.cmd_init(tmp_path, preset="rn")
    assert "devices.mine" in (tmp_path / "splashdown.local.toml").read_text()


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
    cwd = tmp_path / "co"; cwd.mkdir()
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
    cwd = tmp_path / "co"; cwd.mkdir()
    (cwd / "splashdown.toml").write_text("""
[resources.PORT]
type  = "port"
range = [18900, 18910]
""")
    sd.main(["--cwd", str(cwd)])
    assert (cwd / "splashdown.local.toml").exists()
    assert "devices.simulator" in (cwd / "splashdown.local.toml").read_text()


def test_cli_provision_preserves_existing_local(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cwd = tmp_path / "co"; cwd.mkdir()
    (cwd / "splashdown.toml").write_text("""
[resources.PORT]
type  = "port"
range = [18920, 18930]
""")
    (cwd / "splashdown.local.toml").write_text('[devices.mine]\ntype = "simulator"\n')
    sd.main(["--cwd", str(cwd)])
    assert "devices.mine" in (cwd / "splashdown.local.toml").read_text()


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
    sd.cmd_init(tmp_path, preset="minimal")
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
    sd.cmd_init(tmp_path, preset="minimal")
    sd.cmd_init(tmp_path, preset="minimal", force=True)
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
    (tmp_path / "lefthook.yml").write_text("pre-commit:\n  commands:\n    lint:\n      run: echo lint\n")
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
    (tmp_path / "lefthook.yml").write_text("pre-commit:\n  commands:\n    lint:\n      run: echo lint\n")
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
    (tmp_path / "metro.config.js").write_text(
        "module.exports = { server: { port: 8083 } };\n"
    )
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
    (tmp_path / "metro.config.js").write_text(
        "const config = { server: { port: 8083 } };\n"
    )
    sd._rn_metro_autofix(tmp_path)
    once = (tmp_path / "metro.config.js").read_text()
    sd._rn_metro_autofix(tmp_path)
    twice = (tmp_path / "metro.config.js").read_text()
    assert once == twice
    assert twice.count("process.env.RCT_METRO_PORT") == 1


def test_rn_metro_autofix_noop_when_no_port(tmp_path):
    text = "module.exports = { server: { someOtherThing: 1 } };\n"
    (tmp_path / "metro.config.js").write_text(text)
    sd._rn_metro_autofix(tmp_path)
    assert (tmp_path / "metro.config.js").read_text() == text
    # Detect still reports problem; manual instructions will be printed by doctor.
    assert sd._rn_metro_detect(tmp_path)[0] == "problem"


# ---------- rn-pkg-port check ----------

def test_rn_pkg_not_applicable_without_pkg(tmp_path):
    assert sd._rn_pkg_applies(tmp_path) is False


def test_rn_pkg_detect_ok_when_clean(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "scripts": {"start": "react-native start", "ios": "react-native run-ios"}
    }))
    assert sd._rn_pkg_detect(tmp_path)[0] == "ok"


def test_rn_pkg_detect_problem_with_space_form(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "scripts": {"start": "react-native start --port 8083"}
    }))
    status, detail = sd._rn_pkg_detect(tmp_path)
    assert status == "problem"
    assert "start" in detail


def test_rn_pkg_detect_problem_with_equals_form(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "scripts": {"ios": "react-native run-ios --port=8083"}
    }))
    assert sd._rn_pkg_detect(tmp_path)[0] == "problem"


def test_rn_pkg_autofix_strips_port_flag(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "x",
        "scripts": {
            "android": "react-native run-android --port 8083",
            "ios": "react-native run-ios --port 8083",
            "start": "react-native start --port 8083",
            "test": "jest",
        },
        "dependencies": {"react-native": "0.83"},
    }, indent=2))
    sd._rn_pkg_autofix(tmp_path)
    data = json.loads((tmp_path / "package.json").read_text())
    assert data["scripts"]["start"] == "react-native start"
    assert data["scripts"]["ios"] == "react-native run-ios"
    assert data["scripts"]["android"] == "react-native run-android"
    assert data["scripts"]["test"] == "jest"  # unrelated script preserved
    assert data["dependencies"]["react-native"] == "0.83"  # rest of file preserved
    assert sd._rn_pkg_detect(tmp_path)[0] == "ok"


def test_rn_pkg_autofix_idempotent(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "scripts": {"start": "react-native start --port 8083"}
    }))
    sd._rn_pkg_autofix(tmp_path)
    once = (tmp_path / "package.json").read_text()
    sd._rn_pkg_autofix(tmp_path)
    twice = (tmp_path / "package.json").read_text()
    assert once == twice


def test_rn_pkg_targets_react_native_scripts_by_command(tmp_path):
    # An unconventional script name that still invokes react-native should be caught.
    (tmp_path / "package.json").write_text(json.dumps({
        "scripts": {"dev": "react-native start --port 8083"}
    }))
    assert sd._rn_pkg_detect(tmp_path)[0] == "problem"
    sd._rn_pkg_autofix(tmp_path)
    assert json.loads((tmp_path / "package.json").read_text())["scripts"]["dev"] == "react-native start"


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
        "# header\n"
        "export NODE_BINARY=node\n"
        "\n"
        "# Pin Metro port\n"
        "export RCT_METRO_PORT=8083\n",
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
    _make_ios(tmp_path, (
        "export NODE_BINARY=node\n"
        "if [ -z \"${RCT_METRO_PORT:-}\" ] && [ -f \"${SRCROOT}/../splashdown.env\" ]; then\n"
        "  export RCT_METRO_PORT=\"$(grep '^RCT_METRO_PORT=' \"${SRCROOT}/../splashdown.env\" | cut -d= -f2)\"\n"
        "fi\n"
        "export RCT_METRO_PORT=\"${RCT_METRO_PORT:-8083}\"\n"
    ))
    assert sd._rn_xcode_detect(tmp_path)[0] == "ok"


def test_rn_xcode_autofix_noop_when_already_referencing_splashdown(tmp_path):
    content = (
        "export NODE_BINARY=node\n"
        "if [ -f \"${SRCROOT}/../splashdown.env\" ]; then\n"
        "  . \"${SRCROOT}/../splashdown.env\"\n"
        "fi\n"
    )
    _make_ios(tmp_path, content)
    sd._rn_xcode_autofix(tmp_path)
    assert (tmp_path / "ios" / ".xcode.env").read_text() == content


def test_cmd_init_rn_preset_wires_everything(tmp_path):
    """`splash init --preset=rn` in an RN-shaped repo scaffolds AND wires."""
    _git_init(tmp_path)
    # RN-shaped repo before splashdown.
    (tmp_path / "package.json").write_text(json.dumps({
        "scripts": {
            "start": "react-native start --port 8083",
            "ios": "react-native run-ios --port 8083",
        },
        "dependencies": {"react-native": "0.83"},
        "devDependencies": {"lefthook": "^1.0"},
    }, indent=2))
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
    # Run init — should scaffold + wire.
    sd.cmd_init(tmp_path, preset="rn")
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
    r = _sp.run(["git", "-C", str(tmp_path), "config", "--get", "core.hooksPath"], capture_output=True)
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
    (tmp_path / "package.json").write_text(json.dumps({
        "scripts": {
            "start": "react-native start --port 8083",
            "ios": "react-native run-ios --port 8083",
            "android": "react-native run-android --port 8083",
        },
        "dependencies": {"react-native": "0.83"},
        "devDependencies": {"lefthook": "^1.0"},
    }, indent=2))
    (tmp_path / "metro.config.js").write_text(
        "const config = {\n  server: {\n    port: 8083,\n  },\n};\n"
        "module.exports = config;\n"
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
