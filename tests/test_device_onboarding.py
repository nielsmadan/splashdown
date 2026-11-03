from __future__ import annotations

import json
import os
import pty
import select
import signal
import subprocess
import sys
import termios
import time

import pytest

from splashdown import cli, device_onboarding
from splashdown.device_types import DiscoverySnapshot, PhysicalObservation


def phone(**overrides):
    return PhysicalObservation(
        **{
            "platform": "android",
            "name": "Pixel 9",
            "hardware_id": "PIXEL123",
            "transport_id": "192.0.2.4:5555",
            "state": "device",
            "detail": "",
            "selectable": True,
            **overrides,
        }
    )


def interactive(monkeypatch, choices, names=()):
    choices = iter(choices)
    names = iter(names)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(device_onboarding, "choose", lambda *_args: next(choices))
    monkeypatch.setattr(device_onboarding, "read_name", lambda _prompt: next(names))


def snapshot(monkeypatch, devices=(), issues=()):
    monkeypatch.setattr(
        device_onboarding,
        "discover_for_onboarding",
        lambda _p: DiscoverySnapshot(tuple(devices), tuple(issues)),
    )


def isolated_state(monkeypatch, tmp_path):
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    return state


def test_guided_add_saves_hardware_identity_without_claiming(tmp_path, monkeypatch, capsys):
    (tmp_path / "splashdown.toml").write_text("")
    state = isolated_state(monkeypatch, tmp_path)
    snapshot(monkeypatch, [phone()])
    interactive(monkeypatch, [0, 0], [""])
    assert cli.main(["--cwd", str(tmp_path), "target", "add", "device"]) == 0
    from splashdown.recipe import LocalConfig

    assert LocalConfig.load(tmp_path / "splashdown.local.toml").targets == {
        "device": {"pixel-9": {"id": "PIXEL123", "platform": "android"}}
    }
    assert (state / "splashdown" / "claims.tsv").read_text() == ""
    output = capsys.readouterr()
    assert output.out == ""
    assert "splash run device pixel-9" in output.err


@pytest.mark.parametrize("choices,names", [([None], []), ([0, None], ["phone"])])
def test_cancel_keeps_files_and_registry_untouched(tmp_path, monkeypatch, choices, names):
    (tmp_path / "splashdown.toml").write_text("")
    state = isolated_state(monkeypatch, tmp_path)
    snapshot(monkeypatch, [phone()])
    interactive(monkeypatch, choices, names)
    assert cli.main(["--cwd", str(tmp_path), "target", "add", "device"]) == 0
    assert sorted(path.name for path in tmp_path.iterdir()) == ["splashdown.toml"]
    assert not state.exists()


def test_eof_at_name_cancels(tmp_path, monkeypatch):
    snapshot(monkeypatch, [phone()])
    interactive(monkeypatch, [0])

    monkeypatch.setattr(device_onboarding, "read_name", lambda _prompt: None)
    assert device_onboarding.onboard_device(None, tmp_path / "local.toml", set()) is None


def test_blocked_device_setup_rescan_and_duplicate_name(tmp_path, monkeypatch, capsys):
    scans = iter(
        [
            DiscoverySnapshot(
                (
                    phone(
                        hardware_id=None,
                        state="unauthorized",
                        selectable=False,
                        detail="Accept debugging authorization",
                    ),
                ),
                ("iOS: Xcode missing",),
            ),
            DiscoverySnapshot((phone(),), ()),
        ]
    )
    monkeypatch.setattr(device_onboarding, "discover_for_onboarding", lambda _p: next(scans))
    interactive(monkeypatch, [0, 0, 0, 0], ["used", "my-phone"])
    declaration = device_onboarding.onboard_device("android", tmp_path / "local.toml", {"used"})
    assert declaration == device_onboarding.DeviceDeclaration("my-phone", "android", "PIXEL123")
    output = capsys.readouterr().err
    assert "unauthorized" in output
    assert "Xcode missing" in output
    assert "USB debugging" in output
    assert "already exists" in output


