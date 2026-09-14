from __future__ import annotations

import contextlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from .agentdocs import remove_agent_guidance, sync_agent_guidance
from .bootstrap import (
    GitDirs,
    bootstrap_complete,
    clear_bootstrap_completion,
    git_dirs,
    is_trusted,
    is_worktree_creation,
    lifecycle_active,
    lifecycle_environment,
    lifecycle_lock,
    mark_bootstrap_complete,
    record_trust,
    revoke_trust,
    trusted_execution,
)
from .catalog import PROFILES
from .cli_output import render_env_list, render_status, render_sync
from .constants import ENV_FILE_NAME, ENV_NAME_RE, LOCAL_NAME, RECIPE_NAME
from .device_claims import claim_available_target
from .devices import DeviceError, device_destroy_row
from .errors import ApplicationError, MissingRecipeError, UsageError
from .hooks import (
    _activate_post_checkout_hook,
    _configure_post_checkout_hook,
    _ensure_gitignore,
    _git_worktree_root,
    _nested_project,
    _nested_worktree,
    _print_nested_checkout_hook_note,
    _revert_gitignore,
)
from .inventory import ProjectInventory
from .loaders import LOADERS, NOTHING, Loader, WirePlan, apply_wire_plan
from .provisioning import (
    DEFAULT_WRITER,
    WriterResult,
    clear_writer_destinations,
    existing_managed_keys,
    provision,
    resolve_writer,
    run_bootstrap,
    run_setup,
    write_outputs,
)
from .recipe import (
    LOCAL_SKELETON,
    Recipe,
    _slug,
    validate_env_file_option,
)
from .registry import Registry
from .scanner import (
    LoaderSelection,
    Scanner,
    _build_resource_catalog,
    _merge_app_targets,
    _prune_unresolvable_templates,
    _should_defer_monorepo,
    select_loader,
)
from .status import build_status_report
from .target_commands import cmd_destroy as cmd_destroy  # noqa: PLC0414
from .target_commands import cmd_gc as cmd_gc  # noqa: PLC0414
from .target_commands import cmd_run as cmd_run  # noqa: PLC0414
from .target_commands import cmd_start as cmd_start  # noqa: PLC0414
from .target_commands import cmd_stop as cmd_stop  # noqa: PLC0414
from .target_commands import cmd_target_gc as cmd_target_gc  # noqa: PLC0414
from .target_commands import cmd_target_prune as cmd_target_prune  # noqa: PLC0414
from .target_commands import cmd_target_refresh as cmd_target_refresh  # noqa: PLC0414
from .target_commands import cmd_targets_list as cmd_targets_list  # noqa: PLC0414
from .targets import _load_recipe_or_empty
from .wiring import WiringCheck


def _create_local_skeleton(cwd: Path) -> bool:
    path = cwd / LOCAL_NAME
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    for _ in range(2):
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            try:
                entry = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(entry.st_mode):
                raise ValueError(
                    f"refusing to write `{path}`: destination is not a regular file"
                ) from None
            return False
        except OSError as error:
            raise ValueError(f"could not safely create `{path}`: {error}") from error

        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError(f"refusing to write `{path}`: destination is not a regular file")
            file = os.fdopen(fd, "w", encoding="utf-8", newline="")
            fd = -1
            with file:
                file.write(LOCAL_SKELETON)
        except (OSError, ValueError) as error:
            if fd >= 0:
                os.close(fd)
            if isinstance(error, ValueError):
                raise
            raise ValueError(f"could not safely create `{path}`: {error}") from error
        return True
    raise ValueError(f"could not safely create `{path}` because it changed during inspection")


def cmd_status(  # noqa: PLR0913 — compatibility wrapper mirrors CLI status options
    cwd: Path,
    registry: Registry,
    fmt: str,
    *,
    show_all: bool = False,
    check: bool = False,
    verbose: bool = False,
    show_values: bool = False,
) -> int:
    detailed = fmt == "json" or show_values or not (show_all and not verbose)
    report = build_status_report(
        cwd,
        registry,
        show_all=show_all,
        check=check,
        detailed=detailed,
    )
    render_status(report, fmt, verbose=verbose, show_values=show_values)
    return 0


_COMPLETION_SHELLS = ("bash", "zsh")


def _detect_shell() -> str:
    """Shell basename from $SHELL, for `splash completion` with no argument."""
    return Path(os.environ.get("SHELL", "")).name or "bash"


def cmd_completion(shell: str | None) -> int:
    """Print shell-completion registration for `eval "$(splash completion)"`.

    splash bundles argcomplete, so this emits the full shellcode itself: no
    separately-installed `register-python-argcomplete`, and for zsh no
    `bashcompinit`. Autodetects the shell from $SHELL when not given."""
    shell = shell or _detect_shell()
    if shell not in _COMPLETION_SHELLS:
        print(
            f"splash completion: unsupported shell {shell!r} "
            f"(supported: {', '.join(_COMPLETION_SHELLS)})",
            file=sys.stderr,
        )
        return 2
    import argcomplete  # noqa: PLC0415

    # argcomplete doesn't export shellcode in its typed surface (autocomplete is).
    code = argcomplete.shellcode(["splash"], shell=shell)  # type: ignore[attr-defined]
    print(code)
    return 0


