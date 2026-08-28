from __future__ import annotations

import json
import subprocess

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
    sd.LOADERS["direnv"].wire(tmp_path)
    text = (tmp_path / ".envrc").read_text()
    assert "# >>> splashdown-managed dotenv >>>" in text
    assert "dotenv_if_exists splashdown.env" in text
    assert "# <<< splashdown-managed dotenv <<<" in text


def test_direnv_loader_wire_idempotent(tmp_path):
    sd.LOADERS["direnv"].wire(tmp_path)
    first = (tmp_path / ".envrc").read_text()
    sd.LOADERS["direnv"].wire(tmp_path)
    assert (tmp_path / ".envrc").read_text() == first


def test_direnv_loader_wire_upgrades_legacy_dotenv_block(tmp_path):
    (tmp_path / ".envrc").write_text(
        "# >>> splashdown-managed dotenv >>>\n"
        "dotenv splashdown.env\n"
        "# <<< splashdown-managed dotenv <<<\n"
    )
    sd.LOADERS["direnv"].wire(tmp_path)
    text = (tmp_path / ".envrc").read_text()
    assert "dotenv_if_exists splashdown.env" in text
    assert "\ndotenv splashdown.env\n" not in text


def test_direnv_loader_wire_preserves_existing_envrc(tmp_path):
    (tmp_path / ".envrc").write_text("use nix\nlayout python\n")
    sd.LOADERS["direnv"].wire(tmp_path)
    text = (tmp_path / ".envrc").read_text()
    assert "use nix" in text
    assert "layout python" in text
    assert "dotenv_if_exists splashdown.env" in text


def test_devbox_loader_wire_adds_init_hook(tmp_path):
    (tmp_path / "devbox.json").write_text('{"packages": ["nodejs@22"]}')
    sd.LOADERS["devbox"].wire(tmp_path)
    data = json.loads((tmp_path / "devbox.json").read_text())
    hooks = data.get("shell", {}).get("init_hook", [])
    assert any("splashdown.env" in h for h in hooks)


def test_devbox_loader_wire_preserves_existing_packages(tmp_path):
    (tmp_path / "devbox.json").write_text('{"packages": ["nodejs@22", "pnpm@9"]}')
    sd.LOADERS["devbox"].wire(tmp_path)
    data = json.loads((tmp_path / "devbox.json").read_text())
    assert data["packages"] == ["nodejs@22", "pnpm@9"]


def test_devbox_loader_wire_idempotent(tmp_path):
    (tmp_path / "devbox.json").write_text("{}")
    sd.LOADERS["devbox"].wire(tmp_path)
    first = (tmp_path / "devbox.json").read_text()
    sd.LOADERS["devbox"].wire(tmp_path)
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
    checks = sd.PROFILES["react-native"].wiring_checks(app)
    ids = {c.id for c in checks}
    assert "hook" in ids
    assert "rn-metro-config" in ids
    assert "rn-pkg-port" in ids
    assert "rn-xcode-env" in ids


def test_mise_loader_unwire_deletes_solely_managed_file(tmp_path):
    sd.LOADERS["mise"].wire(tmp_path)
    assert (tmp_path / "mise.toml").exists()
    sd.LOADERS["mise"].unwire(tmp_path)
    assert not (tmp_path / "mise.toml").exists()


def test_mise_loader_unwire_keeps_user_content(tmp_path):
    (tmp_path / "mise.toml").write_text('[env]\nFOO = "bar"\n\n[tools]\nnode = "20"\n')
    sd.LOADERS["mise"].wire(tmp_path)
    sd.LOADERS["mise"].unwire(tmp_path)
    text = (tmp_path / "mise.toml").read_text()
    assert (tmp_path / "mise.toml").exists()
    assert '_.file = "splashdown.env"' not in text
    assert 'FOO = "bar"' in text
    assert 'node = "20"' in text


def test_mise_loader_unwire_drops_empty_env_table(tmp_path):
    (tmp_path / "mise.toml").write_text('[tools]\nnode = "20"\n')
    sd.LOADERS["mise"].wire(tmp_path)
    sd.LOADERS["mise"].unwire(tmp_path)
    text = (tmp_path / "mise.toml").read_text()
    assert "[env]" not in text
    assert 'node = "20"' in text


def test_mise_loader_unwire_noop_when_absent(tmp_path):
    sd.LOADERS["mise"].unwire(tmp_path)
    assert not (tmp_path / "mise.toml").exists()


def test_direnv_loader_unwire_strips_block(tmp_path):
    (tmp_path / ".envrc").write_text("use nix\nlayout python\n")
    sd.LOADERS["direnv"].wire(tmp_path)
    sd.LOADERS["direnv"].unwire(tmp_path)
    text = (tmp_path / ".envrc").read_text()
    assert "use nix" in text
    assert "layout python" in text
    assert "splashdown.env" not in text
    assert "splashdown-managed" not in text


def test_direnv_loader_unwire_deletes_solely_managed_file(tmp_path):
    sd.LOADERS["direnv"].wire(tmp_path)
    sd.LOADERS["direnv"].unwire(tmp_path)
    assert not (tmp_path / ".envrc").exists()


def test_devbox_loader_unwire_removes_hook(tmp_path):
    (tmp_path / "devbox.json").write_text('{"packages": ["nodejs@22"]}')
    sd.LOADERS["devbox"].wire(tmp_path)
    sd.LOADERS["devbox"].unwire(tmp_path)
    data = json.loads((tmp_path / "devbox.json").read_text())
    hooks = data.get("shell", {}).get("init_hook", [])
    assert not any("splashdown.env" in h for h in hooks)
    assert data["packages"] == ["nodejs@22"]


