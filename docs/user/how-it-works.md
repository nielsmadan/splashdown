# How it works

Splashdown is the glue between git and your env loader (mise, direnv, devbox). It installs a `post-checkout` git hook, coexisting with lefthook/husky if you already use a hook manager, that fires `splash` whenever you check out a branch or add a worktree, so each checkout's free ports (and other resources) are allocated and handed to your loader automatically.

Run `splash init` once in your project. Splashdown walks the filesystem, identifies your apps and their frameworks, and writes a recipe (`splashdown.toml`) declaring per-checkout resources (ports, db urls, UUIDs, sim/emulator variants). On every `git checkout` or `git worktree add`, a post-checkout hook fires `splash`, which allocates concrete values into a gitignored `splashdown.env`. Your shell-env loader (mise / direnv / devbox) sources that file automatically, so every process in the checkout sees the right `PORT`, `DATABASE_URL`, etc.

Four files end up in the project:

| File | Committed? | Purpose |
|------|-----------|---------|
| `splashdown.toml` | Yes | Recipe: `[project]`, `[apps.*]`, `[resources.*]`, and any team-shared `[targets.*]` variants |
| `splashdown.local.toml` | **No** (gitignored) | Per-checkout *additional* `[targets.*]` variants on top of the recipe's |
| `splashdown.env` | **No** (gitignored) | Generated `KEY=VALUE` env file. Splashdown owns it (overwritten wholesale on every run, don't hand-edit) |
| `mise.toml` (or `.envrc` / `devbox.json`) | Yes | Your shell-env loader's config, gains a line that sources `splashdown.env` |

The registry at `~/.local/state/splashdown/` is machine-wide, so when two checkouts both want port 8081 splashdown gives one of them 8082, even across unrelated repos.

**Provision output** is change-aware: the first run (or any run that allocates a new port or rewrites a file) prints only the changed vars and writer lines. A no-op run (`git pull --rebase` on a checkout already provisioned) collapses to one line:

```
splashdown: up to date (3 vars, 2 files)
```

This is what you see through lefthook or other hook managers when nothing actually changed.
