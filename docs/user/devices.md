# Running and managing devices

```sh
splash run     [type] [variant]    # reconcile + start + build + launch
splash start   [type] [variant]    # reconcile + start (no build/launch)
splash stop    [type] [variant]    # shut down the device (preserves it)
splash destroy [type] [variant]    # delete the device + its registry entry
```

Both `type` and `variant` are optional. `type` is inferred when exactly one target type is declared. Otherwise pass `simulator`, `emulator`, or `device`. `variant` defaults to `default`, then to the only declared variant if there's just one, else errors with the list of choices.

## Platform support

| Target | macOS | Linux |
| --- | --- | --- |
| iOS simulator/device | Xcode required | Unsupported; explicit commands return an actionable error |
| Android emulator/device | Android SDK required | Android SDK required |
| Ports, environment, and config | Supported | Supported |

An explicit unsupported target returns exit 1 with the missing host or tool requirement and no
traceback. Commands that inspect or reconcile both platforms—`target refresh`, `target prune`,
`gc`, and status inspection—warn once, skip the unavailable platform, and continue supported work.
Status labels skipped targets `unavailable`; it does not report them as missing or stale.

**Prefix matching** (on by default): you can abbreviate both `type` and `variant` to any unambiguous prefix: `splash run sim` resolves the simulator type, `splash run sim low` the `lowest-supported` variant. A prefix that matches more than one variant errors with the candidates. A type prefix wins over an identically-prefixed variant name. Toggle it off in [Settings](settings.md).

```sh
splash run                            # one type, one variant, no args needed
splash run simulator                  # picks `default`
splash run sim                        # prefix → simulator
splash run sim low                    # prefix → simulator / lowest-supported
splash run simulator lowest-supported

splash target                         # show every declared variant + its live sim state
splash stop    simulator              # shut down the running sim
splash destroy simulator small-screen # delete that variant's sim
splash target remove simulator repro-bug      # strip a local variant (and destroy its sim)
splash target remove simulator repro-bug --keep-instance   # toml-only edit
```

`target remove` only accepts variants declared in `splashdown.local.toml`. Recipe-owned or missing
variants and malformed config are rejected before any simulator or emulator is touched. If the
lifecycle step reports an error, the local declaration and registry row remain intact. If the
configured name changed after provisioning, removal still deletes the instance recorded in the
registry; an instance that is already absent does not block config cleanup.

Use `--keep-instance` when the SDK is unavailable or you intentionally want a config-only removal.
It removes the local declaration but leaves any instance and registry row untouched. If a row
exists, a later `splash target refresh` sees it as undeclared and destroys the retained instance.

Framework auto-detected for `run`:

- `pubspec.yaml` → `flutter run -d <id>`
- `package.json` with `react-native` → `npx react-native run-ios --udid` / `run-android --deviceId`. Optional `[project.ios] scheme`/`mode` and `[project.android] mode` forward `--scheme`/`--mode` to select the Xcode scheme / build variant (e.g. a `*Dev` scheme that copies `.env.development`).
- `package.json` with `expo` + `app.json` → `npx expo run:ios --device` / `run:android --device`
- `*.xcodeproj` / `*.xcworkspace` at root (no JS/Flutter signals) → `xcodebuild build` → `xcrun simctl install`/`launch` (or `xcrun devicectl` for a physical device). `splash init` records the sole shared scheme automatically, prompts for a choice when interactive, or accepts `--ios-scheme=NAME`.
- `build.gradle*` + `settings.gradle*` at root (no JS/Flutter signals) → `./gradlew :module:installVariant` → `adb shell am start`. Tunable via `[project.android] module`/`variant`/`application_id`/`launch_activity`.
- Override via `[project] framework = "..."`

## Custom run command

Set `[project] run` when the built-in launcher isn't what you want: a different package manager (`yarn`/`pnpm` instead of `npx`), a monorepo subdir, `expo start --dev-client`, or any custom wrapper. It **replaces** the framework launcher (and skips framework detection), so it also works on a project splashdown doesn't recognize. splashdown still reconciles and boots the declared `[targets.*]` first. Your command is the launch step, run in a shell at the repo root.

`run` is either a single string (shared) or a `[project.run]` table with `ios`/`android` keys when they differ:

```toml
# Flutter — one command for both platforms:
[project]
run = "flutter run -d {device_id}"

# React Native with yarn — per platform:
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

Variants with `ios = "latest"` (the default) reconcile on every `splash run`. If the registered sim's iOS is older than the current latest, splashdown destroys the old sim and creates a new one in place. Pinned variants (`ios = "17.0"`) are left alone forever. They're explicit version coverage.

Some apps *require* a pinned older runtime: a pod that excludes arm64 for the simulator (e.g. Google ML Kit) only builds on an x86_64 sim, which only iOS ≤ 18.x provides. Against the default (newest, arm64-only) sim, `xcodebuild` fails with an opaque "Unable to find a destination". Pin `ios = "18.5"`. On a failed `react-native` iOS run, splash detects the exclusion and prints this hint.

```sh
splash gc                           # drop dead-checkout entries (ports, vars, sims)
splash target refresh               # destroy + recreate stale 'latest' sims (newer iOS landed)
splash target prune [ios|android]   # destroys every sim/AVD splashdown did NOT create
                                    # (the Xcode default-template pile, hand-made sims, etc.)
```

## iOS sim management

Backed by `xcrun simctl`. Default device type: latest iPhone Pro. Default runtime: latest installed. Sim name defaults to `<parent-dir>/<checkout-name>/<variant>` so different worktrees and variants never collide. Override per-variant with `model = "..."` and `ios = "18.5"` in the recipe (or in `splashdown.local.toml` for an add-only variant).

## Android emulator management

Backed by `avdmanager` / `sdkmanager` / `emulator` / `adb` from `$ANDROID_HOME`. Default device profile: `pixel_9`. Default system image: latest installed, falling back to a known-good Android 34 image. AVD is created if missing, then booted detached. `splash` polls `adb` for the serial to appear.

## Physical devices

`splash run device` builds and launches on a connected phone. Discovery uses `xcrun devicectl` (iOS, Xcode 15+) and `adb devices` (Android). The device's native udid/serial is passed straight to the framework launcher.

```sh
splash run device                 # auto-picks the one connected device
splash target                       # device rows show: connected / absent / ambiguous
```

With exactly one device plugged in, no config is needed, auto-pick resolves it. When both an iPhone and an Android phone are connected, or several of one platform, narrow with the variant's `platform`, `id`, or `name` (or `splash target add device <variant> --platform ios` / `--id ...` / `--name "..."`).

Because splashdown doesn't own the hardware, the lifecycle verbs differ: `start` just confirms the device is connected, and `stop`/`destroy` are no-ops (nothing is created, so nothing is torn down). Physical devices are never written to the registry.

Declare your test phones once, machine-wide, and they're available in every project without re-adding them per repo: `splash target add device my-iphone --platform ios --name "..." --global`. See [machine-wide test devices](overrides.md#machine-wide-test-devices).
