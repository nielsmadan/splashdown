# Scanning & Extension Layer

The detection + extension layer: how splashdown looks at a repo on disk and decides
*what* it is (workspace shape, apps, frameworks, secondary capabilities, shell loader) before any provisioning
happens. `inventory.py` defines scan results, `catalog.py` owns the shared ordered profile
registry, and `scanner.py` performs filesystem detection. Framework rules are split across
`profile_core.py` and the `profiles_*` implementation modules, while `profiles.py` assembles
their detection order and provides the compatibility import surface. `runners.py` and
`launching.py` own launch behavior, `agentdocs.py` renders framework guidance, and `loaders.py`
wires shell environments.

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

`splash init` needs to answer, purely by inspecting the
filesystem: is this a monorepo or a single project? What package/build manager runs it?
Which apps live inside it, and what framework is each one? Which shell-env loader (if
any) has the user already adopted? The answers drive which default resources get written
into `splashdown.toml`, which consumer-config patches the doctor will offer, and how
`splashdown.env` reaches the running app.

This layer is **pure inspection on the detection side** (the `Scanner` never writes) and a
**registry/plugin extension point** on the framework side (`PROFILES`, `LOADERS`). Both
registries are dicts populated at import time, and **insertion order is load-bearing** for
detection precedence. Profile implementations do not mutate the catalog themselves:
`profiles.py` assembles the complete built-in sequence explicitly.

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

**5. Loader selection** — `select_loader()` (`scanner.py`) asks each `Loader` for its
`config_paths(cwd)`, so a loader with several config files is one candidate. It returns a
`LoaderSelection` carrying the name, how it was decided (`override` / `detected` /
`unconfigured`) and the config filenames. An explicit `--loader` wins; otherwise the sole
configured loader is selected, several raise `UsageError` before init writes anything, and none
selects `"none"`. `Scanner.scan(cwd, loader=…)` records an already-made choice instead of
repeating it. Unlike profiles this is not first-match-wins: ambiguity is an error, not a
precedence question, and PATH is never consulted.

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
user to choose explicit monorepo resources.

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
defines eight extension points and flags; subclasses override the ones that apply:

- `detect(app_path)` — filesystem predicate; the Scanner's match key.
- `resources(app)` — `{resource_name: {type, range, ...}}` to merge
  into `[resources.*]`. Names are canonical; the Scanner mangles on collision. Built-in
  port ranges start above the framework's default port so splashdown never allocates the
  conventional default.
- `targets(app)` — default device targets emitted during scanner-driven init.
- `wiring_checks(app, env_file)` — `WiringCheck`s that `splash init` and `splash doctor` run
  to patch consumer configs (see `docs/tech/wiring.md`). `env_file` is the environment output
  destination this checkout writes, already spelled relative to `app.path`. A check that patches
  a consumer of those values must bind it — through a factory closing over it, as
  `_rn_xcode_check` and `_vite_process_env_check` do — instead of assuming the default name;
  a check that patches nothing destination-specific ignores the argument.
- `agent_guidance(app, port_names)` — framework-specific Markdown launch instructions.
  Init supplies the recipe's actual names after collision mangling. Common guidance is
  generated automatically for every app that references a port resource.
- `validate_run(cwd, recipe, kind)` — raise when the recipe cannot produce a launch on that
  destination kind (`"ios"`, `"android"`, or `None` when the target declares no platform).
  `cmd_run` calls it through `validate_device_run` before it provisions values, writes outputs,
  claims a physical target, or creates and boots a managed device, so the failure lands with the
  machine untouched. A profile that resolves a value the launch will need writes it back into
  `recipe` so the launch path does not resolve it a second time. The base implementation is a
  no-op.
- `run` — build+install+launch on a device. Only mobile/native profiles implement
  [`RunnableProfile`](../../src/splashdown/inventory.py). Accept its optional `env` keyword
  and forward that environment to every build, settings, install, and launch subprocess so
  resolved resources override stale shell values. Web/backend profiles
  deliberately expose no launch capability, and command preflight rejects them before
  provisioning or booting a target.
