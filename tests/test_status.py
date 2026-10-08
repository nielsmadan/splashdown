from __future__ import annotations

import json
import subprocess

import pytest

import splashdown as sd
from conftest import _git_init
from splashdown import bootstrap, status, status_targets
from splashdown.device_types import EmulatorRecord, SimulatorRecord
from splashdown.port_inspection import PortOwner
from splashdown.registry import RegistrySnapshot
from splashdown.results import Diagnostic


def _row(result):
    return result.data["checkouts"][0]


def _checks(row):
    return {check["id"]: check for check in row["checks"]}


def _recipe(path, text=""):
    (path / sd.RECIPE_NAME).write_text(text)


def _snapshot(path, **kwargs):
    return RegistrySnapshot(values=((str(path), "TOKEN", "top-secret-value"),), **kwargs)


def test_empty_fleet_has_no_cwd_fallback(tmp_path):
    result = status.build_status_report(tmp_path, RegistrySnapshot(), show_all=True)
    assert result.exit_code == 0
    assert result.data == {"checkouts": []}


def test_unallocated_valid_checkout_and_uninitialized_directory(tmp_path):
    result = status.build_status_report(tmp_path, RegistrySnapshot())
    assert result.exit_code == 1
    assert _checks(_row(result))["recipe"]["next_steps"] == ["Run splash init in this checkout."]
    _recipe(tmp_path)
    result = status.build_status_report(tmp_path, RegistrySnapshot())
    assert result.exit_code == 0
    assert _row(result)["complete"] is True
    assert _row(result)["resources"] == []


@pytest.mark.parametrize(
    ("spec", "step"),
    [
        ('type="set"', "Run splash env set TOKEN=VALUE."),
        ('type="set"\ndefault="fallback"', "Run splash sync."),
        ('type="set"\ndefault=""', "Run splash sync."),
        ('type="uuid"', "Run splash sync."),
    ],
)
@pytest.mark.parametrize("output_format", ["text", "json"])
def test_unassigned_resource_recovery(tmp_path, capsys, spec, step, output_format):
    _recipe(tmp_path, f"[resources.TOKEN]\n{spec}\n")
    assert sd.cmd_status(tmp_path, RegistrySnapshot(), output_format) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    if output_format == "json":
        payload = json.loads(captured.out)
        row = payload["data"]["checkouts"][0]
        assert _checks(row)["resource:TOKEN"] == {
            "id": "resource:TOKEN",
            "state": "finding",
            "message": "TOKEN: unassigned",
            "next_steps": [step],
        }
    else:
        assert step in captured.out


@pytest.mark.parametrize("invalid", ["[bad", '[resources.X]\ntype="unknown-secret"'])
def test_invalid_recipe_preserves_assignments_without_values(tmp_path, invalid):
    _recipe(tmp_path, invalid)
    result = status.build_status_report(tmp_path, _snapshot(tmp_path))
    assert result.exit_code == 1
    assert _row(result)["resources"] == [{"key": "TOKEN", "state": "assigned", "port_state": ""}]
    encoded = json.dumps(sd.results.result_payload(result))
    assert "top-secret-value" not in encoded
    assert "unknown-secret" not in encoded


def test_mixed_fleet_keeps_sorted_deleted_and_invalid_rows(tmp_path):
    good, bad, deleted = (tmp_path / name for name in ("a", "b", "c"))
    good.mkdir()
    bad.mkdir()
    _recipe(good)
    _recipe(bad, "[")
    snapshot = RegistrySnapshot(
        values=tuple((str(path), "KEY", "value") for path in (deleted, bad, good))
    )
    result = status.build_status_report(tmp_path, snapshot, show_all=True)
    assert result.exit_code == 1
    rows = result.data["checkouts"]
    assert [row["checkout"] for row in rows] == [str(good), str(bad), str(deleted)]
    assert [row["complete"] for row in rows] == [True, False, True]
    assert _checks(rows[2])["checkout"]["state"] == "finding"


def test_partial_registry_diagnostics_retain_available_rows(tmp_path):
    _recipe(tmp_path)
    error = Diagnostic("registry_read_error", "kv.tsv row 2 is invalid")
    result = status.build_status_report(tmp_path, _snapshot(tmp_path), diagnostics=(error,))
    assert result.status == "partial"
    assert result.warnings == (error,)
    assert _row(result)["resources"][0]["key"] == "TOKEN"