def test_devbox_loader_unwire_deletes_solely_managed_file(tmp_path):
    (tmp_path / "devbox.json").write_text("{}")
    sd.LOADERS["devbox"].wire(tmp_path)
    sd.LOADERS["devbox"].unwire(tmp_path)
    assert not (tmp_path / "devbox.json").exists()


def test_mise_loader_wire_returns_true_on_create_false_on_rerun(tmp_path):
    assert sd.LOADERS["mise"].wire(tmp_path) is True
    assert (tmp_path / "mise.toml").exists()
    assert sd.LOADERS["mise"].wire(tmp_path) is False


def test_mise_loader_wire_returns_false_when_editing_existing_file(tmp_path):
    (tmp_path / "mise.toml").write_text('[tools]\nnode = "20"\n')
    assert sd.LOADERS["mise"].wire(tmp_path) is False
    assert '_.file = "splashdown.env"' in (tmp_path / "mise.toml").read_text()


def test_direnv_loader_wire_returns_true_on_create_false_when_editing_existing(tmp_path):
    assert sd.LOADERS["direnv"].wire(tmp_path) is True
    (tmp_path / ".envrc").write_text("use nix\n")
    assert sd.LOADERS["direnv"].wire(tmp_path) is False


def test_direnv_loader_wire_manual_hint_only_when_editing_existing(tmp_path, capsys):
    sd.LOADERS["direnv"].wire(tmp_path)
    assert "direnv allow" not in capsys.readouterr().err
    (tmp_path / ".envrc").write_text("use nix\n")
    sd.LOADERS["direnv"].wire(tmp_path)
    assert "direnv allow" in capsys.readouterr().err


def test_devbox_and_none_loader_wire_return_false(tmp_path):
    (tmp_path / "devbox.json").write_text("{}")
    assert sd.LOADERS["devbox"].wire(tmp_path) is False
    assert sd.LOADERS["none"].wire(tmp_path) is False


def test_mise_loader_approve_invokes_mise_trust_with_config_path(tmp_path, monkeypatch):
    (tmp_path / "mise.toml").write_text("[env]\n")
    calls = _record_run_ok(monkeypatch, ok=True)
    assert sd.LOADERS["mise"].approve(tmp_path) is True
    assert calls == [(["mise", "trust", str(tmp_path / "mise.toml")], tmp_path)]


def test_mise_loader_approve_targets_dot_mise_toml_when_only_that_exists(tmp_path, monkeypatch):
    (tmp_path / ".mise.toml").write_text("[env]\n")
    calls = _record_run_ok(monkeypatch, ok=True)
    sd.LOADERS["mise"].approve(tmp_path)
    assert calls[0][0] == ["mise", "trust", str(tmp_path / ".mise.toml")]


def test_mise_loader_approve_noop_when_no_config(tmp_path, monkeypatch):
    calls = _record_run_ok(monkeypatch, ok=True)
    assert sd.LOADERS["mise"].approve(tmp_path) is False
    assert calls == []


def test_mise_loader_approve_returns_false_on_command_failure(tmp_path):
    (tmp_path / "mise.toml").write_text("[env]\n")
    # The autouse _stub_loader_approval makes _run_ok return False.
    assert sd.LOADERS["mise"].approve(tmp_path) is False


def test_direnv_loader_approve_invokes_direnv_allow_with_cwd(tmp_path, monkeypatch):
    (tmp_path / ".envrc").write_text("dotenv_if_exists splashdown.env\n")
    calls = _record_run_ok(monkeypatch, ok=True)
    assert sd.LOADERS["direnv"].approve(tmp_path) is True
    assert calls == [(["direnv", "allow", str(tmp_path)], tmp_path)]


def test_direnv_loader_approve_noop_when_no_envrc(tmp_path, monkeypatch):
    calls = _record_run_ok(monkeypatch, ok=True)
    assert sd.LOADERS["direnv"].approve(tmp_path) is False
    assert calls == []


def test_devbox_and_none_loader_approve_never_shell_out(tmp_path, monkeypatch):
    calls = _record_run_ok(monkeypatch, ok=True)
    assert sd.LOADERS["devbox"].approve(tmp_path) is False
    assert sd.LOADERS["none"].approve(tmp_path) is False
    assert calls == []


def test_mise_approve_announce_prints_success(tmp_path, monkeypatch, capsys):
    (tmp_path / "mise.toml").write_text("[env]\n")
    _record_run_ok(monkeypatch, ok=True)
    sd.LOADERS["mise"].approve(tmp_path, announce=True)
    assert "trusted mise.toml" in capsys.readouterr().err


def test_mise_approve_announce_prints_fallback_on_failure(tmp_path, monkeypatch, capsys):
    (tmp_path / "mise.toml").write_text("[env]\n")
    _record_run_ok(monkeypatch, ok=False)
    sd.LOADERS["mise"].approve(tmp_path, announce=True)
    assert "run `mise trust`" in capsys.readouterr().err


def test_approve_silent_when_not_announced(tmp_path, monkeypatch, capsys):
    (tmp_path / "mise.toml").write_text("[env]\n")
    _record_run_ok(monkeypatch, ok=True)
    sd.LOADERS["mise"].approve(tmp_path)
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
