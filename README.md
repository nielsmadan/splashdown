# splashdown

[![CI](https://github.com/nielsmadan/splashdown/actions/workflows/ci.yml/badge.svg)](https://github.com/nielsmadan/splashdown/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/nielsmadan/splashdown/branch/main/graph/badge.svg)](https://codecov.io/gh/nielsmadan/splashdown)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Per-checkout or per-worktree simulators, emulators, and dev ports for development.**

Do you have any of these problems?

* You installed an app on a simulator / emulator but you forgot which one.
* You created two worktrees from the same project, and now the ports are clashing during dev or e2e testing.
* You want to select a free port for a new project, so it doesn't conflict, but you don't know which one is free.

Splashdown solves them. Pin system resources to your checkouts, keep track of them globally, automatically select free ones when creating new worktrees.

## Contents

- [Install](#install)
- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [The recipe: `splashdown.toml`](#the-recipe-splashdowntoml)
- [Per-checkout overrides: `splashdown.local.toml`](#per-checkout-overrides-splashdownlocaltoml)
- [Shell completion](#shell-completion)
- [Running and managing devices](#running-and-managing-devices)
- [Framework wiring (`splash doctor`)](#framework-wiring-splash-doctor)
- [Profiles and loaders](#profiles-and-loaders)
- [CLI summary](#cli-summary)
- [Global port coordination](#global-port-coordination)
- [CI integration](#ci-integration)
- [Development](#development)

## Install

```sh
brew install nielsmadan/tap/splashdown
# or, managed by mise
mise use -g pipx:splashdown
```

This puts `splash` on your `PATH`. The resource registry at `$XDG_STATE_HOME/splashdown/` (default `~/.local/state/splashdown/`) is shared across every repo on your machine.

## Quick start

In any project (single app or monorepo, web or backend or mobile), `splash init` scans the filesystem, scaffolds the recipe, wires your loader and the post-checkout hook, then allocates ports for this checkout. Most popular frameworks are auto-detected, nothing to declare:

```sh
splash init
# scanning project…
#   detected: pnpm (apps/api/apps/web-admin)
#   apps/api          → node-backend
#   apps/web-admin    → vite
#   shell loader      → mise
# wrote splashdown.toml + splashdown.local.toml + mise.toml + post-checkout hook
# allocating ports…  PORT=9081  WEB_DEV_PORT=5174
# wrote splashdown.env
```

(Pass `--no-sync` to scaffold the files without reserving ports.)

The recipe is on disk, the loader is wired, the hook fires on every checkout. Add a worktree and the second checkout allocates free ports automatically, no manual editing or syncing needed:

```sh
git worktree add ../myapp.feat-x feat-x
cd ../myapp.feat-x

# post-checkout hook fired `splash`. splashdown.env now has the per-checkout ports.
pnpm dev    # api on 9082 instead of 9081, vite on 5175 instead of 5174
```

See [`examples/`](./examples/) for hook + mise wiring patterns. Verify wiring later with `splash doctor` (and `splash doctor --fix` to re-apply).

### Mobile: simulators & emulators

For a mobile app, the scan also declares the simulator/emulator variants in `[targets.*]`. Each checkout gets its own sim/emulator instance (named `<parent>/<cwd>/<variant>`), so worktrees never fight over one device. Boot, build, and launch in one command:

```sh
splash run                            # one target type + one variant: no args needed
splash run simulator                  # name the type when you declare more than one
splash run simulator lowest-supported # ...and a specific variant

splash target                         # list declared variants + which are booted right now
splash stop simulator                 # shut the sim down (keeps it)
```

When a new iOS (or Android system image) lands, recreate the `latest` sims in place and clear out the cruft Xcode/`avdmanager` leave behind:

```sh
splash target refresh                 # destroy + recreate stale 'latest' sims (newer iOS landed)
splash target prune ios               # delete every sim splashdown did NOT create (the Xcode template pile)
splash gc                             # drop registry entries for checkouts you've since deleted
```

Variants pinned to a fixed version (`ios = "17.0"`) are never touched by `refresh` — they're deliberate version coverage. See [Running and managing devices](#running-and-managing-devices) for the full lifecycle.

## How it works

Splashdown is the glue between git and your env loader (mise, direnv, devbox). It installs a `post-checkout` git hook, coexisting with lefthook/husky if you already use a hook manager, that fires `splash` whenever you check out a branch or add a worktree, so each checkout's free ports (and other resources) are allocated and handed to your loader automatically.

Run `splash init` once in your project. Splashdown walks the filesystem, identifies your apps and their frameworks, and writes a recipe (`splashdown.toml`) declaring per-checkout resources (ports, db urls, UUIDs, sim/emulator variants). On every `git checkout` or `git worktree add`, a post-checkout hook fires `splash`, which allocates concrete values into a gitignored `splashdown.env`. Your shell-env loader (mise / direnv / devbox) sources that file automatically, so every process in the checkout sees the right `PORT`, `DATABASE_URL`, etc.

Four files end up in the project:

| File | Committed? | Purpose |
|------|-----------|---------|
| `splashdown.toml` | Yes | Recipe: `[project]`, `[apps.*]`, `[resources.*]`, and any team-shared `[targets.*]` variants |
| `splashdown.local.toml` | **No** (gitignored) | Per-checkout *additional* `[targets.*]` variants on top of the recipe's |
| `splashdown.env` | **No** (gitignored) | Generated `KEY=VALUE` env file. Splashdown owns it (overwritten wholesale on every run; don't hand-edit) |
| `mise.toml` (or `.envrc` / `devbox.json`) | Yes | Your shell-env loader's config; gains a line that sources `splashdown.env` |

The registry at `~/.local/state/splashdown/` is machine-wide, so when two checkouts both want port 8081 splashdown gives one of them 8082, even across unrelated repos.

**Provision output** is change-aware: the first run (or any run that allocates a new port or rewrites a file) prints only the changed vars and writer lines. A no-op run — `git pull --rebase` on a checkout already provisioned — collapses to one line:

```
splashdown: up to date (3 vars, 2 files)
```

This is what you see through lefthook or other hook managers when nothing actually changed.

## The recipe: `splashdown.toml`

The committed file. Four kinds of top-level tables: `[project]`, `[apps.*]`, `[resources.*]`, and (for mobile) `[targets.*]`. The scanner produces a working version; edit freely.

```toml
[project]
workspace = "pnpm"             # single | pnpm | yarn | npm | cargo | gradle
loader    = "mise"             # mise | direnv | devbox | none

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

A common pattern for consumers that need a stable short identifier (e.g. Docker Compose project names have a practical length limit):

```toml
[resources.COMPOSE_PROJECT_NAME]
type     = "template"
template = "myapp-test-{{ truncate(hash(cwd_abs), 8) }}"
# → "myapp-test-352e9e09" — stable per checkout path, 8-char truncated SHA256
```

**For mobile**, the recipe also declares a `[targets.*]` catalog: the simulator and emulator variants the team agrees this project supports. Sim *instances* are created lazily per checkout, named `<parent>/<cwd>/<variant>`. With `ios = "latest"` (the default), the sim is auto-recreated whenever a newer iOS lands; pin an explicit version like `ios = "18.5"` for fixed coverage.

```toml
[targets.simulator.default]
model = "iPhone 17"

[targets.simulator.lowest-supported]
model = "iPhone 12"
ios   = "17.0"

[targets.emulator.default]
device = "pixel_9"
```

For a **plugged-in phone**, declare a `device` target (or just rely on auto-pick). Unlike sims/emulators, splashdown doesn't create or own physical hardware — it discovers what's connected and hands the native id to the launcher. All fields are optional; with one device connected, no config is needed at all.

```toml
[targets.device.default]
# platform = "ios"        # scope auto-pick to one platform: "ios" | "android"
# name     = "My iPhone"  # match by device name
# id       = "..."        # exact udid / adb serial
```

Target types: `simulator`, `emulator`, `device`.

## Per-checkout overrides: `splashdown.local.toml`

A **gitignored**, per-checkout file. Use it to **add** extra target variants on top of what the recipe declares (never to override or repeat). Each checkout has its own copy; what you add here is local to this worktree/clone.

```toml
# Reproduce a bug only this checkout sees:
[targets.simulator.repro-bug]
model = "iPhone 16"
ios   = "17.5"
```

Name collisions with a recipe-declared variant are an error (pick a different variant name). Add programmatically with:

```sh
splash target add simulator repro-bug --model="iPhone 16" --ios=17.5
```

## Shell completion

`splash` ships bash/zsh tab-completion (subcommands, device types, and dynamic device-variant names).

| Install method | Setup |
|---|---|
| Homebrew | Zero-touch — the formula installs completion files. |
| mise | `mise use -g pipx:argcomplete`, then add the line below. |

For **zsh**, load bash-compat completion first:

```zsh
autoload -U +X bashcompinit && bashcompinit
eval "$(register-python-argcomplete splash)"
```

For **bash**:

```bash
eval "$(register-python-argcomplete splash)"
```

## Running and managing devices

```sh
splash run     [type] [variant]    # reconcile + start + build + launch
splash start   [type] [variant]    # reconcile + start (no build/launch)
splash stop    [type] [variant]    # shut down the device (preserves it)
splash destroy [type] [variant]    # delete the device + its registry entry
```

Both `type` and `variant` are optional. `type` is inferred when exactly one target type is declared; otherwise pass `simulator`, `emulator`, or `device`. `variant` defaults to `default`, then to the only declared variant if there's just one, else errors with the list of choices.

```sh
splash run                            # one type, one variant, no args needed
splash run simulator                  # picks `default`
splash run simulator lowest-supported

splash target                         # show every declared variant + its live sim state
splash stop    simulator              # shut down the running sim
splash destroy simulator small-screen # delete that variant's sim
splash target remove simulator repro-bug      # strip a local variant (and destroy its sim)
splash target remove simulator repro-bug --keep-instance   # toml-only edit
```

Framework auto-detected for `run`:

- `pubspec.yaml` → `flutter run -d <id>`
- `package.json` with `react-native` → `npx react-native run-ios --udid` / `run-android --deviceId`
- `package.json` with `expo` + `app.json` → `npx expo run:ios --device` / `run:android --device`
- `*.xcodeproj` / `*.xcworkspace` at root (no JS/Flutter signals) → `xcodebuild build` → `xcrun simctl install`/`launch` (or `xcrun devicectl` for a physical device). Needs `[project.ios] scheme = "..."`.
- `build.gradle*` + `settings.gradle*` at root (no JS/Flutter signals) → `./gradlew :module:installVariant` → `adb shell am start`. Tunable via `[project.android] module`/`variant`/`application_id`/`launch_activity`.
- Override via `[project] framework = "..."`

### Auto-upgrade: no more manual `mksim`/`simctl delete` after Xcode updates

Variants with `ios = "latest"` (the default) reconcile on every `splash run`. If the registered sim's iOS is older than the current latest, splashdown destroys the old sim and creates a new one in place. Pinned variants (`ios = "17.0"`) are left alone forever; they're explicit version coverage.

```sh
splash gc                           # drop dead-checkout entries (ports, vars, sims)
splash target refresh               # destroy + recreate stale 'latest' sims (newer iOS landed)
splash target prune [ios|android]   # destroys every sim/AVD splashdown did NOT create
                                    # (the Xcode default-template pile, hand-made sims, etc.)
```

### iOS sim management

Backed by `xcrun simctl`. Default device type: latest iPhone Pro. Default runtime: latest installed. Sim name defaults to `<parent-dir>/<checkout-name>/<variant>` so different worktrees and variants never collide. Override per-variant with `model = "..."` and `ios = "18.5"` in the recipe (or in `splashdown.local.toml` for an add-only variant).

### Android emulator management

Backed by `avdmanager` / `sdkmanager` / `emulator` / `adb` from `$ANDROID_HOME`. Default device profile: `pixel_9`. Default system image: latest installed, falling back to a known-good Android 34 image. AVD is created if missing, then booted detached; `splash` polls `adb` for the serial to appear.

### Physical devices

`splash run device` builds and launches on a connected phone. Discovery uses `xcrun devicectl` (iOS, Xcode 15+) and `adb devices` (Android); the device's native udid/serial is passed straight to the framework launcher.

```sh
splash run device                 # auto-picks the one connected device
splash target                       # device rows show: connected / absent / ambiguous
```

With exactly one device plugged in, no config is needed — auto-pick resolves it. When both an iPhone and an Android phone are connected, or several of one platform, narrow with the variant's `platform`, `id`, or `name` (or `splash target add device <variant> --platform ios` / `--id ...` / `--name "..."`).

Because splashdown doesn't own the hardware, the lifecycle verbs differ: `start` just confirms the device is connected, and `stop`/`destroy` are no-ops (nothing is created, so nothing is torn down). Physical devices are never written to the registry.

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

A **Loader** is the per-shell-env-tool wiring: how `splashdown.env` gets sourced into your shell when you `cd` into the project. Splashdown supports three: `mise` (sets `_.file = "splashdown.env"` in `mise.toml`), `direnv` (appends `dotenv_if_exists splashdown.env` between sentinel markers in `.envrc`, then prompts you to `direnv allow`), `devbox` (adds an `init_hook` entry in `devbox.json`). All three are idempotent and reversible.

When no loader config is found, splashdown does **not** impose one. It picks `none` and instead routes the generated values straight into a dotenv file the project already reads — `.env` if present, else `.env.local` — provided at least one app actually reads dotenv files (Next.js, Django, FastAPI, Node backends). For apps that only see values exported into the process environment (Vite, Spring Boot, mobile), a plain dotenv file can't reach them, so splashdown keeps generating `splashdown.env` and prints how to source it or which loader to install. Force any choice with `splash init --loader=mise|direnv|devbox|none`.

**Override at any layer.** Edit `[project] workspace`, `[project] loader`, `[apps.<name>] profile`, or any `[resources.*]` table; splashdown picks up the change on the next sync and never re-scans unless you ask. `splash init --rescan` re-runs the scanner against the current filesystem (e.g. after you add a new app to the monorepo).

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
splash                              # sync this checkout (the post-checkout hook runs this)
splash --version
splash sync [--force] [--setup N]   # pick free ports, resolve vars, write splashdown.env
splash status [all]                 # resources + targets + which ports are bound right now
splash init [preset] [--rescan] [--no-sync] [--loader=…] [--overwrite]   # scaffold + first sync
splash doctor [--fix] [--framework=…]

splash run     [type] [variant]     # boot target + build + launch
splash start   [type] [variant]     # boot target (no build/launch)
splash stop    [type] [variant]     # shut down
splash destroy [type] [variant]     # delete this checkout's target instance

splash target                       # list declared targets + live state
splash target add/remove <type> <variant> …
splash target refresh [ios|android] # recreate stale sims/emulators
splash target prune   [ios|android] # destroy sims/emulators splashdown didn't create

splash env                          # list this checkout's resolved values
splash env get KEY | set KEY=VALUE | release [KEY]

splash gc                           # drop dead-checkout entries (ports, vars, sims)
```

`splash status` answers "what's the state of this checkout?": resolved env vars (with `[in use]` / `[free]` for port-typed resources), declared device variants and whether each is booted, and a count of stale registry rows. `splash sync --force` reallocates ports; the auto-reallocation lives in `Registry.allocate_port`, so plain `splash` does the same thing. `splash init` scaffolds the project files and then runs the first sync so the current checkout has values immediately (`--no-sync` scaffolds only). `splash init --rescan` re-scans the filesystem, useful after adding a new app to a monorepo. Available presets for `splash init`: `rn`, `flutter`, `server` (alias `nextjs`), `electron`, `ios-native`, `android-native`, `minimal`.

## Global port coordination

The registry at `~/.local/state/splashdown/{ports.tsv,kv.tsv}` is **machine-wide**, not per-repo. When any checkout allocates a port, the allocator considers:

1. Every other checkout's pinned ports (any repo, any worktree)
2. Live `bind()` probes (catches ports held by non-splashdown processes)

So three unrelated projects can each declare `range = [3000, 3100]` and never collide. Splashdown hands them 3000, 3001, 3002 (or whatever's free at allocation time).

Lazy GC: entries for checkouts whose directory no longer exists are dropped on next allocation. That's how `git worktree remove` cleanup works without a hook.

## CI integration

CI runners are ephemeral and have no global registry — `splash` is not installed there, and there's nothing to allocate from. The pattern is to **write `splashdown.env` directly** in the CI job with the fixed ports your service containers expose, so the rest of the job sees the same env-file shape that local dev uses:

```yaml
# GitHub Actions example
- name: Write splashdown.env with fixed CI ports
  run: |
    cat > splashdown.env << 'EOF'
    POSTGRES_PORT=5432
    REDIS_PORT=6379
    DATABASE_URL=postgresql://user:pass@localhost:5432/testdb
    REDIS_URL=redis://localhost:6379
    COMPOSE_PROJECT_NAME=myapp-test-ci
    EOF

- name: Run integration tests
  run: bun --env-file=.env.test --env-file=splashdown.env test tests/
```

Key points:
- **`splash` is never installed or run in CI.** The heredoc produces the same `KEY=VALUE` format splashdown emits locally.
- **Each CI step runs in a fresh shell.** Any step that needs values from `splashdown.env` must load it explicitly — either via `--env-file` flags, `source splashdown.env`, or by exporting the vars as job-level `env:`. A step that writes the file does not automatically export its contents into subsequent steps.
- **`splashdown.local.toml` must be in `.gitignore`.** Its first line says "Gitignored, not committed" — the file contains per-checkout device declarations that vary between machines. Once committed, every fresh clone starts with a tracked file that `splash target add` will mutate, polluting `git status` permanently.

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
