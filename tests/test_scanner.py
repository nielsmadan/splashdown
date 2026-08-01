"""Tests for splashdown scanner behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import splashdown as sd


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


def test_refresh_inventory_rejects_unknown_resource_key_without_writing(tmp_path):
    path = tmp_path / "splashdown.toml"
    path.write_text("""\
# top-of-file comment
[project]
workspace = "single"
loader = "mise"

[apps.main]
path = "."
profile = "unknown"
resources = ["MY_PORT_WITH_UNDERSCORES"]

[resources.MY_PORT_WITH_UNDERSCORES]
type = "port"
range = [9000, 9010]
# user note inside the block
custom_unknown_key = "keep me"
""")
    before = path.read_text()
    with pytest.raises(ValueError, match="unknown field `custom_unknown_key`"):
        sd.cmd_refresh_inventory(tmp_path)
    assert path.read_text() == before


def test_refresh_inventory_validates_existing_apps_before_replacing_them(tmp_path):
    path = tmp_path / "splashdown.toml"
    path.write_text("""\
[project]
workspace = "single"
loader = "none"

[apps.main]
path = "."
profile = "unknown"
resources = []
unknown = "would otherwise be erased"
""")
    before = path.read_text()
    with pytest.raises(ValueError, match=r"\[apps\.main\] unknown field `unknown`"):
        sd.cmd_refresh_inventory(tmp_path)
    assert path.read_text() == before


def _vite_app(root: Path, *, proxy: bool) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text('{"name": "web", "devDependencies": {"vite": "^5"}}')
    config = (
        'export default { server: { proxy: { "/api": "http://localhost:9081" } } }'
        if proxy
        else "export default {}"
    )
    (root / "vite.config.ts").write_text(config)


def test_init_prunes_vite_api_port_when_no_app_declares_port(tmp_path, capsys):
    """Vite emits API_DEV_PORT = "{{ PORT }}" for any proxying config, but PORT
    only exists when the repo also has a backend app. Without pruning, the
    unresolvable reference fails Recipe validation and init writes nothing."""
    _vite_app(tmp_path, proxy=True)
    sd.cmd_init(tmp_path)
    recipe = sd.Recipe.load(tmp_path / sd.RECIPE_NAME)
    assert set(recipe.resources) == {"WEB_DEV_PORT"}
    assert recipe.apps["main"]["resources"] == ["WEB_DEV_PORT"]
    assert "skipped API_DEV_PORT" in capsys.readouterr().err


def test_init_keeps_vite_api_port_when_a_backend_declares_port(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "mono", "private": true}')
    (tmp_path / "pnpm-workspace.yaml").write_text('packages:\n  - "apps/*"\n')
    _vite_app(tmp_path / "apps" / "web", proxy=True)
    api = tmp_path / "apps" / "api"
    api.mkdir(parents=True)
    (api / "package.json").write_text('{"name": "api", "dependencies": {"express": "^4"}}')
    (api / "index.js").write_text("require('express')()")

    sd.cmd_init(tmp_path)
    recipe = sd.Recipe.load(tmp_path / sd.RECIPE_NAME)
    assert recipe.resources["API_DEV_PORT"]["template"] == "{{ PORT }}"
    assert "API_DEV_PORT" in recipe.apps["web"]["resources"]


def test_refresh_inventory_keeps_template_resolved_by_the_existing_recipe(tmp_path):
    """A resource the recipe already declares keeps a profile template resolvable,
    so refresh must not prune it just because no scanned app emits PORT."""
    _vite_app(tmp_path, proxy=True)
    (tmp_path / sd.RECIPE_NAME).write_text("""\
[project]
workspace = "single"
loader = "none"

[apps.main]
path = "."
profile = "vite"
resources = ["WEB_DEV_PORT", "API_DEV_PORT"]

[resources.PORT]
type = "port"
range = [9081, 9100]

[resources.WEB_DEV_PORT]
type = "port"
range = [5174, 5200]

