# Scanning & Extension Layer

The detection + extension layer: how splashdown looks at a repo on disk and decides
*what* it is (workspace shape, apps, frameworks, secondary capabilities, shell loader) before any provisioning
happens. `inventory.py` defines scan results, `catalog.py` owns the shared ordered profile
registry, and `scanner.py` performs filesystem detection. Framework rules are split across
`profile_core.py` and the `profiles_*` implementation modules, while `profiles.py` assembles
their detection order and provides the compatibility import surface. `runners.py` and
`launching.py` own launch behavior, `scaffolds.py` holds preset data, `agentdocs.py` renders
framework guidance, and `loaders.py` wires shell environments.

## Contents

- [Purpose](#purpose)
- [How it works (current state)](#how-it-works-current-state)
  - [scanner.py — repo → ProjectInventory](#scannerpy--repo--projectinventory)
  - [Profile modules — the framework extension point](#profile-modules--the-framework-extension-point)
  - [agentdocs.py — managed instruction-file guidance](#agentdocspy--managed-instruction-file-guidance)
  - [loaders.py — idempotent shell-env wiring](#loaderspy--idempotent-shell-env-wiring)
- [Key entry points](#key-entry-points)
- [Gotchas](#gotchas)
- [Why](#why)
- [Related](#related)

## Purpose

`splash init` and `splash init --rescan` need to answer, purely by inspecting the
filesystem: is this a monorepo or a single project? What package/build manager runs it?
Which apps live inside it, and what framework is each one? Which shell-env loader (if
any) has the user already adopted? The answers drive which default resources get written
into `splashdown.toml`, which consumer-config patches the doctor will offer, and how
`splashdown.env` reaches the running app.

This layer is **pure inspection on the detection side** (the `Scanner` never writes) and a
**registry/plugin extension point** on the framework side (`PROFILES`, `LOADERS`). Both
registries are dicts populated at import time, and **insertion order is load-bearing** for
detection precedence. Profile implementations do not mutate the catalog themselves:
`profiles.py` assembles the complete built-in sequence explicitly. `SCAFFOLDS` is a separate,
intentionally small registry of intent presets, not another list of supported frameworks.

## How it works (current state)

### scanner.py — repo → ProjectInventory

`Scanner.scan()` (`scanner.py`) is the single public entry. It runs workspace, loader,
profile, and capability detection, then assembles the `ProjectInventory` and `AppInventory`
records defined in `inventory.py`. No writes, no caching of significance — the same instance
is reusable.

**1. Workspace detection** — `_detect_workspace()` (`scanner.py`) returns one of
`pnpm | yarn | npm | cargo | gradle | single` by probing marker files in a fixed order:

- `pnpm-workspace.yaml` → `pnpm`.
- `package.json` with a truthy `workspaces` key → `yarn` if `yarn.lock` present, else
  `npm` (also the default when a workspace-shaped `package.json` has no lockfile signal).
- `Cargo.toml` containing a `[workspace]` table → `cargo`.
- `settings.gradle` / `settings.gradle.kts` → `gradle`.
- otherwise `single`.

The order matters: a pnpm monorepo usually also has a `package.json`, so pnpm is checked
first. JS workspace detection requires a *truthy* `workspaces` value; an empty array or object
falls through to `single`.

Package metadata consumers share `package_json.py`. Missing, unreadable, malformed, and
non-object JSON all produce an empty mapping, and only object-shaped dependency tables are
merged.

**2. App enumeration** — `_enumerate_apps()` (`scanner.py`) turns the workspace kind
into `[(name, path), ...]`:

- `single` short-circuits to one synthetic app `("main", cwd)` (`scanner.py`).
- `pnpm` hand-parses the `packages:` glob list out of `pnpm-workspace.yaml` with a
  minimal line reader (no YAML dependency); the first non-list, non-comment line ends the
  block (`scanner.py`).
- `yarn`/`npm` read `workspaces` from `package.json`, tolerating both the array form and
  the `{ packages: [...] }` object form (`scanner.py`).
- `cargo` extracts `[workspace] members` via stdlib `tomllib` (`scanner.py`).
- `gradle` regex-scrapes quoted tokens out of `settings.gradle*`, removes the optional
  leading `:`, and maps the remaining `:` separators to `/` (`:api:server` →
  `api/server`). It keeps only entries that resolve to real directories (`scanner.py`).
  A member without its own settings file is recognized as `android-native` only when its
  build file applies the Android application plugin.

Glob expansion is centralized in `_expand_workspace_globs()` (`scanner.py`). It only
understands a single trailing-ish `*` (it splits on the first `*` and lists the parent
dir's children), and it **excludes `node_modules` and dotdirs** while expanding
(`scanner.py`). There is no general recursive walk: the scanner trusts the
workspace manifest to point at app roots, so it never descends into `node_modules` or
`.git` — they are skipped structurally, not blacklisted.

**3. Profile matching** — for each enumerated app, `Scanner._match_profile()`
(`scanner.py`) iterates `PROFILES` in insertion order and returns the first
`profile.detect(app_path)` that is truthy, falling back to `"unknown"`. This is the only
place precedence is consumed; the ordering itself lives in `profiles.py` (see below).

**4. Secondary capability detection** — Electron is detected from package dependencies
without replacing the primary Profile. An Electron/Vite app remains `profile="vite"` and
also carries `capabilities=("electron",)`. Electron-only workspace members are retained;
other unmatched workspace members are still treated as shared libraries and omitted.

**5. Loader detection** — `_detect_loader()` (`scanner.py`) asks each `Loader` in
`LOADERS` order whether it `detect()`s, returning the first hit or `"none"`. Same
first-match-wins pattern as profiles.

**Cross-app resource-name collisions.** Profiles emit *canonical* resource names
(e.g. a Vite app wants `WEB_DEV_PORT`, a Next.js app wants `PORT`). When two apps of
overlapping profiles coexist, those names would collide in the single flat
`[resources.*]` table. `_build_resource_catalog()` builds the owner counts and, for any
name owned by more than one app, mangles every instance to `<NAME>_<APP>` (uppercased,
`-`→`_`): e.g. `WEB_DEV_PORT` becomes `WEB_DEV_PORT_ADMIN` /
`WEB_DEV_PORT_CUSTOMER`. Single-owner names stay canonical. The helper returns both the
flat resource table and the per-app `resources = [...]` lists, deriving each pair from
the same resolved name so declarations and references cannot diverge. If two app names
normalize to the same suffix, a stable digest disambiguates them while keeping valid
environment identifiers. Scanner-driven `splash init` checks for collisions before calling this
helper: when automatic output would need mangling, it writes a structure-only recipe and asks the
user to choose explicit monorepo resources. The mangling helper remains the shared catalog
mechanism for already-explicit/rescan flows.

`_should_defer_monorepo()` (`scanner.py`) has a second conservative trigger: an immediate sibling
Xcode or Gradle project that no enumerated app claims. `_unclaimed_native_dirs()` treats native
directories inside or containing a detected app as claimed, so the ordinary `ios/` and `android/`
subdirectories of a root React Native, Expo, or Flutter app do not cause deferral. Compose is not a
trigger. `cmd_init` decides whether to defer before it adds project-level Compose resources, because
a compose file alongside an otherwise unambiguous app does not make the scanner output unsafe.

`PROFILES` is declared in dependency-free `catalog.py`, filled in precedence order by
`profiles.py` at import, and read by scanner, recipe validation, launch dispatch, doctor,
and agent guidance. Those consumers share the catalog without importing one another.

### Profile modules — the framework extension point

A `Profile` (`profile_core.py`) is the per-framework integration contract. The base class
defines seven extension points and flags; subclasses override the ones that apply:

- `detect(app_path)` — filesystem predicate; the Scanner's match key.
- `resources(app)` — `{resource_name: {type, range, ...}}` to merge
  into `[resources.*]`. Names are canonical; the Scanner mangles on collision. Built-in
  port ranges start above the framework's default port so splashdown never allocates the
  conventional default.
- `targets(app)` — default device targets emitted during scanner-driven init.
- `wiring_checks(app)` — `WiringCheck`s the doctor runs to patch
  consumer configs (see `docs/tech/wiring.md`).
- `agent_guidance(app, port_names)` — framework-specific Markdown launch instructions.
  Init supplies the recipe's actual names after collision mangling. Common guidance is
  generated automatically for every app that references a port resource.
- `run(cwd, recipe, info)` — build+install+launch on a device. Only mobile/native profiles
  implement it and therefore satisfy the `RunnableProfile` protocol. Web/backend profiles
  deliberately expose no launch capability, and command preflight rejects them before
  provisioning or booting a target.
- `reads_dotenv` class flag — declares whether the framework picks up
  a plain `.env`/`.env.local` on its own (Next.js, Django, FastAPI, Flask, Rails, Laravel,
  Node backends → True; Vite, Spring Boot, ASP.NET Core, mobile → False). Consumed when no
  shell loader is present, to decide
  whether a dotenv-file fallback can actually reach the app.

Implementations are grouped by responsibility:

- `profiles_web.py` — Astro, Laravel, Nuxt, Angular, Vite, Node, Deno, and Next.js.
- `profiles_server.py` — Django, FastAPI, Flask, Spring Boot, ASP.NET Core, and Rails.
- `profiles_mobile.py` — Flutter, Expo, React Native, iOS native, and Android native,
  including their detection helpers and launcher delegation.
- `profiles_compose.py` — project-level Compose resources and wiring checks; Compose is
  infrastructure rather than an app profile, so it is not registered in `PROFILES`.

`profiles.py` imports those classes, re-exports the former module surface, and owns the one
explicit built-in catalog. Its tuple order is the **detection-precedence order**:

```
astro → laravel → nuxt → angular → vite → node-backend → deno → nextjs →
django → fastapi → flask → springboot → aspnetcore → rails → flutter → expo →
react-native → ios-native → android-native
```

Two orderings here are load-bearing and were both found by running real generated
projects rather than by reading detection code:

- **`laravel`, `nuxt` and `angular` before `vite`.** Laravel has shipped a `vite.config.js` since Laravel 9, so
  `ViteProfile` matches every modern Laravel app. Registered after vite, `LaravelProfile`
  was dead code on real projects and the PHP server's port went unmanaged. Its detection
  needs `artisan` *and* `laravel/framework` in `composer.json`, so it cannot steal a plain
  Vite app. Laravel is also the one profile that claims two ports (`SERVER_PORT` for
  `php artisan serve`, `WEB_DEV_PORT` for the asset server) because it runs two dev
  servers that both collide across worktrees. `nuxt` is Vite-based for the same reason
  (a Nuxt app that adds a vite.config would otherwise get a `WEB_DEV_PORT` that
  `nuxt dev` never reads), and `angular` is registered alongside them for symmetry
  since Angular's builder is Vite-backed from v17 on.
- **`flask` after `fastapi`.** Both substring-match the same `pyproject.toml` /
  `requirements.txt`, and flask is the more common incidental dependency of the two, so a
  project declaring both resolves to fastapi.

`reads_dotenv` is False for `angular` and `deno`: neither reads a dotenv file, and both
need their port threaded through a command line rather than an environment lookup.

The `_BUILTIN_PROFILES` tuple is applied at import; **tuple order is registration order is
detection precedence.** The mobile tail is deliberate: `flutter` (a `pubspec.yaml` wins even
if JS tooling leaks in) before `expo` (needs both an `expo` dependency and `app.json`) before
plain `react-native`. The two native profiles guard against false positives by first checking
`_has_js_or_flutter()` and bailing — an Expo app has an `.xcodeproj`, but it must not match
`ios-native`.

A couple of profiles carry real integration logic worth noting:

- **ViteProfile** (`profiles_web.py`) emits `WEB_DEV_PORT` unconditionally, and only adds
  `API_DEV_PORT` (as a `{{ PORT }}` template) when the Vite config mentions `proxy` — apps that
  don't proxy don't need the API's port. The test is a raw substring over the file text, so a
  commented-out proxy still counts. Because `API_DEV_PORT` renders `{{ PORT }}`, a merged `PORT`
  resource has to exist or init/rescan aborts and writes no recipe at all. Its
  wiring check rewrites `env.VAR` (the `loadEnv` idiom) to `process.env.VAR` so values
  loaded by the shell loader are visible.
- **The native iOS profile fails open on ambiguity.** An `.xcodeproj` is not proof of an iOS
  app — a macOS-only project matches the same globs but has no simulator to build for.
  `_pbxproj_targets_ios` (`profiles_mobile.py`) accepts immediately on
  `IPHONEOS_DEPLOYMENT_TARGET` or `SDKROOT = iphoneos`, and rejects only when the pbxproj names
  macOS and nothing names iOS. An unreadable pbxproj, a target-silent one, or a workspace-only
  layout is accepted, because deployment settings may live in an `.xcconfig` instead.
- **SpringBootProfile** (`profiles_server.py`) ships a wiring check whose `autofix` is
  `None` — patching Spring config is too risky to auto-rewrite, so
  it's report-only with manual instructions.

The `run()` overrides delegate to helpers in `runners.py` (`_flutter_run`, `_rn_run`,
`_expo_run`, `_ios_native_run`, `_android_native_run`). The two
native runners are the heavy ones — `_ios_native_run` drives `xcodebuild` and reads the
built `.app`'s `Info.plist` for the bundle id, branching to `devicectl` for physical
hardware vs `simctl` for simulators; `_android_native_run` drives Gradle install tasks and
resolves `applicationId` from the installed variant's AGP output metadata when not pinned,
falling back to Gradle properties for older builds. Recipe-supplied
positionals passed to these tools go through `_no_flag()` in `runners.py` to reject
leading-`-` values that argv would otherwise swallow as tool flags.

**SCAFFOLDS** in `scaffolds.py` contains exactly `minimal`, `server`, and `electron`.
Framework-specific and historical alias presets are deliberately absent: framework setup
comes from scanner-driven init and Profiles. Electron is the boundary case. Its explicit
preset deterministically requests a renderer port and stable profile identifier, while
plain init detects Electron as a secondary capability and asks whether to add only the
optional profile-isolation overlay.

### Profile-adjacent modules

Launch implementations, launch orchestration, scaffold data, and profile categories have
separate owners.

- **`runners.py`** — everything `Profile.run` delegates to: `_rn_run`, `_expo_run`,
  `_flutter_run`, `_ios_native_run`, `_android_native_run`, the xcodebuild/gradle
  argument builders, and the `[project] run` custom-command path. It also owns the two argv
  validators (`_no_flag`, `_android_component`) since they exist to sanitize values on
  their way into a subprocess. Its only device-layer dependency is the iOS runtime query
  used to produce architecture advice, imported lazily at the point of use; `DeviceError`
  comes directly from dependency-free `errors.py`, and devices never imports runners.
- **`launching.py`** — framework detection, workspace app-directory resolution, runnable-profile
  preflight, custom-command selection, and final `Profile.run` dispatch. It depends on the
  profile catalog and runners, while `devices.py` remains solely below it.
- **`scaffolds.py`** — the three intent-preset `splashdown.toml` templates and the
  `SCAFFOLDS` dict. Pure strings, no imports, no logic.
- **`profile_core.py` / `profiles_*.py`** — the base contract and categorized framework
  implementations. The implementation modules never register themselves; the facade's one
  `_BUILTIN_PROFILES` sequence keeps precedence reviewable and prevents import order from
  silently changing detection.

### agentdocs.py — managed instruction-file guidance

`agentdocs.py` turns a validated `Recipe` into concise, per-app Splashdown guidance. It
reads the import-populated `PROFILES` registry so each profile can add stable launch
commands through `Profile.agent_guidance`, while the common renderer names every actual
port resource from the recipe. In monorepos those are the post-mangling names, not the
profile's canonical defaults.

`commands.py` calls `sync_agent_guidance()` only after successful scanner init, preset
init, or rescan, including the structure-only deferred-monorepo path. A recipe with no
port-bearing apps renders no block and removes any previous complete block. Deinit calls
`remove_agent_guidance()` independently of recipe parsing, so malformed or missing recipes
cannot strand managed content.

The module mutates only existing root `AGENTS.md` and independent `CLAUDE.md` files. It
does not create either file, skips symlinks and non-regular files, and owns only the text
between its HTML sentinels. Complete blocks are replaced or removed idempotently;
malformed marker pairs are warned about and left unchanged. If `CLAUDE.md` imports an
existing `AGENTS.md`, synchronization removes any older complete local block before
leaving Claude to consume the shared file.

### loaders.py — idempotent shell-env wiring

A `Loader` (`loaders.py`) detects or selects a shell-env tool and idempotently
wires it to source `splashdown.env` on `cd`. Two methods: `detect(cwd)` and `wire(cwd)`.
Every `wire` is **idempotent** — re-running it produces no diff — and uses
sentinel-wrapped blocks so the managed region is visually obvious and machine-findable.

- **MiseLoader** detects `mise.toml`/`.mise.toml`; `wire()` delegates to
  `_ensure_mise_file_directive()` in `hooks.py`, which
  adds a `_.file` directive into mise's `[env]` so mise itself loads `splashdown.env`.
- **DirenvLoader** (`loaders.py`) detects `.envrc`/`.envrc.local`; `wire()` appends (or
  regex-replaces, between `_DIRENV_BEGIN`/`_DIRENV_END` sentinels at `loaders.py`) a
  block containing `dotenv_if_exists splashdown.env`. It uses `dotenv_if_exists` rather
  than `dotenv` so a fresh checkout doesn't hard-error before `splashdown.env` exists
  (`loaders.py`). `wire()` returns whether it created `.envrc`; `approve()` runs
  `direnv allow` (mise's runs `mise trust`) so the config actually loads. Editing a
  *pre-existing* `.envrc` invalidates direnv's trust hash but is not auto-approved — `wire()`
  prints the `direnv allow` reminder instead (a freshly-created file skips the reminder because
  `approve()` handles it).
- **DevboxLoader** (`loaders.py`) detects `devbox.json`; `wire()` parses the JSON, finds
  or appends a `shell.init_hook` entry carrying the `# splashdown-managed` marker
  (`loaders.py`), and the hook does `set -a; source splashdown.env; set +a`. It
  find-and-replaces by marker rather than parsing the hook string, normalizing a
  string-valued `init_hook` into a list first (`loaders.py`).
- **NoneLoader** (`loaders.py`) is the fallback. `detect()` is always `False`; it's
  only ever *selected* as the fallback, never matched. `wire()` is a no-op — `cmd_init`
  decides whether to route values into a dotenv file or just print instructions.

`LOADERS` registers them in precedence order:
`mise → direnv → devbox → none`. As with profiles, **dict insertion order is the order
`_detect_loader` probes**. A configured loader wins; when none is configured, the first
installed binary in that order is selected so a fresh repo can be wired. `none` is used
only when no loader is installed or the user explicitly requests it.

Init calls `approve()` only when `wire()` created a mise or direnv config from nothing.
It does not trust a pre-existing or inherited config, and sync/post-checkout provisioning
never runs an approval command.

## Key entry points

- `scanner.py` — `Scanner.scan()`, the one public detection entry.
- `scanner.py` — `_detect_workspace`, `_enumerate_apps`, and `_expand_workspace_globs`.
- `scanner.py` — collision mangling and per-app references (`_build_resource_catalog`).
- `scanner.py` — `_match_profile` and `_detect_loader`.
- `profile_core.py` — `Profile` and its extension points/flags, including
  `agent_guidance(app, port_names)` and shared guidance helpers.
- `profiles_web.py`, `profiles_server.py`, `profiles_mobile.py`, and
  `profiles_compose.py` — categorized framework and Compose implementations.
- `profiles.py` — compatibility exports and the single ordered `_BUILTIN_PROFILES` catalog.
- `agentdocs.py` — `render_agent_guidance()`, `sync_agent_guidance()`, and
  `remove_agent_guidance()`; invoked by init/rescan/deinit orchestration in `commands.py`.
- `catalog.py` — the dependency-free `PROFILES` registry; `profiles.py` populates it in
  precedence order.
- `scaffolds.py` — `SCAFFOLDS` registry; substituted by `_cmd_init_preset` in `commands.py`.
- `loaders.py` — `Loader`, its mise/direnv/devbox/none implementations, and the precedence-ordered
  `LOADERS` registry.
- Consumers: scanner-driven init and rescan in `commands.py`, `_build_resource_catalog`
  in `scanner.py`, and `_cmd_init_preset` for `SCAFFOLDS`.
- Registration wiring: `catalog.py` owns the dictionary and `__init__.py` imports
  `profiles` first to populate it before public consumers are re-exported. Internal modules
  import the catalog directly and never depend back on the package root.

## Gotchas

- **`PROFILES` insertion order silently controls detection precedence.** There is no
  explicit priority field — `Scanner._match_profile` (`scanner.py`) returns the *first*
  `detect()` hit. Inserting a new profile in the wrong position (e.g. a broad
  `package.json`-based detector before a narrow one) will silently shadow later profiles.
  Same hazard for `LOADERS`. The explicit `_BUILTIN_PROFILES` tuple and its exact-order test
  make this visible; insert new profiles at the intended precedence point.
- **Adding a Profile does not imply adding a preset.** A new framework needs the
  `Profile` subclass in the appropriate implementation module and an entry in
  `_BUILTIN_PROFILES` at the right precedence position.
  Framework coverage belongs in scanner-driven init. If it has consumer configs to patch, also add
  `WiringCheck`s in `wiring.py` and return them from `wiring_checks()`.
- **Capabilities do not compete with Profiles.** Electron must remain a secondary
  capability so it cannot shadow a renderer framework such as Vite or Next.js.
- **`ReactNativeProfile` and `ExpoProfile` both emit `RCT_METRO_PORT`.** The allocation
  range starts at `8082`, deliberately excluding Metro's framework-default port `8081`.
- **Resource scoping has one source of truth.** `_build_resource_catalog` derives both
  declarations and each app's resource references from the same resolved-name map.
- **Glob expansion only handles one `*`** (`scanner.py`) and lists a single
  directory level. `apps/**/foo`-style deep globs are not expanded the way a real pnpm/yarn
  matcher would; the scanner assumes the common `apps/*` / `packages/*` shapes.
- **JS workspace detection is truthiness-based**, not lockfile-authoritative: a
  `package.json` with a non-empty `workspaces` value and *no* lockfile defaults to `npm`
  (`scanner.py`).
- **Configured loader beats installed loader.** Every `Loader.detect()` probes for a
  config file (`mise.toml`/`.mise.toml`, `.envrc`, `devbox.json`) before scanner detection
  falls back to installed binaries on `PATH`. A repository's chosen loader therefore wins
  even when another loader appears earlier in PATH fallback order.
- **mise wiring must not scaffold a second config file.** `MiseLoader.detect` matches
  either `mise.toml` or `.mise.toml` (`loaders.py`), so `_ensure_mise_file_directive`
  (`hooks.py`) prefers an existing `mise.toml`, falls back to an existing
  `.mise.toml`, and only creates a new `mise.toml` when neither exists. Hardcoding
  `mise.toml` here would scaffold a duplicate beside a `.mise.toml`-only user's file
  (mise merges both, so it silently "works" while leaving two configs).

## Why

The detection side is split from the integration side on purpose. `Scanner` is pure,
side-effect-free inspection so it can be re-run cheaply (`splash init`, `splash init --rescan`,
status)
and unit-tested without touching disk state. The integration side uses small implementation
modules behind shared catalogs. `profiles.py` centralizes profile assembly because precedence
is behavior, while `LOADERS` remains a compact module-local registry. There is no priority
metadata, so insertion order is the only knob. `SCAFFOLDS` remains a policy-controlled list
of intent presets outside that parity.

The import graph itself is a build invariant. Pylint's `cyclic-import` checker analyzes the
package as a whole and fails the local and CI gates with `R0401` when a cycle is introduced.

The split between the *declarative* `PROFILES`/`LOADERS`/`SCAFFOLDS` registries and the
*imperative* `WiringCheck` lists returned from `wiring_checks()` mirrors the two phases:
detection answers "what is this," while wiring imperatively patches consumer configs and
must report/repair state — so the latter lives behind the doctor flow rather than in the
declarative tables. The dotenv-vs-loader fallback (`reads_dotenv`) exists because not every
framework reads a `.env` file; when no shell loader is adopted, splashdown needs to know
whether dropping a dotenv file would even reach the app or whether it must print manual
instructions instead.

## Related

- `docs/features/init-and-onboarding.md` — user-facing model for `splash init`, preset
  selection, and what gets written.
- `docs/tech/wiring.md` — the doctor / `WiringCheck` internals that `wiring_checks()`
  feeds (see also `docs/features/framework-wiring.md` for the user-facing wiring behavior).
- [`0003: Separate inferred frameworks from explicit intent`](../decisions/0003-separate-inferred-frameworks-from-explicit-intent.md)
  — why Profiles, intent presets, and secondary capabilities remain separate concepts.