@pytest.mark.parametrize("verbose", [False, True])
def test_text_retains_every_finding_and_unavailable_check(tmp_path, capsys, verbose):
    _recipe(tmp_path, '[project]\nloader="mise"\n[resources.TOKEN]\ntype="set"\n')
    assert sd.cmd_status(tmp_path, _snapshot(tmp_path), "text", verbose=verbose) == 0
    text = capsys.readouterr().out
    for phrase in (
        "Generated output is missing",
        "Run splash sync",
        "needs configuration",
        "approval is not observed",
        "Check authorization with mise",
    ):
        assert phrase in text
    assert "top-secret-value" not in text


def test_json_is_identical_with_verbose_and_one_envelope(tmp_path, capsys):
    _recipe(tmp_path)
    payloads = []
    for verbose in (False, True):
        assert sd.cmd_status(tmp_path, _snapshot(tmp_path), "json", verbose=verbose) == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        payloads.append(json.loads(captured.out))
    assert payloads[0] == payloads[1]
    assert set(payloads[0]) == {
        "command",
        "status",
        "exit_code",
        "data",
        "error",
        "warnings",
        "next_steps",
    }


def test_output_comparison_preserves_coowned_bytes(tmp_path):
    _recipe(tmp_path, '[resources.TOKEN]\ntype="set"\n')
    path = tmp_path / "splashdown.env"
    path.write_text("USER='unchanged'\nTOKEN=top-secret-value\n\n")
    original = path.read_bytes()
    result = status.build_status_report(tmp_path, _snapshot(tmp_path))
    assert _checks(_row(result))["output:splashdown.env"]["state"] == "ok"
    assert path.read_bytes() == original
    path.write_text("USER='unchanged'\nTOKEN=old\n")
    result = status.build_status_report(tmp_path, _snapshot(tmp_path))
    assert _checks(_row(result))["output:splashdown.env"]["state"] == "finding"
    assert result.exit_code == 0


@pytest.mark.parametrize("text", ["TOKEN='unterminated", "TOKEN=a\nTOKEN=b\n"])
def test_invalid_output_is_incomplete(tmp_path, text):
    _recipe(tmp_path, '[resources.TOKEN]\ntype="set"\n')
    (tmp_path / "splashdown.env").write_text(text)
    result = status.build_status_report(tmp_path, _snapshot(tmp_path))
    assert result.exit_code == 1
    assert _checks(_row(result))["output:splashdown.env"]["state"] == "error"


def test_nonfile_writers_do_not_require_outputs(tmp_path):
    _recipe(tmp_path, '[resources.TOKEN]\ntype="set"\nwriter="none"\n')
    result = status.build_status_report(tmp_path, _snapshot(tmp_path))
    assert result.exit_code == 0
    assert _checks(_row(result))["output:splashdown.env"]["state"] == "not_applicable"


@pytest.mark.parametrize("name", [sd.LOCAL_NAME, "global.toml"])
def test_required_target_config_read_failure_is_not_empty(tmp_path, monkeypatch, name):
    _recipe(tmp_path)
    if name == "global.toml":
        monkeypatch.setattr(status, "_global_config_path", lambda: tmp_path / name)
    (tmp_path / name).write_text("[invalid")
    result = status.build_status_report(tmp_path, RegistrySnapshot())
    assert result.exit_code == 1
    assert _checks(_row(result))["target_config"]["state"] == "error"


def test_trust_reports_saved_authorization_without_completion(tmp_path):
    _git_init(tmp_path)
    _recipe(tmp_path, '[bootstrap]\nrun="true"\n')
    dirs = bootstrap.git_dirs(tmp_path)
    bootstrap.record_trust(dirs)
    (dirs.private / "splashdown" / "bootstrap-v1.json").write_text("invalid ignored marker")
    result = status.build_status_report(tmp_path, RegistrySnapshot())
    assert result.exit_code == 0
    assert _checks(_row(result))["trust"]["state"] == "ok"
    assert "completion" not in json.dumps(result.data)
    (dirs.common / "splashdown" / "trust-v1.json").write_text("invalid trust")
    result = status.build_status_report(tmp_path, RegistrySnapshot())
    assert result.exit_code == 1
    assert _checks(_row(result))["trust"]["state"] == "error"


