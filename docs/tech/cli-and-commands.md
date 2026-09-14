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
installation and coexistence with other hook managers, delegating the per-manager configuration
editors to `hook_configs.py`. `completion.py` provides the argcomplete
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
   error renderer but before Registry construction, so no init path touches machine-wide registry
   state or output writers.
6. Every other checkout command constructs the Registry
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
parser levels: the root output-option support matrix and the redundant
`target remove --global --keep-instance` pair. It also rejects
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

#### One shape for a file splashdown declines to edit

Severity says whether the command continued, not which subsystem noticed. A project file
splashdown preserves instead of editing is always reported as
`warning: left <name> alone: <cause>` at exit 0, and the cause is the real one: `safe_files`
raises `UneditablePath`, whose `reason` a caller prints through `refusal_reason` so a symlinked
config is never described as a shape problem. `_report_left_alone` (`hook_configs.py`) is that
shape for the hook-configuration editors, `agentdocs._refuse` for the guidance files, and
`_ensure_gitignore`/`_revert_gitignore` for the ignore block. An `error:` prefix and a non-zero
exit are for a refusal that stops the command: a loader splashdown cannot wire leaves the checkout
with nothing sourcing its environment output, so `LoaderConflictError` still exits 1 and names
`--loader none` as the way through.

`_configure_post_checkout_hook` prints the manager's manual instructions when its adapter refuses,
the way `_activate_post_checkout_hook` already did for `splash trust`, so the preservation and the
edit that finishes the integration arrive together.

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
`_report_uncovered` (`hooks.py`) follows in the same lock, naming any destination this run wrote
that Git still leaves visible. It only reads, so sync never dirties a tracked `.gitignore`, and it
asks Git nothing when the run wrote nothing. `run_setup()` runs after release. `render_sync` owns all text/JSON output. A missing recipe is
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
resolved `KEY=VALUE` line, annotating changed keys. This applies equally to normal sync and an
up-to-date no-op sync.

`cmd_init` applies the same contract to generated TOML. Scanner recipes and
minimal-monorepo recipes go through `Recipe.parse` before
the recipe path is written. This keeps generator/profile/loader drift from producing a file
that the next sync cannot load. Every generated-recipe write uses same-directory atomic replacement, preserving an existing
regular file's mode while replacing its directory entry. Symlinks and non-regular entries are
rejected; hardlinks are safely broken rather than truncating their shared inode.

`cmd_init` orchestrates scan → scaffold recipe → local skeleton → ignore coverage → loader →
project hook configuration → framework wiring → next-step report. For a nested project, the hook
step prints a manual nested sync command instead because Git invokes checkout hooks from the
worktree root. The refusal path raises `UsageError`; `main()` renders it and returns exit 2.

Init is configuration-only. It allocates nothing, writes no environment output, records no trust,
runs no loader approval command, and installs no local hook. Its framework-wiring pass reinforces
that boundary: `_apply_init_wiring_checks` skips every `WiringCheck` marked `activation`, so the
`hook` check cannot install the local wrapper behind init's back. `cmd_trust` owns those activation
effects: it installs the native wrapper, or runs the owning hook manager's own install command,
through `_activate_post_checkout_hook`, records trust, and runs the loader's `approve()`. A nested project
skips hook activation there too, for the same worktree-root reason. Approval runs `mise trust` /
`direnv allow` only when `Loader.owns_config` reports that the loader file holds splashdown's
integration and nothing else, so pre-existing or inherited configuration carrying user commands is
never approved automatically.

#### `deinit` teardown

`cmd_deinit` is the reverse-orchestration path for state splashdown
owns or marks explicitly. It reads the recipe before deleting it so it can discover the
loader and writer destinations, but a malformed recipe only disables those recipe-dependent
steps; it does not block the rest of teardown.

