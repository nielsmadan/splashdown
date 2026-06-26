# CLI and Commands

How a `splash` invocation gets from `argv` to a handler: argument parsing and dispatch (`cli.py`), the `cmd_*` orchestration handlers plus git post-checkout hook installation (`commands.py`), and the fail-silent shell completers (`completion.py`).

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

`cli.py` is the entry point: it builds a single flat argparse parser, defaults a bare invocation to `sync` (so the git hook can call `splash` with no arguments), and dispatches each subcommand to a handler. `commands.py` holds those handlers (`cmd_*`) — the orchestration layer that wires together `scanner`, `profiles`, `provisioning`, `registry`, `loaders`, `devices`, and `wiring` to actually do the work, plus the git-hook installation logic that coexists with an existing hook manager. `completion.py` provides the argcomplete completers, which must never raise or print because they run on every `<Tab>`.

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

The dispatch `try` is wrapped by a single `except (DeviceError, ValueError)` (`cli.py:391`). It prints `error: <msg>` to stderr and returns exit 1 — the uniform failure path for device/target lifecycle errors (`DeviceError`) and recipe-validation errors (`ValueError`, e.g. an unknown target type). A missing recipe (`FileNotFoundError`) is deliberately *not* caught here: the sync path handles it gracefully as a no-op exit 0 (see [provision handlers](#provision-handlers-sync--init)), so the hook stays silent in non-splashdown repos.

### `commands.py` — the orchestration layer

This is a ~1570-line module that mixes three concerns (hook wiring, status rendering, device lifecycle) — see [Gotchas](#gotchas). Below, the parts grouped by concern.

#### Provision handlers (`sync` / `init`)

`_cmd_provision` (`cli.py:390` → `commands.py:1388`) is a thin shim over `_cmd_provision_inner` (`commands.py:1402`), the shared engine for both `splash sync` and the tail of `splash init`.

`_cmd_provision_inner` snapshots `registry.all_for(abspath)` *before* provisioning so it can report only what changed, calls `provision()` (`provisioning.py`), then `write_outputs()` and `run_setup()`. The key error handling: a missing `splashdown.toml` surfaces as `FileNotFoundError`, caught at `commands.py:1414` and turned into **exit 0** with the message printed — that is what makes the post-checkout hook a silent no-op outside splashdown projects. Recipe/template errors (`ValueError`, `TemplateError`, `RuntimeError`) become `error:` + exit 1 (`commands.py:1417`). The "up to date (N vars, M files)" vs. per-line change report is decided by `anything_changed` (`commands.py:1429`).

`cmd_init` (`commands.py:1244`) is the big onboarding orchestrator: scan → scaffold recipe → write local skeleton → `_ensure_gitignore` → wire the loader (`LOADERS[inv.loader].wire`) → `_ensure_post_checkout_hook` → run framework wiring autofixes. An explicit preset short-circuits to `_cmd_init_legacy_preset` (`commands.py:1321`), which writes a `SCAFFOLDS` template verbatim and bypasses the scanner. Note `cmd_init` returns `None`, not an exit code — its refuse path uses `sys.exit(2)` directly (see [below](#_confirm-and-the-cmd_init-refuse-path)). `main()` runs the first sync after `cmd_init` returns, unless `--no-sync` (`cli.py:347`–`353`), and `--rescan` diverts entirely to `cmd_refresh_inventory` (`commands.py:1355`).

#### Git post-checkout hook installation

`_ensure_post_checkout_hook` (`commands.py:267`) wires `post-checkout → splash sync` while *coexisting* with whatever hook manager the project already uses, rather than clobbering it. `_detect_hook_manager` (`commands.py:125`) classifies the project into one of four cases, in priority order:

1. **`lefthook`** — a `lefthook.{yml,yaml}`/`.lefthook.yml` file exists, or `lefthook` is a (dev)dependency in `package.json`. Handled by `_wire_post_checkout_lefthook` (`commands.py:168`): idempotently inject a `post-checkout: → commands: → splashdown: → run: splash` block into the YAML (merging into an existing `post-checkout:` section if present), then best-effort run `lefthook install` (`commands.py:210`, trying `yarn`/`npx`/bare in turn) to regenerate the real git hooks.
2. **`husky`** — a `.husky/` directory exists. `_wire_post_checkout_husky` (`commands.py:239`) drops a `.husky/post-checkout` script (the shared `POST_CHECKOUT_HOOK` body, `commands.py:73`) and `chmod 0o755`.
3. **`core-hookspath-other`** — `git config core.hooksPath` is set to something *other than* `.githooks`. Splashdown refuses to take over a foreign hooks dir: it prints a warning telling the user to add a `splash sync` hook there themselves (`commands.py:287`) and wires nothing.
4. **`none`** (the last-resort default) — `_wire_post_checkout_corehookspath` (`commands.py:249`) owns `.githooks/post-checkout` and sets `core.hooksPath .githooks`. This is the only case where splashdown claims `core.hooksPath`, and only when nothing else is using it.

The shared `POST_CHECKOUT_HOOK` script (`commands.py:73`) is defensive: `cd` to the repo top, exit 0 if there's no `splashdown.toml`, and run `splash sync >&2 || true` (never fail a checkout) if `splash` is on PATH.

#### Status rendering

`cmd_status` (`commands.py:618`) is the entry; the rendering is spread across several helpers. The branching:

- **`all` (positional scope) without `--verbose` (text)** → `_cmd_status_table` (`commands.py:507`): a compact one-row-per-checkout table (PATH / SUMMARY / optional ISSUE column, where ISSUE only appears if at least one row flags something — `commands.py:551`).
- **everything else** → per-checkout blocks built by `_gather_status_for_checkout` (`commands.py:425`) and emitted by `_emit_status_block_text` (`commands.py:468`). JSON output uses the same block structure (`commands.py:665`).

The block builder splits device sourcing two ways: `all` mode reads devices straight from the registry (`_gather_devices_all`, `commands.py:328`); default mode reads the recipe+local catalog (`_gather_targets_declared`, `commands.py:370`). `--check` revalidates liveness, accumulating defunct/orphan/stale/missing counts into a shared `summary` dict, and `_print_check_summary` (`commands.py:570`) prints the footer routing each issue class to the command that fixes it (`gc` for defunct, `target refresh` for orphan/stale, `run` for missing). The default-mode footer (no `--check`) instead does a lightweight stale-row count and points out unfilled `set` resources (`commands.py:677`–`708`).

#### The no-loader delivery fallback

When the scanner detects no shell-env loader (`inv.loader == "none"`), `splashdown.env` would be written but nothing would source it. `_apply_no_loader_fallback` (`commands.py:1229`) handles this during `init`: `_resolve_no_loader_delivery` (`commands.py:1185`) decides whether to route values into an existing `.env`/`.env.local` (only when at least one app actually reads a dotenv file — checked via each profile's `reads_dotenv`) by injecting an `envfile=<name>` `writer` onto each resource. If no dotenv target fits, it falls back to printing `_NO_LOADER_INSTRUCTIONS` (`commands.py:1160`), telling the user to install a loader or source the file by hand. It also warns when the chosen target isn't gitignored (`_path_git_ignored`, `commands.py:1167`).

#### `_confirm` and the `cmd_init` refuse path

`_confirm` (`commands.py:1101`) is the shared interactive `[y/N]` gate for destructive ops — used by both `cmd_destroy` (`commands.py:1120`) and `cmd_target_prune` (`commands.py:1024`). `yes=True` (from `--yes`) skips the prompt and returns `True`.

`cmd_init`'s refuse path is the one place a handler exits the process directly rather than returning a code: when `splashdown.toml` already exists and `--overwrite` wasn't passed, it prints and calls `sys.exit(2)` (`commands.py:1253`). `_cmd_init_legacy_preset` does the same for an unknown preset (`commands.py:1330`). This is inconsistent with every other handler, which returns an int (see [Gotchas](#gotchas)).

#### Device lifecycle handlers

`cmd_run`/`cmd_start`/`cmd_stop`/`cmd_destroy` (`commands.py:1044`/`1064`/`1084`/`1109`) share a prelude: `_infer_dtype` (`commands.py:1130`) resolves an omitted TYPE to the single declared target type (erroring if zero or multiple are declared), and `_resolve_variant_for_cli` (`commands.py:1148`) loads recipe+local, merges, and picks the variant. Each then calls into `devices.py` (`ensure_fresh_sim`, `ios_boot`/`android_boot`, `device_run`, etc.). The bulk of `commands.py` is also the `target` subcommand machinery — `cmd_gc`/`cmd_target_gc` (`commands.py:840`/`786`), `cmd_target_refresh` (`commands.py:899`), `cmd_target_prune` (`commands.py:985`) — all of which iterate registry device rows and reconcile them against the live sims/AVDs.

#### `target` and `env` dispatchers

The `target` and `env` subcommands have their own nested subparser actions, so they get sub-dispatchers rather than a single handler: `_target_dispatch` (`commands.py:1464`) and `_env_dispatch` (`commands.py:1528`). Both treat a bare invocation (`splash target` / `splash env`) as "list" (mirroring bare `splash` → sync), and both normalize the checkout key to `str(Path(...).resolve())` so they hit the same registry key `provision()` wrote.

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
- `_cmd_provision_inner` — shared `sync`/`init` provisioning engine — `commands.py:1402`
- `cmd_init` — onboarding orchestrator (returns `None`, `sys.exit`s) — `commands.py:1244`
- `_ensure_post_checkout_hook` / `_detect_hook_manager` — hook coexistence — `commands.py:267` / `:125`
- `POST_CHECKOUT_HOOK` — the shared hook script body — `commands.py:73`
- `cmd_status` — status entry — `commands.py:618`
- `_apply_no_loader_fallback` / `_resolve_no_loader_delivery` — no-loader delivery — `commands.py:1229` / `:1185`
- `_confirm` — shared `[y/N]` gate — `commands.py:1101`
- `_target_dispatch` / `_env_dispatch` — nested-subcommand dispatchers — `commands.py:1464` / `:1528`
- `variant_completer` / `device_arg_completer` / `install` — completion — `completion.py:33` / `:51` / `:68`

## Gotchas

- **`commands.py` is a ~1570-line god-module.** It mixes hook-wiring, status rendering, and device lifecycle in one file. There is no clear seam; the section comments (`# ---------- … ----------`) are the only structure. Treat it as several modules wearing a trench coat.
- **Circular-import dance via lazy imports.** `loaders.py` and `wiring.py` both lazy-import hook helpers back out of `commands.py` rather than importing at module top — `commands.py` imports from them, so a top-level import the other way would be circular. If you move a hook helper, grep for the deferred imports.
- **`cmd_init` uses `sys.exit`, not a return code.** Unlike every other handler (which returns an int that `main()` returns), `cmd_init` returns `None` and exits the process directly on its refuse path (`sys.exit(2)`, `commands.py:1253`) and on the unknown-preset path (`commands.py:1330`). Callers can't intercept those exits.
- **`KNOWN_CMDS` is a second source of truth.** It is maintained by hand alongside the `add_parser` calls so `_ensure_subcommand` can pre-classify argv. Add a subcommand and you must update both, or bare-`splash` rewriting will misfire on it.
- **A variant named like a type is unreachable.** Because run/start/stop/destroy drop argparse `choices` on the `dtype` slot, `_normalize_device_args` resolves type-vs-variant by "type names win". A variant literally named `simulator`/`emulator`/`device` can never be selected positionally — the token is always read as the type. Name variants something else.
- **`--yes` exists only on `destroy`** among the four device verbs. `run`/`start`/`stop` are non-destructive and never prompt, so they have no flag.

## Why

- **Default-to-`sync` for the hook.** The post-checkout hook runs `splash sync` (and the bare-`splash` rewrite means even a misconfigured hook calling `splash` works). Making `sync` the zero-arg default keeps the hot path — the thing that fires on every `git checkout`/`worktree add` — trivial and fast, with lazy version/completion/submodule imports so it pays for nothing it doesn't use.
- **Hook-manager coexistence over clobbering.** A project that already uses lefthook or husky has a hooks pipeline a developer depends on; silently overwriting `.git/hooks` or seizing `core.hooksPath` would break it. So splashdown detects the existing manager and *adds* its entry to that manager's config, only claiming `.githooks/` + `core.hooksPath` as a last resort when nothing else owns hooks — and refusing to touch a foreign `core.hooksPath` at all.

## Related

- [init-and-onboarding.md](../prd/init-and-onboarding.md) — user-facing `splash init` behavior, the loader/hook wiring, and the onboarding promise.
- [status-and-inspect.md](../prd/status-and-inspect.md) — what `splash status` (and `--all`/`--check`/`--verbose`) reports.
- [device-targets.md](../prd/device-targets.md) — the device-target model behind `run`/`start`/`stop`/`destroy`/`target`.
