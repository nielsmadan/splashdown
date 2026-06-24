# Resource cleanup — freeing ports/sims for deleted checkouts

> Covers **UC7** (`docs/product/use-cases.md`): "When I delete worktrees, I want their
> reserved ports/sims freed, so the machine doesn't leak resources." Audience: the
> parallel-agent developer and the mobile/web secondary personas (`docs/product/persona.md`)
> who create and destroy worktrees rapidly. `README.md` is the authoritative spec.

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
post-checkout git hook). `Registry.allocate_port` calls `busy_ports(gc=True)`
(`src/splashdown/registry.py:143`), which reads every port row, skips any whose checkout
directory no longer exists, and rewrites `ports.tsv` if anything was dropped
(`src/splashdown/registry.py:119`). So a dead worktree's port is reclaimed the next time
*any* checkout allocates a port in that range — no command, no human. This is why UC7 is
"occasional, low stakes": the common case self-heals.

Lazy GC only touches **port** rows on the allocation path. Stale **kv** rows (uuids,
template results, `set` values) and **device** rows are not swept by allocation alone —
they accumulate until an explicit `splash gc`. They are harmless (each is scoped to a dead
abspath and never matched by a live checkout), but they do persist.

### `splash gc` (explicit, also sims + recipe reconcile)

`cmd_gc` (`src/splashdown/commands.py:840`) is the full machine-wide sweep, in two parts:

- `cmd_target_gc` (`src/splashdown/commands.py:786`) iterates every device row; for any
  whose checkout directory is gone, it destroys the underlying sim/AVD (`xcrun simctl
  delete` / `avdmanager delete avd`) and removes the registry row.
- `Registry.gc` (`src/splashdown/registry.py:392`) then drops dead-checkout **port** and
  **kv** rows, calls `gc_devices` (drops device rows whose checkout is gone *or* whose
  sim/AVD was hand-deleted — an "orphan", `src/splashdown/registry.py:312`), and finally
  `reconcile_with_recipes` (`src/splashdown/registry.py:352`).

`reconcile_with_recipes` is a subtler form of cleanup: it drops port/kv entries for
checkouts that **still exist** but whose current recipe no longer declares that key (e.g. a
leftover `DART_PORT` after a profile stopped emitting it). Critically, a checkout whose
recipe is missing or won't parse is left **untouched** — an unloadable recipe must never be
read as "declares nothing" and nuke live entries.

Orphan detection (`_is_orphan_device`, `src/splashdown/devices.py:254`) covers the case
where the user ran `xcrun simctl delete` / `avdmanager delete avd` by hand, leaving the
registry pointing at a ghost; `gc` removes those rows.

### `splash target prune [ios|android]` (foreign sims splashdown didn't create)

`cmd_target_prune` (`src/splashdown/commands.py:985`) is *not* about dead checkouts. It
computes `registry.managed_udids()` (every sim/AVD splashdown created) and discovers every
device on the machine **not** in that set:

- `_discover_foreign_ios` (`src/splashdown/commands.py:951`) lists available sims via
  `xcrun simctl list devices -j`, excluding managed UDIDs.
- `_discover_foreign_avds` (`src/splashdown/commands.py:971`) lists AVDs via `avdmanager
  list avd -c`, excluding managed names.

It prints the kill list, then gates on `_confirm` (`src/splashdown/commands.py:1101`, an
interactive `[y/N]` prompt). `--dry-run` lists and exits without destroying; `--yes` skips
the prompt. The platform arg (`ios` | `android` | `all`, default `all`) scopes which
discovery runs. Splashdown-managed devices in the registry are always preserved — this is
the command that clears the Xcode default-template pile (UC4).

### `splash env release [KEY]` (this checkout's own allocations)

Handled in `_env_dispatch` (`src/splashdown/commands.py:1563`). The target checkout is
`str(cwd.resolve())` by default, or `--checkout` to point at another checkout's entries —
normalized the same way `provision()` keys the registry, so symlinked/relative invocations
don't silently miss.

- `splash env release KEY` → `remove_kv(target, KEY)` + `remove_port(target, KEY)`
  (`src/splashdown/registry.py:222`, `:158`): frees one resource by key.
- `splash env release` (no key) → `registry.release(target)`
  (`src/splashdown/registry.py:168`): removes **all** port, kv, and device rows for the
  checkout in one pass and reports the count. Note `release` does *not* destroy the
  underlying sim/AVD — it only drops the registry rows; the sim itself is reclaimed by a
  later `gc`/`gc_devices` orphan sweep, or by `target prune` once it's no longer managed.

All registry mutations take the relevant `fcntl` file lock, so concurrent agents in
sibling worktrees can't corrupt the TSVs.

## Key entry points

| Concern | Location |
| --- | --- |
| Lazy GC of port rows on allocation | `src/splashdown/registry.py:119` (`busy_ports`), `:143` (caller) |
| Full machine-wide registry GC | `src/splashdown/registry.py:392` (`Registry.gc`) |
| Device-row GC (gone checkout + orphans) | `src/splashdown/registry.py:312` (`gc_devices`) |
| Recipe reconcile for live checkouts | `src/splashdown/registry.py:352` (`reconcile_with_recipes`) |
| Free one key for a checkout | `src/splashdown/registry.py:158` (`remove_port`), `:222` (`remove_kv`) |
| Free everything for a checkout | `src/splashdown/registry.py:168` (`Registry.release`) |
| `splash gc` orchestration | `src/splashdown/commands.py:840` (`cmd_gc`), `:786` (`cmd_target_gc`) |
| `splash target prune` | `src/splashdown/commands.py:985` (`cmd_target_prune`) |
| Foreign-device discovery | `src/splashdown/commands.py:951` (`_discover_foreign_ios`), `:971` (`_discover_foreign_avds`) |
| Confirmation prompt | `src/splashdown/commands.py:1101` (`_confirm`) |
| `splash env release` dispatch | `src/splashdown/commands.py:1563` |
| Orphan-device test | `src/splashdown/devices.py:254` (`_is_orphan_device`) |
| CLI parsers (`gc`, `target prune`, `env release`) | `src/splashdown/cli.py:183`, `:231`, `:179` |

## Configuration

There is no config to enable cleanup — it is built into the registry. The relevant knobs
are command flags:

- `splash target prune [ios|android|all]` — `--dry-run` (preview, no destroy), `--yes`
  (skip the confirm prompt). Parser: `src/splashdown/cli.py:231`.
- `splash env release [KEY]` — optional positional `KEY`; `--checkout PATH` to target a
  different checkout. Parser: `src/splashdown/cli.py:179`.
- `splash gc` takes no flags. Parser: `src/splashdown/cli.py:183`.

Registry location follows `$XDG_STATE_HOME` (default `~/.local/state`), resolved at
`Registry` instantiation (`src/splashdown/registry.py:62`) so tests can override it.

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
  device that only `gc` (orphan path) or `target prune` (foreign path) will actually
  destroy. Don't expect `env release` to free a simulator's disk.
- **`reconcile_with_recipes` deliberately skips unparseable recipes.** If a checkout's
  `splashdown.toml` is missing or invalid, its rows are *kept*, not pruned — a parse error
  must never be misread as "declares no resources." Expect stale-looking rows to linger for
  a checkout with a broken recipe; that's intentional, not a bug.
- **`target prune` is destructive and machine-wide.** It deletes *every* sim/AVD splashdown
  didn't create, across all projects — not just this checkout's. Always `--dry-run` first;
  the `_confirm` gate is the only safety net when `--yes` is absent.
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