The handler destroys every registered simulator/emulator for the checkout (hardware rows are
not owned), releases all remaining registry rows including physical claims and addressed notices,
and asks `clear_writer_destinations` to remove splashdown's keys — the recipe's resources plus
the registry keys read before the release, minus any resource whose writer is `none` or `stdout` —
from every destination including the default one, deleting a file left with nothing else. A
destination whose scan reports an unterminated quoted value is left untouched with a warning
rather than edited on a best-effort basis. It then calls the loader's `unwire`, reverts the
agent-guidance block, and removes `splashdown.local.toml` only when it still equals
`LOCAL_SKELETON`. A modified local file is preserved with a note. `_revert_gitignore` runs after
both, so it can decide per rule: a managed rule whose file still exists on disk is kept, every
other managed rule is dropped, and the block disappears when nothing is left. Lines outside the
markers belong to the user and are never removed. `splashdown.toml` is deleted last. Framework files
patched by `doctor --fix` are outside this reversal because they have no sentinels or saved
originals. Clone-wide trust and the shared hook remain; checkout completion is removed.

#### Git post-checkout hook installation

`hooks.py` owns post-checkout integration. `_configure_post_checkout_hook` (init) writes only the
project-owned configuration; `_ensure_post_checkout_hook` (doctor `--fix`) additionally performs
local installation. Both *coexist* with whatever hook manager the project already uses, rather than
clobbering it.

`detect_hook_configuration` returns a `HookDetection` with the owning manager, the reason, and
every candidate. It ranks evidence from what Git enforces down to what a project declares:

1. **`core.hooksPath`**, when set. A path inside `.husky` is `husky` (husky 9 sets `.husky/_`);
   any other value is `core-hookspath-other`.
2. **The installed `post-checkout` hook** in the effective hooks directory, matched against
   `_INSTALLED_HOOK_SIGNATURES`. Splashdown's own bodies and `*.sample` files are skipped.
3. **Any other installed hook** in that directory carrying such a signature.
4. **A manager configuration file** in the checkout.
5. **A `package.json` declaration** — a dependency, or the `simple-git-hooks` key.

The first level with a candidate decides. Two or more candidates at that level is `conflict`:
splashdown preserves every file, names the candidates, and writes nothing. This means a
checkout with both a `.husky/` directory and lefthook in `package.json` resolves to `husky`,
because a configuration directory outranks a bare dependency.

`_ADAPTERS` maps each automatically integrated manager to its `configure`, `state`, `install`,
`config_path`, and manual text:

1. **`lefthook`** — `lefthook.{yml,yaml}` / `.lefthook.{yml,yaml}`, or a `lefthook` (dev)dependency. `_wire_post_checkout_lefthook` idempotently injects a `post-checkout.commands.splashdown` job that forwards `{1} {2} {3}`. `_run_lefthook_install` is a separate activation step invoked by `_ensure_post_checkout_hook` and `_activate_post_checkout_hook`, never by init. It never executes project-controlled `yarn` or `npx` commands.
2. **`husky`** — a `.husky/` directory, or `core.hooksPath` inside it. `_wire_post_checkout_husky` drops a `.husky/post-checkout` script using the shared `POST_CHECKOUT_HOOK` body and makes it executable. Activation installs nothing; it verifies that husky's own shims exist in this checkout, which they do not in a fresh linked worktree because `.husky/_` is gitignored.
3. **`pre-commit`** — `.pre-commit-config.yaml`, the only spelling pre-commit reads. `wire_pre_commit` (`hook_configs.py`) adds a `local` repo hook with `id: splashdown`, `language: system`, `always_run: true`, `pass_filenames: false`, and `stages: [post-checkout]`. Its `entry` is `sh -c` over the shared guard, translating `$PRE_COMMIT_FROM_REF`, `$PRE_COMMIT_TO_REF` and `$PRE_COMMIT_CHECKOUT_TYPE` into the three positional values the handler takes. Activation runs `pre-commit install --hook-type post-checkout`.
4. **`prek`** — `prek.toml`, `.pre-commit-config.yaml`, then `.pre-commit-config.yml`: `prek_config_path` mirrors prek's own first-match-wins lookup, so `wire_prek` never creates a file that outranks the configuration the project already uses. A native `prek.toml` is edited through `tomlkit`, so comments and existing entries survive; a YAML configuration is handed to `wire_pre_commit(manager="prek")` and reported as prek. Detection still keys on `prek.toml` alone (`_MANAGER_CONFIG_NAMES`), so a checkout with only `.pre-commit-config.yaml` is classified `pre-commit` and the same entry serves both. Activation runs `prek install --hook-type post-checkout`.
5. **`simple-git-hooks`** — a JSON or `package.json` configuration, or the dependency. `wire_simple_git_hooks` writes a `post-checkout` command into the first configuration the tool itself would read, and the command forwards `"$1" "$2" "$3"` because simple-git-hooks copies it into the generated hook verbatim. Dynamic `.js`/`.cjs`/`.mjs` configuration is never executed or edited, and a `package.json` value that is a string is a path to another configuration file, so it is reported as an indirection and left alone. `_write_json` restores the file's own line endings and passes `ensure_ascii=False`, so a first rewrite preserves CRLF and non-ASCII content. Activation runs the checkout's own `node_modules/.bin/simple-git-hooks` and never fetches the package.
6. **`overcommit`** — `.overcommit.{yml,yaml}`. Recognized and preserved; manual instructions only, no adapter.
7. **`core-hookspath-other`** — `core.hooksPath` points somewhere splashdown does not recognize. Splashdown refuses to take over that hooks directory: it prints event-forwarding instructions using a trusted absolute executable and wires nothing.
8. **`conflict`** — two managers with equal evidence. Everything is preserved and the candidates are named.
9. **`none`** — `_wire_post_checkout_native` writes `post-checkout` under Git's common hooks directory. That location is shared by all worktrees, and splashdown never changes `core.hooksPath`. It lives in the local `.git` directory, so init only announces it and `cmd_trust` writes it.

