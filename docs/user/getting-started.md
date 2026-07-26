# Getting started

Splashdown pins per-checkout system resources (dev ports, env vars, and mobile simulators or emulators) and coordinates them across every git checkout on your machine, so two worktrees of the same project never collide. This page walks through a first setup for a web or backend project. For a mobile app, see [Getting started with mobile](getting-started-mobile.md).

## Prerequisites

- Git.
- Recommended: a shell env loader (mise, direnv, or devbox). Splashdown writes a `splashdown.env` file and wires your loader to source it automatically when you `cd` into the project. mise is the smoothest option. A loader is not strictly required. Without one you can source the file yourself (`set -a; . ./splashdown.env; set +a`), or route values straight into an app `.env` file (see [Writing straight to a .env file](#writing-straight-to-a-env-file)).

## Install

```sh
brew install nielsmadan/tap/splashdown
# or, managed by mise
mise use -g pipx:splashdown
```

This puts `splash` on your `PATH`. The machine-wide registry lives at `$XDG_STATE_HOME/splashdown/` (default `~/.local/state/splashdown/`) and is shared across every repo.

Tab-completion is optional but recommended:

```sh
# zsh (~/.zshrc)
eval "$(splash completion zsh)"
# bash (~/.bashrc)
eval "$(splash completion bash)"
```

More detail at [Shell completion](shell-completion.md).

## Initialize a project

Run this once at the repo root:

```sh
splash init
```

Splashdown scans the filesystem, detects your workspace layout and framework, and does four things:

1. Writes `splashdown.toml`, the committed recipe describing this project's per-checkout resources.
2. Writes `splashdown.local.toml`, a gitignored per-checkout file (empty to start).
3. Wires your env loader (`mise.toml`, `.envrc`, or `devbox.json`) to source `splashdown.env` when one is present, and installs a `post-checkout` git hook.
4. Allocates this checkout's resources and writes them to `splashdown.env`.

Sample output for a small backend:

```
scanning project…
  detected: single (main)
  .            → node-backend
  shell loader → mise
wrote splashdown.toml
updated mise.toml (+_.file = "splashdown.env")
wrote .githooks/post-checkout, set core.hooksPath
  PORT=9081
  -> splashdown.env: 1 vars (changed)
```

Pass `--no-sync` to scaffold the files without reserving ports yet.

!!! note "Commit the recipe"
    Commit `splashdown.toml` and the loader change. Do not commit `splashdown.local.toml` or `splashdown.env` (both are gitignored by init). Committing the recipe is what lets new worktrees inherit it.

## What got created

| File | Committed | Purpose |
| --- | --- | --- |
| `splashdown.toml` | Yes | The recipe: resources, apps, and (for mobile) device targets |
| `splashdown.local.toml` | No | Per-checkout additions (gitignored) |
| `splashdown.env` | No | Generated `KEY=VALUE` file, owned by splashdown (gitignored) |
| loader config | Yes | Gains one line that sources `splashdown.env` |

See [How it works](how-it-works.md) for the full model.

## Verify it

Open a new shell in the project (so the loader re-reads its config), then check the value is present:

```sh
splash status         # this checkout's resources and their values
echo $PORT            # e.g. 9081, loaded by your env loader
```

If `$PORT` is empty, your loader has not picked up `splashdown.env` yet. Run `splash doctor` to check the wiring, and confirm your loader is active in this directory.

## The payoff: worktrees get their own ports

Add a second checkout and the post-checkout hook provisions it automatically, with no manual editing:

```sh
git worktree add ../myapp.feature feature
cd ../myapp.feature
splash status         # PORT is 9082 here, not 9081
```

Both checkouts can run their dev servers at once without a port clash. The machine-wide registry guarantees it, even across unrelated repos.

## Editing the recipe

Open `splashdown.toml` to add or change resources. A port and a templated value, for example:

```toml
[resources.PORT]
type  = "port"
range = [9081, 9100]      # globally-coordinated lowest-free port in this range

[resources.DATABASE_URL]
type     = "template"
template = "postgres://localhost/myapp_{{ slug(branch) }}"
```

Re-run `splash` (or just switch branches) to apply. The full schema is in [The recipe](recipe.md).

## Writing straight to a .env file

If you would rather not use an env loader, or an app reads its own `.env` directly, point a resource at that file with a per-resource `writer`. The value lands in the named file instead of `splashdown.env`:

```toml
[resources.PORT]
type   = "port"
range  = [9081, 9100]
writer = "envfile=.env"    # writes `PORT=9081` into ./.env
```

Splashdown owns the lines it writes and updates them in place on each run. Use a path relative to the checkout root, for example `writer = "envfile=apps/web/.env"` in a monorepo. Values routed this way are not in `splashdown.env`, so a loader will not see them. Prefer the loader path when both an app and your shell need the value.

## Keeping wiring healthy

- `splash doctor` checks that your loader and the git hook are wired, and that no config file hardcodes a port over the env var. `splash doctor --fix` re-applies safe fixes.
- `splash sync` forces a re-provision. `splash gc` drops registry entries for checkouts you have since deleted.

## Next steps

- [The recipe](recipe.md): resources, templates, and the full schema.
- [Per-checkout overrides](overrides.md): add variants in `splashdown.local.toml`, and machine-wide devices.
- [Monorepos](monorepos.md): multi-app workspaces, worked end to end.
- [Getting started with mobile](getting-started-mobile.md): simulators, emulators, and physical devices.