- `reads_dotenv` class flag — declares whether the framework picks up
  a plain `.env`/`.env.local` on its own (Next.js, Django, FastAPI, Flask, Rails, Laravel,
  Node backends → True; Vite, Spring Boot, ASP.NET Core, mobile → False). Declarative
  metadata with no current consumer: init no longer infers a destination from what the
  filesystem holds, so the user picks one with `--env-file` instead.

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
plain `react-native`. When both dependencies are present, `start`, `ios`, or `android` package
scripts invoking `react-native start`, `run-ios`, or `run-android` select React Native. Installing
Expo modules into a React Native CLI app therefore preserves its launcher and wiring checks.
The two native profiles guard against false positives by first checking
`_has_js_or_flutter()` and bailing — an Expo app has an `.xcodeproj`, but it must not match
`ios-native`.

A couple of profiles carry real integration logic worth noting:

- **ViteProfile** (`profiles_web.py`) emits `WEB_DEV_PORT` unconditionally, and only adds
  `API_DEV_PORT` (as a `{{ PORT }}` template) when the Vite config mentions `proxy` — apps that
  don't proxy don't need the API's port. The test is a raw substring over the file text, so a
  commented-out proxy still counts. Because `API_DEV_PORT` renders `{{ PORT }}`, a merged `PORT`
  resource has to exist or init prunes the unresolved template before writing the recipe. Its
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

**Electron** is the boundary case for generation. Init detects it as a secondary capability and
generates nothing for it, printing a pointer to the opt-in isolation recipe in
`docs/user/recipe.md`. Detected and undetected Electron projects therefore follow the same
hand-edited path, because the resource is inert until the main process applies it.

### Profile-adjacent modules

Launch implementations, launch orchestration, and profile categories have separate owners.

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

The common preamble names `Recipe.env_file`, the configured output destination, rather than
assuming `splashdown.env`, and restricts itself to commands the 1.0 surface actually has:
`splash env get KEY`, `splash env` (a name listing, since bare `env` hides resolved values),
and `splash sync`. It tells agents to prefer the
project's own scripts when those already consume the environment, so nothing in the block
routes an already-integrated script through a wrapper command. Allocated values are never
embedded: the block is committed content, and the numbers belong to one checkout.

`commands.py` calls `sync_agent_guidance()` only after a successful scanner init,
including the structure-only deferred-monorepo path. A recipe with no
port-bearing apps renders no block and removes any previous complete block. Deinit calls
`remove_agent_guidance()` independently of recipe parsing, so malformed or missing recipes
cannot strand managed content. Stale guidance needs no migration path: every sync rewrites
the whole span between the sentinels from the current recipe, so an older version's text
cannot survive one.

The module mutates only existing root `AGENTS.md` and independent `CLAUDE.md` files. It
does not create either file, skips symlinks and non-regular files, and owns only the text
between its HTML sentinels. Complete blocks are replaced or removed idempotently;
malformed marker pairs are warned about and left unchanged. If `CLAUDE.md` imports an
existing `AGENTS.md`, synchronization removes any older complete local block before
leaving Claude to consume the shared file.

**Externally generated files.** `_generated_marker()` recognizes an instruction file another
tool owns and refuses to edit it, because such an edit is destroyed on that tool's next sync.
The rule is deliberately narrow: only the HTML comments that *open* the file are inspected
(a BOM, blank lines, and leading YAML frontmatter are skipped, scanning stops at the first
non-comment content, and frontmatter fields themselves are never inspected), and
a comment counts only when it attributes the file to a generator (`@generated`,
`generated by/from/with X`, with an optional `auto-` or `auto ` prefix) or forbids editing
(`do not edit`, `don't edit`). Prose that merely mentions generated code, and a generator
header further down the file, do not match, so a hand-written file is never skipped for
naming the word. There is no per-tool adapter: loadout's
`<!-- Generated by loadout from ... -->` header matches the general shape, not a loadout rule.

