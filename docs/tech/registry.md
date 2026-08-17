# Registry

## Purpose

`Registry` (`src/splashdown/registry.py`) is the machine-wide coordinator that pins per-checkout resources — allocated ports, key/value entries (uuids, template results, `set` values), and managed sim/AVD devices — so concurrent checkouts never collide. Mutations take a per-file lock and atomically replace the complete TSV; inspection reads take an unlocked old-or-new snapshot. A separate checkout-scoped operation lock serializes sync, env mutation, deinit, target edits, and device lifecycle operations through their external side effects.

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

Three flat TSV files live under `$XDG_STATE_HOME/splashdown/` (falling back to `~/.local/state/splashdown/`), resolved at *instantiation* time, not import time, so tests can `setenv("XDG_STATE_HOME", ...)` (`src/splashdown/registry.py:66-78`). The constructor creates the dir, `touch`es all three files so reads never hit a missing-file error, and makes them owner-readable/writable (`registry.py:79-85`).

The column layouts (`registry.py:61-63`, declared as field counts at `registry.py:26-30`):

- `ports.tsv` — `port \t abspath \t key` (3 fields)
- `kv.tsv` — `abspath \t key \t value` (3 fields)
- `devices.tsv` — `abspath \t dtype \t variant \t identifier \t model \t runtime \t created_at` (7 fields). Explicit codecs preserve the historical bytes while decoding simulator rows to `SimulatorRecord` and emulator rows to `EmulatorRecord`; Android's three payload slots mean AVD name, device profile, and system image.

`abspath` is the checkout/worktree directory — the cross-row primary-key prefix used by every per-checkout query (`all_for`, `release`, `devices_for`, GC existence checks).

### Locking: the sidecar `.lock`

Every per-file mutation runs inside `_lock(path)` (`registry.py:88-100`), a context manager that takes an exclusive `fcntl.flock` on a **sidecar** `<file>.lock` rather than on the TSV itself. Atomic replacement changes the TSV inode, so locking the data file would strand the lock on the old inode; the stable sidecar continues to coordinate later openers. The lock is process-level advisory (`flock`), so it coordinates concurrent `splash` invocations on one machine, not across NFS.

`operation_lock(abspath)` (`registry.py:103-108`) hashes the canonical checkout path into one of 256 stable sidecars beside `kv.tsv`. The bounded shard set avoids leaking a permanent inode for every deleted worktree; two unrelated checkouts can occasionally share a shard, which only adds temporary serialization. `_cmd_provision_inner` holds the lock from the pre-provision snapshot through all output writers; env mutation, deinit, local target edits, and device lifecycle commands use the same boundary. `run` releases it before launching the long-lived app process. Fleet refresh/GC locks and rereads one checkout row at a time. Per-file registry locks are acquired only inside the operation lock, never the reverse. Setup commands run after it is released so an arbitrary user command cannot block later syncs indefinitely.

Locks are per-file. `release`, `gc`, and `reconcile_with_recipes` acquire the ports, kv, and device locks **sequentially, never nested** (e.g. `release` at `registry.py:199-217`), so there is no lock-ordering deadlock risk between them.

### Read/write helpers

Each file has a `_read_*`/`_write_*` pair. Reads (`_read_ports` `registry.py:109-122`, `_read_kv` `registry.py:219-228`, `_read_devices` `registry.py:277-286`) split on `\t`, skip blank lines, and **silently drop any row whose field count doesn't match** the expected width — malformed rows are ignored rather than fatal. `_read_ports` additionally drops rows whose port column isn't an int. `_read_kv` uses `split("\t", 2)` so a value may itself contain tabs on read — but the write side forbids that (see below).

Writes serialize and validate every row before `_atomic_write` (`registry.py:46-55`) creates a mode-`0600` same-directory temporary file and calls `os.replace`. An unlocked reader therefore sees either the complete prior inode or the complete replacement, never an in-place truncation window. `_write_ports`, `_write_kv`, and `_write_devices` all use this one path (`registry.py:124`, `:230`, `:288`).

Public mutators are read-modify-write under the lock: `set_kv`/`remove_kv` filter out the matching `(abspath, key)` then optionally re-append (`registry.py:245-264`); `record_simulator` / `record_emulator` construct typed records, and `set_managed_device` / `remove_device` replace by `(checkout, dtype, variant)`. The legacy `set_device` signature remains as a compatibility adapter. `get_or_create_kv` (`registry.py:251-260`) performs lookup, factory invocation, and append under one kv lock, so concurrent UUID provisioning returns the one committed value. There is no in-memory cache: every call re-reads the file, which keeps concurrent invocations consistent at the cost of re-parsing.

