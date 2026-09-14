---
title: Profiles and loaders
description: Understand framework profiles, shell environment loaders, and resource writer behavior.
---

# Profiles and loaders

Two extension points decide what `splash init` produces.

A **Profile** is the per-framework integration rules: what resources this kind of app wants, and what config files (if any) need patching so the values reach the running process. The Vite Profile, for example, emits `WEB_DEV_PORT` (and `API_DEV_PORT` if it sees a `server.proxy` block) and rewrites `vite.config.{ts,js}` to read `process.env.X` instead of `loadEnv()`. The Spring Boot Profile emits `PORT` and checks that `application.properties` uses the `server.port=${PORT:8080}` placeholder. React Native and the native iOS/Android profiles add mobile wiring checks. Expo and Flutter currently rely on their launchers and declare no doctor checks.

A **Loader** is the per-shell-env-tool wiring: how the env destination gets sourced into your shell when you `cd` into the project. Splashdown supports three: `mise` (sets `_.file` in `mise.toml`), `direnv` (appends `dotenv_if_exists` between sentinel markers in `.envrc`), `devbox` (adds an `init_hook` entry in `devbox.json`). All three are idempotent and reversible. Each one names whatever `[project] env_file` selects, which is `splashdown.env` unless you chose otherwise with `splash init --env-file PATH`.

mise and direnv only load a config once you trust or allow it. `splash trust` runs `mise trust` or `direnv allow`, and only when the loader file contains Splashdown's integration and nothing else. It never approves a config that also holds your own settings, and `init`, `sync`, and the post-checkout hook never approve anything. Review and approve those files with the loader's normal command. This matters in a new worktree because the inherited config has a new path and may need a separate approval.

**How `splash init` picks one.** It looks for loader configuration in the project directory: `mise.toml` or `.mise.toml`, `.envrc` or `.envrc.local`, `devbox.json`. Both mise files count as one candidate, not two. The sole loader configured there is the one splashdown wires, and init prints which file decided it. Having the tool installed is never enough on its own, because adopting an integration the project has not chosen is not init's call to make.

When several loaders are configured, init stops before changing anything and asks you to pick one with `--loader`. When none is configured, splashdown picks `none`. Finding an existing `.env` is never a reason to route values there on its own, because delivery is your choice to make. Init keeps generating `splashdown.env` and prints how to source it or which loader to install.

Force any choice with `splash init --loader=mise|direnv|devbox|none`. An explicit value wins over anything detected, and `--loader none` is the explicit opt-out from auto-wiring.

**The destination is a separate choice.** `--loader` decides how the environment is loaded and
`--env-file` decides which file receives the values. Supply both and the selected loader is wired
to read that file, so `splash init --loader mise --env-file .env` records direct dotenv delivery
and points mise at `.env`. With `--loader none --env-file .env` the destination is configured and
no loader file is touched, which is how an app that already reads `.env` keeps its ordinary launch
command. Loading a shared `.env` also exposes its other values through the loader.

**Existing wiring is reused, not duplicated.** If the loader config already loads the selected destination, init leaves the file exactly as it is and reports it as already configured. That covers a mise `_.file` naming the file, in either the plain or the list form, a `dotenv` or `dotenv_if_exists` line you wrote yourself in `.envrc`, and a devbox init hook that exports the environment and sources the file. Simple quoting, tabs and a leading `./` all count as the same reference. A filename inside a comment does not count, and neither does an indented line, which splashdown reads as sitting inside a function body or a conditional rather than running when you enter the directory. Splashdown reads these files, it never runs them, so only a plain unindented statement counts. Init also edits the config file you already have instead of adding a second one, so a project with `.mise.toml` never gets a `mise.toml` next to it.

Where the file exists but lacks the integration, init adds only the missing directive and keeps everything else. A mise config that already sets `_.file` to another file gets the selected destination added alongside it rather than in place of it. If the file cannot be extended safely, init stops with an explanation and writes nothing. `--overwrite` replaces the recipe only, and never overrides that.

Because init only ever adds its own directive, teardown only ever removes its own. `splash deinit` leaves a directive you wrote yourself untouched.

Splashdown marks its own mise directive with a trailing `splashdown-managed` comment, which is how it tells the two apart. A `_.file` line written by a Splashdown build from before that marker existed looks exactly like one you wrote, so Splashdown treats it as yours and leaves it forever. When init reuses an unmarked directive that names the selected destination, it says so. If Splashdown wrote that line, remove that entry from it and run `splash init` again to get it back with the marker. Delete the whole line when that entry is the only thing it names.

**Override at any layer.** Edit `[project] workspace`, `[project] loader`, `[apps.<name>] profile`, or any `[resources.*]` table. Values must name a supported built-in workspace, loader, or profile. Unknown names and fields are errors. Splashdown picks up a valid change on the next sync. To regenerate the whole recipe from the current filesystem, use `splash init --overwrite`, which replaces manual edits.

**Multi-instance collisions** make scanner-driven init defer automatic resource generation. Two Vite apps both want `WEB_DEV_PORT`, so splashdown writes the detected app structure without resources and points you to the monorepo guide to choose explicit names and ranges.

**Unknown framework.** An app whose framework splashdown doesn't recognize gets `profile = "unknown"`: no resources allocated for it, no wiring attempted. The rest of the project still works. To add support, contribute a Profile upstream.

## The `writer` field (power-user escape hatch)

Resources route to the `[project] env_file` destination by default, which is `splashdown.env` until you pick another with `splash init --env-file PATH`. That's what mise/direnv/devbox load. For consumers that can't read `process.env` (legacy build systems, vendor tooling, frameworks splashdown doesn't have a Profile for yet), set `writer` on the resource:

```toml
[resources.LEGACY_PORT]
type   = "port"
range  = [9999, 10100]
writer = "envfile=path/to/legacy/.env"
```

Available writers: `splashdown-env` (default, meaning the `[project] env_file` destination),
`envfile=PATH` (that .env-format file), `envrc` (writes `.envrc.local`), `stdout` (explicitly
discloses `KEY=value` in text output or the JSON `stdout` field), and `none` (registry-only, no
file output). Every file writer preserves lines the recipe does not declare. A resource with its
own `writer` is not also copied into the default destination.

An `envfile=` path and `[project] env_file` must be non-empty, relative to the checkout root, and stay inside the checkout. Absolute paths, `..` traversal, bare `envfile`, and other writer spellings are rejected during recipe validation, before any resource is allocated. `--env-file` is checked the same way, before init writes anything.

Most of the time the framework Profile handles routing implicitly and `writer` stays unset. Use it when no Profile covers your consumer yet.
