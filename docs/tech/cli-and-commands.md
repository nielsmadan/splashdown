# CLI and Commands

How a `splash` invocation gets from `argv` to a handler: argument parsing and dispatch (`cli.py`),
checkout orchestration (`commands.py`), target orchestration (`target_commands.py`), target catalog
edits (`targets.py`), post-checkout integration (`hooks.py`), and fail-silent shell completion
(`completion.py`). Framework launch dispatch lives in `launching.py`; `doctor.py` orchestrates
checks defined in `wiring.py`.

For the *user-facing* contract of each command, see the PRD docs cross-linked under [Related](#related). This doc covers the internals — the parser quirks, the dispatch table, and how the handlers compose the lower-level modules.

## Contents

- [Purpose](#purpose)
- [How it works (current state)](#how-it-works-current-state)
  - [`cli.py` — parse and dispatch](#clipy--parse-and-dispatch)
    - [`main()` flow](#main-flow)
    - [`_ensure_subcommand` — bare `splash` defaults to `sync`](#_ensure_subcommand--bare-splash-defaults-to-sync)
    - [`KNOWN_CMDS` and the parser](#known_cmds-and-the-parser)
    - [Tiered `--help`: `_EpilogOnlyFormatter`](#tiered---help-_epilogonlyformatter)
    - [Lazy `--version`: `_VersionAction`](#lazy---version-_versionaction)
    - [The run/start/stop/destroy parser loop](#the-runstartstopdestroy-parser-loop)
    - [`_normalize_device_args`](#_normalize_device_args)
    - [Top-level exception handler](#top-level-exception-handler)
  - [`commands.py` — the orchestration layer](#commandspy--the-orchestration-layer)
    - [Provision handlers (`sync` / `init`)](#provision-handlers-sync--init)
    - [`deinit` teardown](#deinit-teardown)
    - [Git post-checkout hook installation](#git-post-checkout-hook-installation)
    - [Status rendering](#status-rendering)
    - [The no-loader delivery fallback](#the-no-loader-delivery-fallback)
    - [`_confirm` and the `cmd_init` refuse path](#_confirm-and-the-cmd_init-refuse-path)
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
handler. `commands.py` owns trust/bootstrap, init, sync, deinit, status, and env orchestration. `target_commands.py`
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

`main()` (`cli.py:389`) is the whole control flow:

1. Default `argv` to `sys.argv[1:]`, then run it through `_ensure_subcommand` (`cli.py:392`) to inject a `sync` token if no subcommand is present.
2. Build the parser (`_build_parser`, `cli.py:394`).
3. Install completion (`cli.py:395`–`399`) — imported lazily, immediately before `parse_args`, because during an active completion argcomplete parses `COMP_LINE` itself and exits inside `parse_args` (see [completion](#completionpy--fail-silent-completers)).
4. `parse_args`, dispatch completion before checkout resolution, then resolve `cwd` (`_resolve_cwd`,
   honours `--cwd`, else `$PWD`, always `.resolve()`d).
5. Dispatch `trust`, `untrust`, `bootstrap`, and the hidden hook event before constructing a
   Registry. This is security-relevant for the hook: an untrusted event can return without touching
   machine-wide registry state or output writers.
6. Construct the shared `Registry`, then enter the ordinary flat dispatch table. The final
   fall-through is `sync` (the default), so both bare `splash` and explicit `splash sync` land on
   `_cmd_provision`.

The handler signature shows the orchestration boundary: `main()` resolves `cwd`, creates a Registry
only when needed, and threads dependencies into handlers. Each branch returns the process exit code.

#### `_ensure_subcommand` — bare `splash` defaults to `sync`

`_ensure_subcommand` (`cli.py:364`) makes `splash` (no subcommand) behave as `splash sync`. The
post-checkout hook uses the explicit hidden event command instead. The helper cannot just prepend
`sync`, because top-level flags must still parse at the root parser level — `splash --cwd /path`
has to become `splash --cwd /path sync`, not `splash sync --cwd /path` (which would fail, since
`sync` has no `--cwd`).

The walk: bail early if `-h`/`--help`/`--version` is present (`cli.py:368`) — those are root actions and inserting `sync` would shadow them. Otherwise scan from the front, skipping leading top-level flags: a `--cwd PATH`/`--format json` consumes two slots (the flag set is `_TOP_LEVEL_VALUE_FLAGS`, `cli.py:361`), a `--flag=value` consumes one. The moment a token is a known subcommand (`KNOWN_CMDS`), return `argv` unchanged. The first non-flag, non-subcommand token is where `sync` gets inserted (`cli.py:381-382`), so the flags stay ahead of it.

#### `KNOWN_CMDS` and the parser

`KNOWN_CMDS` (`cli.py:93`) is the hand-maintained set of subcommand names. It exists only so `_ensure_subcommand` can decide whether a subcommand is already present *before* argparse runs — it is a second source of truth alongside the `sub.add_parser(...)` calls and must be kept in sync with them.

`_build_parser` (`cli.py:110`) is a single flat parser (deliberately, hence the `PLR0915` noqa) with one `add_parser` block per subcommand. Every subparser is registered with `help=argparse.SUPPRESS` so the auto-generated command list is hidden — the curated tiered overview in `_HELP_EPILOG` (`cli.py:70`) carries the help text instead. Top-level flags (`--cwd`, `--format`, `--version`) live on the root parser (`cli.py:120`–`122`).

#### Tiered `--help`: `_EpilogOnlyFormatter`

`_EpilogOnlyFormatter` (`cli.py:33`) is a `RawDescriptionHelpFormatter` subclass whose `_format_action` returns an empty string for the subparsers action (`argparse._SubParsersAction`, a private type argparse exposes no public name for). That suppresses argparse's flat `{sync,init,env,…}` dump. The actual command overview is the epilog (`_HELP_EPILOG`), hand-grouped into tiers — "Run on a device", "This checkout", "Set up a project", "More" — so `splash --help` reads as a task-oriented menu rather than an alphabetical list.

#### Lazy `--version`: `_VersionAction`

`_VersionAction` reimplements argparse's built-in version action so the version string is resolved
*only* when `--version` is actually passed. Its `__call__` lazy-imports
`_version.resolve_version` and prints it. The motivation is the hot path:
`importlib.metadata.version(...)` costs ~20ms, which every hook-triggered Splashdown process would
otherwise pay for a string it never prints.

#### The run/start/stop/destroy parser loop

The four device verbs share one parser shape, built in a loop (`cli.py:220`–`238`):

- Each gets an optional positional `dtype` (`TYPE`) and an optional positional `variant`.
- **Crucially, `dtype` has no argparse `choices`** (`cli.py:226`). This is intentional: with `choices=TARGET_TYPES`, a lone variant token like `splash run small-screen` would be rejected as an invalid TYPE. Dropping `choices` lets that token land in the `dtype` slot, to be re-interpreted by `_normalize_device_args` after parsing.
- The two completers are attached here: `device_arg_completer` on `dtype`, `variant_completer` on `variant`.
- **`--yes` is added only to `destroy`** (`cli.py:237`) — it is the only one of the four that is destructive (deletes the sim/AVD), so it is the only one with a confirmation prompt to skip. `run`/`start`/`stop` never prompt.

#### `_normalize_device_args`

`_normalize_device_args` (`cli.py:328`) cleans up after the choice-less `dtype` slot. First, when `prefix_match` is enabled (the default; resolved via `load_settings(_resolve_cwd(args))`), a non-canonical `dtype` token is expanded by `_match_type_prefix` against the types the checkout *declares* (`_declared_target_types`) — `sim` → `simulator`. Scoping to declared types means a short token never gets claimed by an undeclared type: `splash run d` in a sim-only project does *not* become `device`; it stays a variant prefix. If `dtype` still holds a non-type token and `variant` is empty, it shifts it over: `dtype, variant = None, dtype` (so an abbreviated *variant* falls through to the variant slot, where `resolve_variant` does its own prefix matching). Then it validates — anything still sitting in `dtype` that isn't a real `TARGET_TYPES` member raises `DeviceError`. Type names win over equally-named variants, and a type prefix wins over an identically-prefixed variant (see [Gotchas](#gotchas)). It is called from `main()` only for the four device verbs (`cli.py:410`).

#### Top-level exception handler

The dispatch `try` is wrapped by a single `except (DeviceError, ValueError)`
(`cli.py:466`). It prints `error: <msg>` to stderr and returns exit 1 — the
uniform failure path for device/target lifecycle errors and config validation.
`CapabilityError` is a `DeviceError` subtype, so unsupported hosts and missing
fixed launchers use the same clean path without a traceback.
Schema errors arrive as
`SOURCE: [qualified.path] problem; expected ...`, so representative commands
fail cleanly without a traceback. A missing recipe (`FileNotFoundError`) is
deliberately *not* caught here: the sync path handles it gracefully as a no-op
exit 0 (see [provision handlers](#provision-handlers-sync--init)), so the hook
stays silent in non-splashdown repos.

### `commands.py` — the orchestration layer

This module spans status rendering, onboarding, provisioning presentation, and env dispatch. Target
lifecycle and fleet operations live in `target_commands.py`; hook wiring lives in `hooks.py`, while
doctor orchestration lives in `doctor.py`; see [Gotchas](#gotchas).

#### Provision handlers (`sync` / `init`)

`_cmd_provision` delegates to `_cmd_provision_inner` (`commands.py:1702`), the shared engine for both `splash sync` and the tail of `splash init`.

`_cmd_provision_inner` snapshots `registry.all_for(abspath)` *before*
provisioning so it can report only what changed, calls `provision()`
(`provisioning.py`), then `write_outputs()` and `run_setup()` inside the same
failure boundary. A missing `splashdown.toml` becomes the hook-compatible no-op
exit 0.

`provision()` begins with `Recipe.load`, which validates the complete document
and preflights templates before any registry allocation or writer mutation.
Malformed recipe sections, apps, resources, setups, targets, writers, template
syntax/references, and dependency cycles therefore become `error:` + exit 1
without partial provisioning. Setup *execution* remains later: an unknown
requested setup name or a failing command can occur after registry and writer
changes and is not transactional. The "up to date (N vars, M files)" vs.
per-line change report is decided by `anything_changed`.

`cmd_init` applies the same contract to generated TOML. Scanner recipes,
minimal-monorepo recipes, and built-in presets go through `Recipe.parse` before
the recipe path is written. `cmd_refresh_inventory` first loads the existing
recipe through `Recipe.load`, then validates the fully rebuilt document before
replacing it. Invalid fields cannot be erased by the rewrite, and preserved
stale fields abort the rescan instead of being blessed. This keeps
generator/profile/loader drift from producing a file that the next sync cannot
load.

`cmd_init` is the big onboarding orchestrator: scan → scaffold recipe → write local skeleton → `_ensure_gitignore` → wire the loader (`LOADERS[inv.loader].wire`) → `_ensure_post_checkout_hook` → record sync-only clone trust → run framework wiring autofixes. An intent preset short-circuits to `_cmd_init_preset`, which writes one of the three `SCAFFOLDS` templates verbatim and bypasses the scanner. Note `cmd_init` returns `None`, not an exit code — its refuse path uses `sys.exit(2)` directly (see [below](#_confirm-and-the-cmd_init-refuse-path)). `main()` runs the first sync after `cmd_init` returns, unless `--no-sync`, and `--rescan` diverts entirely to `cmd_refresh_inventory`. Init never grants bootstrap trust.

#### `deinit` teardown

`cmd_deinit` (`commands.py:1477`) removes checkout-local state that Splashdown owns or marks
explicitly. It reads the recipe before deleting it so it can discover the
loader and writer destinations, but a malformed recipe only disables those recipe-dependent
steps; it does not block the rest of teardown.

The handler destroys every registered simulator/emulator for the checkout (hardware rows are
not owned), releases all remaining registry rows, removes the wholly-owned `splashdown.env`,
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

Git and hook-manager subprocesses are optional integration probes. Missing and non-executable
tools fall back to detection results or a setup note rather than escaping as Python exceptions.

#### Status rendering

`cmd_status` (`commands.py:515`) is the entry; the rendering is spread across several helpers. The branching:

- **`all` (positional scope) without `--verbose` (text)** → `_cmd_status_table` (`commands.py:380`): a compact one-row-per-checkout table (PATH / SUMMARY / optional ISSUE column, where ISSUE only appears if at least one row flags something — `commands.py:431`).
- **everything else** → per-checkout blocks built by `_gather_status_for_checkout` (`commands.py:290`) and emitted by `_emit_status_block_text` (`commands.py:339`). JSON output uses the same block structure (`commands.py:555`).

The block builder splits device sourcing two ways: `all` mode reads devices straight from the
registry; default mode reads the recipe+local+global catalog. A shared `_StatusContext` carries repair
counters, latest-OS lookup cache, and capability-warning keys across checkouts. When a device
boundary raises `CapabilityError`, status warns once and renders `unavailable` without incrementing
missing, stale, orphan, undeclared, or hardware counters. Other `DeviceError` values retain the
`error: <message>` status. `_print_check_summary` routes actual issues to the action that fixes
them: `gc` for defunct checkouts, `target refresh` for orphan/stale/undeclared rows, `run` for
missing managed targets, and reconnect/pairing guidance for missing physical hardware.

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

#### `_confirm` and the `cmd_init` refuse path

`_confirm` in `target_commands.py` is the shared interactive `[y/N]` gate for `cmd_destroy` and
`cmd_target_prune`. `yes=True` (from `--yes`) skips the prompt and returns `True`.

`cmd_init`'s refuse path is the one place a handler exits the process directly rather than returning a code: when `splashdown.toml` already exists and `--overwrite` wasn't passed, it prints and calls `sys.exit(2)`. `_cmd_init_preset` does the same for an unknown preset. This is inconsistent with every other handler, which returns an int (see [Gotchas](#gotchas)).

#### Device lifecycle handlers

`target_commands.py` owns `cmd_run`/`cmd_start`/`cmd_stop`/`cmd_destroy`. They share a prelude:
`_infer_dtype` resolves an
omitted TYPE to the single project-declared target type (falling back to global only when the
project declares none), and `_resolve_variant_for_cli` loads the full recipe/local/global catalog
and picks the variant. Each calls `devices.py` for target reconciliation and boot, then
`launching.py` for framework preflight and final app dispatch. The target subcommand machinery
iterates registry device rows and reconciles them against live sims/AVDs.

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
add/remove/refresh/prune handlers and receives the registry constructed by
`main()`, so every registry-using target handler shares the composition-root dependency.

`target add` validates its CLI field map with the same `validate_target_spec`
used by recipe, local, and global loads. Flags incompatible with the chosen type
raise `DeviceError` before rendering; the complete edited `LocalConfig` or `GlobalConfig` is
then parsed before writing. The `env set` branch accepts only declared
`type="set"` resources: assignment, recipe, declaration, and type failures
return exit 2 without mutating the registry.

### `completion.py` — fail-silent completers

The completers run on every `<Tab>`, so the module's contract is: **never raise, never print**. Both completers wrap their body in `except Exception: return []` (`completion.py:53`, `:76`) — a malformed recipe or a collision yields no suggestions rather than a traceback that would corrupt the shell line.

- `variant_completer` (`completion.py:39`) offers variant names for the typed-or-inferred type (slot 2).
- `device_arg_completer` (`completion.py:57`) offers declared type names *plus* variant names when exactly one type is declared (slot 1), so `splash run <TAB>` suggests variants in the common single-type case.
- Both share `_catalog` (`completion.py:21`), which mirrors `cli._resolve_cwd` (honour an already-typed `--cwd`, else `$PWD`, then `.resolve()`).

`install` (`completion.py:80`) is a no-op — and imports nothing — unless `_ARGCOMPLETE` is in the environment, so the normal CLI and hook paths pay zero cost. Only an active completion triggers the `import argcomplete` + `autocomplete()`. This is why `main()` calls `install` immediately before `parse_args`: `autocomplete()` parses `COMP_LINE` itself and exits the process before `parse_args` ever returns.

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
- `cmd_status` — status entry — `commands.py`
- `_apply_no_loader_fallback` / `_resolve_no_loader_delivery` — no-loader delivery — `commands.py`
- `_confirm` — shared target `[y/N]` gate — `target_commands.py`
- `_target_dispatch` / `_env_dispatch` — nested-subcommand dispatchers — `target_commands.py` / `commands.py`
- `variant_completer` / `device_arg_completer` / `install` — completion — `completion.py:39` / `:57` / `:80`

## Gotchas

- **`commands.py` remains a large orchestration module.** Hook, doctor, and target orchestration
  have clear owners, but status, onboarding, and provisioning presentation still share this file.
- **Circular imports are a CI invariant.** Shared constants, catalogs, and inventory types live in
  dependency-free modules; Pylint's `cyclic-import` checker analyzes the whole package and reports
  the concrete path when a cycle is introduced.
- **`cmd_init` uses `sys.exit`, not a return code.** Unlike every other handler (which returns an int that `main()` returns), `cmd_init` returns `None` and exits the process directly on its refusal and unknown-preset paths. Callers cannot treat those as ordinary return values.
- **`KNOWN_CMDS` is a second source of truth.** It is maintained by hand alongside the `add_parser` calls so `_ensure_subcommand` can pre-classify argv. Add a subcommand and you must update both, or bare-`splash` rewriting will misfire on it.
- **A variant named like a type is unreachable.** Because run/start/stop/destroy drop argparse `choices` on the `dtype` slot, `_normalize_device_args` resolves type-vs-variant by "type names win". A variant literally named `simulator`/`emulator`/`device` can never be selected positionally — the token is always read as the type. Name variants something else.
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
