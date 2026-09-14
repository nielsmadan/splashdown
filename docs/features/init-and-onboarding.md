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
engine as `splash doctor --fix`), and reports the commands that make the checkout ready.

Init is configuration-only. It allocates no resources, writes no environment output, records no
trust, runs no loader approval command, and installs nothing into the local `.git` directory, so
the user can read and edit the generated recipe before anything reaches the machine. Adoption is
two commands: `splash init`, then `splash trust` plus a sync (bare `splash`).

Four options reshape that flow:

- `splash init --loader=mise|direnv|devbox|none` — override loader detection, including
  an explicit no-loader setup.
- `splash init --overwrite` — replace an existing recipe (init refuses otherwise).
- `splash init --electron-profile=isolated|shared` — make the scanner-driven Electron
  profile choice explicit instead of relying on the interactive prompt/default.
- `splash init --ios-scheme=NAME` — select a native iOS Xcode scheme when discovery is
  ambiguous or when init is running non-interactively.

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
matches (`scanner.py`); and detects the shell loader by asking each `Loader` in priority
order mise → direnv → devbox, falling back to the first one installed on PATH and returning
`"none"` only when none is configured or installed (`_detect_loader`,
`scanner.py`). A `--loader` override replaces the detected loader on the inventory
(`commands.py`). The result is a `ProjectInventory` of `AppInventory` entries defined in
`inventory.py`.

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

**Electron overlay.** Scanner-detected Electron apps can add a stable
`ELECTRON_PROFILE_ID` through `_add_electron_resources` (`commands.py`). Interactive
init asks whether to isolate the profile; non-interactive/EOF defaults to shared. The
`--electron-profile=isolated|shared` flag makes the choice deterministic. Isolation retains
the app's primary Profile and adds a `writer = "splashdown-env"` template resource whose value
the Electron main process uses to derive a per-checkout `userData` directory.

**No-loader fallback.** When the loader is `"none"`, `_apply_no_loader_fallback`
(`commands.py`) decides delivery via `_resolve_no_loader_delivery` (`commands.py`):
if a dotenv file the project already reads exists (`.env` → `.env.local` precedence) **and**
at least one app actually reads dotenv files (`Profile.reads_dotenv`), it uses `setdefault`
to add `writer = "envfile=<file>"` only to generated resources without an explicit writer.
That preserves Electron's `writer = "splashdown-env"` process-env delivery. Otherwise it
keeps generating `splashdown.env` and prints instructions. It warns when the chosen dotenv
file is not gitignored, and notes any apps that read only the process environment
(Vite/Spring/mobile/Electron) and therefore won't pick up a dotenv file.

**Native iOS scheme.** `_resolve_init_ios_scheme` (`commands.py`) discovers shared
Xcode schemes for a scanner-detected `ios-native` app. One shared scheme is recorded
automatically as `[project.ios].scheme`; several produce a TTY prompt, while ambiguous
non-interactive init errors with a direct `--ios-scheme=NAME` retry. An explicit name is
validated and recorded without discovery.

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
`splashdown.env` and `splashdown.local.toml` to `.gitignore`. The selected loader is wired by
`LOADERS[inv.loader].wire(cwd)` (`commands.py`) — every loader's `wire` is idempotent
(`loaders.py`): mise sets `_.file = "splashdown.env"` under `[env]` (editing an existing
`.mise.toml`/`mise.toml` rather than scaffolding a second), direnv appends a sentinel-wrapped
`dotenv_if_exists splashdown.env` block to `.envrc`, devbox adds a marker-tagged `init_hook`,
and `none` wires nothing. Init never runs `mise trust` or `direnv allow`; approval is an
activation effect owned by `cmd_trust` — see the trust-approval note below.

**Git hook configuration.** `_configure_post_checkout_hook` (`hooks.py`) writes the
project-owned configuration that forwards Git's event arguments to Splashdown on later checkout
and worktree transitions. `_detect_hook_manager` (`hooks.py`) classifies the project's existing
setup as `lefthook` / `husky` / `core-hookspath-other` / `none`, and splashdown **coexists**
rather than clobbers:

- **lefthook** → idempotently add a `post-checkout.commands.splashdown` entry that forwards
  `{1} {2} {3}` to the tracked lefthook config. `lefthook install` is local activation and is not
  run by init. Project-controlled `yarn` or `npx` commands are never executed.
- **husky** → drop a `.husky/post-checkout` hook, which is a tracked project file.
- **any configured `core.hooksPath`** → do **not** touch it; print a warning telling the
  user to invoke a trusted absolute `splash` executable with the post-checkout event arguments,
  or run bootstrap manually.
- **none** → there is no project file to write. The native hook lives under Git's common hooks
  directory, which is local state, so init only notes that `splash trust` installs it. Outside a
  Git checkout there is no such hook, and init notes that instead of naming `splash trust`.
  Splashdown never changes `core.hooksPath`, and the common hook is shared by all worktrees.
  The hook body is `POST_CHECKOUT_HOOK` (`hooks.py`): Git already starts it at the repo top, so it
  no-ops if `splashdown.toml` is absent, resolves `splash` once, rejects an executable inside the
  checkout, and invokes the internal event handler once with all three Git arguments. A missing
  executable prints a note. The wrapper absorbs the handler's failure after diagnostics.

`_ensure_post_checkout_hook` is the fused configure-plus-install variant, reached only from
`doctor --fix` through `_autofix_ensure_post_checkout_hook` (`wiring.py`).

**Wiring checks.** For each known-profile app, `cmd_init` runs the profile's `wiring_checks`,
and for any check whose `detect` is not `"ok"` it applies the `autofix` if one exists, swallowing
failures with a printed `✗` line (`commands.py`). This is the same `WiringCheck`
machinery as `splash doctor` (see UC5 / `wiring.py`), minus the checks marked `activation`:
`_apply_init_wiring_checks` skips those, so the `hook` check the mobile profiles carry cannot
install the local hook init just said `splash trust` owns.

**Agent guidance.** After the generated recipe validates, every init path parses that recipe
and derives a sentinel-wrapped Markdown block for port-bearing apps. The block uses each
`[apps.*].resources` entry's actual resolved name and combines common
no-numeric-port rules with `Profile.agent_guidance()` launch instructions. Existing root
`AGENTS.md` and independent `CLAUDE.md` files are updated; neither is created. A `CLAUDE.md`
that imports `@AGENTS.md` has any previous complete block removed, then is skipped. Complete
blocks are replaced idempotently, while malformed markers, symlinks, and non-regular files are
left untouched with a warning. `init --overwrite` can replace or remove stale guidance, and `deinit`
removes complete blocks even when the recipe cannot be parsed.

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
`_activate_post_checkout_hook` (which installs the native wrapper or runs `lefthook install`),
records trust, and runs the loader's `approve()` (`mise trust` / `direnv allow`) when
`Loader.owns_config` reports that the loader file holds splashdown's integration and nothing else.
In a nested project it skips activation and repeats init's nested `splash --cwd PATH sync` note,
since the wrapper it would install belongs to the worktree root where the nested recipe is
invisible. The first sync is an ordinary bare `splash` / `splash sync` run, or the post-checkout
hook on the next worktree.

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
- `_add_electron_resources` / `_resolve_init_ios_scheme`: `src/splashdown/commands.py`.
- `cmd_deinit` — surgical teardown: `src/splashdown/commands.py`.
- `_print_init_next_steps` — the closing report: `src/splashdown/commands.py`.
- `cmd_trust` / `_print_trust_preamble` / `_splashdown_owned_loader` — activation:
  `src/splashdown/commands.py`.
- `_configure_post_checkout_hook` / `_ensure_post_checkout_hook` / `_activate_post_checkout_hook`
  / `_detect_hook_manager` / `_native_hook_path` / `_nested_project`: `src/splashdown/hooks.py`.
- Hook wiring per manager — lefthook/husky/native common hook — and the shared
  `POST_CHECKOUT_HOOK` body: `src/splashdown/hooks.py`.
- `_apply_no_loader_fallback` / `_resolve_no_loader_delivery`: `src/splashdown/commands.py`.
- `_ensure_gitignore` / `_ensure_mise_file_directive`: `src/splashdown/hooks.py`.
- `Scanner.scan`: `src/splashdown/scanner.py`; `ProjectInventory` / `AppInventory`:
  `src/splashdown/inventory.py`.