### `_tsv_field` and row forgery

Because the TSV format has **no escaping**, a field containing a tab or newline would forge or corrupt rows on the next read — e.g. a value like `"a\n/other\tKEY\tval"` parses as a second, well-formed row for a *different* checkout. `_tsv_field` (`registry.py:40-43`) rejects `\t`, `\n`, and `\r` (`_TSV_FORBIDDEN`, `registry.py:37`) at **write** time, raising `ValueError`. It is applied to every field on write across all three files. These chars never legitimately appear in checkout paths, resource keys, ports, or resolved values, so rejection is a guard, not a constraint users will hit.

### Port allocation

`allocate_port(abspath, key, lo, hi)` (`registry.py:156-182`), under the ports lock:

1. **Keep an in-range pin.** If `get_port` already returns a pin for `(abspath, key)` and it's within `[lo, hi]`, return it **unchanged — even if that port is currently bound** (`registry.py:158-166`). A bound pin is almost always this checkout's own running dev server; reallocating would yank the port out from under the live process. Deliberate reallocation is *not* handled here — it happens because `splash sync --force` calls `remove_port` first, so `existing` is `None` by the time allocation runs.
2. **Build the busy set.** `_busy_ports_unlocked(gc=True)` (`registry.py:143-154`) reads every port row across *all* checkouts, skips (and GCs) rows whose checkout dir no longer exists, and returns the set of ports still pinned by live checkouts. This is the cross-checkout reservation layer. Public callers use `busy_ports`, which takes the lock itself.
3. **Scan the range.** For each candidate `lo..hi`, skip it if it's in the busy set or if `_port_in_use(candidate)` reports it live (`registry.py:174-180`).
4. **First free wins.** `_append_port` writes the new row and returns the port (`registry.py:180-187`). If the whole range is exhausted, raise `RuntimeError` (`registry.py:182`).

`_port_in_use` (`registry.py:455-470`) is a best-effort live probe: it attempts a real `bind()` on `127.0.0.1` and `::1` with `SO_REUSEADDR`, treating `EADDRINUSE`/`EACCES` as "in use". This catches ports held by processes that aren't in any registry (other tools, system services) — the registry's own pins (step 2) cover ports reserved but not currently listening. The two layers are complementary: pins survive a dev server being temporarily down; live probes catch non-splashdown occupants.

### KV and devices

KV is the catch-all for non-port resources: `set_kv`/`get_kv`/`get_or_create_kv`/`remove_kv` (`registry.py:239-264`) and `all_for(abspath)` (`registry.py:267-275`), which merges this checkout's ports (stringified) **and** kv into one `{key: value}` dict — the shape `provision()` consumes when emitting `splashdown.env`.

Devices track sims/AVDs splashdown created. Lookups: `get_device`/`devices_for`/`all_devices`/`managed_udids` (`registry.py:298-348`). `managed_udids` is how device code distinguishes splashdown-owned devices from the user's own.

### Garbage collection and reconciliation

GC is **lazy** — it piggybacks on reads rather than running on a timer. `busy_ports(gc=True)` prunes dead-checkout port rows on every allocation. The explicit cleanup paths:

- `gc()` — drop port/kv rows whose `abspath` no longer exists, optionally fold in `gc_devices()`, then run `reconcile_with_recipes()`. Returns total removed.
- `gc_devices(orphan_check=None)` — always drops device rows whose checkout dir is gone. A higher orchestration layer may supply an external-resource predicate, but Registry never imports the device lifecycle layer. The CLI performs live sim/AVD reconciliation through `cmd_target_gc` before registry cleanup.
- `reconcile_with_recipes()` (`registry.py:390-428`) — for each live checkout, load its `splashdown.toml` and drop port/kv rows for keys the current recipe no longer declares (e.g. a leftover `DART_PORT`). Crucially, a recipe that is **missing or won't parse yields `None`, which means skip pruning that checkout** (`registry.py:403-415`) — an unloadable recipe must never be read as "declares nothing" and nuke live entries. Results are cached per path within the call.
- `release(abspath)` (`registry.py:199-217`) — remove *all* rows for one checkout across all three files; returns count removed. Used by `splash env release` (no key) — *not* by `splash destroy`, which drops a single device row via `remove_device`.

