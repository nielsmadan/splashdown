from __future__ import annotations

import errno
import json
import os
import re
import socket
import stat
import subprocess
import tomllib
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

from .constants import LOCAL_NAME, RECIPE_NAME, newline_for, split_lines
from .errors import DeviceError, LoaderConflictError
from .hooks import HookInspection, post_checkout_readiness
from .loaders import LOADERS
from .port_inspection import PortOwner, listening_processes
from .provisioning import (
    DEFAULT_WRITER,
    _destination_lines,
    _merged_lines,
    _reject_ambiguous,
    _scan_assignments,
    _shell_single_quote,
    resolve_writer,
    writer_output_path,
)
from .recipe import (
    GlobalConfig,
    LocalConfig,
    Recipe,
    _env_quote,
    _global_config_path,
    merged_targets,
)
from .registry import RegistrySnapshot
from .results import CommandResult, Diagnostic, JsonObject, JsonValue
from .safe_files import read_optional_editable_text
from .status_targets import TargetObservations
from .targets import target_source


@dataclass(frozen=True)
class ClaimListRow:
    target: str
    source: str
    platform: str
    hardware_id: str
    owner: str
    claimed_at: str


@dataclass(frozen=True)
class TargetInventoryRow:
    type: str
    variant: str
    source: str
    device_name: str
    platform: str
    connection: str
    claim: str
    owner: str


def _check(
    identifier: str, state: str, message: str, *steps: str, path: Path | None = None
) -> JsonObject:
    result: JsonObject = {
        "id": identifier,
        "state": state,
        "message": message,
        "next_steps": list(steps),
    }
    if path is not None:
        result["path"] = str(path)
    return result


def _optional_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except FileNotFoundError:
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        else:
            raise ValueError("dangling configuration link") from None


def _port_in_use(port: int) -> bool:
    observed = False
    for family, address in (
        (socket.AF_INET, ("127.0.0.1", port)),
        (socket.AF_INET6, ("::1", port)),
    ):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as handle:
                handle.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                handle.bind(address)
            observed = True
        except OSError as error:
            if error.errno == errno.EADDRINUSE:
                return True
            if error.errno not in {errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL}:
                raise
    if not observed:
        raise OSError("loopback unavailable")
    return False


class _Inspection:
    def __init__(self) -> None:
        self.targets = TargetObservations()
        self.listeners: dict[int, tuple[PortOwner, ...]] | None = None
        self.listeners_queried = False
        self.port_states: dict[int, bool] = {}

    def resources(
        self, recipe: Recipe | None, values: dict[str, str], checks: list[JsonObject]
    ) -> list[JsonValue]:
        declared = recipe.resources if recipe else {}
        rows: list[JsonValue] = []
        for key in sorted(values.keys() | declared.keys()):
            state = "assigned" if key in values else "unassigned"
            if recipe and key in values and key not in declared:
                state = "undeclared"
            item: JsonObject = {"key": key, "state": state, "port_state": ""}
            if state != "assigned":
                spec = declared.get(key, {})
                step = (
                    f"Run splash env set {key}=VALUE."
                    if state == "unassigned"
                    and spec.get("type") == "set"
                    and spec.get("default") is None
                    else "Run splash sync."
                )
                checks.append(_check(f"resource:{key}", "finding", f"{key}: {state}", step))
            if key in values and declared.get(key, {}).get("type") == "port":
                try:
                    port = int(values[key])
                    low, high = declared[key]["range"]
                    if not low <= port <= high:
                        checks.append(
                            _check(
                                f"resource:{key}:range",
                                "finding",
                                f"{key}: allocation outside declared range",
                                "Run splash sync.",
                            )
                        )
                    if port not in self.port_states:
                        self.port_states[port] = _port_in_use(port)
                    busy = self.port_states[port]
                    item["port_state"] = "in use" if busy else "free"
                    owners: tuple[PortOwner, ...] | None = ()
                    if busy:
                        if not self.listeners_queried:
                            self.listeners = listening_processes()
                            self.listeners_queried = True
                        owners = self.listeners.get(port) if self.listeners is not None else None
                    item["owners"] = (
                        [cast(JsonValue, asdict(owner)) for owner in owners]
                        if owners is not None
                        else None
                    )
                    if owners is None:
                        checks.append(
                            _check(
                                f"resource:{key}:owners",
                                "unavailable",
                                f"{key}: listener ownership unavailable",
                                "Use splash doctor for tool diagnostics.",
                            )
                        )
                except (ValueError, OSError, OverflowError):
                    item["port_state"] = "unavailable"
                    checks.append(
                        _check(
                            f"resource:{key}:port",
                            "unavailable",
                            f"{key}: port observation unavailable",
                        )
                    )
            rows.append(item)
        checks.append(_check("resources", "ok", "Registry resource names inspected."))
        return rows