@pytest.mark.parametrize(
    "contents,state,exit_code",
    [("invalid [", "error", 1), ('[env]\n_.file="other.env"\n', "finding", 0)],
)
def test_loader_syntax_error_vs_parseable_conflict(tmp_path, contents, state, exit_code):
    _recipe(tmp_path, '[project]\nloader="mise"\n')
    (tmp_path / "mise.toml").write_text(contents)
    result = status.build_status_report(tmp_path, RegistrySnapshot())
    assert result.exit_code == exit_code
    assert _checks(_row(result))["loader"]["state"] == state


def test_listener_inventory_reused_for_fleet(tmp_path, monkeypatch):
    snapshot = RegistrySnapshot()
    rows = []
    calls = []
    for index, name in enumerate(("a", "b")):
        checkout = tmp_path / name
        checkout.mkdir()
        _recipe(checkout, '[resources.P]\ntype="port"\nrange=[18001,18010]\n')
        rows.append((18001 + index, str(checkout), "P"))
    snapshot = RegistrySnapshot(ports=tuple(rows))
    monkeypatch.setattr(status, "_port_in_use", lambda port: True)
    monkeypatch.setattr(
        status,
        "listening_processes",
        lambda: calls.append(True) or {18001: (PortOwner(12, "node"),)},
    )
    result = status.build_status_report(tmp_path, snapshot, show_all=True)
    assert calls == [True]
    first, second = result.data["checkouts"]
    assert first["resources"][0]["owners"] == [{"pid": 12, "command": "node"}]
    assert second["resources"][0]["owners"] is None
    assert _checks(second)["resource:P:owners"]["state"] == "unavailable"


def test_free_port_is_normal_and_skips_listener_probe(tmp_path, monkeypatch):
    _recipe(tmp_path, '[resources.P]\ntype="port"\nrange=[18001,18010]\n')
    monkeypatch.setattr(status, "_port_in_use", lambda port: False)
    monkeypatch.setattr(status, "listening_processes", lambda: pytest.fail("unexpected probe"))
    result = status.build_status_report(
        tmp_path, RegistrySnapshot(ports=((18001, str(tmp_path), "P"),))
    )
    assert _row(result)["resources"][0] == {
        "key": "P",
        "state": "assigned",
        "port_state": "free",
        "owners": [],
    }


def _simulator_row(path, model="iPhone 17", runtime="18.5"):
    return SimulatorRecord(str(path), "default", "UDID", model, runtime, "2026-01-01")


@pytest.mark.parametrize(
    ("device", "spec", "inventory", "health"),
    [
        (
            True,
            'model="iPhone 17"\nios="18.5"',
            [{"udid": "UDID", "state": "Shutdown", "isAvailable": True}],
            "healthy",
        ),
        (
            True,
            'model="iPhone 18"\nios="18.5"',
            [{"udid": "UDID", "state": "Shutdown", "isAvailable": True}],
            "drifted",
        ),
        (
            True,
            'model="iPhone 17"\nios="19"',
            [{"udid": "UDID", "state": "Shutdown", "isAvailable": True}],
            "drifted",
        ),
        (True, None, [{"udid": "UDID", "state": "Shutdown", "isAvailable": True}], "undeclared"),
        (True, None, [], "orphan"),
        (False, 'model="iPhone 17"', [], "not_created"),
        (True, 'model="iPhone 17"', [{"udid": "UDID"}], "unavailable"),
    ],
)
def test_target_health_meanings(tmp_path, monkeypatch, device, spec, inventory, health):
    _recipe(tmp_path, "[targets.simulator.default]\n" + spec if spec is not None else "")
    monkeypatch.setattr(
        status_targets, "_xcrun_json", lambda args: {"devices": {"runtime": inventory}}
    )
    snapshot = RegistrySnapshot(devices=(_simulator_row(tmp_path),) if device else ())
    result = status.build_status_report(tmp_path, snapshot)
    target = _row(result)["targets"][0]
    assert target["health"] == health
    assert result.exit_code == 0
    if health in {"not_created", "healthy"}:
        assert _checks(_row(result))["target:simulator.default"]["state"] == "ok"


@pytest.mark.parametrize(
    "response", [{}, {"devices": []}, {"devices": {"x": [1]}}, {"devices": {"x": [{}]}}]
)
def test_malformed_simulator_probe_is_unavailable(tmp_path, monkeypatch, response):
    _recipe(tmp_path, '[targets.simulator.default]\nmodel="iPhone 17"\n')
    monkeypatch.setattr(status_targets, "_xcrun_json", lambda args: response)
    result = status.build_status_report(
        tmp_path, RegistrySnapshot(devices=(_simulator_row(tmp_path),))
    )
    assert _row(result)["targets"][0]["health"] == "unavailable"
    assert result.exit_code == 0


