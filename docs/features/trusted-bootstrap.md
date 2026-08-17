# Trusted worktree bootstrap

Up: [feature overview](overview.md) · Down:
[`bootstrap.py`](../tech/bootstrap.md), [`commands.py` / hooks](../tech/cli-and-commands.md),
[`provisioning.py`](../tech/provisioning.md)

## Current behavior

A strict top-level `[bootstrap]` accepts only `run`, with the same non-empty string-or-array shape
as `[setup.*]`. The distinction is intentional: setups are explicit named actions, while bootstrap
may execute automatically after clone-local authorization.

`splash trust` requires Git and a valid recipe, renders any bootstrap commands with control
characters escaped, warns that authorization covers future refs and inherited environment,
activates only local hook state, then stores trust under Git's common directory. It always grants
hook-driven sync and grants bootstrap execution only when the current recipe has `[bootstrap]`.
Trusting a recipe without that section cannot authorize one added by a future ref. The command
never executes bootstrap or edits tracked Husky/Lefthook files. `splash untrust` is
recipe-independent and revokes both clone-wide capabilities by writing an explicit untrusted
state. Removing `[bootstrap]` cannot erase revocation.

Every init path records sync-only trust after it installs the generated hook. That preserves the
automatic init workflow without granting command execution to a `[bootstrap]` added later.

`splash bootstrap` requires trust before provisioning. It resolves resources, writes outputs, and
runs commands sequentially from the checkout root. Successful completion is recorded under the
private Git directory, so linked worktrees share trust but complete independently. `--rerun`
ignores an existing success marker; a failed rerun leaves that marker intact.

The managed post-checkout entry first checks clone sync trust. Without it, the handler creates no
registry and writes no output. With sync trust, it provisions the checkout. Bootstrap is
additionally eligible only when bootstrap trust is set and Git reports checkout flag `1`, an
all-zero old object id, and a private Git directory different from the common directory. This
identifies linked-worktree creation without firing on branch or file checkout. A malformed event or
primary clone checkout can still sync but cannot bootstrap. A sync-only tracked hook needs repair
or manual bootstrap because it does not forward the event.

## Failure contract

- Trust and completion state are versioned, owner-only, atomically replaced, and fail closed when
  malformed or from an unknown version.
- One private-checkout lifecycle lock covers recipe load, provision, writers, commands, and
  completion. A shared clone trust lock is held during execution; untrust takes it exclusively.
- Commands are fail-fast and non-transactional. Earlier external effects remain and retry begins at
  command one.
- Hook failures print the direct retry command but are absorbed by native, Husky, and Lefthook
  wrappers so a completed `git worktree add` is not reported as failed.
- Nested lifecycle commands fail before lock acquisition, so a bootstrap command cannot deadlock
  by invoking sync, bootstrap, deinit, trust, untrust, or the hidden hook handler.
- `deinit` removes only the current checkout's completion. Trust remains until explicit untrust.

## Compatibility

The schema requires Splashdown 0.17.0. Generated hooks invoke the internal event command once and
forward all three Git arguments. Exact legacy Splashdown hooks migrate; modified or user-owned hooks
remain untouched. Tracked hook-manager files are upgraded only by author-facing init/doctor repair,
never by a teammate's local trust decision. A hook resolves `splash` once, rejects an executable
inside the checkout, and absorbs the handler's failure after it prints a retry command.
