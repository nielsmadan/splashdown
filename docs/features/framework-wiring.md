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

The unit is `WiringCheck` (`src/splashdown/wiring.py:23`): an `id`, a human
`description`, `applies(cwd)`, `detect(cwd) -> ("ok"|"problem", detail)`, an optional
`autofix(cwd)` (`None` means manual-only), and `manual_instructions(cwd)`.

Checks are owned by **Profiles**, not by `doctor` directly. RN checks accumulate in
the module-level list `_RN_WIRING_CHECKS` (`src/splashdown/wiring.py:38`) as each
`rn-*` helper is appended; the shared `_HOOK_WIRING_CHECK`
(`src/splashdown/wiring.py:453`) is reused by native presets that otherwise have no
per-checkout wiring. Vite and Spring Boot define their checks inline in their
Profiles. `cmd_doctor` resolves the active framework (override, else recipe, else
`detect_framework`) via `_resolve_doctor_framework` (`src/splashdown/wiring.py:41`),
then pulls that framework's check list through `_wiring_checks_for_framework`
(`src/splashdown/wiring.py:60`), which synthesizes an `AppInventory` rooted at `cwd`
and calls `Profile.wiring_checks`.

The run loop in `cmd_doctor` (`src/splashdown/wiring.py:72`): for each check, skip if
`applies` is false; if `detect` returns `ok`, print `✓`; on `problem`, if `--fix` was
passed and an `autofix` exists, run it, re-`detect`, and report `(fixed)` or
fall through to manual instructions; otherwise print `✗` plus the manual snippet.
Both the autofix and the `detect` calls are wrapped (`_run_detect`) so one check
failing reports rather than crashing the run. A raising `detect` becomes a `✗`, never
a `✓` — an unreadable file is precisely the case where the check knows nothing.
Exit code is 0 only when nothing is left in the `problem` state.

**A `✓` prints `check.description`, not `detail`** — which is what makes a false green
here worse than a missing check. The line the user reads is an affirmative claim
("compose file templates its host ports and container names") assembled from the check's
own advertisement, so a `detect` that returns `ok` on input it never parsed does not
merely stay silent, it vouches. Two disciplines follow from that, and every check obeys
them: **strip comments before scanning** (`_strip_hash_comments` / `_strip_js_comments`,
`src/splashdown/wiring.py:47`/`:67`) so leftover commented-out wiring is not read as wiring —
this was itself violated on arrival by `_deno_sources_read_port` and all three branches of
`_rn_hook_detect`, the last of which greened a fully commented-out lefthook block;
and **return `problem` for anything unrecognized**, with a detail saying so, rather than
falling through to the `ok` at the bottom of the function. The lexical helpers exist
because splashdown ships no YAML/JS parser (deps frozen at two) — `_yaml_key_regions`
walks a key's value region in either block or flow style, which is enough to read a
config honestly without pretending to parse it.

An empty check list is reported two different ways, gated on `Profile.env_only`
(`src/splashdown/profiles.py:420`). A profile that reads its port straight from the
environment (`nextjs`, `node-backend`, `django`, `fastapi`, `flask`, `rails`,
`laravel`) sets `env_only = True`,
so there is nothing to patch and `doctor` prints ``✓ no wiring checks needed for
`<name>` (env-only)`` — a positive verdict. Anything else with an empty list keeps the
``doctor: no wiring checks defined for framework `<name>`.`` wording. That distinction
is load-bearing: `expo` allocates `RCT_METRO_PORT` and runs Metro, so it genuinely
needs the same config patching `react-native` gets, and reporting it green would be
the exact false-pass this check exists to prevent. Membership in `PROFILES` is *not*
sufficient — a profile has to opt in. Both branches exit 0.

`cmd_doctor` resolves the app directory as well as the framework
(`_resolve_doctor_target`, `src/splashdown/wiring.py:49`). When the recipe places the
app in a subdirectory, checks run against that directory — running them at the
workspace root reports every check "not applicable" and exits 0 having inspected
nothing.

