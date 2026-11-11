"""Tests for physical-claim renderers."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import splashdown as sd
from splashdown import cli_output, device_android
from splashdown.errors import ApplicationError
from splashdown.results import CommandResult, Diagnostic


def _selection(owner: str, *, transport: str = "PXL1234") -> sd.PhysicalSelection:
    target = sd.ConfiguredPhysicalTarget("pixel", "recipe", "recipe:/repo:device:pixel", {})
    claim = sd.PhysicalClaim(
        target.catalog_identity,
        "android",
        "PXL1234",
        "pixel",
        owner,
        "2026-08-26T10:00:00+00:00",
    )
    return sd.PhysicalSelection(
        target,
        sd.AndroidDestination("Pixel", transport, owned=False),
        claim,
        "claimed",
        (),
    )


@pytest.mark.parametrize("transport", ["PXL1234", "192.0.2.10:5555"])
def test_render_claim_selection_text_separates_machine_value_and_diagnostic(capsys, transport):
    selection = _selection("/checkouts/current", transport=transport)

    sd.render_claim_selection(selection, "text", available=True)
    sd.render_claim_selection(selection, "text", available=False)

    captured = capsys.readouterr()
    assert captured.out == "pixel\n"
    assert captured.err == "claimed pixel (android PXL1234) for /checkouts/current\n"


@pytest.mark.parametrize("available", [False, True])
@pytest.mark.parametrize("transport", ["PXL1234", "192.0.2.10:5555"])
def test_render_claim_selection_json_has_claim_contract(capsys, available, transport):
    sd.render_claim_selection(
        _selection("/checkouts/current", transport=transport), "json", available=available
    )

    assert json.loads(capsys.readouterr().out) == {
        "target": "pixel",
        "source": "recipe",
        "platform": "android",
        "hardware_id": "PXL1234",
        "owner": "/checkouts/current",
        "claimed_at": "2026-08-26T10:00:00+00:00",
        "status": "claimed",
    }


def test_render_claim_rows_text_and_json_keep_canonical_owner(capsys):
    rows = (
        sd.status.ClaimListRow(
            "pixel",
            "recipe",
            "android",
            "PXL1234",
            "/checkouts/current",
            "2026-08-26T10:00:00+00:00",
        ),
    )

    sd.render_claim_rows(rows, "text")
    text = capsys.readouterr().out
    sd.render_claim_rows(rows, "json")

    assert text == (
        "TARGET\tSOURCE\tPLATFORM\tHARDWARE ID\tOWNER\tCLAIMED AT\n"
        "pixel\trecipe\tandroid\tPXL1234\t/checkouts/current\t2026-08-26T10:00:00+00:00\n"
    )
    assert json.loads(capsys.readouterr().out)[0]["owner"] == "/checkouts/current"


def test_render_target_inventory_uses_concise_text_owner_and_canonical_json_owner(capsys):
    rows = (
        sd.status.TargetInventoryRow(
            "device",
            "pixel",
            "recipe",
            "Pixel 7",
            "android",
            "connected",
            "claimed",
            "/checkouts/app.feature-pixel",
            connection_type="network",
        ),
        sd.status.TargetInventoryRow(
            "simulator",
            "default",
            "recipe",
            "",
            "ios",
            "absent",
            "not-applicable",
            "",
        ),
    )

    sd.render_target_inventory(rows, "text")
    text = capsys.readouterr().out
    sd.render_target_inventory(rows, "json")

    assert text == (
        "TARGET   SOURCE  PLATFORM  CONNECTION  CLAIM           OWNER\n"
        "pixel    recipe  android   Wi-Fi/LAN   claimed         /checkouts/app.feature-pixel\n"
        "default  recipe  ios       absent      not-applicable\n"
    )
    assert json.loads(capsys.readouterr().out) == [
        {
            "type": "device",
            "variant": "pixel",
            "source": "recipe",
            "device_name": "Pixel 7",
            "platform": "android",
            "connection": "connected",
            "claim": "claimed",
            "owner": "/checkouts/app.feature-pixel",
            "connection_type": "network",
        },
        {
            "type": "simulator",
            "variant": "default",
            "source": "recipe",
            "device_name": "",
            "platform": "ios",
            "connection": "absent",
            "claim": "not-applicable",
            "owner": "",
            "connection_type": None,
        },
    ]


@pytest.mark.parametrize(
    "connection,connection_type,expected",
    [
        ("connected", "usb", "USB"),
        ("connected", "network", "Wi-Fi/LAN"),
        ("connected", None, "connected"),
        ("connected", "future", "connected"),
        ("disconnected", "usb", "disconnected"),
        ("unavailable", "network", "unavailable"),
    ],
)
def test_target_inventory_connection_label_preserves_unavailable_state(
    capsys, connection, connection_type, expected
):
    row = sd.status.TargetInventoryRow(
        "device",
        "phone",
        "global",
        "Phone",
        "ios",
        connection,
        "free",
        "",
        connection_type=connection_type,
    )
    sd.render_target_inventory([row], "text")
    assert capsys.readouterr().out.splitlines()[1].split() == [
        "phone",
        "global",
        "ios",
        expected,
        "free",
    ]


def test_render_target_inventory_shortens_home_in_text_owner(capsys):
    owner = str(Path.home() / "wrksp" / "splashdown" / "dev1")
    rows = (
        sd.status.TargetInventoryRow(
            "device",
            "iphone",
            "global",
            "iPhone",
            "ios",
            "connected",
            "claimed",
            owner,
        ),
    )

    sd.render_target_inventory(rows, "text")

    assert capsys.readouterr().out.endswith("~/wrksp/splashdown/dev1\n")


def test_render_claim_notices_names_action_target_actor_and_event(capsys):
    notices = (
        sd.ClaimNotice(
            "/checkouts/old-owner",
            "recipe:/repo:device:pixel",
            "pixel",
            "transfer",
            "/checkouts/new-owner",
            "2026-08-26T10:00:00+00:00",
            "2026-09-25T10:00:00+00:00",
        ),
        sd.ClaimNotice(
            "/checkouts/old-owner",
            "recipe:/repo:device:iphone",
            "iphone",
            "release",
            "/checkouts/releaser",
            "2026-08-26T10:05:00+00:00",
            "2026-09-25T10:05:00+00:00",
        ),
    )

    sd.render_claim_notices(notices)

    assert capsys.readouterr().err == (
        "warning: physical target pixel was claimed by /checkouts/new-owner "
        "at 2026-08-26T10:00:00+00:00; this checkout no longer owns it\n"
        "warning: physical target iphone was force-released by /checkouts/releaser "
        "at 2026-08-26T10:05:00+00:00; this checkout no longer owns it\n"
    )


@pytest.mark.parametrize("renderer", ["untyped", "application", "result"])
def test_android_verification_errors_escape_terminal_controls(monkeypatch, capsys, renderer):
    monkeypatch.setattr(device_android, "_android_bin", lambda _name: "adb")

    def failed_probe(argv, **_kwargs):
        raise subprocess.CalledProcessError(
            1,
            argv,
            stderr=b"blocked \x1b]52;c;Y2xpcA==\x07\x1b[2J\rretry\n\tUnlock phone",
        )

    monkeypatch.setattr(device_android, "check_output_finite", failed_probe)
    with pytest.raises(sd.DeviceError) as captured:
        device_android.verify_android_connection(
            sd.AndroidDestination("Pixel", "192.0.2.10:5555", owned=False), "PXL1234"
        )
    error = captured.value
    if renderer == "untyped":
        status = cli_output.render_untyped_error(error)
    elif renderer == "application":
        status = cli_output.render_application_error(ApplicationError(str(error)))
    else:
        status = cli_output.emit_result(
            CommandResult("target claim", "error", 1, error=Diagnostic("device_error", str(error))),
            "text",
        )
    assert status == 1
    assert capsys.readouterr().err == (
        "error: could not verify the selected Android phone: adb could not read ro.serialno: "
        "blocked \\x1b]52;c;Y2xpcA==\\x07\\x1b[2J\\rretry\n\tUnlock phone. "
        "Reconnect the intended phone and rerun the command.\n"
    )


@pytest.mark.parametrize("renderer", ["application", "result"])
def test_structured_diagnostics_escape_controls_and_preserve_layout(capsys, renderer):
    warning = Diagnostic("device_warning", "First line\n\tSecond\x1b[2J")
    next_steps = ("splash run\rspoof",)
    if renderer == "application":
        status = cli_output.render_application_error(
            ApplicationError("Failed\x07", warnings=(warning,), next_steps=next_steps)
        )
    else:
        status = cli_output.emit_result(
            CommandResult(
                "target claim",
                "error",
                1,
                error=Diagnostic("device_error", "Failed\x07"),
                warnings=(warning,),
                next_steps=next_steps,
            ),
            "text",
        )
    assert status == 1
    lines = capsys.readouterr().err
    assert "error: Failed\\x07\n" in lines
    assert "warning: First line\n\tSecond\\x1b[2J\n" in lines
    assert "splash run\\rspoof\n" in lines


def test_json_diagnostics_preserve_original_message(capsys):
    message = "Device\x1b[2J\nOffline"
    assert (
        cli_output.emit_result(
            CommandResult("target claim", "error", 1, error=Diagnostic("device_error", message)),
            "json",
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["error"] == {
        "code": "device_error",
        "message": message,
    }
