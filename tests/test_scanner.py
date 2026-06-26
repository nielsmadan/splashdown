"""Tests for splashdown scanner behavior."""

from __future__ import annotations

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


def test_refresh_inventory_preserves_resource_comments_and_unknown_keys(tmp_path):
    """refresh-inventory must round-trip an underscore-named resource verbatim,
    including a user comment and an unknown key inside the block."""
    (tmp_path / "splashdown.toml").write_text("""\
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
    assert sd.cmd_refresh_inventory(tmp_path) == 0
    text = (tmp_path / "splashdown.toml").read_text()
    # Comment + unknown key survive; resource still parses with its value.
    assert "# user note inside the block" in text
    assert 'custom_unknown_key = "keep me"' in text
    rec = sd.Recipe.load(tmp_path / "splashdown.toml")
    assert "MY_PORT_WITH_UNDERSCORES" in rec.resources


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


def test_scanner_gradle_skips_missing_module_dir(tmp_path):
    (tmp_path / "settings.gradle.kts").write_text('include("ghost")\n')
    inv = sd.Scanner().scan(tmp_path)
    assert inv.workspace == "gradle"
    assert all(app.name != "ghost" for app in inv.apps)
