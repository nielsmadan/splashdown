# Provisioning

How `provisioning.py` turns a parsed recipe into resolved values and writes them to disk. This is the engine behind `splash sync` and the post-checkout git hook.

For the *user-facing* contract (what gets pinned, what `splashdown.env` looks like), see [ports-and-env.md](../features/ports-and-env.md). This doc covers the internals.

## Purpose

`provision()` is the core sync step: it walks a recipe's resources in dependency order, resolves each one to a concrete string value (allocating a port, minting a uuid, rendering a template, etc.), and persists durable values to the machine-wide registry. `write_outputs()` then groups those values by their declared `writer` and emits them to the right destination — `splashdown.env`, an arbitrary `.env` file, an `.envrc`, stdout, or nowhere. `run_setup()` runs any `[setup.*]` shell hooks. The module is deliberately read-light on the hot path: it only reads TOML via stdlib `tomllib` (through `Recipe.load`) so the git hook never pulls in `tomlkit`.

## How it works (current state)

### `provision()` — resolve loop

Entry: `provision()` at `provisioning.py:27`.

1. Locate `splashdown.toml` in `cwd`. **If it's missing, raise `FileNotFoundError`** (`provisioning.py:34`). Callers in `commands.py` catch this and turn it into a no-op exit 0, so the post-checkout hook is silent in non-splashdown repos (see Gotchas).
2. Load the recipe, resolve `cwd` to an absolute path (the registry key for this checkout), and read the current git branch via `_current_branch` (used by template scopes).
3. Iterate `topo_sort(recipe)` (`provisioning.py:41`). The topo sort orders resources so any `template` that references another resource is resolved *after* its dependency; `resolved` accumulates values as we go and is passed into each template's scope. Dependency analysis and the sort live in `recipe.py` — see [recipe-and-templates.md](./recipe-and-templates.md).
4. Dispatch on `spec["type"]` — one branch per resource type (`provisioning.py:44`–`83`):

| type | behavior | persisted to registry? |
|------|----------|------------------------|
| `port` | allocate an int in `[lo, hi]` via `registry.allocate_port` after validating the 2-element `range` | yes (ports table) |
| `uuid` | reuse the stored uuid if present, else mint `uuid4()` | yes (kv) |
| `cwd` | the checkout directory name, verbatim | yes (kv) |
| `cwd-slug` | `_slug(cwd.name)` (sanitized) | yes (kv) |
| `template` | render `template` against the current scope on every sync | yes (kv), refreshed every sync |
| `set` | reuse stored value, else fall back to `default`; **error if neither exists** | yes (kv) when defaulted |

   Unknown types raise `ValueError` (`provisioning.py:83`).

5. Each resolved value lands in `resolved[name]`; the dict is returned (`provisioning.py:84`).

**The internal `reprovision` flag** (CLI `--force`) forces new allocations for otherwise-sticky values:
- `port`: `registry.remove_port` first, so the port is re-allocated (may change) instead of pinned (`provisioning.py:49`).
- `uuid`: skip the registry lookup so a fresh uuid is minted (`provisioning.py:53`).
- `template`: unaffected because templates already re-render from current inputs on every sync.
- `set` is unaffected — it always reads the stored value (or default); `reprovision=True` does not reset a user-set value.

### `write_outputs()` — group and emit

Entry: `write_outputs()` at `provisioning.py:91`.

1. Group `resolved` by each resource's `writer` field, defaulting to `splashdown-env` (`provisioning.py:97`).
2. **Truncate guard**: if no resource targets `splashdown-env` anymore but the file still exists on disk, inject an empty group for it so the stale file gets emptied rather than left lying with values that contradict the recipe (`provisioning.py:103`).
3. For each `(writer, items)` group, dispatch (`provisioning.py:107`):
   - `splashdown-env` → `write_splashdown_env(cwd/splashdown.env, items)`. Splashdown owns this file wholesale and rewrites it entirely.
   - `envfile` / `envfile=<path>` → `write_envfile`. Path defaults to `.env.local`; an explicit path comes after `=`. **Containment guard** (`provisioning.py:119`): the resolved target must be `is_relative_to(cwd)`, else `ValueError`. The recipe is committed and auto-run by the post-checkout hook, so an `envfile=` value is untrusted input — without this check an absolute path or `../` escape is an arbitrary-file-write primitive for any cloned repo.
   - `envrc` → `write_envrc(cwd/.envrc.local, items)` (note: fixed filename, no `=<path>` form).
   - `stdout` → print `K=V` lines; reported as `changed=True` since it always "produces output".
   - `none` → registry-only; nothing written, `changed=False`.
   - anything else → `ValueError`.
4. Returns a list of `(message, changed)` tuples for change-aware reporting. `changed` reflects whether disk was actually touched, so `splash sync` can say "up to date" vs. listing what moved.

### Writers and change detection

`_write_if_changed()` (`provisioning.py:141`) is the common gate: it reads the existing file and writes only if the contents differ, returning whether it wrote. This is what makes re-running `sync` a no-op when nothing changed (and keeps mtimes stable for `cd`-triggered loaders).

