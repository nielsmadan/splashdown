from __future__ import annotations

import json
import subprocess
import tomllib

import pytest

import splashdown as sd
from splashdown.loaders import _run_ok as _real_run_ok


def _record_run_ok(monkeypatch, *, ok=True):
    """Replace the autouse `_run_ok` stub with a recorder that captures the argv
    each loader `approve` would shell out with, and reports success/failure."""
    calls: list[tuple[list[str], object]] = []

    def fake(argv, cwd):
        calls.append((list(argv), cwd))
        return ok

    monkeypatch.setattr(sd.loaders, "_run_ok", fake)
    return calls


def test_direnv_loader_wire_appends_sentinel_block(tmp_path):
    sd.LOADERS["direnv"].wire(tmp_path, sd.ENV_FILE_NAME)
    text = (tmp_path / ".envrc").read_text()
    assert "# >>> splashdown-managed dotenv >>>" in text
    assert "dotenv_if_exists splashdown.env" in text
    assert "# <<< splashdown-managed dotenv <<<" in text


def test_direnv_loader_wire_idempotent(tmp_path):
    sd.LOADERS["direnv"].wire(tmp_path, sd.ENV_FILE_NAME)
    first = (tmp_path / ".envrc").read_text()
    sd.LOADERS["direnv"].wire(tmp_path, sd.ENV_FILE_NAME)
    assert (tmp_path / ".envrc").read_text() == first


def test_direnv_loader_wire_upgrades_legacy_dotenv_block(tmp_path):
    (tmp_path / ".envrc").write_text(
        "# >>> splashdown-managed dotenv >>>\n"
        "dotenv splashdown.env\n"
        "# <<< splashdown-managed dotenv <<<\n"
    )
    sd.LOADERS["direnv"].wire(tmp_path, sd.ENV_FILE_NAME)
    text = (tmp_path / ".envrc").read_text()
    assert "dotenv_if_exists splashdown.env" in text
    assert "\ndotenv splashdown.env\n" not in text


def test_direnv_loader_wire_preserves_existing_envrc(tmp_path):
    (tmp_path / ".envrc").write_text("use nix\nlayout python\n")
    sd.LOADERS["direnv"].wire(tmp_path, sd.ENV_FILE_NAME)
    text = (tmp_path / ".envrc").read_text()
    assert "use nix" in text
    assert "layout python" in text
    assert "dotenv_if_exists splashdown.env" in text


def test_devbox_loader_wire_adds_init_hook(tmp_path):
    (tmp_path / "devbox.json").write_text('{"packages": ["nodejs@22"]}')
    sd.LOADERS["devbox"].wire(tmp_path, sd.ENV_FILE_NAME)
    data = json.loads((tmp_path / "devbox.json").read_text())
    hooks = data.get("shell", {}).get("init_hook", [])
    assert any("splashdown.env" in h for h in hooks)


def test_devbox_loader_wire_preserves_existing_packages(tmp_path):
    (tmp_path / "devbox.json").write_text('{"packages": ["nodejs@22", "pnpm@9"]}')
    sd.LOADERS["devbox"].wire(tmp_path, sd.ENV_FILE_NAME)
    data = json.loads((tmp_path / "devbox.json").read_text())
    assert data["packages"] == ["nodejs@22", "pnpm@9"]


def test_devbox_loader_wire_idempotent(tmp_path):
    (tmp_path / "devbox.json").write_text("{}")
    sd.LOADERS["devbox"].wire(tmp_path, sd.ENV_FILE_NAME)
    first = (tmp_path / "devbox.json").read_text()
    sd.LOADERS["devbox"].wire(tmp_path, sd.ENV_FILE_NAME)
    assert (tmp_path / "devbox.json").read_text() == first


