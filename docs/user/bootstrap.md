---
title: Trusted worktree bootstrap
description: Run shared setup commands once when a trusted Git worktree is created.
---

# Trusted worktree bootstrap

Use `[bootstrap]` for checkout setup that should be shared with the project but run only once per
worktree: installing dependencies, applying migrations, creating retry-safe seed data, starting
containers, or preparing a simulator.

```toml
[bootstrap]
run = [
  "pnpm install --frozen-lockfile",
  "python manage.py migrate",
  "python manage.py seed --if-empty",
]
```

`run` accepts one non-empty command or a non-empty array. Commands run sequentially from the
checkout root after splashdown has resolved resources and written their outputs. The resolved
values are present in the command environment.

After adding the section, run `splash doctor --fix` and commit any hook-manager update. Projects
initialized by Splashdown 0.17.0 or newer already have the required hook form.

## Trust a clone

Bootstrap commands are repository-controlled shell code, so Splashdown never runs them or writes
recipe-controlled output merely because you cloned a repository. Review `splashdown.toml`, then
authorize that clone:

```sh
splash trust
splash bootstrap
```

`splash trust` shows the commands and records authorization, but does not execute them. It grants
automatic resource sync and, when the current recipe has `[bootstrap]`, command execution. Trust is
stored inside the clone's Git administrative directory. Linked worktrees share it, while another
clone of the same repository starts untrusted. If you trust a recipe before it has `[bootstrap]`,
adding the section later requires another `splash trust` before commands can run.

`splash init` grants automatic sync trust for the recipe it just generated, but never grants
bootstrap trust. This keeps the normal init workflow automatic without authorizing shell commands
that may appear in a future ref. Once bootstrap trust has been granted, it remains until
`splash untrust`, even while the current ref has no `[bootstrap]` section.

!!! warning
    Trust covers current and future refs in that clone. A future branch can change both the
    `[bootstrap]` commands and any scripts or dependencies they call. Those commands run with your
    user permissions and inherit the environment of the Git or `splash bootstrap` process.

Revoke authorization without reading the current recipe:

```sh
splash untrust
```

Revocation waits for already-running automatic handling to finish, then disables both hook-driven
sync and bootstrap execution. The shared hook may remain installed, but it is inert until the clone
is trusted again. Explicit `splash sync` remains available.

## Worktree behavior

Once the clone is trusted for both capabilities, a normal `git worktree add` provisions the new
checkout and runs its bootstrap once. Branch switches and file checkouts still sync resources but
do not bootstrap.

```sh
git worktree add ../myapp.feature feature
# resources written, then [bootstrap] completed once in ../myapp.feature
```

The initial checkout performed by `git clone` happens before Splashdown can install a hook, which
is why fresh-clone onboarding uses `splash trust` followed by `splash bootstrap`. Git emits no
checkout hook for `git worktree add --no-checkout`, so that form also needs the manual command.

Tracked Husky and Lefthook configuration comes from the branch being created. An older branch may
contain a sync-only hook that cannot identify worktree creation. Run `splash doctor --fix` to
upgrade Splashdown-owned integration, or run `splash bootstrap` manually. A custom
`core.hooksPath` must invoke a trusted absolute `splash` executable and forward Git's three event
arguments. Splashdown does not take over that path.

## Retry and rerun

Splashdown writes completion only after every command succeeds. A failed first attempt can be
retried with:

```sh
splash bootstrap
```

If a later command fails, effects from earlier commands remain and the retry starts from command
one. Bootstrap commands should therefore be idempotent. There is no rollback or automatic
teardown.

Run a completed bootstrap again explicitly with:

```sh
splash bootstrap --rerun
```

A failed rerun preserves the earlier success marker, so ordinary bootstrap remains complete until
another explicit `--rerun`. Recipe edits do not invalidate completion automatically.

`splash deinit` clears completion for the current checkout but leaves clone trust and the shared
hook intact. Use `splash untrust` when you want to revoke clone-wide authorization.

!!! note "Version requirement"
    The top-level `[bootstrap]` section requires Splashdown 0.17.0 or newer. Earlier versions
    reject the section rather than ignoring repository-controlled behavior they do not understand.
