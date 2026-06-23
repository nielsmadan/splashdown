# PYTHON_ARGCOMPLETE_OK
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import TARGET_TYPES
from .commands import (
    _cmd_provision,
    _cmd_provision_inner,
    _env_dispatch,
    _target_dispatch,
    cmd_destroy,
    cmd_gc,
    cmd_init,
    cmd_refresh_inventory,
    cmd_run,
    cmd_start,
    cmd_status,
    cmd_stop,
)
from .devices import DeviceError
from .registry import Registry
from .wiring import cmd_doctor

# ---------- CLI ----------


class _EpilogOnlyFormatter(argparse.RawDescriptionHelpFormatter):
    """Hide the auto-generated subcommand list; the epilog carries the tiered overview."""

    def _format_action(self, action: argparse.Action) -> str:
        # argparse exposes no public type for the subparsers action.
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            return ""
        return super()._format_action(action)


class _VersionAction(argparse.Action):
    """Like argparse's built-in version action, but resolves the version
    lazily (only when `--version` is given) so the hot path skips the
    ~20ms metadata lookup."""

    def __init__(
        self,
        option_strings: list[str],
        dest: str = argparse.SUPPRESS,
        default: str = argparse.SUPPRESS,
        help: str | None = "show program's version number and exit",
    ) -> None:
        super().__init__(option_strings, dest, nargs=0, default=default, help=help)

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        from . import _resolve_version  # noqa: PLC0415

        print(f"splashdown {_resolve_version()}")
        parser.exit()


_HELP_EPILOG = """\
Run on a device
  run      [type] [variant]   build + launch the app on a target   (the daily driver)
  start    [type] [variant]   boot the target (no build/launch)
  stop     [type] [variant]   shut the target down
  destroy  [type] [variant]   delete this checkout's target instance

This checkout
  sync     [--force] [--setup NAME]   pick free ports, resolve vars, write splashdown.env
                                      (also what bare `splash` and the git hook run)
  status   [all]              state of this checkout (or every checkout)

Set up a project
  init     [preset] [--rescan]   scaffold splashdown.toml + first sync (--no-sync skips it)
  doctor   [--fix]            check & fix framework wiring

More
  target   …                 declare & manage device targets   (splash target --help)
  env      …                 inspect resolved values           (splash env --help)
  gc                         drop dead-checkout entries (ports, vars, sims)
"""

KNOWN_CMDS = {
    "sync",
    "init",
    "env",
    "gc",
    "doctor",
    "status",
    "run",
    "start",
    "stop",
    "destroy",
    "target",
}


