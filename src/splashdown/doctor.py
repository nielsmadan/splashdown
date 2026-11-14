from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast

from .catalog import PROFILES
from .cli_output import emit_result, render_doctor_result
from .constants import LOCAL_NAME, RECIPE_NAME, split_lines
from .device_discovery import ExistingADB, UnsupportedADBObservation, observe_existing_adb
from .errors import LoaderConflictError
from .hooks import (
    _configure_post_checkout_hook,
    inspect_hook_inputs,
    post_checkout_configuration_files,
    post_checkout_manual_instructions,
    post_checkout_readiness,
    validate_hook_inputs,
)
from .inventory import AppInventory
from .loaders import (
    apply_wire_plan,
    direnv_approval,
    loader_config_path,
    validate_loader_config,
    validated_loader_plan,
)
from .profile_core import configured_app_inventory
from .profiles import compose_wiring_checks
from .recipe import LocalConfig, Recipe, merged_targets
from .registry import RegistryReadError, checkout_operation_lock, read_registry_snapshot
from .results import CommandResult, Diagnostic, JsonObject, JsonValue, OutputFormat
from .runtime_checks import (
    WATCHMAN_CHECK,
    ToolObservations,
    pods_check,
    tooling_checks,
)
from .safe_files import checkout_controlled_executable, read_optional_editable_bytes
from .wiring import WiringCheck, wiring_destination

DoctorState = Literal["ok", "warning", "problem", "incomplete", "skipped"]


@dataclass(frozen=True)
class DoctorOutcome:
    scope: str
    app: str | None
    path: Path
    check_id: str
    state: DoctorState
    message: str
    next_steps: tuple[str, ...] = ()
    changed_files: tuple[Path, ...] = ()
    repair: Literal["none", "repaired", "failed", "unverified"] = "none"


@dataclass(frozen=True)
class _CheckTarget:
    scope: str
    app: str | None
    path: Path
    check: WiringCheck
    framework: bool = False

    def outcome(self, state: DoctorState, message: str) -> DoctorOutcome:
        return DoctorOutcome(self.scope, self.app, self.path, self.check.id, state, message)


def _app_checks(app: AppInventory, recipe: Recipe) -> list[WiringCheck]:
    if not app.path.is_dir():
        raise ValueError(f"Configured app directory is missing: {app.path}")
    profile = PROFILES.get(app.profile)
    if profile is None:
        return []
    destination = wiring_destination(app.path, recipe.path.parent, recipe.env_file)
    return [check for check in profile.wiring_checks(app, destination) if check.id != "hook"]


def _project_checks(
    recipe: Recipe, apps: list[AppInventory], outcomes: list[DoctorOutcome]
) -> list[WiringCheck]:
    try:
        checks = list(compose_wiring_checks(recipe.path.parent))
    except Exception as error:  # noqa: BLE001
        checks = []
        outcomes.append(
            DoctorOutcome(
                "project",
                None,
                recipe.path.parent,
                "compose-check-construction",
                "incomplete",
                f"Compose check construction failed: {error}",
            )
        )
    mobile = any(app.profile in {"react-native", "expo"} for app in apps)
    try:
        checks.extend(_device_checks(recipe))
    except Exception as error:  # noqa: BLE001
        checks.append(
            _report_check(
                "device-check-construction",
                "incomplete",
                f"Device check construction failed: {error}",
            )
        )
    checks.extend(_loader_checks(recipe))
    checks.extend(_hook_checks(recipe.path.parent))
    if mobile:
        checks.append(WATCHMAN_CHECK)
    return checks


def _report_check(check_id: str, state: str, message: str, guidance: str = "") -> WiringCheck:
    return WiringCheck(
        check_id,
        message,
        lambda _: True,
        lambda _: (state, message),
        None,
        (lambda _: guidance) if guidance else None,
    )


