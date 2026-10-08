---
title: CLI reference
description: Reference for every splash command, option, target action, and environment operation.
---

# CLI reference

```
splash                              # sync this checkout explicitly
splash --version
splash [--cwd PATH] [--format text|json] …  # --cwd and --format work at every command level
splash sync [--force] [--setup N]   # pick free ports, resolve vars, write the env file
splash status [all] [--verbose]
                                      # resources + targets + health/cleanup details
splash init [--loader=…] [--env-file=PATH] [--overwrite]
splash deinit                       # remove checkout-local state, keep shared hook and trust
splash trust                        # authorize automatic handling for this clone
splash untrust                      # revoke clone-wide automatic handling
splash bootstrap [--rerun]          # sync + run bootstrap once for this checkout
splash doctor [--fix] [--framework=…]
splash ai status                    # inspect recorded agent guidance without changing files
splash ai update [--replace]         # render current recipe, optionally replace edited blocks
splash ai uninstall                 # remove guidance without changing provisioning

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

splash env [--cwd PATH]        # list a checkout's stored values
splash env get KEY [--cwd PATH]
splash env set KEY=VALUE [--cwd PATH]
splash env release [KEY] [--cwd PATH]

splash gc                           # drop dead-checkout entries (ports, vars, sims)

splash completion [bash|zsh]        # print shell-completion script (eval it in your rc)
```

`splash status` reports resource names and port state, output consistency, saved trust,
loader/hook readiness, and declared or registered targets. Every normal report includes actionable
findings and unavailable checks. `--verbose` expands text detail. JSON includes the full report
under the shared result envelope's `data.checkouts`, unchanged by verbosity. Resource values stay
hidden. `splash env` returns assigned values and `splash env get KEY` prints one raw value.

`status all` lists only existing registry identities, including deleted checkouts with cleanup
hints. An empty registry gives an empty fleet. Inspection does not initialize state or require
trust. A valid project with no allocations is inspectable. Missing/invalid recipes and unreadable
required inputs return 1 with available partial results. Findings and unavailable optional tools
return 0, so use check fields rather than the exit code to decide readiness. Status never claims
that arbitrary preparation commands previously succeeded. Free ports, stopped devices, and lazy
targets that have not been created are ordinary states.

Port records include `owners`, a list of `{pid, command}`, an empty list for free ports, or null
when ownership is unavailable. Process arguments are not collected. Use `splash sync` to reconcile
resource/output findings and `splash doctor` for deeper tool/framework diagnostics.
For an unassigned `set` resource without a default, supply its value with
`splash env set KEY=VALUE` before syncing.

`--cwd` and `--format` may appear before or after a command or nested action. The last
supplied value wins, and every supplied format must be valid. Long options require their exact
spelling. Use `splash help target add` for contextual help. Help and version always print text.
Completion prints shell code and rejects JSON. `--format` applies to sync, status, init,
`env`, `env get`, bare `target`, `target claims`, `target claim`, and `ai` commands.
Other combinations are usage errors. Status reports and environment values go to stdout.
Diagnostics go to stderr.

Existing project commands find the nearest recipe from the starting directory up to the Git
worktree root. An invalid recipe still selects that directory. Outside Git, selection stays
at the starting directory. Except for the env inspection exception below, every supplied `--cwd` must name an existing readable directory,
even when a later occurrence overrides it. Env set/release act on that exact directory.
Machine-wide commands skip upward project discovery.

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

## Agent guidance

Init updates a Splashdown section in existing root `AGENTS.md` and independent
`CLAUDE.md` files. They create no instruction file. A real Claude `@AGENTS.md` or `@./AGENTS.md`
import uses the shared guidance once that file is successfully updated. Imports shown in Markdown
code examples do not change this selection.

