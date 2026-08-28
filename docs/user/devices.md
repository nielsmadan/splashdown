---
title: Running and managing devices
description: Run, boot, refresh, and manage per-checkout simulators, emulators, and physical devices.
---

# Running and managing devices

```sh
splash run     [type] [variant]    # reconcile + start + build + launch
splash start   [type] [variant]    # reconcile + start (no build/launch)
splash stop    [type] [variant]    # shut down the device (preserves it)
splash destroy [type] [variant]    # delete the device + its registry entry
```

Both `type` and `variant` are optional. `type` is inferred when exactly one project target type is declared. An explicit, exact variant name also infers its type when that name exists under only one type in the merged project, local, and global catalog, so `splash run iphone17` can select a global physical device from a simulator-enabled project. A name shared by multiple types requires an explicit `simulator`, `emulator`, or `device`. Canonical type names and enabled project type prefixes win over same-named variants in the first slot. Name the type explicitly to select such a variant. Without an explicit variant, it defaults to `default`, then to the only declared variant if there's just one, else errors with the list of choices.

## Platform support

| Target | macOS | Linux |
| --- | --- | --- |
| iOS simulator/device | Xcode required | Unsupported. Explicit commands return an actionable error |
| Android emulator/device | Android SDK required | Android SDK required |
| Ports, environment, and config | Supported | Supported |

An explicit unsupported target returns exit 1 with the missing host or tool requirement and no
traceback. Commands that inspect or reconcile both platforms, including `target refresh`, `target
prune`, `gc`, and status inspection, warn once, skip the unavailable platform, and continue
supported work. Status labels skipped targets `unavailable`. It does not report them as missing or
stale.

**Prefix matching** (on by default): you can abbreviate both `type` and `variant` to any unambiguous prefix: `splash run sim` resolves the simulator type, `splash run sim low` the `lowest-supported` variant. A prefix that matches more than one variant errors with the candidates. A type prefix wins over an identically-prefixed variant name. Toggle it off in [Settings](settings.md).

```sh
splash run                            # one type, one variant, no args needed
splash run simulator                  # picks `default`
splash run sim                        # prefix → simulator
splash run sim low                    # prefix → simulator / lowest-supported
splash run simulator lowest-supported
splash run iphone17                   # exact unique variant → its target type

splash target                         # show every declared variant + its live sim state
splash target claims                  # inspect machine-wide physical claims
splash stop    simulator              # shut down the running sim
splash destroy simulator small-screen # delete that variant's sim
splash target remove simulator repro-bug      # strip a local variant (and destroy its sim)
splash target remove simulator repro-bug --keep-instance   # toml-only edit
```

`target remove` only accepts variants declared in `splashdown.local.toml`. Recipe-owned or missing
variants and malformed config are rejected before any simulator or emulator is touched. If the
lifecycle step reports an error, the local declaration and registry row remain intact. If the
configured name changed after provisioning, removal still deletes the instance recorded in the
registry. An instance that is already absent does not block config cleanup.

Use `--keep-instance` when the SDK is unavailable or you intentionally want a config-only removal.
It removes the local declaration but leaves any instance and registry row untouched. If a row
exists, a later `splash target refresh` sees it as undeclared and destroys the retained instance.
Global removal is configuration-only and tells you to run refresh to reap instances that become
undeclared. `--global --keep-instance` is rejected because those two flags request the same
config-only behavior. Physical `device` targets have no managed instance, so their removal also
rejects `--keep-instance` instead of accepting an ineffective flag.

Framework auto-detected for `run`:

- `pubspec.yaml` → `flutter run -d <id>`
- `package.json` with `react-native` → `npx react-native run-ios --udid` / `run-android --deviceId`. Optional `[project.ios] scheme`/`mode` and `[project.android] mode` forward `--scheme`/`--mode` to select the Xcode scheme / build variant (e.g. a `*Dev` scheme that copies `.env.development`).
- `package.json` with `expo` + `app.json` → `npx expo run:ios --device` / `run:android --device`
- `*.xcodeproj` / `*.xcworkspace` at root (no JS/Flutter signals) → `xcodebuild build` → `xcrun simctl install`/`launch` (or `xcrun devicectl` for a physical device). `splash init` records the sole shared scheme automatically, prompts for a choice when interactive, or accepts `--ios-scheme=NAME`.
- `build.gradle*` + `settings.gradle*` at root (no JS/Flutter signals) → `./gradlew :module:installVariant` → `adb shell am start`. Conventional modules such as `include(":app")` are detected automatically. After installation, splashdown reads the selected variant's application ID from AGP's build metadata. `[project.android] application_id` is only needed for non-standard builds. `module`, `variant`, and `launch_activity` are also configurable there.
- Override via `[project] framework = "..."`

`splash run` stays attached to the framework or custom launcher and leaves its standard streams connected, so interactive controls continue to work. Splashdown does not redact launcher output. Disable third-party SDK logging that prints credentials or tokens before running in shared terminals, recorded sessions, CI logs, or agent transcripts.

## Custom run command

Set `[project] run` when the built-in launcher isn't what you want: a different package manager (`yarn`/`pnpm` instead of `npx`), a monorepo subdir, `expo start --dev-client`, or any custom wrapper. It **replaces** the framework launcher (and skips framework detection), so it also works on a project splashdown doesn't recognize. splashdown still reconciles and boots the declared `[targets.*]` first. Your command is the launch step, run in a shell at the repo root.

`run` is either a single string (shared) or a `[project.run]` table with `ios`/`android` keys when they differ:

```toml
# Flutter, one command for both platforms:
[project]
run = "flutter run -d {device_id}"

# React Native with yarn, per platform:
[project.run]
ios     = "yarn react-native run-ios --udid {device_id}"
android = "yarn react-native run-android --deviceId {device_id}"
```

A `[project.run]` table may set only one platform, the other falls back to auto-detection. So `[project.run] ios = "..."` alone customizes iOS and leaves `splash run android` on the built-in launcher.

Placeholders substituted before the command runs (device values are shell-quoted, unknown `{...}` are left untouched):

| Placeholder | Value |
| --- | --- |
| `{device_id}` | the booted device's UDID (iOS) or adb serial (Android) |
| `{device_name}` | the sim / AVD / device name |
| `{platform}` | `ios` or `android` |

The command runs via a shell, so pipes, `&&`, `$ENV`, and `cd` work. In a monorepo, point it at the app: `[project.run] ios = "yarn --cwd apps/mobile react-native run-ios --udid {device_id}"`.

## Auto-upgrade: no more manual `mksim`/`simctl delete` after Xcode updates

Variants with `ios = "latest"` (the default) reconcile on every `splash run`. If the registered sim's iOS is older than the current latest, splashdown destroys the old sim and creates a new one in place. A healthy pinned variant such as `ios = "17.0"` is not upgraded. If its registered instance is missing, refresh recreates it at that declared runtime.

Some apps *require* a pinned older runtime: a pod that excludes arm64 for the simulator (e.g. Google ML Kit) only builds on an x86_64 sim, which only iOS ≤ 18.x provides. Against the default (newest, arm64-only) sim, `xcodebuild` fails with an opaque "Unable to find a destination". Pin `ios = "18.5"`. On a failed `react-native` iOS run, splash detects the exclusion and prints this hint.

```sh
splash gc                           # drop dead-checkout entries (ports, vars, sims)
splash target refresh               # reconcile registered devices, including undeclared/dead rows
splash target prune [ios|android]   # destroys every sim/AVD splashdown did NOT create
                                    # (the Xcode default-template pile, hand-made sims, etc.)
```

`target refresh` checks all registered checkouts, not only the current one. It removes undeclared
and dead-checkout instances without confirmation. Use `status all --check` first when you want a
fleet preview.

## iOS sim management

Backed by `xcrun simctl`. Default device type: latest iPhone Pro. Default runtime: latest installed. Sim name defaults to `<parent-dir>/<checkout-name>/<variant>-<path-hash>` so different worktrees, clones, and variants never collide. The hash is the first eight SHA-256 characters of the resolved checkout path. Override per-variant with `model = "..."` and `ios = "18.5"` in the recipe (or in `splashdown.local.toml` for an add-only variant).

