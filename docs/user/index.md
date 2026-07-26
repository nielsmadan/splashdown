<div align="center">
  <img src="assets/logo.svg" alt="splashdown" width="220">
  <h1>splashdown</h1>
</div>

**Per-checkout or per-worktree simulators, emulators, and dev ports.**

splashdown pins system resources (dev ports, env vars, iOS simulators, Android emulators) to each git checkout and coordinates them machine-wide, so concurrent worktrees of the same (or different) projects never collide.

<p align="center"><img src="assets/demo.gif" alt="splashdown demo: two git worktrees automatically get different, non-colliding ports" width="760"></p>

!!! tip "New here?"
    Start with [Getting started](getting-started.md) for a web or backend project, or
    [Getting started with mobile](getting-started-mobile.md) for a mobile app. The
    [README](https://github.com/nielsmadan/splashdown#readme) has the one-minute quick start.

## Getting started

- [Getting started](getting-started.md): install, `splash init`, and your first per-checkout ports.
- [Getting started with mobile](getting-started-mobile.md): simulators, emulators, and machine-wide physical devices.

## Guides

- [How it works](how-it-works.md): the git-hook + env-loader glue, and the four files splash manages.
- [How it compares](comparison.md): splashdown vs Galactic, Worktrunk, VibeChard, and adjacent tools.
- [The recipe: `splashdown.toml`](recipe.md): the committed config for apps, resources, and mobile targets.
- [Per-checkout overrides](overrides.md): add local target variants in `splashdown.local.toml`.
- [Settings](settings.md): behavior toggles and where they live.
- [Shell completion](shell-completion.md): bash/zsh tab-completion setup.
- [Running and managing devices](devices.md): `run`/`start`/`stop`, auto-upgrade, iOS/Android/physical.
- [Framework wiring (`splash doctor`)](framework-wiring.md): detect and patch configs that hardcode the port.
- [Profiles and loaders](profiles-and-loaders.md): how `splash init` decides what to scaffold.
- [Monorepos](monorepos.md): multi-app workspaces, worked end to end.

## Reference

- [CLI reference](cli.md): every `splash` subcommand at a glance.
- [Global port coordination](port-coordination.md): how the machine-wide registry avoids collisions.
- [CI integration](ci.md): the `splashdown.env` pattern for ephemeral runners.
