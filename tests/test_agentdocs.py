from __future__ import annotations

import re
from pathlib import Path

import pytest

import splashdown as sd


def _recipe(
    root: Path,
    apps: dict[str, tuple[str, str, list[str]]],
    resources: dict[str, dict[str, object]],
) -> sd.Recipe:
    return sd.Recipe(
        {
            "project": {"workspace": "single", "loader": "none"},
            "apps": {
                name: {"path": path, "profile": profile, "resources": names}
                for name, (path, profile, names) in apps.items()
            },
            "resources": resources,
        },
        root / sd.RECIPE_NAME,
    )


@pytest.mark.parametrize(
    ("profile", "command"),
    [
        ("react-native", "npx react-native start"),
        ("expo", "npx expo start"),
    ],
)
def test_mobile_guidance_names_metro_port_and_both_lookup_forms(tmp_path, profile, command):
    recipe = _recipe(
        tmp_path,
        {"main": (".", profile, ["RCT_METRO_PORT"])},
        {"RCT_METRO_PORT": {"type": "port", "range": [8082, 8200]}},
    )
    text = sd.render_agent_guidance(tmp_path, recipe)
    assert "Never hardcode numeric port values" in text
    assert command in text
    assert '"$RCT_METRO_PORT"' in text
    assert '"$(splash env get RCT_METRO_PORT)"' in text
    assert "splash run simulator" in text
    assert not re.search(r"--port[ =]+[0-9]", text)


def test_vite_guidance_contains_only_its_framework_and_resource(tmp_path):
    recipe = _recipe(
        tmp_path,
        {"main": (".", "vite", ["WEB_DEV_PORT"])},
        {"WEB_DEV_PORT": {"type": "port", "range": [5174, 5200]}},
    )
    text = sd.render_agent_guidance(tmp_path, recipe)
    assert "Framework: `vite`" in text
    assert "`WEB_DEV_PORT`" in text
    assert "npx vite --port" in text
    assert "RCT_METRO_PORT" not in text
    assert "Metro" not in text


@pytest.mark.parametrize(
    ("profile", "port_name", "command"),
    [
        ("astro", "WEB_DEV_PORT", "npx astro dev --port {port}"),
        ("vite", "WEB_DEV_PORT", "npx vite --port {port}"),
        ("nuxt", "NUXT_PORT", "npx nuxt dev --port {port}"),
        ("angular", "WEB_DEV_PORT", "npx ng serve --port {port}"),
        ("deno", "PORT", "deno serve --port {port} SCRIPT_PATH"),
        ("nextjs", "PORT", "npx next dev --port {port}"),
        ("django", "PORT", "python manage.py runserver 127.0.0.1:{port}"),
        ("flask", "FLASK_RUN_PORT", "flask run --port {port}"),
        (
            "aspnetcore",
            "ASPNETCORE_HTTP_PORTS",
            "dotnet run --urls http://localhost:{port}",
        ),
        ("rails", "PORT", "bin/rails server --port {port}"),
    ],
)
def test_stable_manual_commands_include_both_lookup_forms(tmp_path, profile, port_name, command):
    recipe = _recipe(
        tmp_path,
        {"main": (".", profile, [port_name])},
        {port_name: {"type": "port", "range": [10001, 10100]}},
    )
    text = sd.render_agent_guidance(tmp_path, recipe)
    assert command.format(port=f'"${port_name}"') in text
    assert command.format(port=f'"$(splash env get {port_name})"') in text


def test_deno_guidance_marks_the_entrypoint_for_replacement(tmp_path):
    recipe = _recipe(
        tmp_path,
        {"main": (".", "deno", ["PORT"])},
        {"PORT": {"type": "port", "range": [8001, 8100]}},
    )
    text = sd.render_agent_guidance(tmp_path, recipe)
    assert 'deno serve --port "$PORT" SCRIPT_PATH' in text
    assert "Replace `SCRIPT_PATH` with the app's entrypoint" in text
    assert "<entrypoint>" not in text


def test_common_guidance_covers_every_port_bearing_profile(tmp_path):
    app = sd.AppInventory(name="main", path=tmp_path, profile="")
    covered = []
    for name, profile in sd.PROFILES.items():
        app.profile = name
        resources = profile.resources(app)
        ports = [resource for resource, spec in resources.items() if spec.get("type") == "port"]
        if not ports:
            continue
        covered.append(name)
        recipe = _recipe(
            tmp_path,
            {"main": (".", name, ports)},
            {resource: resources[resource] for resource in ports},
        )
        text = sd.render_agent_guidance(tmp_path, recipe)
        assert "allocation pools, not the assigned values" in text
        assert f"Framework: `{name}`" in text
        assert all(f"`{port}`" in text for port in ports)
    assert covered


