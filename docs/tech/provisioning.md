# Provisioning

How `provisioning.py` turns a parsed recipe into resolved values and writes them to disk. This is the engine behind `splash sync`, `splash run`, and the post-checkout git hook.

For the *user-facing* contract (what gets pinned, what `splashdown.env` looks like), see [ports-and-env.md](../features/ports-and-env.md). This doc covers the internals.

## Purpose

`provision()` resolves and persists resources. `write_outputs()` groups those values by writer,
writes filesystem destinations, and returns typed `WriterResult` records; it never prints.
`cli_output.py` is the sole text/JSON renderer, including explicit stdout-writer values. The CLI
serializes registry resolution and file output per checkout, then runs already-validated
`[setup.*]` or `[bootstrap]` commands after releasing that operation lock. The hot path reads TOML
through stdlib `tomllib`, so it never loads `tomlkit`.

## How it works (current state)

### `provision()` — resolve loop

Entry: `provision()` at `provisioning.py`.

1. Locate `splashdown.toml` in `cwd`. If it is missing, raise `FileNotFoundError`.
   `_cmd_provision_inner` translates that expected condition to `MissingRecipeError`; `cli.main`
   renders the message and preserves the hook-compatible exit 0.
2. Load and validate the complete recipe, then resolve `cwd` to an absolute path
   (the registry key for this checkout) and read the current git branch via
   `_current_branch` (used by template scopes). `Recipe.load` checks every
   section, resource, writer, setup, target, app reference, and template before
   this function can call a registry method. A schema error therefore causes no
   allocation and no output-file mutation.
3. Iterate `topo_sort(recipe)` (`provisioning.py`). The topo sort orders resources so any `template` that references another resource is resolved *after* its dependency; `resolved` accumulates values as we go and is passed into each template's scope. Dependency analysis and the sort live in `recipe.py` — see [recipe-and-templates.md](./recipe-and-templates.md).
4. Dispatch on `spec["type"]` — one branch per resource type (`provisioning.py`):

| type | behavior | persisted to registry? |
|------|----------|------------------------|
| `port` | allocate an int in the already-validated `[lo, hi]` via `registry.allocate_port` | yes (ports table) |
| `uuid` | atomically reuse the stored uuid or mint and commit one `uuid4()` | yes (kv) |
| `cwd` | the checkout directory name, verbatim | yes (kv) |
| `cwd-slug` | `_slug(cwd.name)` (sanitized) | yes (kv) |
| `template` | render `template` against the current scope on every sync | yes (kv), refreshed every sync |
| `set` | atomically reuse the stored value or commit `default`; **error if neither exists** | yes (kv) when defaulted |

   Unknown types raise `ValueError` (`provisioning.py`). `uuid` and `set` use
   `Registry.get_or_create_kv`, so their read/generate/write sequence is one kv-locked
   operation rather than a racy lookup followed by a separate set.

5. Each resolved value lands in `resolved[name]`; the dict is returned (`provisioning.py`).

**The internal `reprovision` flag** (CLI `--force`) forces new allocations for otherwise-sticky values:
- `port`: `registry.remove_port` first, so the port is re-allocated (may change) instead of pinned (`provisioning.py`).
- `uuid`: mint a fresh uuid and replace the kv row (`provisioning.py`).
- `template`: unaffected because templates already re-render from current inputs on every sync.
- `set` is unaffected — it always reads the stored value (or default); `reprovision=True` does not reset a user-set value.

### CLI operation boundary

`_cmd_provision_inner` in `commands.py` takes
`Registry.operation_lock(abspath)` before the initial registry snapshot and holds it through
`provision()`, local-config creation, and `write_outputs()`. A second sync for the same checkout,
`splash env set`, `splash env release`, or `splash deinit` waits until that output commit finishes.
Operations for different checkouts remain independent, while the narrower per-file locks still
coordinate their shared machine-wide TSVs.

