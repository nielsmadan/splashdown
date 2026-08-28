# Device lifecycle and platform adapters

> Tech doc: how the code works. For the user-facing model (TOML schema, `splash run`, the
> `latest` vs pinned distinction), see `docs/features/device-targets.md` — that and `README.md` are
> authoritative for *behavior*; this file documents *internals*.

## Contents

- [Purpose](#purpose)
- [How it works (current state)](#how-it-works-current-state)
  - [Naming: one sim per checkout per variant](#naming-one-sim-per-checkout-per-variant)
  - [iOS via xcrun simctl](#ios-via-xcrun-simctl)
  - [Android via the SDK toolchain](#android-via-the-sdk-toolchain)
  - [Capability boundaries](#capability-boundaries)
  - [Physical discovery and claim selection](#physical-discovery-and-claim-selection)
  - [ensure_fresh_sim: reconcile-on-drift](#ensure_fresh_sim-reconcile-on-drift)
  - [The launch-destination contract](#the-launch-destination-contract)
  - [Framework launch](#framework-launch)
- [Key entry points](#key-entry-points)
- [Gotchas](#gotchas)
- [Why](#why)
- [Related](#related)

## Purpose

The device layer resolves each `[targets.<dtype>.<variant>]` table to a concrete launch
destination. `devices.py` owns cross-platform discovery, reconciliation, health, and typed
dispatch. `device_ios.py` owns Xcode, `simctl`, and `devicectl`; `device_android.py` owns the
Android SDK, AVD, emulator, and ADB operations. `device_tools.py` supplies the shared finite-call
deadline boundary. Simulators and emulators are *created/destroyed/reconciled* and tracked in the
managed-device registry. Physical hardware is only *discovered* and never persisted as a managed
device; checkout ownership is persisted separately as a claim.

Target catalog writers live in `targets.py`, while lifecycle and fleet command composition lives
in `target_commands.py`. Compatibility re-exports remain in this module for existing extensions.
Status report construction lives in `status.py`; text and JSON rendering live in `cli_output.py`.

## How it works (current state)

### Naming: one sim per checkout per variant

The instance name is the isolation key. `_default_sim_name` in `devices.py` builds
`<parent>/<basename>/<variant>-<path-hash>` from the checkout's `cwd` — for example,
`wrksp/dev1/default-352e9e09`. The readable prefix identifies the checkout, and the first eight
hexadecimal SHA-256 characters of `str(cwd.resolve())` keep matching path tails under different
roots distinct. The variant lets one checkout own several configs (default, lowest-supported, …).
`_resolve_device_name`
picks the name: an explicit `name` field on the variant wins (rendered as a template if it
contains `{{`), else the path default. For `emulator` targets the result is passed through
`_sanitize_avd_name` (`device_android.py`) because `avdmanager` only accepts `[A-Za-z0-9._-]`; the
`/` separators become `_`. A leading `-` is rejected by `_resolve_device_name` so the name can
never be parsed as a CLI flag by `simctl`/`avdmanager create`.

The suffix changed the generated identity for existing installations. Registry rows for iOS store
the stable UDID rather than the display name, so a healthy old simulator is not renamed
automatically. Destroy and recreate each default-named target once after upgrading. Explicit
`name` overrides are unaffected.

### iOS via xcrun simctl

Every iOS read goes through `_xcrun_json` in `device_ios.py`, a thin wrapper that first requires macOS, runs
`xcrun simctl … -j`, and parses the result. A missing or non-executable `xcrun` becomes a typed
`CapabilityError`; a `simctl` process that starts and exits nonzero remains a command failure.
On top of it:

- `_ios_find_device_by_name` scans every runtime bucket of
  `simctl list devices -j` for an `isAvailable` device with the given name, returning
  `(udid, state)`.
- Version selection: `_ios_latest_runtime` and `_ios_latest_runtime_version` sort available
  runtimes by `_version_tuple` — which splits `"18.5"` into `(18, 5)` so `19.0 > 9.0`
  numerically, falling back to `(0,)` on a non-numeric string — and take the max. The
  `_version` variant returns the human string (`"18.5"`); the other returns the runtime
  identifier. `_ios_runtime_identifier` maps a pinned version back to the
  CoreSimulator identifier (`18.5` → `…SimRuntime.iOS-18-5`).
- `_ios_device_type_identifier` resolves the `model` field, or defaults to
  the lexically-last `iPhone … Pro` device type when none is given.
- `ios_ensure` is find-or-create: returns the existing sim's `(udid, state)`
  if present, otherwise `simctl create` and returns `(udid, "Shutdown")` — creation never boots.
- `ios_boot` / `ios_shutdown` / `ios_destroy` wrap
  `boot`/`shutdown`/`delete`. Boot tolerates the benign "current state: Booted" race from a
  concurrent boot, then blocks on `simctl bootstatus -b` before returning. Shutdown first
  checks `_ios_current_state` to skip the noisy 405 simctl raises when the sim is already
  shut down.

### Android via the SDK toolchain

`_android_home` in `device_android.py` resolves the SDK root from
`$ANDROID_HOME`/`$ANDROID_SDK_ROOT` or the platform defaults, and `_android_bin` locates each tool
(`avdmanager`, `sdkmanager`, `emulator`, `adb`) across the known SDK layout directories.
Missing SDK roots and tools raise `CapabilityError("android", ...)` with the setting or package to
install. The same implementation supports macOS and Linux.

- `_android_avd_exists` parses `avdmanager list avd -c`.
- `_android_latest_image` parses `sdkmanager --list_installed`, picks the
  highest installed `system-images;android-N;…` by API level, and falls back to a hard-coded
  known-good image name when nothing is installed.
- `android_ensure` is find-or-create via `avdmanager create avd … --force`,
  feeding `\n` on stdin to decline the custom-hardware-profile prompt; defaults `device` to
  `pixel_9` and `image` to the latest.
- Boot is async: `android_boot` first looks for an existing serial and otherwise spawns
  `emulator -avd <name>` detached (`start_new_session=True`, output to a per-AVD log under the
  calling `Registry.state_dir`). Its 60-second deadline is wall-clock based, including discovery
  and readiness subprocesses. The matcher is the load-bearing bit — there is no AVD→serial map,
  so it lists `adb devices`, then asks each `emulator-*` serial for its AVD name. Once matched, the
  serial is retained and polled directly unless ADB reports a transport failure. Transport
  visibility is not readiness: `_android_boot_ready` also requires `sys.boot_completed=1` and the
  package service before Gradle can install an app.
- `android_shutdown` / `android_destroy` issue `adb emu kill` and
  `avdmanager delete avd`.

### Capability boundaries

`capabilities.py` owns the dependency-free boundary shared by device and launcher code.
`require_macos` rejects Apple operations before process launch, while `translate_tool_errors`
converts only launch-time `OSError` into `CapabilityError`. The error carries a stable capability
key used by `warn_capability` to deduplicate aggregate warnings.

`device_tools.py` bounds finite external operations: discovery/list/status calls get 30 seconds,
and create/install/delete/shutdown calls get 120 seconds. A timeout becomes a `DeviceError` that
names the operation. The per-emulator ADB name probe keeps its 2-second ceiling, while emulator
boot uses a 60-second wall-clock deadline and caps every discovery/readiness subprocess by the
remaining budget. Native iOS post-build settings discovery also uses the 120-second budget because
concurrent Xcode work can serialize it behind another build. Framework builds, attached launchers,
and user-authored commands remain unbounded when they are intentionally long-running.

Explicit operations propagate the error to the CLI, which prints `error: ...` and exits 1.
Fleet operations catch it per row or platform, warn once, and continue supported work. A skipped
registry row is preserved so cleanup can be retried later on a capable host. See
[platform-capabilities.md](platform-capabilities.md) for the complete subprocess audit.

### Physical discovery and claim selection

Physical hardware cannot be created/booted/destroyed; the only operation is *discovery*.
`_devicectl_json` in `device_ios.py` wraps `xcrun devicectl … --json-output -` (Xcode 15+) the
same way `_xcrun_json` wraps simctl. `_ios_physical_devices` gates on
`pairingState == paired` (not `tunnelState == connected`, because wifi devices sit disconnected
until a launch-time tunnel) and excludes `unavailable` tunnels, returning
`{id (udid), name, platform: "ios"}`. `_android_physical_devices` in `device_android.py` parses
`adb devices -l`, skipping `emulator-*` serials (those are the `emulator` dtype), returning
`{id (serial), name, platform: "android"}`.

`physical_discover` in `devices.py` merges both for legacy resolution and status, and is forgiving
by design: with `platform=None` a capability
failure for one platform produces one warning and the other discovery path still runs. An
*explicitly requested* platform re-raises its capability error. The optional shared `warned` set
deduplicates warnings across several target variants. `_physical_match` filters by the spec's `id` (exact) or `name`
(case-insensitive substring). `ensure_physical` in `devices.py` requires exactly one match —
zero raises a setup hint from `_physical_no_match_msg`, two-or-more raises an
"narrow with id/name/platform" error — and returns an `IOSDestination` or
`AndroidDestination` with `owned=False`. `physical_status` maps the same match to
`connected`/`absent`/`ambiguous` for operational `splash status` checks.

Bare `splash target` uses the claim-aware inventory path instead. It runs one concurrent iOS and
Android snapshot under the shared interactive deadline, matches every configured physical target
against that snapshot, and renders connection state independently from registry ownership. A
configured target with no match renders `disconnected`; it can still render `claimed` when its
catalog identity has a persisted claim. A platform capability failure renders that platform's
targets `unavailable` without discarding the other platform's snapshot.

Claim-aware allocation lives in `device_claims.py`. `configured_physical_targets` builds the only
eligible catalog from recipe, local, then global declarations, preserving declaration order within
each source. It assigns a catalog identity scoped to the Git common directory for recipe targets,
the checkout for local targets, and the global config path for global targets. Discovered hardware
that has no configured target is never considered.

`discover_physical_snapshot` invokes each requested platform once. An `any` request runs iOS and
Android discovery concurrently under one deadline. Interactive claims and physical runs use the
normal 30-second discovery budget. The worktree hook passes a five-second total budget.
`match_physical_target` applies every candidate's selectors to the shared snapshot and returns a
typed destination only for exactly one match.

Specific selection calls `Registry.attempt_claim` after matching. Generic selection reads one
claim snapshot, walks the configured catalog, skips disconnected, ambiguous, busy, and
already-owned candidates, and tries later candidates from the same device snapshot when it loses a
claim race. Device discovery never runs under the claims-file lock. Registry transactions enforce
both catalog-identity and `(platform, hardware_id)` conflicts, so aliases cannot allocate the same
phone and recipe targets collide across linked worktrees.

### ensure_fresh_sim: reconcile-on-drift

`device_health` is the shared, read-only reconciliation query. It returns `healthy`, `missing`,
`orphan`, `drifted`, or `undeclared`, and is consumed by both `status --check` and the actuator so
inspection cannot diverge from refresh. `ensure_fresh_sim` is the mutation entry point and
dispatches on `dtype`:

- `device` → delegates to `ensure_physical` (no managed-device row and no reconcile). Physical
  `run` uses the claim-aware path in `device_claims.py` instead.
- `simulator` / `emulator` → resolve the target OS image (`latest` expands to the live latest
  via `_ios_latest_runtime_version` / `_android_latest_image`; an explicit value is taken
  verbatim, i.e. *pinned*). It reads the registry row for `(checkout, dtype, variant)` and
  asks `device_health` for the shared result. Simulator drift compares runtime and model;
  emulator drift compares AVD name, image, and device profile. A missing underlying instance
  is `orphan`, while a missing registry row is `missing`. The registry codec exposes the
  legacy columns as platform-specific simulator/emulator records.

When not stale, it returns the cached `info` unchanged. When stale, it routes the registry row
through `device_destroy_row`, which shuts down any running instance before deleting it. The same
operation handles dead-checkout GC and undeclared rows during fleet refresh, so every registered
device follows one teardown policy. Reconciliation then recreates the device via
`ios_ensure` / `android_ensure` and writes a `SimulatorRecord` or `EmulatorRecord`. **The recreated sim is
left Shutdown** — `ios_ensure` returns `"Shutdown"` and `ensure_fresh_sim` never boots it; booting
is a separate, explicit step. A pinned variant's OS does not drift merely because a newer runtime
or image is installed; edits to its model, device profile, or emulator name can still require
recreation. `latest` variants additionally drift when Xcode/SDK moves the floor.

### The launch-destination contract

Every resolve path returns a discriminated `IOSDestination` or `AndroidDestination`, which is
the handoff to the launcher. Both expose `platform`, `name`, `identifier`, and `owned`; their
types narrow the native identifier and rule out cross-platform key combinations.

- iOS simulator → `IOSDestination(name, udid, owned=True)`
- Android emulator before boot → `AndroidDestination(name, None, owned=True)`; boot returns a
  replacement carrying the resolved ADB serial
- physical hardware → the platform destination with `owned=False`

Compatibility mapping access still accepts `kind`/`udid`/`serial` for callers of the old internal
helpers, but production consumers use the typed attributes.

### Framework launch

`detect_framework` (`launching.py`) honors a `[project] framework` override (`"auto"` means the
same as omitting it), else probes every registered `Profile.detect` in `PROFILES` insertion order.
Runner validation later rejects a detected profile that is not runnable. Failing root detection,
it falls back to the recipe's declared app profiles: exactly one app with a profile other than
`"unknown"` wins, and two or more raise
`DeviceError` naming each app and its profile. The fallback keys on app *name*, not the set of
profiles — two apps sharing a profile are still two apps, and collapsing them would resolve an
ambiguous workspace as if it were unambiguous.

`resolve_app_dir` (`launching.py`) then answers *where* that framework lives: `cwd` when the
root itself matches, else the single declared app's `path`. Both `device_run` and `cmd_doctor`
resolve it, because wiring checks patch files inside the app directory and launchers shell out
there — running either at the workspace root silently does nothing useful.

Runnable profiles structurally implement `RunnableProfile`; web/backend profiles do not expose a
`run` method. `cmd_run` checks this capability (or a matching custom `[project] run`) before device
reconciliation or boot. `launching.device_run` repeats the capability check as a defensive boundary, then
delegates to `PROFILES[fw].run(app_dir, recipe, destination)` — the per-profile launcher consumes the typed destination above
(`flutter run -d <udid/serial>`, `xcodebuild`/`simctl`, `gradle`, etc.). The generic
`device_status` / `device_shutdown` / `device_destroy` dispatchers in `devices.py` drive the
`splash start/stop/destroy` subcommands by `dtype`.

The fixed launchers in `runners.py` use the same capability boundary for Flutter, `npx`,
`xcodebuild`, `xcrun`, Gradle/`gradlew`, and `adb`. Native iOS builds require macOS before project
validation or launch; product lookup, simulator install, and simulator launch use the mutation
deadline. Android-native launch reads the selected variant's `applicationId` from AGP's
`output-metadata.json`, with the Gradle properties query retained as a fallback. Unless
`launch_activity` is configured, it resolves the installed package's LAUNCHER component through
`cmd package resolve-activity` and passes that component to `am start -n`. User-authored
`[project] run` commands remain shell boundaries and return the shell's exit status.

## Key entry points

- `ensure_fresh_sim` — `devices.py` — the reconcile/allocate path; the only registry writer.
- `ensure_physical` — `devices.py` — physical-device resolution → destination.
- `_default_sim_name` / `_resolve_device_name` — `devices.py` — naming.
- `ios_ensure` / `android_ensure` — `device_ios.py` / `device_android.py` — find-or-create.
- `physical_discover` — `devices.py` — toolchain-tolerant cross-platform discovery.
- `configured_physical_targets` / `discover_physical_snapshot` / `match_physical_target` —
  `device_claims.py` — configured catalog, one-snapshot discovery, and matching.
- `claim_configured_target` / `claim_available_target` — `device_claims.py` — specific and generic
  physical allocation orchestration.
- `PhysicalClaim` / `ClaimNotice` / `ClaimAttempt` / `ClaimRelease` — `device_types.py` —
  dependency-free storage and orchestration records.
- `CapabilityError` / `require_macos` / `translate_tool_errors` — `errors.py` and
  `capabilities.py` — typed host/tool availability boundary.
- `_xcrun_json` / `_devicectl_json` — `device_ios.py` — subprocess JSON wrappers.
- `_android_avd_names` / `_android_running_serial` — `device_android.py` — AVD discovery.
- `run_finite` / `check_output_finite` — `device_tools.py` — finite-operation deadlines.
- `device_status` / `device_shutdown` / `device_destroy` — `devices.py` — typed dispatch.
- `detect_framework` / `resolve_app_dir` / `device_run` — `launching.py`.
- `target_add` / `target_remove` — `targets.py` — local-file variant writers, re-exported here for compatibility.

## Gotchas

- **A reconciled `latest` sim is Shutdown, never booted.** `ensure_fresh_sim` returns after
  create; nothing in this module boots it. If a caller assumes "fresh sim" means "running sim",
  it is wrong.
- **Android has no identifier until boot.** Its destination uses `identifier=None` between
  reconciliation and boot; launch runners reject an unresolved destination rather than passing an
  empty device id to a tool.
- **`_resolve_device_name` renders name templates with an EMPTY resources scope.** It calls
  `_make_scope(cwd, branch, {})`, so a template like
  `{{ SLUG }}-{{ VARIANT }}` works but anything referencing a resource (`{{ PORT }}`) raises —
  device names are resolved before resources exist.
- **Duplicate-named sims resolve to the first match.** `_ios_find_device_by_name` returns the
  first `isAvailable` device with that name across all runtime buckets; if two sims share a name
  (e.g. created manually) the later one is invisible to status/shutdown/destroy.
- **The TSV stays backward compatible.** Its historical `udid/model/ios` slots still encode
  simulator identifier/model/runtime or emulator name/device/image. Only the registry codec sees
  that layout; lifecycle code receives `SimulatorRecord` or `EmulatorRecord`.
- **Android boot has no map; it brute-forces.** `_android_running_serial` bounds `adb devices` at
  30 seconds, then queries each emulator serial individually with a 2-second ceiling. During boot,
  both limits are capped by the remaining wall-clock budget.
- **Orphan vs stale are different.** `_is_orphan_device` in `devices.py` flags a registry row
  whose underlying sim/AVD a user deleted by hand; `ensure_fresh_sim` treats a missing UDID/AVD
  as one trigger of `stale` and silently recreates.

## Why

**Auto-recreate on `latest`** exists so an Xcode or Android SDK update doesn't silently leave a
checkout pinned to a now-old OS while the developer thinks they are on the newest. The registry
row records what was created; when the live latest drifts past it, the stale sim is torn down and
rebuilt. Pinned variants encode an intentional version (e.g. "lowest supported OS, committed to
version control"), so they are explicitly exempt from this churn.

**Physical hardware is not lifecycle-owned** because Splashdown cannot meaningfully create or
destroy it. Hardware is discovered fresh and its native ID is passed straight to the launcher.
The separate claim registry persists checkout ownership because process lifetime, reboots, and
temporary disconnects must not let another live worktree silently take the same configured phone.

## Related

- `docs/features/device-targets.md` — user-facing device-target model and TOML schema (authoritative
  for behavior).
- `docs/tech/registry.md` — the machine-wide TSV coordinator; `DeviceRow`, `get_device`,
  `set_device`, and the `devices.tsv` schema that `ensure_fresh_sim` reads and writes.
- `docs/tech/platform-capabilities.md` — host support and subprocess failure classification.
