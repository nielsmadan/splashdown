"""Shared fixtures, helpers, and constants for the splashdown test suite."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import splashdown as sd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

_IPHONE = {"id": "00008-PHONE", "name": "Niels's iPhone", "platform": "ios"}
_PIXEL = {"id": "PXL1234", "name": "Pixel_7", "platform": "android"}


@pytest.fixture
def registry(tmp_path: Path) -> sd.Registry:
    return sd.Registry(
        port_file=tmp_path / "ports.tsv",
        kv_file=tmp_path / "kv.tsv",
        device_file=tmp_path / "devices.tsv",
    )


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    d = tmp_path / "co"
    d.mkdir()
    return d


def _write_recipe(checkout: Path, body: str) -> None:
    (checkout / sd.RECIPE_NAME).write_text(body)


def _stub_physical(monkeypatch, ios=None, android=None):
    monkeypatch.setattr(sd.devices, "_ios_physical_devices", lambda: list(ios or []))
    monkeypatch.setattr(sd.devices, "_android_physical_devices", lambda: list(android or []))


def _write_physical_recipe(tmp_path):
    (tmp_path / "splashdown.toml").write_text("""
[targets.device.default]

[project]
framework = "flutter"
""")
    (tmp_path / "pubspec.yaml").write_text("name: demo\n")


def _stub_ios_boot_chain(monkeypatch):
    monkeypatch.setattr(sd.devices, "_ios_latest_runtime_version", lambda: "18.5")
    monkeypatch.setattr(sd.commands, "_ios_latest_runtime_version", lambda: "18.5")
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.commands, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.devices, "_ios_current_state", lambda u: "Shutdown")
    monkeypatch.setattr(sd.commands, "_ios_current_state", lambda u: "Shutdown")
    monkeypatch.setattr(sd.devices, "ios_ensure", lambda n, m, i: ("UDID-NEW", "Shutdown"))
    monkeypatch.setattr(sd.devices, "ios_boot", lambda u, s: None)
    monkeypatch.setattr(sd.commands, "ios_boot", lambda u, s: None)


def _git_init(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def _make_ios(tmp_path: Path, xcode_env_content: str) -> None:
    (tmp_path / "ios").mkdir()
    (tmp_path / "ios" / ".xcode.env").write_text(xcode_env_content)


def _inv_none(tmp_path, *profiles):
    apps = [
        sd.AppInventory(name=f"app{i}", path=tmp_path, profile=p) for i, p in enumerate(profiles)
    ]
    return sd.ProjectInventory(workspace="single", apps=apps, loader="none")


def _capture_profile_calls(monkeypatch):
    calls: list = []
    monkeypatch.setattr(sd.profiles.subprocess, "call", lambda args, **k: calls.append(args) or 0)
    return calls


def _stub_ios_devices(monkeypatch, devices):
    monkeypatch.setattr(sd.devices, "_xcrun_json", lambda args: {"devices": devices})