def test_react_native_profile_detects_via_package_json(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies": {"react-native": "0.83"}}')
    assert sd.PROFILES["react-native"].detect(tmp_path) is True


def test_expo_profile_detects_via_expo_dep_and_app_json(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies": {"expo": "50"}}')
    (tmp_path / "app.json").write_text("{}")
    assert sd.PROFILES["expo"].detect(tmp_path) is True


def test_flutter_profile_detects_via_pubspec(tmp_path):
    (tmp_path / "pubspec.yaml").write_text("name: x\n")
    assert sd.PROFILES["flutter"].detect(tmp_path) is True


def test_ios_native_profile_detects_via_xcworkspace(tmp_path):
    (tmp_path / "MyApp.xcworkspace").mkdir()
    assert sd.PROFILES["ios-native"].detect(tmp_path) is True


def test_android_native_profile_detects_via_gradle(tmp_path):
    (tmp_path / "build.gradle.kts").write_text("")
    (tmp_path / "settings.gradle.kts").write_text("")
    assert sd.PROFILES["android-native"].detect(tmp_path) is True


def test_react_native_profile_inherits_existing_wiring_checks(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies": {"react-native": "0.83"}}')
    app = sd.AppInventory(name="main", path=tmp_path, profile="react-native")
    checks = sd.PROFILES["react-native"].wiring_checks(app, "splashdown.env")
    ids = {c.id for c in checks}
    assert "hook" in ids
    assert "rn-metro-config" in ids
    assert "rn-pkg-port" in ids
    assert "rn-xcode-env" in ids


def test_mise_loader_unwire_deletes_solely_managed_file(tmp_path):
    sd.LOADERS["mise"].wire(tmp_path, sd.ENV_FILE_NAME)
    assert (tmp_path / "mise.toml").exists()
    sd.LOADERS["mise"].unwire(tmp_path, sd.ENV_FILE_NAME)
    assert not (tmp_path / "mise.toml").exists()


def test_mise_loader_unwire_keeps_user_content(tmp_path):
    (tmp_path / "mise.toml").write_text('[env]\nFOO = "bar"\n\n[tools]\nnode = "20"\n')
    sd.LOADERS["mise"].wire(tmp_path, sd.ENV_FILE_NAME)
    sd.LOADERS["mise"].unwire(tmp_path, sd.ENV_FILE_NAME)
    text = (tmp_path / "mise.toml").read_text()
    assert (tmp_path / "mise.toml").exists()
    assert '_.file = "splashdown.env"' not in text
    assert 'FOO = "bar"' in text
    assert 'node = "20"' in text


def test_mise_loader_unwire_drops_empty_env_table(tmp_path):
    (tmp_path / "mise.toml").write_text('[tools]\nnode = "20"\n')
    sd.LOADERS["mise"].wire(tmp_path, sd.ENV_FILE_NAME)
    sd.LOADERS["mise"].unwire(tmp_path, sd.ENV_FILE_NAME)
    text = (tmp_path / "mise.toml").read_text()
    assert "[env]" not in text
    assert 'node = "20"' in text


def test_mise_loader_unwire_noop_when_absent(tmp_path):
    sd.LOADERS["mise"].unwire(tmp_path, sd.ENV_FILE_NAME)
    assert not (tmp_path / "mise.toml").exists()


def test_direnv_loader_unwire_strips_block(tmp_path):
    (tmp_path / ".envrc").write_text("use nix\nlayout python\n")
    sd.LOADERS["direnv"].wire(tmp_path, sd.ENV_FILE_NAME)
    sd.LOADERS["direnv"].unwire(tmp_path, sd.ENV_FILE_NAME)
    text = (tmp_path / ".envrc").read_text()
    assert "use nix" in text
    assert "layout python" in text
    assert "splashdown.env" not in text
    assert "splashdown-managed" not in text


def test_direnv_loader_unwire_deletes_solely_managed_file(tmp_path):
    sd.LOADERS["direnv"].wire(tmp_path, sd.ENV_FILE_NAME)
    sd.LOADERS["direnv"].unwire(tmp_path, sd.ENV_FILE_NAME)
    assert not (tmp_path / ".envrc").exists()


def test_devbox_loader_unwire_removes_hook(tmp_path):
    (tmp_path / "devbox.json").write_text('{"packages": ["nodejs@22"]}')
    sd.LOADERS["devbox"].wire(tmp_path, sd.ENV_FILE_NAME)
    sd.LOADERS["devbox"].unwire(tmp_path, sd.ENV_FILE_NAME)
    data = json.loads((tmp_path / "devbox.json").read_text())
    hooks = data.get("shell", {}).get("init_hook", [])
    assert not any("splashdown.env" in h for h in hooks)
    assert data["packages"] == ["nodejs@22"]


def test_devbox_loader_unwire_deletes_solely_managed_file(tmp_path):
    (tmp_path / "devbox.json").write_text("{}")
    sd.LOADERS["devbox"].wire(tmp_path, sd.ENV_FILE_NAME)
    sd.LOADERS["devbox"].unwire(tmp_path, sd.ENV_FILE_NAME)
    assert not (tmp_path / "devbox.json").exists()


def test_mise_loader_wire_creates_config_and_is_idempotent(tmp_path):
    sd.LOADERS["mise"].wire(tmp_path, sd.ENV_FILE_NAME)
    first = (tmp_path / "mise.toml").read_text()
    assert '_.file = "splashdown.env"' in first
    sd.LOADERS["mise"].wire(tmp_path, sd.ENV_FILE_NAME)
    assert (tmp_path / "mise.toml").read_text() == first


def test_mise_loader_wire_keeps_user_content_when_editing_existing_file(tmp_path):
    (tmp_path / "mise.toml").write_text('[tools]\nnode = "20"\n')
    sd.LOADERS["mise"].wire(tmp_path, sd.ENV_FILE_NAME)
    text = (tmp_path / "mise.toml").read_text()
    assert '_.file = "splashdown.env"' in text
    assert 'node = "20"' in text


def test_direnv_loader_wire_manual_hint_only_when_editing_existing(tmp_path):
    assert sd.LOADERS["direnv"].wire(tmp_path, sd.ENV_FILE_NAME).hint == ""
    (tmp_path / ".envrc").write_text("use nix\n")
    assert "direnv allow" in sd.LOADERS["direnv"].wire(tmp_path, sd.ENV_FILE_NAME).hint


def test_devbox_loader_wire_is_idempotent(tmp_path):
    (tmp_path / "devbox.json").write_text("{}")
    sd.LOADERS["devbox"].wire(tmp_path, sd.ENV_FILE_NAME)
    first = (tmp_path / "devbox.json").read_text()
    sd.LOADERS["devbox"].wire(tmp_path, sd.ENV_FILE_NAME)
    assert (tmp_path / "devbox.json").read_text() == first


def test_none_loader_wire_writes_nothing(tmp_path):
    sd.LOADERS["none"].wire(tmp_path, sd.ENV_FILE_NAME)
    assert list(tmp_path.iterdir()) == []


def test_mise_loader_approve_invokes_mise_trust_with_config_path(tmp_path, monkeypatch):
    (tmp_path / "mise.toml").write_text("[env]\n")
    calls = _record_run_ok(monkeypatch, ok=True)
    assert sd.LOADERS["mise"].approve(tmp_path, env_file=sd.ENV_FILE_NAME) is True
    assert calls == [(["mise", "trust", str(tmp_path / "mise.toml")], tmp_path)]


def test_mise_loader_approve_targets_dot_mise_toml_when_only_that_exists(tmp_path, monkeypatch):
    (tmp_path / ".mise.toml").write_text("[env]\n")
    calls = _record_run_ok(monkeypatch, ok=True)
    sd.LOADERS["mise"].approve(tmp_path, env_file=sd.ENV_FILE_NAME)
    assert calls[0][0] == ["mise", "trust", str(tmp_path / ".mise.toml")]


def test_mise_loader_approve_noop_when_no_config(tmp_path, monkeypatch):
    calls = _record_run_ok(monkeypatch, ok=True)
    assert sd.LOADERS["mise"].approve(tmp_path, env_file=sd.ENV_FILE_NAME) is False
    assert calls == []


def test_mise_loader_approve_returns_false_on_command_failure(tmp_path):
    (tmp_path / "mise.toml").write_text("[env]\n")
    # The autouse _stub_loader_approval makes _run_ok return False.
    assert sd.LOADERS["mise"].approve(tmp_path, env_file=sd.ENV_FILE_NAME) is False


def test_direnv_loader_approve_invokes_direnv_allow_with_cwd(tmp_path, monkeypatch):
    (tmp_path / ".envrc").write_text("dotenv_if_exists splashdown.env\n")
    calls = _record_run_ok(monkeypatch, ok=True)
    assert sd.LOADERS["direnv"].approve(tmp_path, env_file=sd.ENV_FILE_NAME) is True
    assert calls == [(["direnv", "allow", str(tmp_path)], tmp_path)]


def test_direnv_loader_approve_noop_when_no_envrc(tmp_path, monkeypatch):
    calls = _record_run_ok(monkeypatch, ok=True)
    assert sd.LOADERS["direnv"].approve(tmp_path, env_file=sd.ENV_FILE_NAME) is False
    assert calls == []


def test_devbox_and_none_loader_approve_never_shell_out(tmp_path, monkeypatch):
    calls = _record_run_ok(monkeypatch, ok=True)
    assert sd.LOADERS["devbox"].approve(tmp_path, env_file=sd.ENV_FILE_NAME) is False
    assert sd.LOADERS["none"].approve(tmp_path, env_file=sd.ENV_FILE_NAME) is False
    assert calls == []


def test_mise_approve_announce_prints_success(tmp_path, monkeypatch, capsys):
    (tmp_path / "mise.toml").write_text("[env]\n")
    _record_run_ok(monkeypatch, ok=True)
    sd.LOADERS["mise"].approve(tmp_path, announce=True, env_file=sd.ENV_FILE_NAME)
    assert "trusted mise.toml" in capsys.readouterr().err


def test_mise_approve_announce_prints_fallback_on_failure(tmp_path, monkeypatch, capsys):
    (tmp_path / "mise.toml").write_text("[env]\n")
    _record_run_ok(monkeypatch, ok=False)
    sd.LOADERS["mise"].approve(tmp_path, announce=True, env_file=sd.ENV_FILE_NAME)
    assert "run `mise trust`" in capsys.readouterr().err


def test_approve_silent_when_not_announced(tmp_path, monkeypatch, capsys):
    (tmp_path / "mise.toml").write_text("[env]\n")
    _record_run_ok(monkeypatch, ok=True)
    sd.LOADERS["mise"].approve(tmp_path, env_file=sd.ENV_FILE_NAME)
    assert capsys.readouterr().err == ""


def test_run_ok_true_on_zero_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sd.loaders.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0)
    )
    assert _real_run_ok(["mise", "trust"], tmp_path) is True


def test_run_ok_false_on_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sd.loaders.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1)
    )
    assert _real_run_ok(["mise", "trust"], tmp_path) is False