def _loader_checks(recipe: Recipe) -> list[WiringCheck]:
    name = str(recipe.project.get("loader", "none"))
    if name == "none":
        return [_report_check("loader", "skipped", "No shell loader declared (loader none).")]
    cwd, destination = recipe.path.parent, recipe.env_file
    guidance = f"Review {loader_config_path(cwd, name)} and its directive for {destination}."

    def detect(path: Path) -> tuple[str, str]:
        plan = validated_loader_plan(path, name, destination)
        return (
            "problem" if plan.writes else "ok",
            f"{name}: {plan.note}; destination {destination}.",
        )

    def repair(path: Path) -> None:
        apply_wire_plan(validated_loader_plan(path, name, destination))

    try:
        plan = validated_loader_plan(cwd, name, destination)
    except (LoaderConflictError, ValueError) as error:
        config = _report_check("loader", "problem", f"{name} configuration: {error}", guidance)
    except OSError as error:
        config = _report_check("loader", "incomplete", f"Cannot inspect {name}: {error}", guidance)
    else:
        config = WiringCheck(
            "loader",
            f"{name} loads {destination}",
            lambda _: True,
            detect,
            repair if plan.writes else None,
            lambda _: guidance,
            files=lambda path: (loader_config_path(path, name),),
        )

    def tool(path: Path) -> tuple[str, str]:
        executable = shutil.which(name)
        if executable is None:
            return "problem", f"Selected loader {name} is missing from PATH."
        return "ok", f"Selected loader {name} is available at {executable}."

    def approval(path: Path) -> tuple[str, str]:
        executable = shutil.which(name)
        if executable is None:
            return "skipped", f"{name} authorization cannot be observed while its tool is missing."
        if name == "direnv":
            candidate = Path(executable)
            if not candidate.is_absolute():
                candidate = path / candidate
            resolved = candidate.resolve()
            if checkout_controlled_executable(candidate, path, resolved=resolved):
                return "incomplete", "Refusing to run a checkout-controlled direnv executable."
            validate_loader_config(path, name)
            return direnv_approval(path, str(resolved))
        return (
            "skipped",
            f"{name} authorization and parent-shell activation observation are unsupported; verify with {name}.",
        )

    return [
        config,
        WiringCheck(
            "loader-tool",
            f"selected {name} executable",
            lambda _: True,
            tool,
            None,
            lambda _: f"Install {name} and make it available on PATH.",
        ),
        WiringCheck(
            "loader-approval",
            f"{name} authorization",
            lambda _: True,
            approval,
            None,
            lambda _: (
                f"Review the configuration, then use `{name} {'allow' if name == 'direnv' else 'trust' if name == 'mise' else 'shell'}` yourself. Parent-shell loading is not inspected."
            ),
        ),
    ]


def _hook_checks(cwd: Path) -> list[WiringCheck]:
    try:
        inputs = inspect_hook_inputs(cwd)
        if inputs is None:
            return [
                _report_check(
                    "hook",
                    "skipped",
                    "No project-root Git post-checkout integration applies (non-Git or nested project).",
                )
            ]
        readiness = post_checkout_readiness(cwd, strict=inputs)
        candidates = post_checkout_configuration_files(cwd, inputs)
    except Exception as error:  # noqa: BLE001
        return [_report_check("hook", "incomplete", f"Post-checkout inspection failed: {error}")]

    def detect(path: Path) -> tuple[str, str]:
        fresh = post_checkout_readiness(path, strict=validate_hook_inputs(path, inputs))
        if fresh.manager == "none":
            return (
                "skipped",
                "Native post-checkout installation is local activation, handled by splash trust.",
            )
        return "ok" if fresh.ready else "problem", fresh.detail

    def activation(path: Path) -> tuple[str, str]:
        fresh = post_checkout_readiness(path, strict=validate_hook_inputs(path, inputs))
        return "ok" if fresh.active else "problem", (
            f"{fresh.manager} post-checkout hook is installed."
            if fresh.active
            else f"{fresh.manager} post-checkout integration is inactive; install it with its owning tool or splash trust."
        )

    def repair(path: Path) -> None:
        _configure_post_checkout_hook(path, strict=validate_hook_inputs(path, inputs))

    def guidance(path: Path) -> str:
        return post_checkout_manual_instructions(path, strict=inputs)

    return [
        WiringCheck(
            "hook",
            "post-checkout configuration",
            lambda _: True,
            detect,
            repair if candidates and not readiness.ready else None,
            guidance,
            files=lambda path: post_checkout_configuration_files(path, inputs),
        ),
        WiringCheck(
            "hook-activation",
            "local post-checkout activation",
            lambda _: True,
            activation,
            None,
            guidance,
        ),
    ]


