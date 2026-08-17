# Per-checkout device targets

> Use cases covered: **UC2** (run the app on a sim/emulator that belongs to *this* checkout),
> **UC4** (keep `latest` sims fresh after an Xcode / Android SDK update), **UC10** (commit the
> lowest-supported-OS sim so the device matrix is version control, not tribal knowledge).
> Audience: the **mobile-app developer on worktrees** persona (`docs/product/persona.md`) — and,
> through them, the **parallel-agent developer** who needs each agent's worktree to own its own
> device. `README.md` is the authoritative spec.
> **Implemented by:** [devices](../tech/devices.md), [registry](../tech/registry.md).

## Contents

- [Overview](#overview)
- [How it works (current state)](#how-it-works-current-state)
- [Key entry points](#key-entry-points)
- [Configuration](#configuration)
- [Gotchas](#gotchas)
- [Why](#why)

## Overview

A mobile developer keeps several worktrees of one app open at once (feature branch, hotfix, PR
review) and can't tell which simulator holds which build — or two worktrees fight over a single
booted sim and the Metro port. splashdown gives **each checkout its own named sim/emulator
instance**, derived from the checkout path so worktrees and clones never collide, and drives its
whole lifecycle with four verbs:

- `splash run [type] [variant]` — reconcile the instance, boot it, build, install, launch (UC2).
- `splash start` / `splash stop` / `splash destroy` — boot-only / shutdown / delete.

Which devices a project supports is **code**: the committed recipe declares a `[targets.*]`
catalog (UC10), and per-checkout add-only variants live in the gitignored local file. Variants
declared `ios = "latest"` auto-recreate when a newer iOS lands (UC4); pinned variants like
`ios = "17.0"` are deliberate version coverage and are never upgraded. `splash target
refresh`/`prune` manage the machine-wide fleet. Physical devices are **discovered, not owned** —
splashdown hands the connected device's native id to the launcher but never creates or destroys
hardware.

| Target | macOS | Linux |
| --- | --- | --- |
| iOS simulator/device | Xcode required | Unsupported; explicit commands return an actionable error |
| Android emulator/device | Android SDK required | Android SDK required |
| Ports, environment, and config | Supported | Supported |

## How it works (current state)

**Instance naming and isolation.** Every sim/emulator instance is named
`<parent-dir>/<checkout-name>/<variant>-<path-hash>` (`src/splashdown/devices.py`,
`_default_sim_name`). The readable path component identifies the checkout, while the first eight
hexadecimal characters of SHA-256 over its resolved absolute path prevent unrelated clones with
the same trailing directories from colliding. The variant lets one checkout host several configs
(`default`, `lowest-supported`, …). A per-variant `name =
"..."` (literal or template) overrides the default; for emulators the name is sanitized to
avdmanager's `[A-Za-z0-9._-]` (`_resolve_device_name`; `_sanitize_avd_name`).

**Reconcile (the heart of UC2 and UC4).** `device_health` classifies the registry's recorded
instance as healthy, missing, orphaned, drifted, or undeclared. `ensure_fresh_sim` and
`splash status --check` consume that same result, so inspection and refresh cannot disagree.
The instance needs reconciliation when its registry row is missing, its underlying sim/AVD no
longer exists, or its OS image, model, device profile, or emulator name has drifted from the spec.
For `ios = "latest"` the target OS is resolved live
(`_ios_latest_runtime_version`, `src/splashdown/device_ios.py`); for a pinned `ios = "17.0"` the
target *is* `17.0`, so a stale check never fires on OS — that is what makes pinned variants
permanent (UC10). When stale, the old sim is destroyed and a fresh one created in place, and the
new instance is recorded in the machine-wide registry (`registry.set_device`). Crucially,
reconcile leaves the new sim **Shutdown** — it never boots anything, so the OS-imposed
"too many booted simulators" limit never applies during a fleet-wide refresh.

**Run vs start vs stop vs destroy.** `cmd_run` validates that the selected profile has a launcher
(or that `[project] run` supplies one) before it reconciles or boots anything. It then calls
`ensure_fresh_sim`, boots (`ios_boot` / `android_boot`), and builds + launches via `device_run`.
`cmd_start` reconciles + boots but skips the build/launch. `cmd_stop`
shuts the registered instance down but preserves it. `cmd_destroy` deletes the registered instance
and its registry row, gated behind a `[y/N]` prompt (`_confirm`,
`src/splashdown/target_commands.py`), bypassable with `--yes`. For `type = device`, stop/destroy are
no-ops with an explanatory message because splashdown owns no hardware. Managed teardown uses the
persisted simulator UDID or AVD name; if no registry row exists it reports a no-op and never falls
back to a recipe-derived name.

**Type and variant inference.** When the user omits `TYPE`, `_infer_dtype`
(`src/splashdown/target_commands.py`) resolves it to the single declared target type, or errors with
the list when there are zero or several. `_resolve_variant_for_cli` (`:1056`) loads the full
recipe + local + global catalog and picks the variant (`resolve_variant`): an explicit arg,
else `default`, else the sole declared variant.

**Global targets are a *resolution*-scope concept, never an *inference*-scope one.** Type inference
and type-prefix matching call `_declared_target_types(cwd, include_global=False)` — the project's own
recipe + local types only — and fall back to the global-inclusive list only when the project declares
none (`src/splashdown/target_commands.py`; `src/splashdown/cli.py`). Resolution
(`_resolve_variant_for_cli`, `_gather_targets_declared`) always uses the full merged catalog. Folding
the always-available global `device` type into inference instead would make bare
`splash run`/`start`/`stop`/`destroy` fail with `multiple target types declared (device, simulator)`
in *every* mobile repo the moment a user adds one global test phone, break the `splash run d`
type-prefix invariant in a sim-only project, and break variant completion. The CLI parser additionally reinterprets a lone non-type token as the
variant (`splash run lowest-supported`) in `cli.py` (validated post-parse by
`_normalize_device_args` (`src/splashdown/cli.py:328`)).

**Prefix matching.** When the `prefix_match` setting is on (the default — see
[per-checkout-overrides](per-checkout-overrides.md#settings)), both `TYPE` and `VARIANT` accept
any unambiguous prefix. `_normalize_device_args` expands an abbreviated type token against the types
the checkout *declares* (via `_declared_target_types`) — `sim` → `simulator` — and
`resolve_variant`'s `prefix_match` arg expands a variant prefix against the merged catalog (unique
hit wins; 2+ matches error with the candidate list). Scoping the type match to declared types is
deliberate: a short token like `d` in a simulator-only project is *not* claimed by the undeclared
`device` type — it stays in the variant slot and resolves e.g. `default`. A type prefix wins over an
identically-prefixed variant of a declared type. With the setting off, both slots require exact names
(today's behavior). `load_settings` (`src/splashdown/recipe.py`) resolves the flag from the global
config + local file.

**Framework launcher (UC2 build/launch).** `detect_framework` (`src/splashdown/launching.py`)
picks the launcher from filesystem signals, overridable via `[project] framework`, and falls back
to the recipe's declared app profiles when the repo root matches nothing (erroring when more than
one app is declared). `device_run` (`src/splashdown/launching.py`) dispatches to that profile's
`run`, rooted at `resolve_app_dir` in the same module so a monorepo app in a subdirectory builds in its own
directory rather than the workspace root. Launchers:
flutter (`flutter run -d <id>`), react-native, expo, ios-native (`xcodebuild build` then `xcrun
simctl install`/`launch`, or `xcrun devicectl` for a physical device — `_ios_native_run`,
`src/splashdown/runners.py:265`), and android-native (`./gradlew :module:installVariant` then
`adb shell am start` — `_android_native_run`, `src/splashdown/runners.py:364`).
Missing fixed launchers and non-executable SDK tools become actionable errors without a traceback;
a launcher that starts and exits nonzero keeps its normal exit status.

**Auto-upgrade after an Xcode/SDK bump (UC4).** Two paths:
1. *Lazy*, on `splash run`: `ensure_fresh_sim` recreates the one sim being run if its `latest` OS
   is now behind. This is the zero-effort path the persona hits daily.
2. *Eager*, fleet-wide: `splash target refresh` → `cmd_target_refresh`
   walks every registry device row, first loading every relevant live checkout's
   recipe/local/global catalog. Any malformed config aborts the entire sweep before mutation.
   It then recreates each that is stale
   or missing-but-declared (including pinned variants whose sim was hand-deleted), leaves fresh ones
   alone, and drops rows for defunct checkouts or undeclared variants. Every registered instance is
   shut down before deletion, whether teardown comes from reconcile, refresh, GC, or explicit
   removal. Like
   reconcile, it leaves recreated sims **Shutdown**. The recreate decision is taken *before* the
   call (`device_needs_recreate`) because `ensure_fresh_sim` is a no-op for fresh devices
   and the AVD name is stable across recreation, so the return value can't reveal what happened.
   With no platform argument, an unavailable platform is warned about once and skipped while the
   other platform continues. `target refresh ios` is strict and returns exit 1 when iOS support is
   unavailable.

**Pruning the template pile (UC4 cleanup).** `splash target prune` → `cmd_target_prune`
(`src/splashdown/target_commands.py`) destroys every sim/AVD on the machine that splashdown did *not*
create — the Xcode default-template pile, hand-made sims — by diffing live sims/AVDs against
`registry.managed_udids()` (`_discover_foreign_ios:826`, `_discover_foreign_avds:842`). It prints
the kill list, honors `--dry-run` (preview only) and `--yes` (skip the `_confirm` prompt), and is
scoped by an optional `ios|android|all` platform argument.
The default `all` scope skips an unavailable platform with one warning; an explicit `ios` or
`android` scope propagates the capability error before destructive work on that platform.

**Committing the lowest-supported sim (UC10).** A pinned variant in the committed recipe (e.g.
`[targets.simulator.lowest-supported]` with `model = "iPhone 12"`, `ios = "17.0"`) makes the
backward-compat device part of the repo. `merged_targets` unions the recipe catalog with add-only
local variants so any checkout — or any agent — resolves the same matrix. Because the OS is pinned,
neither `ensure_fresh_sim` nor `target refresh` upgrades a healthy instance. A missing pinned
instance is recreated at its declared version, while GC treats it like any other registered row.

**Physical devices are discovered, not owned.** A `device` target resolves to a *connected* phone
(`ensure_physical`, `src/splashdown/devices.py`; `_physical_match` filters by
`platform`/`id`/`name`). `cmd_run` skips booting for `info["physical"]` and goes straight to the
launcher (`src/splashdown/target_commands.py`). Physical devices are never written to the registry;
`status`/`target` show `connected` / `absent` / `ambiguous` (`physical_status:644`).

**Declaring variants programmatically.** `splash target add` writes an add-only
`[targets.<type>.<variant>]` table into the gitignored local file (`target_add`,
`src/splashdown/targets.py`). Before writing, it applies the same type-specific target schema
used for recipe, local, and global files, rejects incompatible CLI flags, renders the new document
in memory, and validates the complete result. `splash target remove` first verifies that the variant
belongs to the local file and computes the edited TOML, then destroys the instance, writes the
declaration change, and removes its registry row unless `--keep-instance`. When a registry row exists,
deletion uses its actual simulator UDID or AVD name rather than a newly resolved config name.
A recipe-owned or missing variant, or malformed recipe/local file, is rejected before any device
operation. If the lifecycle step raises, the local declaration and registry row remain intact.
Adding a variant that already exists in the recipe is an error.

## Key entry points

| Concern | Location |
|---|---|
| Instance name `<parent>/<cwd>/<variant>-<path-hash>` | `src/splashdown/devices.py` (`_default_sim_name`); override resolution in `_resolve_device_name` |
| Reconcile (create / recreate-on-drift / pin) | `src/splashdown/devices.py` (`ensure_fresh_sim`) |
| Platform lifecycle | `src/splashdown/device_ios.py`; `src/splashdown/device_android.py` |
| Finite tool deadlines | `src/splashdown/device_tools.py` (30-second discovery, 120-second mutation) |
| Latest-OS lookup driving auto-upgrade | `src/splashdown/device_ios.py` (`_ios_latest_runtime_version`); `src/splashdown/device_android.py` (`_android_latest_image`) |
| `splash run` (reconcile + boot + build + launch) | `src/splashdown/target_commands.py` (`cmd_run`) |
| `splash start` / `stop` / `destroy` | `src/splashdown/target_commands.py` |
| Destroy confirmation gate | `src/splashdown/target_commands.py` (`_confirm`) |
| TYPE / variant inference | `src/splashdown/target_commands.py` (`_infer_dtype`, `_resolve_variant_for_cli`) |
| Fleet refresh (eager auto-upgrade) | `src/splashdown/target_commands.py` (`cmd_target_refresh`) |
| Prune foreign (non-managed) sims/AVDs | `src/splashdown/target_commands.py` (`cmd_target_prune`) |
| `target add` / `remove` (local variants) | Catalog edits in `src/splashdown/targets.py`; orchestration in `src/splashdown/target_commands.py` |
| Framework launcher selection | `src/splashdown/launching.py` (`detect_framework`, `device_run`) |
| iOS-native / Android-native launch | `src/splashdown/runners.py:265` / `:364` |
| Physical-device discovery | Cross-platform policy in `src/splashdown/devices.py`; platform probes in `device_ios.py` and `device_android.py` |
| CLI parsers: run/start/stop/destroy loop | `src/splashdown/cli.py:220` (note: `--yes` only on `destroy`, `:237`) |
| CLI parsers: `target refresh`/`prune`/`add`/`remove` | `src/splashdown/cli.py:243` / `:254` / `:267` / `:293` |

## Configuration

Target variants live under `[targets.<type>.<variant>]` in the committed `splashdown.toml`
(team-shared, version control), the gitignored `splashdown.local.toml` (per-checkout, add-only), or
the machine-wide `~/.config/splashdown/config.toml` (`GlobalConfig`, shared across every project).
Three types: `simulator`, `emulator`, `device`.

All three documents are strict: recipe top-level sections are limited to `project`, `apps`,
`resources`, `targets`, `bootstrap`, and `setup`; local and global configs are limited to `settings` and
`targets`. Unknown target types or fields, malformed variant names, non-string/empty values, and
invalid physical-device platforms are hard errors when the document loads. The same target
validator is used for all three sources and `target add`, so invalid declarations fail before
device lifecycle work or file mutation.

`merged_targets(recipe, local, global_config=None)` (`src/splashdown/recipe.py`) folds the three
scopes together. Recipe-vs-local collisions are a hard error; global variants then fold in on top:
physical `device` variants unconditionally (they create nothing — see below), `simulator`/`emulator`
only for types the project already declares, and a project variant silently wins any name collision
with a global one. `target_source` (`src/splashdown/targets.py`) labels the winner
`recipe (shadows global)` so the shadow is visible in `splash target`. `cmd_target_refresh` loads the
global config once, up front and unguarded, so a malformed global file aborts the whole sweep instead
of making every globally-sourced sim/emulator look undeclared and get reaped. `--global` on
`target add`/`remove` (`global_target_add`/`global_target_remove` in `targets.py`) edits the machine
file instead of the local one.

```toml
# splashdown.toml — the team's supported device matrix
[targets.simulator.default]
model = "iPhone 17"            # iOS device type; defaults to latest iPhone Pro

[targets.simulator.lowest-supported]   # UC10: backward-compat coverage as code
model = "iPhone 12"
ios   = "17.0"                 # pinned → never auto-upgraded

[targets.emulator.default]
device = "pixel_9"            # Android device profile; default pixel_9
# image  = "android-34"       # system image; "latest" (default) auto-upgrades

[targets.device.default]       # physical hardware — discovered, not created
# platform = "ios"            # scope auto-pick: "ios" | "android"
# name     = "My iPhone"      # match by device name (substring)
# id       = "..."            # exact udid / adb serial
# name     = "..."            # (simulator/emulator) override the derived instance name
```

Field meaning by type:

- **simulator**: `model` (iOS device type), `ios` (`"latest"` default → auto-upgrade, or a pinned
  version like `"17.0"`), `name` (instance-name override).
- **emulator**: `device` (AVD device profile), `image` (`"latest"` default, or e.g.
  `"android-34"`), `name` (override).
- **device**: `platform` / `id` / `name` — all optional selectors; with one device connected, no
  config is needed.

Every field above is optional, but supplied values must be non-empty strings. `device.platform`
must be `ios` or `android`. Fields do not cross target types: for example, `model` is valid only
for a simulator and `image` only for an emulator.

App build/launch is configured under `[project.*]`:

```toml
[project.ios]
scheme = "MyApp"               # required for ios-native run; optional for react-native
                               # (-> run-ios --scheme; picks the build env for scheme-driven apps)
# mode = "Debug"               # react-native run-ios --mode (optional)
# configuration = "Debug"      # ios-native only

[project.android]
# module          = "app"      # ios-native/android-native: default "app"
# variant         = "debug"    # android-native: default "debug"
# mode            = "developmentDebug"    # react-native run-android --mode (optional)
# application_id  = "com.example.myapp"   # else queried from Gradle
# launch_activity = ".MainActivity"       # else uses the LAUNCHER intent
```

For `react-native`, `[project.ios] scheme` is **optional** but often necessary: RN CLI otherwise
builds the scheme named after the Xcode project (usually Release/prod). If the scheme selects the
build environment (e.g. a `*Dev` scheme that copies `.env.development`), set it here.

For a detected `ios-native` app, `splash init` queries shared Xcode schemes. It writes the sole
scheme automatically, prompts for an exact choice on a TTY, or accepts `--ios-scheme=NAME`.
Ambiguous non-interactive init fails before writing rather than leaving the required field absent.

CLI surface:

```sh
splash run [type] [variant]            # reconcile + boot + build + launch
splash start | stop | destroy [...]    # destroy prompts [y/N]; --yes skips
splash target                          # list declared variants + live state
splash target refresh [ios|android]    # eager auto-upgrade of stale 'latest' sims (no boot)
splash target prune [ios|android] [--dry-run] [--yes]   # remove non-managed sims/AVDs
splash target add <type> <variant> [--model --ios --device --image --name --id --platform]
splash target remove <type> <variant> [--keep-instance]
```

## Gotchas

- **`refresh` and reconcile never boot.** Recreated sims are left **Shutdown**
  (`src/splashdown/target_commands.py`, `ensure_fresh_sim`). A fleet-wide `target refresh`
  across many checkouts therefore won't trip the OS's max-booted-simulators limit — but it also
  means `refresh` alone does not make an app appear; you still need `splash run`/`start`.
- **Pinned vs `latest` is the whole UC4/UC10 distinction.** `ios = "latest"` (the default) is
  reconciled on every run and by `refresh`; a pinned `ios = "17.0"` is *deliberately* frozen and is
  skipped by auto-upgrade when its declared version is still present. Forgetting to pin a
  backward-compat variant means it silently rides the latest OS.
- **`destroy` now prompts.** Interactive `[y/N]` via `_confirm` (`target_commands.py`); scripts/agents must pass
  `--yes`. `--yes` exists *only* on `destroy` among the four verbs (`src/splashdown/cli.py:237`) and
  on `target prune` (`:262`) — not on `stop`/`start`/`run`.
- **`prune` is machine-wide and aggressive.** It destroys every sim/AVD *not* in splashdown's
  registry — including hand-made sims you care about. Use `--dry-run` first; the kill list is
  printed before the prompt.
- **`target refresh`/`prune` are not `gc`.** `gc` (`cmd_gc`, `target_commands.py`) drops dead-checkout
  instances and rows plus live orphan rows; it does **not** recreate an orphan whose checkout still
  exists — `target refresh` does. The `status --check` footer routes each issue to the right command
  (`_print_check_summary`, `commands.py:452`).
- **Unavailable is not broken state.** Status and target catalog views render an unsupported or
  missing platform as `unavailable`, warn once per capability, and do not increment missing,
  stale, or orphan counters. Fleet GC preserves skipped device rows while still removing portable
  port and key rows, so cleanup can be retried on a capable host.
- **A global variant defeats single-variant auto-pick.** `resolve_variant` auto-picks when a type has
  exactly one variant. Add a same-type global variant and the merged catalog has two, so the same
  command that used to work now needs an explicit variant name or a `default` in the recipe. Adding a
  global `simulator`/`emulator` is therefore not free for projects that already declare that type —
  physical `device` variants, which surface everywhere, are the intended use.
- **`target remove` without `--global` refuses a global-only variant up front.** The dispatcher checks
  recipe + local first and raises "`is a global variant; remove it with … --global`" before any
  teardown (`src/splashdown/target_commands.py`), so a mistyped scope can no longer destroy the instance
  and drop the registry row on its way to failing validation. The `--global` path edits
  `~/.config/splashdown/config.toml` only and tells you to run `splash target refresh` to reap
  instances the removal just made undeclared.
- **`target remove` destroys the instance by default.** Pass `--keep-instance` for a toml-only edit.
  It preflights local ownership before destruction, refuses recipe-declared variants, and leaves
  both the declaration and registry row intact when the lifecycle step raises. A registered
  instance is addressed by its stored identifier, so a later config rename cannot redirect
  teardown to an unowned same-name device. A missing registry row is a safe no-op; the declaration
  is removed without looking up an instance by its derived name. `--keep-instance` also leaves any registry row untouched;
  a later `splash target refresh` treats that now-undeclared row as defunct and destroys the
  retained instance.
- **`splashdown.local.toml` is add-only.** A variant name that collides with a recipe-declared one
  is an error (`target_add`, `src/splashdown/targets.py`); pick a different name.
- **The shared CLI parser does not make flags interchangeable.** `target add` shows every target
  flag, but the selected type determines which ones are legal. Incompatible flags and invalid
  values fail before either local or global config is written.
- **Physical-device verbs differ.** For `type = device`, `stop`/`destroy` are no-op messages and
  `start` just confirms connectivity (`src/splashdown/target_commands.py`); nothing
  is ever written to the registry.
- **ios-native needs a scheme.** Scanner-driven init normally writes it, but a hand-authored
  recipe without `[project.ios] scheme` still errors at run time (`_ios_native_run` in
  `src/splashdown/runners.py:265`). For `react-native` the scheme is optional but
  forwards to `run-ios --scheme` when set; Android resolves `application_id` from Gradle if unset, but
  that costs a Gradle round-trip — set it explicitly to skip it.
- **`expo` forwards no scheme or mode, deliberately.** `_expo_run` (`src/splashdown/runners.py:155`)
  passes only `--device`. `expo run:ios --scheme` names a *URL* scheme, not an Xcode scheme, so
  `[project.ios] scheme` has no correct mapping here — don't "fix" the asymmetry with react-native by
  forwarding it. Use `[project] run` (the custom-run escape hatch) for an Expo app that needs extra
  flags.
- **Some apps need an x86_64 simulator.** A pod that excludes arm64 for the simulator
  (`EXCLUDED_ARCHS[sdk=iphonesimulator*] = arm64`, e.g. Google ML Kit) can only build on an x86_64
  sim — which only iOS ≤ 18.x provides. The default `ios = "latest"` picks the newest (arm64-only)
  runtime and `xcodebuild` fails with an opaque "Unable to find a destination". Pin
  `[targets.simulator.default] ios = "18.5"`; on a failed `react-native` iOS run splash detects the
  exclusion and prints this hint (`_rn_ios_arch_hint`).
- **No `[targets.*]` table means no device — with one deliberate exception.** Sims/emulators come
  into existence only from declared `[targets.<type>.<variant>]` tables, created lazily by
  `splash run`/`start` (via `ensure_fresh_sim`). A checkout whose recipe declares only non-device
  resources (e.g. a port) gets **zero** sim/emulator rows. The exception: a **global** `device`
  variant is available in every project regardless, because it creates nothing (`ensure_physical` just
  matches connected hardware, `device_needs_recreate` returns `False`, no registry row). So bare
  `splash run` in an otherwise-target-less repo *does* resolve a lone global physical device.
- **`splash init` scaffolds target tables only on first generation.** It writes the `[targets.*]`
  tables when it creates `splashdown.toml`, but on re-run it preserves existing comments and valid
  tables without backfilling tables the scaffold gained later. A checkout generated before the
  react-native scaffold added its Android `[targets.emulator.default]` will lack it until you edit
  the TOML or run `splash target add`.
- **The path-hash suffix is a one-time identity migration.** Existing healthy simulators do not
  rename themselves because their registry rows store the UDID, not the display name. For each
  default-named target created by an older Splashdown, run `splash destroy TYPE VARIANT --yes`
  once and then `splash start TYPE VARIANT`. Explicit `name = "..."` overrides are unchanged.

## Why

Mobile tooling (`simctl`, `avdmanager`) is verbose and stateful, and nothing natively ties a
simulator to a checkout — so with multiple worktrees open, builds land on the wrong device and
nobody can tell which sim is which. Deriving the instance name from the checkout path makes
isolation automatic and zero-bookkeeping, which matters doubly for the parallel-agent persona where
no human is watching any single checkout. Pinning vs `latest` turns "which OSes do we support?"
into committed configuration instead of one engineer's memory (UC10), and reconcile-on-run plus
`target refresh` absorb the recurring pain of Xcode/SDK bumps without manual `simctl delete` surgery
(UC4). Refresh deliberately never boots so a fleet-wide fix can't exhaust the host's
booted-simulator budget. Physical devices stay discovery-only because owning hardware lifecycle is
both unsafe and unnecessary — the connected device's native id is all the launcher needs.
