from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .registry import Registry, DeviceRow
from .recipe import (
    Recipe, LocalConfig, TemplateError, LOCAL_SKELETON,
    _find_table, _toml_quote, _current_branch,
    merged_devices, resolve_variant,
)
from .provisioning import (
    provision, write_outputs, run_setup,
)
from .devices import (
    DeviceError, device_status, device_shutdown, device_destroy,
    device_add, device_remove, device_run, detect_framework,
    ensure_fresh_sim, ios_boot, ios_shutdown, ios_destroy,
    android_boot, android_shutdown, android_destroy,
    _ios_udid_exists, _android_avd_exists, _android_bin,
    _ios_current_state, _resolve_device_name, _is_orphan_device,
    _device_status_for_row, _short_path, _summary_string,
    _load_recipe_or_empty, _ios_latest_runtime_version, _android_latest_image,
    _android_running_serial, _xcrun_json,
)
from .scanner import (
    Scanner, AppInventory, ProjectInventory,
    _detect_loader, _merge_app_resources, _app_resource_names,
)
from .wiring import (
    WiringCheck, _wiring_checks_for_framework, _resolve_doctor_framework, cmd_doctor,
)
from .loaders import LOADERS
from . import RECIPE_NAME, LOCAL_NAME, ENV_FILE_NAME, DEVICE_TYPES


# ---------- init / scaffolding ----------

POST_CHECKOUT_HOOK = """\
#!/bin/sh
# Splashdown per-checkout provisioning. Fires on git checkout / clone / worktree add.
set -e
TOP=$(git rev-parse --show-toplevel) || exit 0
cd "$TOP"
[ -f splashdown.toml ] || exit 0
if command -v splash >/dev/null 2>&1; then
    splash >&2 || true
else
    echo "post-checkout: \\`splash\\` not on PATH — install splashdown" >&2
fi
exit 0
"""


def _ensure_gitignore(cwd: Path) -> None:
    path = cwd / ".gitignore"
    existing = path.read_text() if path.exists() else ""
    present = set(existing.splitlines())
    additions = [e for e in (ENV_FILE_NAME, LOCAL_NAME) if e not in present]
    if not additions:
        return
    prefix = existing if existing.endswith("\n") or not existing else existing + "\n"
    path.write_text(prefix + "\n".join(additions) + "\n")
    print(f"updated .gitignore (+{', '.join(additions)})", file=sys.stderr)


def _ensure_mise_file_directive(cwd: Path) -> None:
    """Ensure mise.toml has `_.file = "splashdown.env"` under [env]."""
    directive = f'_.file = "{ENV_FILE_NAME}"'
    path = cwd / "mise.toml"
    if not path.exists():
        path.write_text(f"[env]\n{directive}\n")
        print(f"created mise.toml with {directive}", file=sys.stderr)
        return
    text = path.read_text()
    if directive in text:
        return
    lines = text.splitlines()
    start, _end = _find_table(lines, "env")
    if start is None:
        new_text = text.rstrip() + f"\n\n[env]\n{directive}\n"
    else:
        lines.insert(start + 1, directive)
        new_text = "\n".join(lines) + "\n"
    path.write_text(new_text)
    print(f"updated mise.toml (+{directive})", file=sys.stderr)


def _detect_hook_manager(cwd: Path) -> str:
    """Identify the project's existing hook manager so we coexist instead of clobber.

    Returns one of: "lefthook", "husky", "core-hookspath-other", "none".
    """
    if any((cwd / n).exists() for n in ("lefthook.yml", "lefthook.yaml", ".lefthook.yml")):
        return "lefthook"
    pkg = cwd / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
        deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        if "lefthook" in deps:
            return "lefthook"
    if (cwd / ".husky").is_dir():
        return "husky"
    try:
        out = subprocess.check_output(
            ["git", "config", "--get", "core.hooksPath"],
            cwd=cwd, stderr=subprocess.DEVNULL,
        ).decode().strip()
        if out and out != ".githooks":
            return "core-hookspath-other"
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return "none"


def _lefthook_config_path(cwd: Path) -> Path:
    for name in ("lefthook.yml", "lefthook.yaml", ".lefthook.yml"):
        path = cwd / name
        if path.exists():
            return path
    return cwd / "lefthook.yml"  # default if lefthook detected only via package.json


def _wire_post_checkout_lefthook(cwd: Path) -> None:
    """Idempotently add a `post-checkout` -> `splash` entry to the lefthook config."""
    path = _lefthook_config_path(cwd)
    text = path.read_text() if path.exists() else ""
    if "splashdown" in text and "run: splash" in text:
        _run_lefthook_install(cwd)
        return
    lines = text.splitlines()
    pc_idx = next(
        (i for i, l in enumerate(lines) if re.match(r"^post-checkout:\s*$", l)),
        None,
    )
    if pc_idx is None:
        sep = "" if not text or text.endswith("\n") else "\n"
        text = text + sep + (
            "\npost-checkout:\n"
            "  commands:\n"
            "    splashdown:\n"
            "      run: splash\n"
        )
        path.write_text(text)
    else:
        # Find end of post-checkout block (next top-level key or EOF).
        end_idx = len(lines)
        for j in range(pc_idx + 1, len(lines)):
            l = lines[j]
            if l and not l[0].isspace() and not l.startswith("#"):
                end_idx = j
                break
        # If 'commands:' exists under post-checkout, insert splashdown under it;
        # otherwise inject a fresh commands: block right after the header.
        cmds_idx = next(
            (j for j in range(pc_idx + 1, end_idx)
             if re.match(r"^\s+commands:\s*$", lines[j])),
            None,
        )
        if cmds_idx is not None:
            indent = len(lines[cmds_idx]) - len(lines[cmds_idx].lstrip())
            addition = [
                " " * (indent + 2) + "splashdown:",
                " " * (indent + 4) + "run: splash",
            ]
            lines = lines[: cmds_idx + 1] + addition + lines[cmds_idx + 1 :]
        else:
            addition = ["  commands:", "    splashdown:", "      run: splash"]
            lines = lines[: pc_idx + 1] + addition + lines[pc_idx + 1 :]
        path.write_text("\n".join(lines) + ("\n" if text.endswith("\n") or text == "" else ""))
    _run_lefthook_install(cwd)
    print(f"wired post-checkout in {path.name} (lefthook)", file=sys.stderr)


