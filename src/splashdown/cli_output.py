from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import ApplicationError

if TYPE_CHECKING:
    from .device_claims import PhysicalSelection
    from .device_types import ClaimNotice
    from .provisioning import WriterResult
    from .status import (
        AutomationStatus,
        CheckoutStatus,
        ClaimListRow,
        StatusReport,
        StatusSummary,
        TargetInventoryRow,
    )

_SUMMARY_PARTS = (
    ("port", "port", "ports"),
    ("kv", "var", "vars"),
    ("simulator", "sim", "sims"),
    ("emulator", "emu", "emus"),
    ("claim", "claim", "claims"),
)


def _short_path(abspath: str) -> str:
    from pathlib import Path  # noqa: PLC0415

    home = str(Path.home())
    if abspath == home:
        return "~"
    if abspath.startswith(home + "/"):
        return "~" + abspath[len(home) :]
    return abspath


def render_claim_notices(notices: Sequence[ClaimNotice]) -> None:
    for notice in notices:
        action = "claimed" if notice.action == "transfer" else "force-released"
        print(
            f"warning: physical target {notice.target_label} was {action} by "
            f"{notice.actor_checkout} at {notice.event_at}; this checkout no longer owns it",
            file=sys.stderr,
        )


def render_claim_selection(selection: PhysicalSelection, fmt: str, *, available: bool) -> None:
    payload = {
        "target": selection.target.variant,
        "source": selection.target.source,
        "platform": selection.destination.platform,
        "hardware_id": selection.destination.identifier or "",
        "owner": selection.claim.owner_checkout,
        "claimed_at": selection.claim.claimed_at,
        "status": selection.status,
    }
    if fmt == "json":
        print(json.dumps(payload, indent=2))
    elif available:
        print(selection.target.variant)
    else:
        print(
            f"claimed {selection.target.variant} ({selection.destination.platform} "
            f"{selection.destination.identifier}) for {selection.claim.owner_checkout}",
            file=sys.stderr,
        )


def render_claim_rows(rows: Sequence[ClaimListRow], fmt: str) -> None:
    if fmt == "json":
        print(json.dumps([asdict(row) for row in rows], indent=2))
        return
    print("TARGET\tSOURCE\tPLATFORM\tHARDWARE ID\tOWNER\tCLAIMED AT")
    for row in rows:
        print(
            f"{row.target}\t{row.source}\t{row.platform}\t{row.hardware_id}\t"
            f"{row.owner}\t{row.claimed_at}"
        )


def render_target_inventory(rows: Sequence[TargetInventoryRow], fmt: str) -> None:
    if fmt == "json":
        print(json.dumps([asdict(row) for row in rows], indent=2))
        return
    print("TARGET\tSOURCE\tPLATFORM\tCONNECTION\tCLAIM\tOWNER")
    for row in rows:
        owner = Path(row.owner).name if row.owner else ""
        print(
            f"{row.variant}\t{row.source}\t{row.platform}\t{row.connection}\t{row.claim}\t{owner}"
        )


def _summary_string(counts: dict[str, int]) -> str:
    parts: list[str] = []
    for key, singular, plural in _SUMMARY_PARTS:
        count = counts.get(key, 0)
        if count == 1:
            parts.append(f"1 {singular}")
        elif count > 1:
            parts.append(f"{count} {plural}")
    return ", ".join(parts) if parts else "—"


def _checkout_payload(checkout: CheckoutStatus, *, show_values: bool) -> dict[str, object]:
    resources: list[dict[str, object]] = []
    for resource in checkout.resources:
        item: dict[str, object] = {
            "key": resource.key,
            "port_state": resource.port_state,
        }
        if show_values:
            item["value"] = resource.value
        resources.append(item)
    return {
        "checkout": checkout.checkout,
        "exists": checkout.exists,
        "automation": asdict(checkout.automation) if checkout.automation is not None else None,
        "resources": resources,
        "targets": [asdict(target) for target in checkout.targets],
    }


