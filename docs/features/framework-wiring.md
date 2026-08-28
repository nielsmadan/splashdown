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
the inherited environment, causing the server to ignore `splashdown.env` without an obvious error.

`splash doctor` evaluates small, framework-specific wiring facts. The read-only command prints a
`✓`/`✗` report. `splash doctor --fix` applies only safe mechanical rewrites and prints manual
instructions for report-only findings. Scanner-driven init runs the safe fixes after scaffolding so
a fresh setup lands wired.

Electron user-data isolation is adjacent but not a wiring check. Init can add
`ELECTRON_PROFILE_ID` and prints the main-process integration, but Splashdown cannot safely locate
and rewrite an arbitrary Electron entrypoint.

## Behavior

Checks come from the resolved app Profile plus project-level checks such as Compose and bootstrap
hook readiness. They run in the resolved app directory, not blindly at the repository root.
Project-level checks still run when framework detection is ambiguous. Duplicate checks are removed
by id.

For each applicable check, doctor detects the current state. In fix mode it runs an available
autofix, detects again, and reports either `fixed` or the remaining problem and manual action. One
check raising or reading an unfamiliar shape is a `✗`, never a false green. Exit status is zero
only when no applicable check remains in the problem state.

Every writable check reopens its destination through the shared safe-edit path. Checkout-owned
config paths must stay below the checkout with no symlinked parent, and every destination must be a
regular file. Rewrites preserve an existing mode and use same-directory atomic replacement. The
native hook is rooted at Git's resolved hooks directory and receives the same final-file check.

A check that returns `✓` is affirming its human description. Detection therefore strips comments,
examines the relevant value slot, and reports unrecognized syntax as a problem. Implementation and
extension rules live in [Framework wiring engine](../tech/wiring.md).

## Check coverage

| Check | What it verifies | Fix policy |
| --- | --- | --- |
| Post-checkout hook | Exact event-aware Lefthook, Husky, or native hook readiness | Safe repair, except configured `core.hooksPath` |
| React Native Metro | Metro config, package scripts, and `ios/.xcode.env` consume `RCT_METRO_PORT` | Safe recognized shapes; manual otherwise |
| Vite | Shell environment reads and use of `WEB_DEV_PORT` | Env-read rewrite; port consumption report-only |
| Astro | Top-level dev server port consumes `WEB_DEV_PORT` | Safe recognized object shapes |
| Angular | `ng serve` receives `--port $WEB_DEV_PORT` | Safe package-script rewrite |
| Deno | `deno serve` flag order or source-level `PORT` read | Task rewrite; source changes manual |
| Compose | Host ports and container names are checkout-specific | Report-only |
| Spring Boot | Every active config uses a `PORT` placeholder | Report-only |
| Laravel | Backend environment plus Vite asset-server port | Vite port check when Vite is present |
| ASP.NET Core | Launch profiles do not override the inherited HTTP port | Safe JSON rewrite on .NET 8+; older TFMs report-only |

React Native and native iOS/Android Profiles declare checks. Expo and Flutter currently declare no
wiring checks; doctor reports that neutral state rather than claiming their configuration was
verified. Environment-only web/server Profiles need no file rewrites and receive an explicit
env-only success verdict.

## Configuration

- `splash doctor` reports without writing.
- `splash doctor --fix` applies safe autofixes and prints manual instructions for the rest.
- `splash doctor --framework=NAME` overrides framework detection with a registered Profile name;
  an unknown name is a usage error and cannot pass as an empty check set.
- Check lists are Profile-owned; there is no per-check toggle.
- The React Native `ios/.xcode.env` block is sentinel-managed. Edits inside its marker pair are
  overwritten by the next fix.

## Gotchas

- **RN-on-Android Metro port remains a known limitation.** The RN CLI propagates
  `RCT_METRO_PORT` to Gradle, but a bare Gradle build can fall back to 8081.
- **Spring Boot and Compose are report-only.** Rewriting arbitrary YAML, properties, and Compose
  layouts without their parsers would be riskier than giving an exact manual instruction.
- **iOS Metro port changes need a rebuild.** The port becomes part of the iOS binary; repairing
  `.xcode.env` does not change an already-built app.
- **A configured `core.hooksPath` is never taken over.** Doctor reports it and prints manual
  event-forwarding instructions even in fix mode.
- **Vite's env rewrite is narrow.** It changes matched `env.X` reads to `process.env.X` but leaves
  the `loadEnv` call and deliberate shell-then-dotenv fallbacks intact.

## Why

A correctly allocated variable that the framework silently ignores is indistinguishable from a
resource collision during development. Safe mechanical fixes remove that failure mode; explicit
report-only checks preserve hand-authored configuration where a rewrite would require guessing.
