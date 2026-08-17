# Resource cleanup — freeing ports/sims for deleted checkouts

> Covers **UC7** (`docs/product/use-cases.md`): "When I delete worktrees, I want their
> reserved ports/sims freed, so the machine doesn't leak resources." Audience: the
> parallel-agent developer and the mobile/web secondary personas (`docs/product/persona.md`)
> who create and destroy worktrees rapidly. `README.md` is the authoritative spec.
> **Implemented by:** [registry](../tech/registry.md), [devices](../tech/devices.md).

## Table of contents

- [Overview](#overview)
- [How it works (current state)](#how-it-works-current-state)
- [Key entry points](#key-entry-points)
- [Configuration](#configuration)
- [Gotchas](#gotchas)
- [Why](#why)

## Overview

splashdown pins per-checkout resources (ports, env vars / uuids / template values, and
sim/AVD instances) into a machine-wide registry under `$XDG_STATE_HOME/splashdown/`
(`ports.tsv`, `kv.tsv`, `devices.tsv`). When a worktree is deleted, its rows linger — the
registry has no way to be notified that a checkout directory vanished. Cleanup therefore
happens through four distinct, deliberately-layered mechanisms, ordered from
most-automatic to most-explicit:

1. **Lazy / automatic GC** — every port allocation drops dead-checkout *port* rows (and,
   transitively on `gc()`, kv rows) before picking a free port. Mostly invisible; the
   common case needs no command.
2. **`splash gc`** — explicit, machine-wide sweep. Does everything lazy GC does *plus*
   destroys orphaned sims/AVDs and reconciles live checkouts against their current recipes.
3. **`splash target prune [ios|android]`** — destroys sims/AVDs splashdown did *not*
   create (the Xcode default-template pile, hand-made sims). Orthogonal to GC: it targets
   *foreign* devices, not dead-checkout ones.
4. **`splash env release [KEY]`** — frees *this* checkout's own allocations (all, or one
   key), for a checkout that still exists. The manual counterpart to lazy GC.

The key mental model: lazy GC and `gc` reclaim resources for checkouts that are **gone**;
`prune` reclaims devices splashdown **never owned**; `env release` reclaims a live
checkout's **own** resources on demand.

## How it works (current state)

### Lazy / automatic GC (ports + kv, on next allocation)

Port allocation is the hot path that runs on every `splash`/`sync` (and thus on the
post-checkout git hook). `Registry.allocate_port` calls `_busy_ports_unlocked(gc=True)`
(`src/splashdown/registry.py:174`), which reads every port row, skips any whose checkout
directory no longer exists, and rewrites `ports.tsv` if anything was dropped
(`src/splashdown/registry.py:143-154`). So a dead worktree's port is reclaimed the next time
*any* checkout allocates a port in that range — no command, no human. This is why UC7 is
"occasional, low stakes": the common case self-heals.

Lazy GC only touches **port** rows on the allocation path. Stale **kv** rows (uuids,
template results, `set` values) and **device** rows are not swept by allocation alone —
they accumulate until an explicit `splash gc`. They are harmless (each is scoped to a dead
abspath and never matched by a live checkout), but they do persist.

### `splash gc` (explicit, also sims + recipe reconcile)

`cmd_gc` (`src/splashdown/commands.py:738`) is the full machine-wide sweep, in two parts:

- `cmd_target_gc` (`src/splashdown/commands.py:683`) iterates every device row. For a dead
  checkout it destroys the underlying sim/AVD before removing the row; for a live checkout
  it also drops a row whose instance was already deleted by hand. A platform
  `CapabilityError` warns once and preserves that row because splashdown could not verify or
  destroy it.
- `Registry.gc(include_devices=False)` (`src/splashdown/registry.py:430`) then drops
  dead-checkout **port** and **kv** rows and runs `reconcile_with_recipes`
  (`src/splashdown/registry.py:390`). Device cleanup is explicitly disabled here because the
  capability-aware command sweep has already handled every device row; a second generic
  `gc_devices` pass could erase a row that the first pass deliberately preserved. Device
  probing stays in the orchestration layer, so the persistence module never imports platform
  lifecycle code.

`reconcile_with_recipes` is a subtler form of cleanup: it drops port/kv entries for
checkouts that **still exist** but whose current recipe no longer declares that key (e.g. a
leftover `DART_PORT` after a profile stopped emitting it). Critically, a checkout whose
recipe is missing or won't parse is left **untouched** — an unloadable recipe must never be
read as "declares nothing" and nuke live entries.

Orphan detection (`_is_orphan_device`, `src/splashdown/devices.py:343`) covers the case
where the user ran `xcrun simctl delete` / `avdmanager delete avd` by hand, leaving the
registry pointing at a ghost; `gc` removes those rows.

### `splash target prune [ios|android]` (foreign sims splashdown didn't create)

`cmd_target_prune` (`src/splashdown/commands.py:855`) is *not* about dead checkouts. It
computes `registry.managed_udids()` (every sim/AVD splashdown created) and discovers every
device on the machine **not** in that set:

- `_discover_foreign_ios` (`src/splashdown/commands.py:822`) lists available sims via
  `xcrun simctl list devices -j`, excluding managed UDIDs.
- `_discover_foreign_avds` (`src/splashdown/commands.py:838`) lists AVDs via `avdmanager
  list avd -c`, excluding managed names.

It prints the kill list, then gates on `_confirm` (`src/splashdown/commands.py:991`, an
interactive `[y/N]` prompt). `--dry-run` lists and exits without destroying; `--yes` skips
the prompt. The platform arg (`ios` | `android` | `all`, default `all`) scopes which
discovery runs. Splashdown-managed devices in the registry are always preserved — this is
the command that clears the Xcode default-template pile (UC4). When the default `all` scope
hits an unavailable platform, the dispatcher warns and continues with the other platform;
an explicit `target prune ios` or `target prune android` is strict and returns an error
instead (`src/splashdown/commands.py:1726`).

### `splash env release [KEY]` (this checkout's own allocations)

Handled in `_env_dispatch` (`src/splashdown/commands.py:1795`). The target checkout is
`str(cwd.resolve())` by default, or `--checkout` to point at another checkout's entries —
normalized the same way `provision()` keys the registry, so symlinked/relative invocations
don't silently miss.

- `splash env release KEY` → `remove_kv(target, KEY)` + `remove_port(target, KEY)`
  (`src/splashdown/registry.py:262`, `:189`): frees one resource by key.
- `splash env release` (no key) → `registry.release(target)`
  (`src/splashdown/registry.py:199`): removes **all** port, kv, and device rows for the
  checkout in one pass and reports the count. Note `release` does *not* destroy the
  underlying sim/AVD — it only drops the registry rows. The instance then becomes foreign
  and is discoverable by a later `target prune`; `gc` cannot associate it with the checkout
  after the row is gone.

All registry mutations take the relevant `fcntl` file lock and atomically replace the TSV,
so concurrent agents in sibling worktrees cannot corrupt an unlocked inspection read. Env
release also takes the checkout operation lock, preventing it from interleaving with that
checkout's sync/output commit.

## Key entry points

| Concern | Location |
| --- | --- |
| Lazy GC of port rows on allocation | `src/splashdown/registry.py:143` (`_busy_ports_unlocked`), `:174` (caller) |
| Full machine-wide registry GC | `src/splashdown/registry.py:430` (`Registry.gc`) |
| Device-row GC (gone checkout + live orphan rows) | `src/splashdown/commands.py:683` (`cmd_target_gc`) |
| Generic device-row GC helper | `src/splashdown/registry.py:350` (`gc_devices`; not called by `cmd_gc`) |
| Recipe reconcile for live checkouts | `src/splashdown/registry.py:390` (`reconcile_with_recipes`) |
| Free one key for a checkout | `src/splashdown/registry.py:189` (`remove_port`), `:262` (`remove_kv`) |
| Free everything for a checkout | `src/splashdown/registry.py:199` (`Registry.release`) |
| `splash gc` orchestration | `src/splashdown/commands.py:738` (`cmd_gc`), `:683` (`cmd_target_gc`) |
| `splash target prune` | `src/splashdown/commands.py:855` (`cmd_target_prune`) |
| Foreign-device discovery | `src/splashdown/commands.py:822` (`_discover_foreign_ios`), `:838` (`_discover_foreign_avds`) |
| Confirmation prompt | `src/splashdown/commands.py:991` (`_confirm`) |
| `splash env release` dispatch | `src/splashdown/commands.py:1795` |
| Orphan-device test | `src/splashdown/devices.py:343` (`_is_orphan_device`) |
| CLI parsers (`gc`, `target prune`, `env release`) | `src/splashdown/cli.py:199`, `:254`, `:186` |

## Configuration

There is no config to enable cleanup — it is built into the registry. The relevant knobs
are command flags:

- `splash target prune [ios|android|all]` — `--dry-run` (preview, no destroy), `--yes`
  (skip the confirm prompt). Parser: `src/splashdown/cli.py:254`.
- `splash env release [KEY]` — optional positional `KEY`; `--checkout PATH` to target a
  different checkout. Parser: `src/splashdown/cli.py:186`.
- `splash gc` takes no flags. Parser: `src/splashdown/cli.py:199`.

Registry location follows `$XDG_STATE_HOME` (default `~/.local/state`), resolved at
`Registry` instantiation (`src/splashdown/registry.py:66`) so tests can override it.

## Gotchas

- **No post-worktree-remove hook.** git fires a hook on *checkout*, but there is no
  post-worktree-remove / post-delete hook to call. So when a worktree is deleted, **nothing
  runs immediately**. Port reclamation happens lazily on the *next* allocation by some
  other checkout; **sim/AVD destruction does not happen at all until `splash gc` runs
  later**. An agent fleet that churns worktrees will accumulate orphaned sims until someone
  (or something scheduled) runs `gc`. This is the open leak-proofing gap tracked as **CD**
  in `docs/product/use-cases.md`.
- **Lazy GC sweeps ports, not kv or devices.** Only `busy_ports(gc=True)` runs on the
  allocation hot path, and it only rewrites `ports.tsv`. Stale kv and device rows survive
  until an explicit `splash gc`. Harmless but persistent.
- **`env release` drops rows, not sims.** `registry.release()` removes device rows but does
  not call `xcrun simctl delete` / `avdmanager delete avd`. The sim becomes an unmanaged
  device that only `target prune` can later discover and destroy. Don't expect `env release`
  to free a simulator's disk.
- **`reconcile_with_recipes` deliberately skips unparseable recipes.** If a checkout's
  `splashdown.toml` is missing or invalid, its rows are *kept*, not pruned — a parse error
  must never be misread as "declares no resources." Expect stale-looking rows to linger for
  a checkout with a broken recipe; that's intentional, not a bug.
- **`target prune` is destructive and machine-wide.** It deletes *every* sim/AVD splashdown
  didn't create, across all projects — not just this checkout's. Always `--dry-run` first;
  the `_confirm` gate is the only safety net when `--yes` is absent.
- **Unscoped platform cleanup is best-effort; explicit cleanup is strict.** Bare
  `target prune`/`target refresh` warn and skip an unavailable iOS or Android toolchain so the
  other platform can still be processed. An explicit platform argument propagates the
  capability error instead of reporting a partial platform-specific success.
- **A bound port pin is kept, not GC'd.** `allocate_port` keeps an existing in-range pin
  even if the port is currently bound (it's almost always this checkout's own dev server).
  Lazy GC only removes rows for checkouts whose *directory* is gone, not for live-but-bound
  ports. Forced reallocation goes through `sync --force`, which drops the pin first.

## Why

The four-layer split exists because the cleanup triggers are genuinely different. Dead
checkouts can't notify anyone (no hook), so the common resource — ports — is reclaimed
opportunistically on the next allocation, keeping the zero-touch promise for the
parallel-agent persona. Sims are expensive to enumerate and destroy (they shell out), so
that work is deferred to an explicit `gc` rather than run on every `cd`. Foreign sims are a
separate problem (Xcode/SDK churn, UC4) with its own confirm-gated command because deleting
devices a user made by hand is dangerous. And `env release` exists for the one case the
automatic paths can't cover: freeing a *live* checkout's allocation on purpose, without
deleting the worktree.
