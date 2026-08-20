<p align="center">
  <img src="./logo.svg" alt="splashdown" width="200">
</p>

# splashdown

[![CI](https://github.com/nielsmadan/splashdown/actions/workflows/ci.yml/badge.svg)](https://github.com/nielsmadan/splashdown/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/nielsmadan/splashdown/branch/main/graph/badge.svg)](https://codecov.io/gh/nielsmadan/splashdown)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)](#status)

**Per-checkout or per-worktree simulators, emulators, and dev ports for development.**

<p align="center"><img src="docs/user/assets/demo.gif" alt="splashdown demo: two git worktrees automatically get different, non-colliding ports" width="750"></p>

Do you have any of these problems?

* You installed an app on a simulator / emulator but you forgot which one.
* You created two worktrees from the same project, and now the ports are clashing during dev or e2e testing.
* You want to select a free port for a new project, so it doesn't conflict, but you don't know which one is free.

Splashdown solves them. Pin system resources to your checkouts, keep track of them globally, automatically select free ones when creating new worktrees.

- **Automatic.** A `post-checkout` git hook allocates this checkout's free ports and env vars on every branch switch or `git worktree add`, with no manual editing.
- **Machine-wide.** A global registry coordinates resources across every repo and worktree, so two checkouts never grab the same port.
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

In any project (single app or monorepo, web or backend or mobile), `splash init` scans the filesystem, scaffolds the recipe, wires your loader and the post-checkout hook, then allocates ports for this checkout. When the root already has `AGENTS.md` or an independent `CLAUDE.md`, init also adds concise framework-specific instructions so coding agents use the allocated ports. Most popular frameworks are auto-detected, nothing to declare:

```sh
splash init
# scanning project…
#   detected: pnpm (apps/api/apps/web-admin)
#   apps/api          → node-backend
#   apps/web-admin    → vite
#   shell loader      → mise
# wrote splashdown.toml + splashdown.local.toml + mise.toml + post-checkout hook
# updated AGENTS.md
#   PORT (changed)
#   WEB_DEV_PORT (changed)
#   -> splashdown.env: 2 vars (changed)
```

(Pass `--no-sync` to scaffold the files without reserving ports.)

Splashdown validates the complete recipe before reserving anything or changing generated
files. Unknown sections or fields, invalid resource writers, bad template references, and
incompatible target fields are hard errors with the exact config path to fix.

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
is shared by its linked worktrees; another clone starts untrusted and its hook writes nothing.
Future `git worktree add` operations provision the checkout and run bootstrap once, while ordinary
branch switches only sync resources. Trust covers code in future refs too, including scripts called
by an unchanged command. `splash init` grants only automatic sync trust, never bootstrap trust.
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

When a new iOS (or Android system image) lands, recreate the `latest` sims in place and clear out the cruft Xcode/`avdmanager` leave behind:

```sh
splash target refresh                 # destroy + recreate stale 'latest' sims (newer iOS landed)
splash target prune ios               # delete every sim splashdown did NOT create (the Xcode template pile)
splash gc                             # drop registry entries for checkouts you've since deleted
```

Variants pinned to a fixed version (`ios = "17.0"`) are never touched by `refresh` — they're deliberate version coverage. See [Running and managing devices](https://splashdown.dev/devices/) for the full lifecycle.

| Target | macOS | Linux |
| --- | --- | --- |
| iOS simulator/device | Xcode required | Unsupported; explicit commands return an actionable error |
| Android emulator/device | Android SDK required | Android SDK required |
| Ports, environment, and config | Supported | Supported |

Unscoped fleet commands (`target refresh`, `target prune`, `gc`, and status inspection) warn once
and continue when one platform is unavailable. Explicit iOS commands, including `target refresh
ios`, return exit 1 with the missing macOS/Xcode requirement and no traceback.

## Documentation

Full guides and reference live at **[splashdown.dev](https://splashdown.dev)**:

- [How it works](https://splashdown.dev/how-it-works/) — the git-hook + env-loader glue.
- [The recipe: `splashdown.toml`](https://splashdown.dev/recipe/) — apps, resources, mobile targets.
- [Running and managing devices](https://splashdown.dev/devices/) — sims, emulators, physical devices.
- [Framework wiring (`splash doctor`)](https://splashdown.dev/framework-wiring/) — patch configs that hardcode the port.
- [Monorepos](https://splashdown.dev/monorepos/) — multi-app workspaces, worked end to end.
- [CLI reference](https://splashdown.dev/cli/) — every `splash` subcommand.

## Development

```sh
just test                       # run pytest
just build                      # sdist + wheel
just install-local              # install local source as `splash` via uv
just refresh-local              # reinstall after changes
just reset-local                # uninstall the local `splash`
just tag-release-patch          # bump patch, commit, tag, push (triggers release.yml)
```

See `Justfile` and `.github/workflows/release.yml` for the release flow. Tagging publishes a GitHub release and auto-updates the `Formula/splashdown.rb` in `nielsmadan/homebrew-tap`. See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup and commit conventions.
