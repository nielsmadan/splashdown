from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .registry import Registry
from .recipe import LocalConfig, Recipe, TemplateError, LOCAL_SKELETON
from .provisioning import provision, write_outputs, run_setup
from .devices import DeviceError, device_add, device_remove, device_destroy, device_run
from .wiring import cmd_doctor
from .commands import (
    cmd_status, cmd_devices_list, cmd_device_gc, cmd_device_prune,
    cmd_run, cmd_start, cmd_stop, cmd_destroy,
    cmd_init, cmd_refresh_inventory,
    _cmd_provision, _cmd_provision_inner, _cmd_refresh, _device_dispatch,
    _resolve_format_arg,
)
from . import __version__, DEVICE_TYPES, RECIPE_NAME, LOCAL_NAME, ENV_FILE_NAME


# ---------- CLI ----------

KNOWN_CMDS = {
    "provision", "init", "list", "get", "set", "release", "gc", "doctor",
    "status", "refresh", "refresh-inventory",
    "run", "start", "stop", "destroy",
    "devices", "device",
}


def _build_parser() -> argparse.ArgumentParser:
    from .profiles import SCAFFOLDS  # noqa: PLC0415
    parser = argparse.ArgumentParser(prog="splash", description="Per-checkout resource provisioner")
    parser.add_argument("--cwd", default=None, help="working directory (default: $PWD)")
    parser.add_argument("--format", choices=["text", "json"], default=None)
    parser.add_argument("--version", action="version", version=f"splashdown {__version__}")
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")

    p = sub.add_parser("provision", help="provision per splashdown.toml (default if no command)")
    p.add_argument("--reprovision", action="store_true", help="force re-allocate all resources")
    p.add_argument("--setup", help="also run a [setup.NAME] block from the recipe")

    p = sub.add_parser("status", help="show resolved vars, declared devices, and OS-level port collisions")
    p.add_argument("scope", nargs="?", choices=("local", "all"), default="local",
                   help="local (default): this checkout only. all: every tracked checkout.")
    p.add_argument("--check", action="store_true", help="revalidate liveness and print a cleanup hint")
    p.add_argument("--verbose", action="store_true", help="with `all`, expand each checkout into the per-block view")
    sub.add_parser("refresh", help="re-provision and reallocate any port an OS process has squatted on")

    p = sub.add_parser("init", help="scaffold a splashdown.toml")
    p.add_argument(
        "preset", nargs="?", default=None,
        choices=tuple(SCAFFOLDS),
        help="named scaffold (default: scan the project)",
    )
    p.add_argument("--loader", default=None, choices=("mise", "direnv", "devbox"), help="override loader auto-detection")
    p.add_argument("--force", action="store_true")

    sub.add_parser("refresh-inventory", help="re-scan and update [project]/[apps.*] in splashdown.toml")

    p = sub.add_parser("list", help="show this checkout's resolved vars")
    p.add_argument("--checkout", default=None)

    p = sub.add_parser("get", help="echo a single resolved value")
    p.add_argument("key")
    p.add_argument("--checkout", default=None)

    p = sub.add_parser("set", help="manually set a value (for type=\"set\" resources)")
    p.add_argument("assignment", metavar="KEY=VALUE")

    p = sub.add_parser("release", help="release this checkout's registry entries (or just KEY)")
    p.add_argument("key", nargs="?")

    sub.add_parser("gc", help="garbage-collect dead registry entries")

    p = sub.add_parser("doctor", help="check framework-aware wiring of this project")
    p.add_argument("--fix", action="store_true", help="apply safe autofixes; print manual instructions for the rest")
    p.add_argument("--framework", default=None, help="override framework detection (react-native|flutter|expo)")

    for verb, helptxt in (
        ("run", "start the device + build & launch the app on it"),
        ("start", "start the device (create-if-missing); don't build/launch"),
        ("stop", "shut down the device (preserves it for next start)"),
        ("destroy", "delete the device and its registry entry"),
    ):
        p = sub.add_parser(verb, help=helptxt)
        # dtype optional: if there's exactly one declared device type for this
        # checkout, that's what's used.
        p.add_argument("dtype", choices=DEVICE_TYPES, metavar="TYPE", nargs="?")
        p.add_argument("variant", nargs="?", help="variant name (defaults to `default`)")

    sub.add_parser("devices", help="show declared variants + instance state")

    dev = sub.add_parser("device", help="manage device variants (add/remove/gc/refresh/prune)")
    devsub = dev.add_subparsers(dest="device_cmd", metavar="ACTION")

    devsub.add_parser("gc", help="prune splashdown-managed sims for defunct checkouts")
    ref = devsub.add_parser("refresh", help="destroy + recreate stale/missing sims & AVDs to latest (no boot)")
    ref.add_argument(
        "platform", nargs="?", default="all", choices=("ios", "android", "all"),
        help="scope (default: all = both)",
    )

    prune = devsub.add_parser("prune", help="destroy every sim/AVD splashdown did NOT create")
    prune.add_argument(
        "platform", nargs="?", default="all", choices=("ios", "android", "all"),
        help="scope (default: all = both)",
    )
    prune.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    prune.add_argument("--dry-run", action="store_true", dest="dry_run", help="list without deleting")

    add = devsub.add_parser("add", help="declare a variant in splashdown.local.toml")
    add.add_argument("dtype", choices=DEVICE_TYPES, metavar="TYPE")
    add.add_argument("variant", help="variant name (e.g. `default`, `small-screen`)")
    add.add_argument("--model")
    add.add_argument("--ios")
    add.add_argument("--device")
    add.add_argument("--image")
    add.add_argument("--name", dest="sim_name", help="simulator/emulator name override")

    rm = devsub.add_parser("remove", help="remove a variant from splashdown.local.toml (and destroy its sim)")
    rm.add_argument("dtype", choices=DEVICE_TYPES, metavar="TYPE")
    rm.add_argument("variant")
    rm.add_argument(
        "--keep-instance", action="store_true", dest="keep_instance",
        help="leave the simulator/emulator alive; only edit the local toml",
    )

    return parser


