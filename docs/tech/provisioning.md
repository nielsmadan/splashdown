# Provisioning

How `provisioning.py` turns a parsed recipe into resolved values and writes them to disk. This is the engine behind `splash sync` and the post-checkout git hook.

For the *user-facing* contract (what gets pinned, what `splashdown.env` looks like), see [ports-and-env.md](../features/ports-and-env.md). This doc covers the internals.

## Purpose

`provision()` is the core sync step: it walks a recipe's resources in dependency order, resolves each one to a concrete string value (allocating a port, minting a uuid, rendering a template, etc.), and persists durable values to the machine-wide registry. `write_outputs()` then groups those values by their declared `writer` and emits them to the right destination — `splashdown.env`, an arbitrary `.env` file, an `.envrc`, stdout, or nowhere. The CLI serializes those two phases per checkout so a competing sync or env mutation cannot interleave between registry and output commits. `run_setup()` and `run_bootstrap()` execute already-validated command specifications after that operation lock is released. The module is deliberately read-light on the hot path: it only reads TOML via stdlib `tomllib` (through `Recipe.load`) so the git hook never pulls in `tomlkit`.

## How it works (current state)

### `provision()` — resolve loop

Entry: `provision()` at `provisioning.py:36`.

1. Locate `splashdown.toml` in `cwd`. **If it's missing, raise `FileNotFoundError`** (`provisioning.py:42-44`). Callers in `commands.py` catch this and turn it into a no-op exit 0, so the post-checkout hook is silent in non-splashdown repos (see Gotchas).
2. Load and validate the complete recipe, then resolve `cwd` to an absolute path
   (the registry key for this checkout) and read the current git branch via
   `_current_branch` (used by template scopes). `Recipe.load` checks every
   section, resource, writer, setup, target, app reference, and template before
   this function can call a registry method. A schema error therefore causes no
   allocation and no output-file mutation.
3. Iterate `topo_sort(recipe)` (`provisioning.py:50`). The topo sort orders resources so any `template` that references another resource is resolved *after* its dependency; `resolved` accumulates values as we go and is passed into each template's scope. Dependency analysis and the sort live in `recipe.py` — see [recipe-and-templates.md](./recipe-and-templates.md).
4. Dispatch on `spec["type"]` — one branch per resource type (`provisioning.py:52-87`):

| type | behavior | persisted to registry? |
|------|----------|------------------------|
| `port` | allocate an int in the already-validated `[lo, hi]` via `registry.allocate_port` | yes (ports table) |
| `uuid` | atomically reuse the stored uuid or mint and commit one `uuid4()` | yes (kv) |
| `cwd` | the checkout directory name, verbatim | yes (kv) |
| `cwd-slug` | `_slug(cwd.name)` (sanitized) | yes (kv) |
| `template` | render `template` against the current scope on every sync | yes (kv), refreshed every sync |
| `set` | atomically reuse the stored value or commit `default`; **error if neither exists** | yes (kv) when defaulted |

   Unknown types raise `ValueError` (`provisioning.py:87`). `uuid` and `set` use
   `Registry.get_or_create_kv`, so their read/generate/write sequence is one kv-locked
   operation rather than a racy lookup followed by a separate set.

5. Each resolved value lands in `resolved[name]`; the dict is returned (`provisioning.py:88-89`).

**The internal `reprovision` flag** (CLI `--force`) forces new allocations for otherwise-sticky values:
- `port`: `registry.remove_port` first, so the port is re-allocated (may change) instead of pinned (`provisioning.py:58-60`).
- `uuid`: mint a fresh uuid and replace the kv row (`provisioning.py:61-66`).
- `template`: unaffected because templates already re-render from current inputs on every sync.
- `set` is unaffected — it always reads the stored value (or default); `reprovision=True` does not reset a user-set value.

### CLI operation boundary

`_cmd_provision_inner` (`commands.py:1589-1607`) takes
`Registry.operation_lock(abspath)` before the initial registry snapshot and holds it through
`provision()`, local-config creation, and `write_outputs()`. A second sync for the same checkout,
`splash env set`, `splash env release`, or `splash deinit` waits until that output commit finishes.
Operations for different checkouts remain independent, while the narrower per-file locks still
coordinate their shared machine-wide TSVs.

