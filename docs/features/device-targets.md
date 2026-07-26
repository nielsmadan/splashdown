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

## How it works (current state)

**Instance naming and isolation.** Every sim/emulator instance is named
`<parent-dir>/<checkout-name>/<variant>` (`src/splashdown/devices.py:36`,
`_default_sim_name`). The path component keeps sibling worktrees/clones apart; the variant suffix
lets one checkout host several configs (`default`, `lowest-supported`, …). A per-variant `name =
"..."` (literal or template) overrides the default; for emulators the name is sanitized to
avdmanager's `[A-Za-z0-9._-]` (`_resolve_device_name`, `src/splashdown/devices.py:53`;
`_sanitize_avd_name:46`).

**Reconcile (the heart of UC2 and UC4).** `ensure_fresh_sim` (`src/splashdown/devices.py:539`)
diffs the registry's recorded instance against the variant spec. The instance is *stale* when its
registry row is missing, its underlying sim/AVD no longer exists, or its OS image or model has
drifted from the spec. For `ios = "latest"` the target OS is resolved live
(`_ios_latest_runtime_version`, `src/splashdown/devices.py:108`); for a pinned `ios = "17.0"` the
target *is* `17.0`, so a stale check never fires on OS — that is what makes pinned variants
permanent (UC10). When stale, the old sim is destroyed and a fresh one created in place, and the
new instance is recorded in the machine-wide registry (`registry.set_device`). Crucially,
reconcile leaves the new sim **Shutdown** — it never boots anything, so the OS-imposed
"too many booted simulators" limit never applies during a fleet-wide refresh.

**Run vs start vs stop vs destroy.** `cmd_run` (`src/splashdown/commands.py:1044`) calls
`ensure_fresh_sim`, then boots (`ios_boot` / `android_boot`), then builds + launches via
`device_run`. `cmd_start` (`:1064`) reconciles + boots but skips the build/launch. `cmd_stop`
(`:1084`) shuts the instance down but preserves it. `cmd_destroy` (`:1109`) deletes the instance
and its registry row — and now gates the deletion behind a `[y/N]` prompt (`_confirm`,
`src/splashdown/commands.py:1101`), bypassable with `--yes`. For `type = device`, stop/destroy are
no-ops with an explanatory message because splashdown owns no hardware.

**Type and variant inference.** When the user omits `TYPE`, `_infer_dtype`
(`src/splashdown/commands.py:1130`) resolves it to the single declared target type, or errors with
the list when there are zero or several. `_resolve_variant_for_cli` (`:1148`) loads recipe + local,
merges them, and picks the variant (`resolve_variant`): an explicit arg, else `default`, else the
sole declared variant. The CLI parser additionally reinterprets a lone non-type token as the
variant (`splash run lowest-supported`) in `cli.py` (validated post-parse by
`_normalize_device_args` (`src/splashdown/cli.py:284`)).

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

**Framework launcher (UC2 build/launch).** `detect_framework` (`src/splashdown/devices.py:758`)
picks the launcher from filesystem signals, overridable via `[project] framework`. `device_run`
(`src/splashdown/devices.py:774`) dispatches to that profile's `run`. Launchers:
flutter (`flutter run -d <id>`), react-native, expo, ios-native (`xcodebuild build` then `xcrun
simctl install`/`launch`, or `xcrun devicectl` for a physical device — `_ios_native_run`,
`src/splashdown/profiles.py:111`), and android-native (`./gradlew :module:installVariant` then
`adb shell am start` — `_android_native_run`, `src/splashdown/profiles.py:181`).

**Auto-upgrade after an Xcode/SDK bump (UC4).** Two paths:
1. *Lazy*, on `splash run`: `ensure_fresh_sim` recreates the one sim being run if its `latest` OS
   is now behind. This is the zero-effort path the persona hits daily.
2. *Eager*, fleet-wide: `splash target refresh` → `cmd_target_refresh`
   (`src/splashdown/commands.py:899`) walks every registry device row, recreates each that is stale
   or missing-but-declared (including pinned variants whose sim was hand-deleted), leaves fresh ones
   alone, and drops rows for defunct checkouts or undeclared variants (destroying their sim). Like
   reconcile, it leaves recreated sims **Shutdown**. The recreate decision is taken *before* the
   call (`_device_needs_recreate`, `:874`) because `ensure_fresh_sim` is a no-op for fresh devices
   and the AVD name is stable across recreation, so the return value can't reveal what happened.