def _resolve_cwd(args: object) -> Path:
    cwd = getattr(args, "cwd", None)
    return Path(cwd).resolve() if cwd else Path(os.getcwd()).resolve()


# Top-level flags whose value lives in the next argv slot (`--flag value`). Used
# by _ensure_subcommand to skip past them when deciding where to inject the
# default `provision` subcommand.
_TOP_LEVEL_VALUE_FLAGS = {"--cwd", "--format"}


def _ensure_subcommand(argv: list[str]) -> list[str]:
    """Bare `splash …` (no subcommand) defaults to `provision`. Inserts the
    `provision` token at the right slot — after any leading top-level flags
    (`--cwd PATH`, `--format json`, …) so they parse at the root level."""
    if any(a in ("-h", "--help", "--version") for a in argv):
        return argv
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in KNOWN_CMDS:
            return argv  # explicit subcommand already present
        if a in _TOP_LEVEL_VALUE_FLAGS:
            i += 2  # flag + value
            continue
        if a.startswith("--") and "=" in a:
            i += 1  # --flag=value
            continue
        break  # first non-flag, non-subcommand token: insert provision here
    return argv[:i] + ["provision"] + argv[i:]


def _resolve_format(args: object) -> str:
    return getattr(args, "format", None) or "text"


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    argv = _ensure_subcommand(list(argv))

    parser = _build_parser()
    args = parser.parse_args(argv)

    cwd = _resolve_cwd(args)
    registry = Registry()

    try:
        if args.cmd == "init":
            cmd_init(cwd, preset=args.preset, force=args.force, loader_override=args.loader)
            return 0

        if args.cmd == "refresh-inventory":
            return cmd_refresh_inventory(cwd)

        if args.cmd == "gc":
            n = registry.gc()
            print(f"gc: removed {n} dead entries", file=sys.stderr)
            return 0

        if args.cmd == "doctor":
            return cmd_doctor(cwd, fix=args.fix, framework_override=args.framework)

        if args.cmd == "run":
            return cmd_run(cwd, registry, args.dtype, args.variant)

        if args.cmd == "start":
            return cmd_start(cwd, registry, args.dtype, args.variant)

        if args.cmd == "stop":
            return cmd_stop(cwd, args.dtype, args.variant)

        if args.cmd == "destroy":
            return cmd_destroy(cwd, args.dtype, args.variant)

        if args.cmd == "devices":
            return cmd_devices_list(cwd, _resolve_format(args))

        if args.cmd == "status":
            return cmd_status(
                cwd, registry, _resolve_format(args),
                show_all=(args.scope == "all"), check=args.check, verbose=args.verbose,
            )

        if args.cmd == "refresh":
            return _cmd_refresh(cwd, registry)

        if args.cmd == "list":
            target = str(Path(args.checkout).resolve()) if args.checkout else str(cwd)
            data = registry.all_for(target)
            if _resolve_format(args) == "json":
                print(json.dumps(data, indent=2))
            else:
                if not data:
                    print(f"(empty) {target}", file=sys.stderr)
                for k, v in sorted(data.items()):
                    print(f"{k}={v}")
            return 0

        if args.cmd == "get":
            target = str(Path(args.checkout).resolve()) if args.checkout else str(cwd)
            value = registry.all_for(target).get(args.key)
            if value is None:
                return 1
            print(value)
            return 0

        if args.cmd == "set":
            if "=" not in args.assignment:
                print("usage: splash set KEY=VALUE", file=sys.stderr)
                return 2
            key, value = args.assignment.split("=", 1)
            registry.set_kv(str(cwd), key, value)
            print(f"set {key}={value}", file=sys.stderr)
            return 0

        if args.cmd == "release":
            if args.key:
                registry.remove_kv(str(cwd), args.key)
                registry._remove_port(str(cwd), args.key)  # noqa: SLF001
                print(f"released {args.key}", file=sys.stderr)
            else:
                n = registry.release(str(cwd))
                print(f"released {n} entries for {cwd}", file=sys.stderr)
            return 0

        if args.cmd == "device":
            return _device_dispatch(args, cwd)

        # provision (default)
        return _cmd_provision(args, cwd, registry)
    except DeviceError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