Detection is applied after the replacement is computed, so the warning appears only when
Splashdown would otherwise have changed the file. The report names the recognized marker
(collapsed to one printable line and truncated) and names the change the generator's source
needs: add the block, replace a stale one, remove it, or repair malformed markers. Whenever
there is a block to carry over, the report prints it verbatim between two rule lines so it
can be copied into that source without reading Splashdown's own sources. Malformed markers in
a generated file report the generated attribution rather than asking the user to hand-fix
output they do not own. Deinit takes the same path, so teardown leaves a generated file
byte-identical too.

### loaders.py — idempotent shell-env wiring

A `Loader` (`loaders.py`) reports its configuration files and idempotently wires the tool to
source the configured env destination on `cd`. Every entry point takes that destination as its
`env_file` argument, defaulting to `ENV_FILE_NAME`, so a project routing values elsewhere is wired
to the file it actually uses. `config_paths(cwd)` lists the config files that exist (and
backs `detect(cwd)`); `plan(cwd, env_file)` parses and validates the edit and returns a `WirePlan`
without writing; `wire(cwd, env_file)` is `plan` plus `apply_wire_plan`. Every `wire` is **idempotent** —
re-running it produces no diff — and splashdown's own region is marked so it can be found and
removed later. `owns_config(cwd)` reports whether the loader's configuration holds splashdown's
integration and nothing else; `approve(cwd)` runs the loader's trust command; and
`approval_detail` states what that approval means for that loader, which `splash trust`
prints as a warning.

A `WirePlan` carries `status` (`created`, `updated`, `configured`, `reused`, `nothing`), the
path, the text to write (`None` for the no-write outcomes), a `note` for the report, and a
`hint` for a manual follow-up. Because the plan is built before init writes the recipe, a
malformed config or a directive splashdown cannot extend raises `LoaderConflictError`
(`errors.py`) before any file changes. `apply_wire_plan` writes through `safe_files`'
`atomic_write_text` with `root=cwd`, so every loader write carries the same symlink and
parent-chain guards as the hook writes.

Recognition of existing wiring is bounded to forms these files can be read for — simple
quoting, tabs and a leading `./` are normalized by `normalized_env_reference`
(`constants.py`) — and project code is never executed:

- **MiseLoader** detects `mise.toml`/`.mise.toml`; `plan()` uses
  `ensure_mise_file_directive_text()` (`tomlio.py`) to put a `_.file` directive into mise's
  `[env]`. An existing `_.file` naming the file, string or list, dotted or `[env._]` subtable,
  is reused unchanged. One naming another file is widened to a list so that source survives; a
  slot that is neither a string nor a list of strings raises. Widening keeps whatever trailing
  comment the slot already carried and appends the marker to it. Splashdown's own entry carries
  a `splashdown-managed` trailing comment, and `remove_mise_file_directive_text()` removes only
  a marked one, so a user-authored directive survives `unwire`. Removal is entry-scoped rather
  than slot-scoped: the marker says splashdown put an entry in the slot, not that it owns the
  slot. A slot splashdown widened from a string deliberately stays a list after `unwire`,
  because a one-entry remainder cannot be told apart from a list the user wrote.
  A directive from a build before the marker existed is unmarked, so splashdown treats it as
  the user's and `unwire` leaves it; `plan()` carries a `hint` saying so whenever it reuses an
  unmarked directive naming the configured destination.
