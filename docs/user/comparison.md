---
title: How splashdown compares
description: Compare splashdown with worktree tools, reverse proxies, and adjacent development utilities.
---

# How splashdown compares

Git worktrees isolate your *code*, but not your *runtime*. Two checkouts of the same project still fight over the same dev port, the same simulator, the same env. Several tools address parts of that gap, mostly aimed at running parallel AI-coding-agent worktrees. This page is an honest look at where splashdown sits among them.

No single tool does everything splashdown does, and splashdown does not do everything the others do. The table and the sections below should help you pick the right one, or combine a few.

## At a glance

| Tool | Per-worktree ports | Coordinated across all projects | Env vars | Mobile sims / emulators | How it triggers |
| --- | --- | --- | --- | --- | --- |
| **splashdown** | Free port, live-probed | Yes, machine-wide registry | Yes, to `splashdown.env` (mise / direnv / devbox) or a `.env` | Yes, iOS + Android, per checkout | `post-checkout` git hook, automatic |
| **Galactic** | Proxy hostnames, not ports | Routed, not allocated | Exports `HOST`/`PORT` on `cd` | No | On `cd`, macOS GUI |
| **Worktrunk** | `hash_port` filter (you wire it) | No, deterministic hash | Via hooks and per-branch vars | No | Worktree hooks |
| **VibeChard** | No (build-cache isolation) | No | No | Yes, but Apple only, clones a sim per task | Manual (`vch`) |

## The closest tools

**[Galactic](https://github.com/idolaman/galactic)** takes a different philosophy: instead of allocating ports it runs a local reverse proxy on `localhost:1355` and gives each branch a stable hostname like `api.feature-x.project.localhost:1355`, so branches never collide on a port at all. A managed zsh hook exports `HOST`/`PORT` when you `cd` into a service. It is a macOS-only Electron GUI under the AGPL, and does not handle mobile devices. Choose Galactic if you prefer stable hostnames over port numbers and want a desktop command center.

**[Worktrunk](https://worktrunk.dev)** is primarily a worktree manager (a polished three-command UX, hooks, LLM commit messages, per-branch variables). Its `hash_port` template filter gives each worktree a deterministic port, which you wire into your own hook. It does not coordinate ports through a registry or probe for conflicts, and does no env delivery or mobile out of the box. Choose Worktrunk for the worktree workflow itself, and script per-worktree ports on top.

**[VibeChard](https://github.com/Maples7/VibeChard)** is the one tool that also isolates simulators per worktree, and the closest thing to splashdown on the mobile axis. It targets parallel Apple development with AI agents: `vch` clones a simulator per task (`xcrun simctl clone`, bound via `SIMCTL_CHILD_SIMULATOR_UDID`) and isolates the Xcode caches that otherwise thrash when several agents build the same project at once (DerivedData, ModuleCache, SwiftPM caches, xcresult). Its focus is build and test isolation, not dev runtime: it does no ports, no env vars, is Apple-only (no Android), and is invoked explicitly rather than on checkout. splashdown overlaps on the per-checkout simulator idea but covers Android too, ties devices to coordinated ports and env, and provisions automatically. If your pain is specifically parallel Xcode builds colliding on caches and simulators, VibeChard is purpose-built for that.

## What splashdown complements, not competes with

Several categories sit next to splashdown rather than against it. Splashdown often sits on top of them.

- **Env loaders (mise, direnv, devbox).** These load values into your shell when you enter a directory. They do not decide collision-free values or know about worktrees. splashdown allocates the values and writes `splashdown.env`, which your loader then sources. See [How it works](how-it-works.md).
- **Free-port libraries (get-port, portfinder, detect-port).** These probe for a free port in-process, at runtime, with no persistence. Two worktrees each calling one can still race, and nothing remembers which checkout owns which port. splashdown is the ahead-of-time, persisted, cross-process version of the same primitive.
- **Reproducible-env tools (Nix devShells, devenv.sh, Flox, devcontainers).** These make an environment the *same* across machines, which is the opposite axis from splashdown's *per-checkout uniqueness*. They do not coordinate host ports across concurrent worktrees.
- **Worktree managers and local proxies (Conductor, Vibe Kanban, gwq, localias, Caddy).** Worktree managers create and manage the checkouts, the layer beneath splashdown's provisioning. Local proxies route hostnames to ports you still assign yourself. splashdown fills the runtime-provisioning gap that most worktree managers explicitly leave open.
- **Mobile tooling (fastlane, `simctl`, `avdmanager`, idb, maestro).** These are the device primitives splashdown wraps. They pick or drive a device for a run, but do not tie one to a checkout (VibeChard, above, is the exception). A related small tool, [SimTag](https://digitalbunker.dev/simtag-context-for-your-ios-simulators/), overlays the git branch on each iOS Simulator window so parallel builds are easy to tell apart. That is pure context, not isolation, and pairs fine with splashdown.

## Where splashdown is different

Taking the survey honestly, splashdown's distinct ground is the combination, not any single trick:

- **It fires on the git checkout itself.** A managed `post-checkout` hook provisions the checkout on every branch switch or `git worktree add`, with no wrapper command, `cd` hook, or GUI click. The other tools are invoked explicitly or on `cd`.
- **It delivers through your existing env loader.** Values land in `splashdown.env` and mise / direnv / devbox source them, so every process in the checkout sees them. You can also route a value straight into an app `.env`.
- **It isolates mobile devices per checkout, on both platforms.** Per-checkout iOS simulators and Android emulators, auto-recreated when a newer OS lands, plus physical-device selection. VibeChard also isolates simulators per worktree, but it is Apple-only and build/test-focused. splashdown is the only tool that covers both platforms and ties devices to the same coordinated ports and env.

And the honest flip side, where an alternative may fit better: splashdown does not run a database per workspace, does not offer stable proxy hostnames (see Galactic), is not itself a worktree manager (pair it with one, or with plain `git worktree`), and does not isolate Xcode build caches the way VibeChard does.