`splash ai status` changes no files. With a valid recipe, `current` means the installed guidance
also matches the current generated content and `outdated` means an update is available. When the
recipe is missing or invalid, `recorded-current` confirms receipt integrity with no latest-content
comparison. `splash --format json ai status` exposes `content_current` for recorded integrity and
`desired_current` for the available recipe comparison, or `null` when that comparison is unknown.
AI JSON output is one object with `command`, `status`, `exit_code`, `data`, `error`, `warnings`,
and `next_steps`. Per-file records live under `data.files`, with an activation reminder under
`data.activation`. Status is `success`, `error`, or `partial`. Partial failures retain completed
file results. Ctrl+C returns exit 130 and includes any file results already available. Check
`splash ai status` before retrying an interrupted operation.

Run `splash ai update` to render changes in the recipe or in Splashdown's guidance. Reload agent
sessions after updates.

Normal update and uninstall preserve edits inside the section and return nonzero when those edits
or damaged metadata prevent completion. `splash ai update --replace` permits replacement of a
complete edited section while keeping the first original content for later restoration. Malformed
or duplicated markers need manual repair. User prose outside the section survives these operations.
Init warns about guidance failures and keeps successful initialization changes.

`splash ai uninstall` removes receipt-owned guidance even when `splashdown.toml` is missing or
invalid. Deinit also removes guidance. An old installation without receipts is adopted only when
its single complete block exactly matches the current generated guidance. This works on the first
direct deinit with a valid recipe. Edited or unverifiable old blocks stay in place with a warning.
Keep private guidance metadata intact. Missing receipts cannot authorize deleting a block.
After an interrupted update or uninstall, retry the command. Splashdown recovers recognized
interrupted work before checking the files again. If recovery cannot verify the file or metadata,
the command fails and keeps its recovery evidence. `splash ai status` only reports that state.

## Remove splashdown

`splash deinit` surgically removes checkout-local init state plus state created by sync and device runs. It
destroys simulator and emulator instances owned by this checkout, releases its registry entries,
clears splashdown-managed keys from every writer destination and deletes one left with nothing else,
and unwires the loader and unchanged receipt-owned agent instructions. From its `.gitignore`
block it removes the rules for files it deleted and keeps the rules for files it left behind, such
as a `splashdown.local.toml` you edited, and it removes a `.gitignore` left with nothing in it at
all. Private guidance metadata and its permanent ignore entries remain for safe future operations.
The loader configuration is restored to the bytes the project committed, blank separator lines
included. The shared
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

`splash env` and `splash env get KEY` read only stored assignments. They do not parse recipes
or local overrides, require trust, allocate resources, or create registry state. Stale assignments
remain visible. Keys match exactly, including case. An empty listing succeeds, and an assigned
empty string prints a blank line. A missing key exits 1 with an error and a `splash env` hint.
JSON uses the shared result envelope: listing data contains `checkout` and `values`, while get
data contains `checkout`, `key`, and `value`. Missing keys use `assignment_missing` and retain
`checkout` and `key`. Unreadable or malformed registry inputs fail instead of appearing empty.

Inside Git, these two read commands select the nearest recipe entry or registry-known checkout,
without walking beyond the worktree root. Outside Git, they use the exact starting directory.
If neither marker exists, they query that starting directory. An explicit deleted `--cwd` is
accepted only when its canonical path exactly matches a registry identity. Every supplied cwd
is validated, including overridden ones. Set/release still require an existing exact directory.

`splash env set KEY=VALUE` only accepts keys declared with `type = "set"` in the target checkout's recipe. It rejects invalid assignments, missing or malformed recipes, undeclared keys, and generated or allocated resources with exit 2.

For nested environment actions, `--cwd PATH` may appear either immediately after `env` or
after the action arguments. The last supplied value wins. Set and release use that exact directory.

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
`ai` commands, and the hidden post-checkout command do not consume it. `splash deinit` releases the checkout's
claims and pending notices. `splash gc` removes claims for deleted checkouts and expired or
dead-checkout notices.
