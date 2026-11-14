# Framework wiring (`splash doctor`)

> PRD for **UC5** — make the allocated port actually reach the running app.
> See `docs/product/use-cases.md` (UC5) and `docs/product/persona.md`. `README.md`
> ("Framework wiring") is the authoritative user-facing specification.
> **Implemented by:** [wiring](../tech/wiring.md).

## Table of contents

- [Overview](#overview)
- [Behavior](#behavior)
- [Check coverage](#check-coverage)
- [Configuration](#configuration)
- [Gotchas](#gotchas)
- [Why](#why)

## Overview

Allocating a free port is only half the job. Framework config can hardcode a default or override
the inherited environment, causing the server to ignore the generated environment output without
an obvious error.

`splash doctor` evaluates small, framework-specific wiring facts. The read-only command prints a
`✓`/`✗` report. `splash doctor --fix` applies only safe mechanical rewrites and prints manual
instructions for report-only findings. Scanner-driven init runs the safe fixes after scaffolding,
rechecks them, and names the files it changed, so ordinary adoption never needs a follow-up
`splash doctor --fix`.

Electron user-data isolation is adjacent but not a wiring check. Init only points at the opt-in
recipe, because Splashdown cannot safely locate and rewrite an arbitrary Electron entrypoint.

## Behavior

Configured app/resource associations are authoritative: checks consume actual names, kinds,
resolved writers, and destinations. Init builds the same context from the final parsed recipe,
after resource renaming and template pruning. No declared port means skipped consumption
coverage. Multiple arbitrary ports are never selected by order or name similarity: Vite follows a
recognized consumer, while checks unable to disambiguate report incomplete coverage.
Dotenv paths come only from dotenv writers, relative to the app. Shell envrc output and non-file
writers are never treated as dotenv sources. Inherited-value checks establish consumption, not
whether a parent shell currently carries the value.

Doctor first parses the selected `splashdown.toml`. Missing or invalid configuration produces an
exit-1 diagnostic with its location and cause, without running checks or making repairs. Missing
configuration suggests `splash init`; invalid configuration is preserved for correction.

Every declared app is inspected at its path/profile, including apps sharing a profile. Shared
Loader, hook, Compose, and Watchman checks run once at the project root. With no apps, an explicit non-auto
`project.framework` supplies a root app. Without either, app coverage is explicitly skipped.
There is no filesystem guessing, launch-app selection, or framework override.

Outcomes identify app/path/check and distinguish ok, warning, problem, incomplete, and skipped.
Applicability, inspection, guidance, repair, or recheck failures preserve independent results.
Inspection failure never authorizes editing. Repairs retain changed paths and distinguish verified
repair, failure, and unverified changes. Cancellation retains completed work and exits 130.
Warnings and verified repairs exit 0; problems or incomplete checks exit 1; usage errors exit 2.
Text reports use stdout and operational diagnostics stderr. `--format json` emits one shared
result envelope containing all outcomes and the summary. No applicable checks is a coverage
statement, not evidence of health.

Plain doctor creates no registry state, lock files, or trust state and consumes no notices.
Eligible configuration repairs coordinate with existing checkout mutations using only a lock
sidecar, reload their recipe/check context under that lock, and hold it through recheck. The
framework repairs use the actual resource contract. Activation callbacks are disabled; doctor
never activates hooks or installs dependencies.

Init runs the same detect, fix, recheck sequence over the project-owned checks of every detected
app, skipping the activation-class hook check. Integration that already consumes the right values
is recognized and left byte-identical. Integration that is unrecognized or unsupported is reported
as a `✗` with the exact manual edit, never as readiness, and the file is left as the user wrote it.
Files whose bytes changed are added to init's reported change list, including partial writes before
a repair failure. Inspection exceptions never authorize repair, and guidance failures are isolated.

Every writable check reopens its destination through the shared safe-edit path. Checkout-owned
config paths must stay below the checkout with no symlinked parent, and every destination must be a
regular file. Rewrites preserve an existing mode and use same-directory atomic replacement. The
native hook is local activation and is never written by doctor.

A check that returns `✓` is affirming its human description. Detection therefore strips comments,
examines the relevant value slot, and reports unsupported analysis as incomplete. Implementation and
extension rules live in [Framework wiring engine](../tech/wiring.md).

## Check coverage

| Check | What it verifies | Fix policy |
| --- | --- | --- |
| Post-checkout integration | Project-root Git integration, independently of framework, with separate configuration and local activation outcomes | Recognized manager configuration only; native installation and activation are manual |
| Selected loader | Effective env destination, reused directives, tool presence, and observed approval | Configuration-only safe plan; no grants or loader activation |
| React Native Metro | Metro config, package scripts, and `ios/.xcode.env` consume `RCT_METRO_PORT` from the configured output | Safe recognized shapes; manual otherwise |
| CocoaPods manifest | Per-app raw lock/installed-manifest equality for established native layouts, with separate missing, different and unreadable findings | Read-only, including `--fix`; normal preparation or Bundler-aware `pod install` guidance |
| React Native/Expo Watchman | Existing ancestor roots are advisory sharing risks, with failed inspection incomplete | Report-only, bounded no-spawn/no-local query once at checkout root; absent optional tool skips |
| Vite | Actual managed key reaches `server.port` through process env or an admitting `loadEnv` prefix | Narrow excluded-key fallback repair; unknown flows manual |
| Astro | Top-level dev server port consumes `WEB_DEV_PORT` | Safe recognized object shapes |
| Angular | `ng serve` receives `--port $WEB_DEV_PORT` | Safe package-script rewrite |
| Deno | Task flag order or a recognized `Deno.serve` port expression consumes the declared key | Task rewrite preserves numeric fallbacks; source changes manual |
| Compose | Fixed host ports, container names and absolute bind sources identify possible cross-checkout interference or data sharing | Static report-only advisory findings; unsupported/unreadable YAML is incomplete |
| Spring Boot | Every active config uses a `PORT` placeholder | Report-only |
| Laravel | Backend environment plus Vite asset-server port | Vite port check when Vite is present |
| ASP.NET Core | Launch profiles do not override the inherited HTTP port | Safe JSON rewrite on .NET 8+; older TFMs report-only |

React Native and native iOS/Android Profiles declare wiring checks. Expo and Flutter have
no built-in port-wiring rewrites. Mobile apps also receive launcher-tool and applicable CocoaPods
checks, and React Native/Expo receive the project-level Watchman check. CocoaPods equality proves
only manifest consistency, including after a changed Podfile. Unknown/dynamic native layouts are
explicit coverage limits, and ungenerated Expo native projects skip Pods inspection. A verified
Android tool route makes missing/different optional iOS Pods advisory only within that same app.
Unreadable inputs remain incomplete. This standalone snapshot does not gate preparation or run. Environment-only web/server Profiles need no file rewrites and
receive an explicit skipped-coverage verdict.

## Configuration

- `splash doctor` reports without writing.
- `splash doctor --fix` applies safe autofixes and prints manual instructions for the rest.
- `splash doctor --format json` returns the shared result envelope.
- App/check selectors and framework overrides are not supported.
- Check lists are Profile-owned; there is no per-check toggle.
- The React Native `ios/.xcode.env` block is sentinel-managed. Edits inside its marker pair are
  overwritten by the next fix, including when the configured destination changes.
- Destination-aware checks use the resource's resolved writer destination, including per-resource
  overrides. Shell and non-file writers never fabricate a dotenv destination.

## Gotchas

- **RN-on-Android Metro port remains a known limitation.** The RN CLI propagates
  `RCT_METRO_PORT` to Gradle, but a bare Gradle build can fall back to 8081.
- **Spring Boot and Compose are report-only.** Rewriting arbitrary YAML, properties, and Compose
  layouts without their parsers would be riskier than giving an exact manual instruction.
- **iOS Metro port changes need a rebuild.** The port becomes part of the iOS binary; repairing
  `.xcode.env` does not change an already-built app.
- **Watchman checks do not change watches.** Doctor queries the existing daemon within three
  seconds and skips absent optional Watchman without claiming root inspection. Invalid responses,
  failed queries and ambiguous nonzero exits remain incomplete. Ancestor watches are advisory,
  with concrete roots and guidance to review shared use. A valid list with no ancestor proves
  only the inspected watch-root property. Checkout-controlled executables are refused.
- **Compose isolation is advisory.** File/service/value slots identify fixed host ports and
  container names, plus absolute short/long bind sources that can share data despite distinct
  ports and project names. Relative sources, ordinary named volumes and interpolated values are
  distinguished. Unknown YAML stays incomplete. Guidance uses real recipe declarations without
  assuming allocation, service-to-resource roles or parent-shell activation. Intentional sharing
  remains valid. Neither mode queries Docker/Colima or changes Compose files or data.
- **A configured `core.hooksPath` is never taken over.** Doctor reports it and prints manual
  event-forwarding instructions even in fix mode. Overcommit and an unresolved conflict between
  two hook managers are handled the same way.
- **Vite accepts inherited `loadEnv` values.** Its default `VITE_` filter excludes unprefixed
  keys, while `""` or a matching literal string/array admits them independently of the dotenv
  directory. `envPrefix` is separate. Repairs add an inherited fallback only to the managed port
  read, preserving unrelated `API_URL`/dotenv settings and handwritten fallbacks. Dynamic prefixes,
  mutated bindings, arithmetic transformations, and unsupported config flows remain incomplete.
- **`ios/.xcode.env` is read through its `RCT_METRO_PORT` assignments.** A dotenv path named for
  any other purpose wires nothing, so it is ignored in both directions: it never counts as wiring,
  and it never blocks the fix. A port wiring that reads another dotenv is a problem, not a
  rewrite, because splashdown cannot tell whether that reference is load-bearing, so it preserves
  the file and prints the edit.
- **JSON edits preserve unrelated bytes.** React Native, Angular, Deno, and ASP.NET Core
  splice only the affected members. Numeric shell port arguments retain their value as fallback.
  Handwritten fallback expressions are preserved or left for manual repair.


## Why

A correctly allocated variable that the framework silently ignores is indistinguishable from a
resource collision during development. Safe mechanical fixes remove that failure mode; explicit
report-only checks preserve hand-authored configuration where a rewrite would require guessing.
