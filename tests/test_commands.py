from __future__ import annotations

import json
import subprocess

import pytest

import splashdown as sd
from conftest import (
    _IPHONE,
    _git_init,
    _inv_none,
    _stub_physical,
    _write_physical_recipe,
)


def test_physical_run_claims_snapshot_before_launch_and_reuses_its_claim(tmp_path, monkeypatch):
    _write_physical_recipe(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _stub_physical(monkeypatch, ios=[_IPHONE])
    monkeypatch.setattr(
        sd.device_claims, "discover_physical_snapshot", lambda *_args, **_kwargs: (_IPHONE,)
    )
    monkeypatch.setattr(
        sd.devices, "ios_boot", lambda u, s: pytest.fail("should not boot hardware")
    )
    monkeypatch.setattr(
        sd.target_commands, "ios_boot", lambda u, s: pytest.fail("should not boot hardware")
    )
    captured = {}

    def _fake_run(cwd, recipe, info):
        captured["info"] = info
        claim = sd.Registry().all_claims()[0]
        assert claim.hardware_id == info["udid"]
        return 0

    monkeypatch.setattr(sd.target_commands, "device_run", _fake_run)
    assert sd.main(["--cwd", str(tmp_path), "run", "device"]) == 0
    assert sd.main(["--cwd", str(tmp_path), "run", "device"]) == 0
    assert captured["info"]["udid"] == "00008-PHONE"
    assert captured["info"]["physical"] is True
    assert [claim.target_label for claim in sd.Registry().all_claims()] == ["default"]


def test_explicit_physical_actions_require_recipe_even_with_global_target(
    tmp_path, registry, monkeypatch
):
    sd.global_target_add("device", "pixel", {"platform": "android", "id": "PXL1234"})
    monkeypatch.setattr(sd.target_commands, "validate_device_run", lambda *_args: None)
    monkeypatch.setattr(sd.target_commands, "device_run", lambda *_args: 0)
    monkeypatch.setattr(
        sd.device_claims,
        "discover_physical_snapshot",
        lambda *_args, **_kwargs: ({"id": "PXL1234", "name": "Pixel", "platform": "android"},),
    )

    with pytest.raises(FileNotFoundError):
        sd.cmd_run(tmp_path, registry, "device", "pixel")
    with pytest.raises(FileNotFoundError):
        sd.cmd_target_claim(tmp_path, registry, "pixel", available=None, force=False, fmt="text")
    with pytest.raises(FileNotFoundError):
        sd.cmd_target_claim(tmp_path, registry, None, available="android", force=False, fmt="text")
    with pytest.raises(FileNotFoundError):
        sd.cmd_target_release(tmp_path, registry, "pixel", all_owned=False, force=False)


@pytest.mark.parametrize(
    ("source", "config"),
    [
        ("recipe", "splashdown.toml"),
        ("local", "splashdown.local.toml"),
        ("global", "xdg-config/splashdown/config.toml"),
    ],
)
def test_physical_run_claims_targets_from_every_configuration_source(
    tmp_path, monkeypatch, source, config
):
    config_path = tmp_path / config
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text('[targets.device.pixel]\nplatform = "android"\nid = "PXL1234"\n')
    if source != "recipe":
        (tmp_path / sd.RECIPE_NAME).write_text("")
    (tmp_path / "pubspec.yaml").write_text("name: demo\n")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(
        sd.device_claims,
        "discover_physical_snapshot",
        lambda *_args, **_kwargs: ({"id": "PXL1234", "name": "Pixel", "platform": "android"},),
    )
    launched: list[str] = []
    monkeypatch.setattr(
        sd.target_commands,
        "device_run",
        lambda _cwd, _recipe, info: launched.append(info["serial"]) or 0,
    )

    assert sd.main(["--cwd", str(tmp_path), "run", "device", "pixel"]) == 0

    assert launched == ["PXL1234"]
    assert sd.Registry().all_claims()[0].target_label == "pixel"


def test_physical_run_claim_rejects_other_live_owner_before_framework_launch(tmp_path, monkeypatch):
    (tmp_path / sd.RECIPE_NAME).write_text(
        '[targets.device.pixel]\nplatform = "android"\nid = "PXL1234"\n'
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    registry = sd.Registry()
    owner = tmp_path / "owner"
    owner.mkdir()
    target = sd.resolve_physical_target(tmp_path, "pixel")
    registry.attempt_claim(
        sd.PhysicalClaim(target.catalog_identity, "android", "PXL1234", "pixel", str(owner), "old")
    )
    monkeypatch.setattr(sd.target_commands, "validate_device_run", lambda *_args: None)
    monkeypatch.setattr(
        sd.device_claims,
        "discover_physical_snapshot",
        lambda *_args, **_kwargs: ({"id": "PXL1234", "name": "Pixel", "platform": "android"},),
    )
    monkeypatch.setattr(
        sd.target_commands, "device_run", lambda *_args: pytest.fail("framework launched")
    )

    assert sd.main(["--cwd", str(tmp_path), "run", "device", "pixel"]) == 1

    assert registry.all_claims() == (
        sd.PhysicalClaim(target.catalog_identity, "android", "PXL1234", "pixel", str(owner), "old"),
    )


@pytest.mark.parametrize(
    ("snapshot", "message"),
    [
        ((), "no connected physical device"),
        (
            (
                {"id": "PXL1234", "name": "Pixel", "platform": "android"},
                {"id": "PXL5678", "name": "Pixel", "platform": "android"},
            ),
            "multiple connected physical devices",
        ),
    ],
)
def test_physical_run_claim_rejects_unavailable_target_before_framework_launch(
    tmp_path, registry, monkeypatch, snapshot, message
):
    (tmp_path / sd.RECIPE_NAME).write_text('[targets.device.pixel]\nplatform = "android"\n')
    monkeypatch.setattr(sd.target_commands, "validate_device_run", lambda *_args: None)
    monkeypatch.setattr(
        sd.device_claims, "discover_physical_snapshot", lambda *_args, **_kwargs: snapshot
    )
    monkeypatch.setattr(
        sd.target_commands, "device_run", lambda *_args: pytest.fail("framework launched")
    )

    with pytest.raises(sd.DeviceError, match=message):
        sd.cmd_run(tmp_path, registry, "device", "pixel")

    assert registry.all_claims() == ()


def test_physical_run_claim_keeps_claim_after_framework_failure(tmp_path, registry, monkeypatch):
    (tmp_path / sd.RECIPE_NAME).write_text(
        '[targets.device.pixel]\nplatform = "android"\nid = "PXL1234"\n'
    )
    monkeypatch.setattr(sd.target_commands, "validate_device_run", lambda *_args: None)
    monkeypatch.setattr(
        sd.device_claims,
        "discover_physical_snapshot",
        lambda *_args, **_kwargs: ({"id": "PXL1234", "name": "Pixel", "platform": "android"},),
    )
    monkeypatch.setattr(sd.target_commands, "device_run", lambda *_args: 7)

    assert sd.cmd_run(tmp_path, registry, "device", "pixel") == 7

    assert registry.all_claims()[0].hardware_id == "PXL1234"


def test_cli_start_physical_reports_connected_without_boot(tmp_path, monkeypatch, capsys):
    _write_physical_recipe(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _stub_physical(monkeypatch, ios=[_IPHONE])
    monkeypatch.setattr(
        sd.devices, "ios_boot", lambda u, s: pytest.fail("should not boot hardware")
    )
    monkeypatch.setattr(
        sd.target_commands, "ios_boot", lambda u, s: pytest.fail("should not boot hardware")
    )
    rc = sd.main(["--cwd", str(tmp_path), "start", "device"])
    assert rc == 0
    assert "connected" in capsys.readouterr().err.lower()


def test_cli_stop_physical_is_noop_and_keeps_claim(tmp_path, monkeypatch):
    _write_physical_recipe(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = sd.resolve_physical_target(tmp_path, "default")
    sd.Registry().attempt_claim(
        sd.PhysicalClaim(
            target.catalog_identity,
            "ios",
            "00008-PHONE",
            "default",
            str(tmp_path.resolve()),
            "2026-08-26T10:00:00+00:00",
        )
    )
    monkeypatch.setattr(
        sd.target_commands,
        "device_shutdown_row",
        lambda row: pytest.fail("should not touch hardware"),
    )
    rc = sd.main(["--cwd", str(tmp_path), "stop", "device"])
    assert rc == 0
    assert sd.Registry().all_claims()[0].target_label == "default"


def test_cli_destroy_physical_is_noop(tmp_path, monkeypatch):
    _write_physical_recipe(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(
        sd.target_commands,
        "device_destroy_row",
        lambda row: pytest.fail("should not touch hardware"),
    )
    rc = sd.main(["--cwd", str(tmp_path), "destroy", "device"])
    assert rc == 0


def test_target_claim_specific_claims_configured_physical_variant(
    tmp_path, registry, monkeypatch, capsys
):
    (tmp_path / sd.RECIPE_NAME).write_text(
        '[targets.device.pixel]\nplatform = "android"\nid = "PXL1234"\n'
    )
    monkeypatch.setattr(
        sd.device_claims,
        "discover_physical_snapshot",
        lambda *_args, **_kwargs: ({"id": "PXL1234", "name": "Pixel", "platform": "android"},),
    )

    assert (
        sd.cmd_target_claim(tmp_path, registry, "pixel", available=None, force=False, fmt="text")
        == 0
    )

    claim = registry.all_claims()[0]
    assert claim.target_label == "pixel"
    assert claim.owner_checkout == str(tmp_path.resolve())
    assert capsys.readouterr().err == f"claimed pixel (android PXL1234) for {tmp_path.resolve()}\n"


def test_target_claim_available_prints_selected_variant_only(
    tmp_path, registry, monkeypatch, capsys
):
    (tmp_path / sd.RECIPE_NAME).write_text(
        '[targets.device.pixel]\nplatform = "android"\nid = "PXL1234"\n'
    )
    monkeypatch.setattr(
        sd.device_claims,
        "discover_physical_snapshot",
        lambda *_args, **_kwargs: ({"id": "PXL1234", "name": "Pixel", "platform": "android"},),
    )

    assert (
        sd.cmd_target_claim(tmp_path, registry, None, available="android", force=False, fmt="text")
        == 0
    )

    captured = capsys.readouterr()
    assert captured.out == "pixel\n"
    assert captured.err == ""


def test_target_claim_available_json_includes_selection_details(
    tmp_path, registry, monkeypatch, capsys
):
    (tmp_path / sd.RECIPE_NAME).write_text(
        '[targets.device.pixel]\nplatform = "android"\nid = "PXL1234"\n'
    )
    monkeypatch.setattr(
        sd.device_claims,
        "discover_physical_snapshot",
        lambda *_args, **_kwargs: ({"id": "PXL1234", "name": "Pixel", "platform": "android"},),
    )

    assert (
        sd.cmd_target_claim(tmp_path, registry, None, available="android", force=False, fmt="json")
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "target": "pixel",
        "source": "recipe",
        "platform": "android",
        "hardware_id": "PXL1234",
        "owner": str(tmp_path.resolve()),
        "claimed_at": payload["claimed_at"],
        "status": "claimed",
    }


def test_target_claims_reads_registry_without_device_discovery(
    registry, checkout, monkeypatch, capsys
):
    registry.attempt_claim(
        sd.PhysicalClaim(
            "recipe:/repo:device:pixel",
            "android",
            "PXL1234",
            "pixel",
            str(checkout.resolve()),
            "2026-08-26T10:00:00+00:00",
        )
    )
    monkeypatch.setattr(
        sd.device_claims,
        "discover_physical_snapshot",
        lambda *_args, **_kwargs: pytest.fail("claims must not discover devices"),
    )

    assert sd.cmd_target_claims(registry, "json") == 0

    assert json.loads(capsys.readouterr().out) == [
        {
            "target": "pixel",
            "source": "recipe",
            "platform": "android",
            "hardware_id": "PXL1234",
            "owner": str(checkout.resolve()),
            "claimed_at": "2026-08-26T10:00:00+00:00",
        }
    ]


def test_target_release_specific_is_discovery_free_and_releases_owner(
    tmp_path, registry, monkeypatch, capsys
):
    (tmp_path / sd.RECIPE_NAME).write_text('[targets.device.pixel]\nplatform = "android"\n')
    target = sd.resolve_physical_target(tmp_path, "pixel")
    registry.attempt_claim(
        sd.PhysicalClaim(
            target.catalog_identity,
            "android",
            "PXL1234",
            "pixel",
            str(tmp_path.resolve()),
            "2026-08-26T10:00:00+00:00",
        )
    )
    monkeypatch.setattr(
        sd.device_claims,
        "discover_physical_snapshot",
        lambda *_args, **_kwargs: pytest.fail("release must not discover devices"),
    )

    assert sd.cmd_target_release(tmp_path, registry, "pixel", all_owned=False, force=False) == 0

    assert registry.all_claims() == ()
    assert capsys.readouterr().err == "released pixel\n"


def test_target_release_all_only_removes_current_checkout_claims(tmp_path, registry, capsys):
    other = tmp_path / "other"
    other.mkdir()
    mine = sd.PhysicalClaim(
        "recipe:/repo:device:pixel", "android", "PXL", "pixel", str(tmp_path), "a"
    )
    theirs = sd.PhysicalClaim("recipe:/repo:device:ios", "ios", "IOS", "iphone", str(other), "b")
    registry.attempt_claim(mine)
    registry.attempt_claim(theirs)

    assert sd.cmd_target_release(tmp_path, registry, None, all_owned=True, force=False) == 0

    assert registry.all_claims() == (theirs,)
    assert capsys.readouterr().err == "released 1 physical claim(s)\n"


def test_target_release_busy_owner_and_missing_claim_have_stable_results(
    tmp_path, registry, capsys
):
    owner = tmp_path / "owner"
    owner.mkdir()
    (tmp_path / sd.RECIPE_NAME).write_text('[targets.device.pixel]\nplatform = "android"\n')
    target = sd.resolve_physical_target(tmp_path, "pixel")
    registry.attempt_claim(
        sd.PhysicalClaim(
            target.catalog_identity, "android", "PXL", "pixel", str(owner), "2026-08-26"
        )
    )

    with pytest.raises(sd.DeviceError, match="claimed by"):
        sd.cmd_target_release(tmp_path, registry, "pixel", all_owned=False, force=False)
    assert sd.cmd_target_release(tmp_path, registry, "pixel", all_owned=False, force=True) == 0
    assert sd.cmd_target_release(tmp_path, registry, "pixel", all_owned=False, force=False) == 0

    assert capsys.readouterr().err.endswith("no claim for pixel; nothing to release\n")


def test_forced_claim_writes_notice_and_survives_notice_write_failure(
    tmp_path, registry, monkeypatch, capsys
):
    owner = tmp_path / "owner"
    owner.mkdir()
    (tmp_path / sd.RECIPE_NAME).write_text(
        '[targets.device.pixel]\nplatform = "android"\nid = "PXL1234"\n'
    )
    target = sd.resolve_physical_target(tmp_path, "pixel")
    registry.attempt_claim(
        sd.PhysicalClaim(target.catalog_identity, "android", "PXL1234", "pixel", str(owner), "a")
    )
    monkeypatch.setattr(
        sd.device_claims,
        "discover_physical_snapshot",
        lambda *_args, **_kwargs: ({"id": "PXL1234", "name": "Pixel", "platform": "android"},),
    )
    monkeypatch.setattr(
        registry,
        "add_claim_notices",
        lambda _rows: (_ for _ in ()).throw(OSError("disk")),
    )

    assert (
        sd.cmd_target_claim(tmp_path, registry, "pixel", available=None, force=True, fmt="text")
        == 0
    )

    assert registry.all_claims()[0].owner_checkout == str(tmp_path.resolve())
    assert "warning: could not record claim notice: disk" in capsys.readouterr().err


def test_forced_release_writes_notice_for_displaced_owner(tmp_path, registry):
    owner = tmp_path / "owner"
    owner.mkdir()
    (tmp_path / sd.RECIPE_NAME).write_text('[targets.device.pixel]\nplatform = "android"\n')
    target = sd.resolve_physical_target(tmp_path, "pixel")
    registry.attempt_claim(
        sd.PhysicalClaim(target.catalog_identity, "android", "PXL", "pixel", str(owner), "a")
    )

    assert sd.cmd_target_release(tmp_path, registry, "pixel", all_owned=False, force=True) == 0

    notices = registry.consume_claim_notices(str(owner))
    assert len(notices) == 1
    assert notices[0].action == "release"
    assert notices[0].actor_checkout == str(tmp_path.resolve())


def test_forced_claim_keeps_notice_and_rendering_inside_operation_lock(
    tmp_path, registry, monkeypatch
):
    from contextlib import contextmanager

    owner = tmp_path / "owner"
    owner.mkdir()
    (tmp_path / sd.RECIPE_NAME).write_text(
        '[targets.device.pixel]\nplatform = "android"\nid = "PXL1234"\n'
    )
    target = sd.resolve_physical_target(tmp_path, "pixel")
    registry.attempt_claim(
        sd.PhysicalClaim(target.catalog_identity, "android", "PXL1234", "pixel", str(owner), "a")
    )
    monkeypatch.setattr(
        sd.device_claims,
        "discover_physical_snapshot",
        lambda *_args, **_kwargs: ({"id": "PXL1234", "name": "Pixel", "platform": "android"},),
    )
    active = False
    lock_events: list[bool] = []

    @contextmanager
    def track_lock(_checkout):
        nonlocal active
        active = True
        try:
            yield
        finally:
            active = False

    actual_add = registry.add_claim_notices
    monkeypatch.setattr(registry, "operation_lock", track_lock)
    monkeypatch.setattr(
        registry,
        "add_claim_notices",
        lambda notices: lock_events.append(active) or actual_add(notices),
    )
    monkeypatch.setattr(
        sd.cli_output,
        "render_claim_selection",
        lambda *_args, **_kwargs: lock_events.append(active),
    )

    assert (
        sd.cmd_target_claim(tmp_path, registry, "pixel", available=None, force=True, fmt="text")
        == 0
    )

    assert lock_events == [True, True]


def test_forced_release_keeps_notice_and_output_inside_operation_lock(
    tmp_path, registry, monkeypatch
):
    from contextlib import contextmanager

    owner = tmp_path / "owner"
    owner.mkdir()
    (tmp_path / sd.RECIPE_NAME).write_text('[targets.device.pixel]\nplatform = "android"\n')
    target = sd.resolve_physical_target(tmp_path, "pixel")
    registry.attempt_claim(
        sd.PhysicalClaim(target.catalog_identity, "android", "PXL", "pixel", str(owner), "a")
    )
    active = False
    lock_events: list[bool] = []

    @contextmanager
    def track_lock(_checkout):
        nonlocal active
        active = True
        try:
            yield
        finally:
            active = False

    class _Stderr:
        def write(self, _text):
            lock_events.append(active)

        def flush(self):
            return None

    actual_add = registry.add_claim_notices
    monkeypatch.setattr(registry, "operation_lock", track_lock)
    monkeypatch.setattr(
        registry,
        "add_claim_notices",
        lambda notices: lock_events.append(active) or actual_add(notices),
    )
    monkeypatch.setattr(sd.target_commands.sys, "stderr", _Stderr())

    assert sd.cmd_target_release(tmp_path, registry, "pixel", all_owned=False, force=True) == 0

    assert lock_events and all(lock_events)


def test_release_all_writes_output_inside_operation_lock(tmp_path, registry, monkeypatch):
    from contextlib import contextmanager

    registry.attempt_claim(
        sd.PhysicalClaim("recipe:/repo:device:pixel", "android", "PXL", "pixel", str(tmp_path), "a")
    )
    active = False
    output_lock_state: list[bool] = []

    @contextmanager
    def track_lock(_checkout):
        nonlocal active
        active = True
        try:
            yield
        finally:
            active = False

    class _Stderr:
        def write(self, _text):
            output_lock_state.append(active)

        def flush(self):
            return None

    monkeypatch.setattr(registry, "operation_lock", track_lock)
    monkeypatch.setattr(sd.target_commands.sys, "stderr", _Stderr())

    assert sd.cmd_target_release(tmp_path, registry, None, all_owned=True, force=False) == 0

    assert output_lock_state and all(output_lock_state)


def test_cli_destroy_confirms_before_deleting(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "splashdown.toml").write_text('[targets.simulator.default]\nmodel = "iPhone 15"\n')
    destroyed: list = []
    registry = sd.Registry()
    registry.set_device(
        str(tmp_path.resolve()), "simulator", "default", "UDID-STORED", "iPhone 15", "18.5"
    )
    monkeypatch.setattr(sd.target_commands, "device_destroy_row", destroyed.append)

    # Declining at the prompt aborts without touching the device.
    monkeypatch.setattr("builtins.input", lambda: "n")
    assert sd.main(["--cwd", str(tmp_path), "destroy", "simulator"]) == 1
    assert destroyed == []

    # --yes skips the prompt and destroys.
    assert sd.main(["--cwd", str(tmp_path), "destroy", "simulator", "--yes"]) == 0
    assert [row.identifier for row in destroyed] == ["UDID-STORED"]


def test_run_releases_operation_lock_before_launch(tmp_path, registry, monkeypatch):
    from contextlib import contextmanager

    active = False
    events = []
    recipe = sd.Recipe({}, tmp_path / sd.RECIPE_NAME)

    @contextmanager
    def track_lock(target):
        nonlocal active
        active = True
        events.append(("enter", target))
        yield
        active = False
        events.append(("exit", target))

    def reconcile(*_args, **_kwargs):
        assert active is True
        return {"kind": "ios", "udid": "UDID", "name": "sim"}

    def boot(*_args):
        assert active is True

    def launch(*_args):
        assert active is False
        return 0

    monkeypatch.setattr(registry, "operation_lock", track_lock)
    monkeypatch.setattr(sd.target_commands, "_infer_dtype", lambda *_args: "simulator")
    monkeypatch.setattr(
        sd.target_commands,
        "_resolve_variant_for_cli",
        lambda *_args: ("default", {"model": "iPhone 17"}, recipe),
    )
    monkeypatch.setattr(sd.target_commands, "validate_device_run", lambda *_args: None)
    monkeypatch.setattr(sd.target_commands, "ensure_fresh_sim", reconcile)
    monkeypatch.setattr(sd.target_commands, "_ios_current_state", lambda *_args: "Shutdown")
    monkeypatch.setattr(sd.target_commands, "ios_boot", boot)
    monkeypatch.setattr(sd.target_commands, "device_run", launch)

    assert sd.target_commands.cmd_run(tmp_path, registry, None, None) == 0
    target = str(tmp_path.resolve())
    assert events == [("enter", target), ("exit", target)]


@pytest.mark.parametrize("action", ["start", "stop", "destroy"])
def test_short_device_lifecycle_actions_hold_operation_lock(
    tmp_path, registry, monkeypatch, action
):
    from contextlib import contextmanager

    active = False
    mutations = []
    recipe = sd.Recipe({}, tmp_path / sd.RECIPE_NAME)

    @contextmanager
    def track_lock(_target):
        nonlocal active
        active = True
        yield
        active = False

    def mutate(*_args):
        assert active is True
        mutations.append(action)

    monkeypatch.setattr(registry, "operation_lock", track_lock)
    monkeypatch.setattr(sd.target_commands, "_infer_dtype", lambda *_args: "simulator")
    monkeypatch.setattr(
        sd.target_commands,
        "_resolve_variant_for_cli",
        lambda *_args: ("default", {"model": "iPhone 17"}, recipe),
    )
    if action == "start":
        monkeypatch.setattr(
            sd.target_commands,
            "ensure_fresh_sim",
            lambda *_args, **_kwargs: {"kind": "ios", "udid": "UDID", "name": "sim"},
        )
        monkeypatch.setattr(sd.target_commands, "_ios_current_state", lambda *_args: "Shutdown")
        monkeypatch.setattr(sd.target_commands, "ios_boot", mutate)
        assert sd.target_commands.cmd_start(tmp_path, registry, None, None) == 0
    elif action == "stop":
        registry.set_device(
            str(tmp_path.resolve()), "simulator", "default", "UDID", "iPhone 17", "18.5"
        )
        monkeypatch.setattr(sd.target_commands, "device_shutdown_row", mutate)
        assert sd.target_commands.cmd_stop(tmp_path, registry, None, None) == 0
    else:
        registry.set_device(
            str(tmp_path.resolve()), "simulator", "default", "UDID", "iPhone 17", "18.5"
        )
        monkeypatch.setattr(sd.target_commands, "device_destroy_row", mutate)
        assert sd.target_commands.cmd_destroy(tmp_path, registry, None, None, yes=True) == 0

    assert mutations == [action]


def test_android_start_uses_registry_state_directory(tmp_path, registry, monkeypatch):
    recipe = sd.Recipe({}, tmp_path / sd.RECIPE_NAME)
    captured = {}

    monkeypatch.setattr(sd.target_commands, "_infer_dtype", lambda *_args: "emulator")
    monkeypatch.setattr(
        sd.target_commands,
        "_resolve_variant_for_cli",
        lambda *_args: ("default", {"device": "pixel_9"}, recipe),
    )
    monkeypatch.setattr(
        sd.target_commands,
        "ensure_fresh_sim",
        lambda *_args, **_kwargs: sd.AndroidDestination("demo", None, owned=True),
    )

    def boot(name, *, state_dir):
        captured["call"] = (name, state_dir)
        return "emulator-5554"

    monkeypatch.setattr(sd.target_commands, "android_boot", boot)

    assert sd.target_commands.cmd_start(tmp_path, registry, None, None) == 0
    assert captured["call"] == ("demo", registry.state_dir)


def test_cli_status_hints_unfilled_set_resource(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "splashdown.toml").write_text('[resources.MODE]\ntype = "set"\n')
    assert sd.main(["--cwd", str(tmp_path), "status"]) == 0
    err = capsys.readouterr().err
    assert "MODE" in err and "splash env set" in err


def test_cli_devices_lists_physical_status(tmp_path, monkeypatch, capsys):
    _write_physical_recipe(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _stub_physical(monkeypatch, ios=[_IPHONE])
    monkeypatch.setattr(sd.device_claims, "_ios_physical_devices", lambda **_kwargs: [_IPHONE])
    monkeypatch.setattr(sd.device_claims, "_android_physical_devices", lambda **_kwargs: [])
    rc = sd.main(["--cwd", str(tmp_path), "target"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "default" in out
    assert "connected" in out


def test_device_add_physical_writes_id_and_platform(tmp_path):
    sd.target_add(tmp_path, "device", "my-phone", {"id": "ABC123", "platform": "ios", "name": None})
    text = (tmp_path / "splashdown.local.toml").read_text()
    assert "[targets.device.my-phone]" in text
    assert 'id = "ABC123"' in text
    assert 'platform = "ios"' in text


def test_ios_native_run_physical_uses_devicectl(tmp_path, monkeypatch):
    monkeypatch.setattr(sd.capabilities.sys, "platform", "darwin")

    # Build a fake .app with an Info.plist so the run path reaches install/launch.
    app = tmp_path / "Demo.app"
    app.mkdir()
    import plistlib

    with (app / "Info.plist").open("wb") as f:
        plistlib.dump({"CFBundleIdentifier": "com.demo"}, f)

    recipe = sd.Recipe(
        {"project": {"ios": {"scheme": "Demo", "project": "Demo.xcodeproj"}}},
        tmp_path / "splashdown.toml",
    )
    calls = []
    monkeypatch.setattr(sd.runners.subprocess, "call", lambda args, **k: calls.append(args) or 0)
    monkeypatch.setattr(sd.runners, "call_finite", lambda args, **k: calls.append(args) or 0)

    class _Done:
        stdout = json.dumps(
            [{"buildSettings": {"BUILT_PRODUCTS_DIR": str(tmp_path), "WRAPPER_NAME": "Demo.app"}}]
        )

    monkeypatch.setattr(sd.runners.subprocess, "run", lambda *a, **k: _Done())

    info = {"kind": "ios", "udid": "00008-PHONE", "physical": True}
    rc = sd.runners._ios_native_run(tmp_path, recipe, info)
    assert rc == 0
    flat = [" ".join(c) for c in calls]
    assert any("devicectl" in c and "install" in c for c in flat)
    assert any("devicectl" in c and "launch" in c for c in flat)
    assert not any("simctl" in c for c in flat)


@pytest.mark.parametrize(
    ("runner", "info", "expected"),
    [
        (
            sd.runners._flutter_run,
            {"kind": "android", "serial": "emulator-5554"},
            ("flutter", "install Flutter"),
        ),
        (
            sd.runners._rn_run,
            {"kind": "android", "serial": "emulator-5554"},
            ("node", "install Node.js"),
        ),
    ],
)
def test_framework_missing_launcher_is_capability_error(
    tmp_path, monkeypatch, runner, info, expected
):
    recipe = sd.Recipe({}, tmp_path / sd.RECIPE_NAME)
    monkeypatch.setattr(
        sd.runners.subprocess,
        "call",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )

    capability, message = expected
    with pytest.raises(sd.CapabilityError, match=message) as raised:
        runner(tmp_path, recipe, info)

    assert raised.value.capability == capability


def test_ios_native_build_requires_macos_before_launch(tmp_path, monkeypatch):
    recipe = sd.Recipe(
        {"project": {"ios": {"scheme": "Demo", "project": "Demo.xcodeproj"}}},
        tmp_path / sd.RECIPE_NAME,
    )
    monkeypatch.setattr(sd.capabilities.sys, "platform", "linux")
    monkeypatch.setattr(
        sd.runners.subprocess,
        "call",
        lambda *args, **kwargs: pytest.fail("xcodebuild launched"),
    )

    with pytest.raises(
        sd.CapabilityError, match="iOS native build support requires macOS and Xcode"
    ):
        sd.runners._ios_native_run(tmp_path, recipe, {"kind": "ios", "udid": "UDID"})


def test_ios_native_missing_xcodebuild_is_capability_error(tmp_path, monkeypatch):
    recipe = sd.Recipe(
        {"project": {"ios": {"scheme": "Demo", "project": "Demo.xcodeproj"}}},
        tmp_path / sd.RECIPE_NAME,
    )
    monkeypatch.setattr(sd.capabilities.sys, "platform", "darwin")
    monkeypatch.setattr(
        sd.runners.subprocess,
        "call",
        lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("denied")),
    )

    with pytest.raises(sd.CapabilityError, match="install Xcode") as raised:
        sd.runners._ios_native_run(tmp_path, recipe, {"kind": "ios", "udid": "UDID"})

    assert raised.value.capability == "ios"


def test_android_native_missing_gradle_is_capability_error(tmp_path, monkeypatch):
    recipe = sd.Recipe(
        {"project": {"android": {"application_id": "com.demo"}}},
        tmp_path / sd.RECIPE_NAME,
    )
    monkeypatch.setattr(
        sd.runners.subprocess,
        "call",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )

    with pytest.raises(sd.CapabilityError, match="install Gradle or add") as raised:
        sd.runners._android_native_run(
            tmp_path, recipe, {"kind": "android", "serial": "emulator-5554"}
        )

    assert raised.value.capability == "gradle"


def test_android_native_missing_adb_is_capability_error(tmp_path, monkeypatch):
    recipe = sd.Recipe(
        {"project": {"android": {"application_id": "com.demo"}}},
        tmp_path / sd.RECIPE_NAME,
    )

    def launch(args, **kwargs):
        if args[0] == "adb":
            raise FileNotFoundError("missing")
        return 0

    monkeypatch.setattr(sd.runners.subprocess, "call", launch)

    with pytest.raises(sd.CapabilityError, match="install Android SDK platform-tools") as raised:
        sd.runners._android_native_run(
            tmp_path, recipe, {"kind": "android", "serial": "emulator-5554"}
        )

    assert raised.value.capability == "android"


def test_fixed_launcher_nonzero_exit_is_returned(tmp_path, monkeypatch):
    monkeypatch.setattr(sd.runners.subprocess, "call", lambda *args, **kwargs: 7)

    assert (
        sd.runners._flutter_run(
            tmp_path,
            sd.Recipe({}, tmp_path / sd.RECIPE_NAME),
            {"kind": "android", "serial": "emulator-5554"},
        )
        == 7
    )


def test_custom_run_missing_command_returns_shell_status(tmp_path):
    recipe = sd.Recipe(
        {"project": {"run": "splashdown-command-that-does-not-exist"}},
        tmp_path / sd.RECIPE_NAME,
    )

    rc = sd.runners.run_custom_command(
        tmp_path, recipe, {"kind": "android", "serial": "emulator-5554"}
    )

    assert rc == 127


def test_device_add_writes_nested_table(tmp_path):
    sd.target_add(tmp_path, "simulator", "repro-bug", {"model": "iPhone 16", "ios": "17.5"})
    text = (tmp_path / "splashdown.local.toml").read_text()
    assert "[targets.simulator.repro-bug]" in text
    assert 'model = "iPhone 16"' in text
    assert 'ios = "17.5"' in text


def test_device_add_rejects_collision_with_local(tmp_path):
    sd.target_add(tmp_path, "simulator", "repro", {"model": "A"})
    with pytest.raises(sd.DeviceError, match="already exists"):
        sd.target_add(tmp_path, "simulator", "repro", {"model": "B"})


def test_device_add_rejects_collision_with_recipe(tmp_path):
    (tmp_path / "splashdown.toml").write_text('[targets.simulator.default]\nmodel = "iPhone 17"\n')
    with pytest.raises(sd.DeviceError, match="recipe"):
        sd.target_add(tmp_path, "simulator", "default", {"model": "iPhone 16"})


def test_device_add_rejects_bad_type(tmp_path):
    with pytest.raises(sd.DeviceError, match="type"):
        sd.target_add(tmp_path, "not-a-type", "default", {})


def test_device_add_rejects_bad_variant(tmp_path):
    with pytest.raises(sd.DeviceError, match="variant"):
        sd.target_add(tmp_path, "simulator", "has spaces", {"model": "X"})


@pytest.mark.parametrize(
    ("dtype", "fields"),
    [
        ("simulator", {"device": "pixel_9"}),
        ("emulator", {"ios": "latest"}),
        ("device", {"model": "iPhone 17"}),
    ],
)
def test_device_add_rejects_incompatible_fields_before_writing(tmp_path, dtype, fields):
    with pytest.raises(sd.DeviceError, match="unknown field"):
        sd.target_add(tmp_path, dtype, "default", fields)
    assert not (tmp_path / "splashdown.local.toml").exists()


def test_cli_target_add_rejects_incompatible_flags_cleanly(tmp_path, capsys):
    rc = sd.main(
        [
            "--cwd",
            str(tmp_path),
            "target",
            "add",
            "simulator",
            "default",
            "--device",
            "pixel_9",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "unknown field `device`" in captured.err
    assert "Traceback" not in captured.err
    assert not (tmp_path / "splashdown.local.toml").exists()


def test_cli_target_add_writes_local_fields(tmp_path):
    rc = sd.main(
        [
            "--cwd",
            str(tmp_path),
            "target",
            "add",
            "emulator",
            "pixel",
            "--device=pixel_9",
            "--image=system-images;android-35;google_apis;arm64-v8a",
            "--name=pixel-local",
        ]
    )
    assert rc == 0
    local = sd.LocalConfig.load(tmp_path / sd.LOCAL_NAME)
    assert local.targets["emulator"]["pixel"] == {
        "device": "pixel_9",
        "image": "system-images;android-35;google_apis;arm64-v8a",
        "name": "pixel-local",
    }


def test_device_remove_strips_local_variant(tmp_path):
    sd.target_add(tmp_path, "simulator", "repro", {"model": "X"})
    sd.target_add(tmp_path, "simulator", "other", {"model": "Y"})
    sd.target_remove(tmp_path, "simulator", "repro")
    lc = sd.LocalConfig.load(tmp_path / "splashdown.local.toml")
    assert "repro" not in lc.targets.get("simulator", {})
    assert "other" in lc.targets["simulator"]


def test_device_remove_refuses_recipe_variant(tmp_path):
    (tmp_path / "splashdown.toml").write_text('[targets.simulator.default]\nmodel = "iPhone 17"\n')
    with pytest.raises(sd.DeviceError, match="recipe"):
        sd.target_remove(tmp_path, "simulator", "default")


def test_device_remove_errors_when_missing(tmp_path):
    with pytest.raises(sd.DeviceError, match="no target"):
        sd.target_remove(tmp_path, "simulator", "ghost")


def test_target_add_global_writes_config(tmp_path):
    rc = sd.main(
        [
            "--cwd",
            str(tmp_path),
            "target",
            "add",
            "device",
            "my-iphone",
            "--platform=ios",
            "--name=Niels iPhone",
            "--global",
        ]
    )
    assert rc == 0
    gc = sd.GlobalConfig.load(sd._global_config_path())
    assert gc.targets["device"]["my-iphone"]["platform"] == "ios"
    assert not (tmp_path / sd.LOCAL_NAME).exists()


def test_target_remove_global(tmp_path):
    sd.global_target_add("device", "my-iphone", {"platform": "ios"})
    rc = sd.main(["--cwd", str(tmp_path), "target", "remove", "device", "my-iphone", "--global"])
    assert rc == 0
    assert "my-iphone" not in sd.GlobalConfig.load(sd._global_config_path()).targets.get(
        "device", {}
    )


def test_target_list_marks_global_source(tmp_path, monkeypatch, capsys):
    _stub_physical(monkeypatch, ios=[_IPHONE])
    sd.global_target_add("device", "my-iphone", {"platform": "ios"})
    assert sd.main(["--cwd", str(tmp_path), "target"]) == 0
    line = next(ln for ln in capsys.readouterr().out.splitlines() if "my-iphone" in ln)
    assert line.split("\t")[1] == "global"


def test_target_list_annotates_shadowed_global(tmp_path, monkeypatch, capsys):
    _stub_physical(monkeypatch, ios=[_IPHONE])
    (tmp_path / sd.RECIPE_NAME).write_text('[targets.device.my-iphone]\nplatform = "ios"\n')
    sd.global_target_add("device", "my-iphone", {"platform": "android"})
    assert sd.main(["--cwd", str(tmp_path), "target"]) == 0
    line = next(ln for ln in capsys.readouterr().out.splitlines() if "my-iphone" in ln)
    assert "shadows global" in line


def test_global_device_resolves_in_repo_with_no_targets(tmp_path):
    p = sd._global_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('[targets.device.my-iphone]\nplatform = "ios"\n')
    assert sd.target_commands._declared_target_types(tmp_path) == ["device"]
    variant, spec, _ = sd.target_commands._resolve_variant_for_cli(tmp_path, "device", None)
    assert variant == "my-iphone"
    assert spec["platform"] == "ios"


def test_target_refresh_aborts_on_malformed_global(tmp_path, registry):
    registry.set_device(str(tmp_path), "simulator", "default", "UDID1", "iPhone 17", "18.0")
    p = sd._global_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[targets.bogus.x]\n")
    with pytest.raises(ValueError, match="unknown target type"):
        sd.cmd_target_refresh(registry)
    # the row must survive — a malformed global config must never reap devices
    assert registry.get_device(str(tmp_path), "simulator", "default") is not None


def test_target_refresh_preflights_all_local_configs_before_mutation(
    tmp_path, registry, monkeypatch
):
    defunct = tmp_path / "gone"
    malformed = tmp_path / "malformed"
    malformed.mkdir()
    (malformed / sd.LOCAL_NAME).write_text("[targets.simulator.default\n")
    registry.set_device(str(defunct), "simulator", "default", "UDID-GONE", "iPhone 17", "18.0")
    registry.set_device(str(malformed), "simulator", "default", "UDID-LIVE", "iPhone 17", "18.0")
    destroyed: list[str] = []
    monkeypatch.setattr(sd.devices, "ios_destroy", destroyed.append)

    with pytest.raises(ValueError, match="invalid TOML"):
        sd.cmd_target_refresh(registry)

    assert destroyed == []
    assert {row.udid for row in registry.all_devices()} == {"UDID-GONE", "UDID-LIVE"}


def test_global_device_does_not_break_inference_in_mobile_project(tmp_path):
    (tmp_path / sd.RECIPE_NAME).write_text('[targets.simulator.default]\nmodel = "iPhone 17"\n')
    p = sd._global_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('[targets.device.my-iphone]\nplatform = "ios"\n')
    assert sd.target_commands._infer_dtype(tmp_path, None) == "simulator"


def test_exact_global_variant_selects_device_type_in_mobile_project(tmp_path):
    (tmp_path / sd.RECIPE_NAME).write_text('[targets.simulator.default]\nmodel = "iPhone 17"\n')
    p = sd._global_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('[targets.device.iphone17]\nplatform = "ios"\n')

    assert sd.target_commands._infer_dtype(tmp_path, None, "iphone17") == "device"


def test_exact_variant_shared_by_target_types_requires_explicit_type(tmp_path):
    (tmp_path / sd.RECIPE_NAME).write_text('[targets.simulator.shared]\nmodel = "iPhone 17"\n')
    p = sd._global_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('[targets.device.shared]\nplatform = "ios"\n')

    with pytest.raises(sd.DeviceError, match="variant `shared` exists under multiple target types"):
        sd.target_commands._infer_dtype(tmp_path, None, "shared")


@pytest.mark.parametrize("verb", ["start", "stop", "destroy"])
def test_cli_lifecycle_accepts_unique_global_variant_shorthand(tmp_path, monkeypatch, verb):
    (tmp_path / sd.RECIPE_NAME).write_text('[targets.simulator.default]\nmodel = "iPhone 17"\n')
    p = sd._global_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('[targets.device.iphone17]\nplatform = "ios"\nname = "Niels\'s iPhone"\n')
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _stub_physical(monkeypatch, ios=[_IPHONE])

    assert sd.main(["--cwd", str(tmp_path), verb, "iphone17"]) == 0


def test_global_device_type_prefix_stays_project_scoped(tmp_path):
    (tmp_path / sd.RECIPE_NAME).write_text('[targets.simulator.default]\nmodel = "iPhone 17"\n')
    p = sd._global_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('[targets.device.my-iphone]\nplatform = "ios"\n')
    from argparse import Namespace

    args = Namespace(cwd=str(tmp_path), dtype="d", variant=None)
    sd.cli._normalize_device_args(args)
    assert args.dtype is None
    assert args.variant == "d"


def test_remove_global_sourced_sim_without_global_flag_does_not_destroy(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / sd.RECIPE_NAME).write_text('[targets.simulator.default]\nmodel = "iPhone 17"\n')
    sd.global_target_add("simulator", "gsim", {"model": "iPhone 15"})
    destroyed: list = []
    monkeypatch.setattr(sd.target_commands, "device_destroy_row", destroyed.append)
    rc = sd.main(["--cwd", str(tmp_path), "target", "remove", "simulator", "gsim"])
    assert rc == 1
    assert destroyed == []
    assert "global variant" in capsys.readouterr().err


def test_load_variant_spec_loud_on_malformed_global(tmp_path):
    p = sd._global_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[targets.bogus.x]\n")
    with pytest.raises(ValueError, match="unknown target type"):
        sd.target_commands._load_variant_spec(tmp_path, "simulator", "default")


def test_target_list_annotates_local_shadowing_global(tmp_path, monkeypatch, capsys):
    _stub_physical(monkeypatch, ios=[_IPHONE])
    sd.target_add(tmp_path, "device", "my-iphone", {"platform": "ios"})
    sd.global_target_add("device", "my-iphone", {"platform": "android"})
    assert sd.main(["--cwd", str(tmp_path), "target"]) == 0
    line = next(ln for ln in capsys.readouterr().out.splitlines() if "my-iphone" in ln)
    assert line.split("\t")[1] == "local (shadows global)"


def test_target_refresh_keeps_global_sourced_sim(tmp_path, registry, monkeypatch):
    (tmp_path / sd.RECIPE_NAME).write_text('[targets.simulator.default]\nmodel = "iPhone 17"\n')
    p = sd._global_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('[targets.simulator.gsim]\nmodel = "iPhone 15"\n')
    registry.set_device(str(tmp_path), "simulator", "gsim", "UDID-G", "iPhone 15", "18.0")
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.devices, "_ios_latest_runtime_version", lambda: "18.0")
    assert sd.cmd_target_refresh(registry) == 0
    assert registry.get_device(str(tmp_path), "simulator", "gsim") is not None


def test_scanner_rn_declares_default_ios_variant(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies":{"react-native":"0.83"}}')
    sd.cmd_init(tmp_path)
    recipe = sd.Recipe.load(tmp_path / "splashdown.toml")
    assert "default" in recipe.targets.get("simulator", {})
    assert recipe.targets["simulator"]["default"]["model"]
    assert "SIM_NAME" not in recipe.resources


def test_scanner_flutter_declares_both_defaults(tmp_path):
    (tmp_path / "pubspec.yaml").write_text("name: demo\n")
    sd.cmd_init(tmp_path)
    recipe = sd.Recipe.load(tmp_path / "splashdown.toml")
    assert "default" in recipe.targets.get("simulator", {})
    assert "default" in recipe.targets.get("emulator", {})
    assert "SIM_NAME" not in recipe.resources


def test_local_skeleton_documents_additions(tmp_path):
    sd.cmd_init(tmp_path, preset="minimal")
    text = (tmp_path / "splashdown.local.toml").read_text()
    assert "additional" in text.lower() or "additions" in text.lower()
    assert "simulator" in text
    assert "splash target add" in text


def test_no_loader_delivery_prefers_env_for_dotenv_app(tmp_path):
    (tmp_path / ".env").write_text("")
    writer, msg = sd._resolve_no_loader_delivery(tmp_path, _inv_none(tmp_path, "nextjs"))
    assert writer == "envfile=.env"
    assert ".env" in msg


def test_no_loader_delivery_falls_back_to_env_local(tmp_path):
    (tmp_path / ".env.local").write_text("")
    writer, _msg = sd._resolve_no_loader_delivery(tmp_path, _inv_none(tmp_path, "nextjs"))
    assert writer == "envfile=.env.local"


def test_no_loader_delivery_prefers_env_over_env_local(tmp_path):
    (tmp_path / ".env").write_text("")
    (tmp_path / ".env.local").write_text("")
    writer, _msg = sd._resolve_no_loader_delivery(tmp_path, _inv_none(tmp_path, "nextjs"))
    assert writer == "envfile=.env"


def test_no_loader_delivery_no_apps_routes_to_file(tmp_path):
    (tmp_path / ".env").write_text("")
    writer, _msg = sd._resolve_no_loader_delivery(tmp_path, _inv_none(tmp_path))
    assert writer == "envfile=.env"


def test_no_loader_delivery_unknown_profile_routes_to_file(tmp_path):
    # `unknown` apps get the benefit of the doubt (treated as dotenv-capable).
    (tmp_path / ".env").write_text("")
    writer, _msg = sd._resolve_no_loader_delivery(tmp_path, _inv_none(tmp_path, "unknown"))
    assert writer == "envfile=.env"


def test_no_loader_delivery_no_dotenv_file_returns_none(tmp_path):
    writer, msg = sd._resolve_no_loader_delivery(tmp_path, _inv_none(tmp_path, "nextjs"))
    assert writer is None
    assert "splashdown.env" in msg


def test_no_loader_delivery_process_env_only_app_returns_none(tmp_path):
    # A dotenv file exists, but the only app (vite) reads from process.env — a
    # plain .env would reach nothing, so fall to instructions.
    (tmp_path / ".env").write_text("")
    writer, msg = sd._resolve_no_loader_delivery(tmp_path, _inv_none(tmp_path, "vite"))
    assert writer is None
    assert "install mise/direnv/devbox" in msg


def test_no_loader_delivery_mixed_routes_to_file_with_caveat(tmp_path):
    (tmp_path / ".env").write_text("")
    writer, msg = sd._resolve_no_loader_delivery(tmp_path, _inv_none(tmp_path, "nextjs", "vite"))
    assert writer == "envfile=.env"
    assert "app1" in msg
    assert "read env from the process" in msg


def test_no_loader_next_electron_keeps_profile_id_in_process_environment(tmp_path, capsys):
    (tmp_path / ".env").write_text("")
    (tmp_path / "package.json").write_text('{"dependencies":{"next":"16","electron":"43"}}')

    sd.cmd_init(tmp_path, electron_profile="isolated")

    recipe = sd.Recipe.load(tmp_path / "splashdown.toml")
    assert recipe.resources["PORT"]["writer"] == "envfile=.env"
    assert recipe.resources["ELECTRON_PROFILE_ID"]["writer"] == "splashdown-env"
    assert "main read env from the process" in capsys.readouterr().err


def test_no_loader_delivery_warns_when_target_tracked(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".env").write_text("")
    writer, msg = sd._resolve_no_loader_delivery(tmp_path, _inv_none(tmp_path, "nextjs"))
    assert writer == "envfile=.env"
    assert "not gitignored" in msg


def test_no_loader_delivery_no_warning_when_target_ignored(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(".env\n")
    (tmp_path / ".env").write_text("")
    _writer, msg = sd._resolve_no_loader_delivery(tmp_path, _inv_none(tmp_path, "nextjs"))
    assert "not gitignored" not in msg


def test_no_loader_delivery_no_warning_outside_git_repo(tmp_path):
    # No repo → `git check-ignore` exits 128; we must not nag spuriously.
    (tmp_path / ".env").write_text("")
    _writer, msg = sd._resolve_no_loader_delivery(tmp_path, _inv_none(tmp_path, "nextjs"))
    assert "not gitignored" not in msg


def test_none_loader_wire_is_noop(tmp_path):
    assert sd.LOADERS["none"].detect(tmp_path) is False
    sd.LOADERS["none"].wire(tmp_path)
    assert not (tmp_path / "mise.toml").exists()
    assert not (tmp_path / ".envrc").exists()


def test_profile_registry_exists_and_is_dict_of_str_to_profile():
    assert isinstance(sd.PROFILES, dict)
    for name, p in sd.PROFILES.items():
        assert isinstance(name, str)
        assert isinstance(p, sd.Profile)


def test_scanner_falls_back_to_unknown_when_no_profile_matches(tmp_path):
    # A directory with nothing recognizable.
    inv = sd.Scanner().scan(tmp_path)
    assert all(app.profile == "unknown" for app in inv.apps)


def test_vite_profile_detects_vite_config_ts(tmp_path):
    (tmp_path / "vite.config.ts").write_text("export default {}")
    p = sd.PROFILES["vite"]
    assert p.detect(tmp_path) is True


def test_vite_profile_detects_vite_config_js(tmp_path):
    (tmp_path / "vite.config.js").write_text("module.exports = {}")
    assert sd.PROFILES["vite"].detect(tmp_path) is True


def test_vite_profile_does_not_detect_without_config(tmp_path):
    assert sd.PROFILES["vite"].detect(tmp_path) is False


def test_vite_profile_emits_web_dev_port_resource(tmp_path):
    (tmp_path / "vite.config.ts").write_text("export default {}")
    app = sd.AppInventory(name="web", path=tmp_path, profile="vite")
    res = sd.PROFILES["vite"].resources(app)
    assert "WEB_DEV_PORT" in res
    assert res["WEB_DEV_PORT"]["type"] == "port"
    assert res["WEB_DEV_PORT"]["range"] == [5174, 5200]


def test_vite_profile_emits_api_dev_port_when_proxy_present(tmp_path):
    (tmp_path / "vite.config.ts").write_text(
        'export default { server: { proxy: { "/api": "http://localhost:9081" } } }'
    )
    app = sd.AppInventory(name="web", path=tmp_path, profile="vite")
    res = sd.PROFILES["vite"].resources(app)
    assert "API_DEV_PORT" in res
    assert res["API_DEV_PORT"]["type"] == "template"
    assert res["API_DEV_PORT"]["template"] == "{{ PORT }}"


def test_vite_profile_skips_api_dev_port_when_no_proxy(tmp_path):
    (tmp_path / "vite.config.ts").write_text("export default {}")
    app = sd.AppInventory(name="web", path=tmp_path, profile="vite")
    res = sd.PROFILES["vite"].resources(app)
    assert "API_DEV_PORT" not in res


def test_vite_wiring_check_detects_loadenv_pattern(tmp_path):
    (tmp_path / "vite.config.ts").write_text("""
import { defineConfig, loadEnv } from "vite";
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, import.meta.dirname, "");
  return { server: { port: Number(env.WEB_DEV_PORT ?? 5173) } };
});
""")
    app = sd.AppInventory(name="web", path=tmp_path, profile="vite")
    checks = sd.PROFILES["vite"].wiring_checks(app)
    check = next(c for c in checks if c.id == "vite-config-process-env")
    status, _ = check.detect(tmp_path)
    assert status == "problem"


def test_vite_wiring_check_autofix_swaps_loadenv_for_process_env(tmp_path):
    (tmp_path / "vite.config.ts").write_text("""\
import { defineConfig, loadEnv } from "vite";
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, import.meta.dirname, "");
  const webPort = Number(env.WEB_DEV_PORT ?? 5173);
  return { server: { port: webPort } };
});
""")
    app = sd.AppInventory(name="web", path=tmp_path, profile="vite")
    check = next(
        c for c in sd.PROFILES["vite"].wiring_checks(app) if c.id == "vite-config-process-env"
    )
    check.autofix(tmp_path)
    text = (tmp_path / "vite.config.ts").read_text()
    assert "process.env.WEB_DEV_PORT" in text
    status, _ = check.detect(tmp_path)
    assert status == "ok"


def test_vite_wiring_check_preserves_process_env_fallback_chain(tmp_path):
    # `process.env.X || env.X` is a deliberate shell-then-dotenv fallback. The
    # autofix used to rewrite the second term into a duplicate of the first,
    # silently deleting the dotenv layer.
    original = """\
import { defineConfig, loadEnv } from "vite";
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, import.meta.dirname, "");
  const webPort = Number(process.env.WEB_PORT || env.WEB_PORT || 5173);
  return { server: { port: webPort } };
});
"""
    (tmp_path / "vite.config.ts").write_text(original)
    app = sd.AppInventory(name="web", path=tmp_path, profile="vite")
    check = next(
        c for c in sd.PROFILES["vite"].wiring_checks(app) if c.id == "vite-config-process-env"
    )
    status, _ = check.detect(tmp_path)
    assert status == "ok"
    check.autofix(tmp_path)
    assert (tmp_path / "vite.config.ts").read_text() == original


def test_vite_wiring_check_still_fixes_uncovered_env_access(tmp_path):
    # One name has a process.env fallback, the other doesn't — only the bare one
    # gets rewritten.
    (tmp_path / "vite.config.ts").write_text("""\
import { defineConfig, loadEnv } from "vite";
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, import.meta.dirname, "");
  const webPort = Number(process.env.WEB_PORT || env.WEB_PORT || 5173);
  const apiPort = Number(env.API_DEV_PORT ?? 8080);
  return { server: { port: webPort, proxy: { "/api": `http://localhost:${apiPort}` } } };
});
""")
    app = sd.AppInventory(name="web", path=tmp_path, profile="vite")
    check = next(
        c for c in sd.PROFILES["vite"].wiring_checks(app) if c.id == "vite-config-process-env"
    )
    assert check.detect(tmp_path)[0] == "problem"
    check.autofix(tmp_path)
    text = (tmp_path / "vite.config.ts").read_text()
    assert "process.env.WEB_PORT || env.WEB_PORT" in text
    assert "process.env.API_DEV_PORT ?? 8080" in text
    assert check.detect(tmp_path)[0] == "ok"


def test_vite_port_wired_check_flags_config_that_never_names_the_port(tmp_path):
    # No server.port wiring at all — the allocated port goes unused, so reporting
    # "ok" would prove absence rather than presence.
    (tmp_path / "vite.config.ts").write_text(
        'import { defineConfig } from "vite";\nexport default defineConfig({ plugins: [] });\n'
    )
    app = sd.AppInventory(name="web", path=tmp_path, profile="vite")
    check = next(c for c in sd.PROFILES["vite"].wiring_checks(app) if c.id == "vite-port-wired")
    status, detail = check.detect(tmp_path)
    assert status == "problem"
    assert "WEB_DEV_PORT" in detail
    # Report-only: rewriting an arbitrary config to add server.port isn't safe.
    assert check.autofix is None


@pytest.mark.parametrize(
    "body",
    [
        'export default { server: { port: Number(process.env["WEB_DEV_PORT"]) || 5173 } };\n',
        "const { WEB_DEV_PORT } = process.env;\nexport default { server: { port: WEB_DEV_PORT } };\n",
        "export default { server: { port: Number(process.env.WEB_DEV_PORT) || 5173 } };\n",
    ],
)
def test_vite_checks_pass_on_correctly_wired_configs(tmp_path, body):
    # Bracket access and destructuring are correct wiring; flagging them made
    # `doctor --fix` fail permanently on a working project.
    (tmp_path / "vite.config.ts").write_text(body)
    app = sd.AppInventory(name="web", path=tmp_path, profile="vite")
    for check in sd.PROFILES["vite"].wiring_checks(app):
        assert check.detect(tmp_path)[0] == "ok", check.id
    assert sd.cmd_doctor(tmp_path, fix=True, framework_override="vite") == 0


def test_vite_autofix_rewrites_every_uncovered_match(tmp_path):
    # Two bare `env.X` reads: the substitution walks matches in reverse so the
    # earlier match's offsets stay valid after the later one is spliced.
    (tmp_path / "vite.config.ts").write_text("""\
import { defineConfig, loadEnv } from "vite";
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, import.meta.dirname, "");
  const a = Number(env.WEB_DEV_PORT ?? 1);
  const b = Number(env.API_DEV_PORT ?? 2);
  return { server: { port: a, proxy: { "/api": `http://localhost:${b}` } } };
});
""")
    app = sd.AppInventory(name="web", path=tmp_path, profile="vite")
    check = next(
        c for c in sd.PROFILES["vite"].wiring_checks(app) if c.id == "vite-config-process-env"
    )
    check.autofix(tmp_path)
    text = (tmp_path / "vite.config.ts").read_text()
    assert "const a = Number(process.env.WEB_DEV_PORT ?? 1);" in text
    assert "const b = Number(process.env.API_DEV_PORT ?? 2);" in text


def test_vite_wiring_check_idempotent(tmp_path):
    (tmp_path / "vite.config.ts").write_text(
        "export default { server: { port: Number(process.env.WEB_DEV_PORT ?? 5173) } };\n"
    )
    app = sd.AppInventory(name="web", path=tmp_path, profile="vite")
    check = next(
        c for c in sd.PROFILES["vite"].wiring_checks(app) if c.id == "vite-config-process-env"
    )
    status, _ = check.detect(tmp_path)
    assert status == "ok"


def test_loader_registry_exists_with_mise():
    assert isinstance(sd.LOADERS, dict)
    assert "mise" in sd.LOADERS
    assert isinstance(sd.LOADERS["mise"], sd.Loader)


def test_mise_loader_wire_creates_mise_toml(tmp_path):
    sd.LOADERS["mise"].wire(tmp_path)
    assert (tmp_path / "mise.toml").exists()
    assert "splashdown.env" in (tmp_path / "mise.toml").read_text()


def test_mise_loader_wire_is_idempotent(tmp_path):
    sd.LOADERS["mise"].wire(tmp_path)
    first = (tmp_path / "mise.toml").read_text()
    sd.LOADERS["mise"].wire(tmp_path)
    assert (tmp_path / "mise.toml").read_text() == first


_TWO_VARIANT_RECIPE = (
    '[targets.simulator.default]\nmodel = "iPhone 17"\n'
    '[targets.simulator.large-screen]\nmodel = "iPhone 17 Pro Max"\n'
)


def test_resolve_variant_for_cli_prefix_resolves(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / sd.RECIPE_NAME).write_text(_TWO_VARIANT_RECIPE)
    variant, spec, _ = sd.target_commands._resolve_variant_for_cli(tmp_path, "simulator", "lar")
    assert variant == "large-screen"
    assert spec["model"] == "iPhone 17 Pro Max"


def test_resolve_variant_for_cli_prefix_disabled_errors(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg" / "splashdown"
    cfg.mkdir(parents=True)
    (cfg / "config.toml").write_text("[settings]\nprefix_match = false\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / sd.RECIPE_NAME).write_text(_TWO_VARIANT_RECIPE)
    with pytest.raises(sd.DeviceError, match="no variant `lar`"):
        sd.target_commands._resolve_variant_for_cli(tmp_path, "simulator", "lar")


def test_resolve_variant_for_cli_short_variant_prefix_resolves(tmp_path):
    # `splash run d` in a sim-only project: `d` stays a variant token (not the
    # `device` type) and resolves the `default` variant by prefix.
    (tmp_path / sd.RECIPE_NAME).write_text('[targets.simulator.default]\nmodel = "iPhone 17"\n')
    variant, _, _ = sd.target_commands._resolve_variant_for_cli(tmp_path, "simulator", "d")
    assert variant == "default"


def test_declared_target_types_lists_declared(tmp_path):
    (tmp_path / sd.RECIPE_NAME).write_text(
        '[targets.simulator.default]\nmodel = "iPhone 17"\n[targets.emulator.default]\n'
    )
    assert sorted(sd.target_commands._declared_target_types(tmp_path)) == [
        "emulator",
        "simulator",
    ]


def test_deinit_round_trips_init(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _git_init(tmp_path)
    sd.cmd_init(tmp_path, preset="minimal")
    assert (tmp_path / "splashdown.toml").exists()
    hook = tmp_path / ".git" / "hooks" / "post-checkout"
    assert hook.exists()
    rc = sd.main(["--cwd", str(tmp_path), "deinit"])
    assert rc == 0
    assert not (tmp_path / "splashdown.toml").exists()
    assert not (tmp_path / "splashdown.local.toml").exists()
    assert not (tmp_path / "mise.toml").exists()
    assert hook.exists()
    # init (minimal preset) deterministically creates .gitignore with the two
    # managed lines; deinit must strip them but keep the file.
    gi = tmp_path / ".gitignore"
    assert gi.exists()
    assert "splashdown.env" not in gi.read_text()


def test_deinit_deletes_generated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    sd.cmd_init(tmp_path, preset="minimal")
    (tmp_path / "splashdown.env").write_text("FOO=1\n")
    sd.main(["--cwd", str(tmp_path), "deinit"])
    assert not (tmp_path / "splashdown.env").exists()


def test_deinit_clears_registry_rows(tmp_path, registry):
    co = tmp_path / "co"
    co.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    registry.allocate_port(str(co), "METRO", 18081, 18100)
    registry.set_kv(str(co), "ID", "abc")
    registry.allocate_port(str(other), "METRO", 18081, 18100)
    (co / "splashdown.toml").write_text('[project]\nloader = "none"\n')
    sd.cmd_deinit(co, registry)
    assert registry.all_for(str(co)) == {}
    assert registry.all_for(str(other)) != {}


def test_deinit_claim_cleanup_releases_owned_claims_and_addressed_notices(
    tmp_path, registry, capsys
):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / sd.RECIPE_NAME).write_text('[project]\nloader = "none"\n')
    owner = str(checkout.resolve())
    registry.attempt_claim(
        sd.PhysicalClaim(
            "recipe:/repo:device:pixel",
            "android",
            "PXL1234",
            "pixel",
            owner,
            "2026-08-26T09:00:00+00:00",
        )
    )
    registry.add_claim_notices(
        [
            sd.ClaimNotice(
                owner,
                "recipe:/repo:device:iphone",
                "iphone",
                "release",
                "/checkouts/releaser",
                "2026-08-26T10:00:00+00:00",
                "2099-09-25T10:00:00+00:00",
            )
        ]
    )

    assert sd.cmd_deinit(checkout, registry) == 0

    assert registry.all_claims() == ()
    assert registry.consume_claim_notices(owner) == ()
    assert "released 2 registry entries" in capsys.readouterr().err


def test_gc_claim_cleanup_counts_dead_claims_and_dead_or_expired_notices(
    tmp_path, registry, capsys
):
    dead_owner = tmp_path / "dead-owner"
    live_owner = tmp_path / "live-owner"
    dead_owner.mkdir()
    live_owner.mkdir()
    registry.attempt_claim(
        sd.PhysicalClaim(
            "recipe:/repo:device:pixel",
            "android",
            "PXL1234",
            "pixel",
            str(dead_owner),
            "2026-08-26T09:00:00+00:00",
        )
    )
    registry.add_claim_notices(
        [
            sd.ClaimNotice(
                str(dead_owner),
                "recipe:/repo:device:pixel",
                "pixel",
                "transfer",
                str(live_owner),
                "2026-08-26T10:00:00+00:00",
                "2099-09-25T10:00:00+00:00",
            ),
            sd.ClaimNotice(
                str(live_owner),
                "recipe:/repo:device:iphone",
                "iphone",
                "release",
                str(live_owner),
                "2000-01-01T00:00:00+00:00",
                "2000-01-31T00:00:00+00:00",
            ),
            sd.ClaimNotice(
                str(live_owner),
                "recipe:/repo:device:tablet",
                "tablet",
                "transfer",
                str(live_owner),
                "2026-08-26T10:05:00+00:00",
                "2099-09-25T10:05:00+00:00",
            ),
        ]
    )
    dead_owner.rmdir()

    assert sd.target_commands.cmd_gc(registry) == 0

    assert registry.all_claims() == ()
    remaining = registry.consume_claim_notices(str(live_owner))
    assert [notice.target_label for notice in remaining] == ["tablet"]
    assert "gc: removed 3 registry entries" in capsys.readouterr().err


def test_status_claim_summary_includes_checkout_known_only_by_ownership(tmp_path, registry, capsys):
    registry.attempt_claim(
        sd.PhysicalClaim(
            "recipe:/repo:device:pixel",
            "android",
            "PXL1234",
            "pixel",
            str(tmp_path.resolve()),
            "2026-08-26T09:00:00+00:00",
        )
    )

    assert sd.cmd_status(tmp_path, registry, "text", show_all=True) == 0

    err = capsys.readouterr().err
    assert str(tmp_path.resolve()) in err
    assert "1 claim" in err


def test_status_all_check_verbose_counts_defunct_physical_claim(tmp_path, registry, capsys):
    owner = tmp_path / "defunct-owner"
    owner.mkdir()
    registry.attempt_claim(
        sd.PhysicalClaim(
            "recipe:/repo:device:pixel",
            "android",
            "PXL1234",
            "pixel",
            str(owner.resolve()),
            "2026-08-26T09:00:00+00:00",
        )
    )
    owner.rmdir()

    assert (
        sd.cmd_status(
            tmp_path,
            registry,
            "text",
            show_all=True,
            check=True,
            verbose=True,
        )
        == 0
    )

    err = capsys.readouterr().err
    assert str(owner.resolve()) in err
    assert "1 defunct checkout (1 registry row)." in err


def test_status_all_check_json_counts_defunct_physical_claim(tmp_path, registry, capsys):
    owner = tmp_path / "defunct-owner"
    owner.mkdir()
    registry.attempt_claim(
        sd.PhysicalClaim(
            "recipe:/repo:device:pixel",
            "android",
            "PXL1234",
            "pixel",
            str(owner.resolve()),
            "2026-08-26T09:00:00+00:00",
        )
    )
    owner.rmdir()

    assert (
        sd.cmd_status(
            tmp_path,
            registry,
            "json",
            show_all=True,
            check=True,
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["defunct_checkouts"] == 1
    assert payload["summary"]["defunct_rows"] == 1
    assert payload["checkouts"] == [
        {
            "checkout": str(owner.resolve()),
            "exists": False,
            "resources": [],
            "targets": [],
        }
    ]


def test_deinit_destroys_simulator_by_udid(tmp_path, registry, monkeypatch):
    co = tmp_path / "co"
    co.mkdir()
    (co / "splashdown.toml").write_text('[project]\nloader = "none"\n')
    # The simulator row stores the real UDID in the udid column; teardown must
    # destroy by that UDID, not pass it to a by-name lookup that finds nothing.
    registry.set_device(str(co), "simulator", "default", "ABCD-UDID", "iPhone 17", "18.0")
    destroyed = []
    monkeypatch.setattr(sd.devices, "_ios_udid_exists", lambda u: True)
    monkeypatch.setattr(sd.devices, "ios_shutdown", lambda u: destroyed.append(("shutdown", u)))
    monkeypatch.setattr(sd.devices, "ios_destroy", lambda u: destroyed.append(("destroy", u)))
    sd.cmd_deinit(co, registry)
    assert ("destroy", "ABCD-UDID") in destroyed
    assert registry.devices_for(str(co)) == []


def test_deinit_destroys_emulator_by_name(tmp_path, registry, monkeypatch):
    co = tmp_path / "co"
    co.mkdir()
    (co / "splashdown.toml").write_text('[project]\nloader = "none"\n')
    # The emulator row stores the AVD name in the udid column.
    registry.set_device(str(co), "emulator", "default", "pixel_avd", "pixel_9", "android-34")
    destroyed = []
    monkeypatch.setattr(sd.devices, "_android_avd_exists", lambda n: True)
    monkeypatch.setattr(sd.devices, "android_shutdown", lambda n: destroyed.append(("shutdown", n)))
    monkeypatch.setattr(sd.devices, "android_destroy", lambda n: destroyed.append(("destroy", n)))
    sd.cmd_deinit(co, registry)
    assert ("destroy", "pixel_avd") in destroyed
    assert registry.devices_for(str(co)) == []


def test_deinit_continues_when_device_destroy_fails(tmp_path, registry, monkeypatch):
    co = tmp_path / "co"
    co.mkdir()
    (co / "splashdown.toml").write_text('[project]\nloader = "none"\n')
    (co / "splashdown.env").write_text("X=1\n")
    registry.set_device(str(co), "simulator", "default", "ABCD-UDID", "iPhone 17", "18.0")

    def boom(_u):
        raise sd.DeviceError("simctl exploded")

    monkeypatch.setattr(sd.devices, "ios_shutdown", boom)
    monkeypatch.setattr(sd.devices, "ios_destroy", boom)
    rc = sd.cmd_deinit(co, registry)
    assert rc == 0
    assert not (co / "splashdown.env").exists()
    assert registry.devices_for(str(co)) == []


def test_deinit_proceeds_on_unparseable_recipe(tmp_path, registry):
    co = tmp_path / "co"
    co.mkdir()
    # A broken/legacy recipe must not abort the one command meant to clean it up.
    (co / "splashdown.toml").write_text("this is not = valid toml ===\n")
    (co / "splashdown.env").write_text("X=1\n")
    rc = sd.cmd_deinit(co, registry)
    assert rc == 0
    assert not (co / "splashdown.env").exists()
    assert not (co / "splashdown.toml").exists()


def test_deinit_destroys_devices(tmp_path, registry, monkeypatch):
    co = tmp_path / "co"
    co.mkdir()
    (co / "splashdown.toml").write_text('[project]\nloader = "none"\n')
    registry.set_device(str(co), "simulator", "default", "ABCD-UDID", "iPhone 17", "18.0")
    monkeypatch.setattr(sd.devices, "ios_shutdown", lambda u: None)
    monkeypatch.setattr(sd.devices, "ios_destroy", lambda u: None)
    sd.cmd_deinit(co, registry)
    assert registry.devices_for(str(co)) == []


_ENVFILE_RECIPE = (
    '[project]\nloader = "none"\n\n'
    '[resources.WEB_DEV_PORT]\ntype = "port"\nrange = [5174, 5200]\n'
    'writer = "envfile=apps/web/.env"\n'
)


def test_deinit_strips_splashdown_keys_from_envfile_writer(tmp_path, registry):
    co = tmp_path / "co"
    co.mkdir()
    (co / "splashdown.toml").write_text(_ENVFILE_RECIPE)
    envf = co / "apps" / "web" / ".env"
    envf.parent.mkdir(parents=True)
    envf.write_text("USER_KEY=keep\nWEB_DEV_PORT=5174\n")
    sd.cmd_deinit(co, registry)
    text = envf.read_text()
    assert "WEB_DEV_PORT" not in text
    assert "USER_KEY=keep" in text


def test_deinit_removes_envfile_when_only_splashdown_keys(tmp_path, registry):
    co = tmp_path / "co"
    co.mkdir()
    (co / "splashdown.toml").write_text(_ENVFILE_RECIPE)
    envf = co / "apps" / "web" / ".env"
    envf.parent.mkdir(parents=True)
    envf.write_text("WEB_DEV_PORT=5174\n")
    sd.cmd_deinit(co, registry)
    assert not envf.exists()


def test_deinit_strips_splashdown_keys_from_envrc_writer(tmp_path, registry):
    co = tmp_path / "co"
    co.mkdir()
    (co / "splashdown.toml").write_text(
        '[project]\nloader = "none"\n\n'
        '[resources.API_PORT]\ntype = "port"\nrange = [9000, 9100]\n'
        'writer = "envrc"\n'
    )
    envrc = co / ".envrc.local"
    envrc.write_text("export USER_VAR=keep\nexport API_PORT=9000\n")
    sd.cmd_deinit(co, registry)
    text = envrc.read_text()
    assert "API_PORT" not in text
    assert "export USER_VAR=keep" in text


def test_deinit_keeps_modified_local(tmp_path, registry):
    co = tmp_path / "co"
    co.mkdir()
    (co / "splashdown.toml").write_text('[project]\nloader = "none"\n')
    (co / "splashdown.local.toml").write_text('[targets.simulator.mine]\nmodel = "iPhone 16"\n')
    sd.cmd_deinit(co, registry)
    assert not (co / "splashdown.toml").exists()
    assert (co / "splashdown.local.toml").exists()


def test_deinit_loader_none_is_noop(tmp_path, registry):
    co = tmp_path / "co"
    co.mkdir()
    (co / "splashdown.toml").write_text('[project]\nloader = "none"\n')
    sd.cmd_deinit(co, registry)
    assert not (co / "splashdown.toml").exists()
