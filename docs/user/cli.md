---
title: CLI reference
description: Reference for every splash command, option, target action, and environment operation.
---

# CLI reference

```
splash                              # sync this checkout explicitly
splash --version
splash [--format text|json] [--show-values] …
splash sync [--force] [--setup N]   # pick free ports, resolve vars, write splashdown.env
splash status [all]                 # resources + targets + which ports are bound right now
splash init [preset] [--rescan] [--no-sync] [--loader=…] [--overwrite]
                    [--electron-profile=isolated|shared] [--ios-scheme=NAME]
splash deinit                       # remove checkout-local state, keep shared hook and trust
splash trust                        # authorize automatic handling for this clone
splash untrust                      # revoke clone-wide automatic handling
splash bootstrap [--rerun]          # sync + run bootstrap once for this checkout
splash doctor [--fix] [--framework=…]

splash run     [type] [variant]     # boot target + build + launch
splash start   [type] [variant]     # boot target (no build/launch)
splash stop    [type] [variant]     # shut down
splash destroy [type] [variant]     # delete this checkout's target instance

splash target                       # list declared targets + live state
splash target add/remove <type> <variant> …
splash target refresh [ios|android] # recreate stale sims/emulators
splash target prune   [ios|android] # destroy sims/emulators splashdown didn't create

splash env                          # list this checkout's resolved keys
splash env get KEY | set KEY=VALUE | release [KEY]

splash gc                           # drop dead-checkout entries (ports, vars, sims)

splash completion [bash|zsh]        # print shell-completion script (eval it in your rc)
```

`splash status` answers "what's the state of this checkout?": resource keys (with `[in use]` /
`[free]` for ports), declared device variants and whether each is booted, and stale registry rows.
Routine status, env-list, and sync JSON output hides resolved values; add the root-level
`--show-values` flag when you intentionally need them. `splash env get KEY` remains the explicit
single-value read. `splash sync --force` reallocates ports. `splash init` scans the project,
scaffolds the project files, and runs the first sync (`--no-sync` scaffolds only).

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

Commands that load configuration validate the complete document before provisioning or project-file mutation. Unknown sections and fields, wrong types, invalid templates, and malformed target definitions exit 1 with a qualified error and no traceback.

`splash target add` applies the same target schema as TOML files before writing. `simulator` accepts `--model`, `--ios`, and `--name`. `emulator` accepts `--device`, `--image`, and `--name`. Physical `device` accepts `--id`, `--name`, and `--platform=ios|android`. Supplying a flag for the wrong target type is an error and leaves the local or global config unchanged.