def _device_checks(recipe: Recipe) -> list[WiringCheck]:
    cwd = recipe.path.parent
    checkout = str(cwd.resolve())
    checks: list[WiringCheck] = []
    try:
        snapshot = read_registry_snapshot()
    except RegistryReadError as error:
        snapshot = error.snapshot
        checks.extend(
            _report_check(f"device-registry-{index}", "incomplete", diagnostic.message)
            for index, diagnostic in enumerate(error.diagnostics)
        )
    try:
        local_path = cwd / LOCAL_NAME
        local_raw = read_optional_editable_bytes(local_path, root=cwd)
        local = LocalConfig.parse(
            local_raw.decode("utf-8") if local_raw is not None else "", local_path
        )
        catalog = merged_targets(recipe, local)
    except (OSError, ValueError) as error:
        checks.append(
            _report_check(
                "device-configuration", "incomplete", f"Cannot inspect local destinations: {error}"
            )
        )
        catalog = recipe.targets
    cached: list[ExistingADB | Exception] = []

    def android() -> ExistingADB:
        if not cached:
            try:
                cached.append(observe_existing_adb(os.environ))
            except (OSError, ValueError) as error:
                cached.append(error)
        result = cached[0]
        if isinstance(result, Exception):
            raise result
        return result

    def physical(hardware_id: str, platform: str) -> tuple[str, str]:
        if platform == "ios":
            return (
                "skipped",
                f"Saved iOS hardware ID {hardware_id}. Live observation is unsupported without a proven no-start SDK adapter; connection, pairing and tunnel state are unverified. A paired idle tunnel is normal.",
            )
        try:
            observation = android()
        except UnsupportedADBObservation as error:
            return (
                "skipped",
                f"Saved hardware ID {hardware_id}: {error}. Live connection state was not inspected.",
            )
        limitation = " iOS live observation is unsupported." if not platform else ""
        if observation.idle:
            return (
                "warning",
                f"Saved hardware ID {hardware_id}: ADB server is idle; device connection was not inspected.{limitation} Connect the destination when needed.",
            )
        if not observation.devices:
            return (
                "warning",
                f"Saved hardware ID {hardware_id}: existing ADB server lists no physical transports.{limitation} A disconnected destination is normal when unused.",
            )
        matches = [
            row
            for row in observation.devices
            if row.transport_id == hardware_id and row.connection_type == "usb"
        ]
        if matches:
            states = ", ".join(sorted({row.state for row in matches}))
            return (
                "warning",
                f"Saved hardware ID {hardware_id}: a USB transport with the same text reports {states}; its hardware identity is unverified, so this is not a verified observation of the saved phone.{limitation} Unlock/authorize or reconnect that transport if needed.",
            )
        return (
            "skipped",
            f"Saved hardware ID {hardware_id}: host inventory cannot verify hardware identities or establish this phone's absence. Wireless endpoints are not hardware IDs.{limitation} Verify the saved device manually.",
        )

    def physical_check(label: str, hardware_id: str, platform: str) -> WiringCheck:
        return WiringCheck(
            f"device:{label}",
            f"saved destination {label}",
            lambda _: True,
            lambda _: physical(hardware_id, platform),
            None,
            lambda _: (
                "Review this saved destination and connect it when needed. Doctor never boots, claims, pairs or starts device services."
            ),
        )

    declared = set()
    for dtype, variants in catalog.items():
        for variant, spec in variants.items():
            label = f"{dtype}.{variant}"
            if dtype == "device":
                hardware_id, platform = str(spec["id"]), str(spec.get("platform", ""))
                if not platform:
                    claimed_platforms = {
                        claim.platform
                        for claim in snapshot.claims
                        if claim.owner_checkout == checkout and claim.hardware_id == hardware_id
                    }
                    if len(claimed_platforms) == 1:
                        platform = next(iter(claimed_platforms))
                    elif claimed_platforms:
                        declared.add((hardware_id, ""))
                        checks.append(
                            _report_check(
                                f"device:{label}",
                                "incomplete",
                                f"Saved hardware ID {hardware_id}: checkout claims conflict on platform ({', '.join(sorted(claimed_platforms))}); live observation was not attempted. Review the saved destination and claims.",
                            )
                        )
                        continue
                declared.add((hardware_id, platform))
                checks.append(physical_check(label, hardware_id, platform))
            else:
                saved = snapshot.get_device(checkout, dtype, variant)
                detail = (
                    f"Saved instance {saved.identifier}"
                    if saved
                    else "No saved checkout instance (normal lazy state)"
                )
                checks.append(
                    _report_check(
                        f"device:{label}",
                        "skipped",
                        f"{label}: {detail}. Live existence and running state are unsupported without a proven no-start adapter; absent/stopped destinations are normal when unused.",
                    )
                )
    for claim in snapshot.claims:
        if (
            claim.owner_checkout != checkout
            or (claim.hardware_id, claim.platform) in declared
            or (claim.hardware_id, "") in declared
        ):
            continue
        checks.append(
            physical_check(f"claim:{claim.target_label}", claim.hardware_id, claim.platform)
        )
    return checks


