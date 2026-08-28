# Framework wiring engine (`wiring.py` + `doctor.py`)

> Tech doc for `src/splashdown/wiring.py` and `src/splashdown/doctor.py` — the `splash doctor` framework-wiring
> engine. HOW the code works (internals). For the user-facing model (what each
> check does, why wiring matters), see [`docs/features/framework-wiring.md`](../features/framework-wiring.md).

## Table of contents

- [Purpose](#purpose)
- [How it works (current state)](#how-it-works-current-state)
  - [The `WiringCheck` contract](#the-wiringcheck-contract)
  - [Check registries and the profile boundary](#check-registries-and-the-profile-boundary)
  - [`cmd_doctor`: resolve → run loop](#cmd_doctor-resolve--run-loop)
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
splashdown writes into `splashdown.env` is silently ignored and the server boots
on its old default. `wiring.py` defines the checks behind `splash doctor`: a
per-framework registry of small, inspectable facts about a project ("does
`metro.config.js` read `RCT_METRO_PORT`?") that the tool can detect and, where the
rewrite is safe and mechanical, auto-patch. `doctor.py` selects and executes those
checks. `doctor` (no flag) is a read-only
`✓`/`✗` report; `doctor --fix` applies the safe autofixes and prints manual
snippets for the rest; scanner-driven `init` runs each detected app's safe fixes after
scaffolding so a fresh setup lands wired. Intent presets bypass the scanner and run
`doctor --fix` only when framework detection independently finds a Profile with checks.

## How it works (current state)

### The `WiringCheck` contract

`WiringCheck` (`wiring.py`) is a `NamedTuple` carrying everything `doctor` needs
to handle one fact: an `id`, a human `description`, `applies(cwd) -> bool`,
`detect(cwd) -> ("ok"|"problem", detail)`, an optional `autofix(cwd) -> None`, and
`manual_instructions(cwd) -> str`. The contract is deliberately three-state per
check: not-applicable (skip), ok, or problem — with two escape hatches on a
problem (an autofix that may exist, and manual instructions that always do).

`autofix is None` is the load-bearing signal for **report-only** checks: a check
with no safe mechanical rewrite (Spring Boot) sets it to `None`, and the run loop
never attempts a write, only prints the manual snippet. `manual_instructions` is
also `Optional` in the type but every shipped check supplies one.

### Check registries and the profile boundary

Checks are owned by **Profiles**, not by `doctor` directly. The RN checks live in
a module-level list `_RN_WIRING_CHECKS` (`wiring.py`) that is populated by a
sequence of top-level `.append(...)` calls as each `rn-*` helper is defined. The shared `_HOOK_WIRING_CHECK`
(`wiring.py`) is a single check reused by native Profiles that otherwise have no
per-checkout wiring.

This is **order-dependent**, and the coupling runs through `profiles_mobile.py`:

- `profiles_mobile.py` imports `_HOOK_WIRING_CHECK` and `_RN_WIRING_CHECKS`.
  Importing `wiring` executes its entire module body — including every
  `.append()` — before the profile module resumes, so by the time any Profile method runs
  the list is fully built.
- `ReactNativeProfile.wiring_checks` returns
  `list(_RN_WIRING_CHECKS)` — a snapshot copy taken at call time. The native
  Profiles return `[_HOOK_WIRING_CHECK]`.

Practically: any new RN check must be appended in `wiring.py`'s module body (not
lazily, not from another module after import), because Profiles read the populated
list. Web, server, and Compose checks are built by `profiles_web.py`,
`profiles_server.py`, and `profiles_compose.py` rather than going through the RN registry.

### `cmd_doctor`: resolve → run loop

`cmd_doctor` resolves all check targets through `_resolve_check_targets`. Project-level Compose
checks are collected first, and a recipe with `[bootstrap]` adds the hook check. Framework
resolution then selects the app directory and Profile checks. When framework detection is
ambiguous or unavailable, project checks still run; doctor asks for `--framework` only when there
are no project checks to perform. Framework and project checks are combined by id so the shared
hook check is emitted once. A resolved framework with no checks exits 0 with either the env-only
positive verdict or a neutral "no checks defined" note.

The run loop in `doctor.py` walks each check:

1. `applies(cwd)` false → print "not applicable", skip.
2. `detect` returns `ok` → print `✓`.
3. `problem` **and** `--fix` **and** `autofix is not None` → call `autofix` (wrapped
   in `try/except` so one check failing reports rather than crashing the run,
   `doctor.py`), then re-`detect`. On `ok` print `✓ (fixed)`; otherwise print
   `✗ still problem after autofix` and the manual snippet, count it bad.
4. Otherwise (problem with no fix requested/available) → print `✗` plus the manual
   snippet, count it bad.

Exit code is 0 only when nothing is left in the `problem` state.
Scanner-driven init uses `_apply_init_wiring_checks` to run the same Profile-owned safe
autofixes per app. The intent-preset path resolves a framework from the checkout and calls
`cmd_doctor(cwd, fix=True)` only when checks exist (see
`docs/features/framework-wiring.md`).

Every writable check uses `safe_files.py` for its edit. The helper rejects a final symlink,
non-regular destination, configured-root escape, or symlinked parent component; opens existing files with
`O_NOFOLLOW` where available; and commits through same-directory atomic replacement while
preserving the existing mode. The .NET writer additionally passes the detected BOM encoding and
newline convention. Lefthook, Husky, and native-hook repairs use the same helper, with executable
mode applied to hook replacements rather than a follow-up `chmod`.

### The individual checks

- **`hook`** (`_rn_hook_detect` and `_HOOK_WIRING_CHECK`) — delegates detection to
  `post_checkout_readiness`, the same exact policy used during `splash trust`. Lefthook must have
  the owned event-forwarding run value; Husky and native hooks must have the owned body and be
  executable. A modified hook is reported as unverifiable instead of accepted by substring.
  Configured `core.hooksPath` is reported but not touched. Autofix delegates to
  `_ensure_post_checkout_hook` through `_autofix_ensure_post_checkout_hook`
  so it coexists with the project's existing manager. RN and project-level registrations use the
  same `hook` id, so doctor emits one diagnostic.
- **`rn-metro-config`** (`_rn_metro_detect` in `wiring.py`) —
  `metro.config.js` should read `process.env.RCT_METRO_PORT`. Autofix
  (`_rn_metro_autofix`, `wiring.py`) recognizes three object-literal shapes
  (documented in the comment at `wiring.py`): a literal `port: <N>` is rewritten
  in place to `Number(process.env.RCT_METRO_PORT) || <N>` keeping the literal as
  fallback; an existing `server: {` block gets a port line inserted at its open
  brace; a bare `const config = {` / `module.exports = {` object gets a whole
  `server` block injected. The brace-aware injection lives in `_rn_metro_inject`
  (`wiring.py`), driven by three regexes in the same module. If none
  match, autofix returns without writing and the check surfaces manual instructions.
- **`rn-pkg-port`** (`_rn_pkg_detect` in `wiring.py`) — strips
  a hardcoded `--port <N>` from `package.json` scripts that boot Metro. The target
  set (`_pkg_scripts_with_port`, `wiring.py`) is the default RN script names
  `start`/`ios`/`android`, plus any script invoking `react-native start`. The
  `react-native start` match (`wiring.py`) is deliberately narrow so `--port` on
  unrelated tools (`react-native-test-runner --port 4000`) is left alone. Autofix
  re-serializes `package.json` with 2-space indent.
- **`rn-xcode-env`** (`_rn_xcode_detect` in `wiring.py`) —
  `ios/.xcode.env` should source `RCT_METRO_PORT` from this checkout's
  `splashdown.env`. Detection treats *any* mention of `splashdown.env` as ok
  (sentinel block, hand-written conditional, whatever). Autofix (`wiring.py`)
  strips any static literal export, strips any prior sentinel block, then appends a
  sentinel-wrapped managed block (`_XCODE_BLOCK`, `wiring.py`): honor a value
  already set by `run-ios`, else read `splashdown.env`, else fall back to 8083.
- **`vite-config-process-env`** (returned by `ViteProfile.wiring_checks` in
  `profiles_web.py`) —
  rewrites the `loadEnv` idiom `env.X` to `process.env.X` in `vite.config.{ts,js,mjs}`
  so values loaded into the shell by mise/direnv/devbox reach Vite. The matcher
  skips already-fixed `process.env.X`.
- **`vite-port-wired`** (`profiles_web.py`) — report-only assertion that Vite names the
  allocated port variable. It accepts bracket access and destructuring, but never invents a
  `server.port` block in arbitrary config.
- **`astro-config-port`** (`profiles_web.py`) — wires top-level `server.port` to
  `WEB_DEV_PORT`, while refusing ambiguous nested `server` shapes.
- **`angular-pkg-port`** (`profiles_web.py`) — rewrites `ng serve` npm scripts so
  `--port $WEB_DEV_PORT` reaches Angular's CLI.
- **`deno-port-wired`** (`profiles_web.py`) — validates task flag order or a source-level
  `PORT` read; only task commands are mechanically rewritten.
- **`compose-hardcoded-ports`** (`profiles_compose.py`) — project-level, report-only
  detection for pinned host ports and literal container names across block and flow YAML shapes.
- **`springboot-application-properties`** (defined inline in the Spring Boot
  Profile in `profiles_server.py`) — checks that
  `application.properties`/`application.yml` uses the `server.port=${PORT:8080}`
  placeholder. **Report-only**: `autofix=None`, so `doctor`
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
identify one stable main-process entrypoint or module shape. Scanner-driven init therefore
asks whether to add `ELECTRON_PROFILE_ID`, and both accepted scanner init and the explicit
`electron` intent preset print guarded integration code. That code derives a sibling of
Electron's default `userData` directory, creates it, and sets the path before
`requestSingleInstanceLock()`. There is no Electron Profile or autofix: rewriting arbitrary
main-process source would be materially less safe than the mechanical Vite/RN transforms above.

This also preserves renderer wiring. An Electron+Vite app stays on `ViteProfile`, so init
still runs the Vite check while treating Electron user-data isolation as an optional
resource overlay.

### Idempotency: sentinel-wrapped patches

The `ios/.xcode.env` autofix is idempotent by sentinel pair. The managed block is
bracketed by `# >>> splashdown-managed RCT_METRO_PORT >>>` /
`# <<< splashdown-managed RCT_METRO_PORT <<<` (`wiring.py`), and
`_XCODE_BLOCK_RE` (`wiring.py`) is a non-greedy `DOTALL` match across that pair.
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
`doctor.py` is the higher orchestration layer: it reads the profile catalog, resolves launch
context through `launching.py`, combines app and project checks, and renders results. A recipe with
`[bootstrap]` adds the project hook check before framework resolution, so repair remains available
for minimal, generic, and ambiguous projects. This makes
the direction `doctor → profile modules → wiring → hooks`; no function-local import is needed to hide a
backward edge. Pylint's `cyclic-import` check enforces the acyclic package graph in CI.

## Key entry points

- `wiring.py` — `WiringCheck`, the RN check registry, concrete detect/autofix helpers, and
  `_HOOK_WIRING_CHECK`.
- `doctor.py` — `cmd_doctor`, framework/project target resolution, deduplication, and rendering.
- `hooks.py` — shared hook readiness, exact manager parsing, repair, and manual instructions.
- `profiles_mobile.py` — Profiles snapshot/reuse the registries via `list(...)` /
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
  bootstrap as the fallback. A sync-only instruction would not satisfy the check.

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