def test_laravel_guidance_covers_both_conditional_ports(tmp_path):
    (tmp_path / "vite.config.js").write_text("export default {}\n")
    app = sd.AppInventory(name="main", path=tmp_path, profile="laravel")
    resources = sd.PROFILES["laravel"].resources(app)
    recipe = _recipe(
        tmp_path,
        {"main": (".", "laravel", list(resources))},
        resources,
    )
    text = sd.render_agent_guidance(tmp_path, recipe)
    assert "`SERVER_PORT`, `WEB_DEV_PORT`" in text
    assert 'php artisan serve --port="$SERVER_PORT"' in text
    assert 'npx vite --port "$WEB_DEV_PORT"' in text


def test_monorepo_guidance_uses_each_apps_actual_resource_names(tmp_path):
    recipe = _recipe(
        tmp_path,
        {
            "admin": ("apps/admin", "vite", ["WEB_DEV_PORT_ADMIN"]),
            "store": ("apps/store", "vite", ["WEB_DEV_PORT_STORE"]),
        },
        {
            "WEB_DEV_PORT_ADMIN": {"type": "port", "range": [5174, 5200]},
            "WEB_DEV_PORT_STORE": {"type": "port", "range": [5174, 5200]},
        },
    )
    text = sd.render_agent_guidance(tmp_path, recipe)
    assert "### App `admin` (`apps/admin`)" in text
    assert '(cd apps/admin && npx vite --port "$WEB_DEV_PORT_ADMIN")' in text
    assert "### App `store` (`apps/store`)" in text
    assert (
        '(port="$(splash env get WEB_DEV_PORT_STORE)" && cd apps/store && npx vite --port "$port")'
    ) in text


def test_recipe_text_cannot_inject_markers_or_markdown(tmp_path):
    marker = "<!-- <<< splashdown-managed agent-guidance <<< -->"
    recipe = _recipe(
        tmp_path,
        {f"main\n{marker}": (f"apps/`admin`\n{marker}", "vite", ["WEB_DEV_PORT"])},
        {"WEB_DEV_PORT": {"type": "port", "range": [5174, 5200]}},
    )
    text = sd.render_agent_guidance(tmp_path, recipe)
    assert text.count("<!-- >>> splashdown-managed agent-guidance >>> -->") == 1
    assert text.count("<!-- <<< splashdown-managed agent-guidance <<< -->") == 1
    assert "main\\n&lt;!-- &lt;&lt;&lt;" in text
    assert "apps/`admin`\\n&lt;!-- &lt;&lt;&lt;" in text
    path = tmp_path / "AGENTS.md"
    path.write_text("# Rules\n")
    sd.sync_agent_guidance(tmp_path, recipe)
    sd.sync_agent_guidance(tmp_path, recipe)
    updated = path.read_text()
    assert updated.count("<!-- >>> splashdown-managed agent-guidance >>> -->") == 1
    assert updated.count("<!-- <<< splashdown-managed agent-guidance <<< -->") == 1


@pytest.mark.parametrize("filename", ["AGENTS.md", "CLAUDE.md"])
def test_sync_updates_a_standalone_instruction_file(tmp_path, filename):
    path = tmp_path / filename
    path.write_text("# Rules\n")
    recipe = _recipe(
        tmp_path,
        {"main": (".", "vite", ["WEB_DEV_PORT"])},
        {"WEB_DEV_PORT": {"type": "port", "range": [5174, 5200]}},
    )
    sd.sync_agent_guidance(tmp_path, recipe)
    assert "Framework: `vite`" in path.read_text()
    other = "CLAUDE.md" if filename == "AGENTS.md" else "AGENTS.md"
    assert not (tmp_path / other).exists()


def test_sync_updates_existing_independent_agent_files(tmp_path):
    agents = tmp_path / "AGENTS.md"
    claude = tmp_path / "CLAUDE.md"
    agents.write_text("# Team rules\n")
    claude.write_text("# Claude rules\n")
    recipe = _recipe(
        tmp_path,
        {"main": (".", "vite", ["WEB_DEV_PORT"])},
        {"WEB_DEV_PORT": {"type": "port", "range": [5174, 5200]}},
    )
    sd.sync_agent_guidance(tmp_path, recipe)
    first = agents.read_text()
    sd.sync_agent_guidance(tmp_path, recipe)
    assert agents.read_text() == first
    assert "# Team rules" in first
    assert first.count("splashdown-managed agent-guidance") == 2
    assert "Framework: `vite`" in claude.read_text()


