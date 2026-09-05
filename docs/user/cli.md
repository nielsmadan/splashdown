---
title: CLI reference
description: Reference for every splash command, option, target action, and environment operation.
---

# CLI reference

```
splash                              # sync this checkout explicitly
splash --version
splash [--cwd PATH] [--format text|json] [--show-values] …  # root options precede the command
splash sync [--force] [--setup N]   # pick free ports, resolve vars, write splashdown.env
splash status [local|all] [--check] [--verbose]
                                      # resources + targets + health/cleanup details
splash init [preset] [--rescan] [--no-sync] [--loader=…] [--overwrite] [--allow-nested]
                    [--electron-profile=isolated|shared] [--ios-scheme=NAME]
splash deinit                       # remove checkout-local state, keep shared hook and trust
splash trust                        # authorize automatic handling for this clone
splash untrust                      # revoke clone-wide automatic handling
splash bootstrap [--rerun]          # sync + run bootstrap once for this checkout
splash doctor [--fix] [--framework=…]

splash run     [type] [variant]     # boot target + build + launch
splash start   [type] [variant]     # boot target (no build/launch)
splash stop    [type] [variant]     # shut down
splash destroy [type] [variant] [--yes]
                                      # delete this checkout's target instance

splash target                       # list declared targets + live state
splash target add <type> <variant> [--model M] [--ios V] [--device D] [--image I]
                  [--name N] [--id ID] [--platform ios|android] [--global]
splash target remove <type> <variant> [--keep-instance] [--global]
splash target refresh [ios|android|all]
                                      # reconcile every registered checkout
splash target prune   [ios|android|all] [--dry-run] [--yes]
                                      # destroy sims/emulators splashdown didn't create
splash target claims [--format text|json]
                                      # inspect machine-wide physical claims
splash target claim VARIANT [--force] [--format text|json]
splash target claim --available ios|android|any [--format text|json]
splash target release VARIANT [--force]
splash target release --all

splash env [--checkout PATH]        # list a checkout's resolved keys
splash env get KEY [--checkout PATH]
splash env set KEY=VALUE [--checkout PATH]
splash env release [KEY] [--checkout PATH]

splash gc                           # drop dead-checkout entries (ports, vars, sims)

splash completion [bash|zsh]        # print shell-completion script (eval it in your rc)
```

`splash status` answers "what's the state of this checkout?": resource keys (with `[in use]` /
`[free]` for ports), declared device variants and whether each is booted, automatic sync and
bootstrap trust, bootstrap completion, and stale registry rows.
For a bound port, detailed status also shows listener PIDs and command names when `lsof` can
identify them. JSON port records include `owners`, a list of `{pid, command}` objects, an empty
list for a free port, or `null` when the owner is unavailable. Process arguments are not collected.
Routine status, env-list, and sync JSON output hides resolved values. Add the root-level
`--show-values` flag when you intentionally need them. `splash env get KEY` remains the explicit
single-value read. `splash sync --force` reallocates ports. `splash init` scans the project,
scaffolds the project files, and runs the first sync (`--no-sync` scaffolds only).
Root output options go before the command. `--format` applies to sync, status, bare `env`, bare
`target`, `target claims`, and `target claim`. `--show-values` applies to sync, status, a normal
init's first sync, and bare `env`. Other combinations are usage errors instead of accepted no-ops.
In text mode, explicit `--show-values` prints resolved `KEY=VALUE` lines for sync and the first
sync performed by init. With `status all`, it selects detailed checkout blocks so those values have
a place to appear instead of silently remaining in the compact table.
First-time init must target the Git worktree root; `--allow-nested` explicitly creates an
independent Splashdown project below it. That location override does not replace `--overwrite`,
which remains the separate opt-in for replacing an existing recipe. Non-Git projects and existing
regular nested recipes keep their current behavior. Recipe symlinks are rejected rather than
followed. Nested init skips automatic post-checkout hook wiring and prints the explicit
`splash --cwd PATH sync` command to run after checkout.

`splash init --rescan` only refreshes detected project and app inventory in an existing recipe. It
cannot be combined with a preset, `--loader`, `--overwrite`, `--allow-nested`, `--no-sync`,
`--electron-profile`, or `--ios-scheme`.

Named presets are limited to choices that project scanning cannot infer:

- `minimal` creates a framework-neutral recipe with a generated run id.
- `server` creates a generic `PORT` and a `DATABASE_URL` whose readable checkout slug includes a
  short hash of the resolved path, preventing matching directory tails from sharing a database.
- `electron` creates a renderer `PORT` and opts into checkout-specific Electron user data.

Existing server recipes are not rewritten when the hash suffix changes. Update the
`DATABASE_URL` template manually, run `splash sync`, then create or migrate the newly named
database. The old database remains untouched until you remove it.

Plain `splash init` detects Electron in addition to the renderer framework. In an interactive
terminal, it asks once whether to isolate Electron user data per checkout. The default is No,
and non-interactive input or EOF also selects No. Automation can make the choice explicit with
`--electron-profile=isolated|shared`. Choosing isolation adds a stable
`ELECTRON_PROFILE_ID` and prints the main-process integration to add before
`requestSingleInstanceLock()`:

