"""Tests for splashdown profiles behavior."""

from __future__ import annotations

import json
import shlex

import pytest

import splashdown as sd
from conftest import (
    _capture_profile_calls,
)


def test_flutter_run_builds_argv(tmp_path, monkeypatch):
    calls = _capture_profile_calls(monkeypatch)
    rc = sd.runners._flutter_run(
        tmp_path, sd.Recipe({}, tmp_path / "x.toml"), {"kind": "ios", "udid": "U1"}
    )
    assert rc == 0
    assert ["flutter", "run", "-d", "U1"] in calls


def test_rn_run_ios_and_android(tmp_path, monkeypatch):
    calls = _capture_profile_calls(monkeypatch)
    r = sd.Recipe({}, tmp_path / "x.toml")
    sd.runners._rn_run(tmp_path, r, {"kind": "ios", "udid": "U1"})
    sd.runners._rn_run(tmp_path, r, {"kind": "android", "serial": "S1"})
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
    sd.runners._rn_run(tmp_path, r, {"kind": "ios", "udid": "U1"})
    sd.runners._rn_run(tmp_path, r, {"kind": "android", "serial": "S1"})
    flat = [" ".join(c) for c in calls]
    assert any(
        "run-ios" in c and "--scheme DreamHackDev" in c and "--mode Debug" in c for c in flat
    )
    assert any("run-android" in c and "--mode developmentDebug" in c for c in flat)


def test_rn_run_rejects_flaglike_scheme(tmp_path, monkeypatch):
    _capture_profile_calls(monkeypatch)
    r = sd.Recipe({"project": {"ios": {"scheme": "-evil"}}}, tmp_path / "x.toml")
    try:
        sd.runners._rn_run(tmp_path, r, {"kind": "ios", "udid": "U1"})
    except sd.DeviceError:
        return
    raise AssertionError("expected DeviceError for flag-like scheme")


def _write_pods_excluded_arch(tmp_path):
    cfg = tmp_path / "ios" / "Pods" / "Target Support Files" / "MLKit" / "MLKit.debug.xcconfig"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("EXCLUDED_ARCHS[sdk=iphonesimulator*] = arm64\n")


def _fake_x86_64_target(monkeypatch, value):
    monkeypatch.setattr(sd.devices, "ios_x86_64_target", lambda: value)


def test_rn_run_prints_arch_hint_on_failure(tmp_path, monkeypatch, capsys):
    _write_pods_excluded_arch(tmp_path)
    _fake_x86_64_target(monkeypatch, ("18.5", "iPhone 16 Pro"))
    monkeypatch.setattr(sd.runners.subprocess, "call", lambda args, **k: 70)
    rc = sd.runners._rn_run(
        tmp_path, sd.Recipe({}, tmp_path / "x.toml"), {"kind": "ios", "udid": "U1"}
    )
    assert rc == 70
    err = capsys.readouterr().err
    assert 'ios = "18.5"' in err
    # The model must ride along: device types are per-runtime, so pinning `ios`
    # against the default `iPhone 17` model fails `simctl create`.
    assert 'model = "iPhone 16 Pro"' in err


def test_rn_arch_hint_tracks_installed_runtime(tmp_path, monkeypatch, capsys):
    _write_pods_excluded_arch(tmp_path)
    _fake_x86_64_target(monkeypatch, ("17.5", "iPhone 15 Pro"))
    monkeypatch.setattr(sd.runners.subprocess, "call", lambda args, **k: 70)
    sd.runners._rn_run(tmp_path, sd.Recipe({}, tmp_path / "x.toml"), {"kind": "ios", "udid": "U1"})
    err = capsys.readouterr().err
    assert 'ios = "17.5"' in err
    assert 'model = "iPhone 15 Pro"' in err
    assert "18.5" not in err


def test_rn_arch_hint_when_no_x86_64_runtime_installed(tmp_path, monkeypatch, capsys):
    _write_pods_excluded_arch(tmp_path)
    _fake_x86_64_target(monkeypatch, None)
    monkeypatch.setattr(sd.runners.subprocess, "call", lambda args, **k: 70)
    sd.runners._rn_run(tmp_path, sd.Recipe({}, tmp_path / "x.toml"), {"kind": "ios", "udid": "U1"})
    err = capsys.readouterr().err
    assert "no installed runtime has an x86_64 slice" in err
    # No snippet to copy: there is no version to pin, so none may be invented.
    assert 'ios = "' not in err


