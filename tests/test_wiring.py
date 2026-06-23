"""Tests for splashdown wiring behavior."""

from __future__ import annotations

import json
import os

import pytest

import splashdown as sd
from conftest import (
    _git_init,
    _make_ios,
)


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