def _outputs(cwd: Path, recipe: Recipe, values: dict[str, str]) -> list[JsonObject]:
    groups: dict[str, dict[str, str]] = {}
    for key, spec in recipe.resources.items():
        writer = resolve_writer(spec.get("writer", DEFAULT_WRITER), recipe.env_file)
        if writer_output_path(writer) is not None:
            groups.setdefault(writer, {})
            if key in values:
                groups[writer][key] = values[key]
    default = resolve_writer(DEFAULT_WRITER, recipe.env_file)
    groups.setdefault(default, {})
    checks = []
    for writer, items in groups.items():
        relative = writer_output_path(writer)
        if relative is None:
            continue
        path = cwd / relative
        identifier = f"output:{relative}"
        try:
            current = read_optional_editable_text(path, root=cwd)
            if current is None:
                if items:
                    checks.append(
                        _check(
                            identifier,
                            "finding",
                            "Generated output is missing.",
                            "Run splash sync.",
                            path=path,
                        )
                    )
                else:
                    checks.append(
                        _check(
                            identifier,
                            "not_applicable",
                            "No assigned values require this output.",
                            path=path,
                        )
                    )
                continue
            export = writer == "envrc"
            rendered = {
                key: f"export {key}={_shell_single_quote(value)}"
                if export
                else f"{key}={_env_quote(value)}"
                for key, value in items.items()
            }
            managed = set(items)
            if writer == default:
                managed |= {
                    key
                    for key in values
                    if key not in recipe.resources
                    or resolve_writer(
                        recipe.resources[key].get("writer", DEFAULT_WRITER), recipe.env_file
                    )
                    not in {"stdout", "none"}
                }
            lines = _destination_lines(current)
            assignments, problems = _scan_assignments(lines, export=export)
            _reject_ambiguous(path, managed, assignments, problems)
            merged = _merged_lines(lines, assignments, managed, rendered)
            expected = newline_for(current).join(merged) + (newline_for(current) if merged else "")
            checks.append(
                _check(
                    identifier,
                    "ok" if expected == current else "finding",
                    "Output matches stored assignments."
                    if expected == current
                    else "Output differs from stored assignments.",
                    *(() if expected == current else ("Run splash sync.",)),
                    path=path,
                )
            )
        except (OSError, ValueError):
            checks.append(
                _check(
                    identifier,
                    "error",
                    "Output cannot be read or interpreted.",
                    "Inspect the output file and correct its permissions or syntax.",
                    path=path,
                )
            )
    return checks


def _git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
        env={**os.environ, "LC_ALL": "C"},
    )


