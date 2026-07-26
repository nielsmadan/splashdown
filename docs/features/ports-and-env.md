# Per-checkout dev ports, env vars, and templated values

> **Problem:** UC1 — give each checkout free dev ports / env / templated values that "just work"
> on `cd`. See [use-cases](../product/use-cases.md), [persona](../product/persona.md).
> **Implemented by:** [provisioning](../tech/provisioning.md), [registry](../tech/registry.md),
> [recipe-and-templates](../tech/recipe-and-templates.md). `README.md` is the authoritative spec.

## Overview

When a worktree or checkout syncs, splashdown allocates free dev ports machine-wide, mints UUIDs, expands templates, and writes the concrete values to `splashdown.env` (or per-resource `writer` destinations). Every process in the checkout then sees the right `PORT`, `DATABASE_URL`, etc. with no hand-editing — this is what bare `splash` and the post-checkout hook run (UC1). The audience is the parallel-agent / worktree-heavy developer who needs each checkout to be a hermetic sandbox that "just works" on `cd`/checkout.

## Table of contents

- [How it works (current state)](#how-it-works-current-state)
- [Key entry points](#key-entry-points)
- [Configuration](#configuration)
- [Gotchas](#gotchas)
- [Why](#why)

## How it works (current state)

Bare `splash` (and the post-checkout hook) routes to `_cmd_provision_inner`, which provisions resources, writes outputs, then optionally runs a requested setup (`src/splashdown/commands.py:1316-1333`). `provision()` loads `splashdown.toml`, computes the checkout's absolute path and git branch, then resolves every `[resources.*]` entry **in topological order** so a `template` that references another resource sees the referent's value (`src/splashdown/provisioning.py:36-41`, `topo_sort` at `src/splashdown/recipe.py:419`). Resolution dispatches on `type`:

- **`port`** — reads a two-element `range = [lo, hi]` and calls `registry.allocate_port`, which returns the lowest free port in range, machine-wide (`src/splashdown/provisioning.py:44-51`, `src/splashdown/registry.py:135`). Allocation considers every other checkout's pinned ports from `ports.tsv` plus a live `bind()` probe on loopback (IPv4 and IPv6) so it also dodges ports held by non-splashdown processes (`src/splashdown/registry.py:146-151`, `_port_in_use` at `src/splashdown/registry.py:421`). An existing in-range pin for this checkout is kept as-is, even if currently bound — that bound port is almost always this checkout's own running dev server (`src/splashdown/registry.py:138-145`).
- **`uuid`** — returns the persisted value from the kv store if present, else mints a fresh `uuid4` and persists it; stable until `splash sync --force` (`src/splashdown/provisioning.py:52-55`).
- **`template`** — renders the `template = "..."` string against the current scope on every sync and refreshes its registry value, so referenced resources and expression edits propagate immediately (`src/splashdown/provisioning.py:62-69`).
- **`cwd` / `cwd-slug`** — the checkout directory name, raw or slugified (`src/splashdown/provisioning.py:56-61`).
- **`set`** — an externally supplied value: returns the persisted kv value, else the resource's `default`, else raises telling the user to run `splash env set NAME=VALUE` or add a `default` (`src/splashdown/provisioning.py:69-81`).

KV-backed (`uuid`/`template`/`set`/`cwd`/`cwd-slug`) values live in the machine-wide kv store via `set_kv`/`get_kv`; templates replace their row on every sync while the other types remain persisted. Ports live in `ports.tsv`. Both files are flat TSV under `$XDG_STATE_HOME/splashdown/`, `fcntl`-locked per operation.

The template engine renders `{{ expr }}` placeholders through a **restricted AST evaluator** — not `eval()` — that permits only literals, scope-bound names, calls to scope helpers, indexing/slicing, and arithmetic, and forbids attribute access entirely (`render_template` at `src/splashdown/recipe.py:173`, `_eval_node` at `src/splashdown/recipe.py:124`). The scope (`_make_scope`, `src/splashdown/recipe.py:38`) exposes the values `cwd`, `cwd_abs`, `branch`, `repo`, `parent`; the helpers `basename`, `dirname`, `slug`, `lower`, `upper`, `truncate`, `uuid`, `hash`, `port_hash`; and every already-resolved resource by name. A template that resolves to a callable (forgot to call a helper) is a `TemplateError`.

`write_outputs()` groups resolved values by each resource's `writer` field (default `splashdown-env`) and dispatches to a writer (`src/splashdown/provisioning.py:91-138`):

- **`splashdown-env`** — `splashdown.env`, written wholesale; splashdown owns the file (`write_splashdown_env`, `src/splashdown/provisioning.py:150`).
- **`envfile=PATH`** — any dotenv-format file; preserves non-managed lines, replacing only the keys splashdown manages (`write_envfile`, `src/splashdown/provisioning.py:163`).
- **`envrc`** — appends `export` lines to `.envrc.local` (`write_envrc`, `src/splashdown/provisioning.py:178`).
- **`stdout`** — echoes `KEY=value` lines.
- **`none`** — registry-only; allocates and persists but writes no file.

All file writes go through `_write_if_changed`, so a no-op sync touches nothing and reports "up to date" (`src/splashdown/provisioning.py:141`). Dotenv values are single-quoted when not bare-safe (`_env_quote`, `src/splashdown/recipe.py:456`) — single quotes because the env file is `source`d by a shell in the devbox init-hook and the no-loader fallback, where double quotes would let `$(...)`/backticks execute.

`splash sync --setup NAME` validates and runs the requested `[setup.NAME]` after provisioning and writer output. The block must contain a non-empty `run` string or array of non-empty strings. An unknown name, invalid block, or failing command exits nonzero; command execution stops at the first failure. Registry/file changes and earlier successful commands are not rolled back.

## Key entry points

- `src/splashdown/commands.py:1316` — `_cmd_provision_inner`, the bare-`splash`/hook flow: provision, write outputs, run setup, change-aware reporting.
- `src/splashdown/provisioning.py:27` — `provision()`, per-resource resolution in topo order.
- `src/splashdown/provisioning.py:91` — `write_outputs()`, writer dispatch and the envfile path-containment guard at `src/splashdown/provisioning.py:119`.
- `src/splashdown/provisioning.py:246` — `run_setup()`, validation and fail-fast command execution.
- `src/splashdown/registry.py:135` — `allocate_port`, lowest-free machine-wide + live `bind()` probe.
- `src/splashdown/registry.py:226` — `set_kv` / `get_kv` (`src/splashdown/registry.py:220`), persistence for non-port resources.
- `src/splashdown/recipe.py:173` — `render_template`; `src/splashdown/recipe.py:38` `_make_scope`; `src/splashdown/recipe.py:419` `topo_sort`.

## Configuration

`[resources.NAME]` tables in `splashdown.toml`. The resource name must be a valid env-var identifier (`src/splashdown/recipe.py:258`). Each has a `type`:

| type | required fields | value |
|------|-----------------|-------|
| `port` | `range = [lo, hi]` | lowest free port in range, machine-wide |
| `uuid` | — | a stable `uuid4` |
| `template` | `template = "..."` | derived `{{ expr }}` string, refreshed every sync |
| `cwd` / `cwd-slug` | — | checkout dir name (raw / slugified) |
| `set` | `default` (or `splash env set`) | externally supplied value |

Optional on any resource: `writer` ∈ `splashdown-env` (default), `envfile=PATH`, `envrc`, `stdout`, `none` (README "The `writer` field"). Most resources leave `writer` unset — the framework Profile routes them to `splashdown.env` implicitly; reach for `writer` only when no Profile covers the consumer.

Template scope values: `cwd`, `cwd_abs`, `branch`, `repo`, `parent`. Helpers: `basename`, `dirname`, `slug`, `lower`, `upper`, `truncate`, `uuid`, `hash`, `port_hash`. Plus any prior resolved resource by name (e.g. `template = "{{ PORT }}"`).

Stable-id pattern (from README): a Compose project name that's stable per checkout path and length-bounded:

```toml
[resources.COMPOSE_PROJECT_NAME]
type     = "template"
template = "myapp-test-{{ truncate(hash(cwd_abs), 8) }}"
# → "myapp-test-352e9e09"
```

## Gotchas

- **`set` with no value blocks the whole sync.** A `set` resource with neither a persisted value nor a `default` raises, aborting provision until the user runs `splash env set NAME=VALUE` or adds `default` (`src/splashdown/provisioning.py:69-81`).
- **Templates re-render; uuids and set values are sticky.** Editing a template or any referenced resource updates the result on the next sync. A direct `{{ uuid() }}` call therefore changes every time; reference a separate `type = "uuid"` resource for a stable composed value. Ports remain pinned in range, and `--force` reallocates ports and regenerates uuid resources without resetting user-set values.
- **`envfile=` paths are untrusted input and confined to the checkout.** Because the recipe auto-runs from the post-checkout hook on cloned repos, a committed `envfile=` that resolves to an absolute path or escapes the checkout via `..` is rejected — otherwise it's an arbitrary-file-write primitive (`src/splashdown/provisioning.py:119-123`).
- **Templates forbid attribute access by design.** `{{ x.foo }}` won't work; the evaluator only allows scope names, calls, indexing/slicing, and arithmetic (`src/splashdown/recipe.py:124-171`).
- **TSV has no escaping.** Resolved values containing a tab, newline, or CR are rejected at write time to prevent row forgery in the registry (`_tsv_field`, `src/splashdown/registry.py:40`).
- **No-op syncs are silent.** `_write_if_changed` means a re-sync of an already-provisioned checkout collapses to "up to date" and touches no files — the expected output through lefthook/husky on `git pull --rebase` (`src/splashdown/provisioning.py:141`).
- **Cyclic template references are a hard error.** `topo_sort` raises on a cycle among resource references (`src/splashdown/recipe.py:419-447`).

## Why

- **Restricted AST evaluator instead of `eval()`** — an empty-`__builtins__` `eval` is not a real sandbox (object-graph walks like `().__class__.__base__.__subclasses__()` reach `os`/`subprocess`), and recipes execute automatically from the post-checkout hook on untrusted checkouts (`src/splashdown/recipe.py:124-171`).
- **Single-quote dotenv quoting** — the env file is `source`d by a shell in two paths (devbox init-hook, no-loader fallback); double-quoted `$(...)`/backticks would execute, so single quotes neutralize them while mise/direnv still read them literally (`src/splashdown/recipe.py:456-464`).
- **Keep an existing bound port pin** — reallocating a port currently bound by this checkout's own dev server would move it out from under the running process; deliberate reallocation goes through `--force` (`src/splashdown/registry.py:138-145`).