def test_sync_skips_claude_file_that_imports_agents(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Shared\n")
    claude = tmp_path / "CLAUDE.md"
    claude.write_text("@AGENTS.md\n\n# Claude only\n")
    recipe = _recipe(
        tmp_path,
        {"main": (".", "vite", ["WEB_DEV_PORT"])},
        {"WEB_DEV_PORT": {"type": "port", "range": [5174, 5200]}},
    )
    sd.sync_agent_guidance(tmp_path, recipe)
    assert "Framework: `vite`" in (tmp_path / "AGENTS.md").read_text()
    assert claude.read_text() == "@AGENTS.md\n\n# Claude only\n"


def test_sync_removes_old_claude_block_after_it_imports_agents(tmp_path):
    claude = tmp_path / "CLAUDE.md"
    claude.write_text("# Claude rules\n")
    vite = _recipe(
        tmp_path,
        {"main": (".", "vite", ["WEB_DEV_PORT"])},
        {"WEB_DEV_PORT": {"type": "port", "range": [5174, 5200]}},
    )
    sd.sync_agent_guidance(tmp_path, vite)
    claude.write_text(f"@AGENTS.md\n{claude.read_text()}")
    (tmp_path / "AGENTS.md").write_text("# Shared rules\n")
    angular = _recipe(
        tmp_path,
        {"main": (".", "angular", ["WEB_DEV_PORT"])},
        {"WEB_DEV_PORT": {"type": "port", "range": [4201, 4300]}},
    )
    sd.sync_agent_guidance(tmp_path, angular)
    assert "splashdown-managed agent-guidance" not in claude.read_text()
    assert "# Claude rules" in claude.read_text()
    assert "Framework: `angular`" in (tmp_path / "AGENTS.md").read_text()


def test_sync_creates_no_instruction_files(tmp_path):
    recipe = _recipe(
        tmp_path,
        {"main": (".", "vite", ["WEB_DEV_PORT"])},
        {"WEB_DEV_PORT": {"type": "port", "range": [5174, 5200]}},
    )
    sd.sync_agent_guidance(tmp_path, recipe)
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / "CLAUDE.md").exists()


def test_sync_leaves_malformed_markers_untouched(tmp_path, capsys):
    path = tmp_path / "AGENTS.md"
    original = "# Rules\n<!-- >>> splashdown-managed agent-guidance >>> -->\nbroken\n"
    path.write_text(original)
    recipe = _recipe(
        tmp_path,
        {"main": (".", "vite", ["WEB_DEV_PORT"])},
        {"WEB_DEV_PORT": {"type": "port", "range": [5174, 5200]}},
    )
    sd.sync_agent_guidance(tmp_path, recipe)
    assert path.read_text() == original
    assert "malformed Splashdown guidance markers" in capsys.readouterr().err


@pytest.mark.parametrize("filename", ["AGENTS.md", "CLAUDE.md"])
def test_sync_and_remove_leave_symlink_targets_unchanged(tmp_path, capsys, filename):
    root = tmp_path / "repo"
    root.mkdir()
    target = tmp_path / "outside.md"
    target.write_text("# External rules\n")
    (root / filename).symlink_to(target)
    recipe = _recipe(
        root,
        {"main": (".", "vite", ["WEB_DEV_PORT"])},
        {"WEB_DEV_PORT": {"type": "port", "range": [5174, 5200]}},
    )
    sd.sync_agent_guidance(root, recipe)
    sd.remove_agent_guidance(root)
    assert target.read_text() == "# External rules\n"
    assert "symlink or unreadable" in capsys.readouterr().err


