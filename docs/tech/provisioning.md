# Provisioning

How `provisioning.py` turns a parsed recipe into resolved values and writes them to disk. This is the engine behind `splash sync` and the post-checkout git hook.

For the *user-facing* contract (what gets pinned, what `splashdown.env` looks like), see [ports-and-env.md](../prd/ports-and-env.md). This doc covers the internals.

## Purpose

`provision()` is the core sync step: it walks a recipe's resources in dependency order, resolves each one to a concrete string value (allocating a port, minting a uuid, rendering a template, etc.), and persists durable values to the machine-wide registry. `write_outputs()` then groups those values by their declared `writer` and emits them to the right destination — `splashdown.env`, an arbitrary `.env` file, an `.envrc`, stdout, or nowhere. `run_setup()` runs any `[setup.*]` shell hooks. The module is deliberately read-light on the hot path: it only reads TOML via stdlib `tomllib` (through `Recipe.load`) so the git hook never pulls in `tomlkit`.

## How it works (current state)

### `provision()` — resolve loop

Entry: `provision()` at `provisioning.py:27`.

1. Locate `splashdown.toml` in `cwd`. **If it's missing, raise `FileNotFoundError`** (`provisioning.py:34`). Callers in `commands.py` catch this and turn it into a no-op exit 0, so the post-checkout hook is silent in non-splashdown repos (see Gotchas).
2. Load the recipe, resolve `cwd` to an absolute path (the registry key for this checkout), and read the current git branch via `_current_branch` (used by template scopes).
3. Iterate `topo_sort(recipe)` (`provisioning.py:41`). The topo sort orders resources so any `template` that references another resource is resolved *after* its dependency; `resolved` accumulates values as we go and is passed into each template's scope. Dependency analysis and the sort live in `recipe.py` — see [recipe-and-templates.md](./recipe-and-templates.md).
4. Dispatch on `spec["type"]` — one branch per resource type (`provisioning.py:44`–`88`):

| type | behavior | persisted to registry? |
|------|----------|------------------------|
| `port` | allocate an int in `[lo, hi]` via `registry.allocate_port` after validating the 2-element `range` | yes (ports table) |
| `uuid` | reuse the stored uuid if present, else mint `uuid4()` | yes (kv) |
| `cwd` | the checkout directory name, verbatim | yes (kv) |
| `cwd-slug` | `_slug(cwd.name)` (sanitized) | yes (kv) |
| `template` | reuse stored value if present, else `render_template(tpl, scope)` | yes (kv), baked once |
| `set` | reuse stored value, else fall back to `default`; **error if neither exists** | yes (kv) when defaulted |

   Unknown types raise `ValueError` (`provisioning.py:88`).

5. Each resolved value lands in `resolved[name]`; the dict is returned (`provisioning.py:90`).

**The `reprovision` flag** (`--reprovision`) forces re-derivation of otherwise-sticky values:
- `port`: `registry.remove_port` first, so the port is re-allocated (may change) instead of pinned (`provisioning.py:49`).
- `uuid` / `template`: skip the registry lookup (`existing = None`), so a fresh uuid is minted / the template re-renders against the current scope (`provisioning.py:53`, `:66`).
- `set` is unaffected — it always reads the stored value (or default); `--reprovision` does not reset a user-set value.

### `write_outputs()` — group and emit

Entry: `write_outputs()` at `provisioning.py:96`.

1. Group `resolved` by each resource's `writer` field, defaulting to `splashdown-env` (`provisioning.py:102`).
2. **Truncate guard**: if no resource targets `splashdown-env` anymore but the file still exists on disk, inject an empty group for it so the stale file gets emptied rather than left lying with values that contradict the recipe (`provisioning.py:108`).
3. For each `(writer, items)` group, dispatch (`provisioning.py:112`):
   - `splashdown-env` → `write_splashdown_env(cwd/splashdown.env, items)`. Splashdown owns this file wholesale and rewrites it entirely.
   - `envfile` / `envfile=<path>` → `write_envfile`. Path defaults to `.env.local`; an explicit path comes after `=`. **Containment guard** (`provisioning.py:124`): the resolved target must be `is_relative_to(cwd)`, else `ValueError`. The recipe is committed and auto-run by the post-checkout hook, so an `envfile=` value is untrusted input — without this check an absolute path or `../` escape is an arbitrary-file-write primitive for any cloned repo.
   - `envrc` → `write_envrc(cwd/.envrc.local, items)` (note: fixed filename, no `=<path>` form).
   - `stdout` → print `K=V` lines; reported as `changed=True` since it always "produces output".
   - `none` → registry-only; nothing written, `changed=False`.
   - anything else → `ValueError`.
4. Returns a list of `(message, changed)` tuples for change-aware reporting. `changed` reflects whether disk was actually touched, so `splash sync` can say "up to date" vs. listing what moved.

### Writers and change detection

`_write_if_changed()` (`provisioning.py:146`) is the common gate: it reads the existing file and writes only if the contents differ, returning whether it wrote. This is what makes re-running `sync` a no-op when nothing changed (and keeps mtimes stable for `cd`-triggered loaders).