def _render_automation(automation: AutomationStatus | None) -> None:
    print("automation:", file=sys.stderr)
    if automation is None:
        print("  unavailable (not a live Git checkout)", file=sys.stderr)
        return
    print(
        f"  sync trust: {'trusted' if automation.sync_trusted else 'untrusted'}",
        file=sys.stderr,
    )
    print(
        f"  bootstrap trust: {'trusted' if automation.bootstrap_trusted else 'untrusted'}",
        file=sys.stderr,
    )
    print(
        f"  recipe bootstrap: {'declared' if automation.bootstrap_declared else 'not declared'}",
        file=sys.stderr,
    )
    print(
        f"  completion: {automation.bootstrap_completion.replace('-', ' ')}",
        file=sys.stderr,
    )


def _render_status_block(checkout: CheckoutStatus, *, show_all: bool, show_values: bool) -> None:
    header_tag = "  [defunct]" if not checkout.exists else ""
    if show_all:
        print(f"=== {checkout.checkout}{header_tag} ===", file=sys.stderr)
    else:
        print(f"checkout: {checkout.checkout}{header_tag}", file=sys.stderr)
    print("resources:", file=sys.stderr)
    if not checkout.resources:
        print("  (none)", file=sys.stderr)
    for resource in checkout.resources:
        suffix = f"  [{resource.port_state}]" if resource.port_state else ""
        label = f"{resource.key}={resource.value}" if show_values else resource.key
        print(f"  {label}{suffix}", file=sys.stderr)
    print("targets:", file=sys.stderr)
    if not checkout.targets:
        print("  (none)", file=sys.stderr)
    for target in checkout.targets:
        columns = [f"{target.type}.{target.variant}"]
        if target.source:
            columns.append(target.source)
        columns.extend((target.device_name, target.status))
        if target.orphan:
            columns.append("[orphan]")
        elif target.stale:
            columns.append("[stale]")
        elif target.undeclared:
            columns.append("[undeclared]")
        elif target.missing:
            columns.append("[missing]")
        print("  " + "\t".join(columns), file=sys.stderr)
    _render_automation(checkout.automation)
    if show_all:
        print("", file=sys.stderr)


def _render_status_table(report: StatusReport) -> None:
    rendered = [
        (_short_path(row.checkout), _summary_string(row.counts), row.status) for row in report.rows
    ]
    path_width = max((len(path) for path, _summary, _status in rendered), default=4)
    path_width = max(path_width, len("PATH"))
    summary_width = max((len(summary) for _path, summary, _status in rendered), default=7)
    summary_width = max(summary_width, len("SUMMARY"))
    if any(status for _path, _summary, status in rendered):
        row_format = f"{{:<{path_width}}}  {{:<{summary_width}}}  {{}}"
        print(row_format.format("PATH", "SUMMARY", "ISSUE").rstrip(), file=sys.stderr)
        for path, summary, status in rendered:
            print(row_format.format(path, summary, status).rstrip(), file=sys.stderr)
    else:
        row_format = f"{{:<{path_width}}}  {{}}"
        print(row_format.format("PATH", "SUMMARY").rstrip(), file=sys.stderr)
        for path, summary, _status in rendered:
            print(row_format.format(path, summary).rstrip(), file=sys.stderr)


def _render_check_summary(summary: StatusSummary) -> None:
    if not any(
        (
            summary.defunct_checkouts,
            summary.orphan_devices,
            summary.stale_devices,
            summary.undeclared_devices,
            summary.missing_devices,
            summary.missing_hardware,
        )
    ):
        print("Summary: all entries verified.", file=sys.stderr)
        return
    print("Summary:", file=sys.stderr)
    if summary.defunct_checkouts:
        count = summary.defunct_checkouts
        rows = summary.defunct_rows
        print(
            f"  {count} defunct checkout{'s' if count != 1 else ''} "
            f"({rows} registry row{'s' if rows != 1 else ''}).",
            file=sys.stderr,
        )
    if summary.orphan_devices:
        count = summary.orphan_devices
        print(
            f"  {count} orphan device{'s' if count != 1 else ''} (underlying sim/AVD deleted).",
            file=sys.stderr,
        )
    if summary.stale_devices:
        count = summary.stale_devices
        print(
            f"  {count} stale device{'s' if count != 1 else ''} (declared target drifted).",
            file=sys.stderr,
        )
    if summary.undeclared_devices:
        count = summary.undeclared_devices
        print(
            f"  {count} undeclared device row{'s' if count != 1 else ''}.",
            file=sys.stderr,
        )
    if summary.missing_devices:
        count = summary.missing_devices
        print(
            f"  {count} missing device{'s' if count != 1 else ''} (declared but not yet created).",
            file=sys.stderr,
        )
    if summary.missing_hardware:
        count = summary.missing_hardware
        print(
            f"  {count} unplugged physical device{'s' if count != 1 else ''} "
            "(declared but not connected).",
            file=sys.stderr,
        )
    if summary.defunct_checkouts:
        print("  Run `splash gc` to drop dead checkouts.", file=sys.stderr)
    if summary.orphan_devices or summary.stale_devices or summary.undeclared_devices:
        print("  Run `splash target refresh` to reconcile.", file=sys.stderr)
    if summary.missing_devices:
        print("  Run `splash run` to provision.", file=sys.stderr)
    if summary.missing_hardware:
        print(
            "  Connect the device (check pairing/USB) — splashdown can't create hardware.",
            file=sys.stderr,
        )


