# CLI and Commands

How a `splash` invocation gets from `argv` to a handler: argument parsing and dispatch (`cli.py`),
the `cmd_*` orchestration handlers (`commands.py`), post-checkout integration (`hooks.py`), and the
fail-silent shell completers (`completion.py`).

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
handler. `commands.py` holds those handlers (`cmd_*`) and composes `scanner`, `profiles`,
`provisioning`, `registry`, `loaders`, `devices`, and `wiring`. `hooks.py` owns post-checkout
installation and coexistence with other hook managers. `completion.py` provides the argcomplete
completers, which must never raise or print because they run on every `<Tab>`.

The package is built for a fast hot path: `splash` with no args (what the git post-checkout hook runs on every checkout) must reach `provision()` cheaply. That goal shapes several decisions below — lazy version resolution, lazy completion install, lazy submodule imports inside handlers.

## How it works (current state)

### `cli.py` — parse and dispatch

#### `main()` flow

`main()` (`cli.py:328`) is the whole control flow:

1. Default `argv` to `sys.argv[1:]`, then run it through `_ensure_subcommand` (`cli.py:331`) to inject a `sync` token if no subcommand is present.
2. Build the parser (`_build_parser`, `cli.py:333`).
3. Install completion (`cli.py:336`–`338`) — imported lazily, immediately before `parse_args`, because during an active completion argcomplete parses `COMP_LINE` itself and exits inside `parse_args` (see [completion](#completionpy--fail-silent-completers)).
4. `parse_args`, then resolve `cwd` (`_resolve_cwd`, honours `--cwd`, else `$PWD`, always `.resolve()`d) and construct the shared `Registry` (`cli.py:341`–`342`).
5. A `try` block holds a flat dispatch table — one `if args.cmd == …: return cmd_…(…)` per subcommand (`cli.py:344`–`390`). The final fall-through is `sync` (the default), so both bare `splash` and explicit `splash sync` land on `_cmd_provision`.

The handler signature shows the orchestration boundary: `main()` resolves `cwd` and `registry` once and threads them in; the `cmd_*` functions own the work. Each branch returns the process exit code.

#### `_ensure_subcommand` — bare `splash` defaults to `sync`

`_ensure_subcommand` (`cli.py:303`) makes `splash` (no subcommand) behave as `splash sync`, which is what the post-checkout hook relies on. It cannot just prepend `sync`, because top-level flags must still parse at the root parser level — `splash --cwd /path` has to become `splash --cwd /path sync`, not `splash sync --cwd /path` (which would fail, since `sync` has no `--cwd`).

The walk: bail early if `-h`/`--help`/`--version` is present (`cli.py:307`) — those are root actions and inserting `sync` would shadow them. Otherwise scan from the front, skipping leading top-level flags: a `--cwd PATH`/`--format json` consumes two slots (the flag set is `_TOP_LEVEL_VALUE_FLAGS`, `cli.py:300`), a `--flag=value` consumes one. The moment a token is a known subcommand (`KNOWN_CMDS`), return `argv` unchanged. The first non-flag, non-subcommand token is where `sync` gets inserted (`cli.py:321`), so the flags stay ahead of it.

#### `KNOWN_CMDS` and the parser

`KNOWN_CMDS` (`cli.py:90`) is the hand-maintained set of subcommand names. It exists only so `_ensure_subcommand` can decide whether a subcommand is already present *before* argparse runs — it is a second source of truth alongside the `sub.add_parser(...)` calls and must be kept in sync with them.

`_build_parser` (`cli.py:105`) is a single flat parser (deliberately, hence the `PLR0915` noqa) with one `add_parser` block per subcommand. Every subparser is registered with `help=argparse.SUPPRESS` so the auto-generated command list is hidden — the curated tiered overview in `_HELP_EPILOG` (`cli.py:68`) carries the help text instead. Top-level flags (`--cwd`, `--format`, `--version`) live on the root parser (`cli.py:115`–`117`).

#### Tiered `--help`: `_EpilogOnlyFormatter`

`_EpilogOnlyFormatter` (`cli.py:31`) is a `RawDescriptionHelpFormatter` subclass whose `_format_action` returns an empty string for the subparsers action (`argparse._SubParsersAction`, a private type argparse exposes no public name for). That suppresses argparse's flat `{sync,init,env,…}` dump. The actual command overview is the epilog (`_HELP_EPILOG`), hand-grouped into tiers — "Run on a device", "This checkout", "Set up a project", "More" — so `splash --help` reads as a task-oriented menu rather than an alphabetical list.

#### Lazy `--version`: `_VersionAction`

`_VersionAction` (`cli.py:41`) reimplements argparse's built-in version action so the version string is resolved *only* when `--version` is actually passed. Its `__call__` lazy-imports `_resolve_version` and prints (`cli.py:62`–`65`). The motivation is the hot path: `importlib.metadata.version(...)` costs ~20ms, which every silent hook-triggered `splash sync` would otherwise pay for a string it never prints.

#### The run/start/stop/destroy parser loop

The four device verbs share one parser shape, built in a loop (`cli.py:197`–`215`):

- Each gets an optional positional `dtype` (`TYPE`) and an optional positional `variant`.
- **Crucially, `dtype` has no argparse `choices`** (`cli.py:203`). This is intentional: with `choices=TARGET_TYPES`, a lone variant token like `splash run small-screen` would be rejected as an invalid TYPE. Dropping `choices` lets that token land in the `dtype` slot, to be re-interpreted by `_normalize_device_args` after parsing.
- The two completers are attached here: `device_arg_completer` on `dtype`, `variant_completer` on `variant`.
- **`--yes` is added only to `destroy`** (`cli.py:214`) — it is the only one of the four that is destructive (deletes the sim/AVD), so it is the only one with a confirmation prompt to skip. `run`/`start`/`stop` never prompt.

#### `_normalize_device_args`

`_normalize_device_args` (`cli.py:284`) cleans up after the choice-less `dtype` slot. First, when `prefix_match` is enabled (the default; resolved via `load_settings(_resolve_cwd(args))`), a non-canonical `dtype` token is expanded by `_match_type_prefix` against the types the checkout *declares* (`_declared_target_types`) — `sim` → `simulator`. Scoping to declared types means a short token never gets claimed by an undeclared type: `splash run d` in a sim-only project does *not* become `device`; it stays a variant prefix. If `dtype` still holds a non-type token and `variant` is empty, it shifts it over: `dtype, variant = None, dtype` (so an abbreviated *variant* falls through to the variant slot, where `resolve_variant` does its own prefix matching). Then it validates — anything still sitting in `dtype` that isn't a real `TARGET_TYPES` member raises `DeviceError`. Type names win over equally-named variants, and a type prefix wins over an identically-prefixed variant (see [Gotchas](#gotchas)). It is called from `main()` only for the four device verbs (`cli.py:345`).

#### Top-level exception handler

The dispatch `try` is wrapped by a single `except (DeviceError, ValueError)`
(`cli.py:391`). It prints `error: <msg>` to stderr and returns exit 1 — the
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

This is a broad orchestration module covering onboarding, status rendering, and device
lifecycle; hook wiring itself lives in `hooks.py`. Below, the parts are grouped by concern.

#### Provision handlers (`sync` / `init`)

`_cmd_provision` (`cli.py:438` → `commands.py:1302`) is a thin shim over `_cmd_provision_inner` (`commands.py:1316`), the shared engine for both `splash sync` and the tail of `splash init`.

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

`cmd_init` is the big onboarding orchestrator: scan → scaffold recipe → write local skeleton → `_ensure_gitignore` → wire the loader (`LOADERS[inv.loader].wire`) → `_ensure_post_checkout_hook` → run framework wiring autofixes. An intent preset short-circuits to `_cmd_init_preset`, which writes one of the three `SCAFFOLDS` templates verbatim and bypasses the scanner. Note `cmd_init` returns `None`, not an exit code — its refuse path uses `sys.exit(2)` directly (see [below](#_confirm-and-the-cmd_init-refuse-path)). `main()` runs the first sync after `cmd_init` returns, unless `--no-sync`, and `--rescan` diverts entirely to `cmd_refresh_inventory`.

#### Git post-checkout hook installation

`hooks.py` owns post-checkout integration. `_ensure_post_checkout_hook` wires `post-checkout →
splash sync` while *coexisting* with whatever hook manager the project already uses, rather than
clobbering it. `_detect_hook_manager` classifies the project into one of four cases, in priority
order:

1. **`lefthook`** — a `lefthook.{yml,yaml}`/`.lefthook.yml` file exists, or `lefthook` is a (dev)dependency in `package.json`. `_wire_post_checkout_lefthook` idempotently injects a `post-checkout: → commands: → splashdown: → run: splash` block into the YAML (merging into an existing `post-checkout:` section if present), then best-effort runs the installed `lefthook` binary. It never executes project-controlled `yarn` or `npx` commands during init.
2. **`husky`** — a `.husky/` directory exists. `_wire_post_checkout_husky` drops a `.husky/post-checkout` script using the shared `POST_CHECKOUT_HOOK` body and makes it executable.
3. **`core-hookspath-other`** — `git config core.hooksPath` is set to any nonempty value. Splashdown refuses to take over that hooks directory: it prints a warning telling the user to add a `splash sync` hook there themselves and wires nothing.
4. **`none`** — `_wire_post_checkout_native` writes `post-checkout` under Git's common hooks directory. That location is shared by all worktrees, and splashdown never changes `core.hooksPath`.

The shared `POST_CHECKOUT_HOOK` script is defensive: `cd` to the repo top, exit 0 if there's no
`splashdown.toml`, and run `splash sync >&2 || true` (never fail a checkout) if `splash` is on PATH.

Git and hook-manager subprocesses are optional integration probes. Missing and non-executable
tools fall back to detection results or a setup note rather than escaping as Python exceptions.

#### Status rendering

`cmd_status` (`commands.py:618`) is the entry; the rendering is spread across several helpers. The branching:

- **`all` (positional scope) without `--verbose` (text)** → `_cmd_status_table` (`commands.py:507`): a compact one-row-per-checkout table (PATH / SUMMARY / optional ISSUE column, where ISSUE only appears if at least one row flags something — `commands.py:551`).
- **everything else** → per-checkout blocks built by `_gather_status_for_checkout` (`commands.py:425`) and emitted by `_emit_status_block_text` (`commands.py:468`). JSON output uses the same block structure (`commands.py:665`).

The block builder splits device sourcing two ways: `all` mode reads devices straight from the
registry; default mode reads the recipe+local catalog. A shared `_StatusContext` carries repair
counters, latest-OS lookup cache, and capability-warning keys across checkouts. When a device
boundary raises `CapabilityError`, status warns once and renders `unavailable` without incrementing
missing, stale, or orphan counters. Other `DeviceError` values retain the `error: <message>` status.
`_print_check_summary` routes actual issues to the command that fixes them (`gc` for defunct,

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

`_confirm` (`commands.py:1101`) is the shared interactive `[y/N]` gate for destructive ops — used by both `cmd_destroy` (`commands.py:1120`) and `cmd_target_prune` (`commands.py:1024`). `yes=True` (from `--yes`) skips the prompt and returns `True`.

`cmd_init`'s refuse path is the one place a handler exits the process directly rather than returning a code: when `splashdown.toml` already exists and `--overwrite` wasn't passed, it prints and calls `sys.exit(2)`. `_cmd_init_preset` does the same for an unknown preset. This is inconsistent with every other handler, which returns an int (see [Gotchas](#gotchas)).

#### Device lifecycle handlers

`cmd_run`/`cmd_start`/`cmd_stop`/`cmd_destroy` (`commands.py:1044`/`1064`/`1084`/`1109`) share a prelude: `_infer_dtype` (`commands.py:1130`) resolves an omitted TYPE to the single declared target type (erroring if zero or multiple are declared), and `_resolve_variant_for_cli` (`commands.py:1148`) loads recipe+local, merges, and picks the variant. Each then calls into `devices.py` (`ensure_fresh_sim`, `ios_boot`/`android_boot`, `device_run`, etc.). The bulk of `commands.py` is also the `target` subcommand machinery — `cmd_gc`/`cmd_target_gc` (`commands.py:840`/`786`), `cmd_target_refresh` (`commands.py:899`), `cmd_target_prune` (`commands.py:985`) — all of which iterate registry device rows and reconcile them against the live sims/AVDs.

Explicit platform operations propagate `CapabilityError`. The dispatcher sets `skip_unavailable`
only for the `all` scope, so unscoped `target refresh` and `target prune` warn once and continue the
other platform while `target refresh ios` or `target prune ios` returns exit 1. GC performs its own
capability-aware device sweep, preserves skipped rows, then calls
`Registry.gc(include_devices=False)` so dead port/key rows are still cleaned without erasing device
work that could not be attempted.

#### `target` and `env` dispatchers

The `target` and `env` subcommands have their own nested subparser actions, so
they get sub-dispatchers rather than a single handler: `_target_dispatch` and
`_env_dispatch`. Both treat a bare invocation (`splash target` / `splash env`)
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

The completers run on every `<Tab>`, so the module's contract is: **never raise, never print**. Both completers wrap their body in `except Exception: return []` (`completion.py:47`, `:64`) — a malformed recipe or a collision yields no suggestions rather than a traceback that would corrupt the shell line.

- `variant_completer` (`completion.py:33`) offers variant names for the typed-or-inferred type (slot 2).
- `device_arg_completer` (`completion.py:51`) offers declared type names *plus* variant names when exactly one type is declared (slot 1), so `splash run <TAB>` suggests variants in the common single-type case.
- Both share `_catalog` (`completion.py:21`), which mirrors `cli._resolve_cwd` (honour an already-typed `--cwd`, else `$PWD`, then `.resolve()`).

`install` (`completion.py:68`) is a no-op — and imports nothing — unless `_ARGCOMPLETE` is in the environment, so the normal CLI and hook paths pay zero cost. Only an active completion triggers the `import argcomplete` + `autocomplete()`. This is why `main()` calls `install` immediately before `parse_args`: `autocomplete()` parses `COMP_LINE` itself and exits the process before `parse_args` ever returns.

## Key entry points

- `main()` — process entry / dispatch table — `cli.py:328`
- `_ensure_subcommand` — bare-`splash`-defaults-to-`sync` rewrite — `cli.py:303`
- `_build_parser` — the single flat parser — `cli.py:105`
- `_EpilogOnlyFormatter` — suppress argparse's command dump — `cli.py:31`
- `_VersionAction` — lazy `--version` — `cli.py:41`
- `_normalize_device_args` — re-interpret the choice-less `dtype` slot — `cli.py:284`
- `_cmd_provision_inner` — shared `sync`/`init` provisioning engine — `commands.py:1316`
- `cmd_init` — onboarding orchestrator (returns `None`, `sys.exit`s) — `commands.py:1244`
- `_ensure_post_checkout_hook` / `_detect_hook_manager` — hook coexistence — `hooks.py`
- `POST_CHECKOUT_HOOK` — the shared hook script body — `hooks.py`
- `cmd_status` — status entry — `commands.py:618`
- `_apply_no_loader_fallback` / `_resolve_no_loader_delivery` — no-loader delivery — `commands.py:1229` / `:1185`
- `_confirm` — shared `[y/N]` gate — `commands.py:1101`
- `_target_dispatch` / `_env_dispatch` — nested-subcommand dispatchers — `commands.py`
- `variant_completer` / `device_arg_completer` / `install` — completion — `completion.py:33` / `:51` / `:68`

## Gotchas

- **`commands.py` remains broad, but hook wiring is no longer inside it.** Status,
  onboarding, and device lifecycle share the module; git-hook, gitignore, and mise edits
  live in `hooks.py`. Move those helpers only with the direct imports in `loaders.py`,
  `wiring.py`, and `commands.py` in view.
- **`cmd_init` uses `sys.exit`, not a return code.** Unlike every other handler (which
  returns an int that `main()` returns), `cmd_init` returns `None` and exits directly on
  its refusal path, while `_cmd_init_preset` exits for an unknown preset. Callers cannot
  treat either as an ordinary return value.
- **`KNOWN_CMDS` is a second source of truth.** It is maintained by hand alongside the `add_parser` calls so `_ensure_subcommand` can pre-classify argv. Add a subcommand and you must update both, or bare-`splash` rewriting will misfire on it.
- **A variant named like a type is unreachable.** Because run/start/stop/destroy drop argparse `choices` on the `dtype` slot, `_normalize_device_args` resolves type-vs-variant by "type names win". A variant literally named `simulator`/`emulator`/`device` can never be selected positionally — the token is always read as the type. Name variants something else.
- **`--yes` exists only on `destroy`** among the four device verbs. `run`/`start`/`stop` are non-destructive and never prompt, so they have no flag.

## Why

- **Default-to-`sync` for the hook.** The post-checkout hook runs `splash sync` (and the bare-`splash` rewrite means even a misconfigured hook calling `splash` works). Making `sync` the zero-arg default keeps the hot path — the thing that fires on every `git checkout`/`worktree add` — trivial and fast, with lazy version/completion/submodule imports so it pays for nothing it doesn't use.
- **Hook-manager coexistence over clobbering.** A project that already uses lefthook or husky has a hooks pipeline a developer depends on; silently overwriting its hook or seizing `core.hooksPath` would break it. Splashdown adds its entry to the detected manager, uses Git's common native hook only when no manager or custom hooks path exists, and refuses to touch any configured `core.hooksPath`.

## Related

- [init-and-onboarding.md](../features/init-and-onboarding.md) — user-facing `splash init` behavior, the loader/hook wiring, and the onboarding promise.
- [status-and-inspect.md](../features/status-and-inspect.md) — what `splash status` (and `--all`/`--check`/`--verbose`) reports.
- [device-targets.md](../features/device-targets.md) — the device-target model behind `run`/`start`/`stop`/`destroy`/`target`.
- [platform-capabilities.md](platform-capabilities.md) — subprocess classification and host/tool
  failure semantics.
