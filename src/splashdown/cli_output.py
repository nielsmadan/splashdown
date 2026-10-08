from __future__ import annotations

import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict
from typing import TYPE_CHECKING

from .errors import ApplicationError
from .results import CommandResult, OutputFormat, result_payload

if TYPE_CHECKING:
    from .device_claims import PhysicalSelection
    from .device_types import ClaimNotice
    from .provisioning import WriterResult
    from .status import (
        ClaimListRow,
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
    headers = ("TARGET", "SOURCE", "PLATFORM", "CONNECTION", "CLAIM", "OWNER")
    rendered = [
        (
            row.variant,
            row.source,
            row.platform,
            row.connection,
            row.claim,
            _short_path(row.owner) if row.owner else "",
        )
        for row in rows
    ]
    widths = [
        max(len(headers[index]), *(len(values[index]) for values in rendered))
        for index in range(len(headers) - 1)
    ]
    row_format = "  ".join(f"{{:<{width}}}" for width in widths) + "  {}"
    print(row_format.format(*headers).rstrip())
    for values in rendered:
        print(row_format.format(*values).rstrip())


def _summary_string(counts: dict[str, int]) -> str:
    parts: list[str] = []
    for key, singular, plural in _SUMMARY_PARTS:
        count = counts.get(key, 0)
        if count == 1:
            parts.append(f"1 {singular}")
        elif count > 1:
            parts.append(f"{count} {plural}")
    return ", ".join(parts) if parts else "—"


def _render_status_rows(section: str, rows: list[object], *, verbose: bool) -> None:
    print(f"  {section}: {len(rows)}")
    for row in rows:
        if not isinstance(row, dict):
            continue
        if section == "resources":
            suffix = f" [{row['port_state']}]" if row.get("port_state") else ""
            print(f"    {row['key']}: {row['state']}{suffix}")
            owners = row.get("owners")
            if isinstance(owners, list):
                for owner in owners:
                    print(f"      pid {owner['pid']} ({owner['command']})")
        elif verbose:
            print(f"    {row['type']}.{row['variant']}: {row['device_name']} ({row['source']})")


def render_status_result(result: CommandResult, *, verbose: bool = False) -> None:
    if result.data is None:
        return
    checkouts = result.data.get("checkouts")
    if not isinstance(checkouts, list):
        return
    if not checkouts:
        print("No tracked checkouts.")
    for checkout in checkouts:
        if not isinstance(checkout, dict):
            continue
        print(f"checkout: {checkout['checkout']}")
        counts = checkout.get("counts")
        if isinstance(counts, dict):
            print("  registry: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
        for section in ("resources", "targets"):
            rows = checkout.get(section)
            if isinstance(rows, list):
                _render_status_rows(section, list(rows), verbose=verbose)
        checks = checkout.get("checks")
        if not isinstance(checks, list):
            continue
        for check in checks:
            if not isinstance(check, dict):
                continue
            print(f"  {check['state']}: {check['message']}")
            if verbose and check.get("path"):
                print(f"    {check['path']}")
            steps = check.get("next_steps")
            if isinstance(steps, list):
                for step in steps:
                    print(f"    {step}")


def render_env_result(result: CommandResult) -> None:
    if result.status != "success" or result.data is None:
        return
    if result.command == "env get":
        print(result.data["value"])
    else:
        values = result.data["values"]
        if isinstance(values, dict):
            render_env_list(
                {key: str(value) for key, value in values.items()},
                str(result.data["checkout"]),
                "text",
            )


def render_env_list(values: dict[str, str], target: str, fmt: str) -> None:
    if fmt == "json":
        payload: object = values
        print(json.dumps(payload, indent=2))
        return
    if not values:
        print(f"(empty) {target}")
    for key, value in sorted(values.items()):
        print(f"{key}={value}")


def render_sync(
    resolved: dict[str, str],
    writer_results: list[WriterResult],
    setup_messages: list[str],
    changed_keys: list[str],
    fmt: str,
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
    for warning in dict.fromkeys(error.warnings):
        print(f"warning: {warning.message}", file=sys.stderr)
    for step in dict.fromkeys(error.next_steps):
        print(step, file=sys.stderr)
    return error.exit_code


def render_untyped_error(error: Exception) -> int:
    print(f"error: {error}", file=sys.stderr)
    return 1


def emit_result(
    result: CommandResult,
    fmt: OutputFormat,
    *,
    render_text: Callable[[CommandResult], None] | None = None,
) -> int:
    payload = result_payload(result)
    if fmt == "json":
        print(json.dumps(payload))
    else:
        if render_text is not None:
            render_text(result)
        for warning in dict.fromkeys(result.warnings):
            print(f"warning: {warning.message}", file=sys.stderr)
        if result.error is not None:
            print(f"error: {result.error.message}", file=sys.stderr)
        for step in dict.fromkeys(result.next_steps):
            print(step, file=sys.stderr)
    return result.exit_code


def render_ai_result(result: CommandResult) -> None:
    if result.data is None:
        return
    files = result.data.get("files")
    if isinstance(files, list):
        for row in files:
            if isinstance(row, dict):
                detail = (
                    f"; {row['detail']}"
                    if row.get("detail") and row["status"] != "generated"
                    else ""
                )
                print(f"{row['file']}: {row['status']}{detail}")
    activation = result.data.get("activation")
    if activation:
        print(activation)