def test_registered_simulator_with_removed_runtime_is_unavailable(tmp_path, monkeypatch):
    _recipe(tmp_path, '[targets.simulator.default]\nmodel="iPhone 17"\nios="18.5"\n')
    monkeypatch.setattr(
        status_targets,
        "_xcrun_json",
        lambda args: {
            "devices": {"runtime": [{"udid": "UDID", "state": "Shutdown", "isAvailable": False}]}
        },
    )
    result = status.build_status_report(
        tmp_path, RegistrySnapshot(devices=(_simulator_row(tmp_path),))
    )
    row = _row(result)
    assert row["targets"][0]["status"] == "runtime unavailable"
    assert row["targets"][0]["health"] == "unavailable"
    assert _checks(row)["target:simulator.default"]["state"] == "unavailable"


def test_failed_discovery_cached_and_never_cleanup(tmp_path, monkeypatch):
    calls = []

    def fail(args):
        calls.append(args)
        raise sd.DeviceError("failed")

    monkeypatch.setattr(status_targets, "_xcrun_json", fail)
    observations = status_targets.TargetObservations()
    for _ in range(2):
        with pytest.raises(sd.DeviceError):
            observations.observe(tmp_path, "simulator", "default", {}, _simulator_row(tmp_path))
    assert len(calls) == 1


def test_physical_mixed_platform_keeps_known_connection(tmp_path, monkeypatch):
    _recipe(tmp_path, '[targets.device.phone]\nid="ANDROID"\n')
    monkeypatch.setattr(
        status_targets,
        "_physical_ios",
        lambda: (_ for _ in ()).throw(sd.CapabilityError("ios", "unsupported")),
    )
    monkeypatch.setattr(
        status_targets,
        "_physical_android",
        lambda: [{"id": "ANDROID", "platform": "android", "name": "Pixel"}],
    )
    result = status.build_status_report(tmp_path, RegistrySnapshot())
    row = _row(result)
    assert row["targets"][0]["status"] == "connected"
    assert _checks(row)["target_discovery:ios"]["state"] == "unavailable"
    assert result.exit_code == 0


@pytest.mark.parametrize("raw", [b"", b"wrong header\n", b"List of devices attached\nbad\n"])
def test_malformed_android_probe_is_not_absence(monkeypatch, raw):
    monkeypatch.setattr(status_targets, "_android_bin", lambda tool: tool)
    monkeypatch.setattr(status_targets, "check_output_finite", lambda *args, **kwargs: raw)
    with pytest.raises(sd.DeviceError):
        status_targets._physical_android()


def test_android_failed_image_probe_never_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(status_targets.TargetObservations, "avds", lambda self: {"avd"})
    monkeypatch.setattr(status_targets.TargetObservations, "running_emulators", lambda self: set())
    monkeypatch.setattr(status_targets, "_android_bin", lambda tool: tool)
    monkeypatch.setattr(
        status_targets,
        "check_output_finite",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, ["sdkmanager"])
        ),
    )
    row = EmulatorRecord(str(tmp_path), "default", "avd", "", "old", "")
    with pytest.raises(subprocess.CalledProcessError):
        status_targets.TargetObservations().observe(
            tmp_path, "emulator", "default", {"name": "avd"}, row
        )


@pytest.mark.parametrize("raw", [b"Unexpected diagnostic text\n", b"bad/name\n"])
def test_malformed_avd_inventory_never_infers_absence(tmp_path, monkeypatch, raw):
    _recipe(tmp_path, '[targets.emulator.default]\nname="avd"\nimage="image"\n')
    monkeypatch.setattr(status_targets, "_android_bin", lambda tool: tool)
    monkeypatch.setattr(status_targets, "check_output_finite", lambda *args, **kwargs: raw)
    row = EmulatorRecord(str(tmp_path), "default", "avd", "", "image", "")
    result = status.build_status_report(tmp_path, RegistrySnapshot(devices=(row,)))
    target = _row(result)["targets"][0]
    assert target["status"] == "unavailable"
    assert target["health"] == "unavailable"
    assert _checks(_row(result))["target:emulator.default"]["state"] == "unavailable"