`cmd_run` uses the same resolution and writer functions under its checkout operation lock before
device claims, reconciliation, or boot. The launch environment overlays every resolved resource
on an ambient environment copy, including resources assigned to `none` or `stdout` writers.
Run refreshes file writers without rendering sync output or executing setup commands, and releases
the lock before the launcher. See [device launch](devices.md#framework-launch).

Local-config creation inside this boundary is create-only. `_create_local_skeleton()`
(`commands.py`) opens `splashdown.local.toml` with exclusive creation, no-follow where supported,
mode `0600`, and a regular-file descriptor check. An existing regular file is preserved; a symlink
or other non-regular entry is rejected instead of being opened or replaced.

This is serialization, not rollback: each registry TSV and output file is atomically replaced on
its own, but a later writer failure can still leave an earlier registry change committed. Setup
commands deliberately run after the operation lock is released because arbitrary user-authored
commands should not block normal sync indefinitely.

### `write_outputs()` — group and write

Entry: `write_outputs()` at `provisioning.py`.

1. Group `resolved` by each resource's `writer` field, defaulting to `splashdown-env`, with
   `resolve_writer()` mapping `splashdown-env` onto `envfile=<recipe.env_file>` first. One
   destination therefore has one ownership model regardless of which spelling reached it
   (`provisioning.py`).
2. **Stale guard**: if no resource targets the default destination anymore but the file still exists on disk, inject an empty group for it. The default group also passes `drop=` for every resolved key routed to another *file* destination, so a key that moved to an explicit writer stops being defined in two places. A resource whose writer is `none` or `stdout` delivers no file at all, so it is excluded from `drop`: splashdown never wrote a line for it anywhere, and removing its name would delete whatever the user put in the destination under that key (`provisioning.py`).
3. For each `(writer, items)` group, dispatch (`provisioning.py`):
   - `envfile=<path>` → `write_envfile`, creating missing parent directories. The schema already requires a
     non-empty relative path with no `..` component. A defensive **containment
     guard** (`provisioning.py`) also requires the resolved target to be
     `is_relative_to(cwd)`. The recipe is committed and auto-run by the
     post-checkout hook, so an `envfile=` value is untrusted input — without
     these checks an absolute path or `../` escape is an arbitrary-file-write
     primitive for any cloned repo. The guard resolves the *parent*, leaving a symlinked
     destination to the writer's own no-symlink refusal.
   - `envrc` → `write_envrc(cwd/.envrc.local, items)` (note: fixed filename, no `=<path>` form).
   - `stdout` → return the values on the result record; no output occurs in provisioning.
   - `none` → registry-only; nothing written, `changed=False`.
   - anything else → `ValueError`.
4. Return `WriterResult(writer, message, changed, stdout_values)` records. The CLI renderer uses
   them for change-aware reporting and emits stdout-writer values as `KEY=value` in text or one
   `stdout` object in JSON.

Every filesystem writer goes through `safe_files`, which rejects an existing symlink or
non-regular destination and, with `root=cwd`, walks the parent chain for symlinked or
non-directory components. Regular files are updated through a same-directory temporary file and
`os.replace`, so the final operation replaces the checkout entry rather than following it.

**Mode.** A destination splashdown creates is made `0600`. A destination that already exists keeps
the mode its owner chose, and no write re-chmods it. Under ruling R006 the default destination is
an ordinary co-owned file, so silently tightening a shared `.env` a group or container user reads
would break the project without saying so.

### Writers and change detection

`_rewrite()` (`provisioning.py`) is the common gate: it reads through `safe_files`, replaces the
lines of the managed keys, and writes only when the resulting text differs. Changes are committed
by atomic replacement. This makes re-running `sync` a no-op when nothing changed while preventing
checkout-controlled links from redirecting a write.

`_scan_assignments()` parses a destination into logical assignments before anything is replaced.
It recognizes `KEY=`, `export KEY=`, surrounding whitespace, and single- or double-quoted values
that span lines, and it collects *problems*: a managed key assigned in a shape it does not rewrite
(`KEY:`, `KEY+=`) or one whose quoted value never closes. `_reject_ambiguous()` raises on any
problem naming a managed key, and on a managed key assigned more than once, rather than appending
a second competing definition (INIT-10).

- `write_envfile` (`provisioning.py`): *surgical merge* into a co-owned file. Each managed key's `K=_env_quote(V)` line replaces the assignment where it already stands, and only a key the file does not assign yet is appended. Non-managed lines, their order, line endings and trailing blanks are preserved, and missing parent directories are created before the file is written. `drop=` removes a key without re-adding it. `root=` is required: it is the confinement root for the parent-chain and symlink checks in `safe_files`.
- `write_envrc` (`provisioning.py`): same merge strategy and the same required `root=`, but matches `export KEY=` and emits `export K=<single-quoted V>`. Uses shell single-quote escaping (`'\''`) rather than `_env_quote`, since `.envrc` is sourced by a shell (direnv).
- `existing_managed_keys` (`provisioning.py`): the read-only half, used by init to report collisions without disclosing values.

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
commands. Command failures raise typed `SetupError`; explicit CLI sync renders it once. A malformed
declared command, by contrast, fails during recipe load and causes no allocation or output write.
Bootstrap authorization, locking, completion, and retry output are owned by `commands.py` and
`bootstrap.py`, not this executor.

## Key entry points

- `provision()` — resolve loop / per-type dispatch: `provisioning.py`
- Missing-recipe `FileNotFoundError`: `provisioning.py`
- Topo-sorted iteration: `provisioning.py`
- `uuid` / `set` atomic create-if-absent: `provisioning.py`
- `set`-type missing-value error: `provisioning.py`
- CLI operation boundary: `commands.py` (`_cmd_provision_inner`)
- `write_outputs()` / `WriterResult`: `provisioning.py`
- sync renderer and redaction policy: `cli_output.py` (`render_sync`)
- `_scan_assignments()` / `_reject_ambiguous()` / `_rewrite()`: destination parsing and replacement
- `write_envfile` / `write_envrc` / `existing_managed_keys` / `resolve_writer`: writer implementations
- `_run_commands()` / `run_setup()` / `run_bootstrap()`: command execution after validation

## Gotchas

- **Templates are derived; uuids are stable.** A template re-renders on every sync and overwrites its kv row, so dependency or expression changes propagate immediately. A `uuid` resource remains persisted until `splash sync --force` (`reprovision=True` internally). Calling `uuid()` directly inside a template produces a fresh value on every sync; declare a separate uuid resource and reference it when the composed result must be stable.
- **`set`-type resources have no value until you give them one.** A `set` resource with neither a stored value nor a `default` raises `ValueError` telling you to run `splash env set NAME=VALUE` or add `default = ...` (`provisioning.py`). They're the "user must decide" escape hatch.
- **The missing-recipe error is load-bearing.** `provision()` raises `FileNotFoundError` rather
  than silently succeeding; the command layer translates it to the typed exit-0 notice rendered
  by `cli.main`. Do not return `{}` here — callers distinguish the cases.
- **Schema errors are pre-mutation.** `Recipe.load` validates the entire
  document, including late-declared resources, setups, templates, and writers,
  before the first registry access. Do not move schema checks into the resolve
  loop or writer dispatch; doing so would reintroduce partial allocation.
- **Every output destination is untrusted.** The recipe and checkout entries are materialized
  before the post-checkout hook runs. `envfile=` therefore has both its containment guard and the
  shared `safe_files` destination check, and `.envrc.local` receives the same no-symlink,
  non-regular-file, parent-chain protection. Do not replace these checks with `exists()` plus
  `write_text()`: both operations follow symlinks.
- **Writers merge by KEY, not by ownership marker, and that now includes the default destination.** A managed key is replaced where it already stands, and only a key the destination does not assign yet is appended. Ruling R006 makes this uniform: a filename alone does not grant wholesale ownership, so `splashdown.env` is co-owned exactly like a custom path.
- **The registry says what to remove, because the recipe cannot.** A resource deleted from the recipe is absent from `resolved`, so nothing derived from the recipe would ever strip its line. `write_outputs` and `clear_writer_destinations` therefore take `known_keys` — `registry.all_for(checkout)` — and the default destination drops every key splashdown wrote there but no longer declares, minus the keys whose declared writer is `none` or `stdout`. `known_keys` only ever reaches the *default* group: a resource routed to `envfile=apps/api/.env` and then deleted from the recipe keeps its line in that foreign file forever, because nothing records which file a vanished resource targeted. Two paths prune a registry row before the next sync and leave a stale line behind: `splash gc` (`reconcile_with_recipes`), and `splash env release [KEY]`, which calls `remove_kv`/`remove_port` or `registry.release` without touching any destination and can be aimed at another live checkout with `--checkout`. `cmd_deinit` reads the keys *before* `registry.release`, which deletes the rows. `clear_writer_destinations` returns a `TeardownResult` whose `uncleaned` set is the registry keys no destination it knows about carried, and `cmd_deinit` names them, so a value stranded in a foreign file is reported rather than left behind in silence.
- **Destination bytes outside splashdown's keys are preserved as written.** `_rewrite` splits through `constants.split_lines`, the one splitter every editor in the package shares, rather than `str.splitlines`, which also breaks on vertical tab, form feed, the information separators, NEL and the Unicode separators and would rewrite a value containing one; `_destination_lines` drops only the trailing empty element a final newline produces, so trailing blank lines survive. A destination left with no non-blank content is unlinked, so the writer and `_strip_destination` agree instead of one leaving a zero-byte file.
- **Teardown skips a destination it cannot parse.** `_strip_destination` refuses to edit a file whose scan reports an unterminated quote, returning `("unparsed", frozenset())` so `cmd_deinit` says it left the file alone. Below an unclosed quote the scanner resumes matching *inside* the quoted region, so a best-effort removal there could delete part of a user's multi-line value. Teardown still must not raise, so this is a skip and a message, not an error.
- **An unterminated quoted value is fatal whoever opened it.** Below such a line splashdown cannot tell an assignment from prose, so `_reject_ambiguous` raises even when the key is not managed; extending the value to EOF would hide every managed assignment underneath and append a second competing definition on every sync.
- **`reprovision` does not reset `set` values.** It re-rolls ports and uuids only; a user-set value persists across `splash sync --force`, while templates already track current inputs.
- **An app's `resources = [...]` list is cosmetic for allocation.** `provision()` iterates the recipe's `[resources.*]` tables via `topo_sort(recipe)` / `recipe.resources` (`provisioning.py`) — it never reads any `[apps.<name>]` `resources` list. Setting `resources = []` on an app does **not** stop its ports being allocated: as long as a `[resources.*]` table declares the resource, it is provisioned. Keep the per-app list aligned for format consistency, but it is not load-bearing here.

## Why

The split between resolution, destination writes, and CLI rendering keeps the registry authoritative,
prevents concurrent publication races, and gives output one policy boundary. The typed result keeps
secret-bearing stdout values out of provisioning side effects and lets JSON remain one valid
document. Operational JSON exposes `resolved_keys` by default; `--show-values` deliberately
switches it to `resolved`.

## Related

- [ports-and-env.md](../features/ports-and-env.md) — user-facing model for ports, env vars, and `splashdown.env`.
- [registry.md](./registry.md) — the machine-wide port/kv/device coordinator that `provision()` allocates against.
- [recipe-and-templates.md](./recipe-and-templates.md) — `Recipe` parsing, `topo_sort`, the template engine, and scope functions.
- [`0001: Separate shared, local, and generated state`](../decisions/0001-separate-shared-local-and-generated-state.md)
  — why recipe intent, local overrides, and generated output have separate owners.