def test_run_ok_false_on_missing_binary(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise FileNotFoundError

    monkeypatch.setattr(sd.loaders.subprocess, "run", boom)
    assert _real_run_ok(["mise", "trust"], tmp_path) is False


def test_run_ok_false_on_timeout(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="mise", timeout=10)

    monkeypatch.setattr(sd.loaders.subprocess, "run", boom)
    assert _real_run_ok(["mise", "trust"], tmp_path) is False


def test_mise_loader_owns_config_true_for_splashdown_only_config(tmp_path):
    sd.LOADERS["mise"].wire(tmp_path, sd.ENV_FILE_NAME)
    assert sd.LOADERS["mise"].owns_config(tmp_path, sd.ENV_FILE_NAME) is True


def test_mise_loader_owns_config_true_for_dot_mise_toml(tmp_path):
    (tmp_path / ".mise.toml").write_text("")
    sd.LOADERS["mise"].wire(tmp_path, sd.ENV_FILE_NAME)
    assert sd.LOADERS["mise"].owns_config(tmp_path, sd.ENV_FILE_NAME) is True


def test_mise_loader_owns_config_false_with_user_content(tmp_path):
    (tmp_path / "mise.toml").write_text('[tools]\nnode = "20"\n')
    sd.LOADERS["mise"].wire(tmp_path, sd.ENV_FILE_NAME)
    assert sd.LOADERS["mise"].owns_config(tmp_path, sd.ENV_FILE_NAME) is False


def test_mise_loader_owns_config_false_without_splashdown_directive(tmp_path):
    (tmp_path / "mise.toml").write_text('[tools]\nnode = "20"\n')
    assert sd.LOADERS["mise"].owns_config(tmp_path, sd.ENV_FILE_NAME) is False


def test_mise_loader_owns_config_false_for_a_malformed_config(tmp_path):
    (tmp_path / "mise.toml").write_text("[env\n_.file = ")
    assert sd.LOADERS["mise"].owns_config(tmp_path, sd.ENV_FILE_NAME) is False


def test_mise_loader_owns_config_false_for_empty_config(tmp_path):
    (tmp_path / "mise.toml").write_text("")
    assert sd.LOADERS["mise"].owns_config(tmp_path, sd.ENV_FILE_NAME) is False


def test_mise_loader_owns_config_false_when_config_missing(tmp_path):
    assert sd.LOADERS["mise"].owns_config(tmp_path, sd.ENV_FILE_NAME) is False


def test_direnv_loader_owns_config_true_for_splashdown_only_envrc(tmp_path):
    sd.LOADERS["direnv"].wire(tmp_path, sd.ENV_FILE_NAME)
    assert sd.LOADERS["direnv"].owns_config(tmp_path, sd.ENV_FILE_NAME) is True


def test_direnv_loader_owns_config_false_with_user_content(tmp_path):
    (tmp_path / ".envrc").write_text("use nix\n")
    sd.LOADERS["direnv"].wire(tmp_path, sd.ENV_FILE_NAME)
    assert sd.LOADERS["direnv"].owns_config(tmp_path, sd.ENV_FILE_NAME) is False


def test_direnv_loader_owns_config_false_without_splashdown_block(tmp_path):
    (tmp_path / ".envrc").write_text("use nix\n")
    assert sd.LOADERS["direnv"].owns_config(tmp_path, sd.ENV_FILE_NAME) is False


def test_direnv_loader_owns_config_false_for_empty_envrc(tmp_path):
    (tmp_path / ".envrc").write_text("")
    assert sd.LOADERS["direnv"].owns_config(tmp_path, sd.ENV_FILE_NAME) is False


def test_direnv_loader_owns_config_false_when_envrc_missing(tmp_path):
    assert sd.LOADERS["direnv"].owns_config(tmp_path, sd.ENV_FILE_NAME) is False


@pytest.mark.parametrize(
    ("loader", "name", "content"),
    [
        ("mise", "mise.toml", '[env]\n_.file = "splashdown.env" # splashdown-managed\n'),
        (
            "direnv",
            ".envrc",
            "# >>> splashdown-managed dotenv >>>\n"
            "dotenv_if_exists splashdown.env\n"
            "# <<< splashdown-managed dotenv <<<\n",
        ),
    ],
)
def test_loader_owns_config_false_for_a_symlinked_config(tmp_path, loader, name, content):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "target"
    target.write_text(content)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / name).symlink_to(target)
    assert sd.LOADERS[loader].owns_config(checkout, sd.ENV_FILE_NAME) is False


