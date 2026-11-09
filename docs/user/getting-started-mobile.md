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
model = "iPhone 17"        # optional, omit for the latest iPhone Pro

[targets.emulator.default]
device = "pixel_9"         # optional, omit for the default profile
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

A physical-device target binds a command label to a hardware ID. Splashdown does not pair, provision, rename, or reset the hardware. A teammate without the declared phone sees it as `absent`. The target does not block normal provisioning.

Use distinct variant names such as `personal-iphone` and `team-iphone13`. A recipe or local variant shadows a global variant with the same name, and `splash target` displays the winning source.

## Prepare an iPhone

Pairing lets your Mac communicate with the phone. Developer Mode permits development apps to run. Complete both before your first launch. The same steps apply to an iPad.

### Pair by USB

1. Install and open full Xcode on the Mac. Choose **Xcode → Open Developer Tool → Device Hub**. In older Xcode versions, open **Window → Devices and Simulators → Devices**.
2. Unlock the phone and connect it with a data-capable USB cable. Accept any accessory connection prompt on the Mac.
3. On the phone, tap **Trust** in **Trust This Computer** and enter the phone's passcode if asked.
4. Select the phone in Device Hub's sidebar. Follow any setup instructions shown for it, clicking **Pair** if that button appears. If it is already paired, there is no extra pairing code to enter.

### Enable Developer Mode

1. On the phone, open **Settings → Privacy & Security → Developer Mode** and turn it on. If the option is missing, initiate pairing with the Mac first.
2. Tap **Restart**. After the phone restarts, unlock it and confirm Developer Mode in the prompt. Enter the phone's passcode if asked. Turning on the switch alone does not complete setup.
3. Return to Xcode and finish any remaining setup. Device Hub shows the phone under **Available** when ready.