def _path_git_ignored(cwd: Path, name: str) -> bool:
    """True if `name` is gitignored in `cwd`. Best-effort: any git error counts
    as ignored so we never nag spuriously (e.g. outside a repo)."""
    try:
        r = subprocess.run(
            ["git", "check-ignore", "-q", "--", name],
            cwd=cwd,
            capture_output=True,
            check=False,
        )
    except OSError:
        return True
    # Exit 1 is the only result that confirms the path is not ignored.
    return r.returncode != 1


def _persisted_env_file(env_file: str) -> str | None:
    """The recipe records a destination only when it is not the default."""
    return None if env_file == ENV_FILE_NAME else env_file


def _print_env_destination(cwd: Path, env_file: str, keys: list[str], loader: str) -> None:
    """Report the destination, the keys init will manage there, and any key the
    file already assigns. Values are never read back out."""
    print(f"  env output\t→ {env_file}", file=sys.stderr)
    if keys:
        print(f"  manages\t→ {', '.join(keys)}", file=sys.stderr)
    present = existing_managed_keys(cwd / env_file, set(keys), root=cwd)
    if present:
        print(
            f"  {env_file} already sets {', '.join(present)}; "
            "`splash sync` will replace those values",
            file=sys.stderr,
        )
    # `_ensure_gitignore` adds a rule for the default destination moments later.
    if _persisted_env_file(env_file) is not None and not _path_git_ignored(cwd, env_file):
        print(
            f"  warning: {env_file} is not gitignored — per-checkout values "
            "will show up as local changes",
            file=sys.stderr,
        )
    if loader == "none":
        print(
            f"  no shell loader — {env_file} is generated but nothing sources it.\n"
            "  re-run `splash init --loader mise|direnv|devbox` to add one, or source it "
            f"yourself (e.g. `set -a; . ./{env_file}; set +a`)",
            file=sys.stderr,
        )


def _write_minimal_monorepo_recipe(
    cwd: Path,
    inv: ProjectInventory,
    worktree_root: Path | None,
    plan: WirePlan,
    report: InitReport,
    *,
    wire_checkout_hook: bool,
) -> None:
    """Write a structure-only recipe for an ambiguous monorepo and configure its integrations."""
    from .tomlio import render_scanned_recipe  # noqa: PLC0415

    recipe_path = cwd / RECIPE_NAME
    _print_env_destination(cwd, report.env_file, [], inv.loader)
    persisted = _persisted_env_file(report.env_file)
    rendered = render_scanned_recipe(
        inv, {}, {}, cwd, project_metadata={"env_file": persisted} if persisted else None
    )
    Recipe.parse(rendered, recipe_path)
    _write_init_recipe(recipe_path, rendered)
    report.changed.append(RECIPE_NAME)
    print(f"wrote {RECIPE_NAME} (structure only)", file=sys.stderr)
    print(
        f"monorepo detected ({len(inv.apps)} apps) — resources not auto-configured; "
        "see https://splashdown.dev/monorepos/",
        file=sys.stderr,
    )
    if _create_local_skeleton(cwd):
        report.changed.append(LOCAL_NAME)
        print(f"wrote {LOCAL_NAME} (skeleton)", file=sys.stderr)
    _ensure_gitignore(cwd)
    _commit_loader_plan(plan, report)
    _wire_init_checkout_hook(cwd, enabled=wire_checkout_hook)
    sync_agent_guidance(cwd, Recipe.load(recipe_path))
    _print_init_next_steps(cwd, worktree_root, report.env_file)


_ELECTRON_PROFILE_RESOURCE = "ELECTRON_PROFILE_ID"


def _add_electron_resources(
    _cwd: Path,
    inv: ProjectInventory,
    res_by_app: dict[str, dict[str, dict[str, Any]]],
    choice: str | None = None,
) -> bool:
    electron_apps = [app for app in inv.apps if "electron" in app.capabilities]
    if choice not in (None, "isolated", "shared"):
        raise ValueError("electron profile choice must be `isolated` or `shared`")
    if not electron_apps:
        if choice is not None:
            raise ValueError("--electron-profile requires a scanner-detected Electron app")
        return False
    if choice == "shared":
        return False
    if choice is None:
        if not sys.stdin.isatty():
            return False
        if len(electron_apps) == 1:
            prompt = "Set up an independent Electron profile for this checkout?"
        else:
            names = ", ".join(app.name for app in electron_apps)
            prompt = f"Set up independent Electron profiles for these checkouts ({names})?"
        print(f"{prompt} [y/N] ", end="", file=sys.stderr, flush=True)
        try:
            answer = input()
        except EOFError:
            return False
        if answer.strip().lower() not in ("y", "yes"):
            return False
    multiple = len(electron_apps) > 1
    for app in electron_apps:
        template = "splashdown-{{ truncate(hash(cwd_abs), 12) }}"
        if multiple:
            template = f"{template}-{_slug(app.name)}"
        res_by_app[app.name][_ELECTRON_PROFILE_RESOURCE] = {
            "type": "template",
            "template": template,
        }
    return True


