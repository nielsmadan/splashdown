from __future__ import annotations

import subprocess

import pytest

from splashdown import port_inspection
from splashdown.port_inspection import PortOwner


def test_listener_inspection_groups_multiple_owners_and_deduplicates_sockets(monkeypatch):
    calls = []

    def query(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            "p123\ncnode\nf20\nn127.0.0.1:8082\nf21\nn[::1]:8082\n"
            "p456\ncother worker\nn*:8082\nn*:3000\n",
            "",
        )

    monkeypatch.setattr(port_inspection.subprocess, "run", query)

    assert port_inspection.listening_processes() == {
        8082: (PortOwner(123, "node"), PortOwner(456, "other worker")),
        3000: (PortOwner(456, "other worker"),),
    }
    argv, kwargs = calls[0]
    assert argv == ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpcn"]
    assert kwargs["timeout"] == 3


@pytest.mark.parametrize("error", [FileNotFoundError("lsof"), subprocess.TimeoutExpired("lsof", 3)])
def test_listener_inspection_unavailable_is_unknown(monkeypatch, error):
    def query(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(port_inspection.subprocess, "run", query)

    assert port_inspection.listening_processes() is None


@pytest.mark.parametrize(("returncode", "stderr"), [(1, ""), (0, "partial result")])
def test_listener_inspection_rejects_failed_or_incomplete_snapshots(
    monkeypatch, returncode, stderr
):
    monkeypatch.setattr(
        port_inspection.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, returncode, "p123\ncnode\nn*:8082\n", stderr
        ),
    )

    assert port_inspection.listening_processes() is None


def test_listener_inspection_never_reuses_previous_process_identity(monkeypatch):
    monkeypatch.setattr(
        port_inspection.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 0, "p123\ncnode\nn*:8082\np456\nn*:3000\npbad\ncother\nn*:9000\n", ""
        ),
    )

    assert port_inspection.listening_processes() == {8082: (PortOwner(123, "node"),)}