## Android emulator management

Backed by `avdmanager` / `sdkmanager` / `emulator` / `adb` from `$ANDROID_HOME`. Default device profile: `pixel_9`. Default system image: latest installed, falling back to a known-good Android 34 image. AVD is created if missing, then booted detached. `splash` polls `adb` for the serial to appear.

For `splash target add emulator`, `--device` selects this Android hardware profile. It does not
select a connected physical device. Use target type `device` with `--id`, `--name`, or `--platform`
for physical hardware.

After upgrading from a version that generated names without the path hash, recreate each
default-named sim or emulator once with `splash destroy TYPE VARIANT --yes` followed by
`splash start TYPE VARIANT`. Existing healthy iOS rows keep using their recorded UDID until this
explicit recreation. Targets with an explicit `name` are unchanged.

## Physical devices

`splash run pixel` resolves a configured connected phone, claims it for this checkout, then builds
and launches. Discovery uses `xcrun devicectl` for iOS with Xcode 15 or later and `adb devices` for
Android. The native UDID or serial is passed straight to the framework launcher.

Declare physical targets in the committed recipe, the per-checkout local file, or the global
config. Targets from all three sources participate in claims. Undeclared phones discovered by
Xcode or ADB are never claimed or selected, even when only one phone is connected.

```toml
[targets.device.pixel]
platform = "android"
id = "PXL1234"
```

The selectors are optional. Use `platform` when any connected phone on that platform is suitable,
`name` for a case-insensitive name match, or `id` for one exact device. If a selector matches more
than one phone, narrow the target before running it.

```sh
splash target                         # connection, claim, and concise owner state
splash target claims                  # machine-wide ownership, no device discovery
splash target claims --format json    # canonical owner paths for scripts

splash run pixel                      # claim if free, then build and launch
splash target claim pixel             # explicitly claim one configured target
device=$(splash target claim --available android)
splash target claim --available ios
splash target claim --available any
```

`claim --available` checks configured targets in recipe, local, then global order and prints only
the selected variant to stdout in text mode. Diagnostics use stderr. Add `--format json` to receive
`target`, `source`, `platform`, `hardware_id`, `owner`, `claimed_at`, and `status`.

A run against a free connected target claims it before any framework build, installation, or
launch. A target owned by this checkout is reused. A target owned by another live checkout fails
with its owner and claim time, and a disconnected configured target also fails before build. The
claim remains after the launcher exits or fails, so the owner can fix the problem and retry.

Claims belong to the checkout rather than to one process. Inspect and release them explicitly:

```sh
splash target release pixel           # release this checkout's target
splash target release --all           # release every claim owned by this checkout
splash target claim pixel --force     # transfer a live owner's claim to this checkout
splash target release pixel --force   # release a live owner's claim without taking it
```

Forced transfer and forced release warn the displaced checkout on its next ordinary
checkout-scoped `splash` command. The warning is consumed once. Completion, help, version output,
and the hidden post-checkout command do not consume it.

Because Splashdown does not own the hardware, `start` only confirms connection and
`stop` or `destroy` does not control the phone. `splash stop device pixel` does not release the
claim. Claims are also removed by `splash deinit`, by `splash gc` after a checkout disappears, and
when another checkout explicitly forces a transfer or release.

### Claim one device for a new linked worktree

A project can opt into one best-effort allocation after a genuine linked worktree is created:

```toml
[project.worktree]
claim_device = "android" # ios | android | any
```

The hook provisions resources first, then runs a trusted bootstrap when configured, then attempts
the claim. It does not run for the primary checkout or ordinary branch and file checkouts. Device
discovery has a five-second total budget. No free connected match, missing platform tooling, and a
discovery timeout are non-fatal, so the completed worktree creation still succeeds. Retry the
command printed by the hook, for example:

```sh
splash target claim --available android
```

Declare test phones once with `splash target add device my-iphone --platform ios --name "..."
--global` to make them available in every project. See
[machine-wide test devices](overrides.md#machine-wide-test-devices).
