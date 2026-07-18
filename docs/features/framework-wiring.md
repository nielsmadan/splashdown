# Framework wiring (`splash doctor`)

> PRD for **UC5** — make the allocated port actually reach the running app.
> See `docs/product/use-cases.md` (UC5) and `docs/product/persona.md`. `README.md`
> ("Framework wiring") is the authoritative spec.
> **Implemented by:** [wiring](../tech/wiring.md).

## Table of contents

- [Overview](#overview)
- [How it works (current state)](#how-it-works-current-state)
- [Key entry points](#key-entry-points)
- [Configuration](#configuration)
- [Gotchas](#gotchas)
- [Why](#why)

## Overview

Allocating a free port (UC1) is only half the job: most frameworks hardcode the
dev port — or override the env var — in one or two config files, so the value
splashdown writes into `splashdown.env` is silently ignored and the server boots
on its old default (8081, 5173, 8080). The symptom is a confused "why is it still
on 8081?" hunt — exactly the kind of phantom failure the parallel-agent persona
burns tokens on.

`splash doctor` carries a per-framework registry of **wiring checks**. Each check
names one fact about a project (e.g. "`metro.config.js` consumes `RCT_METRO_PORT`"),
detects whether it holds, and — where the rewrite is safe and mechanical —
auto-patches it. `splash doctor` is a read-only `✓`/`✗` report; `splash doctor --fix`
applies the safe autofixes and prints manual instructions for the rest; `splash init`
runs the fixing pass after scaffolding so a fresh setup lands wired.

Checks covered today: the post-checkout hook (`rn-hook`/`hook`), RN
`metro.config.js`, RN `package.json` scripts, RN `ios/.xcode.env`, Vite config, and
Spring Boot `application.properties` (report-only).

## How it works (current state)

The unit is `WiringCheck` (`src/splashdown/wiring.py:22`): an `id`, a human
`description`, `applies(cwd)`, `detect(cwd) -> ("ok"|"problem", detail)`, an optional
`autofix(cwd)` (`None` means manual-only), and `manual_instructions(cwd)`.

Checks are owned by **Profiles**, not by `doctor` directly. RN checks accumulate in
the module-level list `_RN_WIRING_CHECKS` (`src/splashdown/wiring.py:37`) as each
`rn-*` helper is appended; the shared `_HOOK_WIRING_CHECK`
(`src/splashdown/wiring.py:438`) is reused by native presets that otherwise have no
per-checkout wiring. Vite and Spring Boot define their checks inline in their
Profiles. `cmd_doctor` resolves the active framework (override, else recipe, else
`detect_framework`) via `_resolve_doctor_framework` (`src/splashdown/wiring.py:40`),
then pulls that framework's check list through `_wiring_checks_for_framework`
(`src/splashdown/wiring.py:52`), which synthesizes an `AppInventory` rooted at `cwd`
and calls `Profile.wiring_checks`.

The run loop in `cmd_doctor` (`src/splashdown/wiring.py:64`): for each check, skip if
`applies` is false; if `detect` returns `ok`, print `✓`; on `problem`, if `--fix` was
passed and an `autofix` exists, run it, re-`detect`, and report `(fixed)` or
fall through to manual instructions; otherwise print `✗` plus the manual snippet.
The autofix call is wrapped so one check failing reports rather than crashing the
run (`src/splashdown/wiring.py:93`). Exit code is 0 only when nothing is left in the
`problem` state.

The individual checks:

- **`rn-hook` / `hook`** (`src/splashdown/wiring.py:118`, registered at
  `src/splashdown/wiring.py:173` and `:438`) — verifies a `post-checkout` hook fires
  `splash`. Detection branches on the project's hook manager: lefthook config, husky
  `.husky/post-checkout`, a clean `.githooks` + `core.hooksPath`, or a foreign
  `core.hooksPath` (reported, not touched). Autofix delegates to
  `_ensure_post_checkout_hook` so it coexists with the project's existing manager.
- **`rn-metro-config`** (`src/splashdown/wiring.py:201`, registered `:263`) —
  `metro.config.js` should read `process.env.RCT_METRO_PORT`. Autofix handles three
  shapes (`src/splashdown/wiring.py:185` comment): a literal `port: <N>` is rewritten
  to `Number(process.env.RCT_METRO_PORT) || <N>` keeping the literal as fallback; an
  existing `server: {` block gets a port line; a bare config object gets a `server`
  block injected. Unrecognized shapes fall through to manual instructions.
- **`rn-pkg-port`** (`src/splashdown/wiring.py:304`, registered `:341`) — strips a
  hardcoded `--port <N>` from `package.json` scripts that boot Metro (`start`/`ios`/
  `android`, or any script invoking `react-native start`), so the RN CLI reads
  `RCT_METRO_PORT` from the environment. The `react-native start` match is deliberately
  narrow (`src/splashdown/wiring.py:281`) so `--port` on unrelated tools is left alone.
- **`rn-xcode-env`** (`src/splashdown/wiring.py:387`, registered `:424`) — `ios/.xcode.env`
  should source `RCT_METRO_PORT` from this checkout's `splashdown.env`. Autofix strips
  any static literal export and appends a sentinel-wrapped managed block
  (`src/splashdown/wiring.py:356`): honor a value already set by `run-ios`, else read
  `splashdown.env`, else fall back to 8083. Sentinels make the patch idempotent
  (find-by-pair, replace) and mark tool-managed vs hand-edited lines. The literal-export
  regex is intentionally narrow (`src/splashdown/wiring.py:377`) so hand-written
  conditional wirings are not mangled.
- **`vite-config-process-env`** (`src/splashdown/profiles.py:321`, detect/autofix at
  `:332`/`:342`) — rewrites the `loadEnv` idiom `env.X` to `process.env.X` in
  `vite.config.{ts,js,mjs}` so values loaded into the shell by mise/direnv/devbox reach
  Vite. The matcher (`src/splashdown/profiles.py:289`) skips already-fixed
  `process.env.X`.
- **`springboot-application-properties`** (`src/splashdown/profiles.py:477`, detect at
  `:491`) — checks that `application.properties`/`application.yml` uses the
  `server.port=${PORT:8080}` placeholder. **Report-only**: `autofix=None`
  (`src/splashdown/profiles.py:486`) because rewriting Java/Spring config is too risky
  to mechanize; only manual instructions are printed.

`init` runs the same checks in fix mode after scaffolding the recipe, wiring the loader,
and installing the hook: the wiring loop is in `cmd_init`
(`src/splashdown/commands.py:1305`), and the legacy preset path calls
`cmd_doctor(cwd, fix=True)` directly (`src/splashdown/commands.py:1352`).

The hook-coexistence helpers are shared with `doctor` from `commands.py`:
`_detect_hook_manager` (`src/splashdown/commands.py:125`), `_lefthook_config_path`
(`:160`), and `_ensure_post_checkout_hook` (`src/splashdown/commands.py:267`), which
dispatches to the lefthook/husky/`.githooks` wiring writers.

## Key entry points

- `src/splashdown/wiring.py:22` — `WiringCheck` NamedTuple (the check contract).
- `src/splashdown/wiring.py:37` — `_RN_WIRING_CHECKS` registry; `:438` `_HOOK_WIRING_CHECK`.
- `src/splashdown/wiring.py:64` — `cmd_doctor` run loop (detect / `--fix` / manual).
- `src/splashdown/wiring.py:40` — `_resolve_doctor_framework`; `:52` `_wiring_checks_for_framework`.
- `src/splashdown/wiring.py:118`/`:201`/`:304`/`:387` — `rn-hook`, `rn-metro-config`, `rn-pkg-port`, `rn-xcode-env` detect/autofix.
- `src/splashdown/wiring.py:356` — `ios/.xcode.env` sentinel-wrapped managed block.
- `src/splashdown/profiles.py:321`/`:477` — `vite-config-process-env` (autofix) and `springboot-application-properties` (report-only) checks.
- `src/splashdown/cli.py:185` — `doctor` argparse parser (`--fix`, `--framework`); dispatch at `src/splashdown/cli.py:358`.
- `src/splashdown/commands.py:1305` — `init` post-scaffold wiring loop; `:267` `_ensure_post_checkout_hook` (shared hook coexistence).

## Configuration

- `splash doctor` — read-only `✓`/`✗` report; exit 0 only if nothing is in `problem`.
- `splash doctor --fix` — apply safe autofixes, re-detect, print manual instructions for
  whatever can't be auto-fixed (`src/splashdown/cli.py:186`).
- `splash doctor --framework=NAME` — override detection with any profile name (e.g.
  `react-native`, `flutter`, `expo`, `vite`, `springboot`); used when detection can't
  resolve a framework (`src/splashdown/cli.py:191`).
- The active framework's check list comes from its Profile (`Profile.wiring_checks`);
  there is no per-check toggle. Adding a check means appending to `_RN_WIRING_CHECKS`
  or returning it from a Profile's `wiring_checks`.
- The `ios/.xcode.env` managed block is delimited by the sentinel pair
  `# >>> splashdown-managed RCT_METRO_PORT >>>` / `# <<< ... <<<`
  (`src/splashdown/wiring.py:356`) — edits inside it will be overwritten by `--fix`.

## Gotchas

- **RN-on-Android Metro port is not wired (known limitation).** Android bakes the Metro
  port into the build via the RN Gradle plugin / `BuildConfig`, a different mechanism
  from iOS, and splashdown has no check for it. `yarn android` works because the RN CLI
  propagates `RCT_METRO_PORT` to Gradle, but a bare `gradle assembleDebug` may default to
  8081. Tracked as a future check (see `README.md`, "Framework wiring" → Known limitation).
- **Spring Boot checks are manual / report-only by design.** `autofix=None`
  (`src/splashdown/profiles.py:486`) — `doctor` reports whether
  `server.port=${PORT:8080}` is present but never rewrites Java/Spring config, because
  mechanical edits there are too risky. `--fix` will not silence a Spring Boot `✗`.
- **iOS port changes need a rebuild.** `RCT_METRO_PORT` is compiled into the iOS binary
  (via `RCTBundleURLProvider`'s `defaultPort`), so even a correctly wired `ios/.xcode.env`
  only takes effect after the app is rebuilt — wiring alone won't move a running build off
  its old port (`src/splashdown/wiring.py:358` block comment).
- **Detection can come back empty.** If the framework can't be resolved (no recipe, no
  detectable framework) `doctor` errors and asks for `--framework`
  (`src/splashdown/wiring.py:69`); a framework with no checks exits 0 with a note
  (`src/splashdown/wiring.py:77`).
- **A foreign `core.hooksPath` is reported, not auto-wired.** If `core.hooksPath` points
  to a non-`.githooks` directory, the hook check stays a `✗` even under `--fix` and prints
  manual instructions (`src/splashdown/wiring.py:134`).
- **Vite autofix leaves `loadEnv` lines in place.** It only rewrites `env.X` reads to
  `process.env.X`; the `loadEnv` call itself is untouched
  (`src/splashdown/profiles.py:347` comment), so a stray `env.X` outside the matcher's
  shape won't be caught.

## Why

A bare allocated env var isn't enough: frameworks override it in config the user rarely
remembers exists, and the failure mode is silent (wrong port, not an error). For the
parallel-agent persona an agent can't tell a resource clash from a real bug, so "the
port is allocated but the server ignores it" turns into a wasted debugging loop.
Auto-patching the safe, mechanical cases — and clearly flagging the risky ones (Spring
Boot) as manual rather than guessing — keeps the allocated port actually reaching the
process while never corrupting hand-authored config.