def _print_electron_integration(resource_names: list[str]) -> None:
    print(
        "  Electron: in each main process, before requestSingleInstanceLock():",
        file=sys.stderr,
    )
    print('    import { mkdirSync } from "node:fs"', file=sys.stderr)
    for resource in resource_names:
        print(f"    const profileId = process.env.{resource}", file=sys.stderr)
        print("    if (profileId) {", file=sys.stderr)
        print('      const userData = `${app.getPath("userData")}-${profileId}`', file=sys.stderr)
        print("      mkdirSync(userData, { recursive: true })", file=sys.stderr)
        print('      app.setPath("userData", userData)', file=sys.stderr)
        print("    }", file=sys.stderr)


def _ios_native_schemes(cwd: Path) -> list[str]:
    from .runners import _ios_native_schemes as discover  # noqa: PLC0415

    return discover(cwd)


def _resolve_init_ios_scheme(inv: ProjectInventory, explicit: str | None) -> str | None:
    ios_apps = [app for app in inv.apps if app.profile == "ios-native"]
    if not ios_apps:
        if explicit is not None:
            raise ValueError("--ios-scheme requires a scanner-detected native iOS app")
        return None
    if len(ios_apps) != 1:
        raise DeviceError("ios-native: select a single app before choosing its Xcode scheme")
    if explicit is not None:
        scheme = explicit.strip()
        if not scheme:
            raise ValueError("--ios-scheme must not be empty")
        if scheme.startswith("-"):
            raise ValueError("--ios-scheme must not start with `-`")
        return scheme

    schemes = _ios_native_schemes(ios_apps[0].path)
    if len(schemes) == 1:
        return schemes[0]
    if not schemes:
        raise DeviceError(
            "ios-native: no shared Xcode schemes found; rerun `splash init --ios-scheme NAME`"
        )

    choices = ", ".join(schemes)
    if not sys.stdin.isatty():
        raise DeviceError(
            f"ios-native: multiple shared Xcode schemes found ({choices}); "
            "rerun `splash init --ios-scheme NAME`"
        )
    print(f"Select native iOS scheme ({choices}): ", end="", file=sys.stderr, flush=True)
    try:
        selected = input().strip()
    except EOFError as exc:
        raise DeviceError(
            "ios-native: no Xcode scheme selected; rerun `splash init --ios-scheme NAME`"
        ) from exc
    if selected not in schemes:
        raise DeviceError(
            f"ios-native: unknown Xcode scheme `{selected}`; choose one of: {choices}"
        )
    return selected


def _resolve_init_android_module(inv: ProjectInventory) -> str | None:
    if inv.workspace != "gradle":
        return None
    apps = [app for app in inv.apps if app.profile == "android-native"]
    if len(apps) != 1 or apps[0].project_path is None:
        return None
    parts = apps[0].project_path.parts
    return ":".join(parts) if parts and parts != (".",) else None


def _resolve_init_project_metadata(
    inv: ProjectInventory, ios_scheme: str | None, env_file: str
) -> dict[str, str | dict[str, str]] | None:
    metadata: dict[str, str | dict[str, str]] = {}
    if persisted := _persisted_env_file(env_file):
        metadata["env_file"] = persisted
    if resolved_ios_scheme := _resolve_init_ios_scheme(inv, ios_scheme):
        metadata["ios"] = {"scheme": resolved_ios_scheme}
    if resolved_android_module := _resolve_init_android_module(inv):
        metadata["android"] = {"module": resolved_android_module}
    return metadata or None


def _wire_init_checkout_hook(cwd: Path, *, enabled: bool) -> None:
    if enabled:
        _configure_post_checkout_hook(cwd)
        return
    _print_nested_checkout_hook_note(cwd)


def _default_destination_keys(
    merged_resources: dict[str, dict[str, Any]], env_file: str
) -> list[str]:
    """Declared keys the default destination receives: everything without an
    explicit writer, plus anything whose writer already names that same file."""
    default = resolve_writer(DEFAULT_WRITER, env_file)
    return [
        name
        for name, spec in merged_resources.items()
        if resolve_writer(spec.get("writer", DEFAULT_WRITER), env_file) == default
    ]


def _print_init_next_steps(cwd: Path, worktree_root: Path | None, env_file: str) -> None:
    print("configuration written; nothing is allocated or active yet", file=sys.stderr)
    steps = []
    if worktree_root is not None:
        detail = (
            "authorize automatic environment output"
            if _nested_worktree(cwd, worktree_root)
            else "activate automatic post-checkout handling"
        )
        steps.append(f"run `splash trust` to {detail}")
    steps.append(f"run `splash` to allocate values and write {env_file}")
    for index, step in enumerate(steps):
        print(f"{'next:' if index == 0 else '     '} {step}", file=sys.stderr)


@dataclass(frozen=True)
class InitOptions:
    overwrite: bool = False
    env_file: str | None = None