def _run_lefthook_install(cwd: Path) -> None:
    """Best-effort: regenerate the lefthook-managed git hooks. Silent if unavailable."""
    candidates: list[list[str]] = []
    if (cwd / "yarn.lock").exists():
        candidates.append(["yarn", "lefthook", "install"])
    if (cwd / "package.json").exists():
        candidates.append(["npx", "--no-install", "lefthook", "install"])
    candidates.append(["lefthook", "install"])
    for cmd in candidates:
        try:
            r = subprocess.run(
                cmd, cwd=cwd, capture_output=True, timeout=30, text=True,
            )
            if r.returncode == 0:
                return
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    print(
        "note: could not run `lefthook install` automatically — run it yourself "
        "to register the post-checkout hook",
        file=sys.stderr,
    )


def _wire_post_checkout_husky(cwd: Path) -> None:
    """Drop a husky post-checkout hook invoking `splash`."""
    husky_dir = cwd / ".husky"
    husky_dir.mkdir(exist_ok=True)
    hook = husky_dir / "post-checkout"
    hook.write_text(POST_CHECKOUT_HOOK)
    hook.chmod(0o755)
    print("wrote .husky/post-checkout (husky)", file=sys.stderr)


def _wire_post_checkout_corehookspath(cwd: Path) -> None:
    """Default path: own .githooks/ and set core.hooksPath."""
    hooks_dir = cwd / ".githooks"
    hooks_dir.mkdir(exist_ok=True)
    hook = hooks_dir / "post-checkout"
    hook.write_text(POST_CHECKOUT_HOOK)
    hook.chmod(0o755)
    try:
        subprocess.run(
            ["git", "config", "core.hooksPath", ".githooks"],
            cwd=cwd, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass
    print("wrote .githooks/post-checkout, set core.hooksPath", file=sys.stderr)


def _ensure_post_checkout_hook(cwd: Path) -> None:
    """Wire `post-checkout -> splash`, coexisting with any existing hook manager."""
    manager = _detect_hook_manager(cwd)
    if manager == "lefthook":
        _wire_post_checkout_lefthook(cwd)
    elif manager == "husky":
        _wire_post_checkout_husky(cwd)
    elif manager == "core-hookspath-other":
        try:
            current = subprocess.check_output(
                ["git", "config", "--get", "core.hooksPath"],
                cwd=cwd, stderr=subprocess.DEVNULL,
            ).decode().strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            current = "?"
        print(
            f"warning: core.hooksPath is `{current}` — not wiring automatically. "
            f"Add a post-checkout hook there that runs `splash`.",
            file=sys.stderr,
        )
    else:
        _wire_post_checkout_corehookspath(cwd)


# ---------- status helpers ----------

def _gather_status_for_checkout(
    co: str, registry: Registry, *,
    show_all: bool, check: bool, summary: dict[str, int],
    os_cache: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the per-checkout block consumed by both JSON serialization and
    text emission. Mutates `summary` with defunct/orphan/stale/missing counts
    when `check`. `os_cache` memoizes the latest-OS lookups across checkouts."""
    cache = os_cache if os_cache is not None else {}
    co_path = Path(co)
    co_exists = co_path.exists()
    resources = registry.all_for(co)
    if check and not co_exists:
        summary["defunct_checkouts"] += 1
        summary["defunct_rows"] += len(resources) + len(registry.devices_for(co))

    # Port-state needs port-typed-resource knowledge. Read the recipe when
    # the checkout's path is still around; otherwise we can't tag.
    port_keys: set[str] = set()
    if co_exists:
        recipe_path = co_path / RECIPE_NAME
        if recipe_path.exists():
            try:
                rec = Recipe.load(recipe_path)
                port_keys = {n for n, s in rec.resources.items() if s.get("type") == "port"}
            except Exception:  # noqa: BLE001 — malformed recipe shouldn't kill status
                pass

    from .registry import _port_in_use  # noqa: PLC0415
    res_entries: list[dict[str, str]] = []
    for key, value in sorted(resources.items()):
        state = ""
        if key in port_keys:
            try:
                state = "in use" if _port_in_use(int(value)) else "free"
            except ValueError:
                state = ""
        res_entries.append({"key": key, "value": value, "port_state": state})

    # Device entries. In --all mode, source = registry only. In default
    # mode, source = recipe + local catalog (today's behavior).
    dev_entries: list[dict[str, Any]] = []
    if show_all:
        for row in registry.devices_for(co):
            try:
                status = _device_status_for_row(row)
            except DeviceError as e:
                status = f"error: {e}"
            orphan = stale = False
            if check and co_exists:
                if _is_orphan_device(row):
                    orphan = True
                    summary["orphan_devices"] += 1
                else:
                    spec = _load_variant_spec(co_path, row.dtype, row.variant)
                    if spec is not None and _device_stale(row, spec, cache):
                        stale = True
                        summary["stale_devices"] += 1
            dev_entries.append({
                "type": row.dtype, "variant": row.variant, "source": "",
                "device_name": row.udid, "status": status,
                "orphan": orphan, "stale": stale, "missing": False,
            })
    elif co_exists:
        recipe = _load_recipe_or_empty(co_path)
        local = LocalConfig.load(co_path / LOCAL_NAME)
        for dtype, variants in merged_devices(recipe, local).items():
            for variant, spec in variants.items():
                source = "recipe" if variant in recipe.devices.get(dtype, {}) else "local"
                resolved = _resolve_device_name(spec, co_path, variant, dtype)
                try:
                    status = device_status(dtype, resolved)
                except DeviceError as e:
                    status = f"error: {e}"
                orphan = stale = missing = False
                if check:
                    row = registry.get_device(co, dtype, variant)
                    if row is None:
                        if status == "absent":
                            missing = True
                            summary["missing_devices"] += 1
                    elif _is_orphan_device(row):
                        orphan = True
                        summary["orphan_devices"] += 1
                    elif _device_stale(row, spec, cache):
                        stale = True
                        summary["stale_devices"] += 1
                dev_entries.append({
                    "type": dtype, "variant": variant, "source": source,
                    "device_name": resolved, "status": status,
                    "orphan": orphan, "stale": stale, "missing": missing,
                })

    return {
        "checkout": co,
        "exists": co_exists,
        "resources": res_entries,
        "devices": dev_entries,
    }


def _emit_status_block_text(block: dict[str, Any], *, show_all: bool) -> None:
    """Emit one per-checkout text block to stderr."""
    header_tag = "  [defunct]" if not block["exists"] else ""
    if show_all:
        print(f"=== {block['checkout']}{header_tag} ===", file=sys.stderr)
    else:
        print(f"checkout: {block['checkout']}{header_tag}", file=sys.stderr)
    print("resources:", file=sys.stderr)
    if not block["resources"]:
        print("  (none)", file=sys.stderr)
    for r in block["resources"]:
        suffix = f"  [{r['port_state']}]" if r["port_state"] else ""
        print(f"  {r['key']}={r['value']}{suffix}", file=sys.stderr)
    print("devices:", file=sys.stderr)
    if not block["devices"]:
        print("  (none)", file=sys.stderr)
    for d in block["devices"]:
        cols = [f"{d['type']}.{d['variant']}"]
        if d["source"]:
            cols.append(d["source"])
        cols.append(d["device_name"])
        cols.append(d["status"])
        if d["orphan"]:
            cols.append("[orphan]")
        elif d.get("stale"):
            cols.append("[stale]")
        elif d.get("missing"):
            cols.append("[missing]")
        print("  " + "\t".join(cols), file=sys.stderr)
    if show_all:
        print("", file=sys.stderr)


def _cmd_status_table(checkouts: list[str], registry: Registry, check: bool) -> int:
    """Compact one-row-per-checkout view for `splash status --all`."""
    rows: list[tuple[str, str, str]] = []  # (path, summary, status)
    summary = {
        "defunct_checkouts": 0, "defunct_rows": 0,
        "orphan_devices": 0, "stale_devices": 0, "missing_devices": 0,
    }
    os_cache: dict[str, str] = {}

    for co in checkouts:
        counts = registry.summary_for(co)
        path_label = _short_path(co)
        summary_str = _summary_string(counts)
        co_exists = Path(co).exists()

        status_token = ""
        if not co_exists:
            status_token = "defunct"
            if check:
                summary["defunct_checkouts"] += 1
                summary["defunct_rows"] += sum(counts.values())
        elif check:
            for row in registry.devices_for(co):
                if _is_orphan_device(row):
                    summary["orphan_devices"] += 1
                    status_token = "orphan"
                    continue
                spec = _load_variant_spec(Path(co), row.dtype, row.variant)
                if spec is not None and _device_stale(row, spec, os_cache):
                    summary["stale_devices"] += 1
                    status_token = status_token or "stale"

        rows.append((path_label, summary_str, status_token))

    path_width = max((len(r[0]) for r in rows), default=4)
    path_width = max(path_width, len("PATH"))
    summary_width = max((len(r[1]) for r in rows), default=7)
    summary_width = max(summary_width, len("SUMMARY"))

    # ISSUE column only appears when at least one row flags something. Empty
    # cells across the board would just be dead width.
    has_issue = any(r[2] for r in rows)
    if has_issue:
        fmt_row = f"{{:<{path_width}}}  {{:<{summary_width}}}  {{}}"
        print(fmt_row.format("PATH", "SUMMARY", "ISSUE").rstrip(), file=sys.stderr)
        for path_label, summary_str, status_token in rows:
            print(fmt_row.format(path_label, summary_str, status_token).rstrip(), file=sys.stderr)
    else:
        fmt_row = f"{{:<{path_width}}}  {{}}"
        print(fmt_row.format("PATH", "SUMMARY").rstrip(), file=sys.stderr)
        for path_label, summary_str, _ in rows:
            print(fmt_row.format(path_label, summary_str).rstrip(), file=sys.stderr)

    if check:
        print("", file=sys.stderr)
        _print_check_summary(summary)

    return 0


def _print_check_summary(summary: dict[str, int]) -> None:
    """Emit the `--check` footer used by both cmd_status branches: a counts
    block plus hints routed to whatever fixes each issue, or `all entries
    verified` when clean."""
    defunct = summary.get("defunct_checkouts", 0)
    orphan = summary.get("orphan_devices", 0)
    stale = summary.get("stale_devices", 0)
    missing = summary.get("missing_devices", 0)
    if not (defunct or orphan or stale or missing):
        print("Summary: all entries verified.", file=sys.stderr)
        return
    print("Summary:", file=sys.stderr)
    if defunct:
        rows = summary.get("defunct_rows", 0)
        print(
            f"  {defunct} defunct checkout{'s' if defunct != 1 else ''} "
            f"({rows} registry row{'s' if rows != 1 else ''}).",
            file=sys.stderr,
        )
    if orphan:
        print(f"  {orphan} orphan device{'s' if orphan != 1 else ''} "
              f"(underlying sim/AVD deleted).", file=sys.stderr)
    if stale:
        print(f"  {stale} stale device{'s' if stale != 1 else ''} "
              f"(newer OS available).", file=sys.stderr)
    if missing:
        print(f"  {missing} missing device{'s' if missing != 1 else ''} "
              f"(declared but not yet created).", file=sys.stderr)
    # Route each hint to the command that actually fixes it. `splash gc` does NOT
    # recreate an orphan whose checkout still exists — `device refresh` does.
    if defunct:
        print("  Run `splash gc` to drop dead checkouts.", file=sys.stderr)
    if orphan or stale:
        print("  Run `splash device refresh` to recreate.", file=sys.stderr)
    if missing:
        print("  Run `splash run` to provision.", file=sys.stderr)


# ---------- command functions ----------

def cmd_status(
    cwd: Path, registry: Registry, fmt: str, *,
    show_all: bool = False, check: bool = False, verbose: bool = False,
) -> int:
    """Show resolved vars + declared devices.

    Default mode: just this checkout, recipe-aware device labels.
    --all: compact one-row-per-checkout table.
    --all --verbose: today's per-block view (resources + devices spelled out).
    --check: tag defunct checkouts and orphan device rows, print a `splash gc`
    cleanup hint footer. Composes with both --all variants."""
    target = str(cwd.resolve())
    checkouts = registry.all_checkouts() if show_all else [target]
    if not checkouts:
        checkouts = [target]

    # JSON shape is fixed regardless of verbose — consumers want the data, not
    # a table layout. Text branches: --all without --verbose emits a table;
    # everything else falls through to the per-block emitter below.
    if show_all and not verbose and fmt != "json":
        return _cmd_status_table(checkouts, registry, check)

    summary = {
        "defunct_checkouts": 0, "defunct_rows": 0,
        "orphan_devices": 0, "stale_devices": 0, "missing_devices": 0,
    }
    os_cache: dict[str, str] = {}
    blocks = [
        _gather_status_for_checkout(
            co, registry, show_all=show_all, check=check, summary=summary, os_cache=os_cache,
        )
        for co in checkouts
    ]

    if fmt == "json":
        payload: dict[str, Any] = (
            {"checkouts": blocks} if show_all else blocks[0]
        )
        if check:
            payload["summary"] = summary
        print(json.dumps(payload, indent=2))
        return 0

    for block in blocks:
        _emit_status_block_text(block, show_all=show_all)

    if check:
        _print_check_summary(summary)
    elif not show_all:
        # Default-mode footer: lightweight defunct-row count.
        from .registry import _port_in_use  # noqa: PLC0415
        stale = sum(
            1 for r in registry._read_ports() if not Path(r[1]).exists()  # noqa: SLF001
        ) + sum(
            1 for r in registry._read_kv() if not Path(r[0]).exists()  # noqa: SLF001
        )
        if stale:
            print(f"stale registry rows: {stale} (run `splash gc` to clean)", file=sys.stderr)

    return 0


def _cmd_refresh(cwd: Path, registry: Registry) -> int:
    """Re-provision. Identical to bare `splash` — exists as a named verb because
    the OS-squatter auto-reallocation in Registry.allocate_port is the thing
    users reach for under that label."""
    return _cmd_provision_inner(cwd, registry, reprovision=False)


def cmd_devices_list(cwd: Path, fmt: str) -> int:
    """List declared device variants and their live instance state."""
    _dev_status = _resolve_fn("device_status", device_status)
    recipe = _load_recipe_or_empty(cwd)
    local = LocalConfig.load(cwd / LOCAL_NAME)
    catalog = merged_devices(recipe, local)
    if not catalog:
        print(f"(no devices declared in {RECIPE_NAME} or {LOCAL_NAME})", file=sys.stderr)
        return 0
    rows: list[tuple[str, str, str, str, str]] = []
    for dtype, variants in catalog.items():
        for variant, spec in variants.items():
            source = "recipe" if variant in recipe.devices.get(dtype, {}) else "local"
            resolved = _resolve_device_name(spec, cwd, variant, dtype)
            try:
                status = _dev_status(dtype, resolved)
            except DeviceError as e:
                status = f"error: {e}"
            rows.append((dtype, variant, source, resolved, status))
    if fmt == "json":
        print(json.dumps(
            [dict(zip(("type", "variant", "source", "device_name", "status"), r)) for r in rows],
            indent=2,
        ))
    else:
        for dtype, variant, source, resolved, status in rows:
            print(f"{dtype}\t{variant}\t{source}\t{resolved}\t{status}")
    return 0


def _load_variant_spec(cwd: Path, dtype: str, variant: str) -> dict[str, Any] | None:
    """Look up a variant's current spec from a checkout's recipe + local config.
    Returns None if the variant has been removed from both."""
    recipe = _load_recipe_or_empty(cwd)
    try:
        local = LocalConfig.load(cwd / LOCAL_NAME)
    except ValueError:
        local = LocalConfig({}, cwd / LOCAL_NAME)
    return merged_devices(recipe, local).get(dtype, {}).get(variant)


def _emit_progress(label: str, current: int, total: int) -> None:
    """Single-line progress on a TTY ('label: 3/12'). On a non-TTY (CI, pipe)
    print one line per call instead, so logs aren't a single mash of carriage
    returns. Caller should _finish_progress() after the loop to drop a newline."""
    width = len(str(total))
    msg = f"{label}: {current:>{width}}/{total}"
    if sys.stderr.isatty():
        sys.stderr.write(f"\r{msg}")
    else:
        sys.stderr.write(f"{msg}\n")
    sys.stderr.flush()


def _finish_progress() -> None:
    if sys.stderr.isatty():
        sys.stderr.write("\n")
        sys.stderr.flush()


def _resolve_fn(name: str, default):
    """Look up a function via sys.modules['splashdown'] so monkeypatch.setattr(sd, ...)
    in tests takes effect. Falls back to the local binding when the top-level module
    hasn't been imported yet (e.g., during module loading)."""
    import sys  # noqa: PLC0415
    _mod = sys.modules.get("splashdown")
    return getattr(_mod, name, default) if _mod else default


def cmd_device_gc(registry: Registry, *, all_: bool = False) -> int:
    """Splashdown-managed sim cleanup.

    Default: drop registry entries whose checkout dir is gone, destroy their sims.
    --all: additionally destroy sims whose recipe variant uses `ios = "latest"`
    and whose registered iOS is older than the current latest. Pinned variants
    are always preserved."""
    _udid_exists = _resolve_fn("_ios_udid_exists", _ios_udid_exists)
    _avd_exists = _resolve_fn("_android_avd_exists", _android_avd_exists)
    _destroy_ios = _resolve_fn("ios_destroy", ios_destroy)
    _destroy_avd = _resolve_fn("android_destroy", android_destroy)
    _get_latest_ios = _resolve_fn("_ios_latest_runtime_version", _ios_latest_runtime_version)
    destroyed_count = 0
    pruned_count = 0
    latest_ios: str | None = None
    rows = list(registry.all_devices())
    total = len(rows)
    for i, row in enumerate(rows, 1):
        _emit_progress("device gc", i, total)
        cwd = Path(row.checkout)
        if not cwd.exists():
            if row.dtype == "simulator" and _udid_exists(row.udid):
                _destroy_ios(row.udid)
            elif row.dtype == "emulator" and _avd_exists(row.udid):
                _destroy_avd(row.udid)
            registry.remove_device(row.checkout, row.dtype, row.variant)
            destroyed_count += 1
            continue
        if not all_:
            continue
        # `--all`: prune stale "latest" variants.
        spec = _load_variant_spec(cwd, row.dtype, row.variant)
        if spec is None:
            # Variant was removed from recipe + local — also destroy.
            if row.dtype == "simulator" and _udid_exists(row.udid):
                _destroy_ios(row.udid)
            registry.remove_device(row.checkout, row.dtype, row.variant)
            pruned_count += 1
            continue
        if row.dtype == "simulator":
            if spec.get("ios", "latest") != "latest":
                continue  # pinned — leave alone
            if latest_ios is None:
                latest_ios = _get_latest_ios()
            if row.ios == latest_ios:
                continue  # already fresh
            if _udid_exists(row.udid):
                _destroy_ios(row.udid)
            registry.remove_device(row.checkout, row.dtype, row.variant)
            pruned_count += 1
    _finish_progress()
    print(
        f"device gc: removed {destroyed_count} defunct + {pruned_count} stale entries",
        file=sys.stderr,
    )
    return 0


_PLATFORM_OF_DTYPE = {"simulator": "ios", "emulator": "android"}


def _latest_os(dtype: str, cache: dict[str, str]) -> str:
    """Current latest iOS runtime / Android image, memoized per command run
    (these shell out, so resolve at most once each)."""
    key = _PLATFORM_OF_DTYPE.get(dtype, dtype)
    if key not in cache:
        if dtype == "simulator":
            cache[key] = _resolve_fn("_ios_latest_runtime_version", _ios_latest_runtime_version)()
        else:
            cache[key] = _resolve_fn("_android_latest_image", _android_latest_image)()
    return cache[key]


def _target_os(dtype: str, spec: dict[str, Any], cache: dict[str, str]) -> str:
    """The OS version/image a variant should be on: its pinned value, or the
    current latest when declared `latest`."""
    requested = spec.get("ios" if dtype == "simulator" else "image", "latest")
    return _latest_os(dtype, cache) if requested == "latest" else requested


def _device_needs_recreate(row: DeviceRow, spec: dict[str, Any], cache: dict[str, str]) -> bool:
    """Mirror of ensure_fresh_sim's stale check (devices.py): the sim/AVD is
    gone, or its OS/model has drifted from the variant spec."""
    target = _target_os(row.dtype, spec, cache)
    if row.dtype == "simulator":
        if not _resolve_fn("_ios_udid_exists", _ios_udid_exists)(row.udid):
            return True
        return row.ios != target or row.model != spec.get("model", "")
    if row.dtype == "emulator":
        if not _resolve_fn("_android_avd_exists", _android_avd_exists)(row.udid):
            return True
        return row.ios != target or row.model != spec.get("device", "")
    return False


def _device_stale(row: DeviceRow, spec: dict[str, Any], cache: dict[str, str]) -> bool:
    """A present device whose declared-`latest` OS is behind what's now
    available — the `status --check` signal that `device refresh` will act on.
    Pinned variants are never stale."""
    requested = spec.get("ios" if row.dtype == "simulator" else "image", "latest")
    if requested != "latest":
        return False
    return row.ios != _latest_os(row.dtype, cache)


def cmd_device_refresh(
    registry: Registry, *, platforms: tuple[str, ...] = ("ios", "android"),
) -> int:
    """Eagerly reconcile every splashdown-managed device to its declared spec.

    Recreates each sim/AVD that is stale (declared `latest`, older OS now
    available) or missing-but-declared (incl. pinned variants whose sim was
    hand-deleted). Fresh ones are left alone. Rows for defunct checkouts or
    variants no longer declared are dropped (their sim destroyed). Recreation
    leaves the new sim Shutdown — nothing is booted, so no concurrency limits
    apply."""
    _udid_exists = _resolve_fn("_ios_udid_exists", _ios_udid_exists)
    _avd_exists = _resolve_fn("_android_avd_exists", _android_avd_exists)
    _destroy_ios = _resolve_fn("ios_destroy", ios_destroy)
    _destroy_avd = _resolve_fn("android_destroy", android_destroy)
    _fresh_sim = _resolve_fn("ensure_fresh_sim", ensure_fresh_sim)
    recreated = unchanged = dropped = 0
    cache: dict[str, str] = {}
    rows = [r for r in registry.all_devices() if _PLATFORM_OF_DTYPE.get(r.dtype) in platforms]
    total = len(rows)
    for i, row in enumerate(rows, 1):
        _emit_progress("device refresh", i, total)
        cwd = Path(row.checkout)
        spec = _load_variant_spec(cwd, row.dtype, row.variant) if cwd.exists() else None
        if spec is None:
            # Defunct checkout or undeclared variant: drop it, destroy its sim.
            if row.dtype == "simulator" and _udid_exists(row.udid):
                _destroy_ios(row.udid)
            elif row.dtype == "emulator" and _avd_exists(row.udid):
                _destroy_avd(row.udid)
            registry.remove_device(row.checkout, row.dtype, row.variant)
            dropped += 1
            continue
        # Decide before the call — ensure_fresh_sim is a no-op for fresh devices,
        # and the AVD name (Android's udid) is stable across recreation, so we
        # can't infer it from the return value.
        will_recreate = _device_needs_recreate(row, spec, cache)
        _fresh_sim(registry, cwd, row.dtype, row.variant, spec)
        if will_recreate:
            recreated += 1
        else:
            unchanged += 1
    _finish_progress()
    print(
        f"device refresh: recreated {recreated}, unchanged {unchanged}, dropped {dropped}",
        file=sys.stderr,
    )
    return 0


def cmd_device_prune(
    registry: Registry,
    *,
    yes: bool = False,
    dry_run: bool = False,
    platforms: tuple[str, ...] = ("ios", "android"),
) -> int:
    """Destroy every sim/AVD on this machine that splashdown did NOT create.
    Picks up the Xcode default-template pile, hand-made sims, etc.

    Splashdown-managed entries (those in the registry) are always preserved.
    Use --dry-run to preview, --yes to skip the prompt."""
    _xcrun = _resolve_fn("_xcrun_json", _xcrun_json)
    _ios_shut = _resolve_fn("ios_shutdown", ios_shutdown)
    _ios_del = _resolve_fn("ios_destroy", ios_destroy)
    _avd_shut = _resolve_fn("android_shutdown", android_shutdown)
    _avd_del = _resolve_fn("android_destroy", android_destroy)
    managed = registry.managed_udids()
    foreign_ios: list[tuple[str, str, str]] = []  # (udid, name, runtime)
    foreign_avd: list[str] = []

    if "ios" in platforms:
        try:
            data = _xcrun(["simctl", "list", "devices", "-j"])
        except DeviceError as e:
            print(f"warning: skipping iOS sims ({e})", file=sys.stderr)
        else:
            for runtime, devs in (data.get("devices") or {}).items():
                for d in devs:
                    udid = d.get("udid")
                    if not udid or udid in managed:
                        continue
                    if not d.get("isAvailable", True):
                        continue
                    foreign_ios.append((udid, d.get("name", "?"), runtime))

    if "android" in platforms:
        try:
            out = subprocess.check_output(
                [_android_bin("avdmanager"), "list", "avd", "-c"],
                stderr=subprocess.DEVNULL,
            )
        except (DeviceError, subprocess.CalledProcessError, FileNotFoundError):
            pass
        else:
            for line in out.decode().splitlines():
                name = line.strip()
                if name and name not in managed:
                    foreign_avd.append(name)

    total = len(foreign_ios) + len(foreign_avd)
    if total == 0:
        print("device prune: nothing to remove (every sim/AVD is splashdown-managed)",
              file=sys.stderr)
        return 0

    print(
        f"About to remove {total} {'/'.join(platforms)} device(s) not managed by splashdown:",
        file=sys.stderr,
    )
    for udid, name, runtime in foreign_ios:
        print(f"  simulator     {name}  ({runtime})  {udid}", file=sys.stderr)
    for name in foreign_avd:
        print(f"  android     {name}", file=sys.stderr)

    if dry_run:
        print("device prune: --dry-run, nothing destroyed", file=sys.stderr)
        return 0
    if not yes:
        print("Continue? [y/N] ", end="", file=sys.stderr, flush=True)
        if input().strip().lower() not in ("y", "yes"):
            print("device prune: aborted", file=sys.stderr)
            return 1

    done = 0
    for udid, _name, _runtime in foreign_ios:
        _ios_shut(udid)
        _ios_del(udid)
        done += 1
        _emit_progress("device prune", done, total)
    for name in foreign_avd:
        _avd_shut(name)
        _avd_del(name)
        done += 1
        _emit_progress("device prune", done, total)
    _finish_progress()
    print(f"device prune: removed {total} device(s)", file=sys.stderr)
    return 0


def cmd_run(cwd: Path, registry: Registry, dtype: str | None, variant_arg: str | None) -> int:
    """Reconcile the sim, boot it, then build + launch the app via the framework's CLI."""
    _fresh_sim = _resolve_fn("ensure_fresh_sim", ensure_fresh_sim)
    _boot_ios = _resolve_fn("ios_boot", ios_boot)
    _ios_state = _resolve_fn("_ios_current_state", _ios_current_state)
    _boot_android = _resolve_fn("android_boot", android_boot)
    _dev_run = _resolve_fn("device_run", device_run)
    dtype = _infer_dtype(cwd, dtype)
    variant, spec, recipe = _resolve_variant_for_cli(cwd, dtype, variant_arg)
    info = _fresh_sim(registry, cwd, dtype, variant, spec)
    if info["kind"] == "ios":
        _boot_ios(info["udid"], _ios_state(info["udid"]))
    elif info["kind"] == "android":
        info["serial"] = _boot_android(info["name"])
    return _dev_run(cwd, recipe, info)


def cmd_start(cwd: Path, registry: Registry, dtype: str | None, variant_arg: str | None) -> int:
    """Reconcile the sim, then boot it. No build/launch."""
    _fresh_sim = _resolve_fn("ensure_fresh_sim", ensure_fresh_sim)
    _boot_ios = _resolve_fn("ios_boot", ios_boot)
    _ios_state = _resolve_fn("_ios_current_state", _ios_current_state)
    _boot_android = _resolve_fn("android_boot", android_boot)
    dtype = _infer_dtype(cwd, dtype)
    variant, spec, _recipe = _resolve_variant_for_cli(cwd, dtype, variant_arg)
    info = _fresh_sim(registry, cwd, dtype, variant, spec)
    if info["kind"] == "ios":
        _boot_ios(info["udid"], _ios_state(info["udid"]))
    elif info["kind"] == "android":
        info["serial"] = _boot_android(info["name"])
    print(f"started {dtype}.{variant} ({info['name']})", file=sys.stderr)
    return 0


def cmd_stop(cwd: Path, dtype: str | None, variant_arg: str | None) -> int:
    """Shut down the sim/emulator (preserves it for next start)."""
    _dev_shutdown = _resolve_fn("device_shutdown", device_shutdown)
    dtype = _infer_dtype(cwd, dtype)
    variant, spec, _recipe = _resolve_variant_for_cli(cwd, dtype, variant_arg)
    resolved = _resolve_device_name(spec, cwd, variant, dtype)
    _dev_shutdown(dtype, resolved)
    print(f"stopped {dtype}.{variant} ({resolved})", file=sys.stderr)
    return 0


def cmd_destroy(cwd: Path, dtype: str | None, variant_arg: str | None) -> int:
    """Delete the sim/emulator and its registry entry."""
    _dev_destroy = _resolve_fn("device_destroy", device_destroy)
    dtype = _infer_dtype(cwd, dtype)
    variant, spec, _recipe = _resolve_variant_for_cli(cwd, dtype, variant_arg)
    resolved = _resolve_device_name(spec, cwd, variant, dtype)
    _dev_destroy(dtype, resolved)
    Registry().remove_device(str(cwd.resolve()), dtype, variant)
    print(f"destroyed {dtype}.{variant} ({resolved})", file=sys.stderr)
    return 0


def _infer_dtype(cwd: Path, dtype: str | None) -> str:
    """Resolve an unspecified TYPE arg to the only declared device type for
    this checkout, or error if there's not exactly one."""
    if dtype:
        return dtype
    recipe = _load_recipe_or_empty(cwd)
    local = LocalConfig.load(cwd / LOCAL_NAME)
    declared = [t for t, variants in merged_devices(recipe, local).items() if variants]
    if len(declared) == 1:
        return declared[0]
    if not declared:
        raise DeviceError(
            f"no devices declared in {RECIPE_NAME} or {LOCAL_NAME}"
        )
    raise DeviceError(
        f"multiple device types declared ({', '.join(sorted(declared))}); "
        "specify one: simulator | emulator"
    )


def _resolve_variant_for_cli(
    cwd: Path, dtype: str, variant_arg: str | None
) -> tuple[str, dict[str, Any], Recipe]:
    """Common prelude for `splash run`/`start`/`stop`/`destroy`: load recipe+local,
    merge, pick variant."""
    recipe = _load_recipe_or_empty(cwd)
    local = LocalConfig.load(cwd / LOCAL_NAME)
    catalog = merged_devices(recipe, local).get(dtype, {})
    variant, spec = resolve_variant(catalog, variant_arg)
    return variant, spec, recipe


def cmd_init(cwd: Path, preset: str | None = None, force: bool = False, loader_override: str | None = None) -> None:
    """Scaffold splashdown.toml from a project scan (default) or from a named
    preset (legacy path: `splash init --preset NAME`)."""
    from .profiles import SCAFFOLDS  # noqa: PLC0415
    recipe_path = cwd / RECIPE_NAME
    if recipe_path.exists() and not force:
        print(f"refusing to overwrite existing {RECIPE_NAME} (use --force)", file=sys.stderr)
        sys.exit(2)

    # Legacy path: an explicit preset bypasses the Scanner entirely.
    if preset is not None:
        return _cmd_init_legacy_preset(cwd, preset, loader_override=loader_override)

    # Scanner-driven path.
    from .scanner import PROFILES  # noqa: PLC0415
    inv = Scanner().scan(cwd)
    if loader_override:
        inv = ProjectInventory(workspace=inv.workspace, apps=inv.apps, loader=loader_override)

    print(f"scanning project…", file=sys.stderr)
    print(f"  detected: {inv.workspace} ({'/'.join(a.name for a in inv.apps) or 'no apps'})", file=sys.stderr)
    for app in inv.apps:
        rel = app.path.relative_to(cwd) if app.path != cwd else Path(".")
        print(f"  {rel}\t→ {app.profile}", file=sys.stderr)
    print(f"  shell loader\t→ {inv.loader}", file=sys.stderr)

    # Collect per-app resources, then merge with collision-mangling.
    res_by_app: dict[str, dict[str, dict[str, Any]]] = {}
    for app in inv.apps:
        if app.profile == "unknown":
            res_by_app[app.name] = {}
            continue
        profile = PROFILES[app.profile]
        res_by_app[app.name] = profile.resources(app)
    merged_resources = _merge_app_resources(inv.apps, res_by_app)
    app_resource_names = _app_resource_names(inv.apps, res_by_app)

    recipe_path.write_text(_render_scanned_recipe(inv, merged_resources, app_resource_names, cwd))
    print(f"wrote {RECIPE_NAME}", file=sys.stderr)

    local_path = cwd / LOCAL_NAME
    if not local_path.exists():
        local_path.write_text(LOCAL_SKELETON)
        print(f"wrote {LOCAL_NAME} (skeleton)", file=sys.stderr)

    _ensure_gitignore(cwd)
    LOADERS[inv.loader].wire(cwd)
    _ensure_post_checkout_hook(cwd)

    # Run consumer-side wiring (the Profiles' wiring checks).
    if any(app.profile != "unknown" for app in inv.apps):
        for app in inv.apps:
            if app.profile == "unknown":
                continue
            checks = PROFILES[app.profile].wiring_checks(app)
            for check in checks:
                if not check.applies(app.path):
                    continue
                status, _ = check.detect(app.path)
                if status != "ok" and check.autofix is not None:
                    try:
                        check.autofix(app.path)
                    except Exception as e:  # noqa: BLE001
                        print(f"  ✗ {check.id}: autofix failed: {e}", file=sys.stderr)


def _cmd_init_legacy_preset(cwd: Path, preset: str, *, loader_override: str | None = None) -> None:
    """`splash init NAME` path: write the named scaffold, then wire the
    detected (or overridden) shell-env loader and the post-checkout hook."""
    from .profiles import SCAFFOLDS  # noqa: PLC0415
    scaffold = SCAFFOLDS.get(preset)
    if scaffold is None:
        available = sorted(SCAFFOLDS)
        print(f"unknown preset `{preset}`; available: {', '.join(available)}", file=sys.stderr)
        sys.exit(2)
    loader_name = loader_override or _detect_loader(cwd)
    recipe_path = cwd / RECIPE_NAME
    recipe_path.write_text(scaffold.replace("__SPLASH_LOADER__", loader_name))
    print(f"wrote {RECIPE_NAME} (preset={preset})", file=sys.stderr)

    local_path = cwd / LOCAL_NAME
    if not local_path.exists():
        local_path.write_text(LOCAL_SKELETON)
        print(f"wrote {LOCAL_NAME} (skeleton)", file=sys.stderr)

    _ensure_gitignore(cwd)
    LOADERS[loader_name].wire(cwd)
    _ensure_post_checkout_hook(cwd)

    framework = _resolve_doctor_framework(cwd, None)
    if framework and _wiring_checks_for_framework(framework, cwd):
        print(f"running framework wiring for `{framework}`...", file=sys.stderr)
        cmd_doctor(cwd, fix=True)


def cmd_refresh_inventory(cwd: Path) -> int:
    """Re-scan and rewrite [project] / [apps.*] in splashdown.toml; preserve
    [resources.*] sections verbatim. Used both for picking up new apps and for
    upgrading legacy recipes to the new shape."""
    from .scanner import PROFILES  # noqa: PLC0415
    recipe_path = cwd / RECIPE_NAME
    if not recipe_path.exists():
        print(f"no {RECIPE_NAME} in {cwd}; run `splash init` instead", file=sys.stderr)
        return 1
    inv = Scanner().scan(cwd)

    res_by_app: dict[str, dict[str, dict[str, Any]]] = {}
    for app in inv.apps:
        if app.profile == "unknown":
            res_by_app[app.name] = {}
            continue
        res_by_app[app.name] = PROFILES[app.profile].resources(app)
    app_resource_names = _app_resource_names(inv.apps, res_by_app)

    # Preserve existing resource definitions verbatim.
    existing = recipe_path.read_text()
    preserved_resources = _extract_resource_blocks(existing)

    # Rebuild the recipe head ([project], [apps.*]) from the inventory; append
    # the existing [resources.*] blocks at the end. Any resource the Profile
    # newly emits is added only if it's not already in the preserved set.
    new_resources: dict[str, dict[str, Any]] = {}
    profile_emitted = _merge_app_resources(inv.apps, res_by_app)
    for name, spec in profile_emitted.items():
        if name not in preserved_resources:
            new_resources[name] = spec

    rebuilt = _render_scanned_recipe(inv, new_resources, app_resource_names, cwd)
    if preserved_resources:
        rebuilt = rebuilt.rstrip() + "\n\n" + "\n\n".join(preserved_resources.values()) + "\n"
    recipe_path.write_text(rebuilt)
    print(f"refreshed {RECIPE_NAME}: {len(inv.apps)} app(s), {len(preserved_resources) + len(new_resources)} resource(s)", file=sys.stderr)
    return 0


def _extract_resource_blocks(recipe_text: str) -> dict[str, str]:
    """Parse [resources.NAME] blocks out of a recipe, preserving their text.
    Returns {name: full_block_text}. Used to keep existing definitions intact
    when refresh-inventory rebuilds the head of the file."""
    out: dict[str, str] = {}
    lines = recipe_text.splitlines()
    i = 0
    while i < len(lines):
        m = re.match(r"\[resources\.([A-Za-z_][A-Za-z0-9_]*)\]", lines[i].strip())
        if not m:
            i += 1
            continue
        name = m.group(1)
        block = [lines[i]]
        i += 1
        while i < len(lines) and not (lines[i].strip().startswith("[") and lines[i].strip().endswith("]")):
            block.append(lines[i])
            i += 1
        # Drop trailing empty lines from the captured block.
        while block and not block[-1].strip():
            block.pop()
        out[name] = "\n".join(block)
    return out


def _render_scanned_recipe(
    inv: ProjectInventory,
    merged_resources: dict[str, dict[str, Any]],
    app_resource_names: dict[str, list[str]],
    cwd: Path,
) -> str:
    """Render the new recipe shape: [project], [apps.*], [resources.*]."""
    parts: list[str] = [
        "# splashdown.toml — auto-generated by `splash init`. Edit freely;",
        "# splashdown preserves comments and unknown keys on subsequent runs.",
        "",
        "[project]",
        f'workspace = "{inv.workspace}"',
        f'loader = "{inv.loader}"',
        "",
    ]
    for app in inv.apps:
        rel = "." if app.path == cwd else str(app.path.relative_to(cwd))
        parts.append(f"[apps.{app.name}]")
        parts.append(f'path = "{rel}"')
        parts.append(f'profile = "{app.profile}"')
        names = app_resource_names.get(app.name, [])
        parts.append("resources = [" + ", ".join(f'"{n}"' for n in names) + "]")
        parts.append("")
    if merged_resources:
        for res_name, spec in merged_resources.items():
            parts.append(f"[resources.{res_name}]")
            for k, v in spec.items():
                parts.append(f"{k} = {_toml_value(v)}")
            parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _toml_value(v: Any) -> str:
    if isinstance(v, str):
        return _toml_quote(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _cmd_provision(args: Any, cwd: Path, registry: Registry) -> int:
    return _cmd_provision_inner(
        cwd, registry,
        reprovision=args.reprovision,
        setup=args.setup,
        fmt=_resolve_format_arg(args),
    )


def _resolve_format_arg(args: Any) -> str:
    return getattr(args, "format", None) or "text"


def _cmd_provision_inner(
    cwd: Path, registry: Registry, *,
    reprovision: bool = False, setup: str | None = None, fmt: str = "text",
) -> int:
    try:
        resolved = provision(cwd, registry=registry, reprovision=reprovision)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 0
    except (ValueError, TemplateError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    recipe = Recipe.load(cwd / RECIPE_NAME)
    local_path = cwd / LOCAL_NAME
    if not local_path.exists():
        local_path.write_text(LOCAL_SKELETON)
    msgs = write_outputs(cwd, recipe, resolved)
    setup_msgs = run_setup(cwd, recipe, setup, resolved)

    if fmt == "json":
        print(json.dumps({"resolved": resolved, "writers": msgs, "setup": setup_msgs}, indent=2))
    else:
        for k, v in resolved.items():
            print(f"  {k}={v}", file=sys.stderr)
        for m in msgs + setup_msgs:
            print(f"  -> {m}", file=sys.stderr)
    return 0


def _device_dispatch(args: Any, cwd: Path) -> int:
    # Bare `splash device` → list devices (mirrors bare `splash` → provision).
    if args.device_cmd is None:
        return cmd_devices_list(cwd, _resolve_format_arg(args))

    if args.device_cmd == "add":
        fields = {
            "model": args.model,
            "ios": args.ios,
            "device": args.device,
            "image": args.image,
            "name": args.sim_name,
        }
        device_add(cwd, args.dtype, args.variant, fields)
        print(f"added device `{args.dtype}.{args.variant}` to {LOCAL_NAME}", file=sys.stderr)
        return 0

    if args.device_cmd == "remove":
        # Default: also destroy the instance — most users want both. Opt out
        # of state destruction with --keep-instance.
        _dev_destroy = _resolve_fn("device_destroy", device_destroy)
        variant_arg = args.variant
        if not args.keep_instance:
            spec = _load_variant_spec(cwd, args.dtype, variant_arg)
            if spec is not None:
                resolved = _resolve_device_name(spec, cwd, variant_arg, args.dtype)
                try:
                    _dev_destroy(args.dtype, resolved)
                except DeviceError:
                    pass  # sim may not exist yet; the toml edit still proceeds
                Registry().remove_device(str(cwd.resolve()), args.dtype, variant_arg)
        device_remove(cwd, args.dtype, variant_arg)
        suffix = "" if args.keep_instance else " (and destroyed the instance)"
        print(f"removed device `{args.dtype}.{variant_arg}` from {LOCAL_NAME}{suffix}", file=sys.stderr)
        return 0

    if args.device_cmd == "gc":
        return cmd_device_gc(Registry(), all_=False)

    if args.device_cmd == "refresh":
        platforms = ("ios", "android") if args.platform == "all" else (args.platform,)
        _refresh = _resolve_fn("cmd_device_refresh", cmd_device_refresh)
        return _refresh(Registry(), platforms=platforms)

    if args.device_cmd == "prune":
        platforms = ("ios", "android") if args.platform == "all" else (args.platform,)
        _prune = _resolve_fn("cmd_device_prune", cmd_device_prune)
        return _prune(
            Registry(), yes=args.yes, dry_run=args.dry_run, platforms=platforms,
        )

    print(f"splash device {args.device_cmd}: unknown action", file=sys.stderr)
    return 2
