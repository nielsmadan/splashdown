"""Tests for splashdown provisioning behavior."""

from __future__ import annotations

import pytest

import splashdown as sd
from conftest import (
    _write_recipe,
)


def test_provision_writes_splashdown_env(registry, checkout):
    _write_recipe(
        checkout,
        """
[resources.PORT]
type  = "port"
range = [18400, 18410]

[resources.RUN_ID]
type = "uuid"

[resources.URL]
type     = "template"
template = "http://localhost:{{ PORT }}"
""",
    )
    resolved = sd.provision(checkout, registry=registry)
    assert 18400 <= int(resolved["PORT"]) <= 18410
    assert resolved["URL"] == f"http://localhost:{resolved['PORT']}"
    assert len(resolved["RUN_ID"]) == 36  # uuid string length

    recipe = sd.Recipe.load(checkout / "splashdown.toml")
    sd.write_outputs(checkout, recipe, resolved)
    text = (checkout / "splashdown.env").read_text()
    assert f"PORT={resolved['PORT']}" in text
    assert "URL=" in text


def test_provision_idempotent(registry, checkout):
    _write_recipe(
        checkout,
        """
[resources.RUN_ID]
type = "uuid"
[resources.PORT]
type  = "port"
range = [18500, 18510]
""",
    )
    r1 = sd.provision(checkout, registry=registry)
    r2 = sd.provision(checkout, registry=registry)
    assert r1 == r2


def test_provision_reprovision_regenerates_uuid(registry, checkout):
    _write_recipe(
        checkout,
        """
[resources.RUN_ID]
type = "uuid"
""",
    )
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
    assert "MSG='has spaces'" in text
    # A plain URL has no spaces; ':' and '/' are allowed unquoted.
    assert "URL=http://localhost:8082" in text


def test_splashdown_env_writer_neutralizes_shell_injection(tmp_path):
    # The file is `source`d by devbox / the no-loader fallback. A value with a
    # command substitution must be SINGLE-quoted so bash won't execute it.
    target = tmp_path / "splashdown.env"
    sd.write_splashdown_env(target, {"X": "$(touch /tmp/pwned)", "Y": "`id`"})
    text = target.read_text()
    assert "X='$(touch /tmp/pwned)'" in text
    assert "Y='`id`'" in text
    assert '"$(' not in text  # never double-quoted


def test_splashdown_env_writer_overwrites_wholesale(tmp_path):
    target = tmp_path / "splashdown.env"
    target.write_text("STALE=1\nOLD=2\n")
    sd.write_splashdown_env(target, {"PORT": "8082"})
    text = target.read_text()
    assert "STALE" not in text
    assert text.strip() == "PORT=8082"


def test_envfile_writer(registry, checkout):
    _write_recipe(
        checkout,
        """
[resources.MY_VAR]
type     = "template"
template = "hello"
writer   = "envfile=.env.local"
""",
    )
    resolved = sd.provision(checkout, registry=registry)
    recipe = sd.Recipe.load(checkout / "splashdown.toml")
    sd.write_outputs(checkout, recipe, resolved)
    text = (checkout / ".env.local").read_text()
    assert "MY_VAR=hello" in text


def test_envfile_writer_rejects_escaping_relative_path(registry, checkout):
    # The recipe is auto-run from the post-checkout hook, so an `envfile=` path
    # that escapes the checkout would be an arbitrary-file-write primitive.
    _write_recipe(
        checkout,
        """
[resources.MY_VAR]
type     = "template"
template = "hello"
writer   = "envfile=../escape.env"
""",
    )
    resolved = sd.provision(checkout, registry=registry)
    recipe = sd.Recipe.load(checkout / "splashdown.toml")
    with pytest.raises(ValueError, match="outside the checkout"):
        sd.write_outputs(checkout, recipe, resolved)
    assert not (checkout.parent / "escape.env").exists()


def test_envfile_writer_rejects_absolute_path(registry, checkout, tmp_path):
    abs_target = tmp_path / "abs_escape.env"
    _write_recipe(
        checkout,
        f"""
[resources.MY_VAR]
type     = "template"
template = "hello"
writer   = "envfile={abs_target}"
""",
    )
    resolved = sd.provision(checkout, registry=registry)
    recipe = sd.Recipe.load(checkout / "splashdown.toml")
    with pytest.raises(ValueError, match="outside the checkout"):
        sd.write_outputs(checkout, recipe, resolved)
    assert not abs_target.exists()


def test_writer_reports_changed_then_unchanged(tmp_path):
    target = tmp_path / "splashdown.env"
    assert sd.write_splashdown_env(target, {"PORT": "8082"}) is True  # created
    mtime = target.stat().st_mtime_ns
    assert sd.write_splashdown_env(target, {"PORT": "8082"}) is False  # identical
    assert target.stat().st_mtime_ns == mtime  # untouched
    assert sd.write_splashdown_env(target, {"PORT": "9000"}) is True  # value changed


def test_envfile_writer_reports_changed(tmp_path):
    target = tmp_path / ".env.local"
    assert sd.write_envfile(target, {"MY_VAR": "hello"}) is True
    assert sd.write_envfile(target, {"MY_VAR": "hello"}) is False


def test_envfile_writer_quotes_unsafe_values(tmp_path):
    # A value with a space must be quoted so the dotenv line stays parseable;
    # a safe value stays bare (consistent with write_splashdown_env).
    target = tmp_path / ".env.local"
    sd.write_envfile(target, {"MSG": "hello world", "PORT": "8082"})
    text = target.read_text()
    assert "MSG='hello world'" in text
    assert "PORT=8082" in text