[resources.API_DEV_PORT]
type = "template"
template = "{{ PORT }}"
""")
    assert sd.cmd_refresh_inventory(tmp_path) == 0
    recipe = sd.Recipe.load(tmp_path / sd.RECIPE_NAME)
    assert "API_DEV_PORT" in recipe.resources
    assert "API_DEV_PORT" in recipe.apps["main"]["resources"]


def test_prune_unresolvable_templates_cascades(tmp_path):
    resources = {
        "A": {"type": "template", "template": "{{ MISSING }}"},
        "B": {"type": "template", "template": "{{ A }}"},
        "C": {"type": "port", "range": [1, 2]},
    }
    app_names = {"main": ["A", "B", "C"]}
    assert sd.scanner._prune_unresolvable_templates(resources, app_names) == ["A", "B"]
    assert set(resources) == {"C"}
    assert app_names == {"main": ["C"]}


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
    proj = tmp_path / "MyApp.xcodeproj"
    proj.mkdir()
    (proj / "project.pbxproj").write_text("\t\t\t\tIPHONEOS_DEPLOYMENT_TARGET = 15.0;\n")
    r = sd.Recipe({"project": {}}, tmp_path / "splashdown.toml")
    assert sd.detect_framework(tmp_path, r) == "ios-native"


def test_detect_ios_native_rejects_macos_only_project(tmp_path):
    # A menu-bar macOS app matches the same glob but has no simulator to build
    # for, so the ios-native run path would be meaningless.
    proj = tmp_path / "MenuBarApp.xcodeproj"
    proj.mkdir()
    (proj / "project.pbxproj").write_text("\t\t\t\tMACOSX_DEPLOYMENT_TARGET = 14.0;\n")
    r = sd.Recipe({"project": {}}, tmp_path / "splashdown.toml")
    with pytest.raises(sd.DeviceError):
        sd.detect_framework(tmp_path, r)


def test_detect_ios_native_fails_open_without_pbxproj(tmp_path):
    (tmp_path / "MyApp.xcodeproj").mkdir()
    r = sd.Recipe({"project": {}}, tmp_path / "splashdown.toml")
    assert sd.detect_framework(tmp_path, r) == "ios-native"


def test_detect_ios_native_fails_open_when_targets_live_in_xcconfig(tmp_path):
    # A pbxproj that delegates build settings to an .xcconfig names neither
    # deployment target; excluding it on that silence is a false negative.
    proj = tmp_path / "MyApp.xcodeproj"
    proj.mkdir()
    (proj / "project.pbxproj").write_text(
        "\t\t\tbaseConfigurationReference = ABC /* App.xcconfig */;\n"
    )
    r = sd.Recipe({"project": {}}, tmp_path / "splashdown.toml")
    assert sd.detect_framework(tmp_path, r) == "ios-native"


def test_detect_ios_native_finds_ios_project_beside_a_macos_one(tmp_path):
    # Sorted order puts the macOS project first; inspecting only projects[0]
    # made the iOS app undetectable.
    mac = tmp_path / "AMacTool.xcodeproj"
    mac.mkdir()
    (mac / "project.pbxproj").write_text("MACOSX_DEPLOYMENT_TARGET = 14.0;\n")
    ios = tmp_path / "ZiOSApp.xcodeproj"
    ios.mkdir()
    (ios / "project.pbxproj").write_text("IPHONEOS_DEPLOYMENT_TARGET = 15.0;\n")
    r = sd.Recipe({"project": {}}, tmp_path / "splashdown.toml")
    assert sd.detect_framework(tmp_path, r) == "ios-native"


def test_detect_ios_native_rejects_macos_workspace(tmp_path):
    # The workspace short-circuit used to bypass the platform check entirely,
    # which is the common CocoaPods/menu-bar-app layout.
    proj = tmp_path / "MenuBar.xcodeproj"
    proj.mkdir()
    (proj / "project.pbxproj").write_text("MACOSX_DEPLOYMENT_TARGET = 14.0;\n")
    (tmp_path / "MenuBar.xcworkspace").mkdir()
    r = sd.Recipe({"project": {}}, tmp_path / "splashdown.toml")
    with pytest.raises(sd.DeviceError):
        sd.detect_framework(tmp_path, r)


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


def test_cmd_init_scanner_rn_emits_default_targets(tmp_path):
    # Scanner-driven init (no preset) on a React Native project must emit default
    # sim/emulator targets, at parity with the `rn` preset scaffold.
    (tmp_path / "package.json").write_text('{"dependencies": {"react-native": "0.74"}}')
    sd.cmd_init(tmp_path)
    recipe = sd.Recipe.load(tmp_path / "splashdown.toml")
    assert "default" in recipe.targets.get("simulator", {})
    assert "default" in recipe.targets.get("emulator", {})
    assert recipe.targets["simulator"]["default"]["model"]


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


def test_cli_init_runs_first_sync(tmp_path, monkeypatch):
    # `splash init` scaffolds AND allocates ports for the current checkout, so
    # splashdown.env exists immediately (no manual `splash sync` needed).
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "vite.config.ts").write_text("export default {}")
    rc = sd.main(["--cwd", str(tmp_path), "init", "--loader=mise"])
    assert rc == 0
    assert (tmp_path / "splashdown.toml").exists()
    env_text = (tmp_path / "splashdown.env").read_text()
    assert "WEB_DEV_PORT=" in env_text
    # The allocation is pinned in the machine-wide registry.
    ports = (tmp_path / "state" / "splashdown" / "ports.tsv").read_text()
    assert str(tmp_path.resolve()) in ports


def test_cli_init_overwrite_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "vite.config.ts").write_text("export default {}")
    assert sd.main(["--cwd", str(tmp_path), "init", "--loader=mise"]) == 0
    # A second init without --overwrite refuses and names the flag (not --force).
    with pytest.raises(SystemExit) as exc:
        sd.main(["--cwd", str(tmp_path), "init", "--loader=mise"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--overwrite" in err and "--force" not in err
    # --overwrite replaces the recipe.
    assert sd.main(["--cwd", str(tmp_path), "init", "--loader=mise", "--overwrite"]) == 0


def test_cli_init_no_sync_skips_provision(tmp_path, monkeypatch):
    # `--no-sync` scaffolds the files but allocates nothing: no splashdown.env,
    # no registry entry for this checkout.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "vite.config.ts").write_text("export default {}")
    rc = sd.main(["--cwd", str(tmp_path), "init", "--loader=mise", "--no-sync"])
    assert rc == 0
    assert (tmp_path / "splashdown.toml").exists()
    assert not (tmp_path / "splashdown.env").exists()
    ports_file = tmp_path / "state" / "splashdown" / "ports.tsv"
    assert not ports_file.exists() or str(tmp_path.resolve()) not in ports_file.read_text()


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
    assert res == {"PORT": {"type": "port", "range": [3001, 3100]}}


def test_django_profile_detects_manage_py(tmp_path):
    (tmp_path / "manage.py").write_text("import django\n")
    assert sd.PROFILES["django"].detect(tmp_path) is True


def test_django_profile_does_not_detect_without_manage_py(tmp_path):
    assert sd.PROFILES["django"].detect(tmp_path) is False


def test_django_profile_emits_port_resource(tmp_path):
    (tmp_path / "manage.py").write_text("import django\n")
    app = sd.AppInventory(name="api", path=tmp_path, profile="django")
    assert sd.PROFILES["django"].resources(app) == {"PORT": {"type": "port", "range": [8001, 8100]}}


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
        "PORT": {"type": "port", "range": [8001, 8100]}
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
        "PORT": {"type": "port", "range": [8081, 8180]}
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


def test_flask_profile_detects_via_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text("Flask==3.0.0\ngunicorn\n")
    assert sd.PROFILES["flask"].detect(tmp_path) is True


def test_flask_profile_detects_via_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["flask>=3", "gunicorn"]\n'
    )
    assert sd.PROFILES["flask"].detect(tmp_path) is True


def test_flask_profile_does_not_detect_without_flask(tmp_path):
    (tmp_path / "requirements.txt").write_text("django==5.0\n")
    assert sd.PROFILES["flask"].detect(tmp_path) is False


def test_flask_profile_emits_port_resource(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask\n")
    app = sd.AppInventory(name="api", path=tmp_path, profile="flask")
    assert sd.PROFILES["flask"].resources(app) == {
        "FLASK_RUN_PORT": {"type": "port", "range": [5001, 5100]}
    }


def test_fastapi_wins_over_flask_when_both_declared(tmp_path):
    # Both profiles substring-match the same file, so registration order decides.
    (tmp_path / "requirements.txt").write_text("fastapi\nflask\n")
    assert sd.Scanner().scan(tmp_path).apps[0].profile == "fastapi"


def test_aspnetcore_profile_detects_web_sdk_csproj(tmp_path):
    (tmp_path / "Api.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk.Web">\n  <PropertyGroup />\n</Project>\n'
    )
    assert sd.PROFILES["aspnetcore"].detect(tmp_path) is True


def test_aspnetcore_profile_does_not_detect_class_library(tmp_path):
    (tmp_path / "Lib.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk">\n</Project>\n')
    assert sd.PROFILES["aspnetcore"].detect(tmp_path) is False


def test_aspnetcore_profile_emits_port_resource(tmp_path):
    (tmp_path / "Api.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk.Web"></Project>')
    app = sd.AppInventory(name="api", path=tmp_path, profile="aspnetcore")
    assert sd.PROFILES["aspnetcore"].resources(app) == {
        "ASPNETCORE_HTTP_PORTS": {"type": "port", "range": [5201, 5300]}
    }


def _make_aspnet(tmp_path, launch_settings):
    (tmp_path / "Api.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk.Web"></Project>')
    (tmp_path / "Properties").mkdir()
    (tmp_path / "Properties" / "launchSettings.json").write_text(json.dumps(launch_settings))
    app = sd.AppInventory(name="api", path=tmp_path, profile="aspnetcore")
    return next(
        c for c in sd.PROFILES["aspnetcore"].wiring_checks(app) if c.id == "aspnet-launch-settings"
    )


def test_aspnet_wiring_check_flags_pinned_application_url(tmp_path):
    check = _make_aspnet(
        tmp_path,
        {
            "profiles": {
                "http": {"commandName": "Project", "applicationUrl": "http://localhost:5062"}
            }
        },
    )
    status, detail = check.detect(tmp_path)
    assert status == "problem"
    assert "http" in detail


def test_aspnet_wiring_check_accepts_no_application_url(tmp_path):
    check = _make_aspnet(tmp_path, {"profiles": {"http": {"commandName": "Project"}}})
    assert check.detect(tmp_path)[0] == "ok"


def test_aspnet_wiring_check_ignores_iis_express_profile(tmp_path):
    # IIS Express reads its own applicationUrl; `dotnet run` never does.
    check = _make_aspnet(
        tmp_path,
        {
            "profiles": {
                "IIS Express": {
                    "commandName": "IISExpress",
                    "applicationUrl": "http://localhost:8080",
                },
                "http": {"commandName": "Project"},
            }
        },
    )
    assert check.detect(tmp_path)[0] == "ok"


def test_aspnet_wiring_autofix_drops_application_url(tmp_path):
    check = _make_aspnet(
        tmp_path,
        {
            "profiles": {
                "IIS Express": {
                    "commandName": "IISExpress",
                    "applicationUrl": "http://localhost:8080",
                },
                "http": {
                    "commandName": "Project",
                    "applicationUrl": "http://localhost:5062",
                    "environmentVariables": {"ASPNETCORE_ENVIRONMENT": "Development"},
                },
            }
        },
    )
    check.autofix(tmp_path)
    assert check.detect(tmp_path)[0] == "ok"
    data = json.loads((tmp_path / "Properties" / "launchSettings.json").read_text())
    assert "applicationUrl" not in data["profiles"]["http"]
    # Unrelated keys and the IIS Express profile survive the rewrite.
    assert (
        data["profiles"]["http"]["environmentVariables"]["ASPNETCORE_ENVIRONMENT"] == "Development"
    )
    assert data["profiles"]["IIS Express"]["applicationUrl"] == "http://localhost:8080"


def _real_launch_settings_bytes():
    """Byte-for-byte shape of what `dotnet new web` actually emits: UTF-8 BOM and
    CRLF endings. A plain read_text() leaves the BOM in the string and json.loads
    then rejects the file, so the fixtures above (LF, no BOM) can't catch it."""
    body = (
        '{\n  "$schema": "https://json.schemastore.org/launchsettings.json",\n'
        '  "profiles": {\n    "http": {\n      "commandName": "Project",\n'
        '      "applicationUrl": "http://localhost:5270"\n    }\n  }\n}\n'
    )
    return b"\xef\xbb\xbf" + body.replace("\n", "\r\n").encode()