def _no_loader_writes(monkeypatch):
    """Record every safe-file write a loader attempts, without performing it."""
    calls: list[str] = []
    monkeypatch.setattr(
        sd.loaders, "atomic_write_text", lambda path, _text, **_kw: calls.append(str(path))
    )
    return calls


def test_no_loader_writes_records_a_genuine_loader_write(tmp_path, monkeypatch):
    writes = _no_loader_writes(monkeypatch)
    sd.LOADERS["mise"].wire(tmp_path, sd.ENV_FILE_NAME)
    assert writes == [str(tmp_path / "mise.toml")]


@pytest.mark.parametrize(
    "existing",
    [
        '[env]\n_.file = "splashdown.env"\n',
        '[env]\n_.file = "./splashdown.env"\n',
        '[env]\n_.file = ["splashdown.env", "other.env"]\n',
        '[env]\n_.file = ["other.env", "./splashdown.env"]\n',
        '[env._]\nfile = "splashdown.env"\n',
    ],
)
def test_mise_loader_wire_reuses_an_existing_file_directive(tmp_path, monkeypatch, existing):
    (tmp_path / "mise.toml").write_text(existing)
    before = (tmp_path / "mise.toml").read_bytes()
    writes = _no_loader_writes(monkeypatch)
    plan = sd.LOADERS["mise"].wire(tmp_path, sd.ENV_FILE_NAME)
    assert plan.status == "reused"
    assert writes == []
    assert (tmp_path / "mise.toml").read_bytes() == before