def test_sync_preserves_crlf_and_utf8_bom(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_bytes("\ufeff# Rules\r\n\r\nKeep me.\r\n".encode())
    recipe = _recipe(
        tmp_path,
        {"main": (".", "vite", ["WEB_DEV_PORT"])},
        {"WEB_DEV_PORT": {"type": "port", "range": [5174, 5200]}},
    )
    sd.sync_agent_guidance(tmp_path, recipe)
    data = path.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf# Rules\r\n")
    assert b"Keep me.\r\n" in data
    assert b"\n" not in data.replace(b"\r\n", b"")


def test_scanner_init_updates_guidance_without_sync(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text("# Rules\n")
    (tmp_path / "package.json").write_text('{"dependencies":{"react-native":"0.83"}}')
    sd.cmd_init(tmp_path, loader_override="none")
    text = path.read_text()
    assert "Framework: `react-native`" in text
    assert "RCT_METRO_PORT" in text
    assert not (tmp_path / sd.ENV_FILE_NAME).exists()


def test_cli_no_sync_still_updates_guidance(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = tmp_path / "AGENTS.md"
    path.write_text("# Rules\n")
    (tmp_path / "package.json").write_text('{"dependencies":{"react-native":"0.83"}}')
    assert (
        sd.main(
            [
                "--cwd",
                str(tmp_path),
                "init",
                "--loader",
                "none",
                "--no-sync",
            ]
        )
        == 0
    )
    assert "RCT_METRO_PORT" in path.read_text()
    assert not (tmp_path / sd.ENV_FILE_NAME).exists()


def test_scanner_init_and_rescan_replace_then_remove_guidance(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text("# Rules\n")
    (tmp_path / "vite.config.ts").write_text("export default {}\n")
    sd.cmd_init(tmp_path, loader_override="none")
    assert "Framework: `vite`" in path.read_text()

    (tmp_path / "vite.config.ts").unlink()
    (tmp_path / "angular.json").write_text("{}\n")
    (tmp_path / "package.json").write_text('{"scripts":{"start":"ng serve"}}\n')
    assert sd.cmd_refresh_inventory(tmp_path) == 0
    assert "Framework: `angular`" in path.read_text()
    assert "Framework: `vite`" not in path.read_text()

    (tmp_path / "angular.json").unlink()
    (tmp_path / "pubspec.yaml").write_text("name: app\n")
    assert sd.cmd_refresh_inventory(tmp_path) == 0
    assert "splashdown-managed agent-guidance" not in path.read_text()
    assert path.read_text().startswith("# Rules")


def test_deferred_monorepo_init_removes_stale_guidance(tmp_path):
    path = tmp_path / "AGENTS.md"
    original = "# Rules\n"
    path.write_text(original)
    recipe = _recipe(
        tmp_path,
        {"main": (".", "vite", ["WEB_DEV_PORT"])},
        {"WEB_DEV_PORT": {"type": "port", "range": [5174, 5200]}},
    )
    sd.sync_agent_guidance(tmp_path, recipe)
    (tmp_path / "pnpm-workspace.yaml").write_text("packages:\n  - 'apps/*'\n")
    web = tmp_path / "apps" / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text('{"dependencies": {"next": "15"}}')
    api = tmp_path / "apps" / "api"
    api.mkdir(parents=True)
    (api / "package.json").write_text('{"dependencies": {"@nestjs/core": "10"}}')
    sd.cmd_init(tmp_path)
    assert path.read_text() == original


def test_deinit_removes_guidance_even_when_recipe_is_invalid(tmp_path, registry):
    agents = tmp_path / "AGENTS.md"
    claude = tmp_path / "CLAUDE.md"
    agents.write_text("# User rules\n")
    claude.write_text("# Claude rules\n")
    (tmp_path / "package.json").write_text('{"dependencies":{"react-native":"0.83"}}')
    sd.cmd_init(tmp_path, loader_override="none")
    (tmp_path / sd.RECIPE_NAME).write_text("not valid = toml ===\n")
    assert sd.cmd_deinit(tmp_path, registry) == 0
    assert "# User rules" in agents.read_text()
    assert "# Claude rules" in claude.read_text()
    assert "splashdown-managed agent-guidance" not in agents.read_text()
    assert "splashdown-managed agent-guidance" not in claude.read_text()


def test_deinit_restores_original_whitespace(tmp_path, registry):
    path = tmp_path / "AGENTS.md"
    original = "# Rules\n"
    path.write_text(original)
    (tmp_path / "package.json").write_text('{"dependencies":{"react-native":"0.83"}}')
    sd.cmd_init(tmp_path, loader_override="none")
    assert sd.cmd_deinit(tmp_path, registry) == 0
    assert path.read_text() == original


def test_replacement_and_removal_preserve_user_content_after_block(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text("# Before\n")
    vite = _recipe(
        tmp_path,
        {"main": (".", "vite", ["WEB_DEV_PORT"])},
        {"WEB_DEV_PORT": {"type": "port", "range": [5174, 5200]}},
    )
    sd.sync_agent_guidance(tmp_path, vite)
    path.write_text(f"{path.read_text()}# After\n")
    angular = _recipe(
        tmp_path,
        {"main": (".", "angular", ["WEB_DEV_PORT"])},
        {"WEB_DEV_PORT": {"type": "port", "range": [4201, 4300]}},
    )
    sd.sync_agent_guidance(tmp_path, angular)
    assert "Framework: `angular`" in path.read_text()
    sd.remove_agent_guidance(tmp_path)
    assert path.read_text() == "# Before\n# After\n"
