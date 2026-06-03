# splashdown

**Per-checkout or per-worktree simulators, emulators, and dev ports for development.**

Do you have any of these problems?

* You installed an app on a simulator / emulator but you forgot which one.
* You created two worktrees from the same project, and now the ports are clashing during dev or e2e testing.
* You want to select a free port for a new project, so it doesn't conflict, but you don't know which one is free.

Splashdown is here to solve these problems. Pin system resources to your checkouts, keep track of them globally, automatically select free ones when creating new worktrees.

## Install

```sh
brew install nielsmadan/tap/splashdown          
# or
pipx install splashdown
```

This puts `splash` on your `PATH`. The registry at `$XDG_STATE_HOME/splashdown/` (default `~/.local/state/splashdown/`) is shared across every repo on your machine.

## How it works

Run `splash init` once in your project. Splashdown walks the filesystem, identifies your apps and their frameworks, and writes a recipe (`splashdown.toml`) declaring per-checkout resources (ports, db urls, UUIDs, sim/emulator variants). On every `git checkout` or `git worktree add`, a post-checkout hook fires `splash`, which allocates concrete values into a gitignored `splashdown.env`. Your shell-env loader (mise / direnv / devbox) sources that file automatically, so every process in the checkout sees the right `PORT`, `DATABASE_URL`, etc.

Four files end up in the project:

| File | Committed? | Purpose |
|------|-----------|---------|
| `splashdown.toml` | Yes | Recipe: `[project]`, `[apps.*]`, `[resources.*]`, and any team-shared `[devices.*]` variants |
| `splashdown.local.toml` | **No** (gitignored) | Per-checkout *additional* `[devices.*]` variants on top of the recipe's |
| `splashdown.env` | **No** (gitignored) | Generated `KEY=VALUE` env file. Splashdown owns it (overwritten wholesale on every run; don't hand-edit) |
| `mise.toml` (or `.envrc` / `devbox.json`) | Yes | Your shell-env loader's config; gains a line that sources `splashdown.env` |

The registry at `~/.local/state/splashdown/` is machine-wide, so when two checkouts both want port 8081 splashdown gives one of them 8082, even across unrelated repos.

## Quick start

In any project (single app or monorepo, web or backend or mobile):

```sh
splash init
# scanning project…
#   detected: pnpm (apps/api/apps/web-admin)
#   apps/api          → node-backend
#   apps/web-admin    → vite
#   shell loader      → mise
# wrote splashdown.toml + splashdown.local.toml + mise.toml + post-checkout hook
```

The recipe is on disk, the loader is wired, the hook fires on every checkout. Add a worktree and the second checkout picks free ports automatically:

```sh
git worktree add ../myapp.feat-x feat-x
cd ../myapp.feat-x
# post-checkout hook fired `splash`. splashdown.env now has the per-checkout ports.
pnpm dev    # api on 9082 instead of 9081, vite on 5175 instead of 5174
```

For React Native, the legacy preset path also still works and applies the four `rn-*` wiring fixes (Metro port, package.json scripts, `ios/.xcode.env`) in one go:

```sh
splash init rn
splash run simulator       # boots a per-checkout sim, builds, installs, launches
```

See [`examples/`](./examples/) for hook + mise wiring patterns. Verify wiring later with `splash doctor` (and `splash doctor --fix` to re-apply).

## The recipe: `splashdown.toml`

The committed file. Four kinds of top-level tables: `[project]`, `[apps.*]`, `[resources.*]`, and (for mobile) `[devices.*]`. The scanner produces a working version; edit freely.

```toml
[project]
workspace = "pnpm"             # single | pnpm | yarn | npm | cargo | gradle
loader    = "mise"             # mise | direnv | devbox

[apps.api]
path      = "apps/api"
profile   = "node-backend"     # vite | nextjs | node-backend | django | fastapi |
                               # springboot | react-native | expo | flutter |
                               # ios-native | android-native | unknown
resources = ["PORT"]

[apps.web-admin]
path      = "apps/web-admin"
profile   = "vite"
resources = ["WEB_DEV_PORT", "API_DEV_PORT"]

[resources.PORT]
type  = "port"
range = [9081, 9100]           # globally-coordinated lowest-free

[resources.WEB_DEV_PORT]
type  = "port"
range = [5174, 5200]

[resources.API_DEV_PORT]
type     = "template"
template = "{{ PORT }}"        # Vite's /api proxy must hit the api's actual port
```

Resource types: `port`, `uuid`, `template`, `cwd`, `cwd-slug`, `set`.
Template scope: `cwd`, `cwd_abs`, `branch`, `repo`, `parent`, `basename`, `dirname`, `slug`, `lower`, `upper`, `truncate`, `uuid`, `hash`, `port_hash`, plus prior resolved resources.

**For mobile**, the recipe also declares a `[devices.*]` catalog: the simulator and emulator variants the team agrees this project supports. Sim *instances* are created lazily per checkout, named `<parent>/<cwd>/<variant>`. With `ios = "latest"` (the default), the sim is auto-recreated whenever a newer iOS lands; pin an explicit version like `ios = "18.5"` for fixed coverage.

```toml
[devices.simulator.default]
model = "iPhone 17"

[devices.simulator.lowest-supported]
model = "iPhone 12"
ios   = "17.0"

[devices.emulator.default]
device = "pixel_9"
```

Device types: `simulator`, `emulator`.

## Per-checkout overrides: `splashdown.local.toml`

A **gitignored**, per-checkout file. Use it to **add** extra device variants on top of what the recipe declares (never to override or repeat). Each checkout has its own copy; what you add here is local to this worktree/clone.

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

## Running and managing devices

```sh
splash run     [type] [variant]    # reconcile + start + build + launch
splash start   [type] [variant]    # reconcile + start (no build/launch)
splash stop    [type] [variant]    # shut down the device (preserves it)
splash destroy [type] [variant]    # delete the device + its registry entry
```

Both `type` and `variant` are optional. `type` is inferred when exactly one device type is declared; otherwise pass `simulator` or `emulator`. `variant` defaults to `default`, then to the only declared variant if there's just one, else errors with the list of choices.

```sh
splash run                            # one type, one variant, no args needed
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

### Auto-upgrade: no more manual `mksim`/`simctl delete` after Xcode updates

Variants with `ios = "latest"` (the default) reconcile on every `splash run`. If the registered sim's iOS is older than the current latest, splashdown destroys the old sim and creates a new one in place. Pinned variants (`ios = "17.0"`) are left alone forever; they're explicit version coverage.

```sh
splash device gc                    # registry cleanup: defunct checkouts only
splash device refresh               # destroy + recreate stale 'latest' sims (newer iOS landed)
splash device prune [ios|android|all] [--yes] [--dry-run]
                                    # destroys every sim/AVD splashdown did NOT create
                                    # (the Xcode default-template pile, hand-made sims, etc.)
```

### iOS sim management

Backed by `xcrun simctl`. Default device type: latest iPhone Pro. Default runtime: latest installed. Sim name defaults to `<parent-dir>/<checkout-name>/<variant>` so different worktrees and variants never collide. Override per-variant with `model = "..."` and `ios = "18.5"` in the recipe (or in `splashdown.local.toml` for an add-only variant).

### Android emulator management

Backed by `avdmanager` / `sdkmanager` / `emulator` / `adb` from `$ANDROID_HOME`. Default device profile: `pixel_9`. Default system image: latest installed, falling back to a known-good Android 34 image. AVD is created if missing, then booted detached; `splash` polls `adb` for the serial to appear.

## Framework wiring (`splash doctor`)

Allocating a port doesn't always reach the running process. Most frameworks hardcode the port in one or two config files that override the env var, so splashdown carries per-framework wiring checks that detect those hardcoded points and (where safe) auto-patch them. `splash init` runs the wiring after scaffolding; `splash doctor` re-runs it anytime to verify, and `splash doctor --fix` re-applies the autofixes.

| Profile | Check | What it ensures |
|---|---|---|
| react-native | `rn-hook` | post-checkout fires `splash`, wired through your existing hook manager (lefthook / husky) instead of clobbering `core.hooksPath` |
| react-native | `rn-metro-config` | `metro.config.js` consumes `RCT_METRO_PORT`. Auto-patches the recognized `port: <N>` literal shape; otherwise prints the exact snippet to paste |
| react-native | `rn-pkg-port` | `package.json` `start`/`ios`/`android` scripts don't carry `--port <N>` (which would override the env var); auto-stripped |
| react-native | `rn-xcode-env` | `ios/.xcode.env` exports a splashdown-managed `RCT_METRO_PORT` block. iOS bakes the port into the binary at compile time, so Xcode-GUI builds need this to pick up the per-checkout port |
| vite | `vite-config-process-env` | `vite.config.{ts,js}` reads env vars from `process.env` rather than `loadEnv()`. Auto-rewrites `env.X` → `process.env.X` so splashdown.env loaded by mise/direnv/devbox reaches Vite |
| springboot | `springboot-application-properties` | `application.properties` / `application.yml` uses the `server.port=${PORT:8080}` placeholder. Manual-only (Java config rewrites are too risky to automate) |

**Hook-manager coexistence.** `splash` detects lefthook (`lefthook.{yml,yaml}` or in `package.json` devDeps), husky (`.husky/`), or an existing `core.hooksPath`, and wires the post-checkout entry in whichever it finds. Only as a last resort does it own `.githooks/` + `core.hooksPath`.

```sh
splash doctor                    # read-only report (✓/✗ per check)
splash doctor --fix              # apply autofixes; print manual instructions for the rest
splash doctor --framework=react-native   # override detection if needed
```

**Known limitation: RN Android.** Android's Metro port is also baked into the build (via the RN Gradle plugin / `BuildConfig`), with a different mechanism than iOS. Splashdown doesn't currently wire the Android side; for now `yarn android` works (the RN CLI propagates `RCT_METRO_PORT` to Gradle), but bare `gradle assembleDebug` may default to 8081. Tracked as a future check.

## Profiles and loaders

Two extension points decide what `splash init` produces.

A **Profile** is the per-framework integration rules: what resources this kind of app wants, and what config files (if any) need patching so the values reach the running process. The Vite Profile, for example, emits `WEB_DEV_PORT` (and `API_DEV_PORT` if it sees a `server.proxy` block) and rewrites `vite.config.{ts,js}` to read `process.env.X` instead of `loadEnv()`. The Spring Boot Profile emits `PORT` and checks that `application.properties` uses the `server.port=${PORT:8080}` placeholder. The mobile Profiles (`react-native`, `expo`, `flutter`, `ios-native`, `android-native`) bring in the existing per-framework wiring checks.

A **Loader** is the per-shell-env-tool wiring: how `splashdown.env` gets sourced into your shell when you `cd` into the project. Splashdown supports three: `mise` (sets `_.file = "splashdown.env"` in `mise.toml`), `direnv` (appends `dotenv splashdown.env` between sentinel markers in `.envrc`), `devbox` (adds an `init_hook` entry in `devbox.json`). All three are idempotent and reversible.

**Override at any layer.** Edit `[project] workspace`, `[project] loader`, `[apps.<name>] profile`, or any `[resources.*]` table; splashdown picks up the change on the next provision and never re-scans unless you ask. `splash refresh-inventory` re-runs the scanner against the current filesystem (e.g. after you add a new app to the monorepo).

**Multi-instance collisions** are mangled at scan time. Two Vite apps both want `WEB_DEV_PORT`; the scanner renames them `WEB_DEV_PORT_ADMIN` / `WEB_DEV_PORT_CUSTOMER` based on the app names, so the recipe stays unambiguous.

**Unknown framework.** An app whose framework splashdown doesn't recognize gets `profile = "unknown"`: no resources allocated for it, no wiring attempted. The rest of the project still works. To add support, contribute a Profile upstream.

### The `writer` field (power-user escape hatch)

Resources route to `splashdown.env` by default; that's what mise/direnv/devbox load. For consumers that can't read `process.env` (legacy build systems, vendor tooling, frameworks splashdown doesn't have a Profile for yet), set `writer` on the resource:

```toml
[resources.LEGACY_PORT]
type   = "port"
range  = [9999, 10100]
writer = "envfile=path/to/legacy/.env"
```

Available writers: `splashdown-env` (default), `envfile=PATH` (any .env-format file, preserves non-managed lines), `envrc` (writes `.envrc.local`), `stdout` (echoes), `none` (registry-only, no file output).

Most of the time the framework Profile handles routing implicitly and `writer` stays unset. Use it when no Profile covers your consumer yet.

## CLI summary

```
splash                             # provision (what the post-checkout hook calls)
splash --version
splash provision --reprovision     # force re-allocate (regenerates uuids etc.)
splash refresh                     # re-provision and reallocate any port a process squatted on
splash status                      # resources + devices + which ports are bound right now
splash init [NAME] [--loader=mise|direnv|devbox] [--force]
                                   # scan project → write recipe + wire loader + hook
                                   # NAME picks a named scaffold (rn / flutter / server / etc.)
splash refresh-inventory           # re-scan and rewrite [project] / [apps.*] in place
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
splash device prune [ios|android|all] [--yes] [--dry-run]
                                   # destroy every sim/AVD splashdown didn't create

splash list                        # this checkout's resolved env vars
splash get KEY [--checkout=PATH]
splash set KEY=VALUE
splash release [KEY]               # release this checkout's registry entries (all, or just KEY)
splash gc                          # GC the resource registry (ports, uuids, devices)
```

`splash status` answers "what's the state of this checkout?": resolved env vars (with `[in use]` / `[free]` for port-typed resources), declared device variants and whether each is booted, and a count of stale registry rows. `splash refresh` fixes port collisions; the auto-reallocation lives in `Registry.allocate_port`, so plain `splash` does the same thing. `splash refresh-inventory` re-scans the filesystem, useful after adding a new app to a monorepo. Available presets for `splash init NAME`: `rn`, `flutter`, `server` (alias `nextjs`), `electron`, `ios-native`, `android-native`, `minimal`.

## Global port coordination

The registry at `~/.local/state/splashdown/{ports.tsv,kv.tsv}` is **machine-wide**, not per-repo. When any checkout allocates a port, the allocator considers:

1. Every other checkout's pinned ports (any repo, any worktree)
2. Live `bind()` probes (catches ports held by non-splashdown processes)

So three unrelated projects can each declare `range = [3000, 3100]` and never collide. Splashdown hands them 3000, 3001, 3002 (or whatever's free at allocation time).

Lazy GC: entries for checkouts whose directory no longer exists are dropped on next allocation. That's how `git worktree remove` cleanup works without a hook.

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