def test_mise_loader_wire_appends_to_a_list_form_file_directive(tmp_path):
    (tmp_path / "mise.toml").write_text('[env]\n_.file = ["a.env", "b.env"]\n')
    sd.LOADERS["mise"].wire(tmp_path, sd.ENV_FILE_NAME)
    data = tomllib.loads((tmp_path / "mise.toml").read_text())
    assert data["env"]["_"]["file"] == ["a.env", "b.env", "splashdown.env"]


def test_mise_loader_wire_keeps_a_different_single_env_file(tmp_path):
    (tmp_path / "mise.toml").write_text('[env]\n_.file = "other.env"\nFOO = "bar"\n')
    sd.LOADERS["mise"].wire(tmp_path, sd.ENV_FILE_NAME)
    data = tomllib.loads((tmp_path / "mise.toml").read_text())
    assert data["env"]["_"]["file"] == ["other.env", "splashdown.env"]
    assert data["env"]["FOO"] == "bar"


def test_mise_loader_wire_is_idempotent_on_a_list_it_extended(tmp_path):
    (tmp_path / "mise.toml").write_text('[env]\n_.file = ["a.env"]\n')
    sd.LOADERS["mise"].wire(tmp_path, sd.ENV_FILE_NAME)
    first = (tmp_path / "mise.toml").read_bytes()
    sd.LOADERS["mise"].wire(tmp_path, sd.ENV_FILE_NAME)
    assert (tmp_path / "mise.toml").read_bytes() == first


@pytest.mark.parametrize(
    "existing",
    ["[env]\n_.file = 3\n", '[env]\n_.file = { a = "b" }\n', '[env]\n_.file = ["a.env", 3]\n'],
)
def test_mise_loader_plan_rejects_an_unusable_file_slot(tmp_path, existing):
    (tmp_path / "mise.toml").write_text(existing)
    with pytest.raises(sd.LoaderConflictError) as error:
        sd.LOADERS["mise"].plan(tmp_path, sd.ENV_FILE_NAME)
    assert "--loader none" in str(error.value)
    assert (tmp_path / "mise.toml").read_text() == existing


def test_mise_loader_plan_rejects_malformed_toml(tmp_path):
    (tmp_path / "mise.toml").write_text("[env\n_.file = ")
    with pytest.raises(sd.LoaderConflictError):
        sd.LOADERS["mise"].plan(tmp_path, sd.ENV_FILE_NAME)


def test_mise_loader_wire_edits_an_existing_dot_mise_toml(tmp_path):
    (tmp_path / ".mise.toml").write_text('[tools]\nnode = "20"\n')
    sd.LOADERS["mise"].wire(tmp_path, sd.ENV_FILE_NAME)
    assert not (tmp_path / "mise.toml").exists()
    assert 'node = "20"' in (tmp_path / ".mise.toml").read_text()
    assert "splashdown.env" in (tmp_path / ".mise.toml").read_text()


def test_mise_loader_unwire_keeps_a_user_authored_directive(tmp_path):
    original = '[env]\n_.file = "splashdown.env"\n'
    (tmp_path / "mise.toml").write_text(original)
    sd.LOADERS["mise"].wire(tmp_path, sd.ENV_FILE_NAME)
    sd.LOADERS["mise"].unwire(tmp_path, sd.ENV_FILE_NAME)
    assert (tmp_path / "mise.toml").read_text() == original


def test_mise_loader_unwire_removes_only_the_entry_it_added(tmp_path):
    (tmp_path / "mise.toml").write_text('[env]\n_.file = ["a.env"]\n')
    sd.LOADERS["mise"].wire(tmp_path, sd.ENV_FILE_NAME)
    sd.LOADERS["mise"].unwire(tmp_path, sd.ENV_FILE_NAME)
    data = tomllib.loads((tmp_path / "mise.toml").read_text())
    assert data["env"]["_"]["file"] == ["a.env"]


def test_mise_loader_wire_keeps_the_comment_on_a_list_it_widens(tmp_path):
    (tmp_path / "mise.toml").write_text('[env]\n_.file = ["a.env"] # keep this\n')
    sd.LOADERS["mise"].wire(tmp_path, sd.ENV_FILE_NAME)
    assert (tmp_path / "mise.toml").read_text() == (
        '[env]\n_.file = ["a.env", "splashdown.env"] # keep this splashdown-managed\n'
    )


def test_mise_loader_wire_keeps_the_comment_on_a_string_it_widens(tmp_path):
    (tmp_path / "mise.toml").write_text('[env]\n_.file = "a.env" # keep this\n')
    sd.LOADERS["mise"].wire(tmp_path, sd.ENV_FILE_NAME)
    assert (tmp_path / "mise.toml").read_text() == (
        '[env]\n_.file = ["a.env", "splashdown.env"] # keep this splashdown-managed\n'
    )


