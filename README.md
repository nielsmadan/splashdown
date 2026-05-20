# splashdown

**Per-checkout simulators, emulators, and dev ports for mobile development on macOS.**

You're working on the same iOS or Android app across three git worktrees. Each worktree should have its own iPhone sim, its own Metro port, its own DB name, its own everything — automatically, with no manual coordination. That's what `splashdown` does.

```sh
git worktree add ../myapp.feat-auth feat-auth
cd ../myapp.feat-auth
# → already has its own sim "myapp/myapp.feat-auth"
# → already has its own RCT_METRO_PORT (e.g. 8082, picked to not collide
#   with any other checkout's pinned ports machine-wide)
splash device run        # builds + installs + launches on that sim
```

No new commands in day-to-day work: a `post-checkout` hook fires `splash` on every `git checkout` / `git worktree add`, and `mise activate` (already in your shell) loads the resolved env vars from a gitignored `splashdown.env`.

The mobile bits work on macOS only (iOS via `xcrun simctl`, Android via the SDK's `avdmanager`/`emulator`). The generic resource provisioner (ports, UUIDs, templates) works on Linux too.

## Why it exists

Mobile dev across parallel checkouts is a coordination nightmare. Two worktrees both want Metro `8081`. Two simulators both want to be named `myapp`. Two CocoaPods caches collide. You end up shutting down half your work to switch branches, or hand-rolling per-worktree sed scripts. `splashdown` makes the "you have N checkouts" case as smooth as the "you have one checkout" case.

A key benefit of per-checkout device config: each worktree gets its own, non-colliding simulator. One checkout boots `myapp/feat-auth`, another boots `myapp/main` — they never step on each other, you can run both simultaneously.

Read [`provision-spec.md`](./provision-spec.md) for the original design rationale.

## Status

Working v1. Stdlib-only Python 3.11+. 64 tests passing.

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
| `splashdown.local.toml` | **No** (gitignored) | Per-checkout `[devices.*]` config |
| `splashdown.env` | **No** (gitignored) | Generated `KEY=VALUE` env file; splashdown owns it |
| `mise.toml` | Yes | Your existing mise config; gains `_.file = "splashdown.env"` |

Devices live only in `splashdown.local.toml`, never committed. This means each checkout (worktree or clone) can declare its own simulator or emulator without affecting others — different checkouts get different, non-colliding simulators automatically.

`splashdown.env` is overwritten wholesale on every `splash` run. Don't edit it by hand.

## Quick start: React Native worktrees

In your existing RN repo:

```sh
splash init --preset=rn
# writes splashdown.toml:
#   [resources.RCT_METRO_PORT] type="port" range=[8081,8200]
#   [resources.SIM_NAME]       template="{{ basename(parent) }}/{{ cwd }}"
#   [project]                  framework="react-native"
# writes splashdown.local.toml skeleton (gitignored, per-checkout devices)
# adds splashdown.env + splashdown.local.toml to .gitignore
# adds _.file = "splashdown.env" to mise.toml
# installs .githooks/post-checkout

# declare a device for this checkout:
splash device add iphone --type=ios-sim

# first run:
splash
splash device run        # boots the named sim if needed, builds, installs, launches
```

After that, every `git worktree add` gets its own sim, port, and resolved env, with zero manual steps. See [`examples/`](./examples/) for the hook + `mise.toml` task definitions.

## File model: `splashdown.toml`

The committed file that says *what* the repo needs per-checkout. Lives once at the repo root. Contains only `[resources.*]` and `[project]` — no device declarations.

```toml
[resources.RCT_METRO_PORT]
type  = "port"
range = [8081, 8200]            # globally-coordinated lowest-free

[resources.RUN_ID]
type = "uuid"

[resources.SIM_NAME]
type     = "template"
template = "{{ basename(parent) }}/{{ cwd }}"

[resources.METRO_URL]
type     = "template"
template = "http://localhost:{{ RCT_METRO_PORT }}"

[project]
framework = "react-native"      # auto | react-native | flutter | expo
```

Resource types: `port`, `uuid`, `template`, `cwd`, `cwd-slug`, `set`.
Template scope: `cwd`, `cwd_abs`, `branch`, `repo`, `parent`, `basename`, `dirname`, `slug`, `lower`, `upper`, `truncate`, `uuid`, `hash`, `port_hash`, plus prior resolved resources.

## File model: `splashdown.local.toml`

A **gitignored**, per-checkout file. Each worktree or clone has its own copy with its own device declarations. Never committed — this is what gives each checkout a non-colliding simulator.

```toml
# splashdown.local.toml — per-checkout device config. NOT committed.

[devices.iphone]
type = "ios-sim"
# model = "iPhone 16 Pro"       # optional; default = latest iPhone Pro
# ios   = "18.5"                # optional; default = latest installed runtime

[devices.android]
type   = "android-emulator"
# device = "pixel_7"
# image  = "system-images;android-34;google_apis;arm64-v8a"
```

Device types: `ios-sim`, `android-emulator`.

Add a device with `splash device add <name> --type=<type>`, or edit `splashdown.local.toml` directly.

## Device commands

```
splash device list                 # show declared devices + state (booted/shutdown/absent)
splash device boot [NAME]          # create-if-missing + boot
splash device run  [NAME]          # boot + build + launch the app
splash device shutdown [NAME]
splash device destroy [NAME]
```

If exactly one device is declared, `NAME` is optional. With multiple, omit it to list; supply it to act on one.

`splash device run` auto-detects the framework:

- `pubspec.yaml` → `flutter run -d <id>`
- `package.json` with `react-native` → `npx react-native run-ios --udid` / `run-android --deviceId`
- `package.json` with `expo` + `app.json` → `npx expo run:ios --device` / `run:android --device`
- Override via `[project] framework = "..."`

### iOS sim management

Backed by `xcrun simctl`. Default device type: latest iPhone Pro. Default runtime: latest installed. Sim name defaults to `<parent-dir>/<checkout-name>` so two worktrees never collide. Override per-checkout with `model = "..."` and `ios = "18.5"` in `splashdown.local.toml`.

### Android emulator management

Backed by `avdmanager` / `sdkmanager` / `emulator` / `adb` from `$ANDROID_HOME`. Default device profile: `pixel_7`. Default system image: latest installed, falling back to a known-good Android 34 image. AVD is created if missing, then booted detached; `splash` polls `adb` for the serial to appear.

## Non-mobile use cases

The same machinery works for any per-checkout resource. Web/backend repos can declare just `[resources.*]` in `splashdown.toml` (and have no `splashdown.local.toml`) for things like:

- `PORT = [3000, 3100]` for Next.js / Vite dev servers
- `DATABASE_URL` templates with checkout-unique DB names
- `STORYBOOK_PORT`, `STAGING_API_URL`, etc.

Presets ship for `nextjs`, `rn`, `flutter`, `minimal`. Linux is supported for these non-mobile cases; the `device` subcommands obviously need macOS.

## CLI summary

```
splash                             # provision (what the post-checkout hook calls)
splash provision --reprovision     # force re-allocate (regenerates uuids)
splash init [--preset=rn|flutter|nextjs|minimal]
splash list                        # this checkout's resolved vars
splash get KEY [--checkout=PATH]
splash set KEY=VALUE
splash unpin [KEY]
splash gc
splash device list|boot|run|shutdown|destroy [NAME]
splash device add NAME --type=TYPE
```

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
just install-local              # build + put `splash` on PATH locally
just tag-release-patch          # bump patch, commit, tag, push (triggers release.yml)
```

See `Justfile` and `.github/workflows/release.yml` for the release flow. Tagging publishes a GitHub release and auto-updates the `Formula/splashdown.rb` in `nielsmadan/homebrew-tap`.
