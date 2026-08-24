---
title: Getting started with mobile
description: Set up per-checkout iOS simulators, Android emulators, and physical test devices.
---

# Getting started with mobile

For a mobile app, splashdown gives each checkout its own simulator and emulator instances, so worktrees never fight over one device, and it can boot, build, and launch in a single command. This page assumes you have read [Getting started](getting-started.md) for the basics of `splash init` and the env loader.

## Contents

- [Prerequisites](#prerequisites)
- [Initialize](#initialize)
- [Run on a simulator or emulator](#run-on-a-simulator-or-emulator)
- [Choose where a physical device belongs](#choose-where-a-physical-device-belongs)
- [Prepare an iPhone](#prepare-an-iphone)
- [Prepare an Android device](#prepare-an-android-device)
- [Define your personal devices globally](#define-your-personal-devices-globally)
- [Commit a team test-device catalog](#commit-a-team-test-device-catalog)
- [Run on a physical device](#run-on-a-physical-device)
- [Connect to a backend on your computer](#connect-to-a-backend-on-your-computer)
- [Run two worktrees on two physical devices](#run-two-worktrees-on-two-physical-devices)
- [Troubleshooting physical devices](#troubleshooting-physical-devices)
- [Next steps](#next-steps)

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

Each checkout gets its own instance, named `<parent>/<cwd>/<variant>-<path-hash>`, so a worktree or identically named clone under another root never collides with the main checkout. Add a worktree and `splash run` there creates and uses a separate sim.

When a newer iOS or Android system image lands, recreate the `latest` devices in place:

```sh
splash target refresh      # destroy and recreate stale 'latest' sims
splash target prune ios    # delete sims splashdown did not create (the Xcode template pile)
```

The full lifecycle is in [Running and managing devices](devices.md).

## Choose where a physical device belongs

The configuration layer expresses who owns the target:

| Device use | Configuration | Committed? | Availability |
| --- | --- | --- | --- |
| Your regular phones, used across projects | `~/.config/splashdown/config.toml` | No | Every project on this machine |
| Shared team test hardware for this project | `splashdown.toml` | Yes | Every checkout of this project |
| A one-off device for one checkout | `splashdown.local.toml` | No | That checkout only |

A physical-device target is only a selector. Splashdown does not pair, provision, rename, or reset the hardware. A teammate without the declared phone sees it as `absent`; the target does not block normal provisioning.

Use distinct variant names such as `personal-iphone` and `team-iphone13`. A recipe or local variant shadows a global variant with the same name, and `splash target` displays the winning source.

## Prepare an iPhone

1. Connect the unlocked iPhone by cable and accept **Trust This Computer**.
2. Enable **Settings → Privacy & Security → Developer Mode**. Restart and confirm when iOS asks.
3. Open the project in Xcode once. Select the development team under **Signing & Capabilities**; Splashdown does not manage Apple accounts or code signing.
4. Confirm Xcode can see the paired phone:

    ```sh
    xcrun devicectl list devices
    ```

For wireless use, keep the Mac and iPhone on the same network. In [Xcode's Device Hub](https://developer.apple.com/documentation/xcode/managing-your-simulated-and-physical-devices-in-device-hub), choose **Add Device (+) → Pair Nearby Device** and follow the prompts; a cable-trusted phone may already appear. A paired device may report a disconnected tunnel while idle, because Xcode establishes it lazily when the app launches. Keep the phone unlocked during installation.

## Prepare an Android device

1. Open **Settings → About phone** (sometimes **Software information**) and tap **Build number** seven times.
2. Open **Developer options**, enable **USB debugging**, connect the unlocked phone by cable, and accept the computer's RSA key.
3. Confirm `adb` reports state `device`, not `unauthorized` or `offline`:

    ```sh
    adb devices -l
    ```

For wireless use, keep the computer and phone on the same trusted Wi-Fi network. Follow [Android's wireless-debugging setup](https://developer.android.com/tools/adb): open **Developer options → Wireless debugging → Pair device with pairing code**, then use the pairing address and six-digit code:

```sh
adb pair 192.0.2.10:37123
adb connect 192.0.2.10:42137   # use the separate IP address & Port shown on the main page
adb devices -l
```

The connection port can change whenever Wireless debugging is toggled. Do not save an IP address and port in Splashdown config. Match Android hardware by the `model:` value from `adb devices -l`, or by its stable USB serial when cable use is expected.

The Developer options **Stay awake** toggle only prevents sleep while charging. For an unplugged wireless device, temporarily increase the normal display timeout when a long installation needs the screen to remain awake.

## Define your personal devices globally

If you test on real mobile devices (phones or tablets), declare them once in the machine-wide config so every project can use them without re-adding. This is the recommended setup for anyone with a regular set of test devices.

The machine-wide config lives at `~/.config/splashdown/config.toml` (it honors `$XDG_CONFIG_HOME`). Add a `device` target per device. The easiest way is the CLI, which writes to that file when you pass `--global`:

```sh
# an iPhone, matched by name
splash target add device iphone17 --platform=ios --name="My iPhone" --global

# an Android device, matched by the model reported by adb
splash target add device pixel --platform=android --name=Pixel_9a --global
```

That produces:

```toml
# ~/.config/splashdown/config.toml
[targets.device.iphone17]
platform = "ios"
name     = "My iPhone"

[targets.device.pixel]
platform = "android"
name     = "Pixel_9a"
```

Matching fields (all optional):

| Field | Meaning |
| --- | --- |
| `platform` | `ios` or `android`, scopes auto-pick to one platform |
| `name` | case-insensitive substring of the Xcode name or Android `model:` value |
| `id` | exact udid (iOS) or adb serial (Android) |

To look up a connected device's name or id, run `xcrun devicectl list devices` for iOS, or `adb devices -l` for Android. Android Wi-Fi serials may be a changing `IP:port`; prefer `name` for wireless targets when only one connected device matches that model. With a single device connected you can skip these fields entirely and rely on auto-pick.

!!! note "How global devices surface"
    Physical `device` variants from the global config are available in **every** project, because they match connected hardware and create nothing. Global `simulator` and `emulator` variants only appear in projects that already declare that target type. A project's own recipe or local variant of the same name always wins, and `splash target` shows the source (`global`, or `recipe (shadows global)`) so you can see what is in effect.

List and remove global devices:

```sh
splash target                                 # global rows show source "global"
splash target remove device iphone17 --global
```

## Commit a team test-device catalog

Put project-owned test phones in the committed `splashdown.toml`. Every teammate gets the same variant names and commands, while their personal global catalog stays private:

```toml
# splashdown.toml
[targets.device.team-iphone13]
platform = "ios"
name     = "QA iPhone 13"

[targets.device.team-pixel]
platform = "android"
name     = "Pixel_9a"
```

Use the exact `id` instead when several connected devices could match the same name. An iPhone UDID and an Android USB serial are stable selectors for dedicated team hardware, but they are persistent hardware identifiers: commit them only to a private repository where the team has agreed to publish them. In a public repository, prefer a recognizable device `name` and keep any exact ids in local or global config. Do not commit an Android wireless `IP:port`, because it changes when the wireless-debugging service restarts.

The recipe records the supported hardware catalog, not the pairing state. Each developer still trusts or pairs the physical device on their machine. See [The recipe](recipe.md) for the complete committed target schema and [Per-checkout overrides](overrides.md) for local and global precedence.

## Run on a physical device

```sh
splash run device          # auto-picks the one connected device
splash run iphone17        # exact unique variant; type inferred
splash run device team-pixel
```

In a project that also declares a simulator, bare `splash run` still targets the simulator. Name the type (`splash run device`) to launch on hardware.

An exact variant name that exists under only one target type also infers that type, so a global `device.iphone17` supports `splash run iphone17` even in a project with a simulator. If the same variant name exists under more than one type, include the type to resolve the ambiguity. Canonical type names and enabled project type prefixes take precedence in the first slot, so use `splash run device sim` when a device variant itself is named `sim`.

- iOS: connect and unlock the iPhone, and trust the computer. Wifi-paired devices work too, the tunnel is established at launch.
- Android: enable USB debugging and accept the authorization prompt.

Splashdown does not own physical hardware, so it discovers what is connected and hands the native id to the framework launcher. `stop` and `destroy` are no-ops for a physical device, and it is never written to the registry.

For the built-in React Native Android launcher, Splashdown also sets `ANDROID_SERIAL` to the selected device. This scopes Gradle install tasks and nested `adb` calls when several Android phones are connected.

## Connect to a backend on your computer

On physical hardware, `localhost` and `127.0.0.1` refer to the phone, not the development computer. Splashdown selects the device and coordinates development ports; it does not rewrite the app's API URL.

For an iPhone or a wireless Android device on a trusted private network, bind the backend to the computer's LAN interface (or `0.0.0.0` only when the server requires it), allow only the development port from the local subnet through the host firewall, and configure the development build with the computer's LAN address, for example `http://192.168.1.20:9082`. Do not expose a development backend on public or untrusted Wi-Fi: it may contain unauthenticated operations or sensitive test data. The phone and computer must be able to reach each other on the local network.

For an Android device connected through adb, port reversal can keep a localhost URL working:

```sh
adb -s SERIAL reverse tcp:9082 tcp:9082
```

The reversal belongs to that adb connection and may need to be repeated after reconnecting the phone. Plain HTTP also requires the app's development configuration to permit cleartext traffic: App Transport Security on iOS and the network-security policy on Android can otherwise block a reachable backend. Keep production policy strict and scope any exception to development builds.

## Run two worktrees on two physical devices

Global physical-device variants are available in every worktree, while coordinated resources such as `RCT_METRO_PORT` remain checkout-specific. Create the worktrees normally, install the project dependencies in each one, and launch a different target from each terminal:

```sh
# terminal 1
cd ../myapp.pixel
splash run device pixel

# terminal 2
cd ../myapp.xiaomi
splash run device xiaomi
```

The post-checkout hook provisions both worktrees when they are created, so the two React Native launches receive distinct Metro ports automatically. Run `splash status` in each worktree to see its assigned values.

For Flutter, each `splash run` stays attached to its own `flutter run` process and receives an independent ephemeral VM-service port. A brief `Waiting for another flutter command to release the startup lock...` message is normal when two commands start together: Flutter serializes startup, then the builds and debug sessions continue independently. Send `r` or `R` in a worktree's terminal to reload only its device, or `d` to detach while leaving that app running.

Run `splash doctor --fix` before the first launch and commit its safe React Native wiring changes. In particular, iOS compiles `RCT_METRO_PORT` into the app, so each worktree needs its own build after provisioning. A successful built-in React Native launch now warns when nothing is listening on the configured Metro port; start Metro and relaunch the app before expecting source changes or Fast Refresh.

Each worktree still needs the framework's normal dependency setup. Install dependencies separately, or use [trusted worktree bootstrap](bootstrap.md) for a shared setup command. Avoid symlinking `node_modules` from another React Native checkout: Metro resolves real paths and can retain paths from the source checkout, causing bundle-resolution or SHA-1 errors. Create the worktree at its final path before running `pod install`; CocoaPods and Hermes-generated files can embed absolute checkout paths. If a prepared worktree is moved and a build still names its old location, remove the generated `ios/Pods` directory and run `pod install` again without replacing `Podfile.lock`.

Two worktrees that share one app bundle id overwrite each other when installed on the same phone. Use one physical device per simultaneously running worktree, or give the builds distinct development bundle ids.

## Troubleshooting physical devices

### iPhone is paired but not connected

- Unlock the phone and confirm it still appears in `xcrun devicectl list devices`.
- A paired Wi-Fi device can show a disconnected tunnel while idle. Use `splash run device <variant>`; the app launch establishes it. `splash start` only checks that physical hardware is discoverable.
- Signing failures are project/Xcode configuration. Select a valid development team under **Signing & Capabilities**.

### Android Wireless debugging turns itself off

Some Android builds disable wireless debugging whenever Wi-Fi reconnects or roams, even when the SSID and IP address appear unchanged. This is common on mesh networks or with adaptive Wi-Fi features.

- Keep the phone near one access point or use an SSID tied to one access point while testing.
- Temporarily disable vendor options that switch to a better Wi-Fi network or mobile data.
- Toggle Wireless debugging on again, then inspect the fresh endpoint with `adb mdns services` or the phone's **IP address & Port** field.
- Pairing is normally persistent; a changing connection port does not require a new pairing code.

### A large Android debug build fails over Wi-Fi

Universal debug APKs can contain native libraries for every ARM and emulator architecture. Install the first large build over USB, or configure the app to produce an architecture-specific development APK. `splash start device <variant>` only verifies target connectivity; `splash run device <variant>` performs the framework's normal build and installation.

### Android rejects the APK as a version downgrade

`INSTALL_FAILED_VERSION_DOWNGRADE` means the connected phone already has the same package id with a higher `versionCode`. Uninstall that copy if its app data is disposable, or build with a version code at least as high as the installed one. Splashdown passes through the framework install result; it does not remove apps or their data.

### Metro reports an FSEvents or Watchman failure

If a new worktree's Metro process exits with `FSEventStreamStart failed`, first try registering that exact checkout and retry the launch:

```sh
watchman watch-project "$PWD"
splash run device <variant>
```

If `watchman watch-project` reports the same error, the Watchman/macOS event stream is unhealthy rather than merely missing the checkout. Disabling Watchman may allow an initial bundle, but it does not restore Fast Refresh when FSEvents itself is not delivering changes. Restarting the Watchman server affects every watched project, so do that only when disrupting their active sessions is acceptable; a macOS restart is the clean fallback.

Do not use `watchman watch-del-all` for this; it removes unrelated projects already watched on the machine without repairing FSEvents.

## Next steps

- [Running and managing devices](devices.md): the full run, start, stop lifecycle, auto-upgrade, and framework detection.
- [Per-checkout overrides](overrides.md): one-off sim variants for a single checkout, and more on machine-wide devices.
- [Framework wiring](framework-wiring.md): patch configs that hardcode the Metro or dev port.