Every adapter shares `SPLASH_GUARD` (`hook_configs.py`), the one-line form of the native hook's
executable resolution and checkout-controlled-executable rejection.

The YAML editor in `hook_configs.py` recognizes one narrow document shape and refuses everything
else. `_pre_commit_analysis` strips comments through `yamltext.py` (the dependency-free seam that
also serves the framework checks), requires exactly one top-level `repos:` whose value is a block
— `_yaml_key_regions` is what rejects a flow spelling — then reads each sequence item as a block
mapping through `_item_fields`. That reader rejects a key indented below a scalar, a sequence item
where a key belongs, a duplicated key, and a `hooks:` with any inline value, which is the whole
class of shapes that used to be rewritten into YAML no parser would load. `_block_sequence`
accepts a sequence at its key's own column as well as an indented one, and the insertion reuses
whatever column the project already writes. Before writing, `_pre_commit_analysis` re-analyzes the
document it built and returns `unrecognized` unless splashdown's hook reads back out of it, so the
editor can only emit text it can itself parse. `pre_commit_state` runs the same analysis, so
`doctor` cannot report `ok` for a file the editor could not read.

The JSON and TOML editors refuse the same way: malformed TOML, a `package.json` `simple-git-hooks`
value that is a string path, an unparsable `package.json`, and a symlinked configuration of any
kind all yield `unrecognized`. An `unrecognized` state preserves the file and routes to manual
instructions; it is never reported as wired, and `post_checkout_files` names nothing for it.

Nesting detection lives in `hooks.py` alongside the other Git shell-outs: `_git_worktree_root`,
`_nested_worktree`, `_nested_project`, and the shared `_print_nested_checkout_hook_note`.
`_wire_post_checkout_native` refuses to write and prints that note when the directory is a nested
project, so the guard sits at the single point of installation rather than at each of its callers
(init wiring, doctor `--fix`, and trust activation). `commands.py` imports the same helpers, so
init and trust cannot classify a directory differently.

The shared `POST_CHECKOUT_HOOK` script is defensive: Git supplies the checkout root as its working
directory, the script exits 0 if there is no `splashdown.toml`, resolves `splash` once, rejects a
resolved executable inside the checkout, and forwards Git's three arguments to one hidden event
command. It absorbs the handler's failure so Git checkout itself succeeds. There is no feature
probe or older-binary fallback. Trust activates only local state; tracked Lefthook/Husky migration
belongs to init/doctor.

