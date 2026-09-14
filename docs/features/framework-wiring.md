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

Checks that patch a consumer of generated values are built against the environment output
destination this checkout writes, which `splash init --env-file` configures and the recipe records.
A check judges the wiring against that path, so a project delivering values to `.env` is not
reported wired because some file mentions `splashdown.env`, and a fix writes the path that
resolves. Where the app sits below the project root the destination is spelled relative to the
app directory.

Checks come from the resolved app Profile plus project-level checks such as Compose and bootstrap
hook readiness. Profile checks run in the resolved app directory. Project checks run at the
checkout root, including the Watchman check added for React Native and Expo.
Project-level checks still run when framework detection is ambiguous. Duplicate checks are removed
by id.

For each applicable check, doctor detects the current state. In fix mode it runs an available
autofix, detects again, and reports either `fixed` or the remaining problem and manual action. One
check raising or reading an unfamiliar shape is a `✗`, never a false green. Exit status is zero
only when no applicable check remains in the problem state.

Init runs the same detect, fix, recheck sequence over the project-owned checks of every detected
app, skipping the activation-class hook check. Integration that already consumes the right values
is recognized and left byte-identical. Integration that is unrecognized or unsupported is reported
as a `✗` with the exact manual edit, never as readiness, and the file is left as the user wrote it.
Files whose bytes changed are added to init's reported change list.

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
| React Native Metro | Metro config, package scripts, and `ios/.xcode.env` consume `RCT_METRO_PORT` from the configured output | Safe recognized shapes; manual otherwise |
| React Native/Expo Watchman | No existing Watchman root is an ancestor of the checkout | Report-only when Watchman is installed |
| Vite | Shell environment reads and use of `WEB_DEV_PORT` | Env-read rewrite; port consumption report-only |
| Astro | Top-level dev server port consumes `WEB_DEV_PORT` | Safe recognized object shapes |
| Angular | `ng serve` receives `--port $WEB_DEV_PORT` | Safe package-script rewrite |
| Deno | `deno serve` flag order or source-level `PORT` read | Task rewrite; source changes manual |
| Compose | Host ports and container names are checkout-specific | Report-only |
| Spring Boot | Every active config uses a `PORT` placeholder | Report-only |
| Laravel | Backend environment plus Vite asset-server port | Vite port check when Vite is present |
| ASP.NET Core | Launch profiles do not override the inherited HTTP port | Safe JSON rewrite on .NET 8+; older TFMs report-only |

React Native and native iOS/Android Profiles declare checks. Expo's doctor coverage is the
project-level Watchman check. Flutter declares no wiring checks; absent project checks, doctor
reports that neutral state. Environment-only web/server Profiles need no file rewrites and
receive an explicit env-only success verdict.

## Configuration

- `splash doctor` reports without writing.
- `splash doctor --fix` applies safe autofixes and prints manual instructions for the rest.
- `splash doctor --framework=NAME` overrides framework detection with a registered Profile name;
  an unknown name is a usage error and cannot pass as an empty check set.
- Check lists are Profile-owned; there is no per-check toggle.
- The React Native `ios/.xcode.env` block is sentinel-managed. Edits inside its marker pair are
  overwritten by the next fix, including when the configured destination changes.
- There is no per-check destination override. Every destination-aware check reads the recipe's
  single environment output setting.

## Gotchas

- **RN-on-Android Metro port remains a known limitation.** The RN CLI propagates
  `RCT_METRO_PORT` to Gradle, but a bare Gradle build can fall back to 8081.
- **Spring Boot and Compose are report-only.** Rewriting arbitrary YAML, properties, and Compose
  layouts without their parsers would be riskier than giving an exact manual instruction.
- **iOS Metro port changes need a rebuild.** The port becomes part of the iOS binary; repairing
  `.xcode.env` does not change an already-built app.
- **Watchman checks do not change watches.** Doctor queries the existing daemon within three
  seconds and skips absent Watchman. Invalid responses and failed queries are problems. A watch
  at the checkout root or no ancestor watch passes this check without proving Metro readiness.
  An ancestor finding asks the user to review shared use before removing that watch and
  restarting Metro.
- **A configured `core.hooksPath` is never taken over.** Doctor reports it and prints manual
  event-forwarding instructions even in fix mode.
- **Vite's env rewrite is narrow.** It changes matched `env.X` reads to `process.env.X` but leaves
  the `loadEnv` call and deliberate shell-then-dotenv fallbacks intact. It is skipped when Vite
  already loads the configured destination itself, which needs a destination Vite loads in
  `development` mode sitting directly in the Vite root, a `loadEnv` call given that root, and the
  empty prefix.
- **`ios/.xcode.env` is read through its `RCT_METRO_PORT` assignments.** A dotenv path named for
  any other purpose wires nothing, so it is ignored in both directions: it never counts as wiring,
  and it never blocks the fix. A port wiring that reads another dotenv is a problem, not a
  rewrite, because splashdown cannot tell whether that reference is load-bearing, so it preserves
  the file and prints the edit.
- **The React Native `package.json` fix reformats the file.** Stripping `--port` re-serializes the
  JSON with two-space indent. Every key and value survives; the original layout does not.

## Why

A correctly allocated variable that the framework silently ignores is indistinguishable from a
resource collision during development. Safe mechanical fixes remove that failure mode; explicit
report-only checks preserve hand-authored configuration where a rewrite would require guessing.