The individual checks:

- **`rn-hook` / `hook`** (`src/splashdown/wiring.py:137`, registered at
  `src/splashdown/wiring.py:188` and `:438`) — verifies a `post-checkout` hook fires
  `splash`. Detection branches on the project's hook manager: lefthook config, husky
  `.husky/post-checkout`, a clean `.githooks` + `core.hooksPath`, or a foreign
  `core.hooksPath` (reported, not touched). Autofix delegates to
  `_ensure_post_checkout_hook` so it coexists with the project's existing manager.
- **`rn-metro-config`** (`src/splashdown/wiring.py:216`, registered `:278`) —
  `metro.config.js` should read `process.env.RCT_METRO_PORT`. Autofix handles three
  shapes (`src/splashdown/wiring.py:227` comment): a literal `port: <N>` is rewritten
  to `Number(process.env.RCT_METRO_PORT) || <N>` keeping the literal as fallback; an
  existing `server: {` block gets a port line; a bare config object gets a `server`
  block injected. Unrecognized shapes fall through to manual instructions.
- **`rn-pkg-port`** (`src/splashdown/wiring.py:319`, registered `:356`) — strips a
  hardcoded `--port <N>` from `package.json` scripts that boot Metro (`start`/`ios`/
  `android`, or any script invoking `react-native start`), so the RN CLI reads
  `RCT_METRO_PORT` from the environment. The `react-native start` match is deliberately
  narrow (`src/splashdown/wiring.py:296`) so `--port` on unrelated tools is left alone.
- **`rn-xcode-env`** (`src/splashdown/wiring.py:402`, registered `:439`) — `ios/.xcode.env`
  should source `RCT_METRO_PORT` from this checkout's `splashdown.env`. Autofix strips
  any static literal export and appends a sentinel-wrapped managed block
  (`src/splashdown/wiring.py:373`): honor a value already set by `run-ios`, else read
  `splashdown.env`, else fall back to 8083. Sentinels make the patch idempotent
  (find-by-pair, replace) and mark tool-managed vs hand-edited lines. The literal-export
  regex is intentionally narrow (`src/splashdown/wiring.py:392`) so hand-written
  conditional wirings are not mangled.
- **`vite-config-process-env`** (`src/splashdown/profiles.py:705`, detect/autofix at
  `:716`/`:726`) — rewrites the `loadEnv` idiom `env.X` to `process.env.X` in
  `vite.config.{ts,js,mjs}` so values loaded into the shell by mise/direnv/devbox reach
  Vite. The matcher (`src/splashdown/profiles.py:638`) skips already-fixed
  `process.env.X`, and `_vite_unfixed_env_matches` (`:641`) additionally skips any
  `env.X` whose name is already read as `process.env.X` somewhere in the file —
  `process.env.X || env.X` is a deliberate shell-then-dotenv fallback chain, and
  rewriting its second term would silently delete the dotenv layer.
- **`vite-port-wired`** (`src/splashdown/profiles.py:679`) — the companion assertion:
  the config must name the allocated port var (`WEB_DEV_PORT`) somewhere, or the port
  is allocated and never consumed. **Report-only** (`autofix=None`) because injecting a
  `server.port` block into an arbitrary config is not safely mechanical. It deliberately
  tests for the variable name rather than the string `process.env.`, so bracket access
  and destructuring both pass — an earlier substring test flagged those correct configs
  as problems and left `doctor --fix` failing with nothing to fix.
- **`astro-config-port`** (`src/splashdown/profiles.py:583`, detect/autofix at `:594`/`:605`)
  — `astro.config.*` must set `server.port` from `WEB_DEV_PORT`. Astro is the one web
  profile that reads *neither* `PORT` from the environment nor a dotenv file for its dev
  port, so an unwired config silently boots on 4321 no matter what splashdown allocated;
  the port range starts at 4322 so that case can't masquerade as wired. The autofix injects
  the line after a `defineConfig({` or bare `export default {`. It deliberately bails when
  a `server:` block already exists anywhere in the file: Astro configs commonly carry a
  `vite: { server: {…} }`, and a regex can't tell that nesting apart from the top-level
  block, so guessing would put the port where it configures Vite's dev server instead.
