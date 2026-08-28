from __future__ import annotations

import json
from dataclasses import asdict

import pytest

import splashdown as sd
from conftest import _git_init
from splashdown import bootstrap


def _write_bootstrap_recipe(checkout):
    (checkout / sd.RECIPE_NAME).write_text('[bootstrap]\nrun = "true"\n')


def _automation_payload(checkout, registry):
    report = sd.status.build_status_report(checkout, registry)
    automation = report.checkouts[0].automation
    assert automation is not None
    return asdict(automation)


def test_detailed_status_reports_retained_trust_without_a_bootstrap_recipe(tmp_path, registry):
    _git_init(tmp_path)
    bootstrap.record_trust(bootstrap.git_dirs(tmp_path))

    assert _automation_payload(tmp_path, registry) == {
        "sync_trusted": True,
        "bootstrap_trusted": True,
        "bootstrap_declared": False,
        "bootstrap_completion": "not-declared",
    }


def test_detailed_status_distinguishes_bootstrap_completion_states(tmp_path, registry):
    _git_init(tmp_path)
    _write_bootstrap_recipe(tmp_path)
    dirs = bootstrap.git_dirs(tmp_path)

    assert _automation_payload(tmp_path, registry) == {
        "sync_trusted": False,
        "bootstrap_trusted": False,
        "bootstrap_declared": True,
        "bootstrap_completion": "pending",
    }

    bootstrap.mark_bootstrap_complete(dirs)
    assert _automation_payload(tmp_path, registry)["bootstrap_completion"] == "complete"

    completion = dirs.private / "splashdown" / "bootstrap-v1.json"
    completion.write_text("not json\n")
    assert _automation_payload(tmp_path, registry)["bootstrap_completion"] == "invalid"


def test_detailed_status_uses_null_automation_for_non_git_checkout(tmp_path, registry):
    report = sd.status.build_status_report(tmp_path, registry)

    assert report.checkouts[0].automation is None


def test_detailed_status_does_not_probe_git_for_defunct_checkout(tmp_path, registry, monkeypatch):
    checkout = tmp_path / "gone"
    registry.set_kv(str(checkout), "KEY", "value")

    def unexpected_probe(_checkout):
        pytest.fail("defunct checkout triggered a Git probe")

    monkeypatch.setattr(bootstrap, "git_dirs", unexpected_probe)
    report = sd.status.build_status_report(tmp_path, registry, show_all=True, detailed=True)

    assert report.checkouts[0].automation is None


@pytest.mark.parametrize(("show_all", "verbose"), [(False, False), (True, True)])
def test_detailed_status_text_renders_automation(tmp_path, registry, capsys, show_all, verbose):
    _git_init(tmp_path)
    _write_bootstrap_recipe(tmp_path)
    bootstrap.record_trust(bootstrap.git_dirs(tmp_path), bootstrap=False)

    assert (
        sd.cmd_status(
            tmp_path,
            registry,
            "text",
            show_all=show_all,
            verbose=verbose,
        )
        == 0
    )
    err = capsys.readouterr().err
    for line in (
        "automation:",
        "sync trust: trusted",
        "bootstrap trust: untrusted",
        "recipe bootstrap: declared",
        "completion: pending",
    ):
        assert line in err


@pytest.mark.parametrize("show_all", [False, True])
def test_detailed_status_json_includes_automation(tmp_path, registry, capsys, show_all):
    _git_init(tmp_path)
    _write_bootstrap_recipe(tmp_path)
    dirs = bootstrap.git_dirs(tmp_path)
    bootstrap.record_trust(dirs)
    bootstrap.mark_bootstrap_complete(dirs)

    assert sd.cmd_status(tmp_path, registry, "json", show_all=show_all) == 0
    payload = json.loads(capsys.readouterr().out)
    checkout = payload["checkouts"][0] if show_all else payload
    assert checkout["automation"] == {
        "sync_trusted": True,
        "bootstrap_trusted": True,
        "bootstrap_declared": True,
        "bootstrap_completion": "complete",
    }


def test_detailed_status_json_uses_null_automation_for_non_git_checkout(tmp_path, registry, capsys):
    assert sd.cmd_status(tmp_path, registry, "json") == 0

    assert json.loads(capsys.readouterr().out)["automation"] is None


def test_detailed_status_all_isolates_malformed_checkout_recipe(tmp_path, registry, capsys):
    good = tmp_path / "good"
    broken = tmp_path / "broken"
    good.mkdir()
    broken.mkdir()
    _git_init(good)
    _git_init(broken)
    _write_bootstrap_recipe(good)
    (broken / sd.RECIPE_NAME).write_text("[bootstrap\n")
    registry.set_kv(str(good.resolve()), "GOOD", "value")
    registry.set_kv(str(broken.resolve()), "BROKEN", "value")

    assert sd.cmd_status(good, registry, "json", show_all=True) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    by_checkout = {item["checkout"]: item for item in payload["checkouts"]}
    assert by_checkout[str(good.resolve())]["automation"]["bootstrap_declared"] is True
    assert by_checkout[str(broken.resolve())]["automation"] == {
        "sync_trusted": False,
        "bootstrap_trusted": False,
        "bootstrap_declared": None,
        "bootstrap_completion": "unavailable",
    }
    assert str(broken.resolve()) in captured.err
    assert "invalid TOML" in captured.err


def test_status_all_show_values_uses_detailed_blocks(tmp_path, registry, capsys):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    registry.set_kv(str(checkout.resolve()), "TOKEN", "top-secret")

    assert sd.cmd_status(checkout, registry, "text", show_all=True, show_values=True) == 0

    err = capsys.readouterr().err
    assert f"=== {checkout.resolve()} ===" in err
    assert "TOKEN=top-secret" in err


def test_compact_status_all_does_not_probe_git(tmp_path, registry, monkeypatch, capsys):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    registry.set_kv(str(checkout), "KEY", "value")

    def unexpected_probe(_checkout):
        pytest.fail("compact status all triggered a Git probe")

    monkeypatch.setattr(bootstrap, "git_dirs", unexpected_probe)

    assert sd.cmd_status(checkout, registry, "text", show_all=True) == 0
    assert "1 var" in capsys.readouterr().err
