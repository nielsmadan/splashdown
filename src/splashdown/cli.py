# PYTHON_ARGCOMPLETE_OK
from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn, cast

from .catalog import PROFILES
from .cli_output import render_application_error, render_claim_notices, render_untyped_error
from .commands import (
    InitOptions,
    _cmd_provision,
    _env_dispatch,
    cmd_bootstrap,
    cmd_completion,
    cmd_deinit,
    cmd_init,
    cmd_post_checkout_hook,
    cmd_status,
    cmd_trust,
    cmd_untrust,
)
from .constants import TARGET_TYPES
from .devices import DeviceError
from .doctor import cmd_doctor
from .errors import ApplicationError, UsageError
from .project_selection import ProjectSelection, resolve_start_directory, select_project
from .recipe import load_settings
from .registry import Registry
from .results import OutputFormat
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


@dataclass
class ParseState:
    format: OutputFormat = "text"
    command: str | None = None
    formats: list[OutputFormat] = field(default_factory=list)
    cwd_values: list[str] = field(default_factory=list)
    parser: _Parser | None = None


class _ParserUsageError(UsageError):
    def __init__(self, message: str, parser: _Parser) -> None:
        super().__init__(message)
        self.parser = parser


class _Parser(argparse.ArgumentParser):
    state: ParseState
    command: str | None

    def __init__(self, *args: object, **kwargs: object) -> None:
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def parse_known_args(  # type: ignore[override]
        self, args: Iterable[str] | None = None, namespace: argparse.Namespace | None = None
    ) -> tuple[argparse.Namespace, list[str]]:
        self.state.command = self.command
        self.state.parser = self
        return super().parse_known_args(args, namespace)

    def error(self, message: str) -> NoReturn:
        raise _ParserUsageError(message, self.state.parser or self)


class _GlobalAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        parser = cast(_Parser, parser)
        values = cast(str, values)
        if self.dest == "format":
            parser.state.format = "json" if values == "json" else "text"
            parser.state.formats.append(parser.state.format)
        else:
            parser.state.cwd_values.append(values)
        setattr(namespace, self.dest, values)


def _register_global_options(parser: _Parser, *, root: bool, state: ParseState) -> None:
    parser.state = state
    default = None if root else argparse.SUPPRESS
    parser.add_argument(
        "--cwd",
        default=default,
        action=_GlobalAction,
        help="working directory (default: $PWD)",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default=default,
        action=_GlobalAction,
        help="output format for sync, status, init, env/target lists, target claims, or ai guidance",
    )


def _child_parsers(parser: argparse.ArgumentParser) -> dict[str, _Parser]:
    return {
        name: child
        for action in parser._actions  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
        for name, child in action.choices.items()
    }


def _configure_parser_tree(parser: _Parser, state: ParseState, command: str = "") -> None:
    pending = [(parser, command)]
    while pending:
        current, current_command = pending.pop()
        current.command = current_command or None
        _register_global_options(current, root=not current_command, state=state)
        pending.extend(
            (child, f"{current_command} {name}".strip())
            for name, child in reversed(list(_child_parsers(current).items()))
        )


def _command_help(parser: _Parser, path: list[str]) -> None:
    for name in path:
        child = _child_parsers(parser).get(name)
        if child is None:
            parser.state.parser = parser
            parser.state.command = parser.command
            parser.error(f"unknown help command: {name}")
        parser = child
    parser.print_help()


class _EpilogOnlyFormatter(argparse.RawDescriptionHelpFormatter):
    """Hide the auto-generated subcommand list; the epilog carries the tiered overview."""

    def _format_action(self, action: argparse.Action) -> str:
        # argparse exposes no public type for the subparsers action.
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            return ""
        return super()._format_action(action)


class _VersionAction(argparse.Action):
    """Resolve the version only for --version so normal startup skips the metadata lookup."""

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
  sync     [--force] [--setup NAME]   pick free ports, resolve vars, write the env file
                                      (also what bare `splash` and the git hook run)
  status   [all]              state of this checkout (or every checkout)

Set up a project
  init                        scaffold splashdown.toml + project integrations
  deinit                     remove checkout-local state (keeps shared hook and trust)
  trust                      authorize automatic recipe handling for this clone
  untrust                    revoke automatic recipe handling for this clone
  bootstrap [--rerun]        sync + run trusted checkout bootstrap once
  doctor   [--fix]            check & fix framework wiring