```js
import { mkdirSync } from "node:fs"

const profileId = process.env.ELECTRON_PROFILE_ID
if (profileId) {
  const userData = `${app.getPath("userData")}-${profileId}`
  mkdirSync(userData, { recursive: true })
  app.setPath("userData", userData)
}
```

`splash init electron` is the explicit opt-in for a standalone project. It does not prompt,
includes both `PORT` and `ELECTRON_PROFILE_ID`, and prints the same integration. This keeps
profiles beside Electron's normal platform-specific user-data directory instead of inside the
checkout.

For a detected native iOS project, init records the sole shared Xcode scheme automatically. If
several schemes exist, it asks for an exact choice in a terminal. Non-interactive callers must
pass `--ios-scheme=NAME` when the choice is ambiguous.

## Remove splashdown

`splash deinit` surgically removes checkout-local init state plus state created by sync and device runs. It
destroys simulator and emulator instances owned by this checkout, releases its registry entries,
removes `splashdown.env`, clears splashdown-managed keys from `envfile=` and `envrc` destinations,
and unwires the loader, `.gitignore` entries, and managed agent instructions. The shared
post-checkout integration and clone-wide bootstrap trust remain because linked worktrees may still
use them. Deinit clears only this checkout's bootstrap completion.
It then removes `splashdown.toml` and an untouched `splashdown.local.toml` skeleton.

User-owned content is preserved: a modified local config or hook is left with a note, unrelated
dotenv keys remain, physical devices are never destroyed, and framework changes made by
`splash doctor --fix` are not reverted because they have no recoverable original.

`splash sync --setup NAME` runs the recipe's `[setup.NAME]` commands after resolving and writing resources. Empty or malformed setup declarations fail during recipe validation, before those changes. An unknown requested name or failed command exits 1 after provisioning. Resource/output changes and earlier successful setup commands are not rolled back.

`splash trust` authorizes automatic resource sync for the whole clone. When the current recipe has
`[bootstrap]`, it displays and authorizes those commands without running them. A later-added
`[bootstrap]` needs another trust operation unless the clone was already trusted for bootstrap on
an earlier ref. Existing bootstrap trust lasts until `splash untrust`. `splash bootstrap` provisions the checkout and runs
the commands once, and `--rerun` repeats a completed bootstrap. `splash untrust` revokes both
capabilities without needing a valid recipe. See
[Trusted worktree bootstrap](bootstrap.md) for the security and retry contract.

`splash env set KEY=VALUE` only accepts keys declared with `type = "set"` in the target checkout's recipe. It rejects invalid assignments, missing or malformed recipes, undeclared keys, and generated or allocated resources with exit 2.

For nested environment actions, `--checkout PATH` may appear either immediately after `env` or
after the action arguments. It takes precedence over root `--cwd` in both forms.

Commands that load configuration validate the complete document before provisioning or project-file mutation. Unknown sections and fields, wrong types, invalid templates, and malformed target definitions exit 1 with a qualified error and no traceback.

`splash target add` applies the same target schema as TOML files before writing. `simulator` accepts `--model`, `--ios`, and `--name`. `emulator` accepts `--device`, `--image`, and `--name`, where `--device` is the Android emulator hardware profile such as `pixel_9`. Physical `device` accepts `--id`, `--name`, and `--platform=ios|android`. Supplying a flag for the wrong target type is an error and leaves the local or global config unchanged.

Local `target remove` destroys its managed simulator or emulator by default. A global removal edits
the machine-wide declaration only, then `splash target refresh` reaps any instance the removal made
undeclared. Because global removal is already config-only, `--global --keep-instance` is a usage
error. Physical `device` removal has no managed instance, so combining it with `--keep-instance` is
also a usage error.

Refresh is machine-wide even when invoked from one checkout. It recreates stale or externally
deleted registered instances at each declaration's runtime or image, resolving `latest` live. It
also destroys undeclared instances and instances belonging to deleted checkouts without a prompt.
It does not provision declared targets that have never been run.

Physical claim commands act only on configured `device` targets from the recipe, local config,
and global config. `splash run pixel` claims a free connected target before framework build or
installation. A busy or disconnected target fails before that work starts. The claim persists
after process exit and launch failure, and `splash stop device pixel` does not release it.

`splash target claims` reads registry ownership without device discovery. Its text output and JSON
output include the target, source, platform, hardware ID, canonical owner checkout, and claim time.
Specific `target claim VARIANT` writes its human diagnostic to stderr. Generic allocation prints
only the chosen variant to stdout in text mode, so scripts can capture it:

```sh
device=$(splash target claim --available android)
splash run device "$device"
```

For either claim form, `--format json` writes `target`, `source`, `platform`, `hardware_id`,
`owner`, `claimed_at`, and `status` to stdout. Generic allocation checks configured targets in
recipe, local, then global order and skips disconnected or busy matches. `claim VARIANT --force`
atomically transfers a live owner's claim. `release VARIANT --force` clears it without taking it.
`release --all` removes only claims owned by the current checkout.

A forced transfer or release queues a warning for the displaced checkout. Its next ordinary
checkout-scoped command prints and consumes the warning once. Completion, help, version output,
and the hidden post-checkout command do not consume it. `splash deinit` releases the checkout's
claims and pending notices. `splash gc` removes claims for deleted checkouts and expired or
dead-checkout notices.