def test_rn_run_no_arch_hint_on_success_or_missing_pods(tmp_path, monkeypatch, capsys):
    _fake_x86_64_target(monkeypatch, ("18.5", "iPhone 16 Pro"))
    # Success → no hint even when the exclusion is present.
    _write_pods_excluded_arch(tmp_path)
    monkeypatch.setattr(sd.runners.subprocess, "call", lambda args, **k: 0)
    sd.runners._rn_run(tmp_path, sd.Recipe({}, tmp_path / "x.toml"), {"kind": "ios", "udid": "U1"})
    assert "18.5" not in capsys.readouterr().err
    # Failure but no Pods → no hint.
    other = tmp_path / "no_pods"
    other.mkdir()
    monkeypatch.setattr(sd.runners.subprocess, "call", lambda args, **k: 70)
    sd.runners._rn_run(other, sd.Recipe({}, other / "x.toml"), {"kind": "ios", "udid": "U1"})
    assert "18.5" not in capsys.readouterr().err


def test_expo_run_ios_and_android(tmp_path, monkeypatch):
    calls = _capture_profile_calls(monkeypatch)
    r = sd.Recipe({}, tmp_path / "x.toml")
    sd.runners._expo_run(tmp_path, r, {"kind": "ios", "udid": "U1"})
    sd.runners._expo_run(tmp_path, r, {"kind": "android", "serial": "S1"})
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
    assert res["RCT_METRO_PORT"]["range"] == [8082, 8200]


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

    monkeypatch.setattr(sd.runners.subprocess, "run", lambda *a, **k: _Done())
    rc = sd.runners._ios_native_run(tmp_path, recipe, {"kind": "ios", "udid": "SIM-1"})
    assert rc == 0
    flat = [" ".join(c) for c in calls]
    assert any("simctl install SIM-1" in c for c in flat)
    assert any("simctl launch SIM-1 com.demo" in c for c in flat)
    assert not any("devicectl" in c for c in flat)


