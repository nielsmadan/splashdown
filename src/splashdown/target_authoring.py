from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .constants import LOCAL_NAME
from .registry import Registry
from .results import CommandResult
from .targets import global_target_add, target_add, target_add_destination


def add_target(args: argparse.Namespace, cwd: Path) -> CommandResult:
    fields = {
        "model": args.model,
        "ios": args.ios,
        "device": args.device,
        "image": args.image,
        "name": args.sim_name,
        "id": args.device_id,
        "platform": args.platform,
    }
    variant = args.variant
    if args.dtype == "device":
        destination, names = target_add_destination(cwd, global_scope=args.global_scope)
        if variant is None:
            from .device_onboarding import onboard_device  # noqa: PLC0415

            declaration = onboard_device(args.platform, destination, names)
            if declaration is None:
                return CommandResult("target add", "success", 0, data={"saved": False})
            variant = declaration.variant
            fields = {"platform": declaration.platform, "id": declaration.hardware_id}
    if args.global_scope:
        destination = global_target_add(args.dtype, variant, fields)
    else:
        registry = Registry()
        with registry.operation_lock(str(cwd.resolve())):
            target_add(cwd, args.dtype, variant, fields)
        destination = cwd / LOCAL_NAME
    return CommandResult(
        "target add",
        "success",
        0,
        data={
            "saved": True,
            "type": args.dtype,
            "variant": variant,
            "path": str(destination),
            "fields": {key: value for key, value in fields.items() if value is not None},
        },
        next_steps=(f"splash run {args.dtype} {variant}",),
    )


def render_added_target(result: CommandResult) -> None:
    data = result.data
    if data is None:
        return
    if not data.get("saved"):
        print("Cancelled; no target saved.", file=sys.stderr)
        return
    print(f"Added target {data['type']}.{data['variant']} to {data['path']}", file=sys.stderr)