def test_mise_loader_unwire_restores_the_comment_it_marked(tmp_path):
    original = '[env]\n_.file = ["a.env"] # keep this\n'
    (tmp_path / "mise.toml").write_text(original)
    sd.LOADERS["mise"].wire(tmp_path, sd.ENV_FILE_NAME)
    sd.LOADERS["mise"].unwire(tmp_path, sd.ENV_FILE_NAME)
    assert (tmp_path / "mise.toml").read_text() == original


def test_mise_loader_unwire_leaves_a_widened_string_slot_as_a_list(tmp_path):
    (tmp_path / "mise.toml").write_text('[env]\n_.file = "other.env"\n')
    sd.LOADERS["mise"].wire(tmp_path, sd.ENV_FILE_NAME)
    sd.LOADERS["mise"].unwire(tmp_path, sd.ENV_FILE_NAME)
    assert (tmp_path / "mise.toml").read_text() == '[env]\n_.file = ["other.env"]\n'


@pytest.mark.parametrize(
    "line",
    [
        "dotenv splashdown.env",
        "dotenv_if_exists splashdown.env",
        "dotenv_if_exists ./splashdown.env",
        'dotenv_if_exists "splashdown.env"',
        "dotenv_if_exists 'splashdown.env'",
        "dotenv_if_exists splashdown.env  # project values",
        "dotenv_if_exists\tsplashdown.env",
    ],
)
def test_direnv_loader_wire_reuses_an_unmarked_user_directive(tmp_path, monkeypatch, line):
    (tmp_path / ".envrc").write_text(f"use nix\n{line}\n")
    before = (tmp_path / ".envrc").read_bytes()
    writes = _no_loader_writes(monkeypatch)
    plan = sd.LOADERS["direnv"].wire(tmp_path, sd.ENV_FILE_NAME)
    assert plan.status == "reused"
    assert writes == []
    assert (tmp_path / ".envrc").read_bytes() == before


@pytest.mark.parametrize(
    "line",
    [
        "# dotenv_if_exists splashdown.env",
        "  dotenv_if_exists splashdown.env",
        "if [ -f splashdown.env ]; then dotenv splashdown.env; fi",
        "echo splashdown.env",
        "dotenv_if_exists other.env",
        "dotenv_if_exists splashdown.env.example",
        "dotenv_if_exists splashdown.env#foo",
        'dotenv_if_exists "splashdown.env"#foo',
    ],
)
def test_direnv_loader_wire_adds_its_block_for_unrecognized_shapes(tmp_path, line):
    (tmp_path / ".envrc").write_text(f"{line}\n")
    plan = sd.LOADERS["direnv"].wire(tmp_path, sd.ENV_FILE_NAME)
    assert plan.status == "updated"
    text = (tmp_path / ".envrc").read_text()
    assert line in text
    assert "# >>> splashdown-managed dotenv >>>" in text


def test_direnv_loader_wire_adds_no_second_directive_to_its_own_block(tmp_path):
    sd.LOADERS["direnv"].wire(tmp_path, sd.ENV_FILE_NAME)
    sd.LOADERS["direnv"].wire(tmp_path, sd.ENV_FILE_NAME)
    assert (tmp_path / ".envrc").read_text().count("dotenv_if_exists splashdown.env") == 1


def test_direnv_loader_unwire_keeps_a_user_authored_directive(tmp_path):
    original = "use nix\ndotenv_if_exists splashdown.env\n"
    (tmp_path / ".envrc").write_text(original)
    sd.LOADERS["direnv"].wire(tmp_path, sd.ENV_FILE_NAME)
    sd.LOADERS["direnv"].unwire(tmp_path, sd.ENV_FILE_NAME)
    assert (tmp_path / ".envrc").read_text() == original


@pytest.mark.parametrize(
    "hook",
    [
        "set -a; source splashdown.env; set +a",
        "set -a\nsource ./splashdown.env\nset +a",
        'set -a; . "splashdown.env"; set +a',
        "set -o allexport; source splashdown.env; set +o allexport",
        "echo hi; set -a; source splashdown.env; set +a",
        "set -a\nsource splashdown.env  # ports and hosts\nset +a",
    ],
    ids=[
        "one-line",
        "multi-line-dot-slash",
        "dot-and-quotes",
        "allexport",
        "after-another-statement",
        "trailing-comment",
    ],
)
def test_devbox_loader_wire_reuses_an_existing_source_hook(tmp_path, monkeypatch, hook):
    (tmp_path / "devbox.json").write_text(json.dumps({"shell": {"init_hook": [hook]}}, indent=2))
    before = (tmp_path / "devbox.json").read_bytes()
    writes = _no_loader_writes(monkeypatch)
    plan = sd.LOADERS["devbox"].wire(tmp_path, sd.ENV_FILE_NAME)
    assert plan.status == "reused"
    assert writes == []
    assert (tmp_path / "devbox.json").read_bytes() == before


