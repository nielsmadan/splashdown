"""splashdown — per-checkout resource and device provisioner.

Reads `splashdown.toml` (committed schema), allocates ports / generates uuids /
expands templates, and writes resolved values to `splashdown.env`. Per-checkout
device config lives in `splashdown.local.toml`. Maintains a machine-local
registry so concurrent checkouts don't collide.

Python 3.13+ (reads TOML via stdlib tomllib). Two runtime dependencies: argcomplete
(shell completion) and tomlkit (comment-preserving TOML writing). tomlkit is
lazy-imported by writer functions only (see tomlio.py), so the git-hook hot path —
which only reads TOML — never loads it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def _resolve_version() -> str:
    """Resolve the version from installed package metadata (single source of
    truth: pyproject.toml). Falls back to reading pyproject directly for an
    uninstalled source checkout (e.g. the test suite). Called lazily — never on
    the hot path — because a metadata lookup costs ~20ms."""
    from importlib.metadata import PackageNotFoundError, version  # noqa: PLC0415

    try:
        return version("splashdown")
    except PackageNotFoundError:
        import tomllib  # noqa: PLC0415

        try:
            pyproject = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
            return str(tomllib.loads(pyproject.read_text())["project"]["version"])
        except Exception:  # noqa: BLE001 — best-effort fallback; never block on version
            return "0.0.0+unknown"


def __getattr__(name: str) -> object:
    # `__version__` is resolved lazily so importing the package (the git-hook
    # hot path) doesn't pay for a metadata lookup.
    if name == "__version__":
        return _resolve_version()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


STATE_HOME = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
REGISTRY_DIR = STATE_HOME / "splashdown"
PORT_REGISTRY = REGISTRY_DIR / "ports.tsv"
KV_REGISTRY = REGISTRY_DIR / "kv.tsv"
DEVICE_REGISTRY = REGISTRY_DIR / "devices.tsv"

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TARGET_VARIANT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
TARGET_TYPES = ("simulator", "emulator", "device")
RECIPE_NAME = "splashdown.toml"
LOCAL_NAME = "splashdown.local.toml"
GLOBAL_CONFIG_NAME = "config.toml"
ENV_FILE_NAME = "splashdown.env"

# Re-export Path so tests can do `sd.Path` and monkeypatch it.
from pathlib import Path

# Import profiles — this populates PROFILES.
from . import profiles as _profiles_module
from .agentdocs import remove_agent_guidance, render_agent_guidance, sync_agent_guidance
from .cli import KNOWN_CMDS, _build_parser, _ensure_subcommand, main
from .commands import (
    _cmd_init_legacy_preset,
    _env_dispatch,
    _resolve_no_loader_delivery,
    cmd_completion,
    cmd_deinit,
    cmd_gc,
    cmd_init,
    cmd_refresh_inventory,
    cmd_status,
    cmd_target_gc,
    cmd_target_prune,
    cmd_target_refresh,
    cmd_targets_list,
)
from .devices import (
    DeviceError,
    DeviceHealth,
    _android_avd_exists,
    _android_bin,
    _android_home,
    _android_latest_image,
    _android_physical_devices,
    _android_running_serial,
    _default_sim_name,
    _device_status_for_row,
    _ios_current_state,
    _ios_latest_runtime_version,
    _ios_physical_devices,
    _ios_udid_exists,
    _is_orphan_device,
    _resolve_device_name,
    _short_path,
    _summary_string,
    _xcrun_json,
    android_boot,
    android_destroy,
    android_ensure,
    android_shutdown,
    detect_framework,
    device_destroy,
    device_destroy_row,
    device_health,
    device_needs_recreate,
    device_run,
    device_shutdown,
    device_status,
    ensure_fresh_sim,
    ensure_physical,
    global_target_add,
    global_target_remove,
    ios_boot,
    ios_destroy,
    ios_ensure,
    ios_shutdown,
    ios_x86_64_target,
    physical_discover,
    physical_status,
    resolve_app_dir,
    target_add,
    target_remove,
    validate_device_run,
)
from .hooks import (
    _detect_hook_manager,
    _ensure_post_checkout_hook,
    _remove_mise_file_directive,
    _remove_post_checkout_hook,
    _revert_gitignore,
    _wire_post_checkout_corehookspath,
    _wire_post_checkout_husky,
    _wire_post_checkout_lefthook,
)
from .loaders import LOADERS, Loader
from .profiles import (
    Profile,
    compose_project_resources,
    compose_wiring_checks,
)
from .provisioning import (
    provision,
    run_setup,
    write_envfile,
    write_envrc,
    write_outputs,
    write_splashdown_env,
)
from .recipe import (
    GLOBAL_SKELETON,
    LOCAL_SKELETON,
    GlobalConfig,
    LocalConfig,
    Recipe,
    Settings,
    TemplateError,
    _global_config_path,
    _make_scope,
    load_settings,
    merged_targets,
    render_template,
    resolve_variant,
    template_refs,
    topo_sort,
)

# Import submodules in dependency order so PROFILES gets populated before
# anything tries to use it (profiles.py imports scanner.PROFILES and fills it).
from .registry import DeviceRow, Registry, _port_in_use
from .scaffolds import SCAFFOLDS
from .scanner import (
    PROFILES,
    AppInventory,
    ProjectInventory,
    RunnableProfile,
    Scanner,
    _app_resource_names,
    _detect_loader,
    _detect_workspace,
    _enumerate_apps,
    _expand_workspace_globs,
    _merge_app_resources,
)
from .wiring import (
    _RN_WIRING_CHECKS,
    _XCODE_BEGIN,
    _XCODE_BLOCK,
    _XCODE_END,
    WiringCheck,
    _rn_hook_detect,
    _rn_metro_applies,
    _rn_metro_autofix,
    _rn_metro_detect,
    _rn_pkg_applies,
    _rn_pkg_autofix,
    _rn_pkg_detect,
    _rn_xcode_applies,
    _rn_xcode_autofix,
    _rn_xcode_detect,
    cmd_doctor,
)
