# Framework wiring engine (`wiring.py` + `doctor.py`)

> Tech doc for `src/splashdown/wiring.py` and `src/splashdown/doctor.py` — the `splash doctor` framework-wiring
> engine. HOW the code works (internals). For the user-facing model (what each
> check does, why wiring matters), see [`docs/features/framework-wiring.md`](../features/framework-wiring.md).

## Table of contents

- [Purpose](#purpose)
- [How it works (current state)](#how-it-works-current-state)
  - [The `WiringCheck` contract](#the-wiringcheck-contract)
  - [Check registries and the profile boundary](#check-registries-and-the-profile-boundary)
  - [`run_doctor`: resolve → run loop](#run_doctor-resolve--run-loop)
  - [The individual checks](#the-individual-checks)
  - [Why Electron integration is not a WiringCheck](#why-electron-integration-is-not-a-wiringcheck)
  - [Idempotency: sentinel-wrapped patches](#idempotency-sentinel-wrapped-patches)
  - [Dependency direction](#dependency-direction)
- [Key entry points](#key-entry-points)
- [Gotchas](#gotchas)
- [Why](#why)
- [Related](#related)

## Purpose

Allocating a free port is only half the job. Most frameworks hardcode the dev
port — or override the env var — in one or two config files, so the value
splashdown writes into the configured environment output is silently ignored and
the server boots on its old default. `wiring.py` defines the checks behind `splash doctor`: a
per-framework registry of small, inspectable facts about a project ("does
`metro.config.js` read `RCT_METRO_PORT`?") that the tool can detect and, where the
rewrite is safe and mechanical, auto-patch. `doctor.py` selects and executes those
checks. `doctor` (no flag) is a read-only
`✓`/`✗` report; `doctor --fix` applies the safe autofixes and prints manual
snippets for the rest; `init` runs each detected app's safe fixes after
scaffolding, rechecks them, and reports what it changed so a fresh setup lands
wired without a follow-up `doctor --fix`.

## How it works (current state)

### The `WiringCheck` contract

`WiringCheck` (`wiring.py`) is a `NamedTuple` carrying everything `doctor` needs
to handle one fact: an `id`, a human `description`, `applies(cwd) -> bool`,
`detect(cwd) -> (state, detail)`, an optional `autofix(cwd) -> None`,
`manual_instructions(cwd) -> str`, an `activation` flag, and `files(cwd)`.
Detection states are `ok` for verified behavior, `warning` for advisory observations,
`problem` for a diagnosed defect, `incomplete` for failed or unsupported inspection, and
`skipped` for explicit uninspected coverage. A false applicability predicate also yields
`skipped`. Checks can offer a safe repair callback and manual guidance; warnings and
incomplete observations are not inherently repairable. See the
[`run_doctor` contract](#run_doctor-resolve--run-loop) for failure isolation, repair eligibility,
and exit policy.

`files` names the project files this check's autofix may rewrite. The init pass
snapshots those bytes around the fix so it can report the files it changed
without parsing each autofix's own stderr message. Report-only checks leave it at
the `_no_wiring_files` default.

#### Threading the environment output destination

`profile_core.configured_app_inventory` resolves the final recipe's app associations through
`resolve_writer` and `writer_output_path`. Doctor and init share this builder; `inventory.py`
remains dependency-free. `AppInventory.managed_resources=None` means scaffold discovery, while
an empty tuple is a configured app with no resources. Init reconstructs inventories after final
recipe parsing, not from scanner defaults.

Checks close over actual resource names, kinds, writers, and destinations. A missing managed port
skips consumption checks. Several arbitrary ports need a recognized consumer relationship or an
explicit incomplete result. The RN dotenv check resolves the chosen resource's file relative to
the app with `wiring_destination`. Shell `envrc` and non-file writers take a manual inherited-value
path and never invent a dotenv filename.

`activation` classifies what the autofix writes. A check is `activation=True` when
its repair touches the local checkout rather than project-owned, committable
configuration: `_HOOK_WIRING_CHECK` sets it, because its autofix installs the local
Git hook or runs the owning hook manager's install command. `_apply_init_wiring_checks` (`commands.py`)
skips those checks, which keeps init configuration-only. Doctor inspects them but rejects
activation callbacks during repair. Classifying the check itself, rather than
filtering by `id` at init, means a future activation-class check is excluded from
init by construction and by whichever Profile returns it.

`autofix is None` is the load-bearing signal for **report-only** checks: a check
with no safe mechanical rewrite (Spring Boot) sets it to `None`, and the run loop
never attempts a write, only prints the manual snippet. `manual_instructions` is
also `Optional` in the type but every shipped check supplies one.

### Check registries and the profile boundary

Checks are owned by **Profiles**, not by `doctor` directly. The
destination-independent RN checks live in a module-level list `_RN_WIRING_CHECKS`
(`wiring.py`) that is populated by a sequence of top-level `.append(...)` calls as
each `rn-*` helper is defined; `rn_wiring_checks(env_file)` returns that list plus
the destination-bound `rn-xcode-env` check. The shared `_HOOK_WIRING_CHECK`
(`wiring.py`) is a single check reused by native Profiles that otherwise have no
per-checkout wiring; `_RN_WIRING_CHECKS` appends that same object, so the RN and
native hook checks cannot drift apart.

Doctor strips the profile hook registration and builds separate project-wide configuration and
activation checks for every framework. `inspect_hook_inputs` excludes non-Git and nested projects,
uses bounded Git probes, and returns the validated discovery inputs reused for inspection and
configuration edits. Git invokes checkout hooks at the worktree root, where a nested recipe is
invisible.

This is **order-dependent**, and the coupling runs through `profiles_mobile.py`:

- `profiles_mobile.py` imports `_HOOK_WIRING_CHECK` and `_RN_WIRING_CHECKS`.
  Importing `wiring` executes its entire module body — including every
  `.append()` — before the profile module resumes, so by the time any Profile method runs
  the list is fully built.
- `ReactNativeProfile.wiring_checks` returns `rn_wiring_checks(env_file)` — a new
  list built at call time around a snapshot of the registry. The native Profiles
  return `[_HOOK_WIRING_CHECK]`.

Practically: any new RN check must be appended in `wiring.py`'s module body (not
lazily, not from another module after import), because Profiles read the populated
list. Web, server, and Compose checks are built by `profiles_web.py`,
`profiles_server.py`, and `profiles_compose.py` rather than going through the RN registry.

### `run_doctor`: resolve → run loop

`run_doctor(cwd, fix=False)` returns the shared `CommandResult`; `cmd_doctor` emits it through
`cli_output.render_doctor_result`. Configuration is the first outcome. Missing or invalid recipes
stop diagnostics at exit 1, preserving the selected file and parse/schema cause.

Configured `[apps]` entries determine every app path and profile, including repeated profiles.
When apps are absent, an explicit non-auto `[project] framework` describes one app at the root.
Without either, doctor reports limited app coverage and still runs shared checks. It never scans
the filesystem to choose a framework or resolves a launch/default app. Compose, hook and Watchman
checks run once at the project root. App outcomes keep their app name and path even when check IDs
repeat. Check-construction failures remain scoped so independent app/shared checks still run.

Each frozen `DoctorOutcome` records scope, app, path, check ID, state, message, manual next steps,
changed files, and repair status. Applicability, direct detection, guidance, repair preparation,
repair, and recheck failures become explicit incomplete outcomes. Detection exceptions never
permit repair. A skipped check is not a health claim. Exit 0 means completed checks have no
unresolved problems, including advisory warnings; problem/incomplete or failed/unverified repairs
exit 1, usage exits 2, and cancellation exits 130 with completed outcomes and changes retained.

Eligible repairs first establish known candidate files. The lock-only `checkout_operation_lock`
uses the canonical checkout identity, then the engine reloads the recipe and rebuilds the affected
check under the lock. It repeats applicability/detection and holds the lock through the edit and
recheck. Safe byte snapshots before and in `finally` after editing retain partial writes, including
when repair raises or is interrupted. Failed candidate enumeration prevents editing; unreadable or
newly discovered post-edit candidates make change accounting explicitly incomplete. Tool probes
stay outside this repair boundary. Neither mode constructs `Registry` or consumes notices; plain
inspection and ineligible repairs create no lock bookkeeping.

After a repair attempt, pending framework checks reload their recipe/resource context before
inspection. This keeps sibling checks current when the recipe changed while waiting for the lock;
removed checks are skipped and completed outcomes remain intact. Tool and device probes are not
repeated by this refresh.

Framework repairs are enabled only through the resource-aware catalog. Activation callbacks
remain excluded.

Text findings and summary go to stdout, operational failure diagnostics to stderr. JSON uses one
shared envelope with every outcome and summary, including partial results and changed paths.

Init uses `_apply_init_wiring_checks` to run the same Profile-owned safe autofixes per app.
`_apply_init_wiring_check` detects, fixes, then detects again: a fix that did not resolve the
problem prints `✗` with the check's manual instructions rather than passing silently, and the
files whose bytes changed are appended to `InitReport.changed` (see
`docs/features/framework-wiring.md`).

Advisory warnings do not count against doctor's exit code. Required inactive integrations are
problems, while failed applicable probes remain incomplete. Status retains its separate readiness
policy: an observed finding alone does not make status fail.

Every writable check uses `safe_files.py` for its edit. The helper rejects a final symlink,
non-regular destination, configured-root escape, or symlinked parent component; opens existing files with
`O_NOFOLLOW` where available; and commits through same-directory atomic replacement while
preserving the existing mode. The .NET writer additionally passes the detected BOM encoding and
newline convention. Every hook-manager repair — lefthook, husky, pre-commit, prek,
simple-git-hooks — uses the same helper, with executable mode applied to hook
replacements rather than a follow-up `chmod`.

### The individual checks

- **`hook` / `hook-activation`** (`doctor.py`) — use `post_checkout_readiness` and the strict
  `validate_hook_inputs` seam shared with status. Configuration follows manager precedence and
  recognizes exact owned entries, including legacy migration to the short Lefthook command.
  Only a recognized manager configuration gets a repair through `_configure_post_checkout_hook`.
  Native hooks, conflicting managers, custom hook paths and unsupported shapes remain manual.
  Configuration is revalidated inside the operation lock. Local activation is a separate
  report-only outcome: a known inactive required integration is a problem, even after a verified
  configuration repair. No installer or `_ensure_post_checkout_hook` callback runs.
  Activation requires an executable effective Git hook entry. Lefthook flow mappings in the
  root or owned job regions remain manual because the block editor cannot place them safely.
  Refused edits retain bounded discovery inputs through manual guidance.
- **`loader` / `loader-tool` / `loader-approval`** — inspect the recipe's selected loader and
  effective env destination. `validated_loader_plan` shares parsing and the existing `Loader.plan`
  contract with status. Valid user-owned directives count as configured. Malformed/conflicting
  inputs have no repair callback; recognized writes use only `apply_wire_plan`. Tool presence and
  authorization are separate observations after configuration repairs and outside the lock.
  Direnv uses bounded `status --json`, verifies the exact `foundRC.path`, and accepts only known
  integer approval states. Failed probes are incomplete. Mise/devbox authorization observation is
  explicitly unsupported. Parent-shell loading is never inferred; direnv scripts are not evaluated.
  Direnv inspection accepts flat shell commands and refuses compound, expanded, or unterminated
  contexts before offering a repair. Generated direnv/devbox paths are shell-quoted. Mise path
  precedence includes dangling links so an unsafe preferred config cannot redirect a repair to a
  lower-priority file. Locked repair refresh builds only the affected check catalog.
- **`rn-metro-config`** (`_rn_metro_detect` in `wiring.py`) —
  `metro.config.js` should read `process.env.RCT_METRO_PORT`. Autofix
  (`_rn_metro_autofix`, `wiring.py`) recognizes three object-literal shapes
  (documented in the comment at `wiring.py`): a literal `port: <N>` is rewritten
  in place to `Number(process.env.<actual-port>) || <N>` keeping the literal as
  fallback; an existing `server: {` block gets a port line inserted at its open
  brace; a bare `const config = {` / `module.exports = {` object gets a whole
  `server` block injected. The brace-aware injection lives in `_rn_metro_inject`
  (`wiring.py`). The object must reach the export. Unknown or nested consumers remain
  incomplete without editing. Later references to the selected config or exported object,
  including property writes and aliases, also require manual verification of mutations.
- **`rn-pkg-port`** (`wiring.py`) checks recognized React Native launch commands. Numeric
  `--port` arguments become `${RCT_METRO_PORT:-N}` so their original value survives as a fallback.
  JSON member splicing preserves unrelated settings and formatting. Renamed Metro resources need
  an explicit script flag, otherwise the check prints that actual resource name for manual wiring.
  Canonical resources may use the framework's native default only when no explicit port flag
  overrides it. Argument matching respects quotes and rejects duplicate port flags. Unsupported shell syntax
  in a relevant script is incomplete and preserves the file, even when another segment has
  a recognizable literal port.
- **`rn-xcode-env`** (`_rn_xcode_detect` in `wiring.py`) —
  `ios/.xcode.env` should source `RCT_METRO_PORT` from the environment output this
  checkout writes. Detection splits the file into shell statements
  (`_xcode_statements`) and collects a dotenv path only from a statement that
  assigns or exports `RCT_METRO_PORT`, and only when every `if` guarding that
  statement itself names `RCT_METRO_PORT` or a dotenv path. A path named for any
  other purpose — `source "${SRCROOT}/../splashdown.env"`, `export
  ENVFILE=.env.staging`, a build-configuration-only branch — wires nothing, so it
  neither proves wiring nor blocks repair. Collected paths are resolved
  (`${SRCROOT}` and its aliases point at `ios/`, so `${SRCROOT}/../x.env` is the
  app's `x.env`) and compared with the configured destination. A match is `ok`
  for configured filenames without an `.env` suffix as well. Destinations support letters,
  digits, spaces, dots, slashes, underscores and hyphens. Other shell characters yield incomplete
  coverage before any repair, with guidance to change the destination or quote it manually.
  It can be a sentinel block or supported handwritten conditional. A
  port wiring that reads some *other* dotenv is a problem, not an `ok`: it is
  wiring this check did not recognize as reading the right file, and autofix leaves
  it byte-identical so the manual instructions can name the edit. Otherwise autofix replaces a recognized literal export or prior managed block with a
  sentinel-wrapped managed block (`_xcode_block(env_file)`, `wiring.py`): honor a
  value already set by `run-ios`, else read the configured destination, else fall
  back to the previous literal (8083 when absent). Other handwritten assignments are preserved. A reference through an unrecognized shell variable resolves to no
  path, so it reads as unparsed rather than as wired.
- **`watchman-root`** (`watchman_watch_root` in `runtime_checks.py`) — for configured RN/Expo
  apps, runs once at the checkout root using exactly `watchman --no-spawn --no-local watch-list`
  with a three-second timeout. The selected external executable is pinned and checkout-controlled
  paths/links are refused, including tools outside a nested recipe but inside an enclosing
  checkout. The same boundary guard in `safe_files.py` protects direnv authorization probes and diagnostic
  Git selection. `safe_files.diagnostic_git_executable` pins a validated external executable.
  Hook discovery resolves relative PATH entries against the inspected checkout. Doctor alone
  passes `diagnostic=True` through early `select_project` to guard its initial Git probe, which
  retains invocation-relative PATH lookup for outside `--cwd` selection. The guard recognizes both
  `.git` directories and worktree marker files without executing Git. JSON must contain absolute
  roots without an error. Strict ancestor roots
  are advisory sharing risks; a valid empty/local list establishes only that root property.
  Missing optional Watchman skips with roots uninspected. There is no guessed stopped-daemon
  signature: nonzero exits, permission/protocol errors, timeouts and invalid responses remain
  incomplete. The adapter never starts a daemon or changes watches.
- **`vite-port-wired`** (`profiles_web.py`) inspects the
  top-level `server.port` flow in a literal export or simple `defineConfig` return. Supported
  reads include process-env dot/bracket syntax, simple constant aliases/destructuring, numeric
  `Number`/decimal `parseInt` conversion, and `||`/`??` fallbacks following the managed read.
  The shared Vite/Astro/Metro recognizer retains the leading read's identity and source spans
  through aliases and nested conversions. Its fallback summary assumes the managed key's valid
  allocated numeric value and keeps only reads reachable before a guaranteed literal fallback.
  Vite checks those sources in order, so an excluded key can use a reachable same-key inherited
  fallback without borrowing an unreachable read. Bitwise operators, comma expressions,
  nondecimal conversion, unrelated/constant-leading fallbacks, and unparenthesized mixtures of
  `||` and `??` remain incomplete. A key mentioned in logging or another config
  slot proves nothing. Spread, nested/conditional return, mutation, arithmetic, and unknown
  expressions report incomplete coverage.
  A recognized Vite `loadEnv` import admits inherited values as well as dotenv values according
  to its third argument: omitted/undefined means `VITE_`, a literal string admits that prefix,
  `""` admits all, and literal string arrays admit any matching prefix (`[]` admits none).
  `envPrefix` does not change this filter. The directory determines dotenv loading, not the
  inherited-value filter; a positive result does not claim parent-shell activation or file loading.
  Dynamic prefixes are manual. For a recognized excluded managed read, the only repair is
  `(process.env.KEY ?? env.KEY)` at that read, preserving the original fallback and all unrelated
  API/ dotenv accesses. Missing `server.port` remains manual-only.
- **`astro-config-port`** (`profiles_web.py`) — wires top-level `server.port` to
  `WEB_DEV_PORT`, while refusing ambiguous nested `server` shapes.
- **`angular-pkg-port`** (`profiles_web.py`) — rewrites `ng serve` npm scripts so
  the declared key reaches Angular's CLI. Numeric flags retain their number as a shell fallback.
  Only actual argument tokens are edited, preserving quoted unrelated arguments. Duplicate port
  flags and unsupported values are left for manual repair. JSON edits locate root scripts/tasks
  and launch profiles beneath their actual parent, then verify the intended semantic change.
- **`deno-port-wired`** (`profiles_web.py`) — validates task flag order or a source-level
  port expression reading the actual key; only task commands are mechanically rewritten. Source
  calls are located outside quoted literals and require the same narrow effective-value grammar
  as other managed-port consumers. Unreachable reads and quoted examples remain incomplete.
- **`compose-hardcoded-ports`** (`profiles_compose.py`) — project-level static report-only
  detection for fixed host ports, literal container names and fixed absolute bind sources in
  supported block/flow and short/long forms. The narrow Compose mapping/sequence readers use
  `yamltext._yaml_key_regions` to keep service and owned value slots distinct. Findings name the
  file, service, slot and source, and remain advisory unless a separate managed-consumer check
  establishes a real contract violation. Relative/named/interpolated sources are distinguished;
  interpolated values and runtime configuration are unverified. Aliases, inheritance, includes,
  malformed/unknown slots and refused/unreadable files remain incomplete, retaining independent
  service findings. File selection uses `lstat` so dangling links and special files cannot appear
  absent. Inline mapping values cannot discard deeper continuation content. Quoted scalars are
  limited to complete, simply quoted literals; double-quoted escapes and embedded/trailing
  quote forms remain incomplete rather than hiding a fixed source behind unsupported encoding.
  Inspection uses the regular-file-only read seam. No YAML dependency, Docker/Colima
  query or rewrite is involved. Guidance reads actual recipe declarations and expressly leaves
  stored allocations, service-to-resource associations and invoking-shell loading unverified.
- **`springboot-application-properties`** (defined inline in the Spring Boot
  Profile in `profiles_server.py`) — checks that
  `application.properties`/`application.yml` uses the `server.port=${PORT:8080}`
  placeholder as the complete value, optionally quoted in YAML. **Report-only**: `autofix=None`, so `doctor`
  reports the `✗` and prints manual instructions but never rewrites the file, even
  under `--fix`.
- **Laravel's `vite-port-wired`** (`LaravelProfile.wiring_checks` in
  `profiles_server.py`) — reuses the report-only Vite port assertion when the Laravel app has a
  Vite config. The backend `SERVER_PORT` is already environment-only; an API-only app with no Vite
  config has no file wiring to check.
- **`aspnet-launch-settings`** (`AspNetCoreProfile.wiring_checks` in
  `profiles_server.py`) — flags any
  `"commandName": "Project"` profile in `Properties/launchSettings.json` that pins
  `applicationUrl`, which `dotnet run` honours ahead of an inherited
  `ASPNETCORE_HTTP_PORTS`. **Autofix present**, unlike the two report-only checks
  above: launchSettings is JSON, so the fix removes `applicationUrl` from Project profiles.
  `_read_launch_settings` handles the UTF-8 BOM emitted by .NET templates and preserves CRLF on
  write; a plain text JSON round trip would reject or churn those files.
  `_aspnet_project_profiles` is what narrows the rewrite to `Project`
  profiles, leaving `IISExpress` entries untouched. Pre-.NET-8 projects use a report-only twin
  because `ASPNETCORE_HTTP_PORTS` is unavailable there.

### Why Electron integration is not a WiringCheck

Electron capability detection can safely identify the package dependency, but it cannot
identify one stable main-process entrypoint or module shape. Scanner-driven init therefore writes
nothing for it and prints a pointer to the documented opt-in recipe, which carries both the
`ELECTRON_PROFILE_ID` resource and the guarded main-process code that derives a sibling of
Electron's default `userData` directory and sets the path before `requestSingleInstanceLock()`.
There is no Electron Profile or autofix: rewriting arbitrary main-process source would be
materially less safe than the mechanical Vite/RN transforms above.

This also preserves renderer wiring. An Electron+Vite app stays on `ViteProfile`, so init
still runs the Vite check while user-data isolation stays an opt-in recipe the user applies.

### Idempotency: sentinel-wrapped patches

The `ios/.xcode.env` autofix is idempotent by sentinel pair. The managed block is
rendered per destination by `_xcode_block(env_file)`, so repointing a checkout at a
new output rewrites the block in place rather than stacking a second one. It is
bracketed by `# >>> splashdown-managed RCT_METRO_PORT >>>` /
`# <<< splashdown-managed RCT_METRO_PORT <<<` (`wiring.py`), and
`_XCODE_BLOCK_RE` (`wiring.py`) is a non-greedy `DOTALL` match across that pair,
falling back to the paragraph starting at the opening sentinel when the closing one
has been deleted, so a half-removed block is still recognized as splashdown's own
rather than as foreign wiring that blocks repair.
Autofix strips any prior block by that regex before re-appending, so re-running
`--fix` replaces the block's contents in place rather than stacking copies. The
sentinels also document, in the file itself, which lines are tool-managed versus
hand-edited — edits *inside* the pair are overwritten on the next `--fix`. The
literal-export regex `_XCODE_LITERAL_EXPORT_RE` (`wiring.py`) matches only a
*static* `export RCT_METRO_PORT=<digits>` with no variable references, kept narrow
on purpose so a hand-written conditional/shell-substitution wiring is not mangled.

### Dependency direction

`wiring.py` owns the `WiringCheck` contract and shared concrete file checks. The categorized
profile modules depend on those definitions, but wiring never imports profiles, scanner,
commands, or the package root.
`doctor.py` is the higher orchestration layer: it reads the profile catalog and configured app
scopes, combines app and project checks, and returns structured results. A recipe with `[bootstrap]`
adds the project hook check even when no apps are declared. This makes
the direction `doctor → profile modules → wiring → hooks`; no function-local import is needed to hide a
backward edge. Pylint's `cyclic-import` check enforces the acyclic package graph in CI.

## Key entry points

- `wiring.py` — `WiringCheck`, the RN check registry, `rn_wiring_checks`,
  `wiring_destination`, concrete detect/autofix helpers, and `_HOOK_WIRING_CHECK`.
- `doctor.py` — `run_doctor`, configured app/project targets, failure isolation, and repair accounting.
- `hooks.py` — shared hook readiness, exact manager parsing, repair, and manual instructions.
- `profiles_mobile.py` — Profiles reuse the registries via `rn_wiring_checks(env_file)` /
  `[_HOOK_WIRING_CHECK]`.
- `profiles_web.py` / `profiles_server.py` / `profiles_compose.py` — framework and
  project-specific checks.

## Gotchas

- **`rn-metro` autofix only handles the recognized object-literal shapes.** A
  literal `port:`, an existing `server: {` block, or a bare config object literal
  are handled; anything else (functional config, spread merges, an exotic export
  shape) makes `_rn_metro_inject` return `None`, autofix returns without writing,
  and the check stays `✗` with the manual snippet printed (`wiring.py`).
- **RN-on-Android Metro port is not wired (known limitation).** Android bakes the
  Metro port into the build via the RN Gradle plugin, a different mechanism from
  iOS, and there is no check for it. The RN CLI propagates `RCT_METRO_PORT` to
  Gradle so `yarn android` works, but a bare `gradle assembleDebug` may default to
  8081.
- **Spring Boot is report-only by design.** `autofix=None` in `profiles_server.py`: even
  under `--fix`, `doctor` will not silence a Spring Boot `✗` — it only prints the
  manual placeholder snippet. Do not "helpfully" add an autofix here without
  re-reading [Why](#why).
- **The check registry's import-order coupling is real.** `_RN_WIRING_CHECKS` is
  populated by top-level `.append()` calls in `wiring.py`'s body; Profiles read it
  through the import in `profiles_mobile.py`. A new RN check must be
  appended in the module body (not registered lazily from elsewhere), or Profiles
  will snapshot a list that's missing it.
- **iOS port changes need a rebuild.** `RCT_METRO_PORT` is compiled into the iOS
  binary via `RCTBundleURLProvider`'s `defaultPort` (`wiring.py` block comment),
  so even a correctly wired `ios/.xcode.env` only takes effect after the app is
  rebuilt — wiring alone won't move a running build off its old port.
- **A configured `core.hooksPath` is reported, not auto-wired.** The hook check stays `✗`
  even under `--fix`; Splashdown never changes or writes into that configured path. Manual
  instructions show the event-aware hidden command with a trusted absolute executable, plus manual
  bootstrap as the fallback. A sync-only instruction would not satisfy the check. Overcommit and
  an unresolved conflict between two hook managers behave the same way: reported, preserved, and
  given instructions in that manager's own syntax.

## Why

A bare allocated env var isn't enough: frameworks override it in config the user
rarely remembers exists, and the failure mode is *silent* (wrong port, not an
error). Auto-patching the safe, mechanical cases keeps the allocated port actually
reaching the process, while two design choices keep the patcher from corrupting
hand-authored files:

- **Report-only for risky Java/Spring rewrites.** `application.properties`/`.yml` has
  no single mechanical shape that's safe to rewrite blind (property vs YAML,
  profiles, profile-specific overrides), so the check stays `autofix=None` and only
  tells the user what to change. Guessing a rewrite there risks breaking a build
  config; flagging it does not.
- **Sentinel-wrapped idempotency.** Marking the tool's `ios/.xcode.env` block with a
  begin/end sentinel pair lets `--fix` find-and-replace its own block instead of
  re-appending, makes re-runs safe, and draws a clear line between tool-managed and
  hand-edited content so the user knows what they may safely touch.

## Related

- [`docs/features/framework-wiring.md`](../features/framework-wiring.md) — user-facing model
  for `splash doctor` and each check (the authoritative behavior spec).
- [`docs/tech/scanning-and-extension.md`](./scanning-and-extension.md) — how
  Profiles are detected and registered, and how `Profile.wiring_checks` ties a
  framework to its check list (the registry side of this engine).