def test_add_global_ignores_unrelated_invalid_project_and_is_sdk_free(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / "splashdown.toml").write_text("invalid TOML = [")
    state = isolated_state(monkeypatch, tmp_path)

    def unexpected(*_a, **_k):
        pytest.fail("direct authoring must not discover hardware")

    monkeypatch.setattr(device_onboarding, "discover_for_onboarding", unexpected)
    assert (
        cli.main(
            [
                "--cwd",
                str(tmp_path),
                "target",
                "add",
                "device",
                "phone",
                "--global",
                "--platform",
                "android",
                "--id",
                "PIXEL123",
                "--format",
                "json",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["data"]["fields"] == {"id": "PIXEL123", "platform": "android"}
    assert result["data"]["saved"] is True
    assert not state.exists()


@pytest.mark.parametrize("recipe", [None, "not toml ["])
def test_invalid_local_destination_fails_before_discovery(tmp_path, monkeypatch, capsys, recipe):
    if recipe is not None:
        (tmp_path / "splashdown.toml").write_text(recipe)
    interactive(monkeypatch, [])

    def unexpected(*_a):
        pytest.fail("destination must be validated first")

    monkeypatch.setattr(device_onboarding, "discover_for_onboarding", unexpected)
    assert cli.main(["--cwd", str(tmp_path), "target", "add", "device"]) == 1
    assert "error:" in capsys.readouterr().err


def test_revalidates_destination_after_prompt(tmp_path, monkeypatch, capsys):
    (tmp_path / "splashdown.toml").write_text("")
    isolated_state(monkeypatch, tmp_path)
    interactive(monkeypatch, [])
    existing = '[targets.device.phone]\nid = "OTHER"\n'

    def concurrent_add(*_args):
        (tmp_path / "splashdown.local.toml").write_text(existing)
        return device_onboarding.DeviceDeclaration("phone", "android", "PIXEL123")

    monkeypatch.setattr(device_onboarding, "onboard_device", concurrent_add)
    assert cli.main(["--cwd", str(tmp_path), "target", "add", "device"]) == 1
    assert (tmp_path / "splashdown.local.toml").read_text() == existing
    assert "already exists" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [
        ["device", "--format", "json"],
        ["device", "--id", "PHONE", "--format", "json"],
        ["device", "phone", "--format", "json"],
        ["device", "phone", "--name", "Phone", "--format", "json"],
        ["simulator", "--format", "json"],
    ],
)
def test_incomplete_arguments_are_json_usage_errors(args, capsys):
    assert cli.main(["target", "add", *args]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["exit_code"] == 2
    assert result["error"]["code"] == "invalid_arguments"


@pytest.mark.parametrize("stream", ["stdin", "stderr"])
def test_redirected_stream_refuses_guide(stream, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(getattr(sys, stream), "isatty", lambda: False)
    with pytest.raises(SystemExit) as error:
        cli.main(["target", "add", "device"])
    assert error.value.code == 2
    assert "--id HARDWARE_ID" in capsys.readouterr().err


def _read_until(fd, expected, process):
    output = b""
    deadline = time.monotonic() + 5
    while expected not in output:
        if time.monotonic() >= deadline:
            pytest.fail(f"PTY timed out waiting for {expected!r}: {output!r}; rc={process.poll()}")
        if select.select([fd], [], [], 0.1)[0]:
            output += os.read(fd, 65536)
    return output


@pytest.mark.parametrize(
    "action,expected",
    [
        (b"w", 1),
        (b"\x1b[B\r", 1),
        (b"\x1bOB\r", 1),
        (b"\x1bOA\r", 10),
        (b"\x1b[1;2A\r", 0),
        (b"\x1b[3~\r", 0),
        (b"\x1b", None),
        (b"\x04", None),
        (None, "interrupt"),
    ],
)
def test_picker_real_pty_restores_terminal(action, expected):
    master, slave = pty.openpty()
    before = termios.tcgetattr(slave)
    options = ["First", "Second", *(f"Phone {number}" for number in range(9))]
    script = (
        "from splashdown.picker import choose\nimport json\ntry:\n"
        f" r=choose('Phones', {options!r})\n"
        "except KeyboardInterrupt:\n r='interrupt'\nprint(json.dumps(r))"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script], stdin=slave, stderr=slave, stdout=subprocess.PIPE
    )
    try:
        output = _read_until(master, b"Esc cancels", process)
        assert b"[q] First" in output and b"[w] Second" in output
        if action is None:
            process.send_signal(signal.SIGINT)
        else:
            os.write(master, action)
        stdout, _ = process.communicate(timeout=5)
        assert process.returncode == 0
        assert json.loads(stdout) == expected
        restored = termios.tcgetattr(slave)
        restored[3] &= ~getattr(termios, "PENDIN", 0)
        assert restored == before
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        os.close(master)
        os.close(slave)


def test_untrusted_device_text_is_escaped():
    from splashdown.picker import terminal_text

    assert terminal_text("Phone\x1b[2J\nOther") == "Phone\\x1b[2J\\nOther"


@pytest.mark.parametrize(
    "action,expected",
    [
        (b"SpareX\x7fPhone\r", "SparePhone"),
        (b"Phone\x1b[3~\r", "Phone"),
        (b"Phone\x1b[1;2A\r", "Phone"),
        (b"Phone\x1bOA\r", "Phone"),
        (b"Phone\x1b", None),
        (b"Phone\x1b[\x04", None),
        (b"Phone\x04", None),
        (None, "interrupt"),
    ],
)
def test_name_input_real_pty_cancel_edit_and_restore(action, expected):
    master, slave = pty.openpty()
    before = termios.tcgetattr(slave)
    script = "from splashdown.picker import read_name\nimport json\ntry:\n r=read_name('Name: ')\nexcept KeyboardInterrupt:\n r='interrupt'\nprint(json.dumps(r))"
    process = subprocess.Popen(
        [sys.executable, "-c", script], stdin=slave, stderr=slave, stdout=subprocess.PIPE
    )
    try:
        _read_until(master, b"Name: ", process)
        if action is None:
            process.send_signal(signal.SIGINT)
        else:
            os.write(master, action)
        stdout, _ = process.communicate(timeout=5)
        assert process.returncode == 0
        assert json.loads(stdout) == expected
        restored = termios.tcgetattr(slave)
        restored[3] &= ~getattr(termios, "PENDIN", 0)
        assert restored == before
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        os.close(master)
        os.close(slave)