def test_aspnet_wiring_check_reads_bom_and_crlf_file(tmp_path):
    (tmp_path / "Api.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk.Web"></Project>')
    (tmp_path / "Properties").mkdir()
    settings = tmp_path / "Properties" / "launchSettings.json"
    settings.write_bytes(_real_launch_settings_bytes())
    app = sd.AppInventory(name="api", path=tmp_path, profile="aspnetcore")
    check = next(
        c for c in sd.PROFILES["aspnetcore"].wiring_checks(app) if c.id == "aspnet-launch-settings"
    )
    # Must read as a pinned URL, not as "not valid JSON".
    assert check.detect(tmp_path)[0] == "problem"
    assert "http" in check.detect(tmp_path)[1]

    check.autofix(tmp_path)
    assert check.detect(tmp_path)[0] == "ok"

    raw = settings.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM survives the rewrite
    assert b"\r\r\n" not in raw  # translation must not double the CR
    assert raw.count(b"\n") == raw.count(b"\r\n")  # every ending stayed CRLF
    assert b'"$schema"' in raw  # unrelated keys survive


def test_aspnet_wiring_autofix_keeps_lf_file_lf(tmp_path):
    (tmp_path / "Api.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk.Web"></Project>')
    (tmp_path / "Properties").mkdir()
    settings = tmp_path / "Properties" / "launchSettings.json"
    settings.write_text(
        '{\n  "profiles": {\n    "http": {\n      "commandName": "Project",\n'
        '      "applicationUrl": "http://localhost:5270"\n    }\n  }\n}\n'
    )
    app = sd.AppInventory(name="api", path=tmp_path, profile="aspnetcore")
    check = next(
        c for c in sd.PROFILES["aspnetcore"].wiring_checks(app) if c.id == "aspnet-launch-settings"
    )
    check.autofix(tmp_path)
    raw = settings.read_bytes()
    # A BOM-less LF file must not acquire either on the way back out.
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw


def test_aspnet_wiring_check_reports_malformed_json(tmp_path):
    (tmp_path / "Api.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk.Web"></Project>')
    (tmp_path / "Properties").mkdir()
    (tmp_path / "Properties" / "launchSettings.json").write_text("{ not json")
    app = sd.AppInventory(name="api", path=tmp_path, profile="aspnetcore")
    check = next(
        c for c in sd.PROFILES["aspnetcore"].wiring_checks(app) if c.id == "aspnet-launch-settings"
    )
    assert check.detect(tmp_path)[0] == "problem"


def test_rails_profile_detects_via_application_rb(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "application.rb").write_text(
        "module MyApp\n  class Application < Rails::Application\n  end\nend\n"
    )
    assert sd.PROFILES["rails"].detect(tmp_path) is True


def test_rails_profile_detects_via_gemfile(tmp_path):
    (tmp_path / "Gemfile").write_text('source "https://rubygems.org"\ngem "rails", "~> 7.1"\n')
    assert sd.PROFILES["rails"].detect(tmp_path) is True


def test_rails_profile_does_not_detect_plain_ruby_gem(tmp_path):
    # `rails` as a substring of another gem name must not trigger detection.
    (tmp_path / "Gemfile").write_text('source "x"\ngem "rails-html-sanitizer"\ngem "sinatra"\n')
    assert sd.PROFILES["rails"].detect(tmp_path) is False


def test_rails_profile_emits_port_resource(tmp_path):
    (tmp_path / "Gemfile").write_text('gem "rails"\n')
    app = sd.AppInventory(name="web", path=tmp_path, profile="rails")
    assert sd.PROFILES["rails"].resources(app) == {"PORT": {"type": "port", "range": [3001, 3100]}}


def test_laravel_profile_detects_artisan_plus_composer(tmp_path):
    (tmp_path / "artisan").write_text("#!/usr/bin/env php\n")
    (tmp_path / "composer.json").write_text('{"require": {"laravel/framework": "^11.0"}}')
    assert sd.PROFILES["laravel"].detect(tmp_path) is True


def test_laravel_profile_does_not_detect_lumen(tmp_path):
    (tmp_path / "artisan").write_text("#!/usr/bin/env php\n")
    (tmp_path / "composer.json").write_text('{"require": {"laravel/lumen-framework": "^10.0"}}')
    assert sd.PROFILES["laravel"].detect(tmp_path) is False


def _laravel_app(tmp_path, *, vite):
    (tmp_path / "artisan").write_text("")
    (tmp_path / "composer.json").write_text('{"require": {"laravel/framework": "^11.0"}}')
    if vite:
        (tmp_path / "package.json").write_text('{"devDependencies": {"vite": "^6"}}')
        (tmp_path / "vite.config.js").write_text(
            "import laravel from 'laravel-vite-plugin';\nexport default { plugins: [laravel()] };\n"
        )
    return sd.AppInventory(name="web", path=tmp_path, profile="laravel")


def test_laravel_beats_vite_on_a_real_laravel_layout(tmp_path):
    # Laravel has shipped a vite.config since Laravel 9, so ViteProfile matches every
    # modern Laravel app; registration order must keep the PHP server's port managed.
    _laravel_app(tmp_path, vite=True)
    assert sd.Scanner().scan(tmp_path).apps[0].profile == "laravel"


def test_laravel_claims_both_dev_server_ports(tmp_path):
    app = _laravel_app(tmp_path, vite=True)
    assert sd.PROFILES["laravel"].resources(app) == {
        "SERVER_PORT": {"type": "port", "range": [8001, 8100]},
        "WEB_DEV_PORT": {"type": "port", "range": [5174, 5200]},
    }
    assert [c.id for c in sd.PROFILES["laravel"].wiring_checks(app)] == ["vite-port-wired"]


def test_laravel_api_only_skips_the_vite_port(tmp_path):
    app = _laravel_app(tmp_path, vite=False)
    assert "WEB_DEV_PORT" not in sd.PROFILES["laravel"].resources(app)
    assert sd.PROFILES["laravel"].wiring_checks(app) == []


def test_plain_vite_app_is_not_claimed_by_laravel(tmp_path):
    (tmp_path / "vite.config.ts").write_text("export default {}")
    assert sd.Scanner().scan(tmp_path).apps[0].profile == "vite"


@pytest.mark.parametrize(
    ("csproj", "modern"),
    [
        ("<TargetFramework>net9.0</TargetFramework>", True),
        ("<TargetFramework>net8.0</TargetFramework>", True),
        ("<TargetFramework>net7.0</TargetFramework>", False),
        ("<TargetFramework>net6.0</TargetFramework>", False),
        ("<TargetFrameworks>net6.0;net8.0</TargetFrameworks>", True),
        ("<PropertyGroup />", True),  # unparseable → assume modern
    ],
)
def test_aspnet_http_ports_support_by_target_framework(tmp_path, csproj, modern):
    (tmp_path / "Api.csproj").write_text(f'<Project Sdk="Microsoft.NET.Sdk.Web">{csproj}</Project>')
    assert sd.profiles._aspnet_supports_http_ports(tmp_path) is modern


def test_aspnet_legacy_tfm_never_autofixes(tmp_path):
    """net6/net7 ignore ASPNETCORE_HTTP_PORTS outright, so dropping applicationUrl
    would strand the app on the shared default 5000 — worse than leaving it alone."""
    (tmp_path / "Api.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk.Web"><TargetFramework>net6.0</TargetFramework></Project>'
    )
    (tmp_path / "Properties").mkdir()
    settings = tmp_path / "Properties" / "launchSettings.json"
    original = json.dumps(
        {
            "profiles": {
                "http": {"commandName": "Project", "applicationUrl": "http://localhost:5062"}
            }
        }
    )
    settings.write_text(original)
    app = sd.AppInventory(name="api", path=tmp_path, profile="aspnetcore")
    check = next(
        c for c in sd.PROFILES["aspnetcore"].wiring_checks(app) if c.id == "aspnet-launch-settings"
    )
    assert check.autofix is None
    status, detail = check.detect(tmp_path)
    assert status == "problem"
    assert ".NET < 8" in detail
    assert settings.read_text() == original  # untouched


def _angular_app(tmp_path, start_script):
    (tmp_path / "angular.json").write_text('{"projects": {"app": {}}}')
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"start": start_script}}))
    app = sd.AppInventory(name="web", path=tmp_path, profile="angular")
    check = next(c for c in sd.PROFILES["angular"].wiring_checks(app) if c.id == "angular-pkg-port")
    return check