`all_checkouts` (`registry.py:362-372`) and `summary_for` (`registry.py:374-388`) are read-only inspection helpers backing `splash status`.

## Key entry points

- Construction / path resolution — `registry.py:66-85`
- `_atomic_write` (complete mode-`0600` replacement) — `registry.py:46-55`
- `_lock` / `operation_lock` (sidecar flocks) — `registry.py:88-107`
- `_tsv_field` / `_TSV_FORBIDDEN` — `registry.py:37-43`
- `allocate_port` — `registry.py:156-182`
- `busy_ports` / `_busy_ports_unlocked` — `registry.py:137-154`
- `_port_in_use` — `registry.py:455-470`
- `remove_port` / `_remove_port_unlocked` — `registry.py:189-197`
- `set_kv` / `get_or_create_kv` / `get_kv` / `all_for` — `registry.py:239-275`
- `set_device` / `remove_device` / `devices_for` — `registry.py:304-344`
- `gc` / `gc_devices` / `reconcile_with_recipes` / `release` — `registry.py:199-217`, `350-360`, `390-452`

## Gotchas

- **`busy_ports(gc=True)` writes.** The public method takes the ports lock before pruning. `allocate_port`, which already holds that lock, calls `_busy_ports_unlocked` instead to avoid a self-deadlock (`registry.py:137-154`).
- **Unlocked helpers assume the caller holds the lock.** `_busy_ports_unlocked(gc=True)` and `_remove_port_unlocked` may rewrite `ports.tsv`; only call them from an already-locked path (`registry.py:143`, `:193`).
- **Locks are non-reentrant.** Every `_lock` call opens a fresh fd, so taking the same sidecar twice in one process can self-deadlock. The operation lock is a different sidecar and is always outermost; multi-file operations acquire ports, kv, and device locks sequentially.
- **Operation locks are hash-sharded.** The 256-file bound means unrelated checkouts can occasionally serialize on the same sidecar. Correctness does not depend on each checkout receiving a unique inode.
- **In-range bound pins are never reallocated here.** If you want a *different* port, you must `remove_port` first — `allocate_port` alone will hand back the same one (`registry.py:158-166`).
- **Malformed rows vanish silently.** A wrong-width or non-int-port row is dropped on read with no warning (`registry.py:114-120`). A future rewrite will not preserve it.
- **No escaping means writes can raise.** Any field with a tab/newline/CR makes the mutator throw `ValueError` mid-operation; the file is rewritten only after all fields pass, so a partial write won't land — but the caller sees the exception.
- **`reconcile_with_recipes` swallows recipe load errors by design** (`registry.py:410-413`, bare-except `noqa`) — that's the "don't prune on unloadable recipe" guarantee, not an oversight.
- **`reconcile_with_recipes` imports recipe parsing lazily** because GC is a cold path. The dependency is one-way and is covered by Pylint's `cyclic-import` gate.

## Why

- **Flat TSV + `fcntl` over a database.** The git post-checkout hot path runs on every `cd`/checkout and must stay dependency-light and fast to start. A line-oriented TSV needs no driver, no schema migration, no daemon; `flock` gives machine-local mutual exclusion for free. The cost — full re-read/re-write per mutation — is negligible at the row counts a single machine's checkouts produce.
- **Sidecar lock file.** Atomic replacement changes the TSV inode, so the lock must live on a stable file that is never replaced — hence `<file>.lock` rather than the data file itself. Checkout operation locks use the same mechanism with a bounded hash-shard identity.
- **No escaping + reject control chars.** Escaping would complicate both the parser and the hot path. Since tabs/newlines/CR never appear in legitimate values, rejecting them at write time is a simpler, stricter guarantee than escaping — and it closes the row-forgery hole where a crafted value could mint a row for another checkout (`registry.py:32-43`).
- **Keep a bound in-range pin.** Stability beats optimality: a checkout's port should be sticky across dev-server restarts and only change on explicit `--force`. Probing-and-reallocating a bound port would break a running server for no benefit (`registry.py:158-166`).
- **Pins *and* live `bind()` probes.** Pins reserve across checkouts even when nothing is listening; probes catch occupants the registry can't know about. Neither alone is sufficient.

## Related

- [Ports and env](../features/ports-and-env.md) — user-facing model for port allocation and `splashdown.env`.
- [Resource cleanup](../features/resource-cleanup.md) — user-facing behavior of `gc`, `release`, and orphan reclamation.
