# Registry

## Purpose

`Registry` (`src/splashdown/registry.py`) is the machine-wide coordinator that pins per-checkout resources — allocated ports, key/value entries (uuids, template results, `set` values), and managed sim/AVD devices — so concurrent checkouts never collide. It is the single source of truth that port allocation, provisioning, and cleanup all read and mutate under a file lock.

## Table of contents

- [How it works (current state)](#how-it-works-current-state)
  - [On-disk layout](#on-disk-layout)
  - [Locking: the sidecar `.lock`](#locking-the-sidecar-lock)
  - [Read/write helpers](#readwrite-helpers)
  - [`_tsv_field` and row forgery](#_tsv_field-and-row-forgery)
  - [Port allocation](#port-allocation)
  - [KV and devices](#kv-and-devices)
  - [Garbage collection and reconciliation](#garbage-collection-and-reconciliation)
- [Key entry points](#key-entry-points)
- [Gotchas](#gotchas)
- [Why](#why)
- [Related](#related)

## How it works (current state)

### On-disk layout

Three flat TSV files live under `$XDG_STATE_HOME/splashdown/` (falling back to `~/.local/state/splashdown/`), resolved at *instantiation* time, not import time, so tests can `setenv("XDG_STATE_HOME", ...)` (`src/splashdown/registry.py:62-66`). The constructor creates the dir and `touch`es all three files so reads never hit a missing-file error (`registry.py:67-70`).

The column layouts (`registry.py:49-51`, declared as field counts at `registry.py:28-30`):

- `ports.tsv` — `port \t abspath \t key` (3 fields)
- `kv.tsv` — `abspath \t key \t value` (3 fields)
- `devices.tsv` — `abspath \t dtype \t variant \t udid \t model \t ios \t created_at` (7 fields, mirrors the `DeviceRow` NamedTuple at `registry.py:16-23`)

`abspath` is the checkout/worktree directory — the cross-row primary-key prefix used by every per-checkout query (`all_for`, `release`, `devices_for`, GC existence checks).

### Locking: the sidecar `.lock`

Every mutation runs inside `_lock(path)` (`registry.py:72-87`), a context manager that takes an exclusive `fcntl.flock` on a **sidecar** `<file>.lock` rather than on the TSV itself. The reason (and the load-bearing detail): the registry's write path is read-modify-**truncate-rewrite** — `write_text` replaces the whole file. Locking the TSV directly would mean the held fd points at a file that gets truncated and rewritten out from under it; locking a stable sidecar fd sidesteps that entirely. The lock is process-level advisory (`flock`), so it coordinates concurrent `splash` invocations on one machine, not across NFS.

Locks are per-file. `release`, `gc`, and `reconcile_with_recipes` acquire the ports, kv, and device locks **sequentially, never nested** (e.g. `release` at `registry.py:171-185`), so there is no lock-ordering deadlock risk between them.

### Read/write helpers

Each file has a `_read_*`/`_write_*` pair. Reads (`_read_ports` `registry.py:91-104`, `_read_kv` `registry.py:190-199`, `_read_devices` `registry.py:239-248`) split on `\t`, skip blank lines, and **silently drop any row whose field count doesn't match** the expected width — malformed rows are ignored rather than fatal. `_read_ports` additionally drops rows whose port column isn't an int. `_read_kv` uses `split("\t", 2)` so a value may itself contain tabs on read — but the write side forbids that (see below). Writes (`_write_ports` `registry.py:106-111`, `_write_kv` `registry.py:201-208`, `_write_devices` `registry.py:250-258`) join with `\t`/`\n` and append a trailing newline only when non-empty.

Public mutators are read-modify-write under the lock: `set_kv`/`remove_kv` filter out the matching `(abspath, key)` then optionally re-append (`registry.py:216-225`); `set_device`/`remove_device` do the same on `(checkout, dtype, variant)` (`registry.py:266-301`), with `set_device` stamping `created_at` via `datetime.now(UTC).isoformat(timespec="seconds")`. There is no in-memory cache: every call re-reads the file, which keeps concurrent invocations consistent at the cost of re-parsing.

### `_tsv_field` and row forgery

Because the TSV format has **no escaping**, a field containing a tab or newline would forge or corrupt rows on the next read — e.g. a value like `"a\n/other\tKEY\tval"` parses as a second, well-formed row for a *different* checkout. `_tsv_field` (`registry.py:40-43`) rejects `\t`, `\n`, and `\r` (`_TSV_FORBIDDEN`, `registry.py:37`) at **write** time, raising `ValueError`. It is applied to every field on write across all three files. These chars never legitimately appear in checkout paths, resource keys, ports, or resolved values, so rejection is a guard, not a constraint users will hit.

### Port allocation

`allocate_port(abspath, key, lo, hi)` (`registry.py:132-151`), under the ports lock:

1. **Keep an in-range pin.** If `get_port` already returns a pin for `(abspath, key)` and it's within `[lo, hi]`, return it **unchanged — even if that port is currently bound** (`registry.py:135-142`). A bound pin is almost always this checkout's own running dev server; reallocating would yank the port out from under the live process. Deliberate reallocation is *not* handled here — it happens because `splash sync --force` calls `remove_port` first, so `existing` is `None` by the time allocation runs.
2. **Build the busy set.** `busy_ports(gc=True)` (`registry.py:119-130`) reads every port row across *all* checkouts, skips (and GCs) rows whose checkout dir no longer exists, and returns the set of ports still pinned by live checkouts. This is the cross-checkout reservation layer.
3. **Scan the range.** For each candidate `lo..hi`, skip it if it's in the busy set or if `_port_in_use(candidate)` reports it live (`registry.py:144-148`).
4. **First free wins.** `_append_port` writes the new row and returns the port (`registry.py:149-150`, `153-156`). If the whole range is exhausted, raise `RuntimeError` (`registry.py:151`).

`_port_in_use` (`registry.py:411-426`) is a best-effort live probe: it attempts a real `bind()` on `127.0.0.1` and `::1` with `SO_REUSEADDR`, treating `EADDRINUSE`/`EACCES` as "in use". This catches ports held by processes that aren't in any registry (other tools, system services) — the registry's own pins (step 2) cover ports reserved but not currently listening. The two layers are complementary: pins survive a dev server being temporarily down; live probes catch non-splashdown occupants.

### KV and devices

KV is the catch-all for non-port resources: `set_kv`/`get_kv`/`remove_kv` (`registry.py:210-225`) and `all_for(abspath)` (`registry.py:227-235`), which merges this checkout's ports (stringified) **and** kv into one `{key: value}` dict — the shape `provision()` consumes when emitting `splashdown.env`.

Devices track sims/AVDs splashdown created. Lookups: `get_device`/`devices_for`/`all_devices`/`managed_udids` (`registry.py:260-310`). `managed_udids` is how device code distinguishes splashdown-owned devices from the user's own.

### Garbage collection and reconciliation

GC is **lazy** — it piggybacks on reads rather than running on a timer. `busy_ports(gc=True)` prunes dead-checkout port rows on every allocation. The explicit cleanup paths:

- `gc()` (`registry.py:392-408`) — drop port/kv rows whose `abspath` no longer exists, then fold in `gc_devices()` and `reconcile_with_recipes()`. Returns total removed.
- `gc_devices()` (`registry.py:312-322`) — drop device rows whose checkout dir is gone **or** whose sim/AVD has been deleted out from under us, via `_is_orphan_device` (lazy-imported from `devices` to break the `registry ← devices ← registry` cycle).
- `reconcile_with_recipes()` (`registry.py:352-390`) — for each live checkout, load its `splashdown.toml` and drop port/kv rows for keys the current recipe no longer declares (e.g. a leftover `DART_PORT`). Crucially, a recipe that is **missing or won't parse yields `None`, which means skip pruning that checkout** (`registry.py:365-377`) — an unloadable recipe must never be read as "declares nothing" and nuke live entries. Results are cached per path within the call.
- `release(abspath)` (`registry.py:168-186`) — remove *all* rows for one checkout across all three files; returns count removed. Used by `splash env release` (no key) — *not* by `splash destroy`, which drops a single device row via `remove_device`.

`all_checkouts` (`registry.py:324-334`) and `summary_for` (`registry.py:336-350`) are read-only inspection helpers backing `splash status`.

## Key entry points

- Construction / path resolution — `registry.py:54-70`
- `_lock` (sidecar flock) — `registry.py:72-87`
- `_tsv_field` / `_TSV_FORBIDDEN` — `registry.py:37-43`
- `allocate_port` — `registry.py:132-151`
- `busy_ports` — `registry.py:119-130`
- `_port_in_use` — `registry.py:411-426`
- `remove_port` / `_remove_port_unlocked` — `registry.py:158-166`
- `set_kv` / `get_kv` / `all_for` — `registry.py:210-235`
- `set_device` / `remove_device` / `devices_for` — `registry.py:260-307`
- `gc` / `gc_devices` / `reconcile_with_recipes` / `release` — `registry.py:168-186`, `312-322`, `352-408`

## Gotchas

- **`busy_ports(gc=True)` writes.** A read-looking call mutates `ports.tsv` when it finds dead-checkout rows (`registry.py:128-129`). It's invoked inside `allocate_port`'s lock, so that's safe — but don't call it outside a lock expecting a pure read; pass `gc=False` if you only want to observe.
- **`_remove_port_unlocked` assumes the caller holds the lock.** Public `remove_port` takes it; calling the unlocked variant directly without the ports lock races (`registry.py:162-166`).
- **In-range bound pins are never reallocated here.** If you want a *different* port, you must `remove_port` first — `allocate_port` alone will hand back the same one (`registry.py:135-142`).
- **Malformed rows vanish silently.** A wrong-width or non-int-port row is dropped on read with no warning (`registry.py:97-98`, `99-102`). A future rewrite will not preserve it.
- **No escaping means writes can raise.** Any field with a tab/newline/CR makes the mutator throw `ValueError` mid-operation; the file is rewritten only after all fields pass, so a partial write won't land — but the caller sees the exception.
- **`reconcile_with_recipes` swallows recipe load errors by design** (`registry.py:374`, bare-except `noqa`) — that's the "don't prune on unloadable recipe" guarantee, not an oversight.
- **Lazy imports inside `gc_devices`/`reconcile_with_recipes`** exist solely to break import cycles (`registry.py:316`, `360-361`); keep them function-local.

## Why

- **Flat TSV + `fcntl` over a database.** The git post-checkout hot path runs on every `cd`/checkout and must stay dependency-light and fast to start. A line-oriented TSV needs no driver, no schema migration, no daemon; `flock` gives machine-local mutual exclusion for free. The cost — full re-read/re-write per mutation — is negligible at the row counts a single machine's checkouts produce.
- **Sidecar lock file.** The write path truncates and rewrites the whole TSV, so the lock must live on a file that is never truncated — hence `<file>.lock` rather than the data file itself (`registry.py:74-77`).
- **No escaping + reject control chars.** Escaping would complicate both the parser and the hot path. Since tabs/newlines/CR never appear in legitimate values, rejecting them at write time is a simpler, stricter guarantee than escaping — and it closes the row-forgery hole where a crafted value could mint a row for another checkout (`registry.py:33-43`).
- **Keep a bound in-range pin.** Stability beats optimality: a checkout's port should be sticky across dev-server restarts and only change on explicit `--force`. Probing-and-reallocating a bound port would break a running server for no benefit (`registry.py:135-142`).
- **Pins *and* live `bind()` probes.** Pins reserve across checkouts even when nothing is listening; probes catch occupants the registry can't know about. Neither alone is sufficient.

## Related

- [Ports and env](../features/ports-and-env.md) — user-facing model for port allocation and `splashdown.env`.
- [Resource cleanup](../features/resource-cleanup.md) — user-facing behavior of `gc`, `release`, and orphan reclamation.