def test_angular_profile_detects_angular_json(tmp_path):
    (tmp_path / "angular.json").write_text("{}")
    assert sd.PROFILES["angular"].detect(tmp_path) is True


def test_angular_profile_emits_port_resource(tmp_path):
    (tmp_path / "angular.json").write_text("{}")
    app = sd.AppInventory(name="web", path=tmp_path, profile="angular")
    assert sd.PROFILES["angular"].resources(app) == {
        "WEB_DEV_PORT": {"type": "port", "range": [4201, 4300]}
    }


def test_angular_wiring_flags_and_fixes_unwired_serve_script(tmp_path):
    check = _angular_app(tmp_path, "ng serve")
    status, detail = check.detect(tmp_path)
    assert status == "problem"
    assert "start" in detail
    check.autofix(tmp_path)
    assert check.detect(tmp_path)[0] == "ok"
    scripts = json.loads((tmp_path / "package.json").read_text())["scripts"]
    assert scripts["start"] == "ng serve --port $WEB_DEV_PORT"


def test_angular_wiring_replaces_literal_port(tmp_path):
    # The flag goes on `ng serve` itself rather than at the end of the script, so it
    # survives a compound command; other flags keep their order.
    check = _angular_app(tmp_path, "ng serve --port 4300 --open")
    check.autofix(tmp_path)
    scripts = json.loads((tmp_path / "package.json").read_text())["scripts"]
    assert scripts["start"] == "ng serve --port $WEB_DEV_PORT --open"
    assert "4300" not in scripts["start"]