@dataclass
class InitReport:
    """What init configured: the loader it chose, why, and the files it wrote."""

    selection: LoaderSelection
    loader_status: str
    env_file: str = ENV_FILE_NAME
    managed_keys: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    failure: str | None = None

    def as_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "command": "init",
            "ok": self.failure is None,
            "loader": {
                "name": self.selection.name,
                "selected_by": self.selection.selected_by,
                "reason": self.selection.reason,
                "configs": list(self.selection.configs),
                "wiring": self.loader_status,
            },
            "output": {"env_file": self.env_file, "managed": list(self.managed_keys)},
            "changed": list(self.changed),
        }
        if self.failure is not None:
            payload["error"] = self.failure
        return payload


def _commit_loader_plan(plan: WirePlan, report: InitReport) -> None:
    apply_wire_plan(plan)
    if plan.writes and plan.path is not None:
        report.changed.append(plan.path.name)
    for line in (plan.note, plan.hint):
        if line:
            print(line, file=sys.stderr)


def _emit_init_report(report: InitReport, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(report.as_json(), indent=2))
    elif report.changed:
        print(f"changed: {', '.join(report.changed)}", file=sys.stderr)


@contextlib.contextmanager
def _init_effects(report: InitReport, output_format: str) -> Iterator[None]:
    """Report what init completed, whether or not a later effect fails."""
    try:
        yield
    except (ApplicationError, ValueError, OSError) as error:
        report.failure = str(error)
        _emit_init_report(report, output_format)
        raise
    _emit_init_report(report, output_format)


@contextlib.contextmanager
def _init_failure(report: InitReport, output_format: str) -> Iterator[None]:
    """Give a failure raised before the effects phase the same report shape a
    later one gets, so a consumer never faces two failure envelopes."""
    try:
        yield
    except (ApplicationError, ValueError, OSError) as error:
        if report.failure is None:
            report.failure = str(error)
            _emit_init_report(report, output_format)
        raise


def _init_recipe_mode(path: Path) -> int | None:
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise UsageError(f"could not inspect `{path}`: {error}") from error
    if not stat.S_ISREG(entry.st_mode):
        raise UsageError(f"refusing to use `{path}`: destination is not a regular file")
    return stat.S_IMODE(entry.st_mode)


def _init_recipe_exists(path: Path) -> bool:
    return _init_recipe_mode(path) is not None


def _write_init_recipe(path: Path, text: str) -> None:
    mode = _init_recipe_mode(path)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as file:
            os.fchmod(file.fileno(), mode if mode is not None else 0o644)
            file.write(text)
        os.replace(temp_path, path)
    except OSError as error:
        raise ValueError(f"could not safely write `{path}`: {error}") from error
    finally:
        temp_path.unlink(missing_ok=True)


def _print_init_scan(cwd: Path, inv: ProjectInventory, selection: LoaderSelection) -> None:
    print("scanning project…", file=sys.stderr)
    print(
        f"  detected: {inv.workspace} ({'/'.join(a.name for a in inv.apps) or 'no apps'})",
        file=sys.stderr,
    )
    for app in inv.apps:
        rel = app.path.relative_to(cwd) if app.path != cwd else Path(".")
        print(f"  {rel}\t→ {app.profile}", file=sys.stderr)
    print(f"  shell loader\t→ {inv.loader} ({selection.reason})", file=sys.stderr)


def cmd_init(
    cwd: Path,
    options: InitOptions | None = None,
    loader_override: str | None = None,
    electron_profile: str | None = None,
    ios_scheme: str | None = None,
    output_format: str = "text",
) -> InitReport:
    """Scaffold splashdown.toml from a project scan."""

    options = options or InitOptions()
    report = InitReport(
        selection=LoaderSelection("none", "unconfigured", ()),
        loader_status=NOTHING,
    )
    with _init_failure(report, output_format):
        recipe_path = cwd / RECIPE_NAME
        recipe_exists = _init_recipe_exists(recipe_path)
        worktree_root = _git_worktree_root(cwd)
        nested = _nested_worktree(cwd, worktree_root)
        if recipe_exists and not options.overwrite:
            raise UsageError(f"refusing to overwrite existing {RECIPE_NAME} (use --overwrite)")

        env_file = (
            validate_env_file_option(options.env_file, cwd)
            if options.env_file is not None
            else ENV_FILE_NAME
        )
        report.env_file = env_file

        selection = select_loader(cwd, loader_override)
        inv = Scanner().scan(cwd, loader=selection.name)
        report.selection = selection
        wire_plan = LOADERS[selection.name].plan(cwd, env_file)
        report.loader_status = wire_plan.status

        _print_init_scan(cwd, inv, selection)
        res_by_app: dict[str, dict[str, dict[str, Any]]] = {}
        for app in inv.apps:
            if app.profile == "unknown":
                res_by_app[app.name] = {}
                continue
            res_by_app[app.name] = PROFILES[app.profile].resources(app)
        if _should_defer_monorepo(cwd, res_by_app, inv.apps):
            with _init_effects(report, output_format):
                _write_minimal_monorepo_recipe(
                    cwd, inv, worktree_root, wire_plan, report, wire_checkout_hook=not nested
                )
            return report
        electron_isolated = _add_electron_resources(cwd, inv, res_by_app, electron_profile)
        merged_resources, app_resource_names = _build_resource_catalog(res_by_app)
        # Compose is project-level infrastructure, so its resources are merged in after
        # the per-app pass rather than claimed by any one app.
        from .profiles import compose_project_resources  # noqa: PLC0415

        for name, spec in compose_project_resources(cwd).items():
            merged_resources.setdefault(name, spec)
        merged_targets = _merge_app_targets(inv.apps)
        for name in _prune_unresolvable_templates(merged_resources, app_resource_names):
            print(
                f"  skipped {name}: template references a resource no app declares", file=sys.stderr
            )

        project_metadata = _resolve_init_project_metadata(inv, ios_scheme, env_file)
        report.managed_keys = _default_destination_keys(merged_resources, env_file)
        _print_env_destination(cwd, env_file, report.managed_keys, inv.loader)

        from .tomlio import render_scanned_recipe  # noqa: PLC0415

        rendered = render_scanned_recipe(
            inv,
            merged_resources,
            app_resource_names,
            cwd,
            merged_targets,
            project_metadata,
        )
        Recipe.parse(rendered, recipe_path)
        _write_init_recipe(recipe_path, rendered)
        report.changed.append(RECIPE_NAME)
        print(f"wrote {RECIPE_NAME}", file=sys.stderr)

        with _init_effects(report, output_format):
            if _create_local_skeleton(cwd):
                report.changed.append(LOCAL_NAME)
                print(f"wrote {LOCAL_NAME} (skeleton)", file=sys.stderr)

            _ensure_gitignore(cwd)
            _commit_loader_plan(wire_plan, report)
            _wire_init_checkout_hook(cwd, enabled=not nested)
            if electron_isolated:
                resource_names = [
                    name
                    for app in inv.apps
                    if "electron" in app.capabilities
                    for name in app_resource_names[app.name]
                    if name.startswith(_ELECTRON_PROFILE_RESOURCE)
                ]
                _print_electron_integration(resource_names)

            if any(app.profile != "unknown" for app in inv.apps):
                _apply_init_wiring_checks(inv, env_file)
            sync_agent_guidance(cwd, Recipe.load(recipe_path))
        _print_init_next_steps(cwd, worktree_root, env_file)
        return report


