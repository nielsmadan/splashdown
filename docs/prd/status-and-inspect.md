# See this checkout's resolved values, devices, and what's free

## Overview

When a worktree's state is unclear — which ports it holds, whether those ports are bound right now, which device variants it declares and whether they're booted, how much registry cruft it has leaked — the developer (or the agent driving the worktree) needs to *read* the picture without re-deriving it from config files (UC8). `splash status` answers "what does this checkout have right now?": resolved env vars tagged `[in use]`/`[free]` for port-typed resources, declared device variants with boot state, and a stale-row count, with a hint at the unfilled `set` resources that still need a value. `splash status all` widens to a one-row-per-checkout fleet table; `--check` revalidates liveness and routes per-issue cleanup hints; `--format json` emits the same data machine-readably for agent/tooling consumers. `splash env` is the scriptable companion — list/get/set/release this checkout's values, with `--checkout` to reach another checkout. The audience is the parallel-agent / worktree-heavy developer who supervises many sandboxes and needs a predictable, parseable read of any one of them.

## Table of contents

- [How it works (current state)](#how-it-works-current-state)
- [Key entry points](#key-entry-points)
- [Configuration](#configuration)
- [Gotchas](#gotchas)
- [Why](#why)

## How it works (current state)

`cmd_status` is the single entry; it picks the target checkout list (`registry.all_checkouts()` for `all`, else just the resolved cwd) and branches on three axes — scope (`local`/`all`), `--verbose`, and `--format` (`src/splashdown/commands.py:618`, `src/splashdown/commands.py:634`). The branch matrix:

- **`all` + text + not verbose** → the compact fleet table (`_cmd_status_table`, `src/splashdown/commands.py:507`). Every other combination falls through to the per-block path.
- **default (`local`), or `all --verbose`, or any `json`** → builds one block per checkout via `_gather_status_for_checkout`, then either serializes JSON or emits text blocks (`src/splashdown/commands.py:653`, `src/splashdown/commands.py:665`, `src/splashdown/commands.py:672`).

**Per-checkout block (`_gather_status_for_checkout`, `src/splashdown/commands.py:425`).** Pulls this checkout's resolved resources from the registry (`registry.all_for`, `src/splashdown/registry.py:227`) and builds two lists:

- **Resource rows** come from `_gather_resource_entries` (`src/splashdown/commands.py:299`). For each `KEY=value` it tags a `port_state` only when the key is a `port`-typed resource — it reads `splashdown.toml` to learn which keys are ports, then probes liveness with `_port_in_use`, yielding `in use` / `free` (`src/splashdown/commands.py:310`, `src/splashdown/commands.py:314-324`). A malformed recipe is swallowed so status never dies; non-port keys get no tag.
- **Device/target rows** differ by scope. Default mode sources from recipe + local catalog via `_gather_targets_declared` (`src/splashdown/commands.py:370`): it walks `merged_targets(recipe, local)`, labels each variant `recipe` or `local`, resolves the instance name, and reads live boot status (`device_status` for sims/emulators, `physical_status` for hardware). `all` mode sources device rows from the registry instead (`_gather_devices_all`, `src/splashdown/commands.py:328`).

**Text emission (`_emit_status_block_text`, `src/splashdown/commands.py:468`).** Writes a human block to **stderr**: a `checkout:`/`=== … ===` header (with `[defunct]` when the checkout dir is gone), the `resources:` list with each port's `[in use]`/`[free]` suffix, and the `targets:` list (`type.variant`, source, instance name, status, and an `[orphan]`/`[stale]`/`[missing]` tag under `--check`).

**Fleet table (`_cmd_status_table`, `src/splashdown/commands.py:507`).** One row per checkout: short path, a per-source count summary (`registry.summary_for` → `_summary_string`), and an `ISSUE` column that only appears when `--check` flags at least one row (`defunct`/`orphan`/`stale`) — empty width is suppressed (`src/splashdown/commands.py:520-561`).

**`--check` (liveness revalidation).** Both the table and per-block paths accumulate counters into a shared `summary` dict (defunct checkouts + their orphaned registry rows, orphan devices whose underlying sim/AVD was deleted, stale devices with a newer OS available, missing devices declared but not yet created). `_print_check_summary` (`src/splashdown/commands.py:570`) prints the counts and routes each to the command that actually fixes it: `splash gc` for defunct rows, `splash target refresh` for orphan/stale, `splash run` for missing — or `Summary: all entries verified.` when clean. Note `gc` does **not** recreate an orphan whose checkout still lives; `target refresh` does (`src/splashdown/commands.py:605-612`).

**Default-mode footer (no `--check`).** After the block, default mode prints a lightweight stale-registry-row count (ports + kv whose checkout dir no longer exists) hinting at `splash gc`, and — separately — scans the recipe for `set` resources that have no `default` and no registry value yet, printing `N resource(s) need a value (…): run \`splash env set NAME=VALUE\`` (`src/splashdown/commands.py:677-708`).

**JSON output (machine-readable surface).** When `--format json`, the payload is the single block dict for `local` scope, or `{"checkouts": [...]}` for `all`; `--check` adds a top-level `"summary"` with the counter dict. It is printed to **stdout** (unlike the text blocks, which go to stderr) (`src/splashdown/commands.py:665-670`). Each block carries `checkout`, `exists`, `resources` (`{key, value, port_state}`), and `targets` (`{type, variant, source, device_name, status, orphan, stale, missing}`) — the stable contract agents/tooling consume to learn a worktree's allocation without parsing prose.

**`splash env` (`_env_dispatch`, `src/splashdown/commands.py:1528`).** The scriptable value surface:

- **bare `env`** — lists this checkout's resolved values as `KEY=value` on stdout (or a JSON object under `--format json`); empty prints `(empty) <path>` to stderr.
- **`env get KEY`** — prints the value to stdout, or exits **1** if the key has no registry value (the agent-friendly "do I have this?" probe).
- **`env set KEY=VALUE`** — validates `KEY` against `ENV_NAME_RE`, then persists to the kv store; this is how `type="set"` resources get filled (`src/splashdown/commands.py:1552-1562`).
- **`env release [KEY]`** — frees one key (both kv and port rows) or, with no key, releases all of this checkout's entries (`src/splashdown/commands.py:1563-1571`).

All four normalize the target via `str(Path(...).resolve())` so they key the registry exactly as `provision()` does; `get`/`set`/`release` (and bare list) accept `--checkout PATH` to operate on another checkout instead of the cwd (`src/splashdown/commands.py:1532`, `src/splashdown/commands.py:1545`).

## Key entry points

- `src/splashdown/commands.py:618` — `cmd_status`: scope/verbose/format branch matrix; default footer; JSON payload assembly.
- `src/splashdown/commands.py:425` — `_gather_status_for_checkout`: builds the block (resources + targets) shared by JSON and text.
- `src/splashdown/commands.py:299` — `_gather_resource_entries`: tags port-typed resources `in use`/`free` via `_port_in_use`.
- `src/splashdown/commands.py:370` — `_gather_targets_declared` (default mode, recipe+local) / `src/splashdown/commands.py:328` `_gather_devices_all` (`all` mode, registry).
- `src/splashdown/commands.py:468` — `_emit_status_block_text`: per-checkout text block to stderr.
- `src/splashdown/commands.py:507` — `_cmd_status_table`: compact fleet table (`all`, non-verbose, text).
- `src/splashdown/commands.py:570` — `_print_check_summary`: defunct/orphan/stale/missing counts + routed cleanup hints.
- `src/splashdown/commands.py:677-708` — default-mode footer: stale-row count + unfilled-`set` hint.
- `src/splashdown/commands.py:1528` — `_env_dispatch`: `env` list/get/set/release with `--checkout`.
- `src/splashdown/cli.py:128-143` — `status` parser: positional `scope` (`local`/`all`), `--check`, `--verbose`.
- `src/splashdown/cli.py:170-181` — `env` parser: `--checkout` on bare/get/set/release; `get` key, `set` `KEY=VALUE`, `release` optional key.
- `src/splashdown/cli.py:116` — root `--format {text,json}`; `src/splashdown/cli.py:373-384` — dispatch into `cmd_status` / `_env_dispatch`.
- `src/splashdown/registry.py:227` — `all_for` (resolved values per checkout); `src/splashdown/registry.py:336` — `summary_for` (per-source counts for the table); `src/splashdown/registry.py:324` — `all_checkouts`.

## Configuration

- **`--format {text,json}`** is a root-level flag (`src/splashdown/cli.py:116`) that parses at the root regardless of where it appears on the line. JSON is the stable machine-readable contract for both `status` and bare `env`.
- **`scope`** is positional: `splash status` (this checkout) vs `splash status all` (every tracked checkout). `--verbose` only changes `all`, expanding the compact table into per-checkout blocks (`src/splashdown/cli.py:139-143`).
- **`--check`** composes with every status variant; it triggers the liveness probes and the routed cleanup-hint footer.
- **`--checkout PATH`** on `env` (bare/get/set/release) targets a different checkout's registry entries; default is the cwd.
- The `[in use]`/`[free]` tagging requires the checkout's `splashdown.toml` to be present and parseable so port-typed resources can be identified; ports are probed live on loopback.

## Gotchas

- **`status` lists only resources that already have a registry value.** A `type="set"` resource with no `default` and no value yet never reaches the registry, so it does **not** appear as a resource row — it surfaces *only* via the default-mode footer hint pointing at `splash env set` (`src/splashdown/commands.py:692-708`). Don't read the absence of a row as "this resource doesn't exist."
- **`status` text goes to stderr; `--format json` and bare `env` listings go to stdout.** Piping `splash status` expecting stdout gets nothing useful; agents should use `--format json` (or `env`/`env get`) for stdout-parseable output (`src/splashdown/commands.py:472`, `src/splashdown/commands.py:669`).
- **`env get KEY` exits 1 when the key is absent** rather than printing an empty line — the intended "am I provisioned for this?" gate. Scripts must check the exit code, not just stdout (`src/splashdown/commands.py:1548-1549`).
- **`--check` cleanup hints are issue-specific.** `splash gc` drops *defunct-checkout* rows only; it will not recreate an orphan/stale device whose checkout still exists — that's `splash target refresh`. The footer routes each count to the right command so the user doesn't run the wrong one (`src/splashdown/commands.py:605-612`).
- **The `ISSUE` column in the fleet table is conditional.** It appears only when `--check` flags at least one row; without it (or with everything clean) the table is two columns. Parsers keying on a fixed column count will break — prefer `--format json` for the fleet view (`src/splashdown/commands.py:549-561`).
- **Default mode vs `all` mode source devices differently.** Default reads the recipe+local *declaration* (what this checkout *should* have); `all` reads the *registry* (what's actually pinned). A variant declared but never created shows in default mode but not in `all` (`src/splashdown/commands.py:447-458`).

## Why

The parallel-agent persona supervises 3–10+ worktrees that are created and destroyed rapidly, with no human watching any single one. `status`/`env` exist so that picture is *legible on demand*: a human debugging a suspected collision reads the text block, while an agent gates its own work on `--format json` or `env get` exit codes — the machine-readable contract the persona ranks third among its values. `status all` is the fleet view for the supervisor; `--check` plus routed hints turns "something's leaking" into a specific next command, which matters precisely because nobody runs `gc` proactively under churn.