def test_angular_autofix_replaces_a_variable_port_instead_of_duplicating(tmp_path):
    check = _angular_app(tmp_path, "ng serve --port $OTHER_PORT")
    check.autofix(tmp_path)
    script = json.loads((tmp_path / "package.json").read_text())["scripts"]["start"]
    assert script == "ng serve --port $WEB_DEV_PORT"
    assert script.count("--port") == 1


def test_angular_autofix_keeps_the_flag_on_ng_serve_in_a_compound_script(tmp_path):
    # Appending to the whole script handed `--port` to `echo`, and detect then
    # reported that ok.
    check = _angular_app(tmp_path, "ng serve && echo done")
    check.autofix(tmp_path)
    script = json.loads((tmp_path / "package.json").read_text())["scripts"]["start"]
    assert script == "ng serve --port $WEB_DEV_PORT && echo done"


def test_angular_autofix_survives_a_package_json_with_no_scripts(tmp_path):
    (tmp_path / "angular.json").write_text("{}")
    (tmp_path / "package.json").write_text("{}")
    app = sd.AppInventory(name="web", path=tmp_path, profile="angular")
    check = next(c for c in sd.PROFILES["angular"].wiring_checks(app) if c.id == "angular-pkg-port")
    check.autofix(tmp_path)  # must not raise KeyError
    assert check.detect(tmp_path)[0] == "problem"


def test_angular_wiring_ok_when_already_wired(tmp_path):
    check = _angular_app(tmp_path, "ng serve --port $WEB_DEV_PORT")
    assert check.detect(tmp_path)[0] == "ok"


def test_angular_wiring_flags_a_project_with_no_serve_script(tmp_path):
    # Previously reported ok. Angular consults no port env var, so a project with
    # no `ng serve` script has nowhere for WEB_DEV_PORT to go — reporting that
    # green claimed a wiring that does not exist.
    check = _angular_app(tmp_path, "ng build")
    assert check.detect(tmp_path)[0] == "problem"