- **`compose-hardcoded-ports`** (`src/splashdown/profiles.py:211`, detect at `:304`) —
  project-level, not owned by any Profile. Reports host-port mappings whose host side is a
  bare number and any literal `container_name:`. It reads every YAML layout, because the
  original line-anchored regexes (`^\s*-\s*["']?(\d+):\d+`) only saw block style and returned
  a green **"no hardcoded host ports"** on flow style — `ports: ['5432:5432']` and
  `{ container_name: db }` sailed through while both collide across worktrees. A missing
  check leaves you unsure; that one asserted safety. So detection now walks each `ports:`
  key's value region (`_yaml_key_regions`, `src/splashdown/wiring.py:136`), splits it into
  entries (`_split_flow_entries` / `_split_block_entries`, the latter keeping continuation
  lines so long syntax stays one entry), and classifies each (`_classify_port_entry`).
  Three details are each a bug that shipped: the region walker accepts a block sequence at
  its key's **own** indent (`ports:` then `- "5432:5432"` in the same column is legal YAML,
  and stopping at the dedent reported those files clean — worse than the regex it replaced);
  the key is matched inside a flow mapping too (`db: { ports: [...] }`), not only at line
  start; and only the **host slot** is tested for `$`, since testing the whole entry passed
  `"5432:${CONTAINER_PORT}"` as templated while the host side stayed pinned. Variables are
  masked to a colon-free sentinel before the slots are split, because `${VAR:-5432}` contains
  a colon. **Anything it can't classify is reported as a problem, never as ok** — an alias
  (`ports: *shared`), an unrecognized scalar. That's the whole point: the check is allowed to
  say "I couldn't read this", but never allowed to claim clean on a pattern it didn't parse.
  `_yaml_key_regions` strips comments itself (`_strip_hash_comments`) so no caller can forget.
  Still **report-only** (`autofix=None`): splashdown ships no YAML parser (deps are frozen at
  two), so a rewrite would be regex over indentation-sensitive text — reading a value region
  is bounded, rewriting one is not.
  `compose_project_resources` (`:189`) emits
  `COMPOSE_PROJECT_NAME` — the one value that needs no compose edit, since compose reads it
  from the environment — and deliberately invents no per-service ports, because which service
  deserves a pinned port is a judgement call and wrong guesses become config the user must
  undo. `cmd_doctor` appends these via `compose_wiring_checks` (`:207`) against the repo root,
  independent of the resolved framework.
- **`springboot-application-properties`** (`src/splashdown/profiles.py:1089`, detect at
  `:1133`) — checks that every config Spring may load uses the `server.port=${PORT:8080}`
  placeholder. `_springboot_declared_port` reads two spellings: the flat `server.port` form
  (all a `.properties` file can write) and YAML's nested `server:` block, which the original
  `server\.port\s*[:=]` regex could never match — so correctly wired YAML projects carried a
  permanent `✗`. Files are scanned individually via `_springboot_config_files`, which globs
  `application*` rather than reading two fixed names: a profile-specific
  `application-dev.properties` pinning a literal overrides a wired base file, and the old
  concatenate-then-search collapsed exactly that case into a green. **Report-only**:
  `autofix=None` because rewriting Java/Spring config is too risky to mechanize; only
  manual instructions are printed.
