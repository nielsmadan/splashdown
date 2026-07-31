# Init and Onboarding (`splash init`)

> Covers **UC3** (one-command project setup) and **UC6** (a teammate clones a configured repo).
> Audience: senior devs extending splashdown. Persona: the parallel-agent developer and the
> two work-flavor personas (mobile, web/backend) in `docs/product/persona.md`.
> `README.md` is the authoritative spec.
> **Implemented by:** [scanning-and-extension](../tech/scanning-and-extension.md),
> [cli-and-commands](../tech/cli-and-commands.md).

## Contents

- [Overview](#overview)
- [How it works (current state)](#how-it-works-current-state)
- [Key entry points](#key-entry-points)
- [Configuration](#configuration)
- [Gotchas](#gotchas)
- [Why](#why)

## Overview

`splash init` is the single adoption command. In one run it scans the workspace and each
app's framework, scaffolds the committed `splashdown.toml` (plus a per-checkout
`splashdown.local.toml` skeleton), wires the detected shell-env loader (mise/direnv/devbox)
and a git `post-checkout` hook that coexists with an existing hook manager, runs framework
wiring checks (the same engine as `splash doctor --fix`), and finishes with the first
`sync` so this checkout lands with live values (allocated ports, generated ids,
`splashdown.env`). The promise: a checkout has working, collision-free resources in one
command — a bad first run means abandonment.

Three flag variants reshape that flow:

- `splash init <preset>` — write a named scaffold from `SCAFFOLDS` (legacy/explicit path),
  bypassing the scanner entirely.
- `splash init --no-sync` — scaffold and wire only; skip the first sync (no port allocation,
  no `splashdown.env`).
- `splash init --overwrite` — replace an existing recipe (init refuses otherwise).
- `splash init --rescan` — re-detect `[project]`/`[apps.*]` against the current filesystem
  in an existing recipe **without** scaffolding or touching `[resources.*]`.

## How it works (current state)

**Refusal guard.** `cmd_init` refuses to clobber an existing `splashdown.toml` unless
`--overwrite` is passed, exiting with status `2` (`commands.py:1251`–`1253`). This is a hard
`sys.exit(2)`, not a return — it short-circuits before any scan or write.

**Scan.** The default (no preset) path runs `Scanner().scan(cwd)` (`scanner.py:163`), which:
detects the workspace manager (pnpm/yarn/npm/cargo/gradle/`single`) via `_detect_workspace`
(`scanner.py:37`); enumerates apps via `_enumerate_apps` (`scanner.py:67`); matches each app
to a Profile by name through the `PROFILES` registry, defaulting to `"unknown"` when nothing
matches (`scanner.py:172`); and detects the shell loader by asking each `Loader` in priority
order mise → direnv → devbox, falling back to the first one installed on PATH and returning
`"none"` only when none is configured or installed (`_detect_loader`,
`scanner.py:155`). A `--loader` override replaces the detected loader on the inventory
(`commands.py:1263`). The result is a `ProjectInventory` of `AppInventory` entries
(`scanner.py:15`, `scanner.py:24`).

**Resource collection + collision mangling.** For each non-`unknown` app, `cmd_init` asks the
matched `Profile.resources(app)` for the resources it wants, then merges them with
`_merge_app_resources` (`scanner.py:179`): when the same canonical name is owned by more than
one app it is mangled with the upper-cased app name (e.g. two Vite apps →
`WEB_DEV_PORT_ADMIN` / `WEB_DEV_PORT_CUSTOMER`); single-owner names stay canonical.
`_app_resource_names` (`scanner.py:203`) mirrors the mangling so each `[apps.<name>]`
`resources` list points at the right keys (`commands.py:1284`).

**No-loader fallback.** When the loader is `"none"`, `_apply_no_loader_fallback`
(`commands.py:1229`) decides delivery via `_resolve_no_loader_delivery` (`commands.py:1185`):
if a dotenv file the project already reads exists (`.env` → `.env.local` precedence) **and**
at least one app actually reads dotenv files (`Profile.reads_dotenv`), it sets
`writer = "envfile=<file>"` on the generated resources; otherwise it keeps generating
`splashdown.env` and prints instructions. It warns when the chosen dotenv file is not
gitignored, and notes any apps that read only the process environment (Vite/Spring/mobile)
and therefore won't pick up a dotenv file.

**Prune, validate, write + wire.** A Profile emits resources for one app and cannot see its
siblings, so a cross-app template reference can dangle — Vite emits
`API_DEV_PORT = "{{ PORT }}"` for any config mentioning a proxy, but `PORT` exists only when
the repo also has a backend app. `_prune_unresolvable_templates` (`scanner.py`) runs after the
cross-app merge and drops such templates (looping to a fixed point, since pruning one can
strand another) and un-lists them from `[apps.*] resources`, printing one `skipped NAME:` line
each. `--rescan` passes the existing recipe's resource names in as additionally-known, so a
template the recipe already resolves is never pruned. The recipe is then rendered by
`render_scanned_recipe` (lazy-imported from `tomlio`) and parsed in memory through the same
strict `Recipe` validator used by provisioning before it is written. This catches scanner/profile
drift, invalid app resource references, resource/writer/template/schema errors, and unknown
fields before init mutates the recipe or proceeds to loader/hook wiring. A `splashdown.local.toml` skeleton (`LOCAL_SKELETON`)
is written if absent after the recipe passes validation. `_ensure_gitignore` (`commands.py:89`) adds
`splashdown.env` and `splashdown.local.toml` to `.gitignore`. The selected loader is wired by
`LOADERS[inv.loader].wire(cwd)` (`commands.py:1300`) — every loader's `wire` is idempotent
(`loaders.py`): mise sets `_.file = "splashdown.env"` under `[env]` (editing an existing
`.mise.toml`/`mise.toml` rather than scaffolding a second), direnv appends a sentinel-wrapped
`dotenv_if_exists splashdown.env` block to `.envrc`, devbox adds a marker-tagged `init_hook`,
and `none` wires nothing. `wire()` returns whether it created the config from nothing; if so,
init calls the loader's `approve()` (`mise trust` / `direnv allow`) so the freshly-wired
config actually loads — see the trust-approval note below.

**Git hook.** `_ensure_post_checkout_hook` (`commands.py:267`) installs a `post-checkout`
hook that fires `splash sync` on checkout/clone/worktree-add. `_detect_hook_manager`
(`commands.py:125`) classifies the project's existing setup as `lefthook` / `husky` /
`core-hookspath-other` / `none`, and splashdown **coexists** rather than clobbers:

- **lefthook** → idempotently add a `post-checkout` → `run: splash` entry to the lefthook
  config and run `lefthook install` best-effort (`_wire_post_checkout_lefthook`,
  `commands.py:168`).
- **husky** → drop a `.husky/post-checkout` hook (`commands.py:239`).
- **core.hooksPath set to something other than `.githooks`** → do **not** touch it; print a
  warning telling the user to add a `splash sync` hook there themselves (`commands.py:274`).
- **none** → only as a last resort own `.githooks/` and set `core.hooksPath = .githooks`
  (`_wire_post_checkout_corehookspath`, `commands.py:249`). The hook body is `POST_CHECKOUT_HOOK`
  (`commands.py:73`): it `cd`s to the repo top, no-ops if `splashdown.toml` is absent, and runs
  `splash sync` if `splash` is on PATH (otherwise prints a "not on PATH" note).

**Wiring checks.** For each known-profile app, `cmd_init` runs the profile's `wiring_checks`,
and for any check whose `detect` is not `"ok"` it applies the `autofix` if one exists, swallowing
failures with a printed `✗` line (`commands.py:1305`–`1318`). This is the same `WiringCheck`
machinery as `splash doctor` (see UC5 / `wiring.py`).

**First sync.** After `cmd_init` returns, the CLI runs the first sync via
`_cmd_provision_inner` unless `--no-sync` was passed (`cli.py:350`–`353`). That allocates ports
through the registry, expands templates, writes outputs, and prints the resolved values — the
"checkout has live values in one command" payoff.

**Legacy preset path.** `splash init <preset>` routes to `_cmd_init_legacy_preset`
(`commands.py:1321`): it looks the name up in `SCAFFOLDS` (`profiles.py:758`; unknown name →
`sys.exit(2)`), substitutes `__SPLASH_LOADER__`, validates the complete scaffold in memory, then
writes it. Only after validation does it write the local skeleton, ensure gitignore, wire the
loader and hook, and run `cmd_doctor(cwd, fix=True)` when the resolved framework has wiring checks.
Note this path is **not** sync-driven by itself — the post-init sync still comes from the CLI layer.

**`--rescan`.** `cmd_refresh_inventory` (`commands.py:1355`) is dispatched *before* `cmd_init`
(`cli.py:348`) and is a different operation: it requires an existing recipe (errors with
"run `splash init` instead" otherwise), re-scans, and rewrites only `[project]` / `[apps.*]`
via `refresh_recipe`, preserving comments and valid existing `[resources.*]` tables. It validates
both the source recipe and the rebuilt TOML, so an unknown key or invalid generated app/resource
reference fails before the existing file is replaced. Use it to pick up a newly-added monorepo app.

## Key entry points

- `cmd_init` — orchestrator; refusal guard + `sys.exit(2)`: `src/splashdown/commands.py:1244`
  (guard at `:1251`–`1253`).
- `_cmd_init_legacy_preset` — `init <preset>` path: `src/splashdown/commands.py:1321`.
- `cmd_refresh_inventory` — `--rescan`: `src/splashdown/commands.py:1355`.
- `_ensure_post_checkout_hook` / `_detect_hook_manager`:
  `src/splashdown/commands.py:267` / `:125`.
- Hook wiring per manager — lefthook/husky/core.hooksPath:
  `src/splashdown/commands.py:168` / `:239` / `:249`. Hook body `POST_CHECKOUT_HOOK`: `:73`.
- `_apply_no_loader_fallback` / `_resolve_no_loader_delivery`:
  `src/splashdown/commands.py:1229` / `:1185`.
- `_ensure_gitignore`: `src/splashdown/commands.py:89`. `_ensure_mise_file_directive`: `:101`.
- `Scanner.scan` / `ProjectInventory` / `AppInventory`:
  `src/splashdown/scanner.py:163` / `:24` / `:15`.
- `_detect_workspace` / `_enumerate_apps` / `_detect_loader`:
  `src/splashdown/scanner.py:37` / `:67` / `:145`.
- `_merge_app_resources` / `_app_resource_names` (collision mangling):
  `src/splashdown/scanner.py:179` / `:203`.
- `LOADERS` registry + idempotent `wire`: `src/splashdown/loaders.py:123` (mise `:33`,
  direnv `:61`, devbox `:91`, none `:119`).
- `SCAFFOLDS` presets / `PROFILES` registration:
  `src/splashdown/profiles.py:758` / `:364`–`:580`.
- `init` argparse parser: `src/splashdown/cli.py:144`. Dispatch (rescan before init, then
  optional sync): `src/splashdown/cli.py:347`–`353`.

## Configuration

- **`splash init`** (no args) — scan-driven scaffold + wire + sync.
- **`splash init <preset>`** — named scaffold; presets are the keys of `SCAFFOLDS`
  (`profiles.py:758`): `minimal`, `react-native`/`rn`, `flutter`, `ios-native`,
  `android-native`, `electron`, `server`/`nextjs`.
- **`--loader mise|direnv|devbox|none`** — override loader auto-detection
  (`none` = write a dotenv file / print instructions, wire nothing).
- **`--overwrite`** — replace an existing `splashdown.toml` (without it, init exits `2`).
- **`--no-sync`** — scaffold + wire only; skip port allocation and `splashdown.env`. The opt-out
  for CI / scaffold-only runs: generate the committable files without touching the machine registry.
- **`--rescan`** — re-detect `[project]`/`[apps.*]` in an existing recipe; preserves
  valid `[resources.*]` tables and comments. Does not scaffold; the rescan path is dispatched
  before `cmd_init` and returns early (`cli.py:348`). Unknown or invalid retained fields are
  errors, not extension data.
- **Files touched**: `splashdown.toml` (committed recipe), `splashdown.local.toml`
  (gitignored, skeleton), `.gitignore` (+`splashdown.env`, +`splashdown.local.toml`), the
  loader config (`mise.toml`/`.envrc`/`devbox.json`), and the hook target
  (`lefthook.yml` / `.husky/post-checkout` / `.githooks/post-checkout` + `core.hooksPath`).
  `--no-sync` additionally omits `splashdown.env`.

## Gotchas

- **UC6 — a teammate cloning a configured repo is not auto-provisioned (the H1 gap).** Git
  does **not** run hooks on `git clone`, and `core.hooksPath` / `.husky` / lefthook wiring is
  local config that a clone does not activate. The registry (`$XDG_STATE_HOME/splashdown/…`)
  and `splashdown.env` are per-machine and never committed. So even when a teammate clones a
  repo that already commits `splashdown.toml`, they get **no hook fired and no live values** —
  they must run `splash init` themselves, which **re-scans and re-scaffolds** (and refuses
  unless `--overwrite`, since the recipe already exists). There is **no lightweight
  "install the hook + sync only" verb today** (`splash up` / `splash install` is a candidate,
  not built — see `docs/product/use-cases.md` CE and the prior review's H1). Practical
  workaround for a teammate on an already-configured repo: install the hook manually (or run
  whatever the project's hook manager's `install` step is) and run `splash sync`, rather than
  `splash init`.

- **`sys.exit(2)` short-circuits, it does not return.** The refusal guard and the
  unknown-preset branch both call `sys.exit(2)` (`commands.py:1253`, `:1330`). Callers
  embedding `cmd_init` get a `SystemExit`, not a return value.

- **The first sync lives in the CLI layer, not `cmd_init`.** `cmd_init` scaffolds and wires
  but does **not** itself sync; `cli.py:350`–`353` runs `_cmd_provision_inner` afterward. So
  calling `cmd_init` directly (or with `--no-sync`) leaves the checkout scaffolded but
  **without** allocated ports or `splashdown.env`.

- **`--rescan` is a separate code path that never scaffolds.** It is dispatched before
  `cmd_init` (`cli.py:348`), requires an existing recipe, and rewrites only `[project]`/
  `[apps.*]`. Combining `--rescan` with `--overwrite`/`--no-sync` is meaningless — rescan
  returns first.

- **`core.hooksPath` already pointing elsewhere is intentionally not touched.** If a project
  sets `core.hooksPath` to something other than `.githooks`, init only prints a warning and
  installs nothing (`commands.py:274`) — the user must wire `splash sync` into that hook dir
  themselves. Silent non-provisioning is the failure mode to watch for.

- **`lefthook install` is best-effort.** If neither a local lefthook binary nor
  `npx`/`yarn` can run it, the hook entry is written but **not registered** until the user runs
  `lefthook install` (a note is printed, `commands.py:232`).

- **Loader trust is auto-approved.** mise and direnv only load a config after `mise trust` /
  `direnv allow`. splashdown runs that itself via `Loader.approve()`: on the init path for a
  config `wire()` just created, and unconditionally in `_cmd_provision_inner` on every
  `sync`/hook — so a new worktree (whose inherited config is untrusted at its new path) just
  works. A *pre-existing* `.envrc`/`mise.toml` edited during `init --no-sync` is left for the
  user to vet (it may carry their own unreviewed content); the following `sync` approves it.
  `approve()` never fails the run — a missing `mise`/`direnv` binary, non-zero exit, or timeout
  is swallowed (`loaders.py`, `_run_ok`).

- **Loader detection falls back to PATH.** `_detect_loader` (`scanner.py`) first asks each
  `Loader.detect()` whether the repo carries its config (`mise.toml`, `.envrc`, `devbox.json`),
  and a repo-level config always wins. Failing that it checks whether the binary is installed
  (`_loader_on_path` → `shutil.which`) in mise → direnv → devbox order and wires the first hit,
  because a fresh clone has no config file yet and writing `splashdown.env` with nothing to
  source it is a silent no-op. `--loader none` is the explicit opt-out. `Loader.wire()` already
  handles the create-from-nothing case, so no separate scaffolding path is needed.

- **No-loader + process-only apps = silent no-op risk.** Only reachable now when *no* loader is
  installed at all (or `--loader none` was passed) and the only apps read env from the process
  (Vite, Spring Boot, mobile) rather than a dotenv file: splashdown keeps writing
  `splashdown.env` and prints how to source it, but nothing sources it automatically
  (`_resolve_no_loader_delivery` at `commands.py:1023`, `_NO_LOADER_INSTRUCTIONS` at `:998`).

- **`profile = "unknown"` apps are skipped, not failed.** An unrecognized framework gets no
  resources and no wiring; the rest of the project still scaffolds
  (`commands.py:1279`, `:1307`).

- **Generated TOML is not trusted implicitly.** Scanner output, built-in preset output, the
  minimal-monorepo fallback, and rescan output all pass through `Recipe` before writing. A
  validation failure leaves the destination recipe absent or unchanged and prevents subsequent
  init mutations. Unknown recipe keys are hard errors even though comments and valid tables are
  preserved by rescan.

## Why

Onboarding is once-per-project but high-stakes: per the persona, a bad first run equals
abandonment, and the parallel-agent persona needs setup to be zero-touch because an agent
won't run a step it doesn't know about. Folding scan + scaffold + loader + hook + wiring +
sync into one command is what makes "spin up a worktree and it just works" true. The honest
limitation (UC6/H1) — that a *teammate's clone* is not the same as a *second worktree*, because
hooks and the registry don't travel with a clone — is the single biggest onboarding gap and is
documented above as a Gotcha rather than papered over.

**Why the first sync lives in the CLI dispatch layer, not `cmd_init`.** `cmd_init` stays pure
scaffolding so the ~30 tests that call `cmd_init(tmp_path, ...)` directly — without a `Registry` —
keep working; folding the sync into `cmd_init` would either break them or make them write to the
real machine registry. The dispatch composes `cmd_init(...)` then `_cmd_provision_inner(cwd,
registry)` (the same call bare `splash` runs), mirroring how the git post-checkout hook composes
scaffold and sync rather than fusing them.
