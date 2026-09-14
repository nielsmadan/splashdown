"""Shared fixtures, helpers, and constants for the splashdown test suite."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import splashdown as sd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

_IPHONE = {"id": "00008-PHONE", "name": "Niels's iPhone", "platform": "ios"}
_PIXEL = {"id": "PXL1234", "name": "Pixel_7", "platform": "android"}


@pytest.fixture(autouse=True)
def _isolate_global_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point XDG_CONFIG_HOME at an empty per-test dir so `load_settings` never reads
    the real `~/.config/splashdown/config.toml`. Tests that want a global config write
    one under this dir (or re-monkeypatch XDG_CONFIG_HOME themselves)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


@pytest.fixture(autouse=True)
def _isolate_git_config(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_configured_hooks_path` deliberately reads `core.hooksPath` from every config
    level, and `git check-ignore` honours `core.excludesFile`, so a developer's global
    settings would otherwise decide hook detection and ignore coverage for the whole
    suite. Point Git at a per-test global that excludes nothing and at no system
    config, and drop the `GIT_CONFIG_COUNT` pairs, which outrank both files. The file
    lives outside `tmp_path` because tests assert on that directory's contents."""
    for name in [name for name in os.environ if name.startswith("GIT_CONFIG")]:
        monkeypatch.delenv(name, raising=False)
    global_config = tmp_path_factory.mktemp("gitconfig") / "config"
    global_config.write_text(f"[core]\n\texcludesFile = {os.devnull}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)


@pytest.fixture(autouse=True)
def _stub_loader_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let loader `approve` shell out to a real `mise`/`direnv` on the dev
    machine's PATH (it would mutate the developer's global trust store against
    throwaway tmp paths). Stubs the `_run_ok` helper (not `subprocess.run`, which
    is the shared module object other code — e.g. git in the e2e tests — relies
    on) to a no-op returning False. Tests that assert approval argv re-monkeypatch
    `sd.loaders._run_ok` locally to record it."""
    monkeypatch.setattr(sd.loaders, "_run_ok", lambda *_a, **_k: False)


@pytest.fixture(autouse=True)
def _no_watchman_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from splashdown import runtime_checks

    monkeypatch.setattr(runtime_checks, "watchman_available", lambda: False)


@pytest.fixture
def registry(tmp_path: Path) -> sd.Registry:
    return sd.Registry(
        port_file=tmp_path / "ports.tsv",
        kv_file=tmp_path / "kv.tsv",
        device_file=tmp_path / "devices.tsv",
        claim_file=tmp_path / "claims.tsv",
        claim_notice_file=tmp_path / "claim-notices.tsv",
    )


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    d = tmp_path / "co"
    d.mkdir()
    return d


def _write_recipe(checkout: Path, body: str) -> None:
    (checkout / sd.RECIPE_NAME).write_text(body)


def _stub_physical(monkeypatch, ios=None, android=None):
    monkeypatch.setattr(sd.devices, "_ios_physical_devices", lambda **_kwargs: list(ios or []))
    monkeypatch.setattr(
        sd.devices, "_android_physical_devices", lambda **_kwargs: list(android or [])
    )


def _write_physical_recipe(tmp_path):
    (tmp_path / "splashdown.toml").write_text("""
[targets.device.default]

[project]
framework = "flutter"
""")
    (tmp_path / "pubspec.yaml").write_text("name: demo\n")


def _stub_ios_boot_chain(monkeypatch):
    monkeypatch.setattr(sd.devices, "_ios_latest_runtime_version", lambda: "18.5")
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.devices, "_ios_current_state", lambda u: "Shutdown")
    monkeypatch.setattr(sd.target_commands, "_ios_current_state", lambda u: "Shutdown")
    monkeypatch.setattr(sd.devices, "ios_ensure", lambda n, m, i: ("UDID-NEW", "Shutdown"))
    monkeypatch.setattr(sd.devices, "ios_boot", lambda u, s: None)
    monkeypatch.setattr(sd.target_commands, "ios_boot", lambda u, s: None)


def _git_init(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def _make_ios(tmp_path: Path, xcode_env_content: str) -> None:
    (tmp_path / "ios").mkdir()
    (tmp_path / "ios" / ".xcode.env").write_text(xcode_env_content)


def _capture_profile_calls(monkeypatch):
    calls: list = []
    monkeypatch.setattr(sd.runners.subprocess, "call", lambda args, **k: calls.append(args) or 0)
    monkeypatch.setattr(sd.runners, "call_finite", lambda args, **k: calls.append(args) or 0)
    return calls


def _stub_ios_devices(monkeypatch, devices):
    monkeypatch.setattr(sd.device_ios, "_xcrun_json", lambda args: {"devices": devices})
