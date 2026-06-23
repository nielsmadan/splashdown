"""Tests for splashdown cli behavior."""

from __future__ import annotations

import os
import tomllib

import pytest

import splashdown as sd


def test_file_name_constants():
    assert sd.RECIPE_NAME == "splashdown.toml"
    assert sd.LOCAL_NAME == "splashdown.local.toml"
    assert sd.ENV_FILE_NAME == "splashdown.env"


def test_cli_prog_name_is_splash():
    assert sd._build_parser().prog == "splash"


def test_cli_help_shows_tiers(capsys):
    with pytest.raises(SystemExit):
        sd.main(["--help"])
    out = capsys.readouterr().out
    for token in ("run", "sync", "status", "init", "target", "env"):
        assert token in out
    assert "provision" not in out  # old word is gone


def test_localconfig_missing_file_is_empty(tmp_path):
    lc = sd.LocalConfig.load(tmp_path / "splashdown.local.toml")
    assert lc.targets == {}


def test_localconfig_rejects_bad_variant_name(tmp_path):
    p = tmp_path / "splashdown.local.toml"
    p.write_text('[targets.simulator."has spaces"]\nmodel = "iPhone"\n')
    with pytest.raises(ValueError, match="variant name"):
        sd.LocalConfig.load(p)


def test_init_writes_recipe_and_local_skeleton(tmp_path):
    sd.cmd_init(tmp_path, preset="rn")
    recipe = (tmp_path / "splashdown.toml").read_text()
    assert "[resources." in recipe
    assert (tmp_path / "splashdown.local.toml").exists()


def test_init_does_not_clobber_existing_local(tmp_path):
    (tmp_path / "splashdown.local.toml").write_text('[targets.mine]\ntype = "simulator"\n')
    sd.cmd_init(tmp_path, preset="rn")
    assert "targets.mine" in (tmp_path / "splashdown.local.toml").read_text()


def test_cli_init_preset_is_positional(tmp_path, monkeypatch):
    """`splash init rn` (no --preset flag) writes the rn scaffold."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    rc = sd.main(["--cwd", str(tmp_path), "init", "rn"])
    assert rc == 0
    recipe = (tmp_path / "splashdown.toml").read_text()
    # rn preset declares the React Native profile + Metro port resource.
    assert 'profile = "react-native"' in recipe
    assert "RCT_METRO_PORT" in recipe


def test_cli_init_no_arg_runs_scanner(tmp_path, monkeypatch):
    """`splash init` with no positional kicks off the Scanner-driven flow."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "vite.config.ts").write_text("export default {}")
    rc = sd.main(["--cwd", str(tmp_path), "init"])
    assert rc == 0
    recipe = (tmp_path / "splashdown.toml").read_text()
    # Scanner emits the new shape with [apps.*] + a detected profile.
    assert "[apps." in recipe
    assert 'profile = "vite"' in recipe


def test_cli_init_no_arg_emits_rn_metro_port(tmp_path, monkeypatch):
    """Scanner-driven `splash init` on a react-native project emits the
    RCT_METRO_PORT port resource, just like the `rn` preset does."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "package.json").write_text('{"dependencies":{"react-native":"0.83"}}')
    rc = sd.main(["--cwd", str(tmp_path), "init"])
    assert rc == 0
    recipe = (tmp_path / "splashdown.toml").read_text()
    assert 'profile = "react-native"' in recipe
    assert "[resources.RCT_METRO_PORT]" in recipe
    assert 'resources = ["RCT_METRO_PORT"]' in recipe


def test_cli_init_rescan_updates_inventory(tmp_path, monkeypatch):
    # `init --rescan` re-detects apps in an existing recipe instead of scaffolding.
    (tmp_path / "splashdown.toml").write_text('[project]\nworkspace = "single"\nloader = "mise"\n')
    (tmp_path / "pubspec.yaml").write_text("name: demo\n")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    called = {}

    def _fake(cwd):
        called["cwd"] = cwd
        return 0

    monkeypatch.setattr(sd.cli, "cmd_refresh_inventory", _fake)
    rc = sd.main(["--cwd", str(tmp_path), "init", "--rescan"])
    assert rc == 0
    assert called["cwd"] == tmp_path


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
    cwd = tmp_path / "co"
    cwd.mkdir()
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
    cwd = tmp_path / "co"
    cwd.mkdir()
    (cwd / "splashdown.toml").write_text("""
[resources.PORT]
type  = "port"
range = [18900, 18910]
""")
    sd.main(["--cwd", str(cwd)])
    assert (cwd / "splashdown.local.toml").exists()
    assert "targets.simulator" in (cwd / "splashdown.local.toml").read_text()


def test_cli_provision_preserves_existing_local(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cwd = tmp_path / "co"
    cwd.mkdir()
    (cwd / "splashdown.toml").write_text("""
[resources.PORT]
type  = "port"
range = [18920, 18930]
""")
    (cwd / "splashdown.local.toml").write_text('[targets.mine]\ntype = "simulator"\n')
    sd.main(["--cwd", str(cwd)])
    assert "targets.mine" in (cwd / "splashdown.local.toml").read_text()


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
    sd.cmd_init(tmp_path, preset="minimal", loader_override="mise")
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
    sd.cmd_init(tmp_path, preset="minimal", loader_override="mise")
    sd.cmd_init(tmp_path, preset="minimal", force=True, loader_override="mise")
    mise = (tmp_path / "mise.toml").read_text()
    assert mise.count('_.file = "splashdown.env"') == 1


@pytest.mark.parametrize(
    "existing",
    [
        '[env]\n_.path = "./bin"\n',  # coexisting mise PATH directive
        '[env]\n_.file = "other.env"\n',  # different file value (dotted form)
        '[env._]\npath = ["./bin"]\n',  # subtable form
    ],
)
def test_mise_directive_edits_existing_underscore_table_in_place(tmp_path, existing):
    # Adding `_.file` must not crash or produce a double-declared/unparseable
    # `[env]._` when the table already exists.
    (tmp_path / "mise.toml").write_text(existing)
    sd.cmd_init(tmp_path, preset="minimal", force=True, loader_override="mise")
    text = (tmp_path / "mise.toml").read_text()
    data = tomllib.loads(text)  # must stay valid TOML
    assert data["env"]["_"]["file"] == "splashdown.env"


def test_init_writes_post_checkout_hook(tmp_path):
    sd.cmd_init(tmp_path, preset="minimal")
    hook = tmp_path / ".githooks" / "post-checkout"
    assert hook.exists()
    assert os.access(hook, os.X_OK)
    assert POST_CHECKOUT_SENTINEL in hook.read_text()