def _apply_init_wiring_checks(inv: ProjectInventory, env_file: str) -> None:
    """Apply the project-configuration wiring checks for every known-profile app
    found during init. Activation checks belong to `splash trust` and `splash doctor`."""
    for app in inv.apps:
        if app.profile == "unknown":
            continue
        checks = PROFILES[app.profile].wiring_checks(app)
        for check in checks:
            if check.activation or not check.applies(app.path):
                continue
            status, _ = check.detect(app.path)
            if status != "ok" and check.autofix is not None:
                try:
                    check.autofix(app.path)
                except Exception as e:  # noqa: BLE001
                    print(f"  ✗ {check.id}: autofix failed: {e}", file=sys.stderr)
            _warn_wiring_reads_default_destination(check, env_file)


# Checks whose fix writes a hardcoded `splashdown.env` path; `wiring.py` owns the checks.
_DEFAULT_DESTINATION_WIRING_CHECKS = frozenset({"rn-xcode-env"})


def _warn_wiring_reads_default_destination(check: WiringCheck, env_file: str) -> None:
    """A framework patch that names `splashdown.env` reads a file this checkout
    does not write once the destination is configured elsewhere."""
    if env_file == ENV_FILE_NAME or check.id not in _DEFAULT_DESTINATION_WIRING_CHECKS:
        return
    print(
        f"  note: {check.id} wires a fixed {ENV_FILE_NAME} path, which this checkout does not "
        f"write; repoint that configuration at {env_file} yourself",
        file=sys.stderr,
    )


def cmd_deinit(cwd: Path, registry: Registry) -> int:
    if _reject_nested_lifecycle():
        return 1
    with (
        lifecycle_lock(cwd, require_git=False) as dirs,
        registry.operation_lock(str(cwd.resolve())),
    ):
        return _cmd_deinit_locked(cwd, registry, dirs)


