# Scanning & Extension Layer

The detection + extension layer: how splashdown looks at a repo on disk and decides
*what* it is (workspace shape, apps, frameworks, shell loader) before any provisioning
happens. Three modules: `scanner.py` (filesystem walk → inventory), `profiles.py`
(per-framework integration rules + init scaffolds), `loaders.py` (shell-env wiring).

## Contents

- [Purpose](#purpose)
- [How it works (current state)](#how-it-works-current-state)
  - [scanner.py — repo → ProjectInventory](#scannerpy--repo--projectinventory)
  - [profiles.py — the Profile extension point](#profilespy--the-profile-extension-point)
  - [loaders.py — idempotent shell-env wiring](#loaderspy--idempotent-shell-env-wiring)
- [Key entry points](#key-entry-points)
- [Gotchas](#gotchas)
- [Why](#why)
- [Related](#related)

## Purpose

`splash init` and `splash refresh-inventory` need to answer, purely by inspecting the
filesystem: is this a monorepo or a single project? What package/build manager runs it?
Which apps live inside it, and what framework is each one? Which shell-env loader (if
any) has the user already adopted? The answers drive which default resources get written
into `splashdown.toml`, which consumer-config patches the doctor will offer, and how
`splashdown.env` reaches the running app.

This layer is **pure inspection on the detection side** (the `Scanner` never writes) and a
**registry/plugin extension point** on the framework side (`PROFILES`, `LOADERS`,
`SCAFFOLDS`). All three registries are dicts populated at import time, and **insertion
order is load-bearing** for detection precedence.

## How it works (current state)

### scanner.py — repo → ProjectInventory

`Scanner.scan()` (`scanner.py:163`) is the single public entry. It runs three independent
detections and assembles a `ProjectInventory` (`scanner.py:24`) of `AppInventory` entries
(`scanner.py:15`). No writes, no caching of significance — the same instance is reusable.

**1. Workspace detection** — `_detect_workspace()` (`scanner.py:37`) returns one of
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

**2. App enumeration** — `_enumerate_apps()` (`scanner.py:67`) turns the workspace kind
into `[(name, path), ...]`:

- `single` short-circuits to one synthetic app `("main", cwd)` (`scanner.py:70`).
- `pnpm` hand-parses the `packages:` glob list out of `pnpm-workspace.yaml` with a
  minimal line reader (no YAML dependency); the first non-list, non-comment line ends the
  block (`scanner.py:78-90`).
- `yarn`/`npm` read `workspaces` from `package.json`, tolerating both the array form and
  the `{ packages: [...] }` object form (`scanner.py:92-97`).
- `cargo` extracts `[workspace] members` via stdlib `tomllib` (`scanner.py:98-102`).
- `gradle` regex-scrapes quoted tokens out of `settings.gradle*`, mapping Gradle's `:`
  path separator to `/` (`:api:server` → `api/server`), keeping only entries that resolve
  to real directories (`scanner.py:103-117`).

Glob expansion is centralized in `_expand_workspace_globs()` (`scanner.py:121`). It only
understands a single trailing-ish `*` (it splits on the first `*` and lists the parent
dir's children), and it **excludes `node_modules` and dotdirs** while expanding
(`scanner.py:132-136`). There is no general recursive walk: the scanner trusts the
workspace manifest to point at app roots, so it never descends into `node_modules` or
`.git` — they are skipped structurally, not blacklisted.

**3. Profile matching** — for each enumerated app, `Scanner._match_profile()`
(`scanner.py:172`) iterates `PROFILES` in insertion order and returns the first
`profile.detect(app_path)` that is truthy, falling back to `"unknown"`. This is the only
place precedence is consumed; the ordering itself lives in `profiles.py` (see below).

**4. Loader detection** — `_detect_loader()` (`scanner.py:145`) asks each `Loader` in
`LOADERS` order whether it `detect()`s, returning the first hit or `"none"`. Same
first-match-wins pattern as profiles.

**Cross-app resource-name collision mangling.** Profiles emit *canonical* resource names
(e.g. a Vite app wants `WEB_DEV_PORT`, a Next.js app wants `PORT`). When two apps of
overlapping profiles coexist, those names would collide in the single flat
`[resources.*]` table. The scanner resolves this in two mirror helpers consumed by
`cmd_init` / `cmd_refresh_inventory`:

- `_merge_app_resources()` (`scanner.py:179`) builds an `owners` map of
  `resource_name → [app_names]`, then for any name owned by more than one app, mangles
  every instance to `<NAME>_<APP>` (uppercased, `-`→`_`): e.g. `WEB_DEV_PORT` becomes
  `WEB_DEV_PORT_ADMIN` / `WEB_DEV_PORT_CUSTOMER`. Single-owner names stay canonical.
- `_app_resource_names()` (`scanner.py:203`) applies the identical mangling logic to
  produce the per-app `resources = [...]` lists that go under each `[apps.<name>]` table.

The two helpers must agree on the mangling rule or the generated recipe would list a
resource name that the merged table doesn't contain — they intentionally duplicate the
`owners`-then-mangle logic rather than share it.

`PROFILES` itself is *declared* empty in `scanner.py:34` and *filled* by `profiles.py` at
import; `scanner.py` only ever reads it.

### profiles.py — the Profile extension point

A `Profile` (`profiles.py:246`) is the per-framework integration contract. The base class
defines five extension points; subclasses override the ones that apply:

- `detect(app_path)` (`profiles.py:262`) — filesystem predicate; the Scanner's match key.
- `resources(app)` (`profiles.py:265`) — `{resource_name: {type, range, ...}}` to merge
  into `[resources.*]`. Names are canonical; the Scanner mangles on collision.
- `wiring_checks(app)` (`profiles.py:272`) — `WiringCheck`s the doctor runs to patch
  consumer configs (see `docs/tech/wiring.md`).
- `run(cwd, recipe, info)` (`profiles.py:277`) — build+install+launch on a device. The
  base raises `DeviceError`; only mobile/native profiles override it. Web/backend
  profiles deliberately have no `splash run` (you use `pnpm dev` / `gradle bootRun`).
- `reads_dotenv` class flag (`profiles.py:260`) — declares whether the framework picks up
  a plain `.env`/`.env.local` on its own (Next.js, Django, FastAPI, Node backends → True;
  Vite, Spring Boot, mobile → False). Consumed when no shell loader is present, to decide
  whether a dotenv-file fallback can actually reach the app.

The concrete profiles and the **detection-precedence order** they are registered in:

```
PROFILES["vite"]            (profiles.py:364)
PROFILES["node-backend"]    (profiles.py:388)
PROFILES["nextjs"]          (profiles.py:415)
PROFILES["django"]          (profiles.py:435)
PROFILES["fastapi"]         (profiles.py:454)
PROFILES["springboot"]      (profiles.py:507)
PROFILES["flutter"]         (profiles.py:576)
PROFILES["expo"]            (profiles.py:577)
PROFILES["react-native"]    (profiles.py:578)
PROFILES["ios-native"]      (profiles.py:579)
PROFILES["android-native"]  (profiles.py:580)
```

Each `PROFILES[...] = ...(...)` assignment runs at import; **list order is registration
order is detection precedence.** The mobile block at the end is ordered deliberately
(comment at `profiles.py:572`): `flutter` (a `pubspec.yaml` wins even if JS tooling leaks
in) before `expo` (needs both an `expo` dep *and* `app.json`) before plain
`react-native`. The two native profiles guard against false positives by first checking
`_has_js_or_flutter()` and bailing (`profiles.py:78-89`) — an Expo app has an
`.xcodeproj`, but it must not match `ios-native`.

A couple of profiles carry real integration logic worth noting:

- **ViteProfile** (`profiles.py:300`) emits `WEB_DEV_PORT` unconditionally, and only adds
  `API_DEV_PORT` (as a `{{ PORT }}` template) when the Vite config contains a `proxy`
  block (`profiles.py:312-314`) — apps that don't proxy don't need the API's port. Its
  wiring check rewrites `env.VAR` (the `loadEnv` idiom) to `process.env.VAR` so values
  loaded by the shell loader are visible (`profiles.py:321-361`).
- **SpringBootProfile** (`profiles.py:457`) ships a wiring check whose `autofix` is
  `None` (`profiles.py:486`) — patching Spring config is too risky to auto-rewrite, so
  it's report-only with manual instructions.

The `run()` overrides delegate to module-level helpers (`_flutter_run`, `_rn_run`,
`_expo_run`, `_ios_native_run`, `_android_native_run` at `profiles.py:55-235`). The two
native runners are the heavy ones — `_ios_native_run` drives `xcodebuild` and reads the
built `.app`'s `Info.plist` for the bundle id, branching to `devicectl` for physical
hardware vs `simctl` for simulators; `_android_native_run` drives Gradle install tasks and
resolves `applicationId` from Gradle properties when not pinned. Recipe-supplied
positionals passed to these tools go through `_no_flag()` (`profiles.py:22`) to reject
leading-`-` values that argv would otherwise swallow as tool flags.

**SCAFFOLDS** (`profiles.py:758`) is a separate registry of preset `splashdown.toml`
templates for `splash init <preset>`. It is deliberately decoupled from `PROFILES`
(comment at `profiles.py:583`): some presets (`minimal`, `electron`, `server`) have no
detectable framework, and some profiles (`vite`, `springboot`) have no stock scaffold.
Each template embeds the literal token `__SPLASH_LOADER__`, which `_cmd_init_legacy_preset`
substitutes with the detected loader name at write time (`commands.py:1333`). The dict
includes aliases (`rn`→RN scaffold, `nextjs`→server scaffold) (`profiles.py:758-768`).

### loaders.py — idempotent shell-env wiring

A `Loader` (`loaders.py:14`) detects an already-adopted shell-env tool and idempotently
wires it to source `splashdown.env` on `cd`. Two methods: `detect(cwd)` and `wire(cwd)`.
Every `wire` is **idempotent** — re-running it produces no diff — and uses
sentinel-wrapped blocks so the managed region is visually obvious and machine-findable.

- **MiseLoader** (`loaders.py:27`) detects `mise.toml`/`.mise.toml`; `wire()` delegates to
  `_ensure_mise_file_directive()` in `commands.py` (lazy-imported, `loaders.py:36`), which
  adds a `_.file` directive into mise's `[env]` so mise itself loads `splashdown.env`.
- **DirenvLoader** (`loaders.py:55`) detects `.envrc`/`.envrc.local`; `wire()` appends (or
  regex-replaces, between `_DIRENV_BEGIN`/`_DIRENV_END` sentinels at `loaders.py:41-52`) a
  block containing `dotenv_if_exists splashdown.env`. It uses `dotenv_if_exists` rather
  than `dotenv` so a fresh checkout doesn't hard-error before `splashdown.env` exists
  (`loaders.py:43`). Because editing `.envrc` invalidates direnv's trust hash, it prints a
  reminder to run `direnv allow` (`loaders.py:76`).
- **DevboxLoader** (`loaders.py:85`) detects `devbox.json`; `wire()` parses the JSON, finds
  or appends a `shell.init_hook` entry carrying the `# splashdown-managed` marker
  (`loaders.py:81`), and the hook does `set -a; source splashdown.env; set +a`. It
  find-and-replaces by marker rather than parsing the hook string, normalizing a
  string-valued `init_hook` into a list first (`loaders.py:98-105`).
- **NoneLoader** (`loaders.py:109`) is the fallback. `detect()` is always `False`; it's
  only ever *selected* as the fallback, never matched. `wire()` is a no-op — `cmd_init`
  decides whether to route values into a dotenv file or just print instructions.

`LOADERS` (`loaders.py:123`) registers them in precedence order:
`mise → direnv → devbox → none`. As with profiles, **dict insertion order is the order
`_detect_loader` probes** — the first `detect()` hit wins.

## Key entry points

- `scanner.py:163` — `Scanner.scan()`, the one public detection entry.
- `scanner.py:37` / `scanner.py:67` / `scanner.py:121` — workspace detect, app enumerate,
  glob expand.
- `scanner.py:179` / `scanner.py:203` — collision mangling (`_merge_app_resources`,
  `_app_resource_names`).
- `scanner.py:172` / `scanner.py:145` — first-match profile / loader resolution.
- `profiles.py:246` — `Profile` base class (the five extension points at
  `profiles.py:260-281`).
- `profiles.py:364-580` — `PROFILES` registration block (precedence order).
- `profiles.py:758` — `SCAFFOLDS` registry; substituted in `commands.py:1333`.
- `loaders.py:14` — `Loader` base; subclasses `loaders.py:27/55/85/109`.
- `loaders.py:123` — `LOADERS` registry (precedence order).
- Consumers: `commands.py:1262` (`scan()` in init), `commands.py:1284-1285` and
  `commands.py:1373-1374` (mangling helpers), `commands.py:1326` (`SCAFFOLDS.get`).
- Registration wiring: `__init__.py:67` imports `profiles` to populate `PROFILES`;
  the comment at `__init__.py:148` documents the dependency-ordered import that must run
  before anything reads `PROFILES`.

## Gotchas

- **`PROFILES` insertion order silently controls detection precedence.** There is no
  explicit priority field — `Scanner._match_profile` (`scanner.py:172`) returns the *first*
  `detect()` hit. Inserting a new profile in the wrong position (e.g. a broad
  `package.json`-based detector before a narrow one) will silently shadow later profiles.
  Same hazard for `LOADERS`. The mobile ordering at `profiles.py:572-580` exists precisely
  to avoid this and must be preserved.
- **Adding a Profile touches several decoupled places.** A new framework needs: the
  `Profile` subclass, a `PROFILES[...] =` line *at the right precedence position*, and —
  separately — a `SCAFFOLDS` entry if you want `splash init <name>` to work (the two
  registries are not linked). If the framework has consumer configs to patch, you also add
  `WiringCheck`s in `wiring.py` and return them from `wiring_checks()`.
- **`ExpoProfile` has no `resources()` override** (`profiles.py:526`), so it inherits the
  base's empty dict — an Expo app contributes *no* resources to the recipe (unlike
  `ReactNativeProfile`, which emits `RCT_METRO_PORT` at `profiles.py:517`). If Expo should
  pin a Metro/dev port, that's a real gap, not an intentional omission documented anywhere.
- **The two mangling helpers duplicate the owners-then-mangle logic** (`scanner.py:179`
  and `scanner.py:203`). Change the mangling rule in one and the generated recipe's
  per-app `resources` list will drift out of sync with the merged `[resources.*]` table.
- **Glob expansion only handles one `*`** (`scanner.py:127-137`) and lists a single
  directory level. `apps/**/foo`-style deep globs are not expanded the way a real pnpm/yarn
  matcher would; the scanner assumes the common `apps/*` / `packages/*` shapes.
- **JS workspace detection is presence-based**, not lockfile-authoritative: a
  `package.json` with a `workspaces` key and *no* lockfile defaults to `npm`
  (`scanner.py:53`).
- **Loader detection is config-file-only — it never checks `PATH`.** Every
  `Loader.detect()` probes for a config *file* (`mise.toml`/`.mise.toml`, `.envrc`,
  `devbox.json`), not an installed binary — a repo with a `mise.toml` but no `mise`
  on the machine still detects as `mise`. This is also why the no-config default is
  `none` (`scanner.py:145`) rather than scaffolding mise: the old default returned
  `"mise"` and scaffolded `mise.toml` unconditionally, so ports were allocated and
  `splashdown.env` written but — with no mise installed — nothing ever sourced it
  (silent success, wrong result).
- **mise wiring must not scaffold a second config file.** `MiseLoader.detect` matches
  either `mise.toml` or `.mise.toml` (`loaders.py:27`), so `_ensure_mise_file_directive`
  (`commands.py:101`) prefers an existing `mise.toml`, falls back to an existing
  `.mise.toml`, and only creates a new `mise.toml` when neither exists. Hardcoding
  `mise.toml` here would scaffold a duplicate beside a `.mise.toml`-only user's file
  (mise merges both, so it silently "works" while leaving two configs).

## Why

The detection side is split from the integration side on purpose. `Scanner` is pure,
side-effect-free inspection so it can be re-run cheaply (init, refresh-inventory, status)
and unit-tested without touching disk state. The integration side is a **registry/plugin
pattern**: `PROFILES`, `LOADERS`, and `SCAFFOLDS` are plain dicts filled by import-time
side effects, which keeps "add a framework" to "write a subclass + register it" without a
central switch statement to edit. Insertion-order-as-precedence is the cost of that
simplicity — there's no priority metadata, so ordering is the only knob.

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