def test_provision_noop_prints_up_to_date(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "splashdown.toml").write_text("""
[resources.PORT]
type  = "port"
range = [19100, 19110]
[resources.RUN_ID]
type = "uuid"
""")
    assert sd.main(["--cwd", str(tmp_path)]) == 0
    capsys.readouterr()  # discard first-run (changed) output
    assert sd.main(["--cwd", str(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert err.strip() == "splashdown: up to date (2 vars, 1 files)"


def test_provision_changed_prints_only_changes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "splashdown.toml").write_text("""
[resources.RUN_ID]
type = "uuid"
""")
    assert sd.main(["--cwd", str(tmp_path)]) == 0
    capsys.readouterr()
    # --force regenerates RUN_ID, so exactly that var + its writer report.
    assert sd.main(["--cwd", str(tmp_path), "sync", "--force"]) == 0
    err = capsys.readouterr().err
    assert "RUN_ID=" in err
    assert f"-> {sd.ENV_FILE_NAME}: 1 vars (changed)" in err
    assert "up to date" not in err


def test_cwd_resource_type(registry, tmp_path):
    cwd = tmp_path / "mybranch"
    cwd.mkdir()
    _write_recipe(
        cwd,
        """
[resources.NAME]
type = "cwd"
""",
    )
    resolved = sd.provision(cwd, registry=registry)
    assert resolved["NAME"] == "mybranch"


def test_set_type_uses_default(registry, checkout):
    _write_recipe(
        checkout,
        """
[resources.MODE]
type    = "set"
default = "dev"
""",
    )
    resolved = sd.provision(checkout, registry=registry)
    assert resolved["MODE"] == "dev"


def test_set_type_persists_user_value(registry, checkout):
    _write_recipe(
        checkout,
        """
[resources.MODE]
type = "set"
""",
    )
    registry.set_kv(str(checkout.resolve()), "MODE", "prod")
    resolved = sd.provision(checkout, registry=registry)
    assert resolved["MODE"] == "prod"


def test_cwd_slug_resource_type(registry, tmp_path):
    cwd = tmp_path / "My App"
    cwd.mkdir()
    _write_recipe(cwd, '[resources.SLUG]\ntype = "cwd-slug"\n')
    resolved = sd.provision(cwd, registry=registry)
    assert resolved["SLUG"] == "my-app"


def test_set_resource_without_default_errors(registry, checkout):
    _write_recipe(checkout, '[resources.MODE]\ntype = "set"\n')
    with pytest.raises(ValueError):
        sd.provision(checkout, registry=registry)


def test_invalid_port_range_errors(registry, checkout):
    _write_recipe(checkout, '[resources.P]\ntype = "port"\nrange = 8000\n')
    with pytest.raises(ValueError):
        sd.provision(checkout, registry=registry)


def test_run_setup_runs_commands_and_reports(tmp_path):
    recipe = sd.Recipe({"setup": {"dev": {"run": ["echo hi"]}}}, tmp_path / "splashdown.toml")
    assert sd.run_setup(tmp_path, recipe, "dev", {}) == ["setup.dev: echo hi"]


def test_run_setup_reports_failure(tmp_path):
    recipe = sd.Recipe({"setup": {"dev": {"run": ["exit 3"]}}}, tmp_path / "splashdown.toml")
    msgs = sd.run_setup(tmp_path, recipe, "dev", {})
    assert len(msgs) == 1 and "FAILED" in msgs[0] and "exit 3" in msgs[0]


def test_write_envrc_preserves_unmanaged_and_replaces_managed(tmp_path):
    target = tmp_path / ".envrc.local"
    target.write_text("export OLD='x'\n# keep me\n")
    sd.write_envrc(target, {"NEW": "hello"})
    text = target.read_text()
    assert "export OLD='x'" in text and "# keep me" in text
    assert "export NEW='hello'" in text
    sd.write_envrc(target, {"NEW": "again"})  # replace in place, not duplicate
    text2 = target.read_text()
    assert text2.count("export NEW=") == 1
    assert "export NEW='again'" in text2


def test_write_envfile_preserves_unmanaged(tmp_path):
    target = tmp_path / ".env.local"
    target.write_text("UNMANAGED=x\n")
    sd.write_envfile(target, {"MY": "hello"})
    text = target.read_text()
    assert "UNMANAGED=x" in text and "MY=hello" in text


def test_stdout_writer_prints_keyvalue(registry, checkout, capsys):
    _write_recipe(
        checkout, '[resources.MSG]\ntype = "template"\ntemplate = "hi"\nwriter = "stdout"\n'
    )
    recipe = sd.Recipe.load(checkout / sd.RECIPE_NAME)
    resolved = sd.provision(checkout, registry=registry)
    sd.write_outputs(checkout, recipe, resolved)
    assert "MSG=hi" in capsys.readouterr().out


def test_none_writer_creates_no_file(registry, checkout):
    _write_recipe(checkout, '[resources.X]\ntype = "template"\ntemplate = "v"\nwriter = "none"\n')
    recipe = sd.Recipe.load(checkout / sd.RECIPE_NAME)
    resolved = sd.provision(checkout, registry=registry)
    msgs = sd.write_outputs(checkout, recipe, resolved)
    assert any("registry-only" in m for m, _ in msgs)
    assert not (checkout / sd.ENV_FILE_NAME).exists()
