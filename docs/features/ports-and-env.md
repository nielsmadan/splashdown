# Per-checkout dev ports, env vars, and templated values

> **Problem:** UC1 — give each checkout free dev ports / env / templated values that "just work"
> on `cd`. See [use-cases](../product/use-cases.md), [persona](../product/persona.md).
> **Implemented by:** [provisioning](../tech/provisioning.md), [registry](../tech/registry.md),
> [recipe-and-templates](../tech/recipe-and-templates.md). `README.md` is the authoritative spec.

## Overview

When a worktree or checkout syncs, splashdown allocates free dev ports machine-wide, mints UUIDs, expands templates, and writes the concrete values to `splashdown.env` (or per-resource `writer` destinations). Every process in the checkout then sees the right `PORT`, `DATABASE_URL`, etc. with no hand-editing — this is what bare `splash` and the trusted post-checkout event handler run (UC1). The audience is the parallel-agent / worktree-heavy developer who needs each checkout to be a hermetic sandbox that "just works" on `cd`/checkout.

## Table of contents

- [How it works (current state)](#how-it-works-current-state)
- [Key entry points](#key-entry-points)
- [Configuration](#configuration)
- [Gotchas](#gotchas)
- [Why](#why)

## How it works (current state)

Bare `splash` routes to `_cmd_provision_inner`. The hidden hook handler checks clone trust first,
then uses the same locked provisioning path. It provisions resources, writes outputs, then
optionally runs a requested setup only for explicit sync (`src/splashdown/commands.py`).
`provision()` first loads and validates the complete `splashdown.toml`. Unknown sections or fields,
wrong types, invalid enums, malformed app/resource/setup/target tables, unsafe writers, and
statically invalid templates are hard errors. Validation finishes before any registry allocation or
output-file mutation, and errors identify the source plus the qualified path (for example,
`splashdown.toml: [resources.PORT.range] ...; expected ...`).

After validation, provisioning computes the checkout's absolute path and git branch, then resolves every `[resources.*]` entry **in topological order** so a `template` that references another resource sees the referent's value (`src/splashdown/provisioning.py`, `topo_sort` at `src/splashdown/recipe.py`). Resolution dispatches on `type`:

- **`port`** — reads a two-element `range = [lo, hi]` and calls `registry.allocate_port`, which returns the lowest free port in range, machine-wide (`src/splashdown/provisioning.py`, `src/splashdown/registry.py`). Allocation considers every other checkout's pinned ports from `ports.tsv` plus a live `bind()` probe on loopback (IPv4 and IPv6) so it also dodges ports held by non-splashdown processes (`src/splashdown/registry.py`, `_port_in_use` at `src/splashdown/registry.py`). An existing in-range pin for this checkout is kept as-is, even if currently bound — that bound port is almost always this checkout's own running dev server (`src/splashdown/registry.py`).
- **`uuid`** — returns the persisted value from the kv store if present, else mints a fresh `uuid4` and persists it; stable until `splash sync --force` (`src/splashdown/provisioning.py`).
- **`template`** — renders the `template = "..."` string against the current scope on every sync and refreshes its registry value, so referenced resources and expression edits propagate immediately (`src/splashdown/provisioning.py`).
- **`cwd` / `cwd-slug`** — the checkout directory name, raw or slugified (`src/splashdown/provisioning.py`).
- **`set`** — an externally supplied value: returns the persisted kv value, else the resource's `default`, else raises telling the user to run `splash env set NAME=VALUE` or add a `default` (`src/splashdown/provisioning.py`).

KV-backed (`uuid`/`template`/`set`/`cwd`/`cwd-slug`) values live in the machine-wide kv store via `set_kv`/`get_kv`; templates replace their row on every sync while the other types remain persisted. Ports live in `ports.tsv`. Both files are flat TSV under `$XDG_STATE_HOME/splashdown/`, `fcntl`-locked per operation.

The template engine renders `{{ expr }}` placeholders through a **restricted AST evaluator** — not `eval()` — that permits only literals, scope-bound names, calls to scope helpers, indexing/slicing, and arithmetic, and forbids attribute access entirely (`render_template` at `src/splashdown/recipe.py`, `_eval_node` at `src/splashdown/recipe.py`). Recipe validation parses every expression, rejects disallowed syntax and unknown names, and detects dependency cycles before allocation. The runtime scope (`_make_scope`, `src/splashdown/recipe.py`) exposes the values `cwd`, `cwd_abs`, `branch`, `repo`, `parent`; the helpers `basename`, `dirname`, `slug`, `lower`, `upper`, `truncate`, `uuid`, `hash`, `port_hash`; and every already-resolved resource by name. Errors that depend on runtime values, such as a template resolving to a callable because a helper was not called, remain runtime `TemplateError`s.

`write_outputs()` groups resolved values by each resource's `writer` field (default
`splashdown-env`) and returns typed `WriterResult` records for the CLI renderer:

- **`splashdown-env`** — `splashdown.env`, written wholesale; splashdown owns the file (`write_splashdown_env`, `src/splashdown/provisioning.py`).
- **`envfile=PATH`** — any dotenv-format file; preserves non-managed lines, replacing only the keys splashdown manages (`write_envfile`, `src/splashdown/provisioning.py`).
- **`envrc`** — appends `export` lines to `.envrc.local` (`write_envrc`, `src/splashdown/provisioning.py`).
- **`stdout`** — returns the values as an explicit disclosure record. Text sync prints
  `KEY=value`; JSON sync places them in one `stdout` object, so raw lines can never corrupt the
  JSON document.
- **`none`** — registry-only; allocates and persists but writes no file.

All file writes go through the same safe replacement path: existing symlinks and non-regular
files are rejected, regular files are replaced atomically, and a no-op sync touches nothing and
reports "up to date." Dotenv values are single-quoted when not bare-safe (`_env_quote`,
`src/splashdown/recipe.py`) — single quotes because the env file is `source`d by a shell in
the devbox init-hook and the no-loader fallback, where double quotes would let
`$(...)`/backticks execute.

Every `[setup.NAME]` is validated while the recipe loads, whether or not that setup was requested. The table accepts only `run`, containing either a non-empty string or a non-empty array of non-empty strings. This schema validation happens before provisioning. `splash sync --setup NAME` still executes the selected setup after provisioning and writer output; an unknown name or failing command exits nonzero, execution stops at the first failure, and registry/file changes plus earlier successful commands are not rolled back.

Operational sync output is key-only by default. Text change reports already name keys without
values; JSON returns `resolved_keys`. Add `--show-values` to replace that field with `resolved`.
The explicit `stdout` writer remains value-bearing in either mode.

## Key entry points

- `src/splashdown/commands.py` — `_cmd_provision_inner`, the bare-`splash`/hook operation.
- `src/splashdown/provisioning.py` — `provision`, `WriterResult`, `write_outputs`, and `run_setup`.
- `src/splashdown/cli_output.py` — value-redacted text/JSON sync rendering.
- `src/splashdown/registry.py` — `allocate_port`, lowest-free machine-wide + live `bind()` probe.
- `src/splashdown/registry.py` — `set_kv` / `get_kv`, persistence for non-port resources.
- `src/splashdown/recipe.py` — `render_template`, `_make_scope`, and `topo_sort`.

## Configuration

`[resources.NAME]` tables in `splashdown.toml` are discriminated by `type`. The resource name must be a valid env-var identifier. Unknown fields are rejected, including fields that belong to a different resource type:

| type | type-specific fields | value |
|------|----------------------|-------|
| `port` | required `range = [lo, hi]`, exactly two integers with `1 <= lo <= hi <= 65535` | lowest free port in range, machine-wide |
| `uuid` | none | a stable `uuid4` |
| `template` | required string `template = "..."` | derived `{{ expr }}` string, refreshed every sync |
| `cwd` / `cwd-slug` | none | checkout dir name (raw / slugified) |
| `set` | optional string `default` | externally supplied value; without a default, use `splash env set` |

Optional on any resource: `writer` ∈ `splashdown-env` (default), `envfile=RELATIVE_PATH`, `envrc`, `stdout`, `none` (README "The `writer` field"). An `envfile=` path must be non-empty, relative, and remain inside the checkout; bare `envfile`, absolute paths, escaping `..`, and lookalike prefixes are rejected before allocation. Most resources leave `writer` unset — the framework Profile routes them to `splashdown.env` implicitly; reach for `writer` only when no Profile covers the consumer.

Template scope values: `cwd`, `cwd_abs`, `branch`, `repo`, `parent`. Helpers: `basename`, `dirname`, `slug`, `lower`, `upper`, `truncate`, `uuid`, `hash`, `port_hash`. Plus any prior resolved resource by name (e.g. `template = "{{ PORT }}"`).

Stable-id pattern (from README): a Compose project name that's stable per checkout path and length-bounded:

```toml
[resources.COMPOSE_PROJECT_NAME]
type     = "template"
template = "myapp-test-{{ truncate(hash(cwd_abs), 8) }}"
# → "myapp-test-352e9e09"
```

## Gotchas

- **`set` with no value blocks the whole sync.** A `set` resource with neither a persisted value nor a `default` raises, aborting provision until the user runs `splash env set NAME=VALUE` or adds `default` (`src/splashdown/provisioning.py`).
- **Templates re-render; uuids and set values are sticky.** Editing a template or any referenced resource updates the result on the next sync. A direct `{{ uuid() }}` call therefore changes every time; reference a separate `type = "uuid"` resource for a stable composed value. Ports remain pinned in range, and `--force` reallocates ports and regenerates uuid resources without resetting user-set values.
- **Output paths and checkout entries are untrusted.** Clone trust covers future refs, so a later
  checkout can change writer paths and filesystem entries. `envfile=` paths must remain inside the
  checkout. Every writer also rejects
  a symlink or non-regular destination, including the fixed `splashdown.env` and `.envrc.local`
  names, so a checked-out link cannot redirect sync to another file.
- **Templates forbid attribute access by design.** `{{ x.foo }}` won't work; the evaluator only allows scope names, calls, indexing/slicing, and arithmetic (`src/splashdown/recipe.py`).
- **TSV has no escaping.** Resolved values containing a tab, newline, or CR are rejected at write time to prevent row forgery in the registry (`_tsv_field`, `src/splashdown/registry.py`).
- **No-op syncs are silent.** `_write_if_changed` means a re-sync of an already-provisioned checkout collapses to "up to date" and touches no files — the expected output through lefthook/husky on `git pull --rebase` (`src/splashdown/provisioning.py`).
- **Changing to JSON does not opt into secret disclosure.** Sync JSON contains `resolved_keys`,
  and bare env JSON is a sorted key array. Use `--show-values`, `env get`, or `writer = "stdout"`
  only when the destination is safe for the value.
- **Static template failures abort the whole sync before allocation.** Unknown names, disallowed expression syntax, unmatched delimiters, and cyclic resource references are rejected while the recipe loads. Runtime-only helper/value errors can still occur during rendering.
- **`splash env get NAME` is not a preview of a newly declared resource.** It reads this checkout's
  registry rows, and a resource lands there only when `provision()` runs. A newly declared resource
  therefore exits 1 until a sync.
- **Adding a resource silently takes over a hand-set key in the target file.** `write_envfile` drops every existing line whose key is now managed and re-emits it at the bottom of the file (`src/splashdown/provisioning.py`). So declaring `[resources.DB_NAME]` with `writer = "envfile=apps/api/.env"` replaces a manual `DB_NAME=...` line on the first sync. Unmanaged keys in that file are preserved untouched — but check for a pre-existing hand-tuned line before adding a resource for its key.
- **A recipe has no per-checkout conditional.** Every `[resources.*]` entry applies to *every* managed checkout, including the primary one; there is no "leave this unset in the main checkout, compute it only in worktrees". Design for a value that is uniform-by-construction (a deterministic function of the checkout) rather than one that special-cases a blessed directory.

## Why

- **Restricted AST evaluator instead of `eval()`** — an empty-`__builtins__` `eval` is not a real sandbox (object-graph walks like `().__class__.__base__.__subclasses__()` reach `os`/`subprocess`), and trusted hook execution can parse recipes from future refs (`src/splashdown/recipe.py`).
- **Single-quote dotenv quoting** — the env file is `source`d by a shell in two paths (devbox init-hook, no-loader fallback); double-quoted `$(...)`/backticks would execute, so single quotes neutralize them while mise/direnv still read them literally (`src/splashdown/recipe.py`).
- **Keep an existing bound port pin** — reallocating a port currently bound by this checkout's own dev server would move it out from under the running process; deliberate reallocation goes through `--force` (`src/splashdown/registry.py`).
