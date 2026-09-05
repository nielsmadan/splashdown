# CLI and Commands

How a `splash` invocation gets from `argv` to a handler: argument parsing and dispatch (`cli.py`),
checkout orchestration (`commands.py`), target orchestration (`target_commands.py`), target catalog
edits (`targets.py`), physical allocation (`device_claims.py`), post-checkout integration
(`hooks.py`), and fail-silent shell completion
(`completion.py`). Framework launch dispatch lives in `launching.py`; `doctor.py` orchestrates
checks defined in `wiring.py`. Typed status gathering lives in `status.py`, while
`cli_output.py` owns operational text, JSON, and application-error rendering.

For the *user-facing* contract of each command, see the PRD docs cross-linked under [Related](#related). This doc covers the internals — the parser quirks, the dispatch table, and how the handlers compose the lower-level modules.

## Contents

- [Purpose](#purpose)
- [How it works (current state)](#how-it-works-current-state)
  - [`cli.py` — parse and dispatch](#clipy--parse-and-dispatch)
    - [`main()` flow](#main-flow)
    - [`_ensure_subcommand` — bare `splash` defaults to `sync`](#_ensure_subcommand--bare-splash-defaults-to-sync)
    - [`KNOWN_CMDS` and the parser](#known_cmds-and-the-parser)
    - [Parsed-argument validation](#parsed-argument-validation)
    - [Tiered `--help`: `_EpilogOnlyFormatter`](#tiered---help-_epilogonlyformatter)
    - [Lazy `--version`: `_VersionAction`](#lazy---version-_versionaction)
    - [The run/start/stop/destroy parser loop](#the-runstartstopdestroy-parser-loop)
    - [`_normalize_device_args`](#_normalize_device_args)
    - [Top-level exception handler](#top-level-exception-handler)
  - [`commands.py` — the orchestration layer](#commandspy--the-orchestration-layer)
    - [Provision handlers (`sync` / `init`)](#provision-handlers-sync--init)
    - [`deinit` teardown](#deinit-teardown)
    - [Git post-checkout hook installation](#git-post-checkout-hook-installation)
    - [Status reporting](#status-reporting)
    - [The no-loader delivery fallback](#the-no-loader-delivery-fallback)
    - [`_confirm` and typed usage failures](#_confirm-and-typed-usage-failures)
    - [Device lifecycle handlers](#device-lifecycle-handlers)
    - [`target` and `env` dispatchers](#target-and-env-dispatchers)
  - [`completion.py` — fail-silent completers](#completionpy--fail-silent-completers)
- [Key entry points](#key-entry-points)
- [Gotchas](#gotchas)
- [Why](#why)
- [Related](#related)

## Purpose

`cli.py` is the entry point: it builds a single flat argparse parser, defaults a bare invocation to
`sync` (so the git hook can call `splash` with no arguments), and dispatches each subcommand to a
handler. `commands.py` owns trust/bootstrap, init, sync, deinit, and env orchestration. `status.py`
owns status report construction and `cli_output.py` owns rendering. `target_commands.py`
owns run/start/stop/destroy, fleet maintenance, and the nested target dispatcher; `targets.py` owns
local/global catalog edits. `hooks.py` owns post-checkout
installation and coexistence with other hook managers. `completion.py` provides the argcomplete
completers, which must never raise or print because they run on every `<Tab>`.

The package is built for a fast hot path: the git post-checkout event handler must reach trust
checking and, for an authorized clone, `provision()` cheaply. That goal shapes several decisions
below — lazy version resolution, lazy completion install, lazy Registry construction, and lazy
submodule imports inside handlers.

## How it works (current state)

### `cli.py` — parse and dispatch

#### `main()` flow

`main()` (`cli.py`) is the whole control flow:

1. Default `argv` to `sys.argv[1:]`, then run it through `_ensure_subcommand` (`cli.py`) to inject a `sync` token if no subcommand is present.
2. Build the parser (`_build_parser`, `cli.py`).
3. Install completion (`cli.py`) — imported lazily, immediately before `parse_args`, because during an active completion argcomplete parses `COMP_LINE` itself and exits inside `parse_args` (see [completion](#completionpy--fail-silent-completers)).
4. `parse_args`, validate cross-option contracts, dispatch completion before checkout resolution,
   then resolve `cwd` (`_resolve_cwd`, honours `--cwd`, else `$PWD`, always `.resolve()`d).
5. Dispatch the hidden hook event before constructing a Registry. Handle `init` inside the ordinary
   error renderer but before Registry construction, so rejected, rescanned, and `--no-sync` init
   paths do not touch machine-wide registry state or output writers.
6. A successful init that proceeds to sync constructs the shared `Registry`, consumes pending
   physical-claim notices, and provisions. Every other checkout command constructs the Registry
   and consumes notices before dispatching trust, untrust, bootstrap, or the ordinary flat command
   table. The final fall-through is `sync` (the default), so both bare `splash` and explicit
   `splash sync` land on `_cmd_provision`.

Completion generation, help, version, active argcomplete, hidden hook plumbing, and init paths that
do not sync all exit before Registry construction and therefore do not consume pending claim
notices. Other checkout-scoped commands consume notices before their handler runs, even when that
handler later fails. `render_claim_notices` in `cli_output.py` names the target, transfer or
forced-release action, actor checkout, and event time.

The handler signature shows the orchestration boundary: `main()` resolves `cwd`, creates a Registry
only when needed, and threads dependencies into handlers. Each branch returns the process exit code.

#### `_ensure_subcommand` — bare `splash` defaults to `sync`

`_ensure_subcommand` (`cli.py`) makes `splash` (no subcommand) behave as `splash sync`. The
post-checkout hook uses the explicit hidden event command instead. The helper cannot just prepend
`sync`, because top-level flags must still parse at the root parser level — `splash --cwd /path`
has to become `splash --cwd /path sync`, not `splash sync --cwd /path` (which would fail, since
`sync` has no `--cwd`).

The walk bails early for help/version, then skips root flags before deciding where to inject
`sync`. `--cwd`/`--format` consume a value; `--show-values` is the root boolean flag. Keeping both
sets explicit makes bare `splash --show-values` parse as a sync rather than as a sync-subparser
option.

#### `KNOWN_CMDS` and the parser

`KNOWN_CMDS` (`cli.py`) is the hand-maintained set of subcommand names. It exists only so
`_ensure_subcommand` can decide whether a subcommand is already present *before* argparse runs. It
is a second source of truth alongside the `sub.add_parser(...)` calls, guarded by an exact
parser-choice invariant. The root-help contract separately requires every public command except
the internal `hook` event to appear in the curated map.

`_build_parser` (`cli.py`) is a single flat parser with one block per subcommand. Every subparser is hidden
from argparse's generated list because the curated epilog carries the task-oriented overview.
Root flags are `--cwd`, `--format`, `--show-values`, and `--version`.

#### Parsed-argument validation

`_validate_parsed_args` runs immediately after argparse and before checkout resolution, registry
construction, or command dispatch. It owns constraints argparse cannot express cleanly across
parser levels: `init --rescan` exclusivity, the root output-option support matrix, and the
redundant `target remove --global --keep-instance` pair. It also rejects
`target remove device --keep-instance`, because physical devices have no owned instance. `--format`
is valid for sync, status, bare env, bare target, target claims, and target claim. `--show-values`
is valid for sync, status, normal init, and bare env. Rejected combinations use `parser.error`,
preserving argparse's usage output and exit 2.

The env parent and action parsers intentionally accept `--checkout`. Action defaults use
`argparse.SUPPRESS`, so omitting the after-action form does not overwrite a selector already parsed
before the action. When both root `--cwd` and env `--checkout` are present, the env selector wins.

#### Tiered `--help`: `_EpilogOnlyFormatter`

`_EpilogOnlyFormatter` (`cli.py`) is a `RawDescriptionHelpFormatter` subclass whose `_format_action` returns an empty string for the subparsers action (`argparse._SubParsersAction`, a private type argparse exposes no public name for). That suppresses argparse's flat `{sync,init,env,…}` dump. The actual command overview is the epilog (`_HELP_EPILOG`), hand-grouped into tiers — "Run on a device", "This checkout", "Set up a project", "More" — so `splash --help` reads as a task-oriented menu rather than an alphabetical list.

#### Lazy `--version`: `_VersionAction`

`_VersionAction` reimplements argparse's built-in version action so the version string is resolved
*only* when `--version` is actually passed. Its `__call__` lazy-imports
`_version.resolve_version` and prints it. The motivation is the hot path:
`importlib.metadata.version(...)` costs ~20ms, which every hook-triggered Splashdown process would
otherwise pay for a string it never prints.

#### The run/start/stop/destroy parser loop

The four device verbs share one parser shape, built in a loop in `cli.py`:

- Each gets an optional positional `dtype` (`TYPE`) and an optional positional `variant`.
- **Crucially, `dtype` has no argparse `choices`** (`cli.py`). This is intentional: with `choices=TARGET_TYPES`, a lone variant token like `splash run small-screen` would be rejected as an invalid TYPE. Dropping `choices` lets that token land in the `dtype` slot, to be re-interpreted by `_normalize_device_args` after parsing.
- The two completers are attached here: `device_arg_completer` on `dtype`, `variant_completer` on `variant`.
- **`--yes` is added only to `destroy`** (`cli.py`) — it is the only one of the four that is destructive (deletes the sim/AVD), so it is the only one with a confirmation prompt to skip. `run`/`start`/`stop` never prompt.

#### `_normalize_device_args`

`_normalize_device_args` (`cli.py`) cleans up after the choice-less `dtype` slot. First, when `prefix_match` is enabled (the default; resolved via `load_settings(_resolve_cwd(args))`), a non-canonical `dtype` token is expanded by `_match_target_type_prefix` from `targets.py` against the types the checkout *declares* (`_declared_target_types`) — `sim` → `simulator`. Scoping to declared types means a short token never gets claimed by an undeclared type: `splash run d` in a sim-only project does *not* become `device`; it stays a variant prefix. If `dtype` still holds a non-type token and `variant` is empty, it shifts it over: `dtype, variant = None, dtype` (so an abbreviated *variant* falls through to the variant slot, where `resolve_variant` does its own prefix matching). Then it validates — anything still sitting in `dtype` that isn't a real `TARGET_TYPES` member raises `DeviceError`. Type names win over equally-named variants, and a type prefix wins over an identically-prefixed variant (see [Gotchas](#gotchas)). It is called from `main()` only for the four device verbs.

#### Top-level exception handler

Ordinary dispatch has one application-error renderer. `ApplicationError` carries
an exit code and whether the message receives an `error:` prefix; `UsageError`,
`MissingRecipeError`, and `SetupError` model exit-2 usage failures, the hook-compatible exit-0
missing-recipe notice, and setup failures. `DeviceError` and configuration `ValueError` enter the
same renderer as exit-1 failures. These handlers raise rather than terminating the process, so
direct callers can handle failures and CLI output is emitted exactly once.

The hidden post-checkout event is dispatched before Registry construction and before that ordinary
renderer. Init remains inside the renderer but runs before Registry construction so its refusal
guards have no machine-state side effects. Trust, untrust, and bootstrap retain command-specific
output and retry handling, but run after the shared Registry has consumed any pending claim notice.
New registry-backed commands belong inside the shared boundary; changes to the early
security-sensitive hook and init paths must preserve their explicit rendering contracts.

### `commands.py` — the orchestration layer

This module spans onboarding, provisioning orchestration, and env dispatch. Target
lifecycle and fleet operations live in `target_commands.py`; hook wiring lives in `hooks.py`, while
doctor orchestration lives in `doctor.py`. Status and output formatting have their own modules.

#### Provision handlers (`sync` / `init`)

`_cmd_provision` is a thin shim over `_cmd_provision_inner`, the shared engine for both
`splash sync` and the tail of `splash init`.

`_cmd_provision_inner` snapshots `registry.all_for(abspath)` *before*
provisioning so it can report only what changed, calls `provision()`
(`provisioning.py`), then calls `write_outputs()` while the checkout operation lock is held.
`run_setup()` runs after release. `render_sync` owns all text/JSON output. A missing recipe is
translated to `MissingRecipeError`, which the CLI renders with the hook-compatible exit 0.

`provision()` begins with `Recipe.load`, which validates the complete document
and preflights templates before any registry allocation or writer mutation.
Malformed recipe sections, apps, resources, setups, targets, writers, template
syntax/references, and dependency cycles therefore become `error:` + exit 1
without partial provisioning. Setup *execution* remains later: an unknown
requested setup name or a failing command can occur after registry and writer
changes and is not transactional. The renderer chooses the no-op or per-line report. JSON exposes
`resolved_keys` by default; `--show-values` opts into `resolved`. Explicit stdout-writer values are
always placed in the JSON `stdout` object. In text mode, `--show-values` prints every sorted
resolved `KEY=VALUE` line, annotating changed keys. This applies equally to normal sync, an
up-to-date no-op sync, and init's first sync.

`cmd_init` applies the same contract to generated TOML. Scanner recipes,
minimal-monorepo recipes, and built-in presets go through `Recipe.parse` before
the recipe path is written. `cmd_refresh_inventory` first loads the existing
recipe through `Recipe.load`, then validates the fully rebuilt document before
replacing it. Invalid fields cannot be erased by the rewrite, and preserved
stale fields abort the rescan instead of being blessed. This keeps
generator/profile/loader drift from producing a file that the next sync cannot
load. Every generated-recipe write uses same-directory atomic replacement, preserving an existing
regular file's mode while replacing its directory entry. Symlinks and non-regular entries are
rejected; hardlinks are safely broken rather than truncating their shared inode.

`cmd_init` orchestrates scan → scaffold recipe → local skeleton → gitignore → loader → hook →
sync-only clone trust → framework wiring. For an explicitly allowed nested project, the hook step
prints a manual nested sync command instead because Git invokes checkout hooks from the worktree
root. An intent preset short-circuits to `_cmd_init_preset`.
Refusal and invalid-preset paths raise `UsageError`; `main()` renders them and returns exit 2. The
first sync runs after init unless `--no-sync`; `--rescan` diverts to `cmd_refresh_inventory` and is
exclusive with every scaffold/scan option. Init
never grants bootstrap trust.

#### `deinit` teardown

`cmd_deinit` is the reverse-orchestration path for state splashdown
owns or marks explicitly. It reads the recipe before deleting it so it can discover the
loader and writer destinations, but a malformed recipe only disables those recipe-dependent
steps; it does not block the rest of teardown.

The handler destroys every registered simulator/emulator for the checkout (hardware rows are
not owned), releases all remaining registry rows including physical claims and addressed notices,
removes the wholly-owned `splashdown.env`,
and asks `clear_writer_destinations` to remove only splashdown keys from user-owned
`envfile=`/`envrc` outputs. It then calls the loader's `unwire`, reverts splashdown's gitignore
entries and agent-guidance block,
and removes `splashdown.local.toml` only when it still equals `LOCAL_SKELETON`. A modified
local file is preserved with a note; `splashdown.toml` is deleted last. Framework files
patched by `doctor --fix` are outside this reversal because they have no sentinels or saved
originals. Clone-wide trust and the shared hook remain; checkout completion is removed.

#### Git post-checkout hook installation

`hooks.py` owns post-checkout integration. `_ensure_post_checkout_hook` wires the internal
post-checkout event command while *coexisting* with whatever hook manager the project already uses, rather than
clobbering it. `_detect_hook_manager` classifies the project into one of four cases, in priority
order:

1. **`lefthook`** — a `lefthook.{yml,yaml}`/`.lefthook.yml` file exists, or `lefthook` is a (dev)dependency in `package.json`. `_wire_post_checkout_lefthook` idempotently injects a `post-checkout.commands.splashdown` job that forwards `{1} {2} {3}`, then best-effort runs the installed `lefthook` binary. It never executes project-controlled `yarn` or `npx` commands during init.
2. **`husky`** — a `.husky/` directory exists. `_wire_post_checkout_husky` drops a `.husky/post-checkout` script using the shared `POST_CHECKOUT_HOOK` body and makes it executable.
3. **`core-hookspath-other`** — `git config core.hooksPath` is set to any nonempty value. Splashdown refuses to take over that hooks directory: it prints event-forwarding instructions using a trusted absolute executable and wires nothing.
4. **`none`** — `_wire_post_checkout_native` writes `post-checkout` under Git's common hooks directory. That location is shared by all worktrees, and splashdown never changes `core.hooksPath`.

The shared `POST_CHECKOUT_HOOK` script is defensive: Git supplies the checkout root as its working
directory, the script exits 0 if there is no `splashdown.toml`, resolves `splash` once, rejects a
resolved executable inside the checkout, and forwards Git's three arguments to one hidden event
command. It absorbs the handler's failure so Git checkout itself succeeds. There is no feature
probe or older-binary fallback. Trust activates only local state; tracked Lefthook/Husky migration
belongs to init/doctor.

Hook readiness is a single `HookReadiness` policy in `hooks.py`, shared by doctor detection and
trust activation. Native and Husky hooks must exactly match the owned event-aware body and be
executable. Lefthook must contain the exact event-aware run value. A custom or modified form is
reported as unverifiable rather than green. `splash doctor --fix` can add the project-level hook
check for a recipe with `[bootstrap]` even when framework detection fails, so minimal and generic
projects have the same migration path.

The hidden handler checks the lifecycle recursion marker, takes the private checkout lock, and
loads one recipe snapshot. It then takes shared clone trust. Without sync trust it constructs no
Registry and writes nothing. With sync trust it provisions output; bootstrap additionally requires
bootstrap trust, a validated linked-worktree creation event, and no completion marker.

For the same validated linked-worktree creation event, the strict
`[project.worktree] claim_device = "ios" | "android" | "any"` policy runs after provisioning and
after a successful trusted bootstrap or an existing completion marker. It takes the checkout
operation lock and calls `claim_available_target` with a five-second total discovery budget. No
configured/free match, missing platform capability, and discovery timeout print the exact manual
`splash target claim --available PLATFORM` retry and remain non-fatal. Primary-checkout and
ordinary branch/file events never enter this path. The outer generated hook still absorbs handler
failure so Git's completed worktree operation returns success.

Git and hook-manager subprocesses are optional integration probes. Missing and non-executable
tools fall back to detection results or a setup note rather than escaping as Python exceptions.

#### Status reporting

`cmd_status` is a thin compatibility wrapper around `status.build_status_report` and
`cli_output.render_status`. The typed report builder owns registry/config/device reads, health
counters, latest-OS caching, and deduplicated capability warnings. The renderer owns compact tables,
detailed text blocks, JSON shaping, cleanup hints, and value redaction. Resource values are omitted
unless `--show-values` is set; JSON format alone is never a disclosure opt-in.

`port_inspection.listening_processes` supplies one optional, three-second `lsof` snapshot for
bound ports across a detailed report. Records carry PID/command pairs; a failed or incomplete
snapshot leaves owners unknown. Compact fleet output and reports without bound ports avoid this
query. The renderer adds `owners` only to JSON port records and keeps the existing port-state
strings separate from owner details.

Detailed checkout records also carry an `AutomationStatus`. For live Git checkouts,
`bootstrap.git_dirs` locates clone-wide trust and checkout-local completion state; the current
recipe supplies bootstrap declaration. Completion is modeled as `not-declared`, `pending`,
`complete`, or `invalid`, so a corrupt marker is visible without turning status into a bootstrap
attempt. If one live recipe cannot be read, that checkout retains trust data but uses a nullable
bootstrap declaration plus `unavailable` completion, and gathering warns without aborting the
remaining detailed records. Non-Git and defunct records use `None`, rendered as JSON `null`.
Compact text `status all` returns from the table-row path before automation gathering, preserving
its no-extra-Git-probe contract. JSON, `status all --verbose`, and `status all` with
`--show-values` use detailed records and include automation state.

#### The no-loader delivery fallback

When the scanner detects no shell-env loader (`inv.loader == "none"`), `splashdown.env`
would be written but nothing would source it. `_apply_no_loader_fallback` handles this
during `init`: `_resolve_no_loader_delivery` decides whether to route values into an
existing `.env`/`.env.local` when at least one Profile reads dotenv. It adds an
`envfile=<name>` writer only to resources that do not already declare one, so a capability
overlay such as Electron can retain process-env delivery independently of the primary
Profile. If no dotenv target fits, it prints `_NO_LOADER_INSTRUCTIONS`, telling the user
to install a loader or source the file manually. It also warns when the chosen target is
not gitignored.

#### `_confirm` and typed usage failures

`_confirm` in `target_commands.py` is the shared interactive `[y/N]` gate for `cmd_destroy` and
`cmd_target_prune`. `yes=True` (from `--yes`) skips the prompt and returns `True`.

Init refusal, invalid presets, and invalid `env set` inputs raise `UsageError`. The CLI's shared
renderer prints the message and returns exit 2; no application handler calls `sys.exit`.

#### Device lifecycle handlers

`target_commands.py` owns `cmd_run`/`cmd_start`/`cmd_stop`/`cmd_destroy`. They share a prelude:
`_infer_dtype` first resolves an exact variant name that occurs under one type in the merged
recipe/local/global catalog. A cross-type duplicate is an error. Without an exact variant match,
it resolves an omitted TYPE to the single project-declared target type (falling back to global only
when the project declares none). `_resolve_variant_for_cli` then loads the full catalog and picks
the variant. Each calls `devices.py` for target reconciliation and boot, then
`launching.py` for framework preflight and final app dispatch. The target subcommand machinery
iterates registry device rows and reconciles them against live sims/AVDs.

Physical `cmd_run` has a claim gate between launcher validation and framework dispatch. It resolves
the configured physical target, takes one discovery snapshot, and calls `attempt_claim` while the
checkout operation lock is held. Busy, disconnected, or ambiguous targets raise before any build
or installation. An existing same-checkout claim is reused. The operation lock is released before
`device_run`, and no launch return path releases the claim, including nonzero launch results.
Physical `cmd_stop` and `cmd_destroy` remain hardware no-ops and do not release ownership.

Before that gate, `cmd_run` resolves recipe resources and refreshes file writers under the same
operation lock. It overlays resources on a copy of the ambient environment and passes it through
`device_run` to built-in or custom launchers. Physical destinations receive advisory network
checks from `runtime_checks.py` before dispatch. Run does not execute setup commands.

Explicit platform operations propagate `CapabilityError`. The dispatcher sets `skip_unavailable`
only for the `all` scope, so unscoped `target refresh` and `target prune` warn once and continue the
other platform while `target refresh ios` or `target prune ios` returns exit 1. GC performs its own
capability-aware device sweep, preserves skipped rows, then calls
`Registry.gc(include_devices=False)` so dead port/key rows are still cleaned without erasing device
work that could not be attempted.

#### `target` and `env` dispatchers

The `target` and `env` subcommands have their own nested subparser actions, so
they get sub-dispatchers rather than a single handler: `_target_dispatch` and
`_env_dispatch`. The target dispatcher lives in `target_commands.py`; env dispatch remains in
`commands.py`. Both treat a bare invocation (`splash target` / `splash env`)
as "list" (mirroring bare `splash` → sync). `_target_dispatch` routes to focused
add/remove/refresh/prune/claim/claims/release handlers and receives the registry constructed by
`main()`, so every registry-using target handler shares the composition-root dependency.

The claim parser requires exactly one specific `VARIANT` or `--available ios|android|any`; `--force`
is specific-only. The release parser likewise requires exactly one `VARIANT` or `--all`, with
`--force` specific-only. Claim and release orchestration stays inside the checkout operation lock,
including notice persistence and output. The claims-file transaction completes before a forced
notice write, so claims and notices locks never nest.

`cmd_target_claims` renders `Registry.all_claims()` without device discovery. Text generic
allocation prints only the selected variant to stdout for shell capture, while a specific claim's
diagnostic goes to stderr. JSON selection includes source, platform, hardware ID, canonical owner,
claim time, and claimed/owned status. `render_target_inventory` abbreviates owner paths only in
text; JSON always retains canonical paths.

`target add` validates its CLI field map with the same `validate_target_spec`
used by recipe, local, and global loads. Flags incompatible with the chosen type
raise `DeviceError` before rendering; the complete edited `LocalConfig` or `GlobalConfig` is
then parsed before writing. Local add/remove reopen `splashdown.local.toml` through `safe_files.py`,
which rejects symlinked components and non-regular destinations before lifecycle mutation, then
uses same-directory atomic replacement with mode preservation. The `env set` branch accepts only declared
`type="set"` resources: assignment, recipe, declaration, and type failures
return exit 2 without mutating the registry.

### `completion.py` — fail-silent completers

The completers run on every `<Tab>`, so the module's contract is: **never raise, never print**. Both completers wrap their body in `except Exception: return []` (`completion.py`) — a malformed recipe or a collision yields no suggestions rather than a traceback that would corrupt the shell line.

- `variant_completer` (`completion.py`) offers variant names for the typed-or-inferred type (slot 2).
- `device_arg_completer` (`completion.py`) offers declared type names *plus* variant names when exactly one type is declared (slot 1), so `splash run <TAB>` suggests variants in the common single-type case.
- `physical_variant_completer` offers configured physical variants without discovery or registry
  reads, and `available_platform_completer` offers the fixed `ios`, `android`, and `any` filters.
- Both share `_catalog` (`completion.py`), which mirrors `cli._resolve_cwd` (honour an already-typed `--cwd`, else `$PWD`, then `.resolve()`).

Completion reads declared recipe/local/global variants, not registry instances. `stop` and
`destroy` can therefore suggest a declared variant that has not been provisioned yet. Slot-one
variant suggestions use project-declared types first, so an always-available global physical
device does not hide simulator variants in a simulator-only project.

`install` (`completion.py`) is a no-op — and imports nothing — unless `_ARGCOMPLETE` is in the environment, so the normal CLI and hook paths pay zero cost. Only an active completion triggers the `import argcomplete` + `autocomplete()`. This is why `main()` calls `install` immediately before `parse_args`: `autocomplete()` parses `COMP_LINE` itself and exits the process before `parse_args` ever returns.

## Key entry points

- `main()` — process entry / dispatch table — `cli.py`
- `_ensure_subcommand` — bare-`splash`-defaults-to-`sync` rewrite — `cli.py`
- `_build_parser` — the single flat parser — `cli.py`
- `_EpilogOnlyFormatter` / `_VersionAction` — help and lazy version presentation — `cli.py`
- `_normalize_device_args` — re-interpret the choice-less `dtype` slot — `cli.py`
- `_cmd_provision_inner` — shared `sync`/`init` provisioning engine — `commands.py`
- `cmd_trust` / `cmd_untrust` / `cmd_bootstrap` / `cmd_post_checkout_hook` — trust and bootstrap orchestration — `commands.py`
- `cmd_init` / `cmd_deinit` — onboarding and teardown orchestration — `commands.py`
- `_ensure_post_checkout_hook` / `_detect_hook_manager` — hook coexistence — `hooks.py`
- `POST_CHECKOUT_HOOK` — the shared hook script body — `hooks.py`
- `render_sync` / `render_status` / `render_application_error` — `cli_output.py`
- `build_status_report` and typed report records — `status.py`
- `ApplicationError` / `UsageError` / `MissingRecipeError` / `SetupError` — `errors.py`
- `cmd_status` — status compatibility wrapper — `commands.py`
- `_apply_no_loader_fallback` / `_resolve_no_loader_delivery` — no-loader delivery — `commands.py`
- `_confirm` — shared target `[y/N]` gate — `target_commands.py`
- `_target_dispatch` / `_env_dispatch` — nested-subcommand dispatchers — `target_commands.py` / `commands.py`
- `claim_configured_target` / `claim_available_target` — physical pre-run and generic allocation
  — `device_claims.py`
- `cmd_target_claim` / `cmd_target_claims` / `cmd_target_release` — physical command orchestration
  — `target_commands.py`
- `_consume_claim_notices` / `render_claim_notices` — one-shot warning consumption and rendering
  — `cli.py` / `cli_output.py`
- `variant_completer` / `device_arg_completer` / `install` — completion — `completion.py`

## Gotchas

- **`commands.py` remains the onboarding/application-service module.** Status gathering,
  rendering, target orchestration, hooks, and doctor orchestration now have dedicated owners.
- **Circular imports are a CI invariant.** Shared constants, catalogs, and inventory types live in
  dependency-free modules; Pylint's `cyclic-import` checker analyzes the whole package and reports
  the concrete path when a cycle is introduced.
- **Argparse may still raise `SystemExit`.** Help, version, and parser-level invalid choices keep
  argparse's normal behavior. Application handlers raise typed errors and never terminate the
  process themselves.
- **`KNOWN_CMDS` is a guarded second source of truth.** It is maintained by hand alongside the
  `add_parser` calls so `_ensure_subcommand` can pre-classify argv. The exact-choice and public-help
  tests fail if a new command is added to only one surface.
- **A variant named like a type needs both positionals.** Because run/start/stop/destroy drop
  argparse `choices` on the `dtype` slot, `_normalize_device_args` resolves a lone
  `simulator`/`emulator`/`device` token as the type. Name the type and variant explicitly to select
  such a variant: `splash run simulator simulator`.
- **`--yes` exists only on `destroy`** among the four device verbs. `run`/`start`/`stop` are non-destructive and never prompt, so they have no flag.

## Why

- **Default-to-`sync` for interactive use.** Bare `splash` remains the shortest explicit sync command.
  The hook uses `splash hook post-checkout` because automatic bootstrap needs Git's event arguments
  and because trust must be checked before Registry construction or output writes.
- **Hook-manager coexistence over clobbering.** A project that already uses lefthook or husky has a hooks pipeline a developer depends on; silently overwriting its hook or seizing `core.hooksPath` would break it. Splashdown adds its entry to the detected manager, uses Git's common native hook only when no manager or custom hooks path exists, and refuses to touch any configured `core.hooksPath`.

## Related

- [init-and-onboarding.md](../features/init-and-onboarding.md) — user-facing `splash init` behavior, the loader/hook wiring, and the onboarding promise.
- [status-and-inspect.md](../features/status-and-inspect.md) — what `splash status` (and `--all`/`--check`/`--verbose`) reports.
- [device-targets.md](../features/device-targets.md) — the device-target model behind `run`/`start`/`stop`/`destroy`/`target`.
- [platform-capabilities.md](platform-capabilities.md) — subprocess classification and host/tool
  failure semantics.
- [`0002: Use argcomplete for context-aware completion`](../decisions/0002-use-argcomplete-for-context-aware-completion.md)
  — why dynamic completion is a runtime dependency with a lazy, fail-silent boundary.
- [`0004: Organize the CLI around daily verbs and noun groups`](../decisions/0004-organize-the-cli-around-daily-verbs-and-noun-groups.md)
  — why the command surface and tiered help have their current shape.