def _targets(recipe: Recipe, outcomes: list[DoctorOutcome]) -> list[_CheckTarget]:
    apps = configured_app_inventory(recipe)
    targets: list[_CheckTarget] = []
    if not apps:
        outcomes.append(
            DoctorOutcome(
                "project",
                None,
                recipe.path.parent,
                "app-coverage",
                "skipped",
                "No configured apps; framework wiring was not inspected.",
            )
        )
    observations = ToolObservations()
    for app in apps:
        try:
            tools = tooling_checks(app, recipe, observations)
        except Exception as error:  # noqa: BLE001
            tools = [
                _report_check(
                    "tool-check-construction",
                    "incomplete",
                    f"Tool check construction failed: {error}",
                )
            ]
        targets.extend(_CheckTarget("app", app.name, app.path, check) for check in tools)
        try:
            pods = pods_check(app, recipe, observations)
        except Exception as error:  # noqa: BLE001
            pods = _report_check(
                "pods-check-construction",
                "incomplete",
                f"CocoaPods check construction failed: {error}",
            )
        if pods is not None:
            targets.append(_CheckTarget("app", app.name, app.path, pods))
        try:
            checks = _app_checks(app, recipe)
        except Exception as error:  # noqa: BLE001
            outcomes.append(
                DoctorOutcome(
                    "app",
                    app.name,
                    app.path,
                    "check-construction",
                    "incomplete",
                    f"Check construction failed: {error}",
                )
            )
            continue
        if not checks:
            message = (
                f"No built-in framework wiring checks for `{app.profile}`; coverage is limited."
            )
            outcomes.append(
                DoctorOutcome("app", app.name, app.path, "app-coverage", "skipped", message)
            )
        targets.extend(_CheckTarget("app", app.name, app.path, check, True) for check in checks)
    try:
        targets.extend(
            _CheckTarget("project", None, recipe.path.parent, check)
            for check in _project_checks(recipe, apps, outcomes)
        )
    except Exception as error:  # noqa: BLE001
        outcomes.append(
            DoctorOutcome(
                "project",
                None,
                recipe.path.parent,
                "check-construction",
                "incomplete",
                f"Shared check construction failed: {error}",
            )
        )
    return targets


def _inspect(target: _CheckTarget) -> DoctorOutcome:
    phase = "applicability"
    outcome = target.outcome("incomplete", "Inspection did not complete.")
    try:
        if not target.check.applies(target.path):
            return target.outcome("skipped", "Not applicable.")
        phase = "inspection"
        state, message = target.check.detect(target.path)
        if state not in {"ok", "warning", "problem", "incomplete", "skipped"}:
            raise ValueError(f"unknown check state {state!r}")
        outcome = target.outcome(cast(DoctorState, state), message or target.check.description)
        phase = "guidance"
        if state not in {"ok", "skipped"} and target.check.manual_instructions:
            steps = tuple(split_lines(target.check.manual_instructions(target.path)))
            outcome = replace(outcome, next_steps=steps)
        return outcome
    except Exception as error:  # noqa: BLE001
        return replace(
            outcome,
            state="incomplete",
            message=(f"{outcome.message}; " if phase == "guidance" else "")
            + f"{phase.capitalize()} failed: {error}",
        )


