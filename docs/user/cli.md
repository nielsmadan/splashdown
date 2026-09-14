---
title: CLI reference
description: Reference for every splash command, option, target action, and environment operation.
---

# CLI reference

```
splash                              # sync this checkout explicitly
splash --version
splash [--cwd PATH] [--format text|json] [--show-values] …  # root options precede the command
splash sync [--force] [--setup N]   # pick free ports, resolve vars, write the env file
splash status [local|all] [--check] [--verbose]
                                      # resources + targets + health/cleanup details
splash init [--loader=…] [--env-file=PATH] [--overwrite]
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
single-value read. `splash sync --force` reallocates ports. `splash init` scans the project and
writes the project files. It allocates nothing, so run `splash trust` and then bare `splash`
after it.
Root output options go before the command. `--format` applies to sync, status, init, bare `env`,
bare `target`, `target claims`, and `target claim`. `--show-values` applies to sync, status, and bare
`env`. Other combinations are usage errors instead of accepted no-ops.
In text mode, explicit `--show-values` prints resolved `KEY=VALUE` lines for sync. With
`status all`, it selects detailed checkout blocks so those values have a place to appear instead
of silently remaining in the compact table.

Init creates a project in the current directory, or the directory selected by `--cwd`, whether
at a Git worktree root, inside it, or outside Git. Replacing an existing recipe requires
`--overwrite`. Recipe symlinks are rejected rather than followed. Nested init leaves the
repository's post-checkout hook untouched and prints the explicit `splash --cwd PATH sync`
command to run after checkout.

`--env-file` selects the file that receives the generated values. Without it the destination is
`splashdown.env`, a file splashdown generates next to the recipe. The path is relative to the
checkout and must stay inside it and outside `.git`, so an absolute path, one that escapes, or one
inside the Git directory is rejected before init writes anything. Init records the choice as `env_file` in the recipe's `[project]` table and
reports the destination, the keys it will manage there, and any of those keys the file already
sets. Values are never printed. Nothing is written to the destination by init itself, so run
`splash` afterwards.

Splashdown manages only its declared keys in that file, so pointing `--env-file` at a dotenv file
your project already reads keeps the rest of the file intact. An existing value for a declared key
is replaced on the next sync, with no extra flag or confirmation. A key assigned twice, or
assigned in a shape splashdown cannot rewrite such as `PORT: 3000`, is reported as an error rather
than joined by a second competing definition.

A resource with its own `writer` keeps that destination and is not also copied into the default
file. See [The recipe](recipe.md#writers) for the writer values.

`--loader` selects how the environment is loaded. It works with `--env-file`: the selected loader
is wired to read the chosen destination, so `splash init --loader mise --env-file .env` records
direct dotenv delivery and points mise at `.env`. With `--loader none` only the destination is
configured and no loader file is touched. Without it, init wires the sole loader whose
configuration exists in the directory and says which file decided that. With several configured
it stops before changing anything and asks you to choose. With none configured it selects
`none`. An installed tool is not by itself a reason to adopt it. Init reuses loading directives
the project already has rather than adding a second one, and adds only its own directive where
one is missing. `splash --format json init` reports the same selection, why it was made, and
every file init changed.

Evolve an existing recipe by editing it manually or with an agent. `splash init --overwrite`
regenerates the whole recipe, replacing manual edits. It keeps the destination the recipe already
records, so a project set up with `--env-file config/.env` keeps writing there. Pass `--env-file`
again to move it. See
[adding an app](monorepos.md#adding-or-moving-an-app) for a comparison workflow.

Init always generates the recipe from a project scan. For the choices a scan cannot infer, such
as a generic `PORT` for any server that reads one, a per-checkout Postgres database name, or
Electron user-data isolation, add the resource yourself. See
[The recipe](recipe.md) for each pattern.

Plain `splash init` detects Electron in addition to the renderer framework. It configures the
renderer like any other app and asks nothing. Two checkouts of an Electron app still share one
user-data directory, because separating them needs a change in your main process that Splashdown
cannot make for you. Init points at
[Electron user-data isolation](recipe.md#electron-user-data-isolation), which has the resource to
declare and the code to add. A project init does not detect as Electron opts in the same way.

For a native iOS project, init records no Xcode scheme and never runs `xcodebuild`, so it works
with Xcode missing or broken. `splash run` picks the scheme instead: it uses
`[project.ios] scheme` when your recipe sets one, and otherwise builds the only shared scheme it
finds. With no scheme or several, the run stops before touching a simulator and asks you to set
`[project.ios] scheme`. See [The recipe](recipe.md) for where that goes.

## Remove splashdown

`splash deinit` surgically removes checkout-local init state plus state created by sync and device runs. It
destroys simulator and emulator instances owned by this checkout, releases its registry entries,
clears splashdown-managed keys from every writer destination and deletes one left with nothing else,
and unwires the loader and managed agent instructions. From its `.gitignore` block it removes the rules for files it deleted and keeps the rules for files it left behind, such as a `splashdown.local.toml` you edited, and it removes a `.gitignore` left with nothing in it at all. The loader configuration is restored to the bytes the project committed, blank separator lines included. The shared
post-checkout integration and clone-wide bootstrap trust remain because linked worktrees may still
use them, and deinit names the hook configuration file it left that entry in. Deinit clears only this checkout's bootstrap completion.
It then removes `splashdown.toml` and an untouched `splashdown.local.toml` skeleton.

User-owned content is preserved: a modified local config or hook is left with a note, unrelated
dotenv keys remain, physical devices are never destroyed, and framework changes made by
`splash doctor --fix` are not reverted because they have no recoverable original.

A value whose resource you deleted from the recipe before running deinit cannot be chased: nothing
records which file a writer that the recipe no longer declares sent it to. Deinit names those keys
so you can remove them from that file yourself.

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