def test_devbox_loader_wire_reuses_a_string_valued_init_hook(tmp_path, monkeypatch):
    (tmp_path / "devbox.json").write_text(
        json.dumps({"shell": {"init_hook": "set -a; source splashdown.env; set +a"}}, indent=2)
    )
    before = (tmp_path / "devbox.json").read_bytes()
    writes = _no_loader_writes(monkeypatch)
    assert sd.LOADERS["devbox"].wire(tmp_path, sd.ENV_FILE_NAME).status == "reused"
    assert writes == []
    assert (tmp_path / "devbox.json").read_bytes() == before


@pytest.mark.parametrize(
    "hook",
    [
        "source splashdown.env",
        "# set -a; source splashdown.env; set +a",
        "set -a\n# source splashdown.env\nset +a",
        "set -a\n# later; source splashdown.env",
        "set -a; if [ -f splashdown.env ]; then source splashdown.env; fi; set +a",
        'if [ "$CI" = "1" ]; then\n  set -a\n  source splashdown.env\n  set +a\nfi',
        "load_env() {\n  set -a\n  source splashdown.env\n  set +a\n}",
        "set -a && source splashdown.env",
        "set -a; source other.env; set +a",
        "set -a; echo splashdown.env; set +a",
        "set -a; source a.env; set +a; source splashdown.env",
        'if [ "$CI" = "1" ]; then\n\x0cset -a\n\x0csource splashdown.env\n\x0cset +a\nfi',
        "set -a\r\nsource splashdown.env\r\nset +a",
    ],
    ids=[
        "source-without-export",
        "whole-line-comment",
        "commented-source-line",
        "comment-tail-holding-a-semicolon",
        "one-line-conditional",
        "multi-line-conditional",
        "uncalled-function",
        "and-chain",
        "another-file",
        "mentioned-not-sourced",
        "allexport-turned-back-off",
        "form-feed-indented-block",
        "carriage-returns",
    ],
)
def test_devbox_loader_wire_adds_its_hook_for_unrecognized_shapes(tmp_path, hook):
    (tmp_path / "devbox.json").write_text(json.dumps({"shell": {"init_hook": [hook]}}, indent=2))
    sd.LOADERS["devbox"].wire(tmp_path, sd.ENV_FILE_NAME)
    hooks = json.loads((tmp_path / "devbox.json").read_text())["shell"]["init_hook"]
    assert hooks[0] == hook
    assert any("# splashdown-managed" in entry for entry in hooks)


def test_devbox_loader_wire_reuses_a_statement_in_an_unindented_block_body(tmp_path, monkeypatch):
    """Pins a known limitation: recognition is textual, so a `then` body at column 0
    reads as top level and is reused even though it runs only under the condition."""
    hook = 'if [ "$CI" = "1" ]; then\nset -a\nsource splashdown.env\nset +a\nfi'
    (tmp_path / "devbox.json").write_text(json.dumps({"shell": {"init_hook": [hook]}}, indent=2))
    writes = _no_loader_writes(monkeypatch)
    assert sd.LOADERS["devbox"].wire(tmp_path, sd.ENV_FILE_NAME).status == "reused"
    assert writes == []


def test_devbox_loader_unwire_keeps_a_user_authored_hook(tmp_path):
    original = json.dumps(
        {"shell": {"init_hook": ["set -a; source splashdown.env; set +a"]}}, indent=2
    )
    (tmp_path / "devbox.json").write_text(original)
    sd.LOADERS["devbox"].wire(tmp_path, sd.ENV_FILE_NAME)
    sd.LOADERS["devbox"].unwire(tmp_path, sd.ENV_FILE_NAME)
    assert (tmp_path / "devbox.json").read_text() == original


def test_devbox_loader_plan_rejects_malformed_json(tmp_path):
    (tmp_path / "devbox.json").write_text("{ not json")
    with pytest.raises(sd.LoaderConflictError) as error:
        sd.LOADERS["devbox"].plan(tmp_path, sd.ENV_FILE_NAME)
    assert "--loader none" in str(error.value)


@pytest.mark.parametrize(
    "existing",
    ['{"shell": []}', '{"shell": {"init_hook": 3}}', "[]"],
)
def test_devbox_loader_plan_rejects_an_unusable_config_shape(tmp_path, existing):
    (tmp_path / "devbox.json").write_text(existing)
    with pytest.raises(sd.LoaderConflictError):
        sd.LOADERS["devbox"].plan(tmp_path, sd.ENV_FILE_NAME)
    assert (tmp_path / "devbox.json").read_text() == existing


def test_devbox_loader_wire_preserves_non_string_hook_entries(tmp_path):
    (tmp_path / "devbox.json").write_text(json.dumps({"shell": {"init_hook": [{"a": 1}]}}))
    sd.LOADERS["devbox"].wire(tmp_path, sd.ENV_FILE_NAME)
    hooks = json.loads((tmp_path / "devbox.json").read_text())["shell"]["init_hook"]
    assert hooks[0] == {"a": 1}


@pytest.mark.parametrize(
    ("loader", "name"),
    [("mise", "mise.toml"), ("direnv", ".envrc"), ("devbox", "devbox.json")],
)
def test_loader_plan_refuses_a_symlinked_config(tmp_path, loader, name):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "target"
    target.write_text("{}")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / name).symlink_to(target)
    with pytest.raises(sd.LoaderConflictError) as error:
        sd.LOADERS[loader].plan(checkout, sd.ENV_FILE_NAME)
    assert "symlink" in str(error.value)
    assert target.read_text() == "{}"