def _trust_and_hooks(cwd: Path, recipe: Recipe) -> list[JsonObject]:
    try:
        result = _git(cwd, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return [
            _check("trust", "unavailable", "Git trust discovery unavailable."),
            _check("hook", "unavailable", "Git hook discovery unavailable."),
        ]
    if result.returncode:
        if "not a git repository" in result.stderr:
            return [
                _check("trust", "not_applicable", "No Git checkout trust applies."),
                _check("hook", "not_applicable", "No Git post-checkout hook applies."),
            ]
        return [
            _check("trust", "unavailable", "Git trust discovery failed."),
            _check("hook", "unavailable", "Git hook discovery failed."),
        ]
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        return [
            _check("trust", "unavailable", "Git returned an invalid trust location."),
            _check("hook", "unavailable", "Git returned an invalid hook location."),
        ]
    path = common / "splashdown" / "trust-v1.json"
    checks = []
    try:
        text = _optional_text(path)
        saved = (
            json.loads(text)
            if text is not None
            else {"version": 1, "sync": False, "bootstrap": False}
        )
        if (
            not isinstance(saved, dict)
            or saved.get("version") != 1
            or any(type(saved.get(key)) is not bool for key in ("sync", "bootstrap"))
        ):
            raise ValueError("invalid trust record")
        trusted = saved["sync"] and (saved["bootstrap"] or recipe.bootstrap is None)
        checks.append(
            _check(
                "trust",
                "ok" if trusted else "finding",
                f"Saved sync trust: {'trusted' if saved['sync'] else 'untrusted'}; bootstrap trust: {'trusted' if saved['sync'] and saved['bootstrap'] else 'untrusted'}.",
                *(() if trusted else ("Review the recipe, then run splash trust.",)),
                path=path,
            )
        )
    except (OSError, ValueError):
        checks.append(
            _check(
                "trust",
                "error",
                "Saved trust cannot be read or interpreted.",
                "Inspect the saved trust file.",
                path=path,
            )
        )
    try:
        hook_inputs = _validate_hook_inputs(cwd, common)
        readiness = post_checkout_readiness(cwd, strict=hook_inputs)
        ready = readiness.ready and readiness.active
        checks.append(
            _check(
                "hook",
                "ok" if ready else "finding",
                f"Post-checkout integration ({readiness.manager}): {'ready' if ready else 'needs attention'}.",
                *(() if ready else ("Run splash doctor to inspect post-checkout wiring.",)),
            )
        )
    except (OSError, ValueError):
        checks.append(
            _check(
                "hook",
                "error",
                "Hook configuration cannot be read or interpreted.",
                "Inspect hook configuration permissions and syntax.",
            )
        )
    except subprocess.SubprocessError:
        checks.append(
            _check("hook", "unavailable", "Git hook probe unavailable.", "Run splash doctor.")
        )
    return checks


def _validate_hook_inputs(cwd: Path, common: Path) -> HookInspection:
    from .hook_configs import _pre_commit_analysis  # noqa: PLC0415
    from .hooks import _MANAGER_CONFIG_NAMES, detect_hook_configuration  # noqa: PLC0415
    from .yamltext import _yaml_key_regions  # noqa: PLC0415

    configured = _git(cwd, ["config", "--get", "core.hooksPath"])
    if configured.returncode not in {0, 1}:
        raise subprocess.SubprocessError("Git hook configuration probe failed")
    hook_dir = Path(configured.stdout.strip()) if configured.returncode == 0 else common / "hooks"
    if not hook_dir.is_absolute():
        hook_dir = cwd / hook_dir
    with suppress(FileNotFoundError):
        for path in hook_dir.iterdir():
            if not path.name.endswith(".sample") and stat.S_ISREG(path.stat().st_mode):
                path.read_bytes()
    inputs = HookInspection(common, configured.stdout.strip() if configured.returncode == 0 else "")
    detection = detect_hook_configuration(cwd, strict=inputs)
    candidates = _MANAGER_CONFIG_NAMES.get(detection.manager, ())
    for name in candidates:
        path = cwd / name
        text = _optional_text(path)
        if text is None:
            continue
        if path.suffix == ".json" and not isinstance(json.loads(text), dict):
            raise ValueError("expected JSON object")
        if path.suffix == ".toml":
            tomllib.loads(text)
        if path.suffix in {".yml", ".yaml"}:
            if detection.manager in {"pre-commit", "prek"}:
                if _pre_commit_analysis(text)[0] == "unrecognized":
                    raise ValueError("uninterpretable pre-commit configuration")
            elif detection.manager == "lefthook":
                for value in _yaml_key_regions(text, "post-checkout", indent=0):
                    _validate_lefthook_flow(value)
        break
    _optional_text(hook_dir / "post-checkout")
    if detection.manager == "husky":
        _optional_text(cwd / ".husky" / "post-checkout")
    return inputs


def _validate_lefthook_flow(value: str) -> None:
    lines = split_lines(value)
    scalar_indent = -1
    for index, line in enumerate(lines):
        indent = len(line) - len(line.lstrip())
        if scalar_indent >= 0:
            if not line.strip() or indent > scalar_indent:
                continue
            scalar_indent = -1
        match = re.match(r"\s*(?:-\s*)?(?:[^:\n\[{]+:\s*)?([\[{|>])", line)
        if match is None:
            continue
        if match.group(1) in "|>":
            scalar_indent = indent
        elif not _balanced_yaml_flow("\n".join(lines[index:])[match.start(1) :]):
            raise ValueError("uninterpretable lefthook configuration")


def _balanced_yaml_flow(value: str) -> bool:
    stack: list[str] = []
    quote = ""
    escaped = False
    for index, char in enumerate(value):
        if quote:
            if escaped:
                escaped = False
            elif quote == '"' and char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char in "[{":
            stack.append(char)
        elif char in "]}" and (not stack or stack.pop() != {"]": "[", "}": "{"}[char]):
            return False
        elif char in "]}" and not stack:
            return not value[index + 1 :].split("\n", 1)[0].strip()
    return not stack and not quote


def _loader(cwd: Path, recipe: Recipe) -> list[JsonObject]:
    name = str(recipe.project.get("loader", "none"))
    if name == "none":
        return [_check("loader", "not_applicable", "No shell loader declared.")]
    loader = LOADERS[name]
    candidates = {
        "mise": ("mise.toml", ".mise.toml"),
        "direnv": (".envrc",),
        "devbox": ("devbox.json",),
    }[name]
    paths = [cwd / candidate for candidate in candidates]
    try:
        for path in paths:
            text = read_optional_editable_text(path, root=cwd)
            if text is not None and path.suffix == ".toml":
                tomllib.loads(text)
            elif text is not None and path.suffix == ".json":
                value = json.loads(text)
                if not isinstance(value, dict):
                    raise ValueError("invalid loader object")
            if text is not None:
                break
    except (OSError, ValueError):
        return [
            _check(
                "loader",
                "error",
                f"{name} configuration cannot be read or interpreted.",
                "Inspect the loader configuration.",
            )
        ]
    try:
        plan = loader.plan(cwd, recipe.env_file)
        checks = [
            _check(
                "loader",
                "finding" if plan.writes else "ok",
                f"{name} wiring: {'needs configuration' if plan.writes else 'configured'}.",
                *(("Run splash doctor to inspect wiring.",) if plan.writes else ()),
                path=plan.path,
            )
        ]
        if name in {"mise", "direnv"}:
            checks.append(
                _check(
                    "loader_approval",
                    "unavailable",
                    f"{name} approval is not observed by status.",
                    f"Check authorization with {name}.",
                )
            )
        return checks
    except LoaderConflictError:
        return [
            _check(
                "loader",
                "finding",
                f"{name} wiring needs manual configuration.",
                "Inspect the loader configuration and its env-file directive.",
            )
        ]
    except (OSError, ValueError):
        return [
            _check(
                "loader",
                "error",
                f"{name} configuration cannot be read or interpreted.",
                "Inspect the loader configuration.",
            )
        ]


def _target_rows(
    cwd: Path,
    recipe: Recipe,
    snapshot: RegistrySnapshot,
    inspection: _Inspection,
    checks: list[JsonObject],
) -> list[JsonValue]:
    local_path = cwd / LOCAL_NAME
    global_path = _global_config_path()
    try:
        local = LocalConfig.parse(_optional_text(local_path) or "", local_path)
        glob = GlobalConfig.parse(_optional_text(global_path) or "", global_path)
        specs = merged_targets(recipe, local, glob)
    except (OSError, ValueError):
        checks.append(
            _check(
                "target_config",
                "error",
                "Local/global target configuration cannot be read or interpreted.",
                "Inspect splashdown.local.toml and the global config.",
            )
        )
        return _saved_target_rows(snapshot, str(cwd))
    identities = {(dtype, variant) for dtype, variants in specs.items() for variant in variants} | {
        (row.dtype, row.variant) for row in snapshot.devices_for(str(cwd))
    }
    rows: list[JsonValue] = []
    for dtype, variant in sorted(identities):
        spec = specs.get(dtype, {}).get(variant)
        registered = snapshot.get_device(str(cwd), dtype, variant)
        source = target_source(dtype, variant, recipe, local, glob) if spec else "registry"
        try:
            name, state, health = inspection.targets.observe(cwd, dtype, variant, spec, registered)
        except (OSError, ValueError, DeviceError, subprocess.SubprocessError):
            name, state, health = (
                registered.identifier if registered else "",
                "unavailable",
                "unavailable",
            )
        if dtype == "device":
            for platform in sorted(inspection.targets.unavailable):
                checks.append(
                    _check(
                        f"target_discovery:{platform}",
                        "unavailable",
                        f"{platform} physical discovery unavailable.",
                        "Run splash doctor for platform diagnostics.",
                    )
                )
        rows.append(
            {
                "type": dtype,
                "variant": variant,
                "source": source,
                "device_name": name,
                "status": state,
                "health": health,
            }
        )
        check_state = (
            "unavailable"
            if health == "unavailable"
            else "finding"
            if health in {"orphan", "drifted", "undeclared", "missing", "ambiguous"}
            else "ok"
        )
        steps = (
            ("Run splash doctor for platform diagnostics.",)
            if health == "unavailable"
            else ("Connect the physical device and check pairing.",)
            if health == "missing"
            else ("Narrow the physical target id, name, or platform.",)
            if health == "ambiguous"
            else ("Run splash target refresh to reconcile managed targets.",)
            if check_state == "finding"
            else ()
        )
        checks.append(
            _check(
                f"target:{dtype}.{variant}",
                check_state,
                f"{dtype}.{variant}: {state} ({health.replace('_', ' ')})",
                *steps,
            )
        )
    if not identities:
        checks.append(_check("targets", "not_applicable", "No targets declared or registered."))
    return rows


def _saved_target_rows(snapshot: RegistrySnapshot, checkout: str) -> list[JsonValue]:
    return [
        {
            "type": row.dtype,
            "variant": row.variant,
            "source": "registry",
            "device_name": row.identifier,
            "status": "unavailable",
            "health": "unavailable",
        }
        for row in snapshot.devices_for(checkout)
    ]


def _checkout(checkout: str, snapshot: RegistrySnapshot, inspection: _Inspection) -> JsonObject:
    cwd = Path(checkout)
    values = snapshot.all_for(checkout)
    checks: list[JsonObject] = []
    report: JsonObject = {
        "checkout": checkout,
        "exists": True,
        "complete": True,
        "resources": [],
        "targets": _saved_target_rows(snapshot, checkout),
        "checks": cast(list[JsonValue], checks),
        "counts": dict(snapshot.summary_for(checkout)),
    }
    recipe: Recipe | None = None
    try:
        try:
            mode = cwd.stat().st_mode
        except FileNotFoundError:
            report["exists"] = False
            checks.append(
                _check(
                    "checkout",
                    "finding",
                    "Deleted checkout retains registry rows.",
                    "Run splash gc to remove deleted checkout rows.",
                )
            )
        else:
            if not stat.S_ISDIR(mode):
                raise NotADirectoryError(checkout)
            text = _optional_text(cwd / RECIPE_NAME)
            if text is None:
                checks.append(
                    _check(
                        "recipe",
                        "error",
                        "Checkout has no splashdown.toml.",
                        "Run splash init in this checkout.",
                    )
                )
            else:
                recipe = Recipe.parse(text, cwd / RECIPE_NAME)
                checks.append(_check("recipe", "ok", "Recipe parsed.", path=cwd / RECIPE_NAME))
    except (OSError, ValueError):
        checks.append(
            _check(
                "recipe",
                "error",
                "Checkout or recipe cannot be read or interpreted.",
                "Inspect the checkout and splashdown.toml permissions and syntax.",
            )
        )
    report["resources"] = inspection.resources(recipe, values, checks)
    if recipe is not None:
        checks.extend(_outputs(cwd, recipe, values))
        checks.extend(_trust_and_hooks(cwd, recipe))
        checks.extend(_loader(cwd, recipe))
        report["targets"] = _target_rows(cwd, recipe, snapshot, inspection, checks)
    else:
        checks.extend(
            _check(identifier, "unavailable", "Requires a live checkout with a valid recipe.")
            for identifier in ("outputs", "trust", "integrations", "targets")
        )
    report["complete"] = not any(check["state"] == "error" for check in checks)
    return report


def build_status_report(
    cwd: Path,
    registry: RegistrySnapshot,
    *,
    show_all: bool = False,
    diagnostics: tuple[Diagnostic, ...] = (),
) -> CommandResult:
    inspection = _Inspection()
    identities = registry.all_checkouts() if show_all else [str(cwd.resolve())]
    checkouts = []
    try:
        for checkout in identities:
            checkouts.append(_checkout(checkout, registry, inspection))
    except KeyboardInterrupt:
        return CommandResult(
            "status all" if show_all else "status",
            "partial" if checkouts else "error",
            130,
            data={"checkouts": cast(list[JsonValue], checkouts)} if checkouts else None,
            error=Diagnostic("interrupted", "Status inspection interrupted."),
            warnings=diagnostics,
        )
    failures = [
        Diagnostic("inspection_input_error", f"{checkout['checkout']}: {check['message']}")
        for checkout in checkouts
        for check in cast(list[JsonObject], checkout["checks"])
        if check["state"] == "error"
    ]
    errors = [*diagnostics, *failures]
    return CommandResult(
        "status all" if show_all else "status",
        "partial" if errors else "success",
        1 if errors else 0,
        data={"checkouts": cast(list[JsonValue], checkouts)},
        error=Diagnostic("inspection_incomplete", "Status inspection is incomplete.")
        if errors
        else None,
        warnings=tuple(errors),
    )