More
  target   …                 declare & manage device targets   (splash target --help)
  ai       …                 inspect & maintain agent guidance (splash ai --help)
  env      …                 inspect resolved values           (splash env --help)
  gc                         drop dead-checkout entries (ports, vars, sims)
  completion [shell]         print shell completion setup
  help [COMMAND ...]         show help for an exact command path
"""

_VALUE_OUTPUT_HELP = "Output: --format {text,json} at any command level."
_FORMAT_OUTPUT_HELP = "Output: --format {text,json} at any command level."

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
    "ai",
    "help",
}


def _build_parser(state: ParseState | None = None) -> _Parser:  # noqa: PLR0915 — flat parser; one block per subcommand
    from .completion import (  # noqa: PLC0415
        available_platform_completer,
        device_arg_completer,
        physical_variant_completer,
        variant_completer,
    )

    parser = _Parser(
        prog="splash",
        description="splash — keeps each git checkout's ports, env vars & device targets in sync",
        epilog=_HELP_EPILOG,
        formatter_class=_EpilogOnlyFormatter,
    )
    parser.add_argument("--version", action=_VersionAction)
    parser.add_argument(
        "--force", dest="default_force", action="store_true", help="force default sync"
    )
    parser.add_argument(
        "--setup", dest="default_setup", metavar="SETUP", help="run a setup block with default sync"
    )
    sub = parser.add_subparsers(dest="cmd", metavar="<command>")

    p = sub.add_parser("sync", help=argparse.SUPPRESS, epilog=_VALUE_OUTPUT_HELP)
    p.add_argument(
        "--force",
        action="store_true",
        default=argparse.SUPPRESS,
        help="re-allocate everything from scratch (regenerates uuids etc.)",
    )
    p.add_argument(
        "--setup", default=argparse.SUPPRESS, help="also run a [setup.NAME] block from the recipe"
    )

    p = sub.add_parser("status", help=argparse.SUPPRESS, epilog=_VALUE_OUTPUT_HELP)
    p.add_argument(
        "scope",
        nargs="?",
        choices=("all",),
        default=None,
        help="all: every tracked checkout; omitted: the selected project.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="with `all`, expand each checkout into the per-block view",
    )
    p = sub.add_parser("init", help=argparse.SUPPRESS)
    p.add_argument(
        "--loader",
        default=None,
        choices=("mise", "direnv", "devbox", "none"),
        help="override loader auto-detection (none = configure the destination, wire nothing)",
    )
    p.add_argument(
        "--env-file",
        metavar="PATH",
        help="checkout-relative destination for generated values (default: splashdown.env)",
    )
    p.add_argument("--overwrite", action="store_true", help="replace an existing splashdown.toml")

    ai = sub.add_parser(
        "ai",
        help=argparse.SUPPRESS,
        description="Manage guidance in existing AGENTS.md and CLAUDE.md files.",
        epilog=_FORMAT_OUTPUT_HELP,
    )
    aisub = ai.add_subparsers(dest="ai_cmd", required=True, metavar="ACTION")
    aisub.add_parser("status", help="inspect recorded guidance without changing files")
    update = aisub.add_parser("update", help="update guidance from splashdown.toml")
    update.add_argument(
        "--replace",
        action="store_true",
        help="replace edited complete blocks, preserving the first baseline",
    )
    aisub.add_parser("uninstall", help="remove owned guidance, including without a valid recipe")

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
    hook_sub = hook.add_subparsers(dest="hook_cmd", required=True, metavar="EVENT")
    post_checkout = hook_sub.add_parser("post-checkout", help=argparse.SUPPRESS)
    post_checkout.add_argument("old")
    post_checkout.add_argument("new")
    post_checkout.add_argument("flag")

    env = sub.add_parser(
        "env",
        help=argparse.SUPPRESS,
        description="Omit ACTION to list this checkout's stored assignments without changing state.",
        epilog=_VALUE_OUTPUT_HELP,
    )
    envsub = env.add_subparsers(dest="env_cmd", metavar="[ACTION]")
    eg = envsub.add_parser("get", help="print one stored value")
    eg.add_argument("key")
    es = envsub.add_parser("set", help='set a manual value (for type="set" resources)')
    es.add_argument("assignment", metavar="KEY=VALUE")
    er = envsub.add_parser("release", help="free this checkout's allocations (all, or one KEY)")
    er.add_argument("key", nargs="?")

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
        choices=tuple(PROFILES),
        help="override framework detection with a known profile",
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

    dev = sub.add_parser(
        "target",
        help=argparse.SUPPRESS,
        description="Omit ACTION to list this checkout's declared targets.",
        epilog=_FORMAT_OUTPUT_HELP,
    )
    devsub = dev.add_subparsers(dest="target_cmd", metavar="[ACTION]")

    ref = devsub.add_parser(
        "refresh",
        help="reconcile managed sims and AVDs across all registered checkouts",
        description=(
            "Across all registered checkouts, recreate stale or missing managed simulators and\n"
            "emulators at their declared runtime or image, resolving versions configured as\n"
            "`latest`. Without confirmation, destroy managed instances that are undeclared or\n"
            "belong to dead checkouts. Reconciled instances are not booted."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
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

    add = devsub.add_parser(
        "add",
        help="declare a variant in splashdown.local.toml",
        epilog=(
            "Type-specific options:\n"
            "  simulator: --model, --ios, --name\n"
            "  emulator: --device, --image, --name\n"
            "  device: --name, --id, --platform"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add.add_argument("dtype", choices=TARGET_TYPES, metavar="TYPE")
    add.add_argument("variant", help="variant name (e.g. `default`, `small-screen`)")
    add.add_argument("--model", help="iOS simulator model, such as `iPhone 17`")
    add.add_argument("--ios", help="iOS runtime version; defaults to the latest installed")
    add.add_argument("--device", help="Android emulator hardware profile, such as `pixel_9`")
    add.add_argument("--image", help="Android system image; defaults to the latest installed")
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
        "remove",
        help="remove a declared target variant",
        description=(
            "Local removal destroys the managed simulator/emulator by default.\n"
            "Global removal edits configuration only until target refresh."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
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

    devsub.add_parser("claims", help="list machine-wide physical-device claims")

    claim = devsub.add_parser("claim", help="claim a configured physical device")
    claim_variant = claim.add_argument("variant", nargs="?")
    claim_variant.completer = physical_variant_completer  # type: ignore[attr-defined]
    available = claim.add_argument("--available", choices=("ios", "android", "any"))
    available.completer = available_platform_completer  # type: ignore[attr-defined]
    claim.add_argument("--force", action="store_true")

    release = devsub.add_parser("release", help="release a configured physical-device claim")
    release_variant = release.add_argument("variant", nargs="?")
    release_variant.completer = physical_variant_completer  # type: ignore[attr-defined]
    release.add_argument("--all", action="store_true", dest="all_owned")
    release.add_argument("--force", action="store_true")

    help_parser = sub.add_parser("help", help=argparse.SUPPRESS)
    help_parser.add_argument("command_path", nargs="*", metavar="COMMAND")
    _configure_parser_tree(parser, state if state is not None else ParseState())
    return parser


def _resolve_cwd(args: object) -> Path:
    selection = getattr(args, "selection", None)
    if isinstance(selection, ProjectSelection):
        return selection.directory
    return resolve_start_directory(getattr(args, "cwd", None))


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


def _resolve_format(args: object) -> str:
    return getattr(args, "format", None) or "text"


def _consume_claim_notices(cwd: Path, registry: Registry) -> None:
    try:
        notices = registry.consume_claim_notices(str(cwd))
    except OSError as error:
        print(f"warning: unable to consume physical target notices: {error}", file=sys.stderr)
        return
    render_claim_notices(notices)


def _validate_parsed_args(parser: _Parser, args: argparse.Namespace) -> None:
    if (
        args.cmd == "target"
        and args.target_cmd == "remove"
        and args.global_scope
        and args.keep_instance
    ):
        parser.error("target remove --global cannot be combined with --keep-instance")
    if (
        args.cmd == "target"
        and args.target_cmd == "remove"
        and args.dtype == "device"
        and args.keep_instance
    ):
        parser.error("target remove device cannot be combined with --keep-instance")

    supports_format = (
        args.cmd in {"sync", "status", "init", "ai"}
        or (args.cmd == "completion" and args.format == "text")
        or (args.cmd == "env" and args.env_cmd in {None, "get"})
        or (args.cmd == "target" and args.target_cmd in {None, "claim", "claims"})
    )
    if args.format is not None and not supports_format:
        error = _ParserUsageError(
            "--format is only supported by sync, status, init, env/env get, target lists, "
            "target claims, and ai",
            parser.state.parser or parser,
        )
        error.code = "unsupported_format"
        raise error


def main(argv: list[str] | None = None) -> int:
    try:
        return _dispatch(argv)
    except KeyboardInterrupt:
        return 130


def _select_default_command(parser: _Parser, args: argparse.Namespace) -> None:
    if args.cmd is None:
        args.cmd = "sync"
        parser.state.command = "sync"
        parser.state.parser = _child_parsers(parser)["sync"]
    if args.cmd == "sync":
        args.force = getattr(args, "force", False) or args.default_force
        args.setup = getattr(args, "setup", args.default_setup)
    elif args.default_force or args.default_setup is not None:
        parser.error("root --force and --setup require sync")


def _render_usage_error(state: ParseState, error: _ParserUsageError) -> int:
    error.parser.print_usage(sys.stderr)
    if state.format == "text":
        error.parser.exit(
            2, f"{error.parser.prog}: error: {error}\nRun `{error.parser.prog} --help` for usage.\n"
        )
    from .cli_output import emit_result  # noqa: PLC0415
    from .results import CommandResult, Diagnostic  # noqa: PLC0415

    return emit_result(
        CommandResult(
            state.command,
            "error",
            2,
            error=Diagnostic(error.code, str(error)),
            next_steps=(f"Run `{error.parser.prog} --help` for usage.",),
        ),
        "json",
    )


def _render_pre_dispatch_error(
    state: ParseState, error: ApplicationError | OSError | KeyboardInterrupt
) -> int:
    from .cli_output import emit_result  # noqa: PLC0415
    from .results import CommandResult, Diagnostic  # noqa: PLC0415

    return emit_result(
        CommandResult(
            state.command,
            "error",
            130 if isinstance(error, KeyboardInterrupt) else 1,
            error=Diagnostic(
                "interrupted"
                if isinstance(error, KeyboardInterrupt)
                else error.code
                if isinstance(error, ApplicationError)
                else "io_error",
                "Operation interrupted." if isinstance(error, KeyboardInterrupt) else str(error),
            ),
        ),
        state.format,
    )


def _select_command_directory(args: argparse.Namespace, state: ParseState) -> Path:
    for value in state.cwd_values:
        resolve_start_directory(value)
    start = resolve_start_directory(args.cwd)
    exact = (
        args.cmd in {"init", "hook", "gc"}
        or (args.cmd == "status" and args.scope == "all")
        or (args.cmd == "env" and args.env_cmd in {"set", "release"})
        or (
            args.cmd == "target"
            and (
                args.target_cmd in {"refresh", "prune", "available", "claims"}
                or getattr(args, "global_scope", False)
            )
        )
    )
    args.selection = (
        ProjectSelection(start, start, None, None)
        if exact and args.cmd != "init"
        else select_project(start, exact=exact)
    )
    return cast(ProjectSelection, args.selection).directory


def _dispatch_env_inspection(args: argparse.Namespace, state: ParseState) -> int:
    from .cli_output import emit_result, render_env_result  # noqa: PLC0415
    from .commands import inspect_env  # noqa: PLC0415
    from .registry import read_registry_snapshot  # noqa: PLC0415

    try:
        snapshot = read_registry_snapshot()
        known = snapshot.all_checkouts()
        for value in state.cwd_values:
            resolve_start_directory(value, known_checkouts=known)
        start = resolve_start_directory(args.cwd, known_checkouts=known)
        selection = select_project(start, known_checkouts=known)
        result = inspect_env(selection.directory, snapshot, getattr(args, "key", None))
    except (ApplicationError, OSError, KeyboardInterrupt) as error:
        return _render_pre_dispatch_error(state, error)
    return emit_result(result, state.format, render_text=render_env_result)


def _dispatch_ai(args: argparse.Namespace) -> int:
    from .ai_commands import cmd_ai  # noqa: PLC0415
    from .cli_output import emit_result, render_ai_result  # noqa: PLC0415
    from .errors import CapabilityError  # noqa: PLC0415
    from .results import CommandResult, Diagnostic  # noqa: PLC0415

    try:
        result = cmd_ai(_resolve_cwd(args), args.ai_cmd, replace=getattr(args, "replace", False))
    except ApplicationError as error:
        result = CommandResult(
            command=f"ai {args.ai_cmd}",
            status="partial" if error.partial else "error",
            exit_code=error.exit_code,
            data=error.data,
            error=Diagnostic(error.code, str(error)),
            warnings=error.warnings,
            next_steps=error.next_steps,
        )
    except (OSError, DeviceError) as error:
        code = (
            "capability_unavailable"
            if isinstance(error, CapabilityError)
            else "device_error"
            if isinstance(error, DeviceError)
            else "io_error"
        )
        result = CommandResult(
            f"ai {args.ai_cmd}",
            "error",
            1,
            error=Diagnostic(code, str(error)),
        )
    except KeyboardInterrupt:
        result = CommandResult(
            f"ai {args.ai_cmd}",
            "error",
            130,
            error=Diagnostic("interrupted", "Guidance operation interrupted."),
        )
    return emit_result(
        result,
        "json" if _resolve_format(args) == "json" else "text",
        render_text=render_ai_result,
    )


def _dispatch_status(args: argparse.Namespace, state: ParseState, cwd: Path) -> int:
    from .registry import RegistryReadError, read_registry_snapshot  # noqa: PLC0415

    try:
        from .results import Diagnostic  # noqa: PLC0415

        diagnostics: tuple[Diagnostic, ...] = ()
        try:
            snapshot = read_registry_snapshot()
        except RegistryReadError as error:
            snapshot = error.snapshot
            diagnostics = error.diagnostics
        return cmd_status(
            cwd,
            snapshot,
            _resolve_format(args),
            show_all=args.scope == "all",
            verbose=args.verbose,
            diagnostics=diagnostics,
        )
    except (ApplicationError, OSError, KeyboardInterrupt) as error:
        return _render_pre_dispatch_error(state, error)


def _dispatch(argv: list[str] | None = None) -> int:  # noqa: PLR0911, PLR0912 — one return/branch per subcommand; this is the dispatch table
    if argv is None:
        argv = sys.argv[1:]
    state = ParseState()
    parser = _build_parser(state)
    # Must stay immediately before parse_args: during an active completion,
    # autocomplete() parses COMP_LINE itself and exits before parse_args runs.
    from .completion import install as _install_completion  # noqa: PLC0415

    _install_completion(parser)
    try:
        args = parser.parse_args(argv)
        if args.cmd == "help":
            _command_help(parser, args.command_path)
            return 0
        _select_default_command(parser, args)
        _validate_parsed_args(parser, args)
    except _ParserUsageError as error:
        return _render_usage_error(state, error)

    # Needs no checkout or registry — dispatch before touching either.
    if args.cmd == "completion":
        return cmd_completion(args.shell)

    if args.cmd == "env" and args.env_cmd in {None, "get"}:
        return _dispatch_env_inspection(args, state)

    try:
        cwd = _select_command_directory(args, state)
    except (ApplicationError, OSError, KeyboardInterrupt) as error:
        return _render_pre_dispatch_error(state, error)

    if args.cmd == "status":
        return _dispatch_status(args, state, cwd)

    if args.cmd == "ai":
        return _dispatch_ai(args)
    if args.cmd == "hook":
        if args.hook_cmd != "post-checkout":
            parser.error("hook requires an event")
        return cmd_post_checkout_hook(cwd, None, args.old, args.new, args.flag)

    if args.cmd == "init":
        try:
            cmd_init(
                cwd,
                options=InitOptions(overwrite=args.overwrite, env_file=args.env_file),
                loader_override=args.loader,
                output_format=args.format or "text",
            )
            return 0
        except ApplicationError as error:
            return render_application_error(error)
        except (DeviceError, OSError, ValueError) as error:
            return render_untyped_error(error)

    try:
        registry = Registry()
    except (ApplicationError, OSError, KeyboardInterrupt) as error:
        return _render_pre_dispatch_error(state, error)
    _consume_claim_notices(cwd, registry)

    if args.cmd == "trust":
        return cmd_trust(cwd)
    if args.cmd == "untrust":
        return cmd_untrust(cwd)
    if args.cmd == "bootstrap":
        return cmd_bootstrap(cwd, registry, rerun=args.rerun)

    try:
        if args.cmd in ("run", "start", "stop", "destroy"):
            _normalize_device_args(args)

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