- `write_splashdown_env` (`provisioning.py:155`): builds `K=_env_quote(V)` lines and replaces the whole file. Empty `items` → empty file.
- `write_envfile` (`provisioning.py:161`): *surgical merge* into a foreign file. Reads existing lines, drops any line whose `KEY=` is one splashdown manages (regex `^\s*([A-Za-z_]\w*)\s*=`), trims trailing blanks, then appends the managed `K=_env_quote(V)` lines (same quoting as `splashdown.env`). Non-managed lines are preserved.
- `write_envrc` (`provisioning.py:176`): same merge strategy but matches `export KEY=` and emits `export K=<single-quoted V>`. Uses shell single-quote escaping (`'\''`) rather than `_env_quote`, since `.envrc` is sourced by a shell (direnv).

### `run_setup()` — `[setup.*]` hooks

Entry: `run_setup()` at `provisioning.py:198`. Looks up `recipe.setup[preset]`, coerces its `run` (str or list) into a command list, and runs each via `subprocess.run(..., shell=True)` with `cwd` set to the checkout and env = `os.environ` overlaid with the freshly resolved values (`provisioning.py:208`). Commands are user-authored, so `shell=True` is intentional (`# noqa: S602`). A failing command is recorded as `... FAILED (cmd): exit N` and the loop continues — one bad setup step doesn't abort the rest. Returns the per-command message list.

## Key entry points

- `provision()` — resolve loop / per-type dispatch: `provisioning.py:27`
- Missing-recipe `FileNotFoundError`: `provisioning.py:34`
- Topo-sorted iteration: `provisioning.py:41`
- `set`-type missing-value error: `provisioning.py:74`
- `write_outputs()` — writer grouping + dispatch: `provisioning.py:96`
- `splashdown-env` truncate guard: `provisioning.py:108`
- envfile path-containment guard: `provisioning.py:124`
- `_write_if_changed()`: `provisioning.py:146`
- `write_splashdown_env` / `write_envfile` / `write_envrc`: `provisioning.py:155` / `:161` / `:176`
- `run_setup()`: `provisioning.py:198`

## Gotchas

- **Templates and uuids bake their value at first provision.** Once stored in the kv registry, a `template` value is returned as-is and is *not* re-rendered even if its inputs (branch, dependency values) change — it only re-derives under `--reprovision` (`provisioning.py:66`). Same for `uuid`. This is intentional stability, but surprising if you expect a template to track the current branch live.
- **`set`-type resources have no value until you give them one.** A `set` resource with neither a stored value nor a `default` raises `ValueError` telling you to run `splash env set NAME=VALUE` or add `default = ...` (`provisioning.py:74`). They're the "user must decide" escape hatch.
- **The missing-recipe error is load-bearing.** `provision()` raises `FileNotFoundError` rather than silently succeeding; the no-op-in-non-splashdown-repo behavior of the git hook depends on `commands.py` catching it and returning 0. Don't "fix" this by returning `{}` here — callers distinguish the cases.
- **`envfile=<path>` is untrusted.** The containment guard (`provisioning.py:124`) is a security boundary, not a convenience check. The recipe runs automatically on checkout, so a malicious `envfile=/etc/...` or `envfile=../../x` would otherwise write outside the project. `envrc` has no `=<path>` form, so it isn't exposed to this.
- **`envfile`/`envrc` merge by KEY, not by ownership marker.** They strip any line matching a *currently managed* key and re-append it. A managed var that you later remove from the recipe will stop being stripped and any hand-added stale line for it survives — splashdown only owns keys it's actively writing in those foreign files (unlike `splashdown.env`, which it owns wholesale).
- **`reprovision` does not reset `set` values.** It re-rolls ports/uuids/templates only; a user-set value persists across `--reprovision`.
- **An app's `resources = [...]` list is cosmetic for allocation.** `provision()` iterates the recipe's `[resources.*]` tables via `topo_sort(recipe)` / `recipe.resources` (`provisioning.py:41`–`42`) — it never reads any `[apps.<name>]` `resources` list. Setting `resources = []` on an app does **not** stop its ports being allocated: as long as a `[resources.*]` table declares the resource, it is provisioned. Keep the per-app list aligned for format consistency, but it is not load-bearing here.

## Why

The split between "resolve" (`provision`) and "emit" (`write_outputs`) keeps the registry the single source of truth for durable values while letting output destinations vary per resource. Storing derived values (uuid/template/cwd) in the registry — not just ports — is what makes them *stable across re-syncs*: a re-run reads them back instead of regenerating, so generated env files don't churn on every `cd`. The change-aware `(message, changed)` return threads back up to the CLI so sync output reflects reality rather than always claiming it wrote.

## Related

- [ports-and-env.md](../prd/ports-and-env.md) — user-facing model for ports, env vars, and `splashdown.env`.
- [registry.md](./registry.md) — the machine-wide port/kv/device coordinator that `provision()` allocates against.
- [recipe-and-templates.md](./recipe-and-templates.md) — `Recipe` parsing, `topo_sort`, the template engine, and scope functions.