def test_nuxt_profile_detects_config_and_dep(tmp_path, tmp_path_factory):
    (tmp_path / "nuxt.config.ts").write_text("export default defineNuxtConfig({})")
    assert sd.PROFILES["nuxt"].detect(tmp_path) is True
    other = tmp_path_factory.mktemp("bydep")
    (other / "package.json").write_text('{"dependencies": {"nuxt": "^4"}}')
    assert sd.PROFILES["nuxt"].detect(other) is True


def test_nuxt_profile_emits_nuxt_port(tmp_path):
    (tmp_path / "nuxt.config.ts").write_text("export default {}")
    app = sd.AppInventory(name="web", path=tmp_path, profile="nuxt")
    assert sd.PROFILES["nuxt"].resources(app) == {
        "NUXT_PORT": {"type": "port", "range": [3001, 3100]}
    }


def test_nuxt_beats_vite_when_both_configs_present(tmp_path):
    # Nuxt is Vite-based; a project that adds a vite.config must still resolve to nuxt
    # or it gets a WEB_DEV_PORT that `nuxt dev` never reads.
    (tmp_path / "nuxt.config.ts").write_text("export default {}")
    (tmp_path / "vite.config.ts").write_text("export default {}")
    assert sd.Scanner().scan(tmp_path).apps[0].profile == "nuxt"


def _deno_app(tmp_path, config, name="deno.json"):
    (tmp_path / name).write_text(config)
    app = sd.AppInventory(name="api", path=tmp_path, profile="deno")
    return next(c for c in sd.PROFILES["deno"].wiring_checks(app) if c.id == "deno-port-wired")


def test_deno_profile_detects_json_and_jsonc(tmp_path, tmp_path_factory):
    (tmp_path / "deno.json").write_text("{}")
    assert sd.PROFILES["deno"].detect(tmp_path) is True
    other = tmp_path_factory.mktemp("jsonc")
    (other / "deno.jsonc").write_text("// comment\n{}")
    assert sd.PROFILES["deno"].detect(other) is True


def test_deno_profile_emits_port_resource(tmp_path):
    (tmp_path / "deno.json").write_text("{}")
    app = sd.AppInventory(name="api", path=tmp_path, profile="deno")
    assert sd.PROFILES["deno"].resources(app) == {"PORT": {"type": "port", "range": [8001, 8100]}}


def test_deno_autofix_inserts_port_before_the_script_arg(tmp_path):
    """Regression: `deno serve` passes everything after the script argument to the
    script itself, so an appended --port is silently ignored and the server keeps
    binding 8000. The flag has to land directly after `deno serve`."""
    check = _deno_app(tmp_path, '{"tasks": {"dev": "deno serve --allow-net server.ts"}}')
    assert check.detect(tmp_path)[0] == "problem"
    check.autofix(tmp_path)
    task = json.loads((tmp_path / "deno.json").read_text())["tasks"]["dev"]
    assert task == "deno serve --port $PORT --allow-net server.ts"
    assert not task.endswith("$PORT")
    assert check.detect(tmp_path)[0] == "ok"


def test_deno_autofix_leaves_deno_run_tasks_alone(tmp_path):
    # `deno run` takes no --port; the port has to be read in code instead.
    original = '{"tasks": {"dev": "deno run --allow-net main.ts"}}'
    check = _deno_app(tmp_path, original)
    check.autofix(tmp_path)
    assert (tmp_path / "deno.json").read_text() == original
    assert check.detect(tmp_path)[0] == "problem"


def test_deno_detect_accepts_code_that_reads_port(tmp_path):
    check = _deno_app(tmp_path, '{"tasks": {"dev": "deno run main.ts"}}')
    (tmp_path / "main.ts").write_text(
        'Deno.serve({ port: Number(Deno.env.get("PORT")) || 8000 }, () => new Response("ok"));'
    )
    assert check.detect(tmp_path)[0] == "ok"


def test_deno_autofix_skips_jsonc_to_preserve_comments(tmp_path):
    original = '// keep me\n{"tasks": {"dev": "deno serve server.ts"}}'
    check = _deno_app(tmp_path, original, name="deno.jsonc")
    check.autofix(tmp_path)
    assert (tmp_path / "deno.jsonc").read_text() == original


def test_deno_detect_rejects_a_port_flag_after_the_script_arg(tmp_path):
    # The mirror of the autofix regression above: everything after the script
    # argument is passed to the script, so this task still binds 8000.
    check = _deno_app(tmp_path, '{"tasks": {"dev": "deno serve server.ts --port $PORT"}}')
    status, detail = check.detect(tmp_path)
    assert status == "problem"
    assert "script argument" in detail


def test_deno_detect_ignores_port_in_an_unrelated_task(tmp_path):
    check = _deno_app(
        tmp_path,
        '{"tasks": {"dev": "deno serve server.ts", "db": "psql -p $PORT"}}',
    )
    assert check.detect(tmp_path)[0] == "problem"


def test_deno_detect_accepts_the_flag_before_the_script_arg(tmp_path):
    for task in (
        "deno serve --port $PORT server.ts",
        "deno serve --allow-net --port=${PORT} server.ts",
        'deno serve --port "$PORT" server.ts',
    ):
        check = _deno_app(tmp_path, json.dumps({"tasks": {"dev": task}}))
        assert check.detect(tmp_path)[0] == "ok", task


def test_deno_detect_ignores_commented_out_wiring_in_jsonc(tmp_path):
    check = _deno_app(
        tmp_path,
        '{\n  // "dev": "deno serve --port $PORT main.ts"\n'
        '  "tasks": {"dev": "deno serve main.ts"}\n}\n',
        name="deno.jsonc",
    )
    assert check.detect(tmp_path)[0] == "problem"