def _cmd_deinit_locked(cwd: Path, registry: Registry, dirs: GitDirs | None) -> int:
    """Remove local state; preserve clone trust, shared hooks, and framework patches."""
    abspath = str(cwd.resolve())

    # Read the loader before deleting the recipe; parse failures must not block teardown.
    try:
        recipe = _load_recipe_or_empty(cwd)
        loader_name = recipe.project.get("loader")
    except Exception as e:  # noqa: BLE001 — a recipe we can't parse must not block teardown
        print(
            f"warning: could not read {RECIPE_NAME} ({e}); skipping loader un-wiring",
            file=sys.stderr,
        )
        recipe = None
        loader_name = None

    # Use registry identifiers so undeclared or orphaned managed devices are destroyed too.
    for row in registry.devices_for(abspath):
        try:
            device_destroy_row(row)
            print(f"destroyed {row.dtype}.{row.variant} ({row.identifier})", file=sys.stderr)
        except DeviceError as e:
            print(f"warning: could not destroy {row.dtype}.{row.variant}: {e}", file=sys.stderr)

    # The registry rows name every key splashdown wrote here, including ones the
    # recipe stopped declaring; read them before release drops them.
    written_keys = set(registry.all_for(abspath))
    removed = registry.release(abspath)
    if removed:
        print(f"released {removed} registry entr{'y' if removed == 1 else 'ies'}", file=sys.stderr)

    # Every writer destination, the default one included, holds splashdown's own
    # keys inside a file it does not own wholesale: remove those keys and delete
    # the file only when nothing else remains.
    if recipe is not None:
        for relpath, action in clear_writer_destinations(cwd, recipe, known_keys=written_keys):
            if action == "unparsed":
                print(
                    f"warning: left {relpath} alone; it opens a quoted value that is never "
                    "closed, so splashdown cannot tell which lines are its own",
                    file=sys.stderr,
                )
            else:
                print(f"{action} {relpath}", file=sys.stderr)
    elif (cwd / ENV_FILE_NAME).exists():
        (cwd / ENV_FILE_NAME).unlink()
        print(f"removed {ENV_FILE_NAME}", file=sys.stderr)

    loader = LOADERS.get(loader_name) if loader_name else None
    if loader is not None:
        loader.unwire(cwd, recipe.env_file if recipe is not None else ENV_FILE_NAME)

    _revert_gitignore(cwd)
    remove_agent_guidance(cwd)

    # Only remove splashdown.local.toml when it's still the untouched skeleton.
    local_path = cwd / LOCAL_NAME
    if local_path.exists():
        if local_path.read_text() == LOCAL_SKELETON:
            local_path.unlink()
            print(f"removed {LOCAL_NAME}", file=sys.stderr)
        else:
            print(f"note: {LOCAL_NAME} was modified — left in place", file=sys.stderr)

    recipe_path = cwd / RECIPE_NAME
    if recipe_path.exists():
        recipe_path.unlink()
        print(f"removed {RECIPE_NAME}", file=sys.stderr)

    if dirs is not None and clear_bootstrap_completion(dirs):
        print("removed this checkout's bootstrap completion", file=sys.stderr)

    print("splashdown removed from this checkout", file=sys.stderr)
    return 0


def _cmd_provision(args: Any, cwd: Path, registry: Registry) -> int:
    return _cmd_provision_inner(
        cwd,
        registry,
        reprovision=args.force,
        setup=args.setup,
        fmt=_resolve_format_arg(args),
        show_values=getattr(args, "show_values", False),
    )


def _resolve_format_arg(args: Any) -> str:
    return getattr(args, "format", None) or "text"


class _ProvisionResult(NamedTuple):
    resolved: dict[str, str]
    writers: list[WriterResult]
    setup: list[str]
    changed: dict[str, str]


def _load_required_recipe(cwd: Path) -> Recipe:
    path = cwd / RECIPE_NAME
    if not path.exists():
        raise FileNotFoundError(f"no {RECIPE_NAME} in {cwd}; run `splash init`")
    return Recipe.load(path)


def _provision_locked(
    cwd: Path,
    registry: Registry,
    recipe: Recipe,
    *,
    reprovision: bool = False,
    setup: str | None = None,
) -> _ProvisionResult:
    abspath = str(cwd.resolve())
    with registry.operation_lock(abspath):
        before = registry.all_for(abspath)
        resolved = provision(
            cwd,
            registry=registry,
            reprovision=reprovision,
            recipe=recipe,
        )
        _create_local_skeleton(cwd)
        writers = write_outputs(cwd, recipe, resolved, known_keys=set(before))
    setup_messages = run_setup(
        cwd,
        recipe,
        setup,
        resolved,
        extra_env=lifecycle_environment(),
    )
    changed = {key: value for key, value in resolved.items() if before.get(key) != value}
    return _ProvisionResult(resolved, writers, setup_messages, changed)


def _emit_provision(
    result: _ProvisionResult,
    *,
    fmt: str,
    show_values: bool = False,
) -> None:
    render_sync(
        result.resolved,
        result.writers,
        result.setup,
        list(result.changed),
        fmt,
        show_values=show_values,
    )


def _cmd_provision_inner(
    cwd: Path,
    registry: Registry,
    *,
    reprovision: bool = False,
    setup: str | None = None,
    fmt: str = "text",
    show_values: bool = False,
) -> int:
    if _reject_nested_lifecycle():
        return 1
    try:
        with lifecycle_lock(cwd, require_git=False):
            recipe = _load_required_recipe(cwd)
            result = _provision_locked(
                cwd,
                registry,
                recipe,
                reprovision=reprovision,
                setup=setup,
            )
    except FileNotFoundError as error:
        raise MissingRecipeError(str(error)) from error
    _emit_provision(result, fmt=fmt, show_values=show_values)
    return 0


def _reject_nested_lifecycle() -> bool:
    if not lifecycle_active():
        return False
    print(
        "error: recipe commands may not invoke splashdown lifecycle commands",
        file=sys.stderr,
    )
    return True


def _bootstrap_commands(recipe: Recipe) -> tuple[str, ...]:
    if recipe.bootstrap is None:
        raise ValueError("recipe has no [bootstrap] section")
    return recipe.bootstrap.commands


def _splashdown_owned_loader(cwd: Path, recipe: Recipe) -> Loader | None:
    """Return the loader whose configuration holds nothing but splashdown's
    integration. Configuration carrying anything else is the user's to approve."""
    loader = LOADERS.get(str(recipe.project.get("loader") or "none"))
    if loader is None or not loader.owns_config(cwd, recipe.env_file):
        return None
    return loader


