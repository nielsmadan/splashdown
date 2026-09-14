<p align="center">
  <img src="./logo.svg" alt="splashdown" width="200">
</p>

# splashdown

[![CI](https://github.com/nielsmadan/splashdown/actions/workflows/ci.yml/badge.svg)](https://github.com/nielsmadan/splashdown/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/nielsmadan/splashdown/branch/main/graph/badge.svg)](https://codecov.io/gh/nielsmadan/splashdown)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)](#status)

**Per-checkout simulators, emulators, dev ports, and exclusive physical-device claims.**

<p align="center"><img src="docs/user/assets/demo.gif" alt="splashdown demo: two git worktrees automatically get different, non-colliding ports" width="750"></p>

Do you have any of these problems?

* You installed an app on a simulator / emulator but you forgot which one.
* You created two worktrees from the same project, and now the ports are clashing during dev or e2e testing.
* Two worktrees are trying to build and launch on the same connected phone.
* You want to select a free port for a new project, so it doesn't conflict, but you don't know which one is free.

Splashdown solves them. Pin system resources to your checkouts, keep track of them globally, automatically select free ones when creating new worktrees.

- **Automatic.** A `post-checkout` git hook allocates this checkout's free ports and env vars on every branch switch or `git worktree add`, with no manual editing.
- **Machine-wide.** A global registry coordinates ports and physical-device claims across every repo and worktree.
- **Framework-aware.** It detects your stack and wires the env loader, the git hook, and per-checkout iOS simulators and Android emulators.

📖 **Full documentation: [splashdown.dev](https://splashdown.dev)**

## Status

splashdown is **alpha** (pre-1.0). It is actively used and works well, but the CLI surface and the `splashdown.toml` schema may still shift between minor releases while the design settles. Expect incremental changes, nothing drastic. Any breaking change is called out in the [changelog](CHANGELOG.md), and while on `0.x` it can land in a minor version, so pin a version if you need strict stability.

## Install

```sh
brew install nielsmadan/tap/splashdown
# or, managed by mise
mise use -g pipx:splashdown
```

This puts `splash` on your `PATH`. The resource registry at `$XDG_STATE_HOME/splashdown/` (default `~/.local/state/splashdown/`) is shared across every repo on your machine.

### Shell completion

`splash` ships bash/zsh tab-completion for subcommands, device types, and device-variant names. With Homebrew it is zero-touch, the formula installs the completion files. With any other install (mise, pipx, uv), add one line to your shell rc. `splash` bundles everything it needs, so there is no separate package to install and no `bashcompinit` step.

zsh (`~/.zshrc`):

```zsh
eval "$(splash completion zsh)"
```

bash (`~/.bashrc`):

```bash
eval "$(splash completion bash)"
```

`splash completion` with no argument autodetects your shell. More detail at [splashdown.dev/shell-completion](https://splashdown.dev/shell-completion/).

## Quick start

Adoption takes two commands. At the Git worktree root of any project (single app or monorepo, web or backend or mobile), `splash init` scans the filesystem, scaffolds the recipe, and writes the project's loader and post-checkout hook configuration. It allocates nothing, so you can read and edit the generated recipe first. When the root already has `AGENTS.md` or an independent `CLAUDE.md`, init also adds concise framework-specific instructions so coding agents use the allocated ports, and it leaves that file alone when another tool generates it. Most popular frameworks are auto-detected, nothing to declare:

```sh
splash init
# scanning project…
#   detected: pnpm (apps/api/apps/web-admin)
#   apps/api          → node-backend
#   apps/web-admin    → vite
#   shell loader      → mise (detected mise.toml)
#   env output        → splashdown.env
# wrote splashdown.toml + splashdown.local.toml + mise.toml
# updated AGENTS.md
# configuration written; nothing is allocated or active yet
# next: run `splash trust` to activate automatic post-checkout handling
#       run `splash` to allocate values and write splashdown.env
```

Then activate the checkout. `splash trust` authorizes automatic handling for this clone, installs the local post-checkout hook, and allows the loader configuration when it holds splashdown's integration and nothing else. Bare `splash` runs the first sync:

```sh
splash trust
splash
#   PORT (changed)
#   WEB_DEV_PORT (changed)
#   -> splashdown.env: 2 vars (changed)
```

Init creates a project in the current directory, or the directory selected by top-level
`--cwd PATH`, including subdirectories of a Git worktree. Replacing an existing recipe requires
`--overwrite`. Nested init leaves the worktree-root post-checkout hook untouched because Git
runs that hook from the root. It prints the explicit `splash --cwd PATH sync` command to run
after checkout.

Splashdown validates the complete recipe before reserving anything or changing generated
files. Unknown sections or fields, invalid resource writers, bad template references, and
incompatible target fields are hard errors with the exact config path to fix.

Pick a different destination with `splash init --env-file PATH`, for example `.env` when an app
already reads that file. The choice is recorded as `env_file` under `[project]`, the selected
loader is wired to read it, and splashdown manages only the keys the recipe declares, leaving the
rest of the file intact.

The recipe is on disk, the loader is wired, the hook fires on every checkout. Add a worktree and the second checkout allocates free ports automatically, no manual editing or syncing needed:

```sh
git worktree add ../myapp.feat-x feat-x
cd ../myapp.feat-x

# post-checkout hook provisioned splashdown.env with the per-checkout ports.
pnpm dev    # api on 9082 instead of 9081, vite on 5175 instead of 5174
```

See [`examples/`](./examples/) for hook + mise wiring patterns. Verify wiring later with `splash doctor` (and `splash doctor --fix` to re-apply).

> Multi-app / monorepo setups: see [splashdown.dev/monorepos](https://splashdown.dev/monorepos/).

### Trusted worktree bootstrap

Projects can share one-time worktree setup without making cloned repositories execute shell code
automatically:

```toml
[bootstrap]
run = ["pnpm install --frozen-lockfile", "python manage.py migrate"]
```

Review the recipe, then run `splash trust` and `splash bootstrap`. Trust belongs to this clone and
is shared by its linked worktrees. Another clone starts untrusted and its hook writes nothing.
Future `git worktree add` operations provision the checkout and run bootstrap once, while ordinary
branch switches only sync resources. Trust covers code in future refs too, including scripts called
by an unchanged command. `splash init` grants no trust of its own.
Revoke it with `splash untrust`. Full security and retry behavior:
[splashdown.dev/bootstrap](https://splashdown.dev/bootstrap/).

### Mobile: simulators & emulators

For a mobile app, the scan also declares the simulator/emulator variants in `[targets.*]`. Each checkout gets its own sim/emulator instance (named `<parent>/<cwd>/<variant>-<path-hash>`), so even unrelated clones with the same trailing directories never fight over one device. Boot, build, and launch in one command:

```sh
splash run                            # one target type + one variant: no args needed
splash run simulator                  # name the type when you declare more than one
splash run simulator lowest-supported # ...and a specific variant

splash target                         # list declared variants + which are booted right now
splash stop simulator                 # shut the sim down (keeps it)
```

A lone exact variant name also selects its target type when that name is unique across the merged
project, local, and global catalog, for example `splash run iphone17` for a global physical device.
If two target types use the same variant name, include the type. Type names and enabled project type
prefixes take precedence in the first slot, so use the explicit two-token form for a colliding
variant.

`splash run` refreshes the recipe's resource outputs and passes every resolved resource to the
launcher, overriding stale shell values. It works from shells without a mise or direnv loader.
Physical-device runs warn about loopback addresses in resources and, for React Native and Expo
on iOS, missing or unverifiable local-network descriptions. See the
[device guide](https://splashdown.dev/devices/) for setup and preflight limits.

Configured physical targets from `splashdown.toml`, `splashdown.local.toml`, and the global config
also participate in machine-wide claims. Undeclared phones are never allocated. A physical run
claims a free connected target before the framework builds. A target owned by another live
checkout, or a configured target that is disconnected, fails before build or installation.

```sh
splash run pixel                       # claim if free, then build and launch
splash target claims                   # inspect every claim without device discovery
device=$(splash target claim --available android)
splash target release pixel
splash target claim pixel --force      # explicitly transfer a busy claim
```

Claims belong to the checkout and survive process exit and failed launches. `splash stop device`
does not release them. Use `splash target release --all`, `splash deinit`, or `splash gc` for
cleanup. A project can claim one available phone after successful linked-worktree setup:

```toml
[project.worktree]
claim_device = "android" # ios | android | any
```

Automatic allocation is limited to five seconds and is non-fatal when no device can be selected.
The hook prints the manual `splash target claim --available android` retry command.

When a new iOS or Android system image lands, reconcile the managed fleet and clear out the cruft
Xcode and `avdmanager` leave behind:

```sh
splash target refresh                 # reconcile registered devices across every tracked checkout
splash target prune ios               # delete every sim splashdown did NOT create (the Xcode template pile)
splash gc                             # drop registry entries for checkouts you've since deleted
```

`target refresh` is machine-wide. It recreates stale or externally deleted registered instances at
their declared runtime or image, resolving `latest` live. It also destroys undeclared instances
and instances from deleted checkouts without confirmation. It does not create a declared variant
that has never been run. A healthy variant pinned to a fixed version such as `ios = "17.0"` is not
upgraded. See [Running and managing devices](https://splashdown.dev/devices/) for the full lifecycle.

| Target | macOS | Linux |
| --- | --- | --- |
| iOS simulator/device | Xcode required | Unsupported. Explicit commands return an actionable error |
| Android emulator/device | Android SDK required | Android SDK required |
| Ports, environment, and config | Supported | Supported |

Unscoped fleet commands (`target refresh`, `target prune`, `gc`, and status inspection) warn once
and continue when one platform is unavailable. Explicit iOS commands, including `target refresh
ios`, return exit 1 with the missing macOS/Xcode requirement and no traceback.

## Documentation

Full guides and reference live at **[splashdown.dev](https://splashdown.dev)**:

- [How it works](https://splashdown.dev/how-it-works/): the git-hook + env-loader glue.
- [The recipe: `splashdown.toml`](https://splashdown.dev/recipe/): apps, resources, mobile targets.
- [Running and managing devices](https://splashdown.dev/devices/): sims, emulators, physical devices.
- [Framework wiring (`splash doctor`)](https://splashdown.dev/framework-wiring/): patch configs that hardcode the port.
- [Monorepos](https://splashdown.dev/monorepos/): multi-app workspaces, worked end to end.
- [CLI reference](https://splashdown.dev/cli/): every `splash` subcommand.

## Development

```sh
just test                       # run pytest
just build                      # sdist + wheel
just install                    # install or replace the current-source snapshot as `splash`
just install-editable           # link `splash` to this checkout
just uninstall                  # remove the CLI installation, preserving configuration and data
just release                    # propose a version, confirm or override, then publish
just release --dry-run          # inspect the proposal without checks or publication
```

Re-running `just install` refreshes the installed code even when the version is unchanged.
With `just install-editable`, source edits take effect without reinstalling.

See [Release and distribution](docs/tech/release.md) for version overrides and the release flow. Tagging publishes a GitHub release and auto-updates the `Formula/splashdown.rb` in `nielsmadan/homebrew-tap`. See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup and commit conventions.
