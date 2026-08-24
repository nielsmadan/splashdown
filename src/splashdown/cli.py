# PYTHON_ARGCOMPLETE_OK
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .cli_output import render_application_error, render_untyped_error
from .commands import (
    _cmd_provision,
    _cmd_provision_inner,
    _env_dispatch,
    cmd_bootstrap,
    cmd_completion,
    cmd_deinit,
    cmd_init,
    cmd_post_checkout_hook,
    cmd_refresh_inventory,
    cmd_status,
    cmd_trust,
    cmd_untrust,
)
from .constants import TARGET_TYPES
from .devices import DeviceError
from .doctor import cmd_doctor
from .errors import ApplicationError
from .recipe import load_settings
from .registry import Registry
from .target_commands import (
    _declared_target_types,
    _target_dispatch,
    cmd_destroy,
    cmd_gc,
    cmd_run,
    cmd_start,
    cmd_stop,
)
from .targets import _match_target_type_prefix


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
        from ._version import resolve_version  # noqa: PLC0415

        print(f"splashdown {resolve_version()}")
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
  deinit                     remove checkout-local state (keeps shared hook and trust)
  trust                      authorize automatic recipe handling for this clone
  untrust                    revoke automatic recipe handling for this clone
  bootstrap [--rerun]        sync + run trusted checkout bootstrap once
  doctor   [--fix]            check & fix framework wiring

More
  target   …                 declare & manage device targets   (splash target --help)
  env      …                 inspect resolved values           (splash env --help)
  gc                         drop dead-checkout entries (ports, vars, sims)