def test_empty_avd_inventory_is_absence(tmp_path, monkeypatch):
    _recipe(tmp_path, '[targets.emulator.default]\nname="avd"\nimage="image"\n')
    monkeypatch.setattr(status_targets, "_android_bin", lambda tool: tool)
    monkeypatch.setattr(status_targets, "check_output_finite", lambda *args, **kwargs: b"")
    row = EmulatorRecord(str(tmp_path), "default", "avd", "", "image", "")
    result = status.build_status_report(tmp_path, RegistrySnapshot(devices=(row,)))
    assert _row(result)["targets"][0]["health"] == "orphan"


@pytest.mark.parametrize(
    "failure",
    [subprocess.CalledProcessError(1, ["avdmanager"]), subprocess.TimeoutExpired("avdmanager", 30)],
)
def test_failed_avd_inventory_is_cached_and_unavailable(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(status_targets, "_android_bin", lambda tool: tool)
    calls = []

    def failed(*args, **kwargs):
        calls.append(args)
        raise failure

    monkeypatch.setattr(status_targets, "check_output_finite", failed)
    observations = status_targets.TargetObservations()
    row = EmulatorRecord(str(tmp_path), "default", "avd", "", "image", "")
    for _ in range(2):
        with pytest.raises(type(failure)):
            observations.observe(tmp_path, "emulator", "default", {"name": "avd"}, row)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "manager,path,content",
    [
        ("lefthook", "lefthook.yml", "post-checkout: [unterminated\n"),
        ("lefthook", "lefthook.yml", "post-checkout:\n  commands: [unterminated\n"),
        ("pre-commit", ".pre-commit-config.yaml", "repos: [unterminated\n"),
    ],
)
def test_malformed_yaml_hook_is_required_input_error(tmp_path, manager, path, content):
    _git_init(tmp_path)
    _recipe(tmp_path)
    (tmp_path / path).write_text(content)
    result = status.build_status_report(tmp_path, RegistrySnapshot())
    assert result.exit_code == 1
    assert _checks(_row(result))["hook"]["state"] == "error"


@pytest.mark.parametrize(
    "content",
    [
        "post-checkout:\n  commands:\n    other:\n      run: echo ok\n",
        "post-checkout:\n  commands:\n    other:\n      run: echo [shell\n",
        "post-checkout:\n  commands:\n    other:\n      run: echo {shell # plain scalar\n",
        'post-checkout:\n  commands:\n    other:\n      run: "echo [shell"\n',
        "post-checkout:\n  commands:\n    other:\n      run: |\n        echo: [shell\n",
        'post-checkout: {commands: {other: {run: "echo \\"[shell"}}}\n',
        'post-checkout:\n  commands: {other: {run: "echo \\"[shell"}}\n',
        'post-checkout:\n  commands: {other: {run: "echo \\\\\\"[shell"}}\n',
        'post-checkout:\n  commands: {other: {run: "echo \\\\"}}\n',
    ],
)
def test_parseable_unwired_yaml_hook_is_finding(tmp_path, content):
    _git_init(tmp_path)
    _recipe(tmp_path)
    (tmp_path / "lefthook.yml").write_text(content)
    result = status.build_status_report(tmp_path, RegistrySnapshot())
    assert result.exit_code == 0
    assert _checks(_row(result))["hook"]["state"] == "finding"


def test_installed_manager_does_not_require_unused_package_json(tmp_path):
    _git_init(tmp_path)
    _recipe(tmp_path)
    (tmp_path / ".git" / "hooks" / "post-checkout").write_text("#!/bin/sh\ncall_lefthook\n")
    (tmp_path / "lefthook.yml").write_text(
        "post-checkout:\n  commands:\n    other:\n      run: echo ok\n"
    )
    (tmp_path / "package.json").write_text("{")
    result = status.build_status_report(tmp_path, RegistrySnapshot())
    assert result.exit_code == 0
    assert _checks(_row(result))["hook"]["state"] == "finding"


def test_selected_package_json_parse_error_is_required_input(tmp_path):
    _git_init(tmp_path)
    _recipe(tmp_path)
    (tmp_path / "package.json").write_text("{")
    result = status.build_status_report(tmp_path, RegistrySnapshot())
    assert result.exit_code == 1
    assert _checks(_row(result))["hook"]["state"] == "error"