Hook readiness is a single `HookReadiness` policy in `hooks.py`, shared by doctor detection and
trust activation. `ready` means the project-owned configuration carries splashdown's current
entry; `active` means the owning manager's `post-checkout` hook is installed in this checkout, so
the two halves of the configuration/activation split are reported separately rather than
conflated. Native and Husky hooks must exactly match the owned event-aware body and be
executable. Lefthook must contain the exact event-aware run value; pre-commit, prek, and
simple-git-hooks must carry the exact forwarding entry. A custom or modified form is
reported as unverifiable rather than green, and `ready` without `active` is a `doctor` warning
rather than a tick, because the configuration is correct but no event can arrive yet.
`post_checkout_files` names the file an adapter writes, which is what doctor reports as changed,
and names nothing when the adapter's state is `unrecognized`. `splash doctor --fix` can add the project-level hook
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

#### The env output destination

`init --env-file PATH` selects the file that receives generated values, validated by
`validate_env_file_option` (`recipe.py`) before init writes anything and recorded as
`[project] env_file`. `Recipe.env_file` is the single reader, defaulting to `ENV_FILE_NAME`.
`resolve_writer` (`provisioning.py`) maps the `splashdown-env` writer onto that destination, so
one path never carries two ownership models. The selected loader is wired to the same file
through `Loader.plan(cwd, env_file)`.

Delivery is never inferred from what is on disk: an existing `.env` does not select itself.
`_print_env_destination` (`commands.py`) reports the destination, the keys init will manage
there, the declared keys the file already assigns (names only), and, for `loader == "none"`, how to
source the file or which loader to add. Ignore coverage is not warned about here: `_ensure_gitignore`
covers the configured destination itself and reports what Git says about it, and every later sync
re-reports a destination that stays visible when it writes to one.

#### `_confirm` and typed usage failures

`_confirm` in `target_commands.py` is the shared interactive `[y/N]` gate for `cmd_destroy` and
`cmd_target_prune`. `yes=True` (from `--yes`) skips the prompt and returns `True`.

Init refusal and invalid `env set` inputs raise `UsageError`. The CLI's shared
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

Launcher validation also runs `Profile.validate_run` with the destination kind, which fails a launch
the recipe cannot configure, such as an `ios-native` app pointed at an emulator or one with no
resolvable Xcode scheme, before any of the following happens.

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
- `_cmd_provision_inner` — the `sync` provisioning engine — `commands.py`
- `cmd_trust` / `cmd_untrust` / `cmd_bootstrap` / `cmd_post_checkout_hook` — trust and bootstrap orchestration — `commands.py`
- `cmd_init` / `cmd_deinit` — onboarding and teardown orchestration — `commands.py`
- `_configure_post_checkout_hook` / `_ensure_post_checkout_hook` / `_detect_hook_manager` — hook coexistence — `hooks.py`
- `_git_worktree_root` / `_nested_worktree` / `_nested_project` — nesting detection shared by init, trust, and the hook check — `hooks.py`
- `POST_CHECKOUT_HOOK` — the shared hook script body — `hooks.py`
- `render_sync` / `render_status` / `render_application_error` — `cli_output.py`
- `build_status_report` and typed report records — `status.py`
- `ApplicationError` / `UsageError` / `MissingRecipeError` / `SetupError` — `errors.py`
- `cmd_status` — status compatibility wrapper — `commands.py`
- `_print_env_destination` / `_persisted_env_file` / `_default_destination_keys` — env output destination — `commands.py`
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
- **Hook-manager coexistence over clobbering.** A project that already uses a hook manager has a hooks pipeline a developer depends on; silently overwriting its hook or seizing `core.hooksPath` would break it. Splashdown adds only its own entry to the detected manager, preserving unrelated jobs and scripts, uses Git's common native hook only when no manager or custom hooks path exists, and refuses to touch any configured `core.hooksPath`. When the evidence is ambiguous it reports the conflict rather than guessing, because an entry written into a file the project does not actually run is worse than no entry at all.

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
