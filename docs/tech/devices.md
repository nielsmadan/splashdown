# devices.py — sim / emulator / physical-device lifecycle

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
  - [Physical devices: discovered, never created](#physical-devices-discovered-never-created)
  - [ensure_fresh_sim: reconcile-on-drift](#ensure_fresh_sim-reconcile-on-drift)
  - [The info handoff contract](#the-info-handoff-contract)
  - [Framework launch](#framework-launch)
- [Key entry points](#key-entry-points)
- [Gotchas](#gotchas)
- [Why](#why)
- [Related](#related)

## Purpose

`devices.py` is the largest module in `src/splashdown/`. It owns the full lifecycle of the
mobile-device half of a recipe: iOS simulators, Android emulators (AVDs), and physical hardware.
Each `[targets.<dtype>.<variant>]` table resolves through here to a concrete, bootable device
whose native id is then handed to a framework launcher. The three device types are deliberately
asymmetric: sims and emulators are *created/destroyed/reconciled* and tracked in the registry;
physical devices are only *discovered* and never persisted.

It also hosts the `splash target add/remove` writers and the `splash status --all` summary
formatting helpers — colocated here because they are about device variants, not because they are
device-lifecycle proper.

## How it works (current state)

### Naming: one sim per checkout per variant

The instance name is the isolation key. `_default_sim_name` (`devices.py:43`) builds
`<parent>/<basename>/<variant>-<path-hash>` from the checkout's `cwd` — for example,
`wrksp/dev1/default-352e9e09`. The readable prefix identifies the checkout, and the first eight
hexadecimal SHA-256 characters of `str(cwd.resolve())` keep matching path tails under different
roots distinct. The variant lets one checkout own several configs (default, lowest-supported, …).
`_resolve_device_name` (`devices.py:60`)
picks the name: an explicit `name` field on the variant wins (rendered as a template if it
contains `{{`), else the path default. For `emulator` targets the result is passed through
`_sanitize_avd_name` (`devices.py:53`) because `avdmanager` only accepts `[A-Za-z0-9._-]`; the
`/` separators become `_`. A leading `-` is rejected outright (`devices.py:74`) so the name can
never be parsed as a CLI flag by `simctl`/`avdmanager create`.

The suffix changed the generated identity for existing installations. Registry rows for iOS store
the stable UDID rather than the display name, so a healthy old simulator is not renamed
automatically. Destroy and recreate each default-named target once after upgrading. Explicit
`name` overrides are unaffected.

### iOS via xcrun simctl

Every iOS read goes through `_xcrun_json`, a thin wrapper that first requires macOS, runs
`xcrun simctl … -j`, and parses the result. A missing or non-executable `xcrun` becomes a typed
`CapabilityError`; a `simctl` process that starts and exits nonzero remains a command failure.
On top of it:

- `_ios_find_device_by_name` (`devices.py:92`) scans every runtime bucket of
  `simctl list devices -j` for an `isAvailable` device with the given name, returning
  `(udid, state)`.
- Version selection: `_ios_latest_runtime` (`devices.py:112`) and
  `_ios_latest_runtime_version` (`devices.py:117`) sort available runtimes by
  `_version_tuple` (`devices.py:158`) — which splits `"18.5"` into `(18, 5)` so `19.0 > 9.0`
  numerically, falling back to `(0,)` on a non-numeric string — and take the max. The
  `_version` variant returns the human string (`"18.5"`); the other returns the runtime
  identifier. `_ios_runtime_identifier` (`devices.py:181`) maps a pinned version back to the
  CoreSimulator identifier (`18.5` → `…SimRuntime.iOS-18-5`).
- `_ios_device_type_identifier` (`devices.py:186`) resolves the `model` field, or defaults to
  the lexically-last `iPhone … Pro` device type when none is given.
- `ios_ensure` (`devices.py:204`) is find-or-create: returns the existing sim's `(udid, state)`
  if present, otherwise `simctl create` and returns `(udid, "Shutdown")` — creation never boots.
- `ios_boot` / `ios_shutdown` / `ios_destroy` (`devices.py:252`, `:270`, `:280`) wrap
  `boot`/`shutdown`/`delete`. Boot tolerates the benign "current state: Booted" race from a
  concurrent boot; shutdown first checks `_ios_current_state` (`devices.py:829`) to skip the
  noisy 405 simctl raises when the sim is already shut down.

### Android via the SDK toolchain

`_android_home` (`devices.py:295`) resolves the SDK root from `$ANDROID_HOME`/`$ANDROID_SDK_ROOT`
or the platform defaults, and `_android_bin` (`devices.py:312`) locates each tool
(`avdmanager`, `sdkmanager`, `emulator`, `adb`) across the known SDK layout directories.
Missing SDK roots and tools raise `CapabilityError("android", ...)` with the setting or package to
install. The same implementation supports macOS and Linux.

- `_android_avd_exists` (`devices.py:329`) parses `avdmanager list avd -c`.
- `_android_latest_image` (`devices.py:354`) parses `sdkmanager --list_installed`, picks the
  highest installed `system-images;android-N;…` by API level, and falls back to a hard-coded
  known-good image name when nothing is installed.
- `android_ensure` (`devices.py:378`) is find-or-create via `avdmanager create avd … --force`,
  feeding `\n` on stdin to decline the custom-hardware-profile prompt; defaults `device` to
  `pixel_9` and `image` to the latest.
- Boot is async: `android_boot` (`devices.py:436`) spawns `emulator -avd <name>` detached
  (`start_new_session=True`, output to a per-AVD log under `REGISTRY_DIR`), then polls
  `_android_running_serial` (`devices.py:402`) for up to 60s. That matcher is the load-bearing
  bit — there is no AVD→serial map, so it lists `adb devices`, and for each `emulator-*` serial
  asks `adb -s <serial> emu avd name` to find the one whose reported AVD name matches.
- `android_shutdown` / `android_destroy` (`devices.py:466`, `:474`) issue `adb emu kill` and
  `avdmanager delete avd`.

### Capability boundaries

`capabilities.py` owns the dependency-free boundary shared by device and launcher code.
`require_macos` rejects Apple operations before process launch, while `translate_tool_errors`
converts only launch-time `OSError` into `CapabilityError`. The error carries a stable capability
key used by `warn_capability` to deduplicate aggregate warnings.

Explicit operations propagate the error to the CLI, which prints `error: ...` and exits 1.
Fleet operations catch it per row or platform, warn once, and continue supported work. A skipped
registry row is preserved so cleanup can be retried later on a capable host. See
[platform-capabilities.md](platform-capabilities.md) for the complete subprocess audit.

### Physical devices: discovered, never created

Physical hardware cannot be created/booted/destroyed; the only operation is *discovery*.
`_devicectl_json` (`devices.py:494`) wraps `xcrun devicectl … --json-output -` (Xcode 15+) the
same way `_xcrun_json` wraps simctl. `_ios_physical_devices` (`devices.py:515`) gates on
`pairingState == paired` (not `tunnelState == connected`, because wifi devices sit disconnected
until a launch-time tunnel) and excludes `unavailable` tunnels, returning
`{id (udid), name, platform: "ios"}`. `_android_physical_devices` (`devices.py:543`) parses
`adb devices -l`, skipping `emulator-*` serials (those are the `emulator` dtype), returning
`{id (serial), name, platform: "android"}`.

`physical_discover` merges both, and is forgiving by design: with `platform=None` a capability
failure for one platform produces one warning and the other discovery path still runs. An
*explicitly requested* platform re-raises its capability error. The optional shared `warned` set
deduplicates warnings across several target variants. `_physical_match` filters by the spec's `id` (exact) or `name`
(case-insensitive substring). `ensure_physical` (`devices.py:611`) requires exactly one match —
zero raises a setup hint (`_physical_no_match_msg`, `devices.py:635`), two-or-more raises an
"narrow with id/name/platform" error — and returns the `info` dict. `physical_status`
(`devices.py:644`) maps the same match to `connected`/`absent`/`ambiguous` for `splash targets`.

### ensure_fresh_sim: reconcile-on-drift

`device_health` is the shared, read-only reconciliation query. It returns `healthy`, `missing`,
`orphan`, `drifted`, or `undeclared`, and is consumed by both `status --check` and the actuator so
inspection cannot diverge from refresh. `ensure_fresh_sim` is the mutation entry point and
dispatches on `dtype`:

- `device` → delegates to `ensure_physical` (no registry, no reconcile).
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

The `if not stale:` block contains a `row is None` re-check that "never fires" — `stale` is True
whenever `row is None`, so the guard exists purely to narrow the Optional for mypy (`devices.py:753`).

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

### Framework launch (`launching.py`)

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
`device_status` / `device_shutdown` / `device_destroy` dispatchers (`devices.py:776`, `:789`,
`:798`) drive the `splash start/stop/destroy` subcommands by `dtype`.

The fixed launchers in `runners.py` use the same capability boundary for Flutter, `npx`,
`xcodebuild`, `xcrun`, Gradle/`gradlew`, and `adb`. Native iOS builds require macOS before project
validation or launch. User-authored `[project] run` commands remain shell boundaries and return
the shell's exit status.

## Key entry points

- `ensure_fresh_sim` — `devices.py:729` — the reconcile/allocate path; the only registry writer.
- `ensure_physical` — `devices.py:611` — physical-device resolution → `info`.
- `_default_sim_name` / `_resolve_device_name` — `devices.py:43`, `:60` — naming.
- `ios_ensure` / `android_ensure` — `devices.py:204`, `:378` — find-or-create.
- `physical_discover` — `devices.py:568` — toolchain-tolerant discovery.
- `CapabilityError` / `require_macos` / `translate_tool_errors` — `errors.py` and
  `capabilities.py` — typed host/tool availability boundary.
- `_xcrun_json` / `_devicectl_json` — `devices.py:82`, `:494` — subprocess JSON wrappers.
- `device_status` / `device_shutdown` / `device_destroy` — `devices.py:776`, `:789`, `:798`.
- `detect_framework` / `resolve_app_dir` / `device_run` — `launching.py:13`, `:40`, `:65`.
- `target_add` / `target_remove` — `devices.py:913`, `:965` — local-file variant writers.

## Gotchas

- **A reconciled `latest` sim is Shutdown, never booted.** `ensure_fresh_sim` returns after
  create; nothing in this module boots it. If a caller assumes "fresh sim" means "running sim",
  it is wrong.
- **Android has no identifier until boot.** Its destination uses `identifier=None` between
  reconciliation and boot; launch runners reject an unresolved destination rather than passing an
  empty device id to a tool.
- **`_resolve_device_name` renders name templates with an EMPTY resources scope.** It calls
  `_make_scope(cwd, branch, {})` (`devices.py:70`), so a template like
  `{{ SLUG }}-{{ VARIANT }}` works but anything referencing a resource (`{{ PORT }}`) raises —
  device names are resolved before resources exist.
- **Duplicate-named sims resolve to the first match.** `_ios_find_device_by_name` returns the
  first `isAvailable` device with that name across all runtime buckets; if two sims share a name
  (e.g. created manually) the later one is invisible to status/shutdown/destroy.
- **The TSV stays backward compatible.** Its historical `udid/model/ios` slots still encode
  simulator identifier/model/runtime or emulator name/device/image. Only the registry codec sees
  that layout; lifecycle code receives `SimulatorRecord` or `EmulatorRecord`.
- **Android boot has no map; it brute-forces.** `_android_running_serial` queries each emulator
  serial individually with a 2s timeout. A wedged emulator process can slow status calls.
- **Orphan vs stale are different.** `_is_orphan_device` (`devices.py:343`) flags a registry row
  whose underlying sim/AVD a user deleted by hand; `ensure_fresh_sim` treats a missing UDID/AVD
  as one trigger of `stale` and silently recreates.

## Why

**Auto-recreate on `latest`** exists so an Xcode or Android SDK update doesn't silently leave a
checkout pinned to a now-old OS while the developer thinks they are on the newest. The registry
row records what was created; when the live latest drifts past it, the stale sim is torn down and
rebuilt. Pinned variants encode an intentional version (e.g. "lowest supported OS, committed to
version control"), so they are explicitly exempt from this churn.

**Physical devices are not owned** because splashdown cannot meaningfully create or destroy
hardware, and writing them to the machine-wide registry would be meaningless across reboots and
unplugs. So they are discovered fresh each time and their native ids are passed straight through
to the launcher, never persisted.

## Related

- `docs/features/device-targets.md` — user-facing device-target model and TOML schema (authoritative
  for behavior).
- `docs/tech/registry.md` — the machine-wide TSV coordinator; `DeviceRow`, `get_device`,
  `set_device`, and the `devices.tsv` schema that `ensure_fresh_sim` reads and writes.
- `docs/tech/platform-capabilities.md` — host support and subprocess failure classification.
