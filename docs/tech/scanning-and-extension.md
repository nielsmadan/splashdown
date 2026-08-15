# Scanning & Extension Layer

The detection + extension layer: how splashdown looks at a repo on disk and decides
*what* it is (workspace shape, apps, frameworks, secondary capabilities, shell loader) before any provisioning
happens. Six modules: `scanner.py` (filesystem walk → inventory), `profiles.py`
(per-framework detection, resources and wiring checks), `runners.py`
(build/install/launch behind `Profile.run`), `scaffolds.py` (the intent-preset
`splashdown.toml` templates, pure data), `agentdocs.py` (managed framework guidance),
and `loaders.py` (shell-env wiring).

## Contents

- [Purpose](#purpose)
- [How it works (current state)](#how-it-works-current-state)
  - [scanner.py — repo → ProjectInventory](#scannerpy--repo--projectinventory)
  - [profiles.py — the Profile extension point](#profilespy--the-profile-extension-point)
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
detection precedence. `SCAFFOLDS` is a separate, intentionally small registry of explicit
intent presets, not another list of supported frameworks.

## How it works (current state)

### scanner.py — repo → ProjectInventory

`Scanner.scan()` (`scanner.py:184`) is the single public entry. It runs workspace, loader,
profile, and capability detection, then assembles a `ProjectInventory` (`scanner.py:27`) of `AppInventory` entries
(`scanner.py:16`). No writes, no caching of significance — the same instance is reusable.

**1. Workspace detection** — `_detect_workspace()` (`scanner.py:44`) returns one of
`pnpm | yarn | npm | cargo | gradle | single` by probing marker files in a fixed order:

- `pnpm-workspace.yaml` → `pnpm`.
- `package.json` with a truthy `workspaces` key → `yarn` if `yarn.lock` present, else
  `npm` (also the default when a workspace-shaped `package.json` has no lockfile signal).
- `Cargo.toml` containing a `[workspace]` table → `cargo`.
- `settings.gradle` / `settings.gradle.kts` → `gradle`.
- otherwise `single`.

The order matters: a pnpm monorepo usually also has a `package.json`, so pnpm is checked
first. Note JS-workspace detection keys off the *presence* of the `workspaces` field, not
its contents.

Package metadata consumers share `package_json.py`. Missing, unreadable, malformed, and
non-object JSON all produce an empty mapping, and only object-shaped dependency tables are
merged.

**2. App enumeration** — `_enumerate_apps()` (`scanner.py:69`) turns the workspace kind
into `[(name, path), ...]`:

- `single` short-circuits to one synthetic app `("main", cwd)` (`scanner.py:72`).
- `pnpm` hand-parses the `packages:` glob list out of `pnpm-workspace.yaml` with a
  minimal line reader (no YAML dependency); the first non-list, non-comment line ends the
  block (`scanner.py:74-93`).
- `yarn`/`npm` read `workspaces` from `package.json`, tolerating both the array form and
  the `{ packages: [...] }` object form (`scanner.py:94-99`).
- `cargo` extracts `[workspace] members` via stdlib `tomllib` (`scanner.py:100-104`).
- `gradle` regex-scrapes quoted tokens out of `settings.gradle*`, mapping Gradle's `:`
  path separator to `/` (`:api:server` → `api/server`), keeping only entries that resolve
  to real directories (`scanner.py:105-119`).

Glob expansion is centralized in `_expand_workspace_globs()` (`scanner.py:123`). It only
understands a single trailing-ish `*` (it splits on the first `*` and lists the parent
dir's children), and it **excludes `node_modules` and dotdirs** while expanding
(`scanner.py:133-139`). There is no general recursive walk: the scanner trusts the
workspace manifest to point at app roots, so it never descends into `node_modules` or
`.git` — they are skipped structurally, not blacklisted.

**3. Profile matching** — for each enumerated app, `Scanner._match_profile()`
(`scanner.py:207`) iterates `PROFILES` in insertion order and returns the first
`profile.detect(app_path)` that is truthy, falling back to `"unknown"`. This is the only
place precedence is consumed; the ordering itself lives in `profiles.py` (see below).

**4. Secondary capability detection** — Electron is detected from package dependencies
without replacing the primary Profile. An Electron/Vite app remains `profile="vite"` and
also carries `capabilities=("electron",)`. Electron-only workspace members are retained;
other unmatched workspace members are still treated as shared libraries and omitted.

**5. Loader detection** — `_detect_loader()` (`scanner.py:156`) asks each `Loader` in
`LOADERS` order whether it `detect()`s, returning the first hit or `"none"`. Same
first-match-wins pattern as profiles.

**Cross-app resource-name collision mangling.** Profiles emit *canonical* resource names
(e.g. a Vite app wants `WEB_DEV_PORT`, a Next.js app wants `PORT`). When two apps of
overlapping profiles coexist, those names would collide in the single flat
`[resources.*]` table. `_build_resource_catalog()` builds the owner counts and, for any
name owned by more than one app, mangles every instance to `<NAME>_<APP>` (uppercased,
`-`→`_`): e.g. `WEB_DEV_PORT` becomes `WEB_DEV_PORT_ADMIN` /
`WEB_DEV_PORT_CUSTOMER`. Single-owner names stay canonical. The helper returns both the
flat resource table and the per-app `resources = [...]` lists, deriving each pair from
the same resolved name so declarations and references cannot diverge. If two app names
normalize to the same suffix, a stable digest disambiguates them while keeping valid
environment identifiers.

`PROFILES` itself is *declared* empty in `scanner.py:41` and *filled* by `profiles.py` at
import; `scanner.py` only ever reads it.

### profiles.py — the Profile extension point

A `Profile` (`profiles.py`) is the per-framework integration contract. The base class
defines seven extension points and flags; subclasses override the ones that apply:

- `detect(app_path)` (`profiles.py:116`) — filesystem predicate; the Scanner's match key.
- `resources(app)` (`profiles.py:119`) — `{resource_name: {type, range, ...}}` to merge
  into `[resources.*]`. Names are canonical; the Scanner mangles on collision. Built-in
  port ranges start above the framework's default port so splashdown never allocates the
  conventional default.
- `targets(app)` — default device targets emitted during scanner-driven init.
- `wiring_checks(app)` (`profiles.py:132`) — `WiringCheck`s the doctor runs to patch
  consumer configs (see `docs/tech/wiring.md`).
- `agent_guidance(app, port_names)` — framework-specific Markdown launch instructions.
  Init supplies the recipe's actual names after collision mangling. Common guidance is
  generated automatically for every app that references a port resource.
- `run(cwd, recipe, info)` — build+install+launch on a device. Only mobile/native profiles
  implement it and therefore satisfy the `RunnableProfile` protocol. Web/backend profiles
  deliberately expose no launch capability, and command preflight rejects them before
  provisioning or booting a target.
- `reads_dotenv` class flag (`profiles.py:106`) — declares whether the framework picks up
  a plain `.env`/`.env.local` on its own (Next.js, Django, FastAPI, Flask, Rails, Laravel,
  Node backends → True; Vite, Spring Boot, ASP.NET Core, mobile → False). Consumed when no
  shell loader is present, to decide
  whether a dotenv-file fallback can actually reach the app.

The concrete profiles and the **detection-precedence order** they are registered in:

```
PROFILES["astro"]           (profiles.py:472)
PROFILES["laravel"]         (profiles.py:645)
PROFILES["nuxt"]            (profiles.py:674)
PROFILES["angular"]         (profiles.py:791)
PROFILES["vite"]            (profiles.py:793)
PROFILES["node-backend"]    (profiles.py:811)
PROFILES["deno"]            (profiles.py:1039)
PROFILES["nextjs"]          (profiles.py:1065)
PROFILES["django"]          (profiles.py:1095)
PROFILES["fastapi"]         (profiles.py:1115)
PROFILES["flask"]           (profiles.py:1143)
PROFILES["springboot"]      (profiles.py:1229)
PROFILES["aspnetcore"]      (profiles.py:1402)
PROFILES["rails"]           (profiles.py:1429)
PROFILES["flutter"]         (profiles.py:1547)
PROFILES["expo"]            (profiles.py:1548)
PROFILES["react-native"]    (profiles.py:1549)
PROFILES["ios-native"]      (profiles.py:1550)
PROFILES["android-native"]  (profiles.py:1551)
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

Each `PROFILES[...] = ...(...)` assignment runs at import; **list order is registration
order is detection precedence.** The mobile block at the end is ordered deliberately
(registration block at `profiles.py:1547`): `flutter` (a `pubspec.yaml` wins even if JS tooling leaks
in) before `expo` (needs both an `expo` dep *and* `app.json`) before plain
`react-native`. The two native profiles guard against false positives by first checking
`_has_js_or_flutter()` and bailing (`profiles.py:50-83`) — an Expo app has an
`.xcodeproj`, but it must not match `ios-native`.

A couple of profiles carry real integration logic worth noting:

- **ViteProfile** (`profiles.py:499`) emits `WEB_DEV_PORT` unconditionally, and only adds
  `API_DEV_PORT` (as a `{{ PORT }}` template) when the Vite config contains a `proxy`
  block (`profiles.py:505-514`) — apps that don't proxy don't need the API's port. Its
  wiring check rewrites `env.VAR` (the `loadEnv` idiom) to `process.env.VAR` so values
  loaded by the shell loader are visible (`profiles.py:483-588`).
- **SpringBootProfile** (`profiles.py:1146`) ships a wiring check whose `autofix` is
  `None` (`profiles.py:1166`) — patching Spring config is too risky to auto-rewrite, so
  it's report-only with manual instructions.

The `run()` overrides delegate to helpers in `runners.py` (`_flutter_run`, `_rn_run`,
`_expo_run`, `_ios_native_run`, `_android_native_run`). The two
native runners are the heavy ones — `_ios_native_run` drives `xcodebuild` and reads the
built `.app`'s `Info.plist` for the bundle id, branching to `devicectl` for physical
hardware vs `simctl` for simulators; `_android_native_run` drives Gradle install tasks and
resolves `applicationId` from Gradle properties when not pinned. Recipe-supplied
positionals passed to these tools go through `_no_flag()` in `runners.py` to reject
leading-`-` values that argv would otherwise swallow as tool flags.

**SCAFFOLDS** in `scaffolds.py` contains exactly `minimal`, `server`, and `electron`.
Framework-specific and historical alias presets are deliberately absent: framework setup
comes from scanner-driven init and Profiles. Electron is the boundary case. Its explicit
preset deterministically requests a renderer port and stable profile identifier, while
plain init detects Electron as a secondary capability and asks whether to add only the
optional profile-isolation overlay.

### runners.py & scaffolds.py — split out of profiles.py

`profiles.py` had grown to ~1490 lines holding three unrelated concerns. Two were
extracted; the dependency arrow points one way, `profiles -> runners`, and `scaffolds`
depends on nothing at all.

- **`runners.py`** — everything `Profile.run` delegates to: `_rn_run`, `_expo_run`,
  `_flutter_run`, `_ios_native_run`, `_android_native_run`, the xcodebuild/gradle
  argument builders, and the `[project] run` custom-command path
  (`run_custom_command`, imported lazily by `devices.py`). It also owns the two argv
  validators (`_no_flag`, `_android_component`) since they exist to sanitize values on
  their way into a subprocess. Imports `DeviceError` from `errors.py` directly rather
  than `devices.py`, so it carries no dependency on the device layer.
- **`scaffolds.py`** — the three intent-preset `splashdown.toml` templates and the
  `SCAFFOLDS` dict. Pure strings, no imports, no logic.

The **detection helpers stayed in `profiles.py`** (`_detect_flutter`,
`_detect_expo`, `_detect_rn`, `_detect_ios_native`, `_detect_android_native`,
`_pbxproj_targets_ios`): they implement `Profile.detect`, so they belong with the
profile they serve.

**What was deliberately not done:** splitting `profiles.py` into a `profiles/` package
by category. `PROFILES` insertion order *is* detection precedence, and scattering the
registrations across modules would make that ordering implicit in import order — the
laravel-vs-vite bug is what that failure mode looks like. Keeping every
`PROFILES[...] = ...` line in one readable sequence is worth the file length.

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
- **DirenvLoader** (`loaders.py:93`) detects `.envrc`/`.envrc.local`; `wire()` appends (or
  regex-replaces, between `_DIRENV_BEGIN`/`_DIRENV_END` sentinels at `loaders.py:79-90`) a
  block containing `dotenv_if_exists splashdown.env`. It uses `dotenv_if_exists` rather
  than `dotenv` so a fresh checkout doesn't hard-error before `splashdown.env` exists
  (`loaders.py:81`). `wire()` returns whether it created `.envrc`; `approve()` runs
  `direnv allow` (mise's runs `mise trust`) so the config actually loads. Editing a
  *pre-existing* `.envrc` invalidates direnv's trust hash but is not auto-approved — `wire()`
  prints the `direnv allow` reminder instead (a freshly-created file skips the reminder because
  `approve()` handles it).
- **DevboxLoader** (`loaders.py:149`) detects `devbox.json`; `wire()` parses the JSON, finds
  or appends a `shell.init_hook` entry carrying the `# splashdown-managed` marker
  (`loaders.py:145`), and the hook does `set -a; source splashdown.env; set +a`. It
  find-and-replaces by marker rather than parsing the hook string, normalizing a
  string-valued `init_hook` into a list first (`loaders.py:155-169`).
- **NoneLoader** (`loaders.py:201`) is the fallback. `detect()` is always `False`; it's
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

- `scanner.py:184` — `Scanner.scan()`, the one public detection entry.
- `scanner.py:44` / `scanner.py:69` / `scanner.py:123` — workspace detect, app enumerate,
  glob expand.
- `scanner.py` — collision mangling and per-app references (`_build_resource_catalog`).
- `scanner.py:207` / `scanner.py:156` — first-match profile / loader resolution.
- `profiles.py` — `Profile` base class and its seven extension points/flags, including
  `agent_guidance(app, port_names)`.
- `agentdocs.py` — `render_agent_guidance()`, `sync_agent_guidance()`, and
  `remove_agent_guidance()`; invoked by init/rescan/deinit orchestration in `commands.py`.
- `profiles.py:472-1551` — `PROFILES` registrations (precedence order).
- `scaffolds.py` — `SCAFFOLDS` registry; substituted by `_cmd_init_preset` in `commands.py`.
- `loaders.py:30` — `Loader` base; subclasses `loaders.py:56/93/149/201`.
- `loaders.py:215` — `LOADERS` registry (precedence order).
- Consumers: scanner-driven init and rescan in `commands.py`, `_build_resource_catalog`
  in `scanner.py`, and `_cmd_init_preset` for `SCAFFOLDS`.
- Registration wiring: `__init__.py:67` imports `profiles` to populate `PROFILES`;
  the comment at `__init__.py:177` documents the dependency-ordered import that must run
  before anything reads `PROFILES`.

## Gotchas

- **`PROFILES` insertion order silently controls detection precedence.** There is no
  explicit priority field — `Scanner._match_profile` (`scanner.py:207`) returns the *first*
  `detect()` hit. Inserting a new profile in the wrong position (e.g. a broad
  `package.json`-based detector before a narrow one) will silently shadow later profiles.
  Same hazard for `LOADERS`. The mobile ordering at `profiles.py:1547-1551` exists precisely
  to avoid this and must be preserved.
- **Adding a Profile does not imply adding a preset.** A new framework needs the
  `Profile` subclass and a `PROFILES[...] =` line at the right precedence position.
  Framework coverage belongs in scanner-driven init. If it has consumer configs to patch, also add
  `WiringCheck`s in `wiring.py` and return them from `wiring_checks()`.
- **Capabilities do not compete with Profiles.** Electron must remain a secondary
  capability so it cannot shadow a renderer framework such as Vite or Next.js.
- **`ReactNativeProfile` and `ExpoProfile` both emit `RCT_METRO_PORT`.** The allocation
  range starts at `8082`, deliberately excluding Metro's framework-default port `8081`.
- **Resource scoping has one source of truth.** `_build_resource_catalog` derives both
  declarations and each app's resource references from the same resolved-name map.
- **Glob expansion only handles one `*`** (`scanner.py:127-143`) and lists a single
  directory level. `apps/**/foo`-style deep globs are not expanded the way a real pnpm/yarn
  matcher would; the scanner assumes the common `apps/*` / `packages/*` shapes.
- **JS workspace detection is presence-based**, not lockfile-authoritative: a
  `package.json` with a `workspaces` key and *no* lockfile defaults to `npm`
  (`scanner.py:48-55`).
- **Configured loader beats installed loader.** Every `Loader.detect()` probes for a
  config file (`mise.toml`/`.mise.toml`, `.envrc`, `devbox.json`) before scanner detection
  falls back to installed binaries on `PATH`. A repository's chosen loader therefore wins
  even when another loader appears earlier in PATH fallback order.
- **mise wiring must not scaffold a second config file.** `MiseLoader.detect` matches
  either `mise.toml` or `.mise.toml` (`loaders.py:59`), so `_ensure_mise_file_directive`
  (`hooks.py:47`) prefers an existing `mise.toml`, falls back to an existing
  `.mise.toml`, and only creates a new `mise.toml` when neither exists. Hardcoding
  `mise.toml` here would scaffold a duplicate beside a `.mise.toml`-only user's file
  (mise merges both, so it silently "works" while leaving two configs).

## Why

The detection side is split from the integration side on purpose. `Scanner` is pure,
side-effect-free inspection so it can be re-run cheaply (init, refresh-inventory, status)
and unit-tested without touching disk state. The integration side is a **registry/plugin
pattern**: `PROFILES` and `LOADERS` are plain dicts filled by import-time
side effects, which keeps "add a framework" to "write a subclass + register it" without a
central switch statement to edit. Insertion-order-as-precedence is the cost of that
simplicity — there's no priority metadata, so ordering is the only knob. `SCAFFOLDS`
remains a policy-controlled list of intent presets outside that parity.

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