- **`laravel` → `vite-port-wired`** — Laravel is the only profile that claims two ports,
  because a Laravel app runs two dev servers: `php artisan serve` (`SERVER_PORT`, read
  straight from the environment) and, since Laravel 9, Vite for assets. It reuses vite's
  report-only port check for the second half and returns an empty list for API-only apps
  with no vite config, which then get the green `env_only` verdict. `LaravelProfile` is
  registered *ahead* of `ViteProfile` for the same reason: every modern Laravel app ships a
  `vite.config.js`, so vite would otherwise claim it and leave the PHP port unmanaged.
- **`aspnet-launch-settings`** (`src/splashdown/profiles.py:958`, detect at `:971`) —
  ASP.NET Core is the one profile whose env var is real but conditionally ignored:
  `ASPNETCORE_HTTP_PORTS` works, except `dotnet run` reads `applicationUrl` out of
  `Properties/launchSettings.json` first and that wins. The check flags any
  `"commandName": "Project"` profile declaring the key. **Autofix**: pops
  `applicationUrl` and rewrites the file — the one config rewrite here that *is* safely
  mechanical, because launchSettings is JSON and round-trips through `json.loads`/`dumps`
  rather than regex over indentation-sensitive text (contrast compose and Spring, both
  report-only for exactly that reason). `IISExpress` profiles keep their `applicationUrl`:
  only IIS Express reads it, so editing it would be a change with no effect on the port.
  Gated on the target framework (`_aspnet_supports_http_ports`): `ASPNETCORE_HTTP_PORTS`
  is .NET 8+, so net6.0/net7.0 projects get a report-only twin instead — there, dropping
  `applicationUrl` would hand the app the shared default 5000 and make collisions worse.

`init` runs the same checks in fix mode after scaffolding the recipe, wiring the loader,
and installing the hook: the wiring loop is in `cmd_init`
(`src/splashdown/commands.py:1305`), and the legacy preset path calls
`cmd_doctor(cwd, fix=True)` directly (`src/splashdown/commands.py:1352`).

The hook-coexistence helpers are shared with `doctor` from `commands.py`:
`_detect_hook_manager` (`src/splashdown/commands.py:125`), `_lefthook_config_path`
(`:160`), and `_ensure_post_checkout_hook` (`src/splashdown/commands.py:267`), which
dispatches to the lefthook/husky/`.githooks` wiring writers.

## Key entry points

- `src/splashdown/wiring.py:23` — `WiringCheck` NamedTuple (the check contract).
- `src/splashdown/wiring.py:38` — `_RN_WIRING_CHECKS` registry; `:438` `_HOOK_WIRING_CHECK`.
- `src/splashdown/wiring.py:215` — `cmd_doctor` run loop (detect / `--fix` / manual).
- `src/splashdown/wiring.py:175` — `_resolve_doctor_framework`; `:203` `_wiring_checks_for_framework`.
- `src/splashdown/wiring.py:277`/`:359`/`:462`/`:545` — `rn-hook`, `rn-metro-config`, `rn-pkg-port`, `rn-xcode-env` detect/autofix.
- `src/splashdown/wiring.py:373` — `ios/.xcode.env` sentinel-wrapped managed block.
- `src/splashdown/profiles.py:514`/`:488`/`:1089` — `vite-config-process-env` (autofix), `vite-port-wired` and `springboot-application-properties` (both report-only) checks.
- `src/splashdown/wiring.py:47`/`:67`/`:136` — `_strip_hash_comments` / `_strip_js_comments` / `_yaml_key_regions`, the lexical helpers every detect uses to read config without a parser.
- `src/splashdown/wiring.py:194` — `_run_detect`, the guard that turns a raising check into one `✗`.
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
  (`src/splashdown/wiring.py:373`) — edits inside it will be overwritten by `--fix`.

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
  (`src/splashdown/wiring.py:78`); a framework with no checks exits 0 with a note
  (`src/splashdown/wiring.py:90`).
- **A foreign `core.hooksPath` is reported, not auto-wired.** If `core.hooksPath` points
  to a non-`.githooks` directory, the hook check stays a `✗` even under `--fix` and prints
  manual instructions (`src/splashdown/wiring.py:151`).
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