- `_detect_workspace` / `_enumerate_apps` / `_detect_loader`: `src/splashdown/scanner.py`.
- `_build_resource_catalog` (collision mangling and app references):
  `src/splashdown/scanner.py`.
- `LOADERS` registry and idempotent loader implementations: `src/splashdown/loaders.py`.
- `init` argparse parser and dispatch: `src/splashdown/cli.py`.

## Configuration

- **`splash init`** — scan-driven scaffold + project-configuration wiring. It takes no positional argument;
  scanner-driven generation is the only recipe path. Recipes that a scan cannot infer, such as a
  generic `PORT`, a per-checkout Postgres database name, or Electron user-data isolation, are
  documented examples in `docs/user/recipe.md`.
- **`--loader mise|direnv|devbox|none`** — override loader auto-detection
  (`none` = write a dotenv file / print instructions, wire nothing).
- **`--overwrite`** — replace an existing `splashdown.toml` (without it, init exits `2`).
- **`--electron-profile=isolated|shared`** — scanner-only Electron choice. `isolated` adds a
  stable process-env profile id; `shared` explicitly declines isolation.
- **`--ios-scheme=NAME`** — scanner-only native iOS scheme override; required for ambiguous
  non-interactive discovery.
- **Files touched**: `splashdown.toml` (committed recipe), `splashdown.local.toml`
  (gitignored, skeleton), `.gitignore` (+`splashdown.env`, +`splashdown.local.toml`), the
  loader config (`mise.toml`/`.envrc`/`devbox.json`), the project-owned hook target
  (`lefthook.yml` / `.husky/post-checkout`), and managed blocks in existing root `AGENTS.md` /
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
  local hook (including `lefthook install`), and approves loader configuration splashdown
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

- **Any configured `core.hooksPath` is intentionally not touched.** If a project sets
  `core.hooksPath`, init only prints a warning and installs nothing — the user must wire
  a trusted absolute executable as `splash hook post-checkout "$1" "$2" "$3"` in that hook
  directory, or run bootstrap manually. A sync-only call cannot recognize worktree creation.

- **`lefthook install` is best-effort, and it happens at activation.** Init writes only the
  tracked config entry. `splash trust` (and `doctor --fix`) invoke an installed `lefthook`
  binary; if it is unavailable or fails, the entry stays **unregistered** until the user runs
  `lefthook install`, and a note is printed.

- **Only splashdown-owned loader config is auto-approved, and only at trust.** mise and direnv
  only load a config after `mise trust` / `direnv allow`. `cmd_trust` calls `Loader.approve()`
  only when `Loader.owns_config()` reports that removing splashdown's own directive would leave
  the file empty. It never approves pre-existing or inherited config that may carry the user's
  `[tools]`, `[tasks]`, or `.envrc` commands, and `init`/`sync`/post-checkout never approve
  anything; users must review and trust those files themselves.
  `approve()` never fails the run — a missing `mise`/`direnv` binary, non-zero exit, or timeout
  is swallowed (`loaders.py`, `_run_ok`).

- **Loader detection falls back to PATH.** `_detect_loader` (`scanner.py`) first asks each
  `Loader.detect()` whether the repo carries its config (`mise.toml`, `.envrc`, `devbox.json`),
  and a repo-level config always wins. Failing that it checks whether the binary is installed
  (`_loader_on_path` → `shutil.which`) in mise → direnv → devbox order and wires the first hit,
  because a fresh clone has no config file yet and writing `splashdown.env` with nothing to
  source it is a silent no-op. `--loader none` is the explicit opt-out. `Loader.wire()` already
  handles the create-from-nothing case, so no separate scaffolding path is needed.

- **No-loader + process-only apps = silent no-op risk.** Only reachable now when *no* loader is
  installed at all (or `--loader none` was passed) and the only apps read env from the process
  (Vite, Spring Boot, mobile) rather than a dotenv file: sync keeps writing
  `splashdown.env` and init prints how to source it, but nothing sources it automatically
  (`_resolve_no_loader_delivery` and `_NO_LOADER_INSTRUCTIONS` in `commands.py`).

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