def render_status(
    report: StatusReport,
    fmt: str,
    *,
    verbose: bool = False,
    show_values: bool = False,
) -> None:
    for warning in report.warnings:
        print(warning, file=sys.stderr)
    if fmt == "json":
        checkout_payloads = [
            _checkout_payload(checkout, show_values=show_values) for checkout in report.checkouts
        ]
        payload: dict[str, object] = (
            {"checkouts": checkout_payloads} if report.show_all else checkout_payloads[0]
        )
        if report.check:
            payload["summary"] = asdict(report.summary)
        print(json.dumps(payload, indent=2))
        return
    if report.show_all and not verbose:
        _render_status_table(report)
    else:
        for checkout in report.checkouts:
            _render_status_block(
                checkout,
                show_all=report.show_all,
                show_values=show_values,
            )
    if report.check:
        if report.show_all and not verbose:
            print("", file=sys.stderr)
        _render_check_summary(report.summary)
    elif not report.show_all:
        if report.stale_registry_rows:
            print(
                f"stale registry rows: {report.stale_registry_rows} (run `splash gc` to clean)",
                file=sys.stderr,
            )
        if report.unfilled_resources:
            names = ", ".join(report.unfilled_resources)
            print(
                f"{len(report.unfilled_resources)} resource(s) need a value ({names}): "
                "run `splash env set NAME=VALUE`",
                file=sys.stderr,
            )


def render_env_list(
    values: dict[str, str], target: str, fmt: str, *, show_values: bool = False
) -> None:
    if fmt == "json":
        payload: object = values if show_values else sorted(values)
        print(json.dumps(payload, indent=2))
        return
    if not values:
        print(f"(empty) {target}", file=sys.stderr)
    for key, value in sorted(values.items()):
        print(f"{key}={value}" if show_values else key)


def render_sync(
    resolved: dict[str, str],
    writer_results: list[WriterResult],
    setup_messages: list[str],
    changed_keys: list[str],
    fmt: str,
    *,
    show_values: bool = False,
) -> None:
    changed = (
        bool(changed_keys)
        or any(result.changed for result in writer_results)
        or bool(setup_messages)
    )
    stdout_values = {
        key: value for result in writer_results for key, value in result.stdout_values.items()
    }
    if fmt == "json":
        payload: dict[str, object] = {
            "writers": [result.message for result in writer_results],
            "stdout": stdout_values,
            "setup": setup_messages,
            "changed": changed,
            "changed_keys": sorted(changed_keys),
        }
        if show_values:
            payload["resolved"] = resolved
        else:
            payload["resolved_keys"] = sorted(resolved)
        print(json.dumps(payload, indent=2))
        return

    for key, value in stdout_values.items():
        print(f"{key}={value}")
    if not changed:
        files = sum(1 for result in writer_results if result.writer not in ("stdout", "none"))
        print(
            f"splashdown: up to date ({len(resolved)} vars, {files} files)",
            file=sys.stderr,
        )
        return
    for key in changed_keys:
        print(f"  {key} (changed)", file=sys.stderr)
    for result in writer_results:
        if result.changed:
            print(f"  -> {result.message} (changed)", file=sys.stderr)
    for message in setup_messages:
        print(f"  -> {message}", file=sys.stderr)


def render_application_error(error: ApplicationError) -> int:
    prefix = "error: " if error.is_error else ""
    print(f"{prefix}{error}", file=sys.stderr)
    return error.exit_code


def render_untyped_error(error: Exception) -> int:
    print(f"error: {error}", file=sys.stderr)
    return 1
