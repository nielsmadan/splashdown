"""splashdown — per-checkout resource and device provisioner.

Reads `splashdown.toml` (committed schema), allocates ports / generates uuids /
expands templates, and writes resolved values to `splashdown.env`. Per-checkout
device config lives in `splashdown.local.toml`. Maintains a machine-local
registry so concurrent checkouts don't collide.

Python 3.13+ (reads TOML via stdlib tomllib). Two runtime dependencies: argcomplete
(shell completion) and tomlkit (comment-preserving TOML writing). tomlkit is
isolated in a lazily imported module (see tomlio.py), so the git-hook hot path —
which only reads TOML — never loads it.
"""

from __future__ import annotations

from pathlib import Path

from ._version import resolve_version as _resolve_version
from .constants import (
    CLAIM_NOTICE_DAYS,
    CLAIM_NOTICE_REGISTRY,
    CLAIM_REGISTRY,
    DEVICE_REGISTRY,
    ENV_FILE_NAME,
    ENV_NAME_RE,
    GLOBAL_CONFIG_NAME,
    KV_REGISTRY,
    LOCAL_NAME,
    PORT_REGISTRY,
    RECIPE_NAME,
    REGISTRY_DIR,
    STATE_HOME,
    TARGET_TYPES,
    TARGET_VARIANT_RE,
    state_directory,
)


def __getattr__(name: str) -> object:
    # `__version__` is resolved lazily so importing the package (the git-hook
    # hot path) doesn't pay for a metadata lookup.
    if name == "__version__":
        return _resolve_version()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Import profiles first to populate PROFILES before catalog consumers load.
from . import capabilities as capabilities
from . import cli_output as cli_output
from . import device_android as device_android
from . import device_ios as device_ios
from . import device_tools as device_tools
from . import profiles as _profiles_module
from . import status as status
from .agentdocs import remove_agent_guidance, render_agent_guidance, sync_agent_guidance
from .bootstrap import (
    GitDirs,
    TrustState,
    bootstrap_complete,
    clear_bootstrap_completion,
    git_dirs,
    is_trusted,
    is_worktree_creation,
    mark_bootstrap_complete,
    record_trust,
    revoke_trust,
    trust_state,
)
from .catalog import PROFILES
from .cli import KNOWN_CMDS, _build_parser, _ensure_subcommand, main
from .cli_output import (
    _short_path,
    _summary_string,
    render_claim_notices,
    render_claim_rows,
    render_claim_selection,
    render_target_inventory,
)
from .commands import (
    InitOptions,
    _cmd_init_preset,
    _env_dispatch,
    _resolve_no_loader_delivery,
    cmd_completion,
    cmd_deinit,
    cmd_init,
    cmd_refresh_inventory,
    cmd_status,
)
from .device_claims import (
    ConfiguredPhysicalTarget,
    PhysicalSelection,
    claim_available_target,
    claim_configured_target,
    configured_physical_targets,
    discover_physical_snapshot,
    match_physical_target,
    notices_for_displaced,
    resolve_physical_target,
)
from .device_types import (
    AndroidDestination,
    ClaimAttempt,
    ClaimNotice,
    ClaimRelease,
    EmulatorRecord,
    IOSDestination,
    LaunchDestination,
    ManagedDevice,
    PhysicalClaim,
    SimulatorRecord,
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
    _xcrun_json,
    android_boot,
    android_destroy,
    android_ensure,
    android_shutdown,
    device_destroy,
    device_destroy_row,
    device_health,
    device_needs_recreate,
    device_shutdown,
    device_shutdown_row,
    device_status,
    ensure_fresh_sim,
    ensure_physical,
    ios_boot,
    ios_destroy,
    ios_ensure,
    ios_shutdown,
    ios_x86_64_target,
    physical_discover,
    physical_status,
)
from .doctor import cmd_doctor
from .errors import (
    ApplicationError,
    CapabilityError,
    MissingRecipeError,
    SetupError,
    UsageError,
)
from .hooks import (
    LEGACY_POST_CHECKOUT_HOOK,
    _detect_hook_manager,
    _ensure_post_checkout_hook,
    _native_hook_path,
    _remove_mise_file_directive,
    _revert_gitignore,
    _wire_post_checkout_husky,
    _wire_post_checkout_lefthook,
    _wire_post_checkout_native,
    post_checkout_readiness,
)
from .inventory import AppInventory, ProjectInventory, RunnableProfile
from .launching import (
    detect_framework,
    device_run,
    resolve_app_dir,
    validate_device_run,
)
from .loaders import LOADERS, Loader
from .profiles import (
    Profile,
    compose_project_resources,
    compose_wiring_checks,
)
from .provisioning import (
    WriterResult,
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
    CommandSpec,
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
from .registry import DeviceRow, Registry, _port_in_use
from .scaffolds import SCAFFOLDS
from .scanner import (
    Scanner,
    _build_resource_catalog,
    _detect_loader,
    _detect_workspace,
    _enumerate_apps,
    _expand_workspace_globs,
)
from .status import ClaimListRow, TargetInventoryRow
from .target_commands import (
    cmd_destroy,
    cmd_gc,
    cmd_run,
    cmd_start,
    cmd_stop,
    cmd_target_claim,
    cmd_target_claims,
    cmd_target_gc,
    cmd_target_prune,
    cmd_target_refresh,
    cmd_target_release,
    cmd_targets_list,
)
from .targets import global_target_add, global_target_remove, target_add, target_remove
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
)
