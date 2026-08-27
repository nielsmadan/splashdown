# Bootstrap trust and lifecycle state

`bootstrap.py` owns Git identity, clone trust, per-worktree completion, and the locks that make
bootstrap once-only. Shell execution remains in `provisioning.py`; CLI orchestration and the
post-checkout transaction live in `commands.py`.

## State model

`git rev-parse --path-format=absolute --git-dir --git-common-dir` produces two identities:

- common Git directory: clone-wide `trust-v1.json` and `trust.lock`;
- private Git directory: worktree-local `bootstrap-v1.json` and `lifecycle.lock`.

State lives in a Splashdown-owned `splashdown/` directory with mode `0700`; state and lock files
use `0600`. Trust JSON contains a version plus separate `sync` and `bootstrap` booleans. Completion
JSON contains a version and one boolean. Both are written with a sibling temporary file, `fsync`,
and `os.replace`. Corrupt or unknown trust state fails closed for both capabilities. Corrupt
completion blocks automatic execution and requires explicit `splash bootstrap --rerun`.

`record_trust(..., bootstrap=False)` is used by init to grant sync-only trust. `splash trust`
always grants sync and also grants bootstrap when the current recipe declares it. A later sync-only
grant does not downgrade existing bootstrap trust. Revocation atomically writes both booleans false
instead of deleting state, so removing `[bootstrap]` cannot turn an explicit untrust into missing
state with ambiguous meaning.

Trust is deliberately not bound to the text of `[bootstrap]`: an unchanged command can invoke
arbitrarily changed branch code. The CLI warning describes the real boundary—every future ref in
that clone—not a weaker command digest.

## Transactions and lock order

Sync, direct bootstrap, the hook path, and deinit take the private lifecycle lock. Provisioning then
uses one validated `Recipe` snapshot for resource resolution, writers, and any commands. The hook
does not construct a Registry or provision until shared clone trust has confirmed sync authority.
Bootstrap takes the common trust lock in shared mode immediately before authorization and holds it
through command completion. `untrust` takes only the exclusive trust lock, so linked worktrees may
bootstrap concurrently but revocation waits for running commands and excludes new ones.

Recipe commands receive `SPLASHDOWN_LIFECYCLE_ACTIVE=1`. Every Splashdown lifecycle command that
could acquire the private or shared lock checks it first. Nested sync, bootstrap, deinit, trust,
untrust, and hook handling therefore fail instead of waiting on a non-reentrant lock.

Completion means at least one full command sequence succeeded. First-run failure leaves no marker;
failed `--rerun` leaves a prior marker untouched. Recipe changes never invalidate the marker.

## Linked-worktree device allocation

`[project.worktree] claim_device = "ios" | "android" | "any"` is a committed machine-local
allocation policy, not a project command. `recipe.py` validates `project.worktree` as a strict table
whose only field is `claim_device`.

The post-checkout handler evaluates the policy only when `is_worktree_creation` confirms a genuine
linked worktree: the checkout has a private Git directory distinct from the common directory,
Git's checkout flag is `1`, the old object ID is all zeroes, and the new object ID is a nonzero
valid ID of the same width. Primary-checkout initialization, ordinary branch switches, file
checkouts, and other post-checkout events do not allocate a phone.

The transaction order in `commands.py` is:

1. provision and render the checkout's resources;
2. run a trusted incomplete bootstrap, then persist its completion marker;
3. take the checkout operation lock and call the generic physical allocator;
4. print the selected target or an actionable manual retry.

An already-complete bootstrap skips step 2 but still permits step 3 on the qualifying creation
event. A bootstrap failure prevents allocation. The allocation uses one discovery snapshot with a
five-second total budget and claims at most one configured target. No connected/free match,
unavailable platform tooling, and discovery timeout print
`splash target claim --available PLATFORM` and remain non-fatal. The generated outer hook absorbs
handler failures so Git never turns a completed worktree creation into a nonzero result.

The lifecycle lock remains outermost for the hook, followed by shared clone trust. Provisioning
and claim allocation each take and release the checkout operation lock within that boundary. The
claims-file lock is acquired only inside the allocation transaction after discovery; notice locks
are not involved in automatic allocation.

## Hook path

The generated native/Husky hook and Lefthook job resolve `splash` once, normalize a relative PATH
result, and reject an executable located inside the checkout. They pass all three post-checkout
arguments to one hidden `splash hook post-checkout` invocation. There is no support probe or
fallback invocation. The outer manager command absorbs failure only after the internal command has
printed an actionable retry.

The event handler is dispatched before CLI Registry construction, so an untrusted hook cannot
touch registry state or output writers. Native hooks live under the common Git hooks directory and
can be upgraded locally. Husky and Lefthook files are tracked, so `splash trust` only
verifies/activates their current integration; `splash init` and `splash doctor --fix` own exact
legacy migration. Readiness uses one exact parser shared by trust activation and doctor, including
executable checks for native and Husky hooks and exact argument forwarding for all managers.