def test_interrupt_retains_completed_fleet_rows(tmp_path, monkeypatch):
    first, second = tmp_path / "a", tmp_path / "b"
    snapshot = RegistrySnapshot(values=((str(first), "A", "a"), (str(second), "B", "b")))
    original = status._checkout

    def inspect(path, registry, context):
        if path == str(second):
            raise KeyboardInterrupt
        return original(path, registry, context)

    monkeypatch.setattr(status, "_checkout", inspect)
    result = status.build_status_report(tmp_path, snapshot, show_all=True)
    assert result.exit_code == 130
    assert result.status == "partial"
    assert [row["checkout"] for row in result.data["checkouts"]] == [str(first)]


def test_status_dispatch_does_not_construct_registry_or_consume_notices(
    tmp_path, monkeypatch, capsys
):
    _recipe(tmp_path)
    monkeypatch.setattr(sd.cli, "Registry", lambda: pytest.fail("mutable registry constructed"))
    monkeypatch.setattr(
        sd.cli, "_consume_claim_notices", lambda *args: pytest.fail("notice consumed")
    )
    assert sd.main(["--cwd", str(tmp_path), "status"]) == 0
    assert "checkout:" in capsys.readouterr().out


@pytest.mark.parametrize("obsolete", ["local", "--check"])
def test_removed_status_spellings_are_usage_errors(tmp_path, obsolete, capsys):
    assert sd.main(["--format", "json", "--cwd", str(tmp_path), "status", obsolete]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "invalid_arguments"


def test_status_all_claim_only_deleted_identity(tmp_path):
    claim = sd.PhysicalClaim(
        "recipe:/repo:device:pixel", "android", "PXL", "pixel", str(tmp_path / "gone"), "2026-01-01"
    )
    result = status.build_status_report(tmp_path, RegistrySnapshot(claims=(claim,)), show_all=True)
    assert result.exit_code == 0
    assert _row(result)["counts"]["claim"] == 1
    assert _row(result)["checkout"] == str(tmp_path / "gone")


def test_target_name_template_preserves_resolution_and_caches_git(tmp_path, monkeypatch):
    calls = []

    def git(argv, **kwargs):
        calls.append((argv, kwargs["timeout"]))
        text = "/repo/project\n" if argv[1] == "rev-parse" else "feature/topic\n"
        return subprocess.CompletedProcess(argv, 0, text, "")

    monkeypatch.setattr(status_targets.subprocess, "run", git)
    observations = status_targets.TargetObservations()
    spec = {"name": "{{ repo }}/{{ branch }}/{{ cwd }}"}
    expected = f"project_feature_topic_{tmp_path.name}"
    assert observations.name(tmp_path, "emulator", "default", spec) == expected
    assert observations.name(tmp_path, "emulator", "other", spec) == expected
    assert [timeout for _, timeout in calls] == [5, 5]


def test_target_name_git_timeout_is_unavailable_and_cached(tmp_path, monkeypatch):
    calls = []

    def timeout(argv, **kwargs):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(status_targets.subprocess, "run", timeout)
    observations = status_targets.TargetObservations()
    for _ in range(2):
        with pytest.raises(subprocess.TimeoutExpired):
            observations.name(tmp_path, "emulator", "default", {"name": "{{ branch }}"})
    assert len(calls) == 1


def test_ignored_loader_config_and_unrelated_binary_hook_are_not_errors(tmp_path):
    _git_init(tmp_path)
    _recipe(tmp_path, '[project]\nloader="mise"\n')
    (tmp_path / "mise.toml").write_text('[env]\n_.file="splashdown.env"\n')
    (tmp_path / ".mise.toml").write_text("[invalid")
    (tmp_path / ".git" / "hooks" / "pre-commit").write_bytes(b"\xff\x00binary")
    result = status.build_status_report(tmp_path, RegistrySnapshot())
    assert result.exit_code == 0
    assert _checks(_row(result))["loader"]["state"] == "ok"


def test_hook_discovery_reuses_bounded_git_inputs(tmp_path, monkeypatch):
    _git_init(tmp_path)
    _recipe(tmp_path)
    original = status._git
    calls = []

    def run(cwd, args):
        calls.append(args)
        return original(cwd, args)

    monkeypatch.setattr(status, "_git", run)
    monkeypatch.setattr(
        sd.hooks.subprocess,
        "check_output",
        lambda *args, **kwargs: pytest.fail("repeated Git discovery"),
    )
    result = status.build_status_report(tmp_path, RegistrySnapshot())
    assert result.exit_code == 0
    assert len(calls) == 2
