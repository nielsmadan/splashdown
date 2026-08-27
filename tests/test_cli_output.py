"""Tests for physical-claim renderers."""

from __future__ import annotations

import json

import splashdown as sd


def _selection(owner: str) -> object:
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
        sd.AndroidDestination("Pixel", "PXL1234", owned=False),
        claim,
        "claimed",
        (),
    )


def test_render_claim_selection_text_separates_machine_value_and_diagnostic(capsys):
    selection = _selection("/checkouts/current")

    sd.render_claim_selection(selection, "text", available=True)
    sd.render_claim_selection(selection, "text", available=False)

    captured = capsys.readouterr()
    assert captured.out == "pixel\n"
    assert captured.err == "claimed pixel (android PXL1234) for /checkouts/current\n"


def test_render_claim_selection_json_has_claim_contract(capsys):
    sd.render_claim_selection(_selection("/checkouts/current"), "json", available=False)

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
        ),
    )

    sd.render_target_inventory(rows, "text")
    text = capsys.readouterr().out
    sd.render_target_inventory(rows, "json")

    assert text == (
        "TARGET\tSOURCE\tPLATFORM\tCONNECTION\tCLAIM\tOWNER\n"
        "pixel\trecipe\tandroid\tconnected\tclaimed\tapp.feature-pixel\n"
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
        }
    ]


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