See [Apple's Developer Mode instructions](https://developer.apple.com/documentation/xcode/enabling-developer-mode-on-a-device) for the confirmation screens.

### Use Wi-Fi

After USB pairing, put the phone and Mac on the same Wi-Fi network and unplug the cable. The network needs IPv6 enabled. Keep the phone unlocked during the first connection and app installation. A paired device may report a disconnected tunnel while idle, because Xcode establishes it when needed at launch.

For initial pairing **without a cable**, Device Hub requires **iOS or iPadOS 27 or newer**:

1. Connect the phone and Mac to the same Wi-Fi network.
2. In Device Hub, choose **Add Device (+) → Pair Nearby Device** and select the iPhone or iPad device type.
3. Follow the instructions shown for that device type. Allow the Mac to find devices on the local network if prompted.
4. Select the discovered phone and click **Next**. Enter the PIN displayed on the phone into the Mac's pairing dialog.
5. Tap **Trust** on the phone and complete Developer Mode setup as described above.

For earlier iOS versions, use USB for the initial pairing. See [Apple's Device Hub guide](https://developer.apple.com/documentation/xcode/managing-your-simulated-and-physical-devices-in-device-hub) for pairing and connection details.

### Verify and add the phone

In another terminal, check that Xcode lists the phone:

```sh
xcrun devicectl list devices
```

If it is missing, reconnect it by cable, unlock it and check Xcode for unfinished setup or trust prompts. If `devicectl` is unavailable, install and select full Xcode, rather than only the standalone Command Line Tools.

Return to Splashdown and choose **Phone setup complete · scan again**, or start `splash target add device --platform ios --global`. Select the phone, choose a command name and save it. To change the name reported by the phone itself, use **Settings → General → About → Name**, then scan again.

Before running your app, open its project in Xcode and select the development team under **Signing & Capabilities**. Pairing does not verify app signing or installation. Splashdown does not manage Apple accounts or code signing.

## Prepare an Android device

Install current [Android SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools) on the computer and make sure `adb` is on `PATH`. If the Splashdown guide is already open, run the commands below in another terminal.

### Enable Developer options

1. Open **Settings → About phone** and tap **Build number** seven times. On Samsung, it is under **About phone → Software information → Build number**. Follow any confirmation prompt.
2. Find **Developer options** in Settings, often under **System**, and keep its main switch enabled. Menu names vary by manufacturer, so use Settings search if necessary. See [Android's Developer options guide](https://developer.android.com/studio/debug/dev-options).

Choose either USB or Wi-Fi below. USB authorization and wireless pairing are separate. You can use Wi-Fi without connecting a USB cable first on Android 11 or newer.

### Connect by USB

1. Turn on **USB debugging** in Developer options.
2. Connect a data-capable USB cable to the computer and unlock the phone.
3. Accept **Allow USB debugging?** on the phone. This authorizes the computer's RSA key. On your own test machine, you can also select **Always allow from this computer**.
4. Check the connection:

    ```sh
    adb devices -l
    ```

Look for your phone with state `device`. No `adb pair` command is needed for USB.

### Pair over Wi-Fi

These steps require Android 11 or newer and a computer and phone on the same Wi-Fi network:

1. Open **Developer options → Wireless debugging**, turn it on and allow debugging on the network when prompted.
2. Tap **Pair device with pairing code**. Keep this dialog open. It shows a pairing IP address, a port and a six-digit code.
3. Run `adb pair` with the address and port from that dialog, replacing the example below with the values on your phone:

    ```sh
    adb pair 192.0.2.10:37123
    ```

4. Enter the six-digit code when the terminal asks. Wait for the successful-pairing message, then run `adb devices -l`.
5. If the phone is still missing, return to the main **Wireless debugging** page. Use the separate **IP address & Port** displayed there with `adb connect`:

    ```sh
    adb connect 192.0.2.10:42137
    adb devices -l
    ```

The two addresses above are examples. **The pairing port and connection port are different.** Pairing authorizes the computer, while connecting opens the debugging session. A successful pairing alone does not guarantee the phone will appear in Splashdown. Check for state `device` in `adb devices -l`. See [Android's wireless-debugging guide](https://developer.android.com/tools/adb#connect-to-a-device-over-wi-fi).

If Wireless debugging turns off, enable it again and use the connection address currently displayed. Its port can change. If your computer is still listed under **Paired devices**, try `adb connect` before pairing again. Pair again if the computer has been forgotten or its debugging authorization revoked.

### Verify and add the phone

Run:

```sh
adb devices -l
```

| What you see | Next step |
| --- | --- |
| Your phone with state `device` | Return to Splashdown and choose **Phone setup complete · scan again**. |
| `unauthorized` | Unlock the phone and accept its debugging authorization prompt. |
| `offline` | Reconnect the USB cable or use the current Wi-Fi connection address, then check again. |
| No USB entry | Try another data cable or port. With the cable connected, toggle USB debugging off and on, then accept the authorization prompt. |
| No Wi-Fi entry | Check Wireless debugging is still enabled, both devices use the same network, and `adb connect` uses the main page's current connection port. |
| `adb` command not found | Install Platform-Tools and add its `platform-tools` directory to `PATH`. |

If the guide is not open, run `splash target add device --platform android --global`. Select the phone, choose a command name and save it.

Splashdown saves the hardware serial and resolves the current connection when you run or claim the phone. Do not save an IP address, port or wireless discovery service name in Splashdown config. If hardware identity cannot be resolved wirelessly, connect over USB and use the guided add flow.

The Developer options **Stay awake** toggle only prevents sleep while charging. For an unplugged wireless device, temporarily increase the normal display timeout when a long installation needs the screen to remain awake.

## Define your personal devices globally

If you test on real mobile devices (phones or tablets), declare them once in the machine-wide config so every project can use them without re-adding. This is the recommended setup for anyone with a regular set of test devices.

The machine-wide config lives at `~/.config/splashdown/config.toml` (it honors `$XDG_CONFIG_HOME`).
Start the guide to discover, name and save a phone:

```sh
splash target add device --global
# or limit discovery and setup instructions to one platform
splash target add device --platform android --global
```

The guide lists physical phones, using recognizable Android manufacturer and device names when
available, and groups connection problems separately. Choose **Set up an unlisted phone** for
USB or Wi-Fi guidance, then **Phone setup complete · scan again**. Letter shortcuts or arrows and
Enter select an option, and each completed menu becomes a summary of your choice.
Before saving, it shows the exact hardware ID, your chosen target name and the destination
file. See [Add a phone](devices.md#add-a-phone) for the complete flow and cancellation behavior.

For a known hardware ID, the direct command also works without a connected phone or installed SDK:

```sh
splash target add device iphone17 --platform ios --id 00008110-EXAMPLE --global
splash target add device pixel --platform android --id PXL1234 --global
```

Replace the example IDs with your devices' hardware IDs. These commands produce:

```toml
# ~/.config/splashdown/config.toml
[targets.device.iphone17]
platform = "ios"
id = "00008110-EXAMPLE"

[targets.device.pixel]
platform = "android"
id = "PXL1234"
```

Every physical target requires `id`, the iOS hardware UDID or Android hardware serial.
`platform` is optional and accepts `ios` or `android`. Names such as `iphone17` and `pixel` are
command labels. Splashdown matches hardware IDs even when a phone's display name or wireless
connection address changes. If you need machine-readable output, add `--format json` to a direct
named command. The interactive guide requires terminal stdin and stderr.

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
id = "00008110-TEAM-EXAMPLE"

[targets.device.team-pixel]
platform = "android"
id = "TEAM-PIXEL-SERIAL"
```

Replace these example IDs with the team devices' hardware IDs. Keep personal devices in local or
global config when their identifiers should not be committed. A wireless Android connection
address is transient and is not a valid hardware ID. The guide obtains the stable identity before
saving a device.

The recipe records the supported hardware catalog, not the pairing state. Each developer still trusts or pairs the physical device on their machine. See [The recipe](recipe.md) for the complete committed target schema and [Per-checkout overrides](overrides.md) for local and global precedence.

## Run on a physical device

```sh
splash run device iphone17       # configured global target
splash run device team-pixel     # configured recipe target
```

Run a named `device` variant from the recipe, local config, or global config. Undeclared phones
found during discovery are never candidates. In a project that also declares a simulator, bare
`splash run` still targets the simulator.

An exact variant name that exists under only one target type also infers that type, so a global
`device.iphone17` supports `splash run iphone17` even in a project with a simulator. If the same
variant name exists under more than one type, include the type to resolve the ambiguity. Canonical
type names and enabled project type prefixes take precedence in the first slot, so use
`splash run device sim` when a device variant itself is named `sim`.

- iOS: connect and unlock the iPhone, and trust the computer. Wifi-paired devices work too, the tunnel is established at launch.
- Android: enable USB debugging and accept the authorization prompt.

Before framework build or installation, `run` discovers the matching hardware and automatically
claims it for the current checkout. A busy or disconnected target fails before the build starts.
The claim persists if the app exits or launch fails.

Splashdown still does not manage the physical hardware itself. It records only checkout ownership
in the machine-wide `claims.tsv` registry. Physical `stop` and `destroy` leave that claim in place.
See [Running and managing devices](devices.md) for claim inspection, release, forced transfer, and
cleanup commands.

For the built-in React Native Android launcher, Splashdown also sets `ANDROID_SERIAL` to the selected device. This scopes Gradle install tasks and nested `adb` calls when several Android phones are connected.

## Connect to a backend on your computer

On physical hardware, `localhost` and `127.0.0.1` refer to the phone, not the development computer. Splashdown selects the device and coordinates development ports. It does not rewrite the app's API URL.

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

The post-checkout hook provisions both worktrees when they are created, so the two React Native launches receive distinct Metro ports automatically. Run `splash status` in each worktree to inspect resource state, or `splash env` to read assigned values.

For Flutter, each `splash run` stays attached to its own `flutter run` process and receives an independent ephemeral VM-service port. A brief `Waiting for another flutter command to release the startup lock...` message is normal when two commands start together: Flutter serializes startup, then the builds and debug sessions continue independently. Send `r` or `R` in a worktree's terminal to reload only its device, or `d` to detach while leaving that app running.

`splash init` already applied the safe React Native wiring changes, so review and commit them before the first launch. `splash doctor` re-checks them later. In particular, iOS compiles `RCT_METRO_PORT` into the app, so each worktree needs its own build after provisioning. A successful built-in React Native launch now warns when nothing is listening on the configured Metro port. Start Metro and relaunch the app before expecting source changes or Fast Refresh.

Each worktree still needs the framework's normal dependency setup. Install dependencies separately, or use [trusted worktree bootstrap](bootstrap.md) for a shared setup command. Avoid symlinking `node_modules` from another React Native checkout: Metro resolves real paths and can retain paths from the source checkout, causing bundle-resolution or SHA-1 errors. Create the worktree at its final path before running `pod install`. CocoaPods and Hermes-generated files can embed absolute checkout paths. If a prepared worktree is moved and a build still names its old location, remove the generated `ios/Pods` directory and run `pod install` again without replacing `Podfile.lock`.

Two worktrees that share one app bundle id overwrite each other when installed on the same phone. Use one physical device per simultaneously running worktree, or give the builds distinct development bundle ids.

## Troubleshooting physical devices

### iPhone is paired but not connected

- Unlock the phone and confirm it still appears in `xcrun devicectl list devices`.
- A paired Wi-Fi device can show a disconnected tunnel while idle. Use `splash run device <variant>`. The app launch establishes it. `splash start` only checks that physical hardware is discoverable.
- Signing failures are project/Xcode configuration. Select a valid development team under **Signing & Capabilities**.

### Android Wireless debugging turns itself off

Some Android builds disable wireless debugging whenever Wi-Fi reconnects or roams, even when the SSID and IP address appear unchanged. This is common on mesh networks or with adaptive Wi-Fi features.

- Keep the phone near one access point or use an SSID tied to one access point while testing.
- Temporarily disable vendor options that switch to a better Wi-Fi network or mobile data.
- Toggle Wireless debugging on again, then inspect the fresh endpoint with `adb mdns services` or the phone's **IP address & Port** field.
- Pairing is normally persistent. A changing connection port does not require a new pairing code.

### A large Android debug build fails over Wi-Fi

Universal debug APKs can contain native libraries for every ARM and emulator architecture. Install the first large build over USB, or configure the app to produce an architecture-specific development APK. `splash start device <variant>` only verifies target connectivity. `splash run device <variant>` performs the framework's normal build and installation.

### Android rejects the APK as a version downgrade

`INSTALL_FAILED_VERSION_DOWNGRADE` means the connected phone already has the same package id with a higher `versionCode`. Uninstall that copy if its app data is disposable, or build with a version code at least as high as the installed one. Splashdown passes through the framework install result. It does not remove apps or their data.

### Metro reports an FSEvents or Watchman failure

If a new worktree's Metro process exits with `FSEventStreamStart failed`, first try registering that exact checkout and retry the launch:

```sh
watchman watch-project "$PWD"
splash run device <variant>
```

If `watchman watch-project` reports the same error, the Watchman/macOS event stream is unhealthy rather than merely missing the checkout. Disabling Watchman may allow an initial bundle, but it does not restore Fast Refresh when FSEvents itself is not delivering changes. Restarting the Watchman server affects every watched project, so do that only when disrupting their active sessions is acceptable. A macOS restart is the clean fallback.

Do not use `watchman watch-del-all` for this. It removes unrelated projects already watched on the machine without repairing FSEvents.

## Next steps

- [Running and managing devices](devices.md): the full run, start, stop lifecycle, auto-upgrade, and framework detection.
- [Per-checkout overrides](overrides.md): one-off sim variants for a single checkout, and more on machine-wide devices.
- [Framework wiring](framework-wiring.md): patch configs that hardcode the Metro or dev port.