def _repair_allowed(check: WiringCheck) -> bool:
    return check.autofix is not None and not check.activation


def _refresh_target(cwd: Path, target: _CheckTarget) -> _CheckTarget | None:
    recipe = Recipe.load(cwd / RECIPE_NAME)
    apps = configured_app_inventory(recipe)
    if target.app is None:
        if target.check.id in {"loader", "loader-tool", "loader-approval"}:
            checks = _loader_checks(recipe)
        elif target.check.id in {"hook", "hook-activation"}:
            checks = _hook_checks(cwd)
        elif target.check.id == WATCHMAN_CHECK.id:
            checks = (
                [WATCHMAN_CHECK]
                if any(app.profile in {"react-native", "expo"} for app in apps)
                else []
            )
        else:
            checks = list(compose_wiring_checks(cwd))
        path = cwd
    else:
        app = next((app for app in apps if app.name == target.app), None)
        if app is None:
            return None
        path = app.path
        checks = _app_checks(app, recipe)
    check = next((check for check in checks if check.id == target.check.id), None)
    return None if check is None else _CheckTarget(target.scope, target.app, path, check)


def _snapshot(target: _CheckTarget, root: Path) -> dict[Path, bytes | None]:
    paths = target.check.files(target.path)
    if not paths:
        raise ValueError("repair has no known candidate files; change accounting is unavailable")
    return {path: read_optional_editable_bytes(path, root=root) for path in paths}


def _changed_after(
    target: _CheckTarget, root: Path, before: dict[Path, bytes | None]
) -> tuple[tuple[Path, ...], str | None, bool]:
    paths = set(before)
    errors = []
    interrupted = False
    try:
        paths.update(target.check.files(target.path))
    except KeyboardInterrupt:
        interrupted = True
        errors.append("candidate enumeration interrupted")
    except Exception as error:  # noqa: BLE001
        errors.append(f"candidate enumeration failed: {error}")
    changed = []
    for path in sorted(paths):
        try:
            after = read_optional_editable_bytes(path, root=root)
            if path not in before:
                errors.append(f"no before snapshot for {path}")
            elif after != before[path]:
                changed.append(path)
        except KeyboardInterrupt:
            interrupted = True
            errors.append(f"reading {path} interrupted")
            break
        except Exception as error:  # noqa: BLE001
            errors.append(f"{path}: {error}")
    return tuple(changed), "; ".join(errors) or None, interrupted


def _apply_repair(
    target: _CheckTarget, cwd: Path, outcomes: list[DoctorOutcome], index: int
) -> None:
    before = _snapshot(target, cwd)
    outcomes[index] = replace(
        outcomes[index],
        state="incomplete",
        message="Repair interrupted before verification.",
        repair="unverified",
    )
    phase = "repair"
    try:
        autofix = target.check.autofix
        if autofix is None:
            raise ValueError("repair callback is unavailable")
        autofix(target.path)
        phase = "recheck"
        result = _inspect(target)
        if result.state == "incomplete":
            result = replace(result, message=f"Recheck incomplete: {result.message}")
        repaired = result.state in {"ok", "warning"}
        outcomes[index] = replace(
            result,
            repair="repaired"
            if repaired
            else "unverified"
            if result.state in {"incomplete", "skipped"}
            else "failed",
        )
    except Exception as error:  # noqa: BLE001
        outcomes[index] = replace(
            outcomes[index],
            state="incomplete",
            message=f"{phase.capitalize()} failed: {error}",
            repair="failed" if phase == "repair" else "unverified",
        )
    finally:
        changed, accounting_error, interrupted = _changed_after(target, cwd, before)
        outcome = replace(outcomes[index], changed_files=changed)
        if accounting_error:
            outcome = replace(
                outcome,
                state="incomplete",
                repair="unverified",
                message=f"{outcome.message}; change accounting incomplete: {accounting_error}",
            )
        outcomes[index] = outcome
        if interrupted:
            raise KeyboardInterrupt