def test_deno_detect_reports_a_config_it_cannot_parse(tmp_path):
    check = _deno_app(tmp_path, "{ this is not json at all ")
    status, detail = check.detect(tmp_path)
    assert status == "problem"
    assert "can't read" in detail


def test_angular_detect_reports_when_no_ng_serve_script_exists(tmp_path):
    # Angular reads no port env var at all, so "nothing to wire" means the
    # allocated port reaches nothing — that is a finding, not a pass.
    check = _angular_app(tmp_path, "node scripts/dev.js")
    status, detail = check.detect(tmp_path)
    assert status == "problem"
    assert "no `ng serve` script" in detail


def test_angular_detect_requires_the_var_to_reach_the_port_flag(tmp_path):
    # Setting the variable is not passing it: `ng serve` ignores the environment.
    check = _angular_app(tmp_path, "cross-env WEB_DEV_PORT=4200 ng serve")
    assert check.detect(tmp_path)[0] == "problem"


def test_angular_detect_accepts_the_port_flag_spellings(tmp_path, tmp_path_factory):
    for script in (
        "ng serve --port $WEB_DEV_PORT",
        "ng serve --port=${WEB_DEV_PORT}",
        'ng serve --port "$WEB_DEV_PORT"',
    ):
        d = tmp_path_factory.mktemp("ng")
        assert _angular_app(d, script).detect(d)[0] == "ok", script