**Pruning the template pile (UC4 cleanup).** `splash target prune` → `cmd_target_prune`
(`src/splashdown/commands.py:985`) destroys every sim/AVD on the machine that splashdown did *not*
create — the Xcode default-template pile, hand-made sims — by diffing live sims/AVDs against
`registry.managed_udids()` (`_discover_foreign_ios:951`, `_discover_foreign_avds:971`). It prints
the kill list, honors `--dry-run` (preview only) and `--yes` (skip the `_confirm` prompt), and is
scoped by an optional `ios|android|all` platform argument.

**Committing the lowest-supported sim (UC10).** A pinned variant in the committed recipe (e.g.
`[targets.simulator.lowest-supported]` with `model = "iPhone 12"`, `ios = "17.0"`) makes the
backward-compat device part of the repo. `merged_targets` unions the recipe catalog with add-only
local variants so any checkout — or any agent — resolves the same matrix. Because the OS is pinned,
neither `ensure_fresh_sim` nor `target refresh` will ever upgrade it; `cmd_target_gc`'s `--all`
prune of stale `latest` sims explicitly skips anything not declared `latest`
(`src/splashdown/commands.py:826`).

**Physical devices are discovered, not owned.** A `device` target resolves to a *connected* phone
(`ensure_physical`, `src/splashdown/devices.py:493`; `_physical_match:480` filters by
`platform`/`id`/`name`). `cmd_run` skips booting for `info["physical"]` and goes straight to the
launcher (`src/splashdown/commands.py:1056`). Physical devices are never written to the registry;
`status`/`target` show `connected` / `absent` / `ambiguous` (`physical_status:526`).

**Declaring variants programmatically.** `splash target add` writes an add-only
`[targets.<type>.<variant>]` table into the gitignored local file (`target_add`,
`src/splashdown/devices.py:756`); `splash target remove` first verifies that the variant belongs to
the local file and computes the edited TOML, then destroys the instance, writes the declaration
change, and removes its registry row unless `--keep-instance`. When a registry row exists,
deletion uses its actual simulator UDID or AVD name rather than a newly resolved config name.
A recipe-owned or missing variant, or malformed recipe/local file, is rejected before any device
operation. If the lifecycle step raises, the local declaration and registry row remain intact.
Adding a variant that already exists in the recipe is an error.

## Key entry points

| Concern | Location |
|---|---|
| Instance name `<parent>/<cwd>/<variant>` | `src/splashdown/devices.py:36` (`_default_sim_name`); override resolution `:53` |
| Reconcile (create / recreate-on-drift / pin) | `src/splashdown/devices.py:539` (`ensure_fresh_sim`) |
| Latest-iOS lookup driving auto-upgrade | `src/splashdown/devices.py:108` (`_ios_latest_runtime_version`); Android `:265` (`_android_latest_image`) |
| `splash run` (reconcile + boot + build + launch) | `src/splashdown/commands.py:1044` (`cmd_run`) |
| `splash start` / `stop` / `destroy` | `src/splashdown/commands.py:1064` / `:1084` / `:1109` |
| Destroy confirmation gate | `src/splashdown/commands.py:1101` (`_confirm`), used at `:1120` |
| TYPE / variant inference | `src/splashdown/commands.py:1130` (`_infer_dtype`), `:1148` (`_resolve_variant_for_cli`) |
| Fleet refresh (eager auto-upgrade) | `src/splashdown/commands.py:899` (`cmd_target_refresh`); recreate decision `:874` |
| Prune foreign (non-managed) sims/AVDs | `src/splashdown/commands.py:985` (`cmd_target_prune`) |
| `target add` / `remove` (local variants) | `src/splashdown/devices.py:756` / `:800`; preflight `:785`; dispatch `src/splashdown/commands.py:1377` |
| Framework launcher selection | `src/splashdown/devices.py:758` (`detect_framework`), `:774` (`device_run`) |
| iOS-native / Android-native launch | `src/splashdown/profiles.py:111` / `:181` |
| Physical-device discovery | `src/splashdown/devices.py:493` (`ensure_physical`), `:480` (`_physical_match`) |
| CLI parsers: run/start/stop/destroy loop | `src/splashdown/cli.py:197` (note: `--yes` only on `destroy`, `:214`) |
| CLI parsers: `target refresh`/`prune`/`add`/`remove` | `src/splashdown/cli.py:220` / `:231` / `:244` / `:263` |

