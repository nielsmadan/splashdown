"""Tests for splashdown commands behavior."""

from __future__ import annotations

import json
import subprocess

import pytest

import splashdown as sd
from conftest import (
    _IPHONE,
    _inv_none,
    _stub_physical,
    _write_physical_recipe,
)


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


def test_cli_destroy_confirms_before_deleting(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "splashdown.toml").write_text('[targets.simulator.default]\nmodel = "iPhone 15"\n')
    destroyed: list = []
    monkeypatch.setattr(sd.commands, "device_destroy", lambda dt, name: destroyed.append(name))
    monkeypatch.setattr(sd.commands, "_resolve_device_name", lambda *a, **k: "SIM-NAME")

    # Declining at the prompt aborts without touching the device.
    monkeypatch.setattr("builtins.input", lambda: "n")
    assert sd.main(["--cwd", str(tmp_path), "destroy", "simulator"]) == 1
    assert destroyed == []

    # --yes skips the prompt and destroys.
    assert sd.main(["--cwd", str(tmp_path), "destroy", "simulator", "--yes"]) == 0
    assert destroyed == ["SIM-NAME"]


def test_cli_status_hints_unfilled_set_resource(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "splashdown.toml").write_text('[resources.MODE]\ntype = "set"\n')
    assert sd.main(["--cwd", str(tmp_path), "status"]) == 0
    err = capsys.readouterr().err
    assert "MODE" in err and "splash env set" in err


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


_TWO_VARIANT_RECIPE = (
    '[targets.simulator.default]\nmodel = "iPhone 17"\n'
    '[targets.simulator.large-screen]\nmodel = "iPhone 17 Pro Max"\n'
)


def test_resolve_variant_for_cli_prefix_resolves(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / sd.RECIPE_NAME).write_text(_TWO_VARIANT_RECIPE)
    variant, spec, _ = sd.commands._resolve_variant_for_cli(tmp_path, "simulator", "lar")
    assert variant == "large-screen"
    assert spec["model"] == "iPhone 17 Pro Max"


def test_resolve_variant_for_cli_prefix_disabled_errors(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg" / "splashdown"
    cfg.mkdir(parents=True)
    (cfg / "config.toml").write_text("[settings]\nprefix_match = false\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / sd.RECIPE_NAME).write_text(_TWO_VARIANT_RECIPE)
    with pytest.raises(sd.DeviceError, match="no variant `lar`"):
        sd.commands._resolve_variant_for_cli(tmp_path, "simulator", "lar")


def test_resolve_variant_for_cli_short_variant_prefix_resolves(tmp_path):
    # `splash run d` in a sim-only project: `d` stays a variant token (not the
    # `device` type) and resolves the `default` variant by prefix.
    (tmp_path / sd.RECIPE_NAME).write_text('[targets.simulator.default]\nmodel = "iPhone 17"\n')
    variant, _, _ = sd.commands._resolve_variant_for_cli(tmp_path, "simulator", "d")
    assert variant == "default"


def test_declared_target_types_lists_declared(tmp_path):
    (tmp_path / sd.RECIPE_NAME).write_text(
        '[targets.simulator.default]\nmodel = "iPhone 17"\n[targets.emulator.default]\n'
    )
    assert sorted(sd.commands._declared_target_types(tmp_path)) == ["emulator", "simulator"]