def test_aspnetcore_detects_blazor_webassembly_sdk(tmp_path):
    # blazor/mvc/webapi/razor all emit Microsoft.NET.Sdk.Web; only standalone Blazor
    # WASM uses its own SDK, and it shares the same launchSettings mechanics.
    (tmp_path / "App.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk.BlazorWebAssembly" />')
    assert sd.PROFILES["aspnetcore"].detect(tmp_path) is True


def test_laravel_profile_emits_port_resource(tmp_path):
    (tmp_path / "artisan").write_text("")
    (tmp_path / "composer.json").write_text('{"require": {"laravel/framework": "^11.0"}}')
    app = sd.AppInventory(name="web", path=tmp_path, profile="laravel")
    assert sd.PROFILES["laravel"].resources(app) == {
        "SERVER_PORT": {"type": "port", "range": [8001, 8100]}
    }


def test_scanner_gradle_skips_missing_module_dir(tmp_path):
    (tmp_path / "settings.gradle.kts").write_text('include("ghost")\n')
    inv = sd.Scanner().scan(tmp_path)
    assert inv.workspace == "gradle"
    assert all(app.name != "ghost" for app in inv.apps)


def test_scan_drops_unknown_members_in_workspace(tmp_path):
    # pnpm workspace: apps/web (vite) + packages/ui (no framework).
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - 'apps/*'\n  - 'packages/*'\n")
    (tmp_path / "apps" / "web").mkdir(parents=True)
    (tmp_path / "apps" / "web" / "vite.config.ts").write_text("export default {}")
    (tmp_path / "packages" / "ui").mkdir(parents=True)
    (tmp_path / "packages" / "ui" / "package.json").write_text('{"name": "ui"}')
    inv = sd.Scanner().scan(tmp_path)
    names = {a.name: a.profile for a in inv.apps}
    assert names == {"web": "vite"}


def test_scan_keeps_single_unknown_app(tmp_path):
    # Bare directory → single workspace → one unknown app, still present.
    inv = sd.Scanner().scan(tmp_path)
    assert [(a.name, a.profile) for a in inv.apps] == [("main", "unknown")]


def test_has_resource_collision_true_on_shared_name():
    res_by_app = {"web": {"PORT": {}}, "api": {"PORT": {}}}
    assert sd.scanner._has_resource_collision(res_by_app) is True


def test_has_resource_collision_false_on_distinct_names():
    res_by_app = {"web": {"WEB_DEV_PORT": {}}, "api": {"PORT": {}}}
    assert sd.scanner._has_resource_collision(res_by_app) is False


def test_unclaimed_native_dirs_finds_sibling_native(tmp_path):
    # JS workspace app under apps/web, plus a sibling native ios/ at root.
    web = tmp_path / "apps" / "web"
    web.mkdir(parents=True)
    (tmp_path / "ios").mkdir()
    (tmp_path / "ios" / "App.xcodeproj").mkdir()
    apps = [sd.AppInventory(name="web", path=web, profile="vite")]
    found = sd.scanner._unclaimed_native_dirs(tmp_path, apps)
    assert [p.name for p in found] == ["ios"]


def test_unclaimed_native_dirs_ignores_rn_subfolders(tmp_path):
    # Single RN app at root: its own ios/ + android/ are inside the app → claimed.
    (tmp_path / "ios").mkdir()
    (tmp_path / "ios" / "App.xcodeproj").mkdir()
    (tmp_path / "android").mkdir()
    (tmp_path / "android" / "build.gradle").write_text("")
    (tmp_path / "android" / "settings.gradle").write_text("")
    apps = [sd.AppInventory(name="main", path=tmp_path, profile="react-native")]
    assert sd.scanner._unclaimed_native_dirs(tmp_path, apps) == []


def test_init_defers_on_port_collision(tmp_path, capsys):
    # apps/web (Next) + apps/api (Nest) → both emit PORT → defer.
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - 'apps/*'\n")
    web = tmp_path / "apps" / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text('{"dependencies": {"next": "15"}}')
    api = tmp_path / "apps" / "api"
    api.mkdir(parents=True)
    (api / "package.json").write_text('{"dependencies": {"@nestjs/core": "10"}}')
    sd.cmd_init(tmp_path)
    text = (tmp_path / "splashdown.toml").read_text()
    assert "[apps.web]" in text and "[apps.api]" in text
    assert "[resources." not in text and "[targets." not in text
    assert "https://splashdown.dev/monorepos/" in capsys.readouterr().err


def test_init_defers_on_unclaimed_native_sibling(tmp_path, capsys):
    # JS workspace + sibling native ios/ → defer.
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - 'apps/*'\n")
    web = tmp_path / "apps" / "web"
    web.mkdir(parents=True)
    (web / "vite.config.ts").write_text("export default {}")
    (tmp_path / "ios").mkdir()
    (tmp_path / "ios" / "App.xcodeproj").mkdir()
    sd.cmd_init(tmp_path)
    text = (tmp_path / "splashdown.toml").read_text()
    assert "[apps.web]" in text
    assert "[resources." not in text
    assert "monorepo detected" in capsys.readouterr().err


def test_init_does_not_defer_on_distinct_names(tmp_path):
    # apps/web (Vite → WEB_DEV_PORT) + apps/api (Next → PORT): distinct names, no
    # native siblings → normal scaffold with resources.
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - 'apps/*'\n")
    web = tmp_path / "apps" / "web"
    web.mkdir(parents=True)
    (web / "vite.config.ts").write_text("export default {}")
    api = tmp_path / "apps" / "api"
    api.mkdir(parents=True)
    (api / "package.json").write_text('{"dependencies": {"next": "15"}}')
    sd.cmd_init(tmp_path)
    recipe = sd.Recipe.load(tmp_path / "splashdown.toml")
    assert "WEB_DEV_PORT" in recipe.resources
    assert "PORT" in recipe.resources


def test_init_rn_single_app_still_scaffolds(tmp_path):
    # Regression: a plain RN repo (own ios/+android/) must NOT defer.
    (tmp_path / "package.json").write_text('{"dependencies": {"react-native": "0.74"}}')
    (tmp_path / "ios").mkdir()
    (tmp_path / "ios" / "App.xcodeproj").mkdir()
    (tmp_path / "android").mkdir()
    (tmp_path / "android" / "build.gradle").write_text("")
    (tmp_path / "android" / "settings.gradle").write_text("")
    sd.cmd_init(tmp_path)
    recipe = sd.Recipe.load(tmp_path / "splashdown.toml")
    assert "RCT_METRO_PORT" in recipe.resources
    assert "default" in recipe.targets.get("simulator", {})


def test_deno_autofix_relocates_a_misordered_port_flag(tmp_path):
    check = _deno_app(tmp_path, '{"tasks": {"dev": "deno serve server.ts --port $PORT"}}')
    assert check.detect(tmp_path)[0] == "problem"
    check.autofix(tmp_path)
    task = json.loads((tmp_path / "deno.json").read_text())["tasks"]["dev"]
    assert task == "deno serve --port $PORT server.ts"
    assert task.count("--port") == 1
    assert check.detect(tmp_path)[0] == "ok"


def test_deno_detect_accepts_jsonc_with_trailing_commas(tmp_path):
    # Deno's own config reader accepts them; rejecting them reported a correctly
    # wired project as broken, and .jsonc is never autofixed, so it stayed broken.
    check = _deno_app(
        tmp_path,
        '{\n  "tasks": {\n    "dev": "deno serve --port $PORT main.ts",\n  },\n}\n',
        name="deno.jsonc",
    )
    assert check.detect(tmp_path)[0] == "ok"


def test_deno_detect_ignores_a_commented_out_task_in_wired_jsonc(tmp_path):
    # The complement of the above: comments stripped, real task still read.
    check = _deno_app(
        tmp_path,
        '{\n  // "dev": "deno serve --port $PORT old.ts"\n'
        '  "tasks": {"dev": "deno serve --port $PORT main.ts"}\n}\n',
        name="deno.jsonc",
    )
    assert check.detect(tmp_path)[0] == "ok"


def test_deno_detect_requires_splashdowns_own_port_var(tmp_path):
    check = _deno_app(tmp_path, json.dumps({"tasks": {"dev": "deno serve --port $MY_PORT app.ts"}}))
    assert check.detect(tmp_path)[0] == "problem"


def test_deno_detect_handles_flags_that_take_a_separate_value(tmp_path):
    # `--host 0.0.0.0` used to be read as the script argument, so a correctly wired
    # task was reported as having its flag in the wrong place.
    for task in (
        "deno serve --host 0.0.0.0 --port $PORT main.ts",
        "deno serve --config deno.json --port $PORT ./src/main.ts",
        "deno serve --log-level debug --port=${PORT} npm:my-server",
    ):
        check = _deno_app(tmp_path, json.dumps({"tasks": {"dev": task}}))
        assert check.detect(tmp_path)[0] == "ok", task


def test_deno_detect_does_not_claim_misordered_without_a_port_flag(tmp_path):
    check = _deno_app(tmp_path, json.dumps({"tasks": {"dev": "deno serve --allow-env=PORT m.ts"}}))
    status, detail = check.detect(tmp_path)
    assert status == "problem"
    assert "script argument" not in detail


def test_deno_detect_ignores_commented_out_source_wiring(tmp_path):
    check = _deno_app(tmp_path, json.dumps({"tasks": {"dev": "deno run main.ts"}}))
    (tmp_path / "main.ts").write_text(
        '// Deno.env.get("PORT")\nDeno.serve(() => new Response(""));'
    )
    assert check.detect(tmp_path)[0] == "problem"


def test_deno_autofix_leaves_chained_commands_alone(tmp_path):
    # A blanket substitution deleted the sidecar's own --port and wrote the file.
    check = _deno_app(
        tmp_path,
        json.dumps({"tasks": {"dev": "deno serve app.ts && psql --port 5432 -c 'select 1'"}}),
    )
    check.autofix(tmp_path)
    task = json.loads((tmp_path / "deno.json").read_text())["tasks"]["dev"]
    assert task == "deno serve --port $PORT app.ts && psql --port 5432 -c 'select 1'"