- **DirenvLoader** (`loaders.py`) detects `.envrc`/`.envrc.local`; `plan()` appends (or
  regex-replaces, between `_DIRENV_BEGIN`/`_DIRENV_END` sentinels at `loaders.py`) a
  block containing `dotenv_if_exists <destination>`. A `dotenv`/`dotenv_if_exists` line at
  column 0 outside that block is reused unchanged; an indented one is inside a function or
  conditional and does not count. Its trailing comment is stripped by `_SHELL_COMMENT_RE`
  (`loaders.py`), the same rule devbox uses, so a `#` must start a word: `splashdown.env#foo`
  is a filename, not the file plus a comment. It uses `dotenv_if_exists` rather
  than `dotenv` so a fresh checkout doesn't hard-error before the destination exists
  (`loaders.py`). `approve()` runs
  `direnv allow` (mise's runs `mise trust`) so the config actually loads. Editing a
  *pre-existing* `.envrc` invalidates direnv's trust hash but is not auto-approved — the plan
  carries the `direnv allow` reminder as its `hint` instead. A freshly-created file skips the
  reminder because init leaves approval to `splash trust`, which prints its own line.
- **DevboxLoader** (`loaders.py`) detects `devbox.json`; `plan()` parses the JSON, finds
  or appends a `shell.init_hook` entry carrying the `# splashdown-managed` marker
  (`loaders.py`), and the hook does `set -a; source <destination>; set +a`. An unmarked hook
  whose statements include a standalone `set -a` (or `set -o allexport`) followed by a
  standalone `source`/`.` of the file is reused unchanged. A `set +a` (or `set +o allexport`)
  turns allexport back off, so only a `source` reached while it is still on counts.
  `_devbox_statements` (`loaders.py`) splits the hook on `\n` alone, because none of the other
  characters `str.splitlines` breaks on end a command in sh. It strips each line's comment
  before splitting on `;`, so a `;` inside a comment cannot produce a statement, and it drops
  indented lines the way direnv drops indented `dotenv` lines: both statements must sit at
  column 0. A chained (`&&`) statement never matches either. It
  find-and-replaces by marker rather than parsing the hook string, normalizing
  a string-valued `init_hook` into a list first (`loaders.py`), and preserves entries it does
  not own.
- **NoneLoader** (`loaders.py`) wires nothing. `detect()` is always `False`; it is only ever
  *selected*, never matched. Its plan is `nothing`, so `--loader none` configures the destination
  and touches no loader file. `cmd_init` prints how to source it instead.

Two recognition bounds are deliberate false negatives, chosen because a missed reuse only
adds a duplicate directive while a wrong one leaves the checkout silently unwired:

- `.envrc.local` and `.mise.local.toml` count toward *detection* where `config_paths` lists
  them, but are never read for reuse. Only `.envrc` and the `mise.toml`/`.mise.toml` that
  `mise_config_path` picks are parsed.
- A devbox `set -a` in one `init_hook` entry and the matching `source` in another is not
  recognized. `_devbox_hook_loads_env_file` runs per entry and does not carry the export across
  entries.

One known bound runs the other way and is a **false positive**, so it costs the expensive side
of that trade. Recognition is textual and no shell parsing is attempted, so a statement at
column 0 inside a block whose body is not indented is still read as top level, for both direnv
and devbox. A `source` of the destination that only runs under an `if` is therefore reported as
reuse: init writes nothing and the checkout stays unwired until the user indents the body or
wires the loader by hand.
`test_devbox_loader_wire_reuses_a_statement_in_an_unindented_block_body` pins it so the hole
stays visible.

`LOADERS` registers them in `mise → direnv → devbox → none` order, which fixes the order
candidates are reported in. It is not a precedence order for selection: several configured
loaders are an error the user resolves with `--loader`, and `none` is used only when nothing is
configured or the user explicitly requests it.

Init never calls `approve()`. `splash trust` does, and only when `owns_config()` is true —
the configuration carries splashdown's integration and nothing else — so a pre-existing or
inherited config is never trusted on the user's behalf. Sync and post-checkout provisioning
never run an approval command.

## Key entry points

- `scanner.py` — `Scanner.scan()`, the one public detection entry.
- `scanner.py` — `_detect_workspace`, `_enumerate_apps`, and `_expand_workspace_globs`.
- `scanner.py` — collision mangling and per-app references (`_build_resource_catalog`).
- `scanner.py` — `_match_profile`, `select_loader`, and `LoaderSelection`.
- `profile_core.py` — `Profile` and its extension points/flags, including
  `agent_guidance(app, port_names)` and shared guidance helpers.
- `profiles_web.py`, `profiles_server.py`, `profiles_mobile.py`, and
  `profiles_compose.py` — categorized framework and Compose implementations.
- `profiles.py` — compatibility exports and the single ordered `_BUILTIN_PROFILES` catalog.
- `agentdocs.py` — `render_agent_guidance()`, `sync_agent_guidance()`, and
  `remove_agent_guidance()`; invoked by init/deinit orchestration in `commands.py`.
- `catalog.py` — the dependency-free `PROFILES` registry; `profiles.py` populates it in
  precedence order.
- `loaders.py` — `Loader`, `WirePlan`, `apply_wire_plan`, its mise/direnv/devbox/none
  implementations, and the ordered `LOADERS` registry.
- Consumers: scanner-driven init in `commands.py` and `_build_resource_catalog`
  in `scanner.py`.
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
- **A new framework is a Profile, nothing else.** A new framework needs the
  `Profile` subclass in the appropriate implementation module and an entry in
  `_BUILTIN_PROFILES` at the right precedence position.
  Framework coverage belongs in scanner-driven init. If it has consumer configs to patch, also add
  `WiringCheck`s in `wiring.py` and return them from `wiring_checks()`. A check whose autofix
  writes a file should set `files` so init can report what it changed, and a check that names the
  environment output must build itself from the `env_file` argument.
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
- **Selection reads config files, never `PATH`.** `select_loader` looks only for
  `mise.toml`/`.mise.toml`, `.envrc`/`.envrc.local` and `devbox.json`. An installed binary
  with no project config selects `none`, because adopting an integration the project has not
  chosen is a project decision rather than an inference from the developer's machine.
- **mise wiring must not scaffold a second config file.** `MiseLoader.detect` matches
  either `mise.toml` or `.mise.toml` (`loaders.py`), so every read and write goes
  through the single `mise_config_path` helper (`hooks.py`), which prefers an existing
  `mise.toml`, falls back to an existing `.mise.toml`, and only names a new `mise.toml`
  when neither exists. Hardcoding
  `mise.toml` here would scaffold a duplicate beside a `.mise.toml`-only user's file
  (mise merges both, so it silently "works" while leaving two configs).

## Why

The detection side is split from the integration side on purpose. `Scanner` is pure,
side-effect-free inspection so it can be re-run cheaply (`splash init`, status)
and unit-tested without touching disk state. The integration side uses small implementation
modules behind shared catalogs. `profiles.py` centralizes profile assembly because precedence
is behavior, while `LOADERS` remains a compact module-local registry. There is no priority
metadata, so insertion order is the only knob.

The import graph itself is a build invariant. Pylint's `cyclic-import` checker analyzes the
package as a whole and fails the local and CI gates with `R0401` when a cycle is introduced.

The split between the *declarative* `PROFILES`/`LOADERS` registries and the
*imperative* `WiringCheck` lists returned from `wiring_checks()` mirrors the two phases:
detection answers "what is this," while wiring imperatively patches consumer configs and
must report/repair state — so the latter lives behind the doctor flow rather than in the
declarative tables. `reads_dotenv` survives as framework metadata, but no longer decides
delivery: inferring a destination from an existing `.env` made a user-visible routing decision
out of a filesystem accident, so `--env-file` replaced it.

## Related

- `docs/features/init-and-onboarding.md` — user-facing model for `splash init` and what gets
  written.
- `docs/tech/wiring.md` — the doctor / `WiringCheck` internals that `wiring_checks()`
  feeds (see also `docs/features/framework-wiring.md` for the user-facing wiring behavior).
- [`0003: Separate inferred frameworks from explicit intent`](../decisions/0003-separate-inferred-frameworks-from-explicit-intent.md)
  — why Profiles and secondary capabilities remain separate concepts.