def _build_parser() -> argparse.ArgumentParser:  # noqa: PLR0915 — flat parser; one block per subcommand
    from .completion import device_arg_completer, variant_completer  # noqa: PLC0415
    from .profiles import SCAFFOLDS  # noqa: PLC0415

    parser = argparse.ArgumentParser(
        prog="splash",
        description="splash — keeps each git checkout's ports, env vars & device targets in sync",
        epilog=_HELP_EPILOG,
        formatter_class=_EpilogOnlyFormatter,
    )
    parser.add_argument("--cwd", default=None, help="working directory (default: $PWD)")
    parser.add_argument("--format", choices=["text", "json"], default=None)
    parser.add_argument("--version", action=_VersionAction)
    sub = parser.add_subparsers(dest="cmd", metavar="<command>")

    p = sub.add_parser("sync", help=argparse.SUPPRESS)
    p.add_argument(
        "--force",
        action="store_true",
        help="re-allocate everything from scratch (regenerates uuids etc.)",
    )
    p.add_argument("--setup", help="also run a [setup.NAME] block from the recipe")

    p = sub.add_parser("status", help=argparse.SUPPRESS)
    p.add_argument(
        "scope",
        nargs="?",
        choices=("local", "all"),
        default="local",
        help="local (default): this checkout only. all: every tracked checkout.",
    )
    p.add_argument(
        "--check", action="store_true", help="revalidate liveness and print a cleanup hint"
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="with `all`, expand each checkout into the per-block view",
    )
    p = sub.add_parser("init", help=argparse.SUPPRESS)
    p.add_argument(
        "preset",
        nargs="?",
        default=None,
        choices=tuple(SCAFFOLDS),
        help="named scaffold (default: scan the project)",
    )
    p.add_argument(
        "--loader",
        default=None,
        choices=("mise", "direnv", "devbox", "none"),
        help="override loader auto-detection (none = write a dotenv file, wire nothing)",
    )
    p.add_argument("--force", action="store_true")
    p.add_argument(
        "--rescan",
        action="store_true",
        help="re-detect [project]/[apps.*] in an existing splashdown.toml (don't scaffold)",
    )
    p.add_argument(
        "--no-sync",
        action="store_true",
        help="scaffold only; skip the first sync (don't allocate ports / write splashdown.env)",
    )

    env = sub.add_parser("env", help=argparse.SUPPRESS)
    env.add_argument("--checkout", default=None)  # for bare `splash env` (list)
    envsub = env.add_subparsers(dest="env_cmd", metavar="ACTION")
    eg = envsub.add_parser("get", help="print one resolved value")
    eg.add_argument("key")
    eg.add_argument("--checkout", default=None)
    es = envsub.add_parser("set", help='set a manual value (for type="set" resources)')
    es.add_argument("assignment", metavar="KEY=VALUE")
    er = envsub.add_parser("release", help="free this checkout's allocations (all, or one KEY)")
    er.add_argument("key", nargs="?")

    sub.add_parser("gc", help=argparse.SUPPRESS)

    p = sub.add_parser("doctor", help=argparse.SUPPRESS)
    p.add_argument(
        "--fix",
        action="store_true",
        help="apply safe autofixes; print manual instructions for the rest",
    )
    p.add_argument(
        "--framework",
        default=None,
        help="override framework detection (any profile name, e.g. react-native|flutter|expo|vite|springboot)",
    )

    for verb in ("run", "start", "stop", "destroy"):
        p = sub.add_parser(verb, help=argparse.SUPPRESS)
        # dtype optional: if there's exactly one declared target type for this
        # checkout, that's what's used. No argparse `choices` here so a lone
        # variant token (`splash run small-screen`) is accepted; validated in
        # _normalize_device_args.
        dtype_arg = p.add_argument(
            "dtype",
            metavar="TYPE",
            nargs="?",
            help="target type (simulator|emulator|device); inferred if one is declared",
        )
        dtype_arg.completer = device_arg_completer  # type: ignore[attr-defined]
        variant_arg = p.add_argument(
            "variant", nargs="?", help="variant name (defaults to `default`)"
        )
        variant_arg.completer = variant_completer  # type: ignore[attr-defined]

    dev = sub.add_parser("target", help=argparse.SUPPRESS)
    devsub = dev.add_subparsers(dest="target_cmd", metavar="ACTION")

    ref = devsub.add_parser(
        "refresh", help="destroy + recreate stale/missing sims & AVDs to latest (no boot)"
    )
    ref.add_argument(
        "platform",
        nargs="?",
        default="all",
        choices=("ios", "android", "all"),
        help="scope (default: all = both)",
    )

    prune = devsub.add_parser("prune", help="destroy every sim/AVD splashdown did NOT create")
    prune.add_argument(
        "platform",
        nargs="?",
        default="all",
        choices=("ios", "android", "all"),
        help="scope (default: all = both)",
    )
    prune.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    prune.add_argument(
        "--dry-run", action="store_true", dest="dry_run", help="list without deleting"
    )

    add = devsub.add_parser("add", help="declare a variant in splashdown.local.toml")
    add.add_argument("dtype", choices=TARGET_TYPES, metavar="TYPE")
    add.add_argument("variant", help="variant name (e.g. `default`, `small-screen`)")
    add.add_argument("--model")
    add.add_argument("--ios")
    add.add_argument("--device")
    add.add_argument("--image")
    add.add_argument(
        "--name",
        dest="sim_name",
        help="simulator/emulator name override; device: match by device name",
    )
    add.add_argument("--id", dest="device_id", help="device: exact udid / adb serial")
    add.add_argument(
        "--platform",
        choices=("ios", "android"),
        help="device: scope auto-pick to one platform",
    )

    rm = devsub.add_parser(
        "remove", help="remove a variant from splashdown.local.toml (and destroy its sim)"
    )
    rm.add_argument("dtype", choices=TARGET_TYPES, metavar="TYPE")
    rm_variant = rm.add_argument("variant")
    rm_variant.completer = variant_completer  # type: ignore[attr-defined]
    rm.add_argument(
        "--keep-instance",
        action="store_true",
        dest="keep_instance",
        help="leave the simulator/emulator alive; only edit the local toml",
    )

    return parser


