"""splashdown — per-checkout resource and device provisioner.

Reads `splashdown.toml` (committed schema), allocates ports / generates uuids /
expands templates, and writes resolved values to `splashdown.env`. Per-checkout
device config lives in `splashdown.local.toml`. Maintains a machine-local
registry so concurrent checkouts don't collide.

Stdlib-only. Python 3.11+ (uses tomllib).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# ---------- paths & constants ----------

__version__ = "0.9.0"  # keep in sync with pyproject.toml

STATE_HOME = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
REGISTRY_DIR = STATE_HOME / "splashdown"
PORT_REGISTRY = REGISTRY_DIR / "ports.tsv"
KV_REGISTRY = REGISTRY_DIR / "kv.tsv"
DEVICE_REGISTRY = REGISTRY_DIR / "devices.tsv"

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEVICE_VARIANT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
DEVICE_TYPES = ("simulator", "emulator")
RECIPE_NAME = "splashdown.toml"
LOCAL_NAME = "splashdown.local.toml"
ENV_FILE_NAME = "splashdown.env"

# Re-export Path so tests can do `sd.Path` and monkeypatch it.
from pathlib import Path  # noqa: F811 (re-export)

# ---------- re-exports ----------
# Import submodules in dependency order so PROFILES gets populated before
# anything tries to use it (profiles.py imports scanner.PROFILES and fills it).

from .registry import Registry, DeviceRow, _port_in_use

from .recipe import (
    Recipe, LocalConfig, TemplateError, LOCAL_SKELETON,
    render_template, template_refs,
    merged_devices, resolve_variant, topo_sort,
    _make_scope, _find_table, _toml_quote,
)

from .provisioning import (
    provision, write_outputs, write_splashdown_env, write_envfile, write_envrc,
    run_setup,
)

from .devices import (
    DeviceError,
    device_status, device_shutdown, device_destroy,
    device_add, device_remove, device_run,
    ensure_fresh_sim,
    ios_boot, ios_destroy, ios_ensure, ios_shutdown,
    android_destroy, android_ensure, android_shutdown,
    _xcrun_json,
    _ios_udid_exists, _ios_current_state, _ios_latest_runtime_version,
    _android_avd_exists, _android_bin, _android_home, _android_latest_image,
    _android_running_serial,
    _short_path, _summary_string, _is_orphan_device, _device_status_for_row,
    detect_framework,
    _default_sim_name, _resolve_device_name,
)

from .scanner import (
    AppInventory, ProjectInventory, Scanner, PROFILES,
    _detect_workspace, _enumerate_apps, _expand_workspace_globs, _detect_loader,
    _merge_app_resources, _app_resource_names,
)

from .loaders import Loader, LOADERS

from .wiring import (
    WiringCheck, _RN_WIRING_CHECKS,
    _rn_hook_detect, _rn_metro_applies, _rn_metro_autofix, _rn_metro_detect,
    _rn_pkg_applies, _rn_pkg_autofix, _rn_pkg_detect,
    _rn_xcode_applies, _rn_xcode_autofix, _rn_xcode_detect,
    _XCODE_BEGIN, _XCODE_BLOCK, _XCODE_END,
    cmd_doctor,
)

# Import profiles — this populates PROFILES (and SCAFFOLDS).
from . import profiles as _profiles_module  # noqa: F401

from .profiles import Profile, SCAFFOLDS

from .commands import (
    cmd_init, _cmd_init_legacy_preset,
    cmd_status, cmd_refresh_inventory,
    cmd_device_gc, cmd_device_prune,
    cmd_devices_list,
    _detect_hook_manager, _ensure_post_checkout_hook,
    _wire_post_checkout_husky, _wire_post_checkout_lefthook,
    _extract_resource_blocks, _render_scanned_recipe,
)

from .cli import main, _build_parser, KNOWN_CMDS, _ensure_subcommand
