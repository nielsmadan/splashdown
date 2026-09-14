---
title: Getting started
description: Install splashdown, initialize a project, and provision collision-free checkout resources.
---

# Getting started

Splashdown pins per-checkout system resources (dev ports, env vars, and mobile simulators or emulators) and coordinates them across every git checkout on your machine, so two worktrees of the same project never collide. This page walks through a first setup for a web or backend project. For a mobile app, see [Getting started with mobile](getting-started-mobile.md).

## Prerequisites

- Git.
- Recommended: a shell env loader (mise, direnv, or devbox). Splashdown writes a `splashdown.env` file and wires your loader to source it automatically when you `cd` into the project. mise is the smoothest option. A loader is not strictly required. Without one you can source the file yourself (`set -a; . ./splashdown.env; set +a`), or send the values straight into an app `.env` file (see [Writing straight to a .env file](#writing-straight-to-a-env-file)).

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

Init creates configuration in the current directory. Use `splash --cwd PATH init` to choose
another directory. A subdirectory of a Git worktree can be an independent Splashdown project.
An existing recipe requires `--overwrite` to replace it.
Nested init leaves the worktree-root post-checkout hook untouched because Git invokes that hook
from the root. Init prints the `splash --cwd PATH sync` command to run manually after checkout.

Splashdown scans the filesystem, detects your workspace layout and framework, and does four things:

1. Writes `splashdown.toml`, the committed recipe describing this project's per-checkout resources.
2. Writes `splashdown.local.toml`, a gitignored per-checkout file (empty to start).
3. Wires your env loader (`mise.toml`, `.envrc`, or `devbox.json`) to source `splashdown.env`, and writes the project's own post-checkout hook configuration for Husky, Lefthook, pre-commit, prek, or simple-git-hooks. Overcommit and a custom `core.hooksPath` are left untouched with manual forwarding instructions.
4. Adds managed framework and port guidance to an existing root `AGENTS.md` or independent `CLAUDE.md`, unless another tool generates that file.

Init writes configuration only. It allocates nothing, records no trust, and installs nothing on
your machine, so you can read and edit the generated recipe before anything else happens.

Sample output for a small backend:

```
scanning project…
  detected: single (main)
  .            → node-backend
  shell loader → mise (detected mise.toml)
wrote splashdown.toml
wrote splashdown.local.toml (skeleton)
updated .gitignore (+splashdown.env, splashdown.local.toml)
updated mise.toml (+_.file = "splashdown.env")
note: the local post-checkout hook is installed by `splash trust`
changed: splashdown.toml, splashdown.local.toml, mise.toml
configuration written; nothing is allocated or active yet
next: run `splash trust` to activate automatic post-checkout handling
      run `splash` to allocate values and write splashdown.env
```

!!! note "Commit the recipe"
    Commit `splashdown.toml` and the loader change. Do not commit `splashdown.local.toml` or `splashdown.env` (both are gitignored by init). Committing the recipe is what lets new worktrees inherit it.

## Activate this checkout

```sh
splash trust
splash
```

`splash trust` authorizes automatic handling for this clone. It installs the local post-checkout
hook, runs your hook manager's own install command when one of them owns the event, and runs
`mise trust` or `direnv allow` when the loader file holds splashdown's integration and nothing
else. A loader
file that also holds settings of your own is left for you to approve with the loader's own
command.

Bare `splash` is a sync. It allocates this checkout's resources and writes `splashdown.env`:

```
  PORT (changed)
  -> splashdown.env: 1 vars (changed)
```

## What got created

| File | Committed | Purpose |
| --- | --- | --- |
| `splashdown.toml` | Yes | The recipe: resources, apps, and (for mobile) device targets |
| `splashdown.local.toml` | No | Per-checkout additions (gitignored) |
| `splashdown.env` | No | Generated `KEY=VALUE` file: splashdown rewrites the keys your recipe declares and leaves anything else in it alone (gitignored) |
| loader config | Yes | Gains one line that sources `splashdown.env`, and is created if absent |
| `AGENTS.md` / `CLAUDE.md` | Yes | Existing files gain a sentinel-wrapped block telling coding agents which port variables, env destination, and launch commands to use |

Splashdown never creates an agent-instruction file. If both exist and `CLAUDE.md` imports
`@AGENTS.md`, only `AGENTS.md` keeps guidance, and any older complete block in `CLAUDE.md` is
removed. `init --overwrite` replaces or removes the managed block as detected frameworks change,
and `deinit` removes the block while leaving the rest of each Markdown file untouched. Symlinks,
non-regular files, and unpaired or duplicate sentinels are treated as user-owned: Splashdown
warns and leaves them unchanged.

The block names the env destination your recipe configures, so a project pointed at `.env` or
`config/dev.env` reads about that file rather than `splashdown.env`. It carries no allocated
values, which keeps it safe to commit. If the file is generated by another tool, Splashdown
prints the generator marker it found, prints the block itself, and asks you to copy it into
that file's source instead of editing output that would be overwritten.

See [How it works](how-it-works.md) for the full model.

## Verify it

Open a new shell in the project (so the loader re-reads its config), then check the value is present:

```sh
splash status         # resource keys and port state (values stay hidden)
splash --show-values status  # include values when you need to inspect them
echo $PORT            # e.g. 9081, loaded by your env loader
```

If `$PORT` is empty, your loader has not picked up `splashdown.env` yet. Run `splash doctor` to check the wiring, and confirm your loader is active in this directory.

## The payoff: worktrees get their own ports

Add a second checkout and the post-checkout hook provisions it automatically, with no manual editing:

```sh
git worktree add ../myapp.feature feature
cd ../myapp.feature
splash --show-values status  # PORT is 9082 here, not 9081
```

Both checkouts can run their dev servers at once without a port clash. The machine-wide registry guarantees it, even across unrelated repos.

If the project also declares trusted one-time setup under `[bootstrap]`, a trusted clone runs it
after provisioning a newly-created worktree. Fresh clones use `splash trust` followed by
`splash bootstrap`. See [Trusted worktree bootstrap](bootstrap.md).

mise and direnv treat the inherited loader file at the new path as untrusted. Splashdown does
not approve project-controlled files from a hook, so review the file and run `mise trust` or
`direnv allow` in the new worktree if your loader asks.

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

Splashdown validates the entire recipe before it allocates anything or updates generated files. Unknown sections or fields, mistyped values, invalid templates, and bad references stop the sync with a qualified error such as `[resources.PORT.range]`, so a late typo cannot leave a partially applied configuration.

## Writing straight to a .env file

If an app already reads its own `.env`, choose that file as the destination when you initialize:

```sh
splash init --loader none --env-file .env
```

That records `env_file = ".env"` under `[project]` and every resource without its own `writer`
lands there, so the app keeps its ordinary launch command. Pick a loader as well and the loader is
wired to read that same file, which is the option to take when your shell needs the values too.

Splashdown manages only the keys your recipe declares. Other lines, comments, and blank lines stay
where they are, and an existing value for a declared key is replaced on the next sync. A key the
file assigns twice, or assigns in a shape splashdown does not rewrite such as `PORT: 3000`, is
reported as an error instead of being joined by a second definition.

The path is relative to the checkout root, for example `--env-file apps/web/.env` in a monorepo.
Missing parent directories are created. Absolute paths and paths containing `..` are rejected
before init writes anything.

To send one resource somewhere other than the default, give it a `writer` of its own:

```toml
[resources.LEGACY_PORT]
type   = "port"
range  = [9999, 10100]
writer = "envfile=path/to/legacy/.env"    # writes `LEGACY_PORT=9999` there
```

That resource is written only to its own destination and is not copied into the default file, so a
loader will not see it. Prefer the default destination when both an app and your shell need the
value.

## Keeping wiring healthy

- `splash doctor` checks that your loader and the git hook are wired, and that no config file hardcodes a port over the env var. `splash doctor --fix` re-applies safe fixes.
- `splash sync` forces a re-provision. `splash gc` drops registry entries for checkouts you have since deleted.

## Next steps

- [The recipe](recipe.md): resources, templates, and the full schema.
- [Per-checkout overrides](overrides.md): add variants in `splashdown.local.toml`, and machine-wide devices.
- [Monorepos](monorepos.md): multi-app workspaces, worked end to end.
- [Getting started with mobile](getting-started-mobile.md): simulators, emulators, and physical devices.
