"""Tests for splashdown loaders behavior."""

from __future__ import annotations

import json

import splashdown as sd


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
    assert "rn-hook" in ids
    assert "rn-metro-config" in ids
    assert "rn-pkg-port" in ids
    assert "rn-xcode-env" in ids