This is serialization, not rollback: each registry TSV and output file is atomically replaced on
its own, but a later writer failure can still leave an earlier registry change committed. Setup
commands deliberately run after the operation lock is released because arbitrary user-authored
commands should not block normal sync indefinitely.

### `write_outputs()` — group and emit

Entry: `write_outputs()` at `provisioning.py:92`.

1. Group `resolved` by each resource's `writer` field, defaulting to `splashdown-env` (`provisioning.py:97-99`).
2. **Truncate guard**: if no resource targets `splashdown-env` anymore but the file still exists on disk, inject an empty group for it so the stale file gets emptied rather than left lying with values that contradict the recipe (`provisioning.py:101-105`).
3. For each `(writer, items)` group, dispatch (`provisioning.py:108-138`):
   - `splashdown-env` → `write_splashdown_env(cwd/splashdown.env, items)`. Splashdown owns this file wholesale and rewrites it entirely.
   - `envfile=<path>` → `write_envfile`, creating missing parent directories. The schema already requires a
     non-empty relative path with no `..` component. A defensive **containment
     guard** (`provisioning.py:120`) also requires the resolved target to be
     `is_relative_to(cwd)`. The recipe is committed and auto-run by the
     post-checkout hook, so an `envfile=` value is untrusted input — without
     these checks an absolute path or `../` escape is an arbitrary-file-write
     primitive for any cloned repo.
   - `envrc` → `write_envrc(cwd/.envrc.local, items)` (note: fixed filename, no `=<path>` form).
   - `stdout` → print `K=V` lines; reported as `changed=True` since it always "produces output".
   - `none` → registry-only; nothing written, `changed=False`.
   - anything else → `ValueError`.
4. Returns a list of `(message, changed)` tuples for change-aware reporting. `changed` reflects whether disk was actually touched, so `splash sync` can say "up to date" vs. listing what moved.

Every filesystem writer rejects an existing symlink or non-regular destination. Regular files
are updated through a same-directory temporary file and `os.replace`, so the final operation
replaces the checkout entry rather than following it. Existing permissions are preserved for
co-owned `envfile=`/`envrc` files; the generated `splashdown.env` is always mode `0600`.

### Writers and change detection

`_write_if_changed()` (`provisioning.py:180`) is the common gate: it opens existing files with
`O_NOFOLLOW`, verifies the opened descriptor is still a regular file, and writes only when the
contents or required mode differ. Changes are committed by atomic replacement. This makes
re-running `sync` a no-op when nothing changed while preventing checkout-controlled links from
redirecting a write or permission change.

- `write_splashdown_env` (`provisioning.py:201`): builds `K=_env_quote(V)` lines and replaces the whole file. Empty `items` → empty file.
- `write_envfile` (`provisioning.py:207`): *surgical merge* into a foreign file. Reads existing lines, drops any line whose `KEY=` is one splashdown manages (regex `^\s*([A-Za-z_]\w*)\s*=`), trims trailing blanks, then appends the managed `K=_env_quote(V)` lines (same quoting as `splashdown.env`). Non-managed lines are preserved, and missing parent directories are created before the file is written.
- `write_envrc` (`provisioning.py:227`): same merge strategy but matches `export KEY=` and emits `export K=<single-quoted V>`. Uses shell single-quote escaping (`'\''`) rather than `_env_quote`, since `.envrc` is sourced by a shell (direnv).

### Recipe commands

Entries: `_run_commands()`, `run_setup()`, and `run_bootstrap()` at the end of the module. The
shape of every declared setup and bootstrap was already validated by `Recipe.load`, before
provisioning. Recipe validation normalizes the string-or-array TOML form into an immutable
`CommandSpec(commands=tuple(...))`, so execution does not reinterpret raw tables. When a setup is
requested, `run_setup` selects it and runs each command via
`subprocess.run(..., shell=True)` with `cwd` set to the checkout and env =
`os.environ` overlaid with the freshly resolved values. Commands are
user-authored, so `shell=True` is intentional. `run_bootstrap` uses the same executor and receives
the lifecycle marker as an additional environment override.

Execution still occurs after provisioning and writer output. An unknown
requested setup name or a command failure exits nonzero at that stage; those
runtime failures do not roll back registry/file changes or earlier successful
commands. A malformed declared setup, by contrast, fails during recipe load and
causes no allocation or output write. Bootstrap authorization, locking, and completion are owned by
`commands.py` and `bootstrap.py`, not this executor.

