# Framework wiring engine (`wiring.py`)

> Tech doc for `src/splashdown/wiring.py` — the `splash doctor` framework-wiring
> engine. HOW the code works (internals). For the user-facing model (what each
> check does, why wiring matters), see [`docs/features/framework-wiring.md`](../features/framework-wiring.md).

## Table of contents

- [Purpose](#purpose)
- [How it works (current state)](#how-it-works-current-state)
  - [The `WiringCheck` contract](#the-wiringcheck-contract)
  - [The two registries and their import-order coupling](#the-two-registries-and-their-import-order-coupling)
  - [`cmd_doctor`: resolve → run loop](#cmd_doctor-resolve--run-loop)
  - [The individual checks](#the-individual-checks)
  - [Why Electron integration is not a WiringCheck](#why-electron-integration-is-not-a-wiringcheck)
  - [Idempotency: sentinel-wrapped patches](#idempotency-sentinel-wrapped-patches)
  - [Import direction and the remaining lazy edge](#import-direction-and-the-remaining-lazy-edge)
- [Key entry points](#key-entry-points)
- [Gotchas](#gotchas)
- [Why](#why)
- [Related](#related)

## Purpose

Allocating a free port is only half the job. Most frameworks hardcode the dev
port — or override the env var — in one or two config files, so the value
splashdown writes into `splashdown.env` is silently ignored and the server boots
on its old default. `wiring.py` is the engine behind `splash doctor`: a
per-framework registry of small, inspectable facts about a project ("does
`metro.config.js` read `RCT_METRO_PORT`?") that the tool can detect and, where the
rewrite is safe and mechanical, auto-patch. `doctor` (no flag) is a read-only
`✓`/`✗` report; `doctor --fix` applies the safe autofixes and prints manual
snippets for the rest; scanner-driven `init` runs each detected app's safe fixes after
scaffolding so a fresh setup lands wired. Intent presets bypass the scanner and run
`doctor --fix` only when framework detection independently finds a Profile with checks.

## How it works (current state)

### The `WiringCheck` contract

`WiringCheck` (`wiring.py:25`) is a `NamedTuple` carrying everything `doctor` needs
to handle one fact: an `id`, a human `description`, `applies(cwd) -> bool`,
`detect(cwd) -> ("ok"|"problem", detail)`, an optional `autofix(cwd) -> None`, and
`manual_instructions(cwd) -> str`. The contract is deliberately three-state per
check: not-applicable (skip), ok, or problem — with two escape hatches on a
problem (an autofix that may exist, and manual instructions that always do).

`autofix is None` is the load-bearing signal for **report-only** checks: a check
with no safe mechanical rewrite (Spring Boot) sets it to `None`, and the run loop
never attempts a write, only prints the manual snippet. `manual_instructions` is
also `Optional` in the type but every shipped check supplies one.

### The two registries and their import-order coupling

Checks are owned by **Profiles**, not by `doctor` directly. The RN checks live in
a module-level list `_RN_WIRING_CHECKS` (`wiring.py:40`) that is populated by a
sequence of top-level `.append(...)` calls as each `rn-*` helper is defined
(`wiring.py:317`, `:407`, `:485`, `:567`). The shared `_HOOK_WIRING_CHECK`
(`wiring.py:579`) is a single check reused by native Profiles that otherwise have no
per-checkout wiring.

This is **order-dependent**, and the coupling runs through `profiles.py`:

- `profiles.py:21` does `from .wiring import _HOOK_WIRING_CHECK, _RN_WIRING_CHECKS`.
  Importing `wiring` executes its entire module body — including every
  `.append()` — before `profiles` resumes, so by the time any Profile method runs
  the list is fully built.
- `ReactNativeProfile.wiring_checks` (`profiles.py:1455`) returns
  `list(_RN_WIRING_CHECKS)` — a snapshot copy taken at call time. The native
  Profiles return `[_HOOK_WIRING_CHECK]` (`profiles.py:1520`, `:1536`).

Practically: any new RN check must be appended in `wiring.py`'s module body (not
lazily, not from another module after import), because Profiles read the populated
list. Vite and Spring Boot build their checks *inline* in their Profiles rather
than going through these registries (`profiles.py:516`, `:1162`).

### `cmd_doctor`: resolve → run loop

`cmd_doctor` (`wiring.py:217`) first resolves which framework to check via
`_resolve_doctor_framework` (`wiring.py:177`): an explicit `--framework` override
wins; else it loads the recipe (or an empty one) and calls `detect_framework`;
`DeviceError` collapses to `None`. A `None` framework prints a "pass `--framework`"
error and exits 1 (`wiring.py:221-226`). It then pulls the check list through
`_wiring_checks_for_framework` (`wiring.py:205`), which synthesizes an
`AppInventory` rooted at `cwd` and calls that Profile's `wiring_checks`; a
framework with no checks exits 0 with a note (`wiring.py:235-243`).

The run loop (`wiring.py:245`) walks each check:

1. `applies(cwd)` false → print "not applicable", skip.
2. `detect` returns `ok` → print `✓`.
3. `problem` **and** `--fix` **and** `autofix is not None` → call `autofix` (wrapped
   in `try/except` so one check failing reports rather than crashing the run,
   `wiring.py:255`), then re-`detect`. On `ok` print `✓ (fixed)`; otherwise print
   `✗ still problem after autofix` and the manual snippet, count it bad.
4. Otherwise (problem with no fix requested/available) → print `✗` plus the manual
   snippet, count it bad.

Exit code is 0 only when nothing is left in the `problem` state (`wiring.py:276`).
Scanner-driven init uses `_apply_init_wiring_checks` to run the same Profile-owned safe
autofixes per app. The intent-preset path resolves a framework from the checkout and calls
`cmd_doctor(cwd, fix=True)` only when checks exist (see
`docs/features/framework-wiring.md`).

### The individual checks

- **`rn-hook` / `hook`** (`_rn_hook_detect`, `wiring.py:279`; registered `:317`
  and shared as `_HOOK_WIRING_CHECK` `:579`) — verifies a `post-checkout` hook
  fires `splash`. Detection branches on the project's hook manager: lefthook config
  (`post-checkout:` + `run: splash`), husky `.husky/post-checkout`, the native hook
  in Git's common hooks directory, or any configured `core.hooksPath`
  (reported but not touched). Autofix delegates to
  `_ensure_post_checkout_hook` through `_autofix_ensure_post_checkout_hook`
  (`wiring.py:313`) so it coexists with the project's
  existing manager.
- **`rn-metro-config`** (`_rn_metro_detect`, `wiring.py:345`; registered `:407`) —
  `metro.config.js` should read `process.env.RCT_METRO_PORT`. Autofix
  (`_rn_metro_autofix`, `wiring.py:374`) recognizes three object-literal shapes
  (documented in the comment at `wiring.py:329`): a literal `port: <N>` is rewritten
  in place to `Number(process.env.RCT_METRO_PORT) || <N>` keeping the literal as
  fallback; an existing `server: {` block gets a port line inserted at its open
  brace; a bare `const config = {` / `module.exports = {` object gets a whole
  `server` block injected. The brace-aware injection lives in `_rn_metro_inject`
  (`wiring.py:356`), driven by three regexes (`wiring.py:335`–`:337`). If none
  match, autofix returns without writing and the check surfaces manual instructions.
- **`rn-pkg-port`** (`_rn_pkg_detect`, `wiring.py:448`; registered `:485`) — strips
  a hardcoded `--port <N>` from `package.json` scripts that boot Metro. The target
  set (`_pkg_scripts_with_port`, `wiring.py:432`) is the default RN script names
  `start`/`ios`/`android`, plus any script invoking `react-native start`. The
  `react-native start` match (`wiring.py:425`) is deliberately narrow so `--port` on
  unrelated tools (`react-native-test-runner --port 4000`) is left alone. Autofix
  re-serializes `package.json` with 2-space indent.
- **`rn-xcode-env`** (`_rn_xcode_detect`, `wiring.py:531`; registered `:567`) —
  `ios/.xcode.env` should source `RCT_METRO_PORT` from this checkout's
  `splashdown.env`. Detection treats *any* mention of `splashdown.env` as ok
  (sentinel block, hand-written conditional, whatever). Autofix (`wiring.py:542`)
  strips any static literal export, strips any prior sentinel block, then appends a
  sentinel-wrapped managed block (`_XCODE_BLOCK`, `wiring.py:502`): honor a value
  already set by `run-ios`, else read `splashdown.env`, else fall back to 8083.
- **`vite-config-process-env`** (returned by `ViteProfile.wiring_checks`,
  `profiles.py:516`; the check factory at `:551`, detect/autofix at `:562`/`:572`) —
  rewrites the `loadEnv` idiom `env.X` to `process.env.X` in `vite.config.{ts,js,mjs}`
  so values loaded into the shell by mise/direnv/devbox reach Vite. The matcher
  (`profiles.py:483`) skips already-fixed `process.env.X`.
- **`springboot-application-properties`** (defined inline in the Spring Boot
  Profile, `profiles.py:1162`; check factory at `:1166`, detect at `:1210`) — checks that
  `application.properties`/`application.yml` uses the `server.port=${PORT:8080}`
  placeholder. **Report-only**: `autofix=None` (`profiles.py:1174`), so `doctor`
  reports the `✗` and prints manual instructions but never rewrites the file, even
  under `--fix`.
- **`aspnet-launch-settings`** (`AspNetCoreProfile.wiring_checks`, `profiles.py:1258`;
  check tuple at `:1352`, detect/autofix at `:1367`/`:1381`) — flags any
  `"commandName": "Project"` profile in `Properties/launchSettings.json` that pins
  `applicationUrl`, which `dotnet run` honours ahead of an inherited
  `ASPNETCORE_HTTP_PORTS`. **Autofix present**, unlike the two report-only checks
  above: launchSettings is JSON, so the fix is `json.loads` → `pop("applicationUrl")`
  → `json.dumps(indent=2)` with no regex over whitespace-significant text.
  `_aspnet_project_profiles` (`:1338`) is what narrows the rewrite to `Project`
  profiles, leaving `IISExpress` entries untouched.

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
`# <<< splashdown-managed RCT_METRO_PORT <<<` (`wiring.py:498`–`:512`), and
`_XCODE_BLOCK_RE` (`wiring.py:514`) is a non-greedy `DOTALL` match across that pair.
Autofix strips any prior block by that regex before re-appending, so re-running
`--fix` replaces the block's contents in place rather than stacking copies. The
sentinels also document, in the file itself, which lines are tool-managed versus
hand-edited — edits *inside* the pair are overwritten on the next `--fix`. The
literal-export regex `_XCODE_LITERAL_EXPORT_RE` (`wiring.py:521`) matches only a
*static* `export RCT_METRO_PORT=<digits>` with no variable references, kept narrow
on purpose so a hand-written conditional/shell-substitution wiring is not mangled.

### Import direction and the remaining lazy edge

Hook integration now lives below both orchestration layers in `hooks.py`, so `wiring.py`
imports `_detect_hook_manager`, `_lefthook_config_path`, and
`_ensure_post_checkout_hook` directly at module load (`wiring.py:11`). It no longer reaches
back into `commands.py`.

The scanner edge remains lazy: `_wiring_checks_for_framework` imports
`PROFILES`/`AppInventory` inside the function (`wiring.py:208`) because `profiles.py` imports
the wiring registries while it populates `PROFILES`. `sys` also stays function-local on
the print-heavy paths so ordinary imports avoid that cold-path work.

## Key entry points

- `wiring.py:25` — `WiringCheck` NamedTuple (the check contract).
- `wiring.py:40` — `_RN_WIRING_CHECKS` registry; `:579` `_HOOK_WIRING_CHECK`.
- `wiring.py:217` — `cmd_doctor` (resolve framework, run loop, `--fix`, manual).
- `wiring.py:177` — `_resolve_doctor_framework`; `:205` `_wiring_checks_for_framework`.
- `wiring.py:279`/`:345`/`:448`/`:531` — `rn-hook`, `rn-metro-config`,
  `rn-pkg-port`, `rn-xcode-env` detect helpers.
- `wiring.py:356` — `_rn_metro_inject` (object-literal brace-aware injection).
- `wiring.py:498`–`:523` — `ios/.xcode.env` sentinel block and its regexes.
- `profiles.py:1455`/`:1520` — Profiles snapshot/reuse the registries via `list(...)` /
  `[_HOOK_WIRING_CHECK]`.
- `profiles.py:516`/`:1162` — `vite-config-process-env` and
  `springboot-application-properties` checks.

## Gotchas

- **`rn-metro` autofix only handles the recognized object-literal shapes.** A
  literal `port:`, an existing `server: {` block, or a bare config object literal
  are handled; anything else (functional config, spread merges, an exotic export
  shape) makes `_rn_metro_inject` return `None`, autofix returns without writing,
  and the check stays `✗` with the manual snippet printed (`wiring.py:391-398`).
- **RN-on-Android Metro port is not wired (known limitation).** Android bakes the
  Metro port into the build via the RN Gradle plugin, a different mechanism from
  iOS, and there is no check for it. The RN CLI propagates `RCT_METRO_PORT` to
  Gradle so `yarn android` works, but a bare `gradle assembleDebug` may default to
  8081.
- **Spring Boot is report-only by design.** `autofix=None` (`profiles.py:1174`): even
  under `--fix`, `doctor` will not silence a Spring Boot `✗` — it only prints the
  manual placeholder snippet. Do not "helpfully" add an autofix here without
  re-reading [Why](#why).
- **The registries' import-order coupling is real.** `_RN_WIRING_CHECKS` is
  populated by top-level `.append()` calls in `wiring.py`'s body; Profiles read it
  through the `from .wiring import …` at `profiles.py:21`. A new RN check must be
  appended in the module body (not registered lazily from elsewhere), or Profiles
  will snapshot a list that's missing it.
- **iOS port changes need a rebuild.** `RCT_METRO_PORT` is compiled into the iOS
  binary via `RCTBundleURLProvider`'s `defaultPort` (`wiring.py:503` block comment),
  so even a correctly wired `ios/.xcode.env` only takes effect after the app is
  rebuilt — wiring alone won't move a running build off its old port.
- **A configured `core.hooksPath` is reported, not auto-wired.** The hook check stays `✗`
  even under `--fix`; Splashdown never changes or writes into that configured path.

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
