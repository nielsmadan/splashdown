# Init and Onboarding (`splash init`)

> Covers **UC3** (one-command project setup) and **UC6** (a teammate clones a configured repo).
> Audience: senior devs extending splashdown. Persona: the parallel-agent developer and the
> two work-flavor personas (mobile, web/backend) in `docs/product/persona.md`.
> `README.md` is the authoritative spec.
> **Implemented by:** [scanning-and-extension](../tech/scanning-and-extension.md),
> [cli-and-commands](../tech/cli-and-commands.md).

## Contents

- [Overview](#overview)
- [How it works (current state)](#how-it-works-current-state)
- [Key entry points](#key-entry-points)
- [Configuration](#configuration)
- [Gotchas](#gotchas)
- [Why](#why)

## Overview

`splash init` is the project-adoption command. In one run it scans the workspace and each
app's framework, scaffolds the committed `splashdown.toml` (plus a per-checkout
`splashdown.local.toml` skeleton), writes the detected shell-env loader's configuration
(mise/direnv/devbox) and the project-owned `post-checkout` hook configuration that coexists
with an existing hook manager, runs the project-configuration framework wiring checks (the same
engine as `splash doctor --fix`, against the configured environment output), and reports the
commands that make the checkout ready.

Init is configuration-only. It allocates no resources, writes no environment output, records no
trust, runs no loader approval command, and installs nothing into the local `.git` directory, so
the user can read and edit the generated recipe before anything reaches the machine. Adoption is
two commands: `splash init`, then `splash trust` plus a sync (bare `splash`).

Three options reshape that flow:

- `splash init --loader=mise|direnv|devbox|none` — override loader detection, including
  an explicit no-loader setup.
- `splash init --env-file=PATH` — choose the file that receives generated values.
- `splash init --overwrite` — replace an existing recipe (init refuses otherwise).

## How it works (current state)

**Location and replacement.** `cmd_init` uses the current directory or explicit `--cwd` as the
project location, including nested Git directories and non-Git projects. It refuses to replace an
existing `splashdown.toml` unless `--overwrite` is passed. Symlinked and other non-regular recipe
entries are rejected rather than followed. These failures raise `UsageError`; the CLI renders
them as exit 2, while direct callers receive the exception. Init is dispatched before `Registry`
construction so a refusal leaves machine state untouched.

**Nested hook behavior.** Git invokes its post-checkout hook from the worktree root, where a nested
recipe is not visible. Nested init therefore skips automatic hook wiring and
prints the nested `splash --cwd PATH sync` command to run after checkout. It never installs a hook
that would silently sync the wrong project.

**Scan.** Init runs `Scanner().scan(cwd)` (`scanner.py`), which:
detects the workspace manager (pnpm/yarn/npm/cargo/gradle/`single`) via `_detect_workspace`
(`scanner.py`); enumerates apps via `_enumerate_apps` (`scanner.py`); matches each app
to a Profile by name through the `PROFILES` registry, defaulting to `"unknown"` when nothing
matches (`scanner.py`). The loader is chosen before the scan by `select_loader`
(`scanner.py`), and `Scanner.scan(cwd, loader=…)` records the choice rather than repeating it.
The result is a `ProjectInventory` of `AppInventory` entries defined in `inventory.py`.

**Loader selection.** `select_loader` (`scanner.py`) asks each `Loader.config_paths(cwd)` which
of its configuration files exist, so mise's `mise.toml` and `.mise.toml` count as one candidate.
An explicit `--loader` wins, including `none`. Otherwise the sole configured loader is selected;
several configured loaders raise `UsageError` naming them, before init writes anything; no
configuration selects `none`. There is no PATH probe: a binary's presence is not a reason to
adopt a project integration. The returned `LoaderSelection` carries `name`, `selected_by`
(`override` / `detected` / `unconfigured`) and the config filenames, which `_print_init_scan`
and the JSON report both render.

**Failure reporting.** `cmd_init` wraps its whole body in `_init_failure` (`commands.py`) and
its write phase in `_init_effects`, so every failure reaches a `--format json` consumer as one
`{"ok": false, "error": …}` envelope carrying whatever had already been selected and changed.
A plan-time `LoaderConflictError` and a late `OSError` are the same shape; `_init_failure`
skips the emission when `_init_effects` already made it. The CLI's init branch catches
`OSError` alongside `ApplicationError`, `DeviceError` and `ValueError`, so a permission failure
exits with an `error:` line rather than a traceback.

**Resource collection + collision deferral.** For each non-`unknown` app, `cmd_init` asks the
matched `Profile.resources(app)` for the resources it wants. If several apps claim the same
canonical resource name, init writes a structure-only recipe and directs the user to configure
explicit monorepo resources instead of guessing names or ranges. Collision-free projects continue
through `_build_resource_catalog`, which produces the flat resource table and each app's resource
list together. `_should_defer_monorepo` (`scanner.py`) also chooses the structure-only path when
the workspace contains an unclaimed sibling Xcode or Gradle project. A detected React Native,
Expo, or Flutter app claims its own native subdirectories, so they do not cause a false deferral.
Compose resources are collected only after this decision; a Compose file by itself is not a
deferral trigger.

**Electron pointer.** Scanner-detected Electron apps keep their renderer resources and their
primary Profile. Init asks nothing and generates no profile-id resource; user-data isolation
needs a main-process change Splashdown cannot make, so `_print_electron_isolation_pointer`
(`commands.py`) names the opt-in recipe at
[recipe.md#electron-user-data-isolation](../user/recipe.md#electron-user-data-isolation),
which carries the template resource and the `app.setPath("userData", ...)` integration it
depends on.

**Output destination.** `--env-file PATH` selects the file that receives generated values.
`validate_env_file_option` (`recipe.py`) checks it before anything is written, sharing
`_checkout_relative_path` with the `envfile=` writer validator, so a non-relative, escaping,
empty, or Windows-absolute path fails in the same way at either entry point. The default is
`ENV_FILE_NAME`. A non-default choice is recorded as `[project] env_file` through
`_resolve_init_project_metadata`, and `Recipe.env_file` is the single reader. Init prints the
destination, the keys that will land there (`_default_destination_keys`), the declared keys the
file already assigns (`existing_managed_keys`, values never disclosed), and a warning when the
destination is not gitignored. `existing_managed_keys` raises on an ambiguous assignment before
init writes anything. There is no file-presence-based routing: finding a `.env` never selects it.

**Native iOS scheme.** Init neither discovers nor records one, and never runs `xcodebuild`, so
a native iOS project initializes with Xcode missing or broken. `[project.ios] scheme` stays a
hand-written recipe setting, and `splash run` resolves it: see
[device-targets.md](device-targets.md#native-ios-scheme-resolution).

**Prune, validate, write + wire.** A Profile emits resources for one app and cannot see its
siblings, so a cross-app template reference can dangle — Vite emits
`API_DEV_PORT = "{{ PORT }}"` for any config mentioning a proxy, but `PORT` exists only when
the repo also has a backend app. `_prune_unresolvable_templates` (`scanner.py`) runs after the
cross-app merge and drops such templates (looping to a fixed point, since pruning one can
strand another) and un-lists them from `[apps.*] resources`, printing one `skipped NAME:` line
each. The recipe is then rendered by `render_scanned_recipe` (lazy-imported from `tomlio`)
and parsed in memory through the same
strict `Recipe` validator used by provisioning before it is written. This catches scanner/profile
drift, invalid app resource references, resource/writer/template/schema errors, and unknown
fields before init mutates the recipe or proceeds to loader/hook wiring. A `splashdown.local.toml` skeleton (`LOCAL_SKELETON`)
is written if absent after the recipe passes validation. `_ensure_gitignore` (`hooks.py`) adds
`splashdown.env` and `splashdown.local.toml` to `.gitignore`.

**Loader wiring.** `Loader.plan(cwd, env_file)` (`loaders.py`) parses and validates the edit and returns a
`WirePlan` without writing; `cmd_init` builds it before the recipe is written, so a malformed
config or an unextendable directive raises `LoaderConflictError` before any file changes.
`_commit_loader_plan` (`commands.py`) applies it afterwards through `safe_files`'
`atomic_write_text`, which refuses symlinked destinations and paths outside the checkout.
`--overwrite` governs the recipe only and does not relax this.

A plan's status is one of `created`, `updated`, `configured` (splashdown's own directive is
already there), `reused` (the project's own directive is), or `nothing` (`--loader none`). The
two reuse outcomes write nothing at all. Recognition is deliberately bounded to forms these
files can be read for, with simple quoting, tabs and a leading `./` normalized by
`normalized_env_reference` (`constants.py`):

- mise: `_.file` naming the file, in the plain or the list form and in the dotted or `[env._]`
  subtable spelling. A slot holding another file is extended to a list rather than replaced;
  a slot that is neither a string nor a list of strings is a conflict.
- direnv: a `dotenv` or `dotenv_if_exists` line at column 0 outside splashdown's sentinel block.
  An indented one sits inside a function or conditional and does not count, and a `#` glued to
  the filename is part of the name rather than a trailing comment.
- devbox: an `init_hook` whose statements include a standalone `set -a` (or `set -o allexport`)
  followed by a standalone `source`/`.` of the file, both at column 0, with no `set +a` in
  between. Comments are stripped per line before the line is split on `;`, so a `;` inside a
  comment cannot produce a statement, and an indented line sits inside a function or conditional
  and does not count. A chained (`&&`) statement does not match either.

Two further bounds are deliberate false negatives: `.envrc.local` and `.mise.local.toml` count
toward detection but are never read for reuse, and a devbox `set -a` in one `init_hook` entry
with the `source` in another is not recognized. Each costs a duplicate directive, which is the
cheap side of the trade.

One known bound is a **false positive**, and it costs the expensive side. Recognition is textual
and no shell parsing is attempted, so a statement at column 0 inside a block whose body is not
indented reads as top level for direnv and devbox alike. A `source splashdown.env` that only
runs under an `if` is reported as reuse, init writes nothing, and the checkout stays unwired
until the user indents the body or wires the loader by hand.

Splashdown removes only what it marked: the mise directive carries a `splashdown-managed`
comment, the direnv block its sentinels, the devbox hook its marker. A directive init merely
reused therefore survives `splash deinit`. The mise marker is entry-scoped, so a slot widened
to a list loses only splashdown's entry and stays a list. A mise directive written before the
marker existed is indistinguishable from a hand-written one, so splashdown leaves it alone and
the plan's `hint` tells the user to delete the line and re-run init if splashdown wrote it. Init never runs `mise trust` or `direnv allow`;
approval is an activation effect owned by `cmd_trust` — see the trust-approval note below.

**Git hook detection.** `detect_hook_configuration` (`hooks.py`) decides who owns this
checkout's post-checkout event and returns the manager, the reason, and every candidate it saw.
Precedence runs from what Git enforces down to what a project merely declares, so a missing
recognized package is never taken as proof that native installation is safe:

1. **`core.hooksPath`**, when set, because Git runs those hooks and no others. A path inside
   `.husky` is husky (husky 9 sets `.husky/_`); anything else is `core-hookspath-other`.
2. **The installed `post-checkout` hook** in the effective hooks directory, when its text carries
   a manager's generator signature. This is observed ownership rather than inferred ownership.
3. **Any other installed Git hook** in that directory with such a signature.
4. **A manager's configuration file** in the checkout.
5. **A `package.json` declaration** — a dependency, or the `simple-git-hooks` key.

Splashdown's own hook bodies are excluded from levels 2 and 3, and `*.sample` files are skipped.
The signatures are `_INSTALLED_HOOK_SIGNATURES` (`hooks.py`), each read off a hook the tool
itself generated (see `docs/tests/qa-1.0/`).

Two or more managers at the same level is a **`conflict`**: splashdown reports the candidates,
writes nothing, and asks the user to add the entry to the manager they actually use. This is why
a repository with both a `.husky/` directory and lefthook in `package.json` now resolves to husky
— a configuration directory outranks a bare dependency — instead of being silently classified
lefthook.

**Git hook configuration.** `_configure_post_checkout_hook` (`hooks.py`) writes the
project-owned configuration that forwards Git's three event arguments (old ref, new ref, branch
flag) to Splashdown on later checkout and worktree transitions. Each manager gets an adapter in
`_ADAPTERS` (`hooks.py`) pairing a `configure` that writes tracked project content with an
`install` that trusted activation runs. Splashdown **coexists** rather than clobbers: it adds or
updates only its own entry, and every adapter is idempotent.

- **lefthook** → a `post-checkout.commands.splashdown` entry in the tracked lefthook config that
  forwards `{1} {2} {3}`, lefthook's own templating. `lefthook install` is local activation and is
  not run by init. Project-controlled `yarn` or `npx` commands are never executed.
- **husky** → a `.husky/post-checkout` hook, which is a tracked project file, forwarding
  `"$1" "$2" "$3"`.
- **pre-commit** → a `local` repo hook with `id: splashdown` in `.pre-commit-config.yaml`, the
  only spelling pre-commit reads. post-checkout hooks are skipped without
  `always_run: true`, and the event arrives in the environment rather than as arguments, so the
  entry translates `$PRE_COMMIT_FROM_REF`, `$PRE_COMMIT_TO_REF` and `$PRE_COMMIT_CHECKOUT_TYPE`
  back into the three positional values the handler expects.
- **prek** → the same local hook in the configuration prek itself loads. prek reads `prek.toml`,
  then `.pre-commit-config.yaml`, then `.pre-commit-config.yml`, first match wins, so a prek
  project configured in YAML is edited in place: creating a `prek.toml` beside it would outrank
  the file and silently retire every hook the project actually runs. A native `prek.toml` is
  edited through `tomlkit` so comments and formatting survive; a YAML configuration goes through
  the same editor pre-commit uses and is reported as prek. prek exposes the identical environment
  contract, verified against its documented hook environment.
- **simple-git-hooks** → a `post-checkout` command in the first configuration the tool itself
  would read: an existing `.simple-git-hooks.json` / `simple-git-hooks.json`, otherwise the
  `simple-git-hooks` key already in `package.json`, otherwise a new `.simple-git-hooks.json`.
  The command is copied into the generated hook verbatim, so it forwards `"$1" "$2" "$3"`.
  A `package.json` rewrite keeps the file's own line endings and its non-ASCII characters
  unescaped, so the first run changes only the block splashdown owns. Dynamic `.js` / `.cjs` /
  `.mjs` configuration, and a `package.json` value that is a string path to another file, are
  **never executed or edited** — they get manual instructions instead.
- **overcommit** → recognized, preserved, and given manual instructions. There is no adapter.
- **any other configured `core.hooksPath`** → do **not** touch it; print a warning telling the
  user to invoke a trusted absolute `splash` executable with the post-checkout event arguments,
  or run bootstrap manually.
- **conflict** → preserve every candidate and name them in the warning.
- **none** → there is no project file to write. The native hook lives under Git's common hooks
  directory, which is local state, so init only notes that `splash trust` installs it. Outside a
  Git checkout there is no such hook, and init notes that instead of naming `splash trust`.
  Splashdown never changes `core.hooksPath`, and the common hook is shared by all worktrees.
  The hook body is `POST_CHECKOUT_HOOK` (`hooks.py`): Git already starts it at the repo top, so it
  no-ops if `splashdown.toml` is absent, resolves `splash` once, rejects an executable inside the
  checkout, and invokes the internal event handler once with all three Git arguments. A missing
  executable prints a note. The wrapper absorbs the handler's failure after diagnostics.

Every adapter shares `SPLASH_GUARD` (`hook_configs.py`), the one-line form of the native hook's
resolution and checkout-controlled-executable rejection, so a manager's configuration cannot be
used to run a `splash` that lives inside the checkout.

A configuration splashdown cannot parse is never reported as wired, and a file it cannot read
back exactly is never rewritten. The YAML editor recognizes block mappings and block sequences
only, at whatever column the project already uses, and returns `unrecognized` for anything else —
any flow-style `repos:` or `hooks:` value, a config with no single top-level `repos:` key, a key
indented below a scalar, a symlinked config, malformed TOML. `unrecognized` preserves the file
and routes the user to manual instructions. Before writing, the editor re-reads the document it
just built and abandons the edit unless splashdown's hook parses back out of it.

`post_checkout_readiness` separates the two halves: `ready` says the project-owned configuration
carries splashdown's current entry, and `active` says the owning manager's `post-checkout` hook is
actually installed in this checkout. Configured but inactive is a warning in `doctor`, never a
green tick, because no event can arrive until that manager's own install has run.
`post_checkout_files` names the file the adapter writes, which is what `doctor` reports as
changed; a configuration in an unrecognized shape names nothing, because splashdown will not
write it.

`_ensure_post_checkout_hook` is the fused configure-plus-install variant, reached only from
`doctor --fix` through `_autofix_ensure_post_checkout_hook` (`wiring.py`).

**Wiring checks.** For each known-profile app, `cmd_init` runs the profile's
`wiring_checks(app, env_file)`, where `env_file` is the configured environment output spelled
relative to that app's directory (`wiring_destination` in `wiring.py`). For any check whose
`detect` is not `"ok"` it applies the `autofix` if one exists, then detects again
(`_apply_init_wiring_check` in `commands.py`): a fix that did not resolve the problem, an autofix
that raised, and a check with no autofix all print a `✗` line followed by the check's manual
instructions, so incomplete or unsupported integration is reported rather than assumed. Files
whose bytes changed around a fix are added to `InitReport.changed`, which is what the `changed:`
line and the JSON report list.

Integration that already reads the configured destination detects as `ok`, so it is never
rewritten and stays byte-identical. Ordinary adoption therefore needs no follow-up
`splash doctor --fix`; doctor keeps its diagnosis and repair role for later drift.

This is the same `WiringCheck` machinery as `splash doctor` (see UC5 / `wiring.py`), minus the
checks marked `activation`: `_apply_init_wiring_checks` skips those, so the `hook` check the
mobile profiles carry cannot install the local hook init just said `splash trust` owns.

**Agent guidance.** After the generated recipe validates, every init path parses that recipe
and derives a sentinel-wrapped Markdown block for port-bearing apps. The block uses each
`[apps.*].resources` entry's actual resolved name and combines common
no-numeric-port rules with `Profile.agent_guidance()` launch instructions. It names the
configured output destination (`Recipe.env_file`, so `init --env-file PATH` or
`[project] env_file` is reflected) and only commands the 1.0 surface has: `splash env get KEY`,
`splash env` (which lists variable names, not values), `splash sync`, and the profile's own
`splash run` launches. It never embeds an
allocated value, and it points agents at the project's existing scripts where those already
consume the environment instead of wrapping them. Existing root
`AGENTS.md` and independent `CLAUDE.md` files are updated; neither is created. A `CLAUDE.md`
that imports `@AGENTS.md` has any previous complete block removed, then is skipped. Complete
blocks are replaced idempotently, while malformed markers, symlinks, and non-regular files are
left untouched with a warning. `init --overwrite` can replace or remove stale guidance, and `deinit`
removes complete blocks even when the recipe cannot be parsed.

A recipe with no port-bearing app renders nothing and any previous block is removed, so an
empty block is never written and stale guidance never outlives the frameworks it described.
Every sync rewrites the whole span between the sentinels, so a block from an older version is
replaced wholesale rather than migrated.

An instruction file another tool generates is recognized by the generator header that opens it
and is left byte-identical by both sync and deinit, because an edit there would be destroyed on
that tool's next sync. Splashdown reports the marker it recognized and tells the user to add the
block to (or remove it from) the file's source instead. The recognition is generic: a leading
HTML comment that attributes the file to a generator or forbids editing. There is no adapter for
any particular tool, and a file that merely mentions generated content is edited normally.

**Next-step report.** `_print_init_next_steps` (`commands.py`) closes every successful init
path, on both the scanned and the structure-only monorepo route. It states that nothing is
allocated or active yet and names bare `splash` (sync). It names `splash trust` for automatic
post-checkout handling only at a worktree root, offers it for environment-output
authorization in a nested project, and omits it outside a Git checkout, where trust cannot run.
`cmd_init` resolves the worktree root once and passes it in, so the report and the hook
decision cannot disagree.

**Activation.** `cmd_trust` (`commands.py`) is the trusted-activation seam. `_print_trust_preamble`
prints the recipe's bootstrap commands and the trust warning and returns the loader the grant will
approve, so the warning and the later `approve()` share one `_splashdown_owned_loader` lookup. The
warning quotes that loader's `approval_detail`, because the grants differ: mise trusts the config
path permanently, while direnv re-prompts after any later edit. `cmd_trust` then calls
`_activate_post_checkout_hook`, records trust, and runs the loader's `approve()`
(`mise trust` / `direnv allow`) when `Loader.owns_config` reports that the loader file holds
splashdown's integration and nothing else. In a nested project it skips activation and repeats
init's nested `splash --cwd PATH sync` note, since the wrapper it would install belongs to the
worktree root where the nested recipe is invisible.

Activation never rewrites a manager's configuration, which is tracked project content. With no
manager it installs the native wrapper. With one, it runs that manager's own hook installer so
the entry init wrote becomes live:

| Manager | Activation |
| --- | --- |
| lefthook | `lefthook install` |
| pre-commit | `pre-commit install --hook-type post-checkout` |
| prek | `prek install --hook-type post-checkout` |
| simple-git-hooks | the checkout's own `node_modules/.bin/simple-git-hooks`, never a fetch |
| husky | none: verify `core.hooksPath` resolves to an installed `.husky/_` shim |

Each installer is best-effort and prints the command to run by hand when it is unavailable.
Splashdown never installs a hook manager itself, and the simple-git-hooks case deliberately
refuses to reach the network: if the package is not already in the checkout, it says so.

husky is the one manager whose activation installs nothing, because husky's `core.hooksPath` is
the worktree-relative `.husky/_`, which is gitignored. A linked worktree therefore has no husky
shims until the project's own husky install runs there, and splashdown reports husky as
configured but not active in that state rather than claiming the event will arrive.

When the entry is missing or modified, activation prints the manager's own manual instruction:
`post_checkout_manual_instructions` (`hooks.py`) gives lefthook a `run:` line using lefthook's
`{1} {2} {3}` templating; pre-commit and prek an `entry` naming `always_run` and the three
`PRE_COMMIT_*` variables plus the install command; simple-git-hooks a JSON command using
`"$1" "$2" "$3"` plus `npx simple-git-hooks`; overcommit a `PostCheckout` hook; and husky and
the native hook a shell line using `"$1" "$2" "$3"`. A configured `core.hooksPath` and a manager
conflict are repaired by nothing, so their instructions promise nothing. The first sync is an
ordinary bare `splash` / `splash sync` run, or the post-checkout hook on the next worktree.

**Recipe evolution.** Users edit the existing recipe manually or with an agent when apps change.
`init --overwrite` regenerates the whole recipe, replacing manual edits.

**Teardown.** `cmd_deinit` (`commands.py`) reverses the owned parts of init without
blindly restoring user files: it destroys registered sims/AVDs that splashdown owns, releases
all registry rows, removes `splashdown.env`, clears only splashdown keys from user-owned writer
destinations, unwires the configured loader, reverts managed gitignore
and agent-guidance entries, removes an untouched local skeleton, and finally deletes the recipe.
A modified `splashdown.local.toml` is preserved, loader cleanup degrades safely when the recipe
cannot be parsed, and framework edits made by `doctor --fix` are intentionally outside deinit's
scope because they have no reversible sentinel/original snapshot. The shared hook and clone-wide
bootstrap trust remain for sibling worktrees; only this checkout's bootstrap completion is cleared.

## Key entry points

- `cmd_init` — orchestrator and typed refusal guard: `src/splashdown/commands.py`.
- `_print_electron_isolation_pointer`: `src/splashdown/commands.py`.
- `cmd_deinit` — surgical teardown: `src/splashdown/commands.py`.
- `_print_init_next_steps` — the closing report: `src/splashdown/commands.py`.
- `cmd_trust` / `_print_trust_preamble` / `_splashdown_owned_loader` — activation:
  `src/splashdown/commands.py`.
- `_configure_post_checkout_hook` / `_ensure_post_checkout_hook` / `_activate_post_checkout_hook`
  / `_detect_hook_manager` / `_native_hook_path` / `_nested_project`: `src/splashdown/hooks.py`.
- Hook wiring per manager — lefthook / husky / pre-commit / prek / simple-git-hooks /
  native common hook — and the shared
  `POST_CHECKOUT_HOOK` body: `src/splashdown/hooks.py`.
- `_print_env_destination` / `_persisted_env_file` / `_default_destination_keys`:
  `src/splashdown/commands.py`; `validate_env_file_option` / `Recipe.env_file`:
  `src/splashdown/recipe.py`.
- `_ensure_gitignore` / `mise_config_path`: `src/splashdown/hooks.py`.
- `Scanner.scan`: `src/splashdown/scanner.py`; `ProjectInventory` / `AppInventory`:
  `src/splashdown/inventory.py`.
- `_detect_workspace` / `_enumerate_apps` / `select_loader` / `LoaderSelection`:
  `src/splashdown/scanner.py`.
- `_build_resource_catalog` (collision mangling and app references):
  `src/splashdown/scanner.py`.
- `LOADERS` registry, `WirePlan`, and idempotent loader implementations:
  `src/splashdown/loaders.py`.
- `InitReport` / `_commit_loader_plan` / `_emit_init_report` / `_init_effects` /
  `_init_failure`: `src/splashdown/commands.py`.
- `init` argparse parser and dispatch: `src/splashdown/cli.py`.

## Configuration

- **`splash init`** — scan-driven scaffold + project-configuration wiring. It takes no positional argument;
  scanner-driven generation is the only recipe path. Recipes that a scan cannot infer, such as a
  generic `PORT`, a per-checkout Postgres database name, or Electron user-data isolation, are
  documented examples in `docs/user/recipe.md`.
- **`--loader mise|direnv|devbox|none`** — override loader selection
  (`none` = write a dotenv file / print instructions, wire nothing).
- **`--overwrite`** — replace an existing `splashdown.toml` (without it, init exits `2`).
- **`--env-file PATH`** — checkout-relative destination for generated values
  (default `splashdown.env`).
- **Files touched**: `splashdown.toml` (committed recipe), `splashdown.local.toml`
  (gitignored, skeleton), `.gitignore` (+`splashdown.env`, +`splashdown.local.toml`), the
  loader config (`mise.toml`/`.envrc`/`devbox.json`), the project-owned hook target
  (`lefthook.yml` / `.husky/post-checkout` / `.pre-commit-config.yaml` / `prek.toml` /
  `.simple-git-hooks.json` or the `package.json` block), and managed blocks in existing root `AGENTS.md` /
  independent `CLAUDE.md` files. Git's common `hooks/post-checkout` belongs to `splash trust`,
  and `splashdown.env` to the first sync.

## Gotchas

- **UC6 — a teammate cloning a configured repo must opt in.** Git
  does **not** run hooks on `git clone`, and `core.hooksPath` / `.husky` / lefthook wiring is
  local config that a clone does not activate. The registry (`$XDG_STATE_HOME/splashdown/…`)
  and `splashdown.env` are per-machine and never committed. So even when a teammate clones a
  repo that already commits `splashdown.toml`, they get no clone-local trust and no live values.
  After reviewing the recipe, `splash trust` is the lightweight onboarding verb: it grants
  automatic sync, grants bootstrap only when currently declared, activates or verifies the
  local hook (running the project's own hook-manager install where one owns the event), and
  approves loader configuration splashdown
  generated, all without rewriting the recipe. The teammate then runs `splash sync`, or
  `splash bootstrap` when the recipe declares it. This is the same second step a project's own
  author takes after `splash init`.

- **Init usage failures are typed.** The refusal guard raises `UsageError`. The CLI renders
  exit 2; embedded callers can catch the application exception. Argparse still raises
  `SystemExit` for an unrecognized argument before dispatch.

- **Init never reaches the registry or the machine.** It leaves the checkout configured but
  **without** allocated ports, `splashdown.env`, recorded trust, an installed native hook, or an
  approved loader. Anything that looks for live values after a bare `cmd_init` must run a sync
  first, and anything that needs automatic handling must run `splash trust`.

- **The local skeleton is create-only.** Init and sync preserve an existing regular
  `splashdown.local.toml`. A symlink or other non-regular entry is an error, so the automatic
  post-checkout path cannot follow it or replace its target.

- **An unrecognized `core.hooksPath` is intentionally not touched.** If a project points
  `core.hooksPath` anywhere other than husky's `.husky/_`, init only prints a warning and installs
  nothing — the user must wire a trusted absolute executable as
  `splash hook post-checkout "$1" "$2" "$3"` in that hook directory, or run bootstrap manually.
  A sync-only call cannot recognize worktree creation.

- **Conflicting managers are reported, not resolved.** When two managers present the same
  strength of evidence, splashdown names both and writes nothing. Guessing would put the entry in
  a file that never runs.

- **A hook manager's install command is best-effort, and it happens at activation.** Init writes
  only the tracked config entry. `splash trust` (and `doctor --fix`) invoke the manager's own
  installed binary; if it is unavailable or fails, the entry stays **unregistered** until the user
  runs that install command, and a note is printed. simple-git-hooks ships no global executable, so
  only the copy already in `node_modules` is run.

- **Only splashdown-owned loader config is auto-approved, and only at trust.** mise and direnv
  only load a config after `mise trust` / `direnv allow`. `cmd_trust` calls `Loader.approve()`
  only when `Loader.owns_config()` reports that removing splashdown's own directive would leave
  the file empty. It never approves pre-existing or inherited config that may carry the user's
  `[tools]`, `[tasks]`, or `.envrc` commands, and `init`/`sync`/post-checkout never approve
  anything; users must review and trust those files themselves.
  `approve()` never fails the run — a missing `mise`/`direnv` binary, non-zero exit, or timeout
  is swallowed (`loaders.py`, `_run_ok`).

- **Loader selection never consults PATH.** A fresh clone with mise installed but no
  `mise.toml` gets `loader = "none"`, not mise. Adopting an integration the project has not
  chosen is a project decision, not an inference from the developer's machine, so the PATH
  fallback that used to make that choice is gone. The cost is that such a clone writes
  `splashdown.env` with nothing sourcing it until the user passes `--loader`, which init says
  plainly in its no-loader instructions.

- **No loader = silent no-op risk.** Reachable whenever no loader is configured (or
  `--loader none` was passed): sync keeps writing the configured destination and init prints
  how to source it, but nothing sources it automatically (`_print_env_destination` in
  `commands.py`). `--env-file` pointing at a file the app itself reads is the other way out.

- **`profile = "unknown"` apps are skipped, not failed.** An unrecognized framework gets no
  resources and no wiring; the rest of the project still scaffolds
  (`_apply_init_wiring_checks` in `commands.py`).

- **Generated TOML is not trusted implicitly.** Scanner output and the
  minimal-monorepo fallback both pass through `Recipe` before writing. A validation failure leaves
  the destination recipe absent or unchanged and prevents subsequent
  init mutations. Unknown recipe keys are hard errors.

## Why

Onboarding is once-per-project but high-stakes: per the persona, a bad first run equals
abandonment, and the parallel-agent persona needs setup to be zero-touch because an agent
won't run a step it doesn't know about. Folding scan + scaffold + loader + hook configuration +
wiring into one command is what makes "spin up a worktree and it just works" true once the
checkout is activated. A teammate's clone is still different from a linked worktree because trust,
hook activation, and registry state do not travel with Git. `splash trust` makes that difference
an explicit security decision without requiring the teammate to regenerate project configuration.

**Why adoption is two commands.** Everything init writes is project configuration a user can read,
edit, and commit; everything that follows touches the machine — the machine-wide registry, the
local `.git` directory, recorded trust, and the loader's approval database. Splitting them at that
line gives the user a review point before any of it happens, and gives the project's own author the
same second step (`splash trust`) a cloning teammate already took. It also makes the boundary
enforceable: init is dispatched before `Registry` construction, so no init path can reach machine
state at all.
