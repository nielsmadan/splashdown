# splashdown

**Per-checkout simulators, emulators, and dev ports for mobile development on macOS.**

You're working on the same iOS or Android app across three git worktrees. Each worktree should have its own iPhone sim, its own Metro port, its own DB name, its own everything — automatically, with no manual coordination. That's what `splashdown` does.

```sh
git worktree add ../myapp.feat-auth feat-auth
cd ../myapp.feat-auth
# → already has its own sim "myapp/myapp.feat-auth"
# → already has its own RCT_METRO_PORT (e.g. 8082, picked to not collide
#   with any other checkout's pinned ports machine-wide)
splash run simulator       # builds + installs + launches on the per-checkout sim
```

No new commands in day-to-day work: a `post-checkout` hook fires `splash` on every `git checkout` / `git worktree add`, and `mise activate` (already in your shell) loads the resolved env vars from a gitignored `splashdown.env`.

The mobile bits work on macOS only (iOS via `xcrun simctl`, Android via the SDK's `avdmanager`/`emulator`). The generic resource provisioner (ports, UUIDs, templates) works on Linux too.

## Why it exists

Mobile dev across parallel checkouts is a coordination nightmare. Two worktrees both want Metro `8081`. Two simulators both want to be named `myapp`. Two CocoaPods caches collide. You end up shutting down half your work to switch branches, or hand-rolling per-worktree sed scripts. `splashdown` makes the "you have N checkouts" case as smooth as the "you have one checkout" case.

A key benefit of per-checkout device config: each worktree gets its own, non-colliding simulator. One checkout boots `myapp/feat-auth`, another boots `myapp/main` — they never step on each other, you can run both simultaneously.

Read [`provision-spec.md`](./provision-spec.md) for the original design rationale.

## Status

Working v1. Stdlib-only Python 3.11+. 151 tests passing.

## Install

```sh
brew install nielsmadan/tap/splashdown          # recommended (macOS)
# or
pipx install splashdown
```

This puts `splash` on your `PATH`. The registry at `$XDG_STATE_HOME/splashdown/` (default `~/.local/state/splashdown/`) is shared across every repo on your machine.

## The three-file model

splashdown uses three files, each with a clear purpose:

| File | Committed? | Purpose |
|------|-----------|---------|
| `splashdown.toml` | Yes | Recipe — `[resources.*]` + `[project]` schema only |
| `splashdown.local.toml` | **No** (gitignored) | Per-checkout *additional* `[devices.*]` variants (recipe's are not repeated) |
| `splashdown.env` | **No** (gitignored) | Generated `KEY=VALUE` env file; splashdown owns it |
| `mise.toml` | Yes | Your existing mise config; gains `_.file = "splashdown.env"` |

Devices live only in `splashdown.local.toml`, never committed. This means each checkout (worktree or clone) can declare its own simulator or emulator without affecting others — different checkouts get different, non-colliding simulators automatically.

`splashdown.env` is overwritten wholesale on every `splash` run. Don't edit it by hand.

## Quick start: React Native worktrees

In your existing RN repo:

```sh
splash init --preset=rn
# Scaffolding:
#   - splashdown.toml (resources + [project] framework="react-native")
#   - splashdown.local.toml skeleton (gitignored, per-checkout devices)
#   - .gitignore (adds splashdown.env, splashdown.local.toml)
#   - mise.toml (adds [env] _.file = "splashdown.env")
#   - post-checkout hook (via your hook manager: lefthook / husky / .githooks)
# Framework wiring (auto-applied; safe + idempotent):
#   - metro.config.js: server.port → Number(process.env.RCT_METRO_PORT) || 8081
#   - package.json: strips --port from start/ios/android scripts
#   - ios/.xcode.env: splashdown-managed RCT_METRO_PORT block (so the iOS build
#     bakes the per-checkout port instead of the default 8081)

# first run:
splash
splash run simulator       # boots the per-checkout sim (creates if missing,
                         # auto-recreates if newer iOS is available),
                         # then builds + installs + launches the app

# add a one-off variant just for this checkout:
splash device add simulator repro-bug --model="iPhone 16" --ios=17.5
splash run simulator repro-bug
```

After that, every `git worktree add` gets its own sim, port, and resolved env, with zero manual steps. See [`examples/`](./examples/) for the hook + `mise.toml` task definitions.

Want to verify or re-apply the wiring later? `splash doctor` (and `splash doctor --fix`). See "Framework wiring" below.

## File model: `splashdown.toml`

The committed file. Resource slots + the catalog of device variants the team agrees this project supports.

```toml
[resources.RCT_METRO_PORT]
type  = "port"
range = [8081, 8200]            # globally-coordinated lowest-free

[resources.METRO_URL]
type     = "template"
template = "http://localhost:{{ RCT_METRO_PORT }}"

# Device variant catalog. Sim *instances* are created lazily per checkout,
# named `<parent>/<cwd>/<variant>`. With `ios = "latest"` (the default), the sim
# is auto-recreated whenever a newer iOS lands. Pin an explicit version like
# `ios = "18.5"` for fixed coverage that never auto-upgrades.
[devices.simulator.default]
model = "iPhone 17"

[devices.simulator.lowest-supported]
model = "iPhone 12"
ios   = "17.0"

[project]
framework = "react-native"      # auto | react-native | flutter | expo
```

Resource types: `port`, `uuid`, `template`, `cwd`, `cwd-slug`, `set`.
Template scope: `cwd`, `cwd_abs`, `branch`, `repo`, `parent`, `basename`, `dirname`, `slug`, `lower`, `upper`, `truncate`, `uuid`, `hash`, `port_hash`, plus prior resolved resources.
Device types: `simulator`, `emulator`.

## File model: `splashdown.local.toml`

A **gitignored**, per-checkout file. Use it to **add** extra device variants on top of what the recipe declares — never to override or repeat. Each checkout has its own copy; what you add here is local to this worktree/clone.

```toml
# Reproduce a bug only this checkout sees:
[devices.simulator.repro-bug]
model = "iPhone 16"
ios   = "17.5"
```

Name collisions with a recipe-declared variant are an error (pick a different variant name). Add programmatically with:

```sh
splash device add simulator repro-bug --model="iPhone 16" --ios=17.5
```

## Running on a device

```sh
splash run     [type] [variant]    # reconcile + start + build + launch
splash start   [type] [variant]    # reconcile + start (no build/launch)
splash stop    [type] [variant]    # shut down the device (preserves it)
splash destroy [type] [variant]    # delete the device + its registry entry
```

Both `type` and `variant` are optional. `type` is inferred when exactly one device type is declared; otherwise pass `simulator` or `emulator`. `variant` defaults to `default`, then to the only declared variant if there's just one, else errors with the list of choices.

```sh
splash run                            # one type, one variant — just run
splash run simulator                  # picks `default`
splash run simulator lowest-supported

splash devices                        # show every declared variant + its live sim state
splash stop    simulator              # shut down the running sim
splash destroy simulator small-screen # delete that variant's sim
splash device remove simulator repro-bug      # strip a local variant (and destroy its sim)
splash device remove simulator repro-bug --keep-instance   # toml-only edit
```

Framework auto-detected for `run`:

- `pubspec.yaml` → `flutter run -d <id>`
- `package.json` with `react-native` → `npx react-native run-ios --udid` / `run-android --deviceId`
- `package.json` with `expo` + `app.json` → `npx expo run:ios --device` / `run:android --device`
- `*.xcodeproj` / `*.xcworkspace` at root (no JS/Flutter signals) → `xcodebuild build` → `xcrun simctl install`/`launch`. Needs `[project.ios] scheme = "..."`.
- `build.gradle*` + `settings.gradle*` at root (no JS/Flutter signals) → `./gradlew :module:installVariant` → `adb shell am start`. Tunable via `[project.android] module`/`variant`/`application_id`/`launch_activity`.
- Override via `[project] framework = "..."`

### Auto-upgrade — no more manual `mksim`/`simctl delete` after Xcode updates

Variants with `ios = "latest"` (the default) reconcile on every `splash run`. If the registered sim's iOS is older than the current latest, splashdown destroys the old sim and creates a new one in place. Pinned variants (`ios = "17.0"`) are left alone forever — they're explicit version coverage.

```sh
splash device gc                    # registry cleanup: defunct checkouts only
splash device refresh               # destroy + recreate stale 'latest' sims (newer iOS landed)
splash device prune [--yes] [--dry-run] [--platforms=ios,android]
                                    # destroys every sim/AVD splashdown did NOT create
                                    # (the Xcode default-template pile, hand-made sims, etc.)
```

### iOS sim management

Backed by `xcrun simctl`. Default device type: latest iPhone Pro. Default runtime: latest installed. Sim name defaults to `<parent-dir>/<checkout-name>/<variant>` so different worktrees and variants never collide. Override per-variant with `model = "..."` and `ios = "18.5"` in the recipe (or in `splashdown.local.toml` for an add-only variant).

### Android emulator management

Backed by `avdmanager` / `sdkmanager` / `emulator` / `adb` from `$ANDROID_HOME`. Default device profile: `pixel_9`. Default system image: latest installed, falling back to a known-good Android 34 image. AVD is created if missing, then booted detached; `splash` polls `adb` for the serial to appear.

## Non-mobile use cases

The same machinery works for any per-checkout resource. Web/backend repos can declare just `[resources.*]` in `splashdown.toml` (and have no `splashdown.local.toml`) for things like:

- `PORT = [3000, 3100]` for Next.js / Vite dev servers
- `DATABASE_URL` templates with checkout-unique DB names
- `STORYBOOK_PORT`, `STAGING_API_URL`, etc.

Presets ship for `server` (alias: `nextjs`), `electron`, `rn`, `flutter`, `ios-native`, `android-native`, and `minimal`. `server` covers any web/backend (Next.js, Django, Rails, FastAPI, Spring Boot, …) with a PORT + DATABASE_URL. `electron` adds `ELECTRON_USER_DATA_DIR` so parallel checkouts don't clobber each other's userData / IndexedDB / SingleInstanceLock. The two `*-native` presets cover plain Swift/Obj-C (`xcodebuild` + `xcrun simctl`) and plain Kotlin/Java (`gradlew` + `adb`) apps — splashdown handles per-checkout sim/emulator provisioning and shells out to the native toolchain for build + install + launch. Linux is supported for the non-mobile cases; the `device` subcommands obviously need macOS (iOS) or a working `adb`/AVD setup (Android).

## Framework wiring (`splash doctor`)

Just provisioning `RCT_METRO_PORT` isn't enough — an RN project typically hardcodes Metro's port in two or three places that override the env var. Splashdown ships a per-framework spec of those wiring points and applies them for you. `splash init --preset=rn` invokes the wiring after scaffolding; `splash doctor` re-runs it anytime to verify and `splash doctor --fix` to re-apply.

**React Native checks (four):**

| id | what it ensures |
|---|---|
| `rn-hook` | post-checkout fires `splash`, wired through your existing hook manager (lefthook / husky) instead of clobbering it via `core.hooksPath` |
| `rn-metro-config` | `metro.config.js` consumes `RCT_METRO_PORT`. **Auto-patches only the recognized literal `port: <N>` shape** — if your config is unusual, the doctor prints the exact snippet to paste |
| `rn-pkg-port` | `package.json` `start`/`ios`/`android` scripts (and anything calling `react-native`) don't carry `--port <N>` (which would override the env var); auto-stripped |
| `rn-xcode-env` | `ios/.xcode.env` exports a splashdown-managed `RCT_METRO_PORT` block. iOS bakes the port into the binary at compile time (`RCTBundleURLProvider`'s `defaultPort` is a `#define` set via `GCC_PREPROCESSOR_DEFINITIONS`); this makes Xcode-GUI builds use the per-checkout port instead of the default |

**Hook-manager coexistence.** `splash` detects lefthook (`lefthook.{yml,yaml}` or in `package.json` devDeps), husky (`.husky/`), or an existing `core.hooksPath`, and wires the post-checkout entry in whichever it finds. Only as a last resort does it own `.githooks/` + `core.hooksPath`.

**Usage:**
```sh
splash doctor                    # read-only report (✓/✗ per check)
splash doctor --fix              # apply autofixes; print manual instructions for the rest
splash doctor --framework=react-native   # override detection if needed
```

**Known limitation — Android.** Android's Metro port is also baked into the build (via the RN Gradle plugin / `BuildConfig`), with a different mechanism than iOS. Splashdown doesn't currently wire the Android side; for now `yarn android` works (the RN CLI propagates `RCT_METRO_PORT` to Gradle), but bare `gradle assembleDebug` may default to 8081. Tracked as a future check.

## CLI summary

```
splash                             # provision (what the post-checkout hook calls)
splash --version
splash provision --reprovision     # force re-allocate (regenerates uuids)
splash refresh                     # re-provision and reallocate any port a process squatted on
splash status                      # resources + devices + which ports are bound right now
splash init [--preset=rn|flutter|server|electron|ios-native|android-native|minimal]
splash doctor [--fix] [--framework=NAME]   # framework-aware wiring check

splash run     [type] [variant]    # reconcile + start + build + launch
splash start   [type] [variant]    # reconcile + start (no build/launch)
splash stop    [type] [variant]    # shut down the device (preserves it)
splash destroy [type] [variant]    # delete the device + its registry entry

splash devices                     # all declared variants + live sim state
splash device add <type> <variant> [--model] [--ios] [--device] [--image]
splash device remove <type> <variant> [--keep-instance]   # also destroys the sim
splash device gc                   # registry cleanup: defunct checkouts only
splash device refresh              # destroy + recreate stale 'latest' sims
splash device prune [--yes] [--dry-run] [--platforms=ios,android]
                                   # destroy every sim/AVD splashdown didn't create

splash list                        # this checkout's resolved env vars
splash get KEY [--checkout=PATH]
splash set KEY=VALUE
splash release [KEY]               # release this checkout's registry entries (all, or just KEY)
splash gc                          # GC the resource registry (ports, uuids, devices)
```

`splash status` answers "what's the state of this checkout?" — the resolved env vars (with `[in use]` / `[free]` for port-typed resources), the declared device variants and whether each is booted, and a count of stale registry rows. `splash refresh` re-runs provision and is the named verb for "fix any port collision" — the underlying detection lives in `Registry.allocate_port`, so plain `splash` does the same thing; `refresh` just makes the intent legible.

## Global port coordination

The registry at `~/.local/state/splashdown/{ports.tsv,kv.tsv}` is **machine-wide**, not per-repo. When any checkout allocates a port, the allocator considers:

1. Every other checkout's pinned ports (any repo, any worktree)
2. Live `bind()` probes (catches ports held by non-splashdown processes)

So three unrelated mobile apps can each declare `range = [8081, 8200]` and never collide.

Lazy GC: entries for checkouts whose directory no longer exists are dropped on next allocation — this is how `git worktree remove` cleanup works without a hook.

## Development

```sh
just test                       # run pytest
just build                      # sdist + wheel
just install-local              # install local source as `splash` via uv
just refresh-local              # reinstall after changes
just reset-local                # uninstall the local `splash`
just tag-release-patch          # bump patch, commit, tag, push (triggers release.yml)
```

See `Justfile` and `.github/workflows/release.yml` for the release flow. Tagging publishes a GitHub release and auto-updates the `Formula/splashdown.rb` in `nielsmadan/homebrew-tap`.