def test_android_native_run_monkey_launcher(tmp_path, monkeypatch):
    recipe = sd.Recipe({"project": {"android": {}}}, tmp_path / "splashdown.toml")
    calls = _capture_profile_calls(monkeypatch)
    monkeypatch.setattr(
        sd.runners.subprocess, "check_output", lambda *a, **k: "applicationId: com.example.app\n"
    )
    rc = sd.runners._android_native_run(
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
    rc = sd.runners._android_native_run(tmp_path, recipe, {"kind": "android", "serial": "S1"})
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
        sd.runners._android_native_run(tmp_path, recipe, {"kind": "android", "serial": "S1"})


def _recipe(tmp_path, project):
    return sd.Recipe({"project": project}, tmp_path / "splashdown.toml")


def test_resolve_custom_run_string_is_shared(tmp_path):
    r = _recipe(tmp_path, {"run": "flutter run -d {device_id}"})
    assert sd.runners._resolve_custom_run(r, "ios") == "flutter run -d {device_id}"
    assert sd.runners._resolve_custom_run(r, "android") == "flutter run -d {device_id}"


def test_resolve_custom_run_table_per_platform(tmp_path):
    r = _recipe(tmp_path, {"run": {"ios": "run-ios", "android": "run-android"}})
    assert sd.runners._resolve_custom_run(r, "ios") == "run-ios"
    assert sd.runners._resolve_custom_run(r, "android") == "run-android"


def test_resolve_custom_run_none_when_absent(tmp_path):
    assert sd.runners._resolve_custom_run(_recipe(tmp_path, {}), "ios") is None


def test_resolve_custom_run_missing_platform_falls_back(tmp_path):
    # A table that customizes only one platform leaves the other on auto-detection.
    r = _recipe(tmp_path, {"run": {"ios": "run-ios"}})
    assert sd.runners._resolve_custom_run(r, "android") is None


def test_resolve_custom_run_bad_type_raises(tmp_path):
    with pytest.raises(ValueError, match=r"\[project\.run\]"):
        _recipe(tmp_path, {"run": 123})


def test_resolve_custom_run_empty_string_raises(tmp_path):
    with pytest.raises(ValueError, match=r"\[project\.run\]"):
        _recipe(tmp_path, {"run": ""})


def test_resolve_custom_run_empty_table_value_raises(tmp_path):
    with pytest.raises(ValueError, match=r"\[project\.run\.ios\]"):
        _recipe(tmp_path, {"run": {"ios": "  "}})


def test_resolve_custom_run_non_string_table_value_raises(tmp_path):
    with pytest.raises(ValueError, match=r"\[project\.run\.ios\]"):
        _recipe(tmp_path, {"run": {"ios": 123}})


def test_substitute_missing_device_id_raises():
    # {device_id} present but neither udid nor serial available -> loud error,
    # not a silent `-d ''`.
    with pytest.raises(sd.DeviceError, match="device_id"):
        sd.runners._substitute_run_placeholders("run -d {device_id}", {"kind": "ios"})


def test_substitute_device_id_ios_and_android():
    tpl = "launch --device {device_id}"
    assert sd.runners._substitute_run_placeholders(tpl, {"kind": "ios", "udid": "ABCD"}) == (
        "launch --device ABCD"
    )
    assert (
        sd.runners._substitute_run_placeholders(tpl, {"kind": "android", "serial": "emulator-5554"})
        == "launch --device emulator-5554"
    )


def test_substitute_name_is_shell_quoted_and_platform(monkeypatch):
    out = sd.runners._substitute_run_placeholders(
        "run {platform} {device_name}",
        {"kind": "ios", "udid": "U", "name": "Alice's iPhone"},
    )
    # {platform} substituted raw; {device_name} shell-quoted so the apostrophe/space
    # can't break the command.
    assert out.startswith("run ios ")
    assert shlex.split(out)[-1] == "Alice's iPhone"


def test_substitute_leaves_unknown_braces_untouched():
    out = sd.runners._substitute_run_placeholders(
        "echo {device_id} {foo}", {"kind": "ios", "udid": "U"}
    )
    assert out == "echo U {foo}"


def test_run_custom_command_executes_with_shell(tmp_path, monkeypatch):
    captured = {}

    def _fake_call(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(sd.runners.subprocess, "call", _fake_call)
    r = _recipe(tmp_path, {"run": {"ios": "yarn rn run-ios --udid {device_id}"}})
    rc = sd.runners.run_custom_command(tmp_path, r, {"kind": "ios", "udid": "ABCD"})
    assert rc == 0
    assert captured["cmd"] == "yarn rn run-ios --udid ABCD"
    assert captured["kwargs"].get("shell") is True
    assert captured["kwargs"].get("cwd") == tmp_path


def test_run_custom_command_none_when_no_run(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sd.runners.subprocess, "call", lambda *a, **k: pytest.fail("should not run")
    )
    assert sd.runners.run_custom_command(tmp_path, _recipe(tmp_path, {}), {"kind": "ios"}) is None


def _astro_check(path):
    app = sd.AppInventory(name="web", path=path, profile="astro")
    return next(c for c in sd.PROFILES["astro"].wiring_checks(app) if c.id == "astro-config-port")


def test_astro_detects_config_file(tmp_path):
    (tmp_path / "astro.config.mjs").write_text("export default {}\n")
    r = sd.Recipe({"project": {}}, tmp_path / "splashdown.toml")
    assert sd.detect_framework(tmp_path, r) == "astro"


def test_astro_detects_package_dependency(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies": {"astro": "^5"}}')
    r = sd.Recipe({"project": {}}, tmp_path / "splashdown.toml")
    assert sd.detect_framework(tmp_path, r) == "astro"


def test_astro_port_range_skips_the_framework_default(tmp_path):
    app = sd.AppInventory(name="web", path=tmp_path, profile="astro")
    lo, hi = sd.PROFILES["astro"].resources(app)["WEB_DEV_PORT"]["range"]
    assert lo > 4321, "an unwired app handed 4321 is indistinguishable from a wired one"
    assert lo < hi


def test_astro_autofix_injects_server_port(tmp_path):
    cfg = tmp_path / "astro.config.mjs"
    cfg.write_text(
        'import { defineConfig } from "astro/config";\n'
        "export default defineConfig({\n  integrations: [],\n});\n"
    )
    check = _astro_check(tmp_path)
    assert check.detect(tmp_path)[0] == "problem"
    check.autofix(tmp_path)
    text = cfg.read_text()
    assert "server: { port: Number(process.env.WEB_DEV_PORT) || 4321 }" in text
    assert "integrations: []" in text  # existing config preserved
    assert check.detect(tmp_path)[0] == "ok"


def test_astro_autofix_handles_bare_default_export(tmp_path):
    cfg = tmp_path / "astro.config.mjs"
    cfg.write_text("export default {\n  site: 'https://example.com',\n};\n")
    check = _astro_check(tmp_path)
    check.autofix(tmp_path)
    assert check.detect(tmp_path)[0] == "ok"
    assert "site: 'https://example.com'" in cfg.read_text()


def test_astro_autofix_leaves_an_existing_server_block_alone(tmp_path):
    # The block may be nested under `vite:`, where a port would configure Vite's
    # server rather than Astro's — report instead of guessing at the nesting.
    cfg = tmp_path / "astro.config.mjs"
    original = (
        'import { defineConfig } from "astro/config";\n'
        "export default defineConfig({\n  vite: { server: { hmr: true } },\n});\n"
    )
    cfg.write_text(original)
    check = _astro_check(tmp_path)
    check.autofix(tmp_path)
    assert cfg.read_text() == original
    assert check.detect(tmp_path)[0] == "problem"
    assert "vite:" in check.manual_instructions(tmp_path)


def test_astro_autofix_leaves_unrecognized_shape_alone(tmp_path):
    cfg = tmp_path / "astro.config.mjs"
    original = "const cfg = makeConfig();\nexport default cfg;\n"
    cfg.write_text(original)
    check = _astro_check(tmp_path)
    check.autofix(tmp_path)
    assert cfg.read_text() == original


@pytest.mark.parametrize(
    "body",
    [
        "export default { server: { port: Number(process.env.WEB_DEV_PORT) || 4321 } };\n",
        'export default { server: { port: Number(process.env["WEB_DEV_PORT"]) } };\n',
        "const { WEB_DEV_PORT } = process.env;\nexport default { server: { port: WEB_DEV_PORT } };\n",
    ],
)
def test_astro_check_passes_on_wired_configs(tmp_path, body):
    (tmp_path / "astro.config.mjs").write_text(body)
    assert _astro_check(tmp_path).detect(tmp_path)[0] == "ok"


def test_compose_resources_only_when_a_compose_file_exists(tmp_path):
    assert sd.compose_project_resources(tmp_path) == {}
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    res = sd.compose_project_resources(tmp_path)
    assert res["COMPOSE_PROJECT_NAME"]["type"] == "template"


def test_compose_project_name_is_unique_per_checkout(tmp_path):
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    tpl = sd.compose_project_resources(tmp_path)["COMPOSE_PROJECT_NAME"]["template"]
    one = sd.render_template(tpl, sd._make_scope(tmp_path / "wrk" / "dev1", None, {}))
    two = sd.render_template(tpl, sd._make_scope(tmp_path / "wrk" / "dev2", None, {}))
    assert one != two
    assert one == "wrk-dev1"


def test_compose_check_flags_hardcoded_ports_and_container_name(tmp_path):
    (tmp_path / "compose.yaml").write_text("""\
services:
  db:
    image: postgres:16
    container_name: myapp_db
    ports:
      - "5432:5432"
""")
    check = sd.compose_wiring_checks(tmp_path)[0]
    status, detail = check.detect(tmp_path)
    assert status == "problem"
    assert "5432" in detail
    assert "myapp_db" in detail
    assert check.autofix is None  # YAML rewriting is not safely mechanical


def test_compose_check_passes_on_a_templated_file(tmp_path):
    (tmp_path / "compose.yaml").write_text("""\
services:
  db:
    image: postgres:16
    ports:
      - "${DB_PORT:-5432}:5432"
""")
    check = sd.compose_wiring_checks(tmp_path)[0]
    assert check.detect(tmp_path)[0] == "ok"


def test_compose_check_allows_a_templated_container_name(tmp_path):
    (tmp_path / "compose.yaml").write_text(
        'services:\n  db:\n    container_name: "${COMPOSE_PROJECT_NAME}_db"\n'
    )
    assert sd.compose_wiring_checks(tmp_path)[0].detect(tmp_path)[0] == "ok"


def _compose_detect(tmp_path, body):
    (tmp_path / "compose.yaml").write_text(body)
    return sd.compose_wiring_checks(tmp_path)[0].detect(tmp_path)


def test_compose_check_flags_flow_style_ports(tmp_path):
    status, detail = _compose_detect(
        tmp_path,
        "services:\n  db:\n    ports: ['5432:5432']\n  db-test:\n    ports: [\"5433:5432\"]\n",
    )
    assert status == "problem"
    assert "5432" in detail
    assert "5433" in detail


def test_compose_check_passes_on_templated_flow_style_ports(tmp_path):
    status, _ = _compose_detect(
        tmp_path, 'services:\n  db:\n    ports: ["${DB_PORT:-5432}:5432", "${X}:80"]\n'
    )
    assert status == "ok"


def test_compose_check_flags_an_inline_container_name(tmp_path):
    status, detail = _compose_detect(
        tmp_path, "services:\n  db: { container_name: myapp_db, image: postgres:16 }\n"
    )
    assert status == "problem"
    assert "myapp_db" in detail
    assert "postgres" not in detail  # the value stops at the flow-mapping comma


def test_compose_check_allows_an_inline_templated_container_name(tmp_path):
    status, _ = _compose_detect(
        tmp_path, 'services:\n  db: { container_name: "${COMPOSE_PROJECT_NAME}_db" }\n'
    )
    assert status == "ok"


def test_compose_check_flags_a_hardcoded_long_syntax_published_port(tmp_path):
    status, detail = _compose_detect(
        tmp_path,
        "services:\n  db:\n    ports:\n      - target: 5432\n        published: 5433\n"
        "        protocol: tcp\n",
    )
    assert status == "problem"
    assert "5433" in detail


def test_compose_check_allows_long_syntax_without_a_fixed_host_port(tmp_path):
    status, _ = _compose_detect(
        tmp_path,
        "services:\n  db:\n    ports:\n      - target: 5432\n        published: ${DB_PORT}\n"
        "      - target: 80\n        protocol: tcp\n",
    )
    assert status == "ok"


def test_compose_check_allows_a_container_only_port(tmp_path):
    # `- "3000"` publishes to an ephemeral host port; nothing to collide over.
    status, _ = _compose_detect(tmp_path, 'services:\n  api:\n    ports:\n      - "3000"\n')
    assert status == "ok"


def test_compose_check_flags_a_host_ip_qualified_port(tmp_path):
    status, detail = _compose_detect(
        tmp_path, 'services:\n  db:\n    ports:\n      - "127.0.0.1:5432:5432/tcp"\n'
    )
    assert status == "problem"
    assert "5432" in detail


def test_compose_check_ignores_commented_out_ports(tmp_path):
    # The comment must be indented *inside* the block: at column 0 the dedent rule
    # discards it before stripping is ever consulted, so the test proved nothing.
    status, _ = _compose_detect(
        tmp_path,
        'services:\n  db:\n    image: postgres:16  # ports: ["5432:5432"]\n'
        '    ports:\n      # - "5432:5432"\n      - "${DB_PORT}:5432"\n',
    )
    assert status == "ok"


def test_compose_check_flags_a_block_sequence_at_the_key_indent(tmp_path):
    # Legal and common YAML: the `-` items sit at `ports:`'s own column. The
    # region walker used to stop dead here and report the file clean.
    status, detail = _compose_detect(
        tmp_path,
        'services:\n  db:\n    image: postgres:16\n    ports:\n    - "5432:5432"\n',
    )
    assert status == "problem"
    assert "5432" in detail


def test_compose_check_flags_ports_inside_an_inline_flow_mapping(tmp_path):
    status, detail = _compose_detect(
        tmp_path, "services:\n  db: { image: postgres:16, ports: ['5432:5432'] }\n"
    )
    assert status == "problem"
    assert "5432" in detail


def test_compose_check_flags_a_pinned_host_with_a_templated_container_port(tmp_path):
    # Testing the whole entry for `$` passed this: the host side is still pinned.
    status, detail = _compose_detect(
        tmp_path, 'services:\n  db:\n    ports:\n      - "5432:${CONTAINER_PORT}"\n'
    )
    assert status == "problem"
    assert "5432" in detail


def test_compose_check_allows_an_empty_host_slot(tmp_path):
    # `127.0.0.1::5432` binds loopback on an ephemeral host port: valid, not unreadable.
    status, _ = _compose_detect(
        tmp_path, 'services:\n  db:\n    ports:\n      - "127.0.0.1::5432"\n'
    )
    assert status == "ok"


def test_compose_check_reports_a_ports_shape_it_cannot_read(tmp_path):
    # The failure mode that matters: never claim safety on a pattern not understood.
    status, detail = _compose_detect(
        tmp_path,
        "x-ports: &shared\n  - '5432:5432'\nservices:\n  db:\n    ports: *shared\n",
    )
    assert status == "problem"
    assert "*shared" in detail


def test_compose_check_allows_an_empty_ports_list(tmp_path):
    status, _ = _compose_detect(tmp_path, "services:\n  db:\n    ports: []\n")
    assert status == "ok"


def test_compose_wiring_checks_empty_without_a_compose_file(tmp_path):
    assert sd.compose_wiring_checks(tmp_path) == []