"""

KNOWN_CMDS = {
    "sync",
    "init",
    "deinit",
    "trust",
    "untrust",
    "bootstrap",
    "hook",
    "env",
    "gc",
    "doctor",
    "status",
    "run",
    "start",
    "stop",
    "destroy",
    "target",
    "completion",
}


def _build_parser() -> argparse.ArgumentParser:  # noqa: PLR0915 — flat parser; one block per subcommand
    from .completion import device_arg_completer, variant_completer  # noqa: PLC0415
    from .scaffolds import SCAFFOLDS  # noqa: PLC0415

    parser = argparse.ArgumentParser(
        prog="splash",
        description="splash — keeps each git checkout's ports, env vars & device targets in sync",
        epilog=_HELP_EPILOG,
        formatter_class=_EpilogOnlyFormatter,
    )
    parser.add_argument("--cwd", default=None, help="working directory (default: $PWD)")
    parser.add_argument("--format", choices=["text", "json"], default=None)
    parser.add_argument(
        "--show-values",
        action="store_true",
        help="include resolved values in operational status, env, and sync output",
    )
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
        help="intent preset (default: scan the project)",
    )
    p.add_argument(
        "--loader",
        default=None,
        choices=("mise", "direnv", "devbox", "none"),
        help="override loader auto-detection (none = write a dotenv file, wire nothing)",
    )
    p.add_argument("--overwrite", action="store_true", help="replace an existing splashdown.toml")
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
    p.add_argument(
        "--electron-profile",
        choices=("isolated", "shared"),
        help="Electron scanner choice (isolated = independent checkout profile)",
    )
    p.add_argument(
        "--ios-scheme",
        help="native iOS Xcode scheme (auto-detected when there is exactly one)",
    )

    sub.add_parser("deinit", help=argparse.SUPPRESS)
    sub.add_parser("trust", help=argparse.SUPPRESS)
    sub.add_parser("untrust", help=argparse.SUPPRESS)
    bootstrap = sub.add_parser("bootstrap", help=argparse.SUPPRESS)
    bootstrap.add_argument(
        "--rerun",
        action="store_true",
        help="run bootstrap again even if this checkout already completed it",
    )

    hook = sub.add_parser("hook", help=argparse.SUPPRESS)
    hook_sub = hook.add_subparsers(dest="hook_cmd", metavar="EVENT")
    post_checkout = hook_sub.add_parser("post-checkout", help=argparse.SUPPRESS)
    post_checkout.add_argument("old")
    post_checkout.add_argument("new")
    post_checkout.add_argument("flag")

    env = sub.add_parser("env", help=argparse.SUPPRESS)
    env.add_argument("--checkout", default=None)  # for bare `splash env` (list)
    envsub = env.add_subparsers(dest="env_cmd", metavar="ACTION")
    eg = envsub.add_parser("get", help="print one resolved value")
    eg.add_argument("key")
    eg.add_argument("--checkout", default=None)
    es = envsub.add_parser("set", help='set a manual value (for type="set" resources)')
    es.add_argument("assignment", metavar="KEY=VALUE")
    es.add_argument("--checkout", default=None)
    er = envsub.add_parser("release", help="free this checkout's allocations (all, or one KEY)")
    er.add_argument("key", nargs="?")
    er.add_argument("--checkout", default=None)

    sub.add_parser("gc", help=argparse.SUPPRESS)

    p = sub.add_parser("completion", help=argparse.SUPPRESS)
    p.add_argument(
        "shell",
        nargs="?",
        help="bash | zsh (default: autodetect from $SHELL)",
    )

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
        # No argparse `choices`: a lone variant token (`splash run small-screen`)
        # lands here first, then normalization and target resolution infer its type.
        dtype_arg = p.add_argument(
            "dtype",
            metavar="TYPE",
            nargs="?",
            help="target type (simulator|emulator|device), or an exact variant unique across types",
        )
        dtype_arg.completer = device_arg_completer  # type: ignore[attr-defined]
        variant_arg = p.add_argument(
            "variant", nargs="?", help="variant name (defaults to `default`)"
        )
        variant_arg.completer = variant_completer  # type: ignore[attr-defined]
        if verb == "destroy":
            p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")

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
    add.add_argument(
        "--global",
        action="store_true",
        dest="global_scope",
        help="add to the machine-wide config (~/.config/splashdown/config.toml), "
        "available in every project",
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
    rm.add_argument(
        "--global",
        action="store_true",
        dest="global_scope",
        help="remove from the machine-wide config instead of splashdown.local.toml",
    )

    return parser


def _resolve_cwd(args: object) -> Path:
    cwd = getattr(args, "cwd", None)
    return Path(cwd).resolve() if cwd else Path(os.getcwd()).resolve()


def _normalize_device_args(args: argparse.Namespace) -> None:
    """For run/start/stop/destroy: the `dtype` slot no longer uses argparse
    `choices`, so a lone non-type token (`splash run small-screen`) lands in
    `dtype`. Reinterpret it as the variant, and validate anything left in the
    type slot. Type names win over equally-named variants.

    When prefix matching is enabled (the default — settings resolved from the
    global config + this checkout's local file), an abbreviated type token like
    `sim` is expanded to its canonical name (`simulator`) before that demotion,
    so `splash run sim` selects the simulator type. The prefix is matched only
    against types the checkout *declares*, so a short token never gets claimed by
    an undeclared type (`splash run d` in a sim-only project stays a variant
    prefix, resolving e.g. `default`, rather than expanding to `device`). A type
    prefix wins over an identically-prefixed variant name."""
    if (
        args.dtype
        and args.dtype not in TARGET_TYPES
        and load_settings(cwd := _resolve_cwd(args)).prefix_match
    ):
        expanded = _match_target_type_prefix(
            args.dtype, _declared_target_types(cwd, include_global=False)
        )
        if expanded is not None:
            args.dtype = expanded
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
_TOP_LEVEL_BOOL_FLAGS = {"--show-values"}


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
        if a in _TOP_LEVEL_BOOL_FLAGS:
            i += 1
            continue
        if a.startswith("--") and "=" in a:
            i += 1  # --flag=value
            continue
        break  # first non-flag, non-subcommand token: insert sync here
    return [*argv[:i], "sync", *argv[i:]]


def _resolve_format(args: object) -> str:
    return getattr(args, "format", None) or "text"


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911, PLR0912 — one return/branch per subcommand; this is the dispatch table
    if argv is None:
        argv = sys.argv[1:]
    argv = _ensure_subcommand(list(argv))

    parser = _build_parser()
    # Must stay immediately before parse_args: during an active completion,
    # autocomplete() parses COMP_LINE itself and exits before parse_args runs.
    from .completion import install as _install_completion  # noqa: PLC0415

    _install_completion(parser)
    args = parser.parse_args(argv)

    # Needs no checkout or registry — dispatch before touching either.
    if args.cmd == "completion":
        return cmd_completion(args.shell)

    cwd = _resolve_cwd(args)
    if args.cmd == "trust":
        return cmd_trust(cwd)
    if args.cmd == "untrust":
        return cmd_untrust(cwd)
    if args.cmd == "bootstrap":
        return cmd_bootstrap(cwd, rerun=args.rerun)
    if args.cmd == "hook":
        if args.hook_cmd != "post-checkout":
            parser.error("hook requires an event")
        return cmd_post_checkout_hook(cwd, None, args.old, args.new, args.flag)

    registry = Registry()

    try:
        if args.cmd in ("run", "start", "stop", "destroy"):
            _normalize_device_args(args)
        if args.cmd == "init":
            if args.rescan:
                return cmd_refresh_inventory(cwd)
            cmd_init(
                cwd,
                preset=args.preset,
                force=args.overwrite,
                loader_override=args.loader,
                electron_profile=args.electron_profile,
                ios_scheme=args.ios_scheme,
            )
            if args.no_sync:
                return 0
            return _cmd_provision_inner(cwd, registry, show_values=args.show_values)

        if args.cmd == "deinit":
            return cmd_deinit(cwd, registry)

        if args.cmd == "gc":
            return cmd_gc(registry)

        if args.cmd == "doctor":
            return cmd_doctor(cwd, fix=args.fix, framework_override=args.framework)

        if args.cmd == "run":
            return cmd_run(cwd, registry, args.dtype, args.variant)

        if args.cmd == "start":
            return cmd_start(cwd, registry, args.dtype, args.variant)

        if args.cmd == "stop":
            return cmd_stop(cwd, registry, args.dtype, args.variant)

        if args.cmd == "destroy":
            return cmd_destroy(cwd, registry, args.dtype, args.variant, yes=args.yes)

        if args.cmd == "status":
            return cmd_status(
                cwd,
                registry,
                _resolve_format(args),
                show_all=(args.scope == "all"),
                check=args.check,
                verbose=args.verbose,
                show_values=args.show_values,
            )

        if args.cmd == "env":
            return _env_dispatch(args, cwd, registry)

        if args.cmd == "target":
            return _target_dispatch(args, cwd, registry)

        # sync (default, what bare `splash` runs)
        return _cmd_provision(args, cwd, registry)
    except ApplicationError as error:
        return render_application_error(error)
    except (DeviceError, ValueError) as error:
        return render_untyped_error(error)