- `write_splashdown_env` (`provisioning.py:150`): builds `K=_env_quote(V)` lines and replaces the whole file. Empty `items` → empty file.
- `write_envfile` (`provisioning.py:163`): *surgical merge* into a foreign file. Reads existing lines, drops any line whose `KEY=` is one splashdown manages (regex `^\s*([A-Za-z_]\w*)\s*=`), trims trailing blanks, then appends the managed `K=_env_quote(V)` lines (same quoting as `splashdown.env`). Non-managed lines are preserved.
- `write_envrc` (`provisioning.py:178`): same merge strategy but matches `export KEY=` and emits `export K=<single-quoted V>`. Uses shell single-quote escaping (`'\''`) rather than `_env_quote`, since `.envrc` is sourced by a shell (direnv).

### `run_setup()` — `[setup.*]` hooks

Entry: `run_setup()` at `provisioning.py:246`. An explicitly requested setup must exist and contain a non-empty `run` string or array of non-empty strings. Each command runs via `subprocess.run(..., shell=True)` with `cwd` set to the checkout and env = `os.environ` overlaid with the freshly resolved values. Commands are user-authored, so `shell=True` is intentional. Execution stops on the first failing command and `splash sync --setup NAME` exits nonzero; an unknown setup name also exits nonzero and lists the declared names. Setup validation and execution happen after provisioning and writer output, so failure does not roll back registry/file changes or earlier successful commands. A fully successful run returns per-command messages for change-aware reporting.

## Key entry points

- `provision()` — resolve loop / per-type dispatch: `provisioning.py:27`
- Missing-recipe `FileNotFoundError`: `provisioning.py:34`
- Topo-sorted iteration: `provisioning.py:41`
- `set`-type missing-value error: `provisioning.py:74`
- `write_outputs()` — writer grouping + dispatch: `provisioning.py:91`
- `splashdown-env` truncate guard: `provisioning.py:103`
- envfile path-containment guard: `provisioning.py:119`
- `_write_if_changed()`: `provisioning.py:141`
- `write_splashdown_env` / `write_envfile` / `write_envrc`: `provisioning.py:150` / `:163` / `:178`
- `run_setup()`: `provisioning.py:246`

## Gotchas

- **Templates are derived; uuids are stable.** A template re-renders on every sync and overwrites its kv row, so dependency or expression changes propagate immediately. A `uuid` resource remains persisted until `splash sync --force` (`reprovision=True` internally). Calling `uuid()` directly inside a template produces a fresh value on every sync; declare a separate uuid resource and reference it when the composed result must be stable.
- **`set`-type resources have no value until you give them one.** A `set` resource with neither a stored value nor a `default` raises `ValueError` telling you to run `splash env set NAME=VALUE` or add `default = ...` (`provisioning.py:74`). They're the "user must decide" escape hatch.
- **The missing-recipe error is load-bearing.** `provision()` raises `FileNotFoundError` rather than silently succeeding; the no-op-in-non-splashdown-repo behavior of the git hook depends on `commands.py` catching it and returning 0. Don't "fix" this by returning `{}` here — callers distinguish the cases.
- **`envfile=<path>` is untrusted.** The containment guard (`provisioning.py:119`) is a security boundary, not a convenience check. The recipe runs automatically on checkout, so a malicious `envfile=/etc/...` or `envfile=../../x` would otherwise write outside the project. `envrc` has no `=<path>` form, so it isn't exposed to this.
- **`envfile`/`envrc` merge by KEY, not by ownership marker.** They strip any line matching a *currently managed* key and re-append it. A managed var that you later remove from the recipe will stop being stripped and any hand-added stale line for it survives — splashdown only owns keys it's actively writing in those foreign files (unlike `splashdown.env`, which it owns wholesale).
- **`reprovision` does not reset `set` values.** It re-rolls ports and uuids only; a user-set value persists across `splash sync --force`, while templates already track current inputs.
- **An app's `resources = [...]` list is cosmetic for allocation.** `provision()` iterates the recipe's `[resources.*]` tables via `topo_sort(recipe)` / `recipe.resources` (`provisioning.py:41`–`42`) — it never reads any `[apps.<name>]` `resources` list. Setting `resources = []` on an app does **not** stop its ports being allocated: as long as a `[resources.*]` table declares the resource, it is provisioned. Keep the per-app list aligned for format consistency, but it is not load-bearing here.

## Why

The split between "resolve" (`provision`) and "emit" (`write_outputs`) keeps the registry the single source of truth for current values while letting output destinations vary per resource. Durable generated values such as uuids remain stable in the registry; derived templates are recomputed and replace their registry row so outputs cannot disagree with their dependencies. The change-aware `(message, changed)` return threads back up to the CLI so sync output reflects reality rather than always claiming it wrote.

## Related

- [ports-and-env.md](../features/ports-and-env.md) — user-facing model for ports, env vars, and `splashdown.env`.
- [registry.md](./registry.md) — the machine-wide port/kv/device coordinator that `provision()` allocates against.
- [recipe-and-templates.md](./recipe-and-templates.md) — `Recipe` parsing, `topo_sort`, the template engine, and scope functions.
