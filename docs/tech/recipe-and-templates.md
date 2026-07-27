# Recipe parsing & the template engine

## Purpose

`recipe.py` owns the dependency-free read and validation side of all three TOML
documents: `splashdown.toml` (the committed recipe),
`splashdown.local.toml` (the gitignored per-checkout add-on), and the global
`config.toml`. It also owns the `{{ … }}` template engine that turns resource
specs into concrete values during a sync. `tomlio.py` owns comment-preserving
writes to those documents.

The split is deliberate and load-bearing for startup latency: reads go through
stdlib `tomllib`, writes go through `tomlkit`, and `tomlio` is structured so the
git-hook hot path never imports `tomlkit` at all (see [Why](#why)).

## Table of contents

- [How it works (current state)](#how-it-works-current-state)
  - [Recipe & LocalConfig parsing](#recipe--localconfig-parsing)
  - [`[targets.*]` parsing](#targets-parsing)
  - [merged_targets & resolve_variant](#merged_targets--resolve_variant)
  - [The template engine](#the-template-engine)
  - [Template preflight and dependency ordering](#template-preflight-and-dependency-ordering)
  - [_env_quote](#_env_quote)
  - [tomlio: comment-preserving writes](#tomlio-comment-preserving-writes)
- [Key entry points](#key-entry-points)
- [Gotchas](#gotchas)
- [Why](#why)
- [Related](#related)

## How it works (current state)

### Recipe, LocalConfig, and GlobalConfig parsing

`Recipe`, `LocalConfig`, and `GlobalConfig` validate the complete TOML document
when constructed. Their `load` classmethods read with stdlib `tomllib`; their
`parse` classmethods provide the same validation for an in-memory string before
a generated or edited document is written. Missing local and global files still
produce empty configs, so callers can load them unconditionally.

Validation is strict and centralized:

- A recipe permits only `project`, `apps`, `resources`, `targets`, and `setup`
  at the top level. Project fields and nested iOS/Android command settings are
  whitelisted and checked against the built-in workspace, loader, and profile
  registries.
- Every app requires `path`, `profile`, and a unique `resources` string array;
  resource references must name declarations in `[resources]`. This is metadata
  validation only — provisioning still operates on the top-level resource
  catalog.
- Resource validation is discriminated by `type`. Required fields, exact TOML
  types, port bounds, type-specific field leakage, and writer syntax are all
  rejected during load. `envfile=PATH` must be a non-empty relative path that
  resolves inside the checkout; absolute, `..`, and escaping-symlink paths fail.
- Every setup table permits only `run`, as either a non-empty string or a
  non-empty array of non-empty strings.
- Local and global documents permit only `settings` and `targets`; both sections
  are validated completely.

Unknown sections and fields are errors rather than extension points. Schema
errors consistently use
`SOURCE: [qualified.path] problem; expected ...`, including TOML decoding
errors. The existing `[devices.*]` rename diagnostic is retained in that format.

The objects continue exposing plain dictionaries (`project`, `apps`,
`resources`, `setup`, `targets`, and `settings`), so consumers do not need a
second model layer. `LOCAL_SKELETON` is the comment-only template written when a
local file is first scaffolded.

### `[targets.*]` parsing

All three documents share `_parse_targets_section`, and CLI mutation shares its
leaf validator, `validate_target_spec`. Together they enforce:

- `<type>` ∈ `TARGET_TYPES` (`simulator`/`emulator`/`device`) — anything else is
  an error listing the known types.
- `<variant>` matches `TARGET_VARIANT_RE`.
- each type table and each variant spec is a TOML table;
- `simulator` accepts only `model`, `ios`, and `name`;
- `emulator` accepts only `device`, `image`, and `name`;
- `device` accepts only `id`, `name`, and `platform`, with platform restricted
  to `ios` or `android`;
- every supplied leaf is a non-empty string.

It also carries one migration guard: any top-level `[devices.*]` table raises a
rename hint, because the section was renamed. The `source` argument is threaded
so error messages name the offending file.

### merged_targets & resolve_variant

`merged_targets` (`recipe.py:287`) unions the recipe's catalog with the local
catalog. The merge is **additive with collision = error**: if a `(type, variant)`
already exists in the recipe, redeclaring it in the local file raises rather than
overriding. This is the mechanism behind the "local adds, never overrides" model
— there is no precedence rule to reason about because overlap is simply illegal.

`resolve_variant` (`recipe.py:305`) picks one variant out of a *single type's*
catalog by these rules, in order: explicit name wins → else the variant literally
named `default` → else the sole variant if exactly one exists → else error. When
its `prefix_match` arg is set (the CLI passes `load_settings(cwd).prefix_match`),
an explicit arg that isn't an exact key expands to a unique-prefix match; 2+
matches raise an "ambiguous variant" error. It lazy-imports `DeviceError` from
`devices.py` to dodge the `devices → recipe` import cycle, and raises that type so
device commands get a consistent error class.

### Settings (load_settings)

`Settings` (`recipe.py`) is a small dataclass of behavior toggles —
`prefix_match: bool = True` for now. `load_settings(cwd)` merges two `[settings]`
tables, highest priority first: the checkout's `splashdown.local.toml`, then the
machine-wide `~/.config/splashdown/config.toml`. The global path is recomputed from
`XDG_CONFIG_HOME` at call time (mirroring `Registry`) so tests can monkeypatch the
env; both files are read with stdlib `tomllib`, keeping the provisioning read-path
dependency-free. Loading settings constructs `LocalConfig`/`GlobalConfig`, so
the *whole* document — not just `[settings]` — must be valid.
`_parse_settings` validates against the `_SETTINGS_SCHEMA` whitelist; unknown
keys and wrong value types raise. Add a new toggle by extending both
`_SETTINGS_SCHEMA` and `Settings`.

### The template engine

A resource value can be a template string containing `{{ expr }}` placeholders.
`render_template` (`recipe.py:152`) substitutes each via `_TEMPLATE_RE`, calling
`_safe_eval` (`recipe.py:144`) on the inner expression. A result that is still
callable is rejected — a guard against `{{ uuid }}` (the helper, not its call).

`_safe_eval` parses the expression with `ast.parse(mode="eval")` and walks it
with `_eval_node` (`recipe.py:103`). **This is an AST-walking interpreter, not
`eval()`.** `_eval_node` whitelists exactly: constants, names bound in `scope`,
a fixed set of binary ops (`_BINOPS`) and unary ops (`_UNARYOPS`), calls to
scope-provided callables, and subscript/slice (`_eval_subscript`,
`recipe.py:135`). Everything else — most importantly **attribute access** — falls
through to a `TemplateError`. Calls additionally forbid `*args`/`**kwargs`
unpacking and reject non-callable targets. Unknown names raise immediately rather
than resolving to `None`.

`_make_scope` (`recipe.py:35`) builds the evaluation namespace. It is the entire
template vocabulary:

- **Context values** computed from the checkout: `cwd` (basename), `cwd_abs`
  (resolved absolute path), `branch`, `repo` (git toplevel name via
  `_repo_name`, `recipe.py:62`, falling back to the dir name when git is absent),
  and `parent`.
- **Helpers**: `basename`, `dirname`, `slug` (`_slug`, `recipe.py:30`), `lower`,
  `upper`, `truncate`, `uuid`, `hash` (sha256 hex of `|`-joined args), and
  `port_hash` (sha256 folded into a `[lo, hi]` range, default 8000–9000, with
  keyword-overridable bounds).
- **Prior resolved resources**: `scope.update(resources)` folds already-resolved
  resource values in by name, so one resource's template can reference another's
  resolved value. This is what makes ordering matter.

`provision()` drives this: it walks resources in dependency order, building a
fresh scope from the accumulating `resolved` dict for each template
(`provisioning.py:71`).

### Template preflight and dependency ordering

Recipe validation preflights every template before provisioning can touch the
registry. Each placeholder is parsed as an expression and walked with the same
syntax restrictions as runtime evaluation. Unknown names, unmatched delimiters,
disallowed syntax, and references to undeclared resources are therefore load
errors. Helper-specific failures that need runtime values — for example invalid
arguments passed to a helper — remain runtime `TemplateError`s.

`template_refs` (`recipe.py:172`) extracts the resource names a template depends
on. It is **AST-based**: it parses each `{{ expr }}` and collects only
`ast.Name` nodes, so an identifier-looking string literal (`{{ "PORT" }}`) is not
mistaken for a dependency and cannot fabricate a phantom edge.

Validation builds the dependency graph and rejects cycles, including
self-references, while the complete document is still in memory. `topo_sort`
then orders the already-validated resources with a recursive DFS using the
classic **temporary-mark / permanent-mark** scheme:
`temp` flags the active path so re-entering a node still on it is a cycle (raised
as a `ValueError` naming the node), `seen` is the permanent set. The output lists
referents before referrers, which is exactly the order `provision()` needs.

### _env_quote

`_env_quote` (`recipe.py:370`) is the dotenv serializer used by the
`splashdown.env` writers in `provisioning.py`. Bare-safe values
(matching `_ENV_SAFE_RE`) pass through; anything else is **single-quoted** with
`'` escaped as `'\''`. Single quotes are intentional: the env file is `source`d
by a shell in two paths (devbox `init_hook`, the no-loader `set -a; source`
fallback), and double quotes would let `$(...)`/backticks execute. Single quotes
neutralize them and are read literally by mise/direnv too.

### tomlio: comment-preserving writes

`tomlio.py` is the *only* module that imports `tomlkit`, and its callers
(`commands.py`, `devices.py`) lazy-import it inside functions
(e.g. `devices.py:734`, `commands.py:1376`) so the read hot path never loads it.
Every function is a pure `str -> str` (or `str | None`) transform; callers own
file I/O.

- `render_scanned_recipe` builds a brand-new recipe document (header comment,
  `[project]`, `[apps.*]`, `[resources.*]`) from scratch. Scanner output and
  built-in preset output are passed through `Recipe.parse` before file I/O.
- `refresh_recipe` (`tomlio.py:92`) is the re-scan path: it `tomlkit.parse`s the
  existing text, mutates `[project]` in place and replaces `[apps.*]` wholesale
  (via `_set_apps`, `tomlio.py:55`), but **only appends** profile resources whose
  names aren't already present. It preserves comments and existing resource
  fields mechanically, but the rebuilt document must pass `Recipe.parse` before
  it replaces the file; an unknown or stale field therefore prevents the write.
- `ensure_mise_file_directive_text` (`tomlio.py:124`) idempotently ensures
  `_.file = "<env file>"` under `[env]`, handling the case where `_` already
  exists as a table (it sets the key in place rather than re-declaring a dotted
  key, which `tomlkit` would reject).
- `target_add_text` / `target_remove_text` back `splash target add/remove`: add
  creates the nested `[targets.<type>.<variant>]` table; remove deletes it and
  prunes now-empty parent tables, returning `None` when the variant was already
  absent. Add validates incompatible flags before rendering and parses the full
  local/global result before writing it.

## Key entry points

- `Recipe.load` / `Recipe.parse` — file and in-memory recipe validation.
- `LocalConfig.load` / `LocalConfig.parse` and `GlobalConfig.load` /
  `GlobalConfig.parse` — local/global validation.
- `_validate_resources`, `_validate_apps`, `_validate_setup`,
  `_validate_project` — recipe schema.
- `_parse_targets_section` / `validate_target_spec` — shared target schema.
- `recipe.py:287` — `merged_targets`; `recipe.py:305` — `resolve_variant`.
- `recipe.py:152` — `render_template`; `recipe.py:144` `_safe_eval`;
  `recipe.py:103` `_eval_node` (the AST sandbox).
- `recipe.py:35` — `_make_scope`.
- `recipe.py:172` — `template_refs`; `recipe.py:333` — `topo_sort`.
- `recipe.py:370` — `_env_quote`.
- `tomlio.py:92` — `refresh_recipe`; `tomlio.py:152`/`tomlio.py:171` —
  `target_add_text`/`target_remove_text`.

## Gotchas

- **The AST sandbox is a security boundary, not a convenience.** Recipes run
  automatically from the post-checkout git hook against whatever a checkout
  contains — i.e. potentially untrusted input. `_eval_node` is `eval`-free *on
  purpose*: an empty-`__builtins__` `eval` is not a real sandbox, because
  object-graph walks like `().__class__.__base__.__subclasses__()` reach
  `os`/`subprocess`. Forbidding attribute access is what closes that escape
  hatch. When extending the template language, never reintroduce attribute access
  or `eval`/`exec`, and add new capabilities only as scope helpers.
- **`refresh_recipe` can drop one standalone comment.** Because `[apps.*]` is
  replaced wholesale, a standalone comment sitting in the gap between the last
  `[apps.*]` table and the first `[resources.*]` table is lost on re-scan.
  Comments inside tables, inline comments, and the file header all survive. This
  is documented in `tomlio.py`'s module docstring.
- **Local cannot override recipe.** `merged_targets` raises on a `(type,
  variant)` collision instead of letting local win. Renaming the recipe variant
  is not how you customize one per checkout — pick a distinct name in the local
  file.
- **String literals do not create dependency edges.** A resource-looking string
  such as `{{ "PORT" }}` is just a string. Self-references are real edges and
  fail schema validation as a cycle.

## Why

- **AST interpreter, not `eval`** — the engine has to be safe against untrusted
  recipes executed by a git hook; see the gotcha above. The whitelist approach
  means the threat surface is the explicit `_BINOPS`/scope set, not the entire
  Python object graph.
- **tomllib read vs tomlkit write split** — reads and schema validation (the hot path: every
  post-checkout sync, `status`, completion) must stay dependency-light and fast,
  so they use stdlib `tomllib`. Only *writes* need to preserve comments, which
  is `tomlkit`'s job. Confining `tomlkit` to `tomlio.py`,
  keeping `tomlio` out of `__init__.py`'s re-exports, and lazy-importing it from
  callers guarantees the read path never pays `tomlkit`'s import cost.

## Related

- [docs/features/ports-and-env.md](../features/ports-and-env.md) — user-facing model for
  resources, ports, and the env file.
- [docs/features/per-checkout-overrides.md](../features/per-checkout-overrides.md) — the
  recipe vs local-config story and the add-only contract.
- [docs/tech/provisioning.md](./provisioning.md) — `provision()`, which consumes
  `topo_sort` + `render_template` + `_make_scope` and feeds the env writers that
  use `_env_quote`.