def _resolve_cwd(args: object) -> Path:
    cwd = getattr(args, "cwd", None)
    return Path(cwd).resolve() if cwd else Path(os.getcwd()).resolve()


def _normalize_device_args(args: argparse.Namespace) -> None:
    """For run/start/stop/destroy: the `dtype` slot no longer uses argparse
    `choices`, so a lone non-type token (`splash run small-screen`) lands in
    `dtype`. Reinterpret it as the variant, and validate anything left in the
    type slot. Type names win over equally-named variants."""
    if args.dtype is not None and args.dtype not in TARGET_TYPES and args.variant is None:
        args.dtype, args.variant = None, args.dtype
    if args.dtype is not None and args.dtype not in TARGET_TYPES:
        raise DeviceError(
            f"invalid device type `{args.dtype}`; expected one of {', '.join(TARGET_TYPES)}"
        )


# Top-level flags whose value lives in the next argv slot (`--flag value`). Used
# by _ensure_subcommand to skip past them when deciding where to inject the
# default `sync` subcommand.
_TOP_LEVEL_VALUE_FLAGS = {"--cwd", "--format"}


def _ensure_subcommand(argv: list[str]) -> list[str]:
    """Bare `splash …` (no subcommand) defaults to `sync`. Inserts the
    `sync` token at the right slot — after any leading top-level flags
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
        break  # first non-flag, non-subcommand token: insert sync here
    return [*argv[:i], "sync", *argv[i:]]


def _resolve_format(args: object) -> str:
    return getattr(args, "format", None) or "text"


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911 — one return per subcommand; this is the dispatch table
    if argv is None:
        argv = sys.argv[1:]
    argv = _ensure_subcommand(list(argv))

    parser = _build_parser()
    # Must stay immediately before parse_args: during an active completion,
    # autocomplete() parses COMP_LINE itself and exits before parse_args runs.
    from .completion import install as _install_completion  # noqa: PLC0415

    _install_completion(parser)
    args = parser.parse_args(argv)

    cwd = _resolve_cwd(args)
    registry = Registry()

    try:
        if args.cmd in ("run", "start", "stop", "destroy"):
            _normalize_device_args(args)
        if args.cmd == "init":
            if args.rescan:
                return cmd_refresh_inventory(cwd)
            cmd_init(cwd, preset=args.preset, force=args.force, loader_override=args.loader)
            if args.no_sync:
                return 0
            return _cmd_provision_inner(cwd, registry)

        if args.cmd == "gc":
            return cmd_gc(registry)

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

        if args.cmd == "status":
            return cmd_status(
                cwd,
                registry,
                _resolve_format(args),
                show_all=(args.scope == "all"),
                check=args.check,
                verbose=args.verbose,
            )

        if args.cmd == "env":
            return _env_dispatch(args, cwd, registry)

        if args.cmd == "target":
            return _target_dispatch(args, cwd)

        # sync (default, what bare `splash` runs)
        return _cmd_provision(args, cwd, registry)
    except (DeviceError, ValueError) as e:
        # DeviceError: device/target lifecycle failures. ValueError: recipe
        # validation (unknown target type, the [devices.*]→[targets.*] rename, …).
        print(f"error: {e}", file=sys.stderr)
        return 1