## Key entry points

- `provision()` — resolve loop / per-type dispatch: `provisioning.py:36`
- Missing-recipe `FileNotFoundError`: `provisioning.py:42-44`
- Topo-sorted iteration: `provisioning.py:50`
- `uuid` / `set` atomic create-if-absent: `provisioning.py:61-66`, `:80-85`
- `set`-type missing-value error: `provisioning.py:27-33`
- CLI operation boundary: `commands.py:1589-1607`
- `write_outputs()` — writer grouping + dispatch: `provisioning.py:92`
- `splashdown-env` truncate guard: `provisioning.py:101-105`
- envfile path-containment guard: `provisioning.py:120`
- `_read_output_file()` / `_write_if_changed()`: safe destination validation and replacement
- `write_splashdown_env` / `write_envfile` / `write_envrc`: filesystem writer implementations
- `_run_commands()` / `run_setup()` / `run_bootstrap()`: command execution after validation

## Gotchas

- **Templates are derived; uuids are stable.** A template re-renders on every sync and overwrites its kv row, so dependency or expression changes propagate immediately. A `uuid` resource remains persisted until `splash sync --force` (`reprovision=True` internally). Calling `uuid()` directly inside a template produces a fresh value on every sync; declare a separate uuid resource and reference it when the composed result must be stable.
- **`set`-type resources have no value until you give them one.** A `set` resource with neither a stored value nor a `default` raises `ValueError` telling you to run `splash env set NAME=VALUE` or add `default = ...` (`provisioning.py:27-33`, `:80-85`). They're the "user must decide" escape hatch.
- **The missing-recipe error is load-bearing.** `provision()` raises `FileNotFoundError` rather than silently succeeding; the no-op-in-non-splashdown-repo behavior of the git hook depends on `commands.py` catching it and returning 0. Don't "fix" this by returning `{}` here — callers distinguish the cases.
- **Schema errors are pre-mutation.** `Recipe.load` validates the entire
  document, including late-declared resources, setups, templates, and writers,
  before the first registry access. Do not move schema checks into the resolve
  loop or writer dispatch; doing so would reintroduce partial allocation.
- **Every output destination is untrusted.** The recipe and checkout entries are materialized
  before the post-checkout hook runs. `envfile=` therefore has both its containment guard and
  the shared destination check, while fixed `splashdown.env` and `.envrc.local` receive the
  same no-symlink/non-regular-file protection. Do not replace these checks with `exists()` plus
  `write_text()`: both operations follow symlinks.
- **`envfile`/`envrc` merge by KEY, not by ownership marker.** They strip any line matching a *currently managed* key and re-append it. A managed var that you later remove from the recipe will stop being stripped and any hand-added stale line for it survives — splashdown only owns keys it's actively writing in those foreign files (unlike `splashdown.env`, which it owns wholesale).
- **`reprovision` does not reset `set` values.** It re-rolls ports and uuids only; a user-set value persists across `splash sync --force`, while templates already track current inputs.
- **An app's `resources = [...]` list is cosmetic for allocation.** `provision()` iterates the recipe's `[resources.*]` tables via `topo_sort(recipe)` / `recipe.resources` (`provisioning.py:50-51`) — it never reads any `[apps.<name>]` `resources` list. Setting `resources = []` on an app does **not** stop its ports being allocated: as long as a `[resources.*]` table declares the resource, it is provisioned. Keep the per-app list aligned for format consistency, but it is not load-bearing here.

## Why

The split between "resolve" (`provision`) and "emit" (`write_outputs`) keeps the registry the single source of truth for current values while letting output destinations vary per resource. The CLI-owned operation lock bridges that module boundary so concurrent commands cannot publish an older output after a newer registry result. Durable generated values such as uuids remain stable in the registry; derived templates are recomputed and replace their registry row so outputs cannot disagree with their dependencies. The change-aware `(message, changed)` return threads back up to the CLI so sync output reflects reality rather than always claiming it wrote.

## Related

- [ports-and-env.md](../features/ports-and-env.md) — user-facing model for ports, env vars, and `splashdown.env`.
- [registry.md](./registry.md) — the machine-wide port/kv/device coordinator that `provision()` allocates against.
- [recipe-and-templates.md](./recipe-and-templates.md) — `Recipe` parsing, `topo_sort`, the template engine, and scope functions.
