# Changelog

All notable user-facing changes to splashdown. While the project is on `0.x` it follows
[Semantic Versioning](https://semver.org) loosely: breaking changes may land in a minor
release and are called out under **Breaking Changes**.

## [0.14.0] - 2026-07-30

### Features

- Recipe validation

### Bug Fixes

- Default port ranges for frameworks
- Handle nested envfile paths safely

## [0.13.0] - 2026-07-26

### Features

- Global device / targets

### Bug Fixes

- Auto-trust mise/direnv config so it loads without a manual step
- Make target removal safe
- Restrict manual environment overrides
- Refresh derived template resources
- Fail fast on setup errors

## [0.12.0] - 2026-07-24

### Features

- Add default targets for mobile projects
- Fuzzy target matching
- Add splash deinit command
- Emit Metro port for Expo projects
- Skip non-framework workspace members in scan
- Add monorepo-ambiguity detectors
- Defer splash init on ambiguous monorepos
- Add device targets for mobile projects
- Add custom mobile run command
- Improve auto-complete for non-homebrew installs

### Bug Fixes

- Keep pinned port when in use
- Reject envfile writer paths outside the checkout
- Derive template refs from AST
- Address product-review UX findings
- Strip envfile/envrc writer keys on deinit
- Prevent duplicate registry port rows and restrict file permissions
- Harden template engine and Android launch args against untrusted recipes
- Unify device staleness check, fixing emulator orphan and redundant lookups
- Don't clobber husky hooks, reject undeclared env-set keys, fix physical hint

## [0.11.0] - 2026-06-22

### Features

- Add default emulator to react-native scaffold
- Route env values without imposing a loader
- Various small fixes and improvements
- Sync on init by default

## [0.10.2] - 2026-06-13

### Bug Fixes

- Completion setup

## [0.10.1] - 2026-06-13

### Features

- Add shell completion for the splash CLI

### Bug Fixes

- Harden template/registry inputs, fix physical-device status, drop test seam
- Stop release workflow from overwriting resource sha256
- Align version information

## [0.10.0] - 2026-06-12

### Features

- Scope-shaped flags become positionals (init preset, device prune platform)
- Device refresh recreates stale/missing sims; status --check flags them
- Better status messages when being called on by git hook
- Improve react native integration handling
- Add port for RN
- Gc prunes registry entries dropped from a recipe
- Manage physical devices
- Rename device command to target
- Revamp CLI to sync/target/env command structure

### Bug Fixes

- Legacy splash init NAME honors --loader override (plus test gaps)
- Cmd_doctor surfaces Profile wiring checks (vite, springboot)
- Preset scaffolds emit current recipe shape

## [0.9.0] - 2026-06-03

### Features

- Rename device types to simulator/emulator + progress display
- Various API extensions & improvements
- Scanner inspects workspace, apps, and shell-env loader
- Profile abstraction + ViteProfile
- Loader abstraction + MiseLoader
- Scanner-driven cmd_init + refresh-inventory command
- NodeBackendProfile detects hono/express/fastify/koa/hapi/nest
- NextJsProfile detects next.config or `next` dep
- DjangoProfile detects manage.py with django import
- FastApiProfile detects fastapi in pyproject or requirements
- SpringBootProfile + application.properties check
- DirenvLoader + dotenv splashdown.env in .envrc
- DevboxLoader + init_hook in devbox.json
- Bridge mobile Presets into the Profile registry
- Splash status --all and --check
- Splash status --all is now a compact table; --verbose for blocks
- Status --all table drops STATUS column when empty, renames to ISSUE
- Splash status takes a scope positional (local | all) instead of --all

## [0.8.0] - 2026-05-25

### Features

- Add LocalConfig loader for splashdown.local.toml
- Presets drop [devices.*]; init writes local skeleton
- Recipe rejects [devices.*] — committed file is schema only
- Splashdown.env writer replaces mise.local.toml writer
- Provision drops a local-config skeleton in new checkouts
- Splash device add / remove edit splashdown.local.toml
- Splash init wires gitignore, mise.toml directive, git hook
- Hook-manager-aware _ensure_post_checkout_hook (lefthook/husky/clean)
- WiringCheck infrastructure + cmd_doctor + 'splash doctor' CLI
- Rn-hook check — verifies post-checkout fires splash
- Rn-metro-config check — metro.config.js consumes RCT_METRO_PORT
- Rn-pkg-port check — strip --port from RN scripts in package.json
- Rn-xcode-env check + end-to-end test of all four RN checks
- Rn-xcode-env treats any splashdown.env-referencing wiring as ok
- Drop TEST_DB from rn preset — most RN apps don't have local DBs
- Device variant catalog + auto-upgrade + sim cleanup

### Bug Fixes

- --cwd/--format no longer clobbered when set before a subcommand

[0.14.0]: https://github.com/nielsmadan/splashdown/compare/v0.13.0..v0.14.0
[0.13.0]: https://github.com/nielsmadan/splashdown/compare/v0.12.0..v0.13.0
[0.12.0]: https://github.com/nielsmadan/splashdown/compare/v0.11.0..v0.12.0
[0.11.0]: https://github.com/nielsmadan/splashdown/compare/v0.10.2..v0.11.0
[0.10.2]: https://github.com/nielsmadan/splashdown/compare/v0.10.1..v0.10.2
[0.10.1]: https://github.com/nielsmadan/splashdown/compare/v0.10.0..v0.10.1
[0.10.0]: https://github.com/nielsmadan/splashdown/compare/v0.9.0..v0.10.0
[0.9.0]: https://github.com/nielsmadan/splashdown/compare/v0.8.0..v0.9.0
[0.8.0]: https://github.com/nielsmadan/splashdown/tree/v0.8.0