def test_devbox_loader_wire_drops_its_own_hook_when_a_user_hook_appears(tmp_path):
    (tmp_path / "devbox.json").write_text("{}")
    sd.LOADERS["devbox"].wire(tmp_path, sd.ENV_FILE_NAME)
    data = json.loads((tmp_path / "devbox.json").read_text())
    data["shell"]["init_hook"].insert(0, "set -a; source splashdown.env; set +a")
    (tmp_path / "devbox.json").write_text(json.dumps(data, indent=2))
    plan = sd.LOADERS["devbox"].wire(tmp_path, sd.ENV_FILE_NAME)
    assert plan.status == "reused"
    hooks = json.loads((tmp_path / "devbox.json").read_text())["shell"]["init_hook"]
    assert hooks == ["set -a; source splashdown.env; set +a"]


def test_none_loader_plan_writes_nothing_and_reports_nothing(tmp_path):
    plan = sd.LOADERS["none"].wire(tmp_path, sd.ENV_FILE_NAME)
    assert (plan.status, plan.note, plan.writes) == ("nothing", "", False)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("loader", ["mise", "direnv", "devbox"])
def test_loader_wires_the_configured_destination(tmp_path, loader):
    sd.LOADERS[loader].wire(tmp_path, ".env")
    text = "".join(path.read_text() for path in tmp_path.iterdir() if path.is_file())
    assert ".env" in text
    assert "splashdown.env" not in text


@pytest.mark.parametrize("loader", ["mise", "direnv", "devbox"])
def test_loader_unwires_the_configured_destination(tmp_path, loader):
    sd.LOADERS[loader].wire(tmp_path, ".env")
    sd.LOADERS[loader].unwire(tmp_path, ".env")
    assert [path.name for path in tmp_path.iterdir()] == []


def test_mise_wired_elsewhere_is_not_reclaimed_for_another_destination(tmp_path):
    sd.LOADERS["mise"].wire(tmp_path, "splashdown.env")
    plan = sd.LOADERS["mise"].plan(tmp_path, ".env")
    assert plan.status == "updated"
    assert ".env" in plan.note


def test_direnv_reuses_a_user_directive_for_the_configured_destination(tmp_path):
    (tmp_path / ".envrc").write_text("dotenv_if_exists .env\n")
    plan = sd.LOADERS["direnv"].plan(tmp_path, ".env")
    assert plan.status == "reused"
    assert plan.writes is False


def test_devbox_reuses_a_user_hook_for_the_configured_destination(tmp_path):
    (tmp_path / "devbox.json").write_text(
        json.dumps({"shell": {"init_hook": ["set -a; source .env; set +a"]}})
    )
    plan = sd.LOADERS["devbox"].plan(tmp_path, ".env")
    assert plan.status == "reused"
    assert plan.writes is False


def test_devbox_loader_wire_keeps_the_formatting_it_did_not_write(tmp_path):
    original = '{\n\t"packages": [\n\t\t"nodejs@22"\n\t],\n\t"env": { "A": "1" }\n}\n'
    (tmp_path / "devbox.json").write_text(original)

    sd.LOADERS["devbox"].wire(tmp_path, sd.ENV_FILE_NAME)

    text = (tmp_path / "devbox.json").read_text()
    assert '\t"packages": [\n\t\t"nodejs@22"\n\t]' in text
    assert '"env": { "A": "1" }' in text
    assert json.loads(text)["shell"]["init_hook"]


def test_devbox_loader_unwire_restores_the_original_bytes(tmp_path):
    original = '{\n\t"packages": [\n\t\t"nodejs@22"\n\t]\n}\n'
    (tmp_path / "devbox.json").write_text(original)

    sd.LOADERS["devbox"].wire(tmp_path, sd.ENV_FILE_NAME)
    sd.LOADERS["devbox"].unwire(tmp_path, sd.ENV_FILE_NAME)

    assert (tmp_path / "devbox.json").read_text() == original


def test_devbox_loader_unwire_keeps_a_user_init_hook_and_its_layout(tmp_path):
    original = '{\n  "shell": {\n    "init_hook": [\n      "echo hi"\n    ]\n  }\n}\n'
    (tmp_path / "devbox.json").write_text(original)

    sd.LOADERS["devbox"].wire(tmp_path, sd.ENV_FILE_NAME)
    sd.LOADERS["devbox"].unwire(tmp_path, sd.ENV_FILE_NAME)

    assert (tmp_path / "devbox.json").read_text() == original


def test_devbox_loader_wire_extends_a_single_line_document_in_place(tmp_path):
    (tmp_path / "devbox.json").write_text('{"packages": ["nodejs@22"]}\n')

    sd.LOADERS["devbox"].wire(tmp_path, sd.ENV_FILE_NAME)

    text = (tmp_path / "devbox.json").read_text()
    assert text.startswith('{"packages": ["nodejs@22"], "shell": {')
    assert json.loads(text)["shell"]["init_hook"]
