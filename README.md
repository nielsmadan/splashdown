# splashdown / `spd`

**Per-checkout simulators, emulators, and dev ports for mobile development on macOS.**

You're working on the same iOS or Android app across three git worktrees. Each worktree should have its own iPhone sim, its own Metro port, its own DB name, its own everything — automatically, with no manual coordination. That's what `splashdown` does.

```sh
git worktree add ../myapp.feat-auth feat-auth
cd ../myapp.feat-auth
# → already has its own sim "myapp/myapp.feat-auth"
# → already has its own RCT_METRO_PORT (e.g. 8082, picked to not collide
#   with any other checkout's pinned ports machine-wide)
spd device run        # builds + installs + launches on that sim
```

No new commands in day-to-day work: a `post-checkout` hook fires `spd` on every `git checkout` / `git worktree add`, and `mise activate` (already in your shell) loads the resolved env vars from a gitignored `mise.local.toml`.

The mobile bits work on macOS only (iOS via `xcrun simctl`, Android via the SDK's `avdmanager`/`emulator`). The generic resource provisioner (ports, UUIDs, templates) works on Linux too.

## Why it exists

Mobile dev across parallel checkouts is a coordination nightmare. Two worktrees both want Metro `8081`. Two simulators both want to be named `myapp`. Two CocoaPods caches collide. You end up shutting down half your work to switch branches, or hand-rolling per-worktree sed scripts. `splashdown` makes the "you have N checkouts" case as smooth as the "you have one checkout" case.

Read [`provision-spec.md`](./provision-spec.md) for the original design rationale.

## Status

Working v1. Stdlib-only Python 3.11+. 41 tests passing.

## Install

```sh
brew install nielsmadan/tap/splashdown          # recommended (macOS)
# or
pipx install splashdown
```

This puts `spd` on your `PATH`. The registry at `$XDG_STATE_HOME/splashdown/` (default `~/.local/state/splashdown/`) is shared across every repo on your machine.

## Quick start: React Native worktrees

In your existing RN repo:

```sh
spd init --preset=rn
# writes .worktree.toml:
#   [resources.RCT_METRO_PORT] type="port" range=[8081,8200]
#   [resources.SIM_NAME]       template="{{ basename(parent) }}/{{ cwd }}"
#   [devices.iphone]           type="ios-sim"
#   [project]                  framework="react-native"

# wire the post-checkout hook so future worktrees provision automatically:
git config core.hooksPath .githooks
mkdir -p .githooks
cp $(pipx environment --value PIPX_LOCAL_VENVS)/splashdown/share/post-checkout .githooks/   # or copy from this repo's examples/

# add to .gitignore:
echo "mise.local.toml" >> .gitignore

# first run:
spd
spd device run        # boots the named sim if needed, builds, installs, launches
```

After that, every `git worktree add` gets its own sim, port, and resolved env, with zero manual steps. See [`examples/`](./examples/) for the hook + `mise.toml` task definitions.

## Recipe — `.worktree.toml`

The committed file that says *what* the repo needs per-checkout. Lives once at the repo root.

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

[devices.iphone]
type  = "ios-sim"
# model = "iPhone 16 Pro"       # optional; default = latest iPhone Pro
# ios   = "18.5"                # optional; default = latest installed runtime

[devices.android]
type   = "android-emulator"
# device = "pixel_7"
# image  = "system-images;android-34;google_apis;arm64-v8a"

[project]
framework = "react-native"      # auto | react-native | flutter | expo
```

Resource types: `port`, `uuid`, `template`, `cwd`, `cwd-slug`, `set`.
Device types: `ios-sim`, `android-emulator`.
Template scope: `cwd`, `cwd_abs`, `branch`, `repo`, `parent`, `basename`, `dirname`, `slug`, `lower`, `upper`, `truncate`, `uuid`, `hash`, `port_hash`, plus prior resolved resources.
Writers: `mise` (default → `mise.local.toml`), `envfile=PATH`, `envrc`, `stdout`, `none`.

## Device commands

```
spd device list                 # show declared devices + state (booted/shutdown/absent)
spd device boot [NAME]          # create-if-missing + boot
spd device run  [NAME]          # boot + build + launch the app
spd device shutdown [NAME]
spd device destroy [NAME]
```

If exactly one device is declared, `NAME` is optional. With multiple, omit it to list; supply it to act on one.

`spd device run` auto-detects the framework:

- `pubspec.yaml` → `flutter run -d <id>`
- `package.json` with `react-native` → `npx react-native run-ios --udid` / `run-android --deviceId`
- `package.json` with `expo` + `app.json` → `npx expo run:ios --device` / `run:android --device`
- Override via `[project] framework = "..."`

### iOS sim management

Backed by `xcrun simctl`. Default device type: latest iPhone Pro. Default runtime: latest installed. Sim name defaults to `<parent-dir>/<checkout-name>` so two worktrees never collide. Override per-recipe with `model = "..."` and `ios = "18.5"`.

### Android emulator management

Backed by `avdmanager` / `sdkmanager` / `emulator` / `adb` from `$ANDROID_HOME`. Default device profile: `pixel_7`. Default system image: latest installed, falling back to a known-good Android 34 image. AVD is created if missing, then booted detached; `spd` polls `adb` for the serial to appear.

## Non-mobile use cases

The same machinery works for any per-checkout resource. Web/backend repos can declare just `[resources.*]` (and skip `[devices.*]`) for things like:

- `PORT = [3000, 3100]` for Next.js / Vite dev servers
- `DATABASE_URL` templates with checkout-unique DB names
- `STORYBOOK_PORT`, `STAGING_API_URL`, etc.

Presets ship for `nextjs`, `rn`, `flutter`, `minimal`. Linux is supported for these non-mobile cases; the `device` subcommands obviously need macOS.

## CLI summary

```
spd                             # provision (what the post-checkout hook calls)
spd provision --reprovision     # force re-allocate (regenerates uuids)
spd init [--preset=rn|flutter|nextjs|minimal]
spd list                        # this checkout's resolved vars
spd get KEY [--checkout=PATH]
spd set KEY=VALUE
spd unpin [KEY]
spd gc
spd device list|boot|run|shutdown|destroy [NAME]
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
just install-local              # build + put `spd` on PATH locally
just tag-release-patch          # bump patch, commit, tag, push (triggers release.yml)
```

See `Justfile` and `.github/workflows/release.yml` for the release flow. Tagging publishes a GitHub release and auto-updates the `Formula/splashdown.rb` in `nielsmadan/homebrew-tap`.
