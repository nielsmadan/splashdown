---
title: Getting started with mobile
description: Set up per-checkout iOS simulators, Android emulators, and physical test devices.
---

# Getting started with mobile

For a mobile app, splashdown gives each checkout its own simulator and emulator instances, so worktrees never fight over one device, and it can boot, build, and launch in a single command. This page assumes you have read [Getting started](getting-started.md) for the basics of `splash init` and the env loader.

## Prerequisites

- iOS: Xcode and its command-line tools (`xcrun`, `simctl`). For a physical iPhone, Xcode 15 or newer (`xcrun devicectl`).
- Android: the Android SDK with `$ANDROID_HOME` set, providing `avdmanager`, `sdkmanager`, `emulator`, and `adb`.
- The general prerequisites from [Getting started](getting-started.md).

## Initialize

Run `splash init` at the repo root. Splashdown detects the framework (React Native, Expo, Flutter, or native iOS/Android) and scaffolds a `[targets.*]` catalog in `splashdown.toml` describing the simulators and emulators this project supports:

```toml
[targets.simulator.default]
model = "iPhone 17"        # optional; omit for the latest iPhone Pro

[targets.emulator.default]
device = "pixel_9"         # optional; omit for the default profile
```

React Native and Expo also get a coordinated Metro port (`RCT_METRO_PORT`). Flutter needs no pinned port, because it picks an ephemeral one on each run.

Commit `splashdown.toml`. See [The recipe](recipe.md) for the full target schema, including how to pin a specific iOS version for backward-compatibility coverage.

## Run on a simulator or emulator

One command reconciles the device to its declared spec, boots it, builds, and launches:

```sh
splash run                 # one declared type and variant, no args needed
splash run simulator       # name the type when you declare more than one
splash run sim low         # prefix matching: simulator / lowest-supported
splash target              # list declared variants and which are booted
splash stop simulator      # shut it down, keep it
```

Each checkout gets its own instance, named `<parent>/<cwd>/<variant>`, so a worktree never collides with the main checkout. Add a worktree and `splash run` there creates and uses a separate sim.

When a newer iOS or Android system image lands, recreate the `latest` devices in place:

```sh
splash target refresh      # destroy and recreate stale 'latest' sims
splash target prune ios    # delete sims splashdown did not create (the Xcode template pile)
```

The full lifecycle is in [Running and managing devices](devices.md).

## Recommended: define your physical test devices once, globally

If you test on real mobile devices (phones or tablets), declare them once in the machine-wide config so every project can use them without re-adding. This is the recommended setup for anyone with a regular set of test devices.

The machine-wide config lives at `~/.config/splashdown/config.toml` (it honors `$XDG_CONFIG_HOME`). Add a `device` target per device. The easiest way is the CLI, which writes to that file when you pass `--global`:

```sh
# an iPhone, matched by name
splash target add device iphone --platform=ios --name="Niels's iPhone" --global

# an Android device, matched by its adb serial
splash target add device pixel --platform=android --id=39121FDJH003AB --global
```

That produces:

```toml
# ~/.config/splashdown/config.toml
[targets.device.iphone]
platform = "ios"
name     = "Niels's iPhone"

[targets.device.pixel]
platform = "android"
id       = "39121FDJH003AB"
```

Matching fields (all optional):

| Field | Meaning |
| --- | --- |
| `platform` | `ios` or `android`, scopes auto-pick to one platform |
| `name` | match by device name, as shown by Xcode or `adb` |
| `id` | exact udid (iOS) or adb serial (Android) |

To look up a connected device's name or id, run `xcrun devicectl list devices` for iOS, or `adb devices -l` for Android (use the serial as `id`). With a single device connected you can skip these fields entirely and rely on auto-pick.

!!! note "How global devices surface"
    Physical `device` variants from the global config are available in **every** project, because they match connected hardware and create nothing. Global `simulator` and `emulator` variants only appear in projects that already declare that target type. A project's own recipe or local variant of the same name always wins, and `splash target` shows the source (`global`, or `recipe (shadows global)`) so you can see what is in effect.

List and remove global devices:

```sh
splash target                                 # global rows show source "global"
splash target remove device iphone --global
```

## Run on a physical device

```sh
splash run device          # auto-picks the one connected device
splash run device iphone   # or name a declared variant
```

In a project that also declares a simulator, bare `splash run` still targets the simulator. Name the type (`splash run device`) to launch on hardware.

- iOS: connect and unlock the iPhone, and trust the computer. Wifi-paired devices work too, the tunnel is established at launch.
- Android: enable USB debugging and accept the authorization prompt.

Splashdown does not own physical hardware, so it discovers what is connected and hands the native id to the framework launcher. `stop` and `destroy` are no-ops for a physical device, and it is never written to the registry.

## Next steps

- [Running and managing devices](devices.md): the full run, start, stop lifecycle, auto-upgrade, and framework detection.
- [Per-checkout overrides](overrides.md): one-off sim variants for a single checkout, and more on machine-wide devices.
- [Framework wiring](framework-wiring.md): patch configs that hardcode the Metro or dev port.