## Configuration

Target variants live under `[targets.<type>.<variant>]` in the committed `splashdown.toml`
(team-shared, version control) or the gitignored `splashdown.local.toml` (per-checkout, add-only).
Three types: `simulator`, `emulator`, `device`.

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
  (`src/splashdown/commands.py:899` docstring, `ensure_fresh_sim`). A fleet-wide `target refresh`
  across many checkouts therefore won't trip the OS's max-booted-simulators limit — but it also
  means `refresh` alone does not make an app appear; you still need `splash run`/`start`.
- **Pinned vs `latest` is the whole UC4/UC10 distinction.** `ios = "latest"` (the default) is
  reconciled on every run and by `refresh`; a pinned `ios = "17.0"` is *deliberately* frozen and is
  skipped by both auto-upgrade and the `gc --all` stale-prune (`:826`). Forgetting to pin a
  backward-compat variant means it silently rides the latest OS.
- **`destroy` now prompts.** Interactive `[y/N]` via `_confirm` (`:1101`); scripts/agents must pass
  `--yes`. `--yes` exists *only* on `destroy` among the four verbs (`src/splashdown/cli.py:214`) and
  on `target prune` (`:239`) — not on `stop`/`start`/`run`.
- **`prune` is machine-wide and aggressive.** It destroys every sim/AVD *not* in splashdown's
  registry — including hand-made sims you care about. Use `--dry-run` first; the kill list is
  printed before the prompt.
- **`target refresh`/`prune` are not `gc`.** `gc` (`cmd_gc`, `:840`) drops *dead-checkout* rows and
  their orphaned sims; it does **not** recreate an orphan whose checkout still exists — `target
  refresh` does. The `status --check` footer routes each issue to the right command
  (`_print_check_summary`, `:570`).
- **`target remove` destroys the instance by default.** Pass `--keep-instance` for a toml-only edit.
  It preflights local ownership before destruction, refuses recipe-declared variants, and leaves
  both the declaration and registry row intact when the lifecycle step raises. A registered
  instance is addressed by its stored identifier, so a later config rename cannot orphan it;
  already-absent instances are accepted. `--keep-instance` also leaves any registry row untouched;
  a later `splash target refresh` treats that now-undeclared row as defunct and destroys the
  retained instance.
- **`splashdown.local.toml` is add-only.** A variant name that collides with a recipe-declared one
  is an error (`target_add`, `src/splashdown/devices.py:756`); pick a different name.
- **Physical-device verbs differ.** For `type = device`, `stop`/`destroy` are no-op messages and
  `start` just confirms connectivity (`src/splashdown/commands.py:1073`, `:1089`, `:1114`); nothing
  is ever written to the registry.
- **ios-native needs a scheme.** Without `[project.ios] scheme`, `run` errors
  (`src/splashdown/profiles.py`, `_ios_native_run`). For `react-native` the scheme is optional but
  forwards to `run-ios --scheme` when set; Android resolves `application_id` from Gradle if unset, but
  that costs a Gradle round-trip — set it explicitly to skip it.
- **Some apps need an x86_64 simulator.** A pod that excludes arm64 for the simulator
  (`EXCLUDED_ARCHS[sdk=iphonesimulator*] = arm64`, e.g. Google ML Kit) can only build on an x86_64
  sim — which only iOS ≤ 18.x provides. The default `ios = "latest"` picks the newest (arm64-only)
  runtime and `xcodebuild` fails with an opaque "Unable to find a destination". Pin
  `[targets.simulator.default] ios = "18.5"`; on a failed `react-native` iOS run splash detects the
  exclusion and prints this hint (`_rn_ios_arch_hint`).
- **No `[targets.*]` table means no device.** Sims/emulators come into existence only from declared
  `[targets.<type>.<variant>]` tables, created lazily by `splash run`/`start` (via
  `ensure_fresh_sim`). A checkout whose recipe declares only non-device resources (e.g. a port) gets
  **zero** device rows and nothing to boot — there is no implicit "default simulator" behind the
  scenes.
- **`splash init` scaffolds target tables only on first generation.** It writes the `[targets.*]`
  tables when it creates `splashdown.toml`, but on re-run it preserves existing comments/keys and
  does **not** backfill tables the scaffold gained later. A checkout generated before the
  react-native scaffold added its Android `[targets.emulator.default]` will lack it until you edit
  the toml or run `splash target add`.

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