def _repair(target: _CheckTarget, cwd: Path, outcomes: list[DoctorOutcome], index: int) -> None:
    try:
        _snapshot(target, cwd)
        with checkout_operation_lock(str(cwd.resolve())):
            fresh = _refresh_target(cwd, target)
            if fresh is None:
                outcomes[index] = target.outcome(
                    "skipped", "Configuration changed; check no longer required."
                )
                return
            outcomes[index] = _inspect(fresh)
            if outcomes[index].state not in {"problem", "warning"} or not _repair_allowed(
                fresh.check
            ):
                return
            _apply_repair(fresh, cwd, outcomes, index)
    except Exception as error:  # noqa: BLE001
        outcomes[index] = replace(
            outcomes[index], state="incomplete", message=f"Repair preparation failed: {error}"
        )


def _outcome_data(outcome: DoctorOutcome) -> JsonObject:
    return {
        "scope": outcome.scope,
        "app": outcome.app,
        "path": str(outcome.path),
        "check_id": outcome.check_id,
        "state": outcome.state,
        "message": outcome.message,
        "next_steps": list(outcome.next_steps),
        "changed_files": [str(path) for path in outcome.changed_files],
        "repair": outcome.repair,
    }


def _result(outcomes: list[DoctorOutcome], *, interrupted: bool = False) -> CommandResult:
    states = ("ok", "warning", "problem", "incomplete", "skipped")
    summary: JsonObject = {state: sum(row.state == state for row in outcomes) for state in states}
    summary["changed_files"] = [
        str(path) for path in sorted({path for row in outcomes for path in row.changed_files})
    ]
    summary["applicable"] = sum(
        row.state != "skipped" and row.check_id != "configuration" for row in outcomes
    )
    summary["interrupted"] = interrupted
    rows: list[JsonValue] = [_outcome_data(row) for row in outcomes]
    failed = any(
        row.state in {"problem", "incomplete"} or row.repair in {"failed", "unverified"}
        for row in outcomes
    )
    code = 130 if interrupted else 1 if failed else 0
    return CommandResult(
        "doctor",
        "partial" if code else "success",
        code,
        data={"outcomes": rows, "summary": summary},
        error=Diagnostic(
            "interrupted" if interrupted else "doctor_failed",
            "Doctor interrupted."
            if interrupted
            else "Doctor found unresolved problems or incomplete checks.",
        )
        if code
        else None,
    )


def run_doctor(cwd: Path, fix: bool = False) -> CommandResult:
    outcomes: list[DoctorOutcome] = []
    configuration = DoctorOutcome(
        "project", None, cwd / RECIPE_NAME, "configuration", "ok", "Configuration parsed."
    )
    try:
        try:
            recipe = Recipe.load(configuration.path)
        except FileNotFoundError:
            outcomes.append(
                replace(
                    configuration,
                    state="problem",
                    message=f"Missing {configuration.path}.",
                    next_steps=("Run `splash init` to configure this project.",),
                )
            )
            return _result(outcomes)
        except Exception as error:  # noqa: BLE001
            outcomes.append(
                replace(
                    configuration,
                    state="problem",
                    message=f"Cannot load {configuration.path}: {error}",
                )
            )
            return _result(outcomes)
        outcomes.append(configuration)
        refresh_framework = False
        for selected in _targets(recipe, outcomes):
            target = selected
            outcomes.append(target.outcome("incomplete", "Check interrupted before completion."))
            index = len(outcomes) - 1
            if fix and (
                (target.framework and refresh_framework)
                or target.check.id in {"loader-tool", "loader-approval", "hook-activation"}
            ):
                try:
                    fresh = _refresh_target(cwd, target)
                except Exception as error:  # noqa: BLE001
                    outcomes[index] = target.outcome(
                        "incomplete", f"Observation refresh failed: {error}"
                    )
                    continue
                if fresh is None:
                    outcomes[index] = target.outcome(
                        "skipped", "Configuration changed; observation no longer required."
                    )
                    continue
                target = fresh
            outcomes[index] = _inspect(target)
            if (
                fix
                and outcomes[index].state in {"problem", "warning"}
                and _repair_allowed(target.check)
            ):
                _repair(target, cwd, outcomes, index)
                refresh_framework = True
        return _result(outcomes)
    except KeyboardInterrupt:
        return _result(outcomes, interrupted=True)


def cmd_doctor(cwd: Path, *, fix: bool = False, output_format: OutputFormat = "text") -> int:
    return emit_result(run_doctor(cwd, fix), output_format, render_text=render_doctor_result)