def _print_trust_preamble(cwd: Path, recipe: Recipe) -> Loader | None:
    """Print what trust authorizes and return the loader configuration it approves."""
    if recipe.bootstrap is not None:
        print("bootstrap commands:", file=sys.stderr)
        for index, command in enumerate(recipe.bootstrap.commands, start=1):
            print(f"  {index}. {json.dumps(command, ensure_ascii=True)}", file=sys.stderr)
    else:
        print("bootstrap commands: none", file=sys.stderr)
    warning = (
        "warning: trusting this clone permits current and future refs to write automatic "
        "environment output"
    )
    if recipe.bootstrap is not None:
        warning += " and run declared bootstrap commands with your user permissions"
    print(warning, file=sys.stderr)
    owned_loader = _splashdown_owned_loader(cwd, recipe)
    if owned_loader is not None:
        print(
            f"warning: trust also approves the {owned_loader.name} configuration splashdown "
            f"wired, so {owned_loader.approval_detail}",
            file=sys.stderr,
        )
    return owned_loader


def cmd_trust(cwd: Path) -> int:
    if _reject_nested_lifecycle():
        return 1
    try:
        dirs = git_dirs(cwd)
        recipe = _load_required_recipe(cwd)
    except (FileNotFoundError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    bootstrap_was_trusted = is_trusted(dirs)
    owned_loader = _print_trust_preamble(cwd, recipe)
    if recipe.bootstrap is None and bootstrap_was_trusted:
        print(
            "bootstrap execution remains authorized from earlier trust; "
            "run `splash untrust` to revoke it",
            file=sys.stderr,
        )
    elif recipe.bootstrap is None:
        print(
            "bootstrap execution is not authorized; adding [bootstrap] requires `splash trust`",
            file=sys.stderr,
        )
    if _nested_project(cwd):
        automatic = False
        _print_nested_checkout_hook_note(cwd)
    else:
        try:
            automatic = _activate_post_checkout_hook(cwd)
        except OSError as error:
            automatic = False
            print(f"note: could not activate automatic handling: {error}", file=sys.stderr)
    try:
        record_trust(dirs, bootstrap=recipe.bootstrap is not None)
    except OSError as error:
        print(f"error: could not record trust: {error}", file=sys.stderr)
        return 1
    print("trusted this clone for automatic splashdown handling", file=sys.stderr)
    if owned_loader is not None:
        owned_loader.approve(cwd, announce=True, env_file=recipe.env_file)
    if not automatic:
        print("automatic post-checkout handling is not active for this checkout", file=sys.stderr)
    if recipe.bootstrap is not None:
        print("next: run `splash bootstrap`", file=sys.stderr)
    return 0


def cmd_untrust(cwd: Path) -> int:
    if _reject_nested_lifecycle():
        return 1
    try:
        dirs = git_dirs(cwd)
        existed = revoke_trust(dirs)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if existed:
        print("revoked automatic splashdown trust for this clone", file=sys.stderr)
    else:
        print("this clone was not trusted for automatic splashdown handling", file=sys.stderr)
    return 0


def cmd_bootstrap(cwd: Path, registry: Registry | None = None, *, rerun: bool) -> int:
    if _reject_nested_lifecycle():
        return 1
    try:
        initial_dirs = git_dirs(cwd)
        initial_recipe = _load_required_recipe(cwd)
        _bootstrap_commands(initial_recipe)
        if not is_trusted(initial_dirs):
            raise ValueError("clone is not trusted; review the recipe and run `splash trust`")
        registry = registry or Registry()
        with lifecycle_lock(cwd, require_git=True) as dirs:
            if dirs is None:
                raise ValueError("bootstrap trust requires a Git checkout")
            with trusted_execution(dirs) as trusted:
                if not trusted.bootstrap:
                    raise ValueError(
                        "clone is not trusted; review the recipe and run `splash trust`"
                    )
                recipe = _load_required_recipe(cwd)
                _bootstrap_commands(recipe)
                result = _provision_locked(cwd, registry, recipe)
                try:
                    complete = bootstrap_complete(dirs)
                except ValueError:
                    if not rerun:
                        raise
                    complete = False
                if complete and not rerun:
                    _emit_provision(result, fmt="text")
                    print(
                        "splashdown: bootstrap already complete; use `--rerun` to run it again",
                        file=sys.stderr,
                    )
                    return 0
                messages = run_bootstrap(
                    cwd,
                    recipe,
                    result.resolved,
                    extra_env=lifecycle_environment(),
                )
                mark_bootstrap_complete(dirs)
    except (FileNotFoundError, OSError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        retry = "splash bootstrap --rerun" if rerun else "splash bootstrap"
        print(f"retry with `{retry}` after fixing the problem", file=sys.stderr)
        return 1
    _emit_provision(result, fmt="text")
    for message in messages:
        print(f"  -> {message}", file=sys.stderr)
    print("splashdown: bootstrap complete", file=sys.stderr)
    return 0


def _post_checkout_messages(
    cwd: Path,
    registry: Registry | None,
    dirs: GitDirs | None,
    old: str,
    new: str,
    flag: str,
) -> list[str] | None:
    recipe = _load_required_recipe(cwd)
    if dirs is None:
        return None
    with trusted_execution(dirs) as trusted:
        if not trusted.sync:
            print(
                "splashdown: automatic handling skipped because this clone is untrusted; "
                "review the recipe and run `splash trust`",
                file=sys.stderr,
            )
            return None
        registry = registry or Registry()
        result = _provision_locked(cwd, registry, recipe)
        _emit_provision(result, fmt="text")
        created = is_worktree_creation(dirs, old, new, flag)
        if not created:
            return None
        messages = None
        if recipe.bootstrap is not None:
            if not trusted.bootstrap:
                print(
                    "splashdown: bootstrap skipped because its commands are not trusted; "
                    "review them and run `splash trust`, then `splash bootstrap`",
                    file=sys.stderr,
                )
                return None
            if not bootstrap_complete(dirs):
                messages = run_bootstrap(
                    cwd,
                    recipe,
                    result.resolved,
                    extra_env=lifecycle_environment(),
                )
                mark_bootstrap_complete(dirs)
        policy = recipe.project.get("worktree", {}).get("claim_device")
        if policy is not None:
            with registry.operation_lock(str(cwd.resolve())):
                try:
                    selection = claim_available_target(registry, cwd, policy, timeout=5)
                except DeviceError:
                    print(
                        f"no physical device claimed; retry: "
                        f"splash target claim --available {policy}",
                        file=sys.stderr,
                    )
                else:
                    print(f"claimed physical device: {selection.target.variant}", file=sys.stderr)
        return messages


def cmd_post_checkout_hook(
    cwd: Path,
    registry: Registry | None,
    old: str,
    new: str,
    flag: str,
) -> int:
    if _reject_nested_lifecycle():
        return 1
    try:
        with lifecycle_lock(cwd, require_git=False) as dirs:
            messages = _post_checkout_messages(cwd, registry, dirs, old, new, flag)
    except FileNotFoundError:
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        print("retry with `splash bootstrap` after fixing the problem", file=sys.stderr)
        return 1
    if messages is None:
        return 0
    for message in messages:
        print(f"  -> {message}", file=sys.stderr)
    print("splashdown: bootstrap complete", file=sys.stderr)
    return 0


def _env_set(assignment: str, target: str, registry: Registry) -> int:
    """`splash env set KEY=VALUE` — persist a value into the registry kv store."""
    if "=" not in assignment:
        raise UsageError("usage: splash env set KEY=VALUE")
    key, value = assignment.split("=", 1)
    if not ENV_NAME_RE.match(key):
        raise UsageError(f"invalid env name `{key}` (must match {ENV_NAME_RE.pattern})")
    recipe_path = Path(target) / RECIPE_NAME
    if not recipe_path.exists():
        raise UsageError(
            f'no {RECIPE_NAME} in {target}; declare `{key}` as a type="set" resource',
        )
    try:
        resources = Recipe.load(recipe_path).resources
    except (OSError, ValueError) as error:
        raise UsageError(f"could not read {recipe_path}: {error}") from error
    spec = resources.get(key)
    if spec is None:
        raise UsageError(
            f"`{key}` is not a resource in {RECIPE_NAME}; declare it as "
            f'`[resources.{key}]` with type = "set" before setting it'
        )
    if not isinstance(spec, dict):
        raise UsageError(f"`{key}` in {RECIPE_NAME} must be a resource table")
    rtype = spec.get("type")
    if rtype != "set":
        raise UsageError(
            f'`{key}` is type `{rtype}`; only type="set" resources accept manual values'
        )
    registry.remove_port(target, key)
    registry.set_kv(target, key, value)
    print(f"set {key}", file=sys.stderr)
    return 0


def _env_dispatch(args: Any, cwd: Path, registry: Registry) -> int:
    """`splash env …` — this checkout's resolved values. Bare = list."""
    fmt = _resolve_format_arg(args)
    if args.env_cmd is None:
        target = str(Path(args.checkout).resolve()) if args.checkout else str(cwd)
        data = registry.all_for(target)
        render_env_list(data, target, fmt, show_values=getattr(args, "show_values", False))
        return 0
    # Normalize the same way provision() keys the registry (str(cwd.resolve())),
    # or get/set/release silently miss each other on symlinked/relative invocations.
    # --checkout targets another checkout's entries (default: this one).
    target = str(Path(args.checkout).resolve()) if args.checkout else str(cwd.resolve())
    if args.env_cmd == "get":
        value = registry.all_for(target).get(args.key)
        if value is None:
            return 1
        print(value)
        return 0
    if args.env_cmd == "set":
        with registry.operation_lock(target):
            return _env_set(args.assignment, target, registry)
    if args.env_cmd == "release":
        with registry.operation_lock(target):
            if args.key:
                registry.remove_kv(target, args.key)
                registry.remove_port(target, args.key)
                print(f"released {args.key}", file=sys.stderr)
            else:
                n = registry.release(target)
                print(f"released {n} entries for {target}", file=sys.stderr)
        return 0
    raise UsageError(f"splash env {args.env_cmd}: unknown action")
