# Per-checkout overrides: `splashdown.local.toml`

> Feature PRD · Use case **UC9** (see `docs/product/use-cases.md`) ·
> Personas: parallel-agent / mobile developer (see `docs/product/persona.md`) ·
> Authoritative spec: `README.md` ("Per-checkout overrides: `splashdown.local.toml`").
> **Implemented by:** [recipe-and-templates](../tech/recipe-and-templates.md),
> [devices](../tech/devices.md).

## Overview

When a bug reproduces only in *one* checkout, the developer wants a throwaway device
target — a specific sim/emulator model + OS — scoped to that checkout alone, without
editing the committed recipe that every teammate and every parallel agent shares.

`splashdown.local.toml` is a **gitignored, per-checkout** file that *adds* extra
`[targets.<type>.<variant>]` variants on top of the committed `splashdown.toml`. It is
strictly **add-only**: it never overrides or repeats a recipe-declared variant. A name
collision with a recipe variant is an error, by design — the two files form a union, not
an override stack, so the recipe stays the single source of truth for shared targets while
each checkout layers its own one-offs.

`splash target add <type> <variant> --model/--ios/...` writes a local variant
programmatically; `splash target remove <type> <variant>` strips it and, by default, also
destroys that variant's sim/emulator instance (opt out with `--keep-instance`). Once added,
the variant behaves like any recipe target: `splash run <type> <variant>` boots it.

## How it works (current state)

Two TOML files are loaded independently and unioned at read time — the local file is never
merged into the recipe on disk. Both documents are validated completely before their values
are used:

- The committed recipe is parsed into a `Recipe`; unknown top-level sections and fields are
  hard errors, and its `[targets.*]` tables go through the shared target validator.
- The per-checkout file is parsed into a `LocalConfig` (`src/splashdown/recipe.py:877`),
  using the same target validator. Its only permitted top-level sections are `settings` and
  `targets`; both are fully validated, including unknown nested fields. `LocalConfig.load`
  returns an empty config when the file is absent, so a checkout with no local variants is
  the normal case.
- `merged_targets` (`src/splashdown/recipe.py:988`) unions the two catalogs. It copies the
  recipe's per-type variant dicts, then folds in each local variant — and **raises** if a
  `(type, variant)` pair already exists in the recipe bucket. This is the collision rule.
- `resolve_variant` (`src/splashdown/recipe.py:1028`) selects one variant from a single
  type's merged catalog for the `run`/`start`/`stop`/`destroy` verbs: explicit name wins,
  else `default`, else the sole variant, else an error listing the choices.

The CLI side:

- `add` validates, then writes the variant. `_target_add` (`src/splashdown/commands.py:1656`) collects
  the `--model/--ios/--device/--image/--name/--id/--platform` flags into a field dict and
  calls `target_add` (`src/splashdown/devices.py:913`). It rejects flags that do not belong
  to the selected target type, validates field values, re-checks for collisions against
  *both* the recipe and any existing local variant, renders the edit in memory, then parses
  the complete result as `LocalConfig` before writing. A bad flag or malformed existing
  document therefore leaves the file untouched. The tomlkit writer preserves comments and
  unrelated valid tables.
- `remove` preflights the variant and computes the edited TOML through
  `_prepare_target_remove` (`src/splashdown/devices.py:945`) before any lifecycle action. Unless `--keep-instance` is set or the
  type is a physical `device`, it destroys the registry row's actual simulator UDID/AVD name when
  one exists, falling back to the currently resolved name only for an unprovisioned target. It
  then writes the prepared TOML and drops the registry row. `target_remove_text`
  (`src/splashdown/tomlio.py`) prunes now-empty parent tables. Recipe-owned or missing variants
  and malformed config fail before destruction; if the lifecycle step raises, the registry row
  and local declaration remain intact. An already-absent registered instance is accepted.
- Consumers read the union the same way everywhere: `cmd_targets_list` and
  `_load_variant_spec` (`src/splashdown/commands.py:653`) both go through
  `merged_targets`, and `_resolve_variant_for_cli` (`src/splashdown/commands.py:1056`)
  feeds it into `resolve_variant`.

## Key entry points

- `src/splashdown/recipe.py:877` — `LocalConfig`, the per-checkout config type.
- `src/splashdown/recipe.py:867` — `_TargetConfig.load` (absent file → empty).
- `src/splashdown/recipe.py:827` — `LOCAL_SKELETON`, the commented template seeded when the
  file is first created.
- `src/splashdown/recipe.py:988` — `merged_targets` (the union + collision error).
- `src/splashdown/recipe.py:1028` — `resolve_variant`.
- `src/splashdown/commands.py:1740` — `_target_dispatch` routes to `_target_add` / `_target_remove`.
- `src/splashdown/commands.py:653` — `_load_variant_spec` (union lookup used by target refresh).
- `src/splashdown/devices.py:913` — `target_add` (collision re-check + write).
- `src/splashdown/devices.py:945` — `_prepare_target_remove` (removal ownership and TOML preflight).
- `src/splashdown/devices.py:965` — `target_remove` (local-file edit).
- `src/splashdown/tomlio.py:205` / `src/splashdown/tomlio.py:214` — tomlkit writers.
- `src/splashdown/cli.py:267` — `target add` parser and its
  `--model/--ios/--device/--image/--name/--id/--platform` flags.
- `src/splashdown/cli.py:293` — `target remove` parser and `--keep-instance`.

## Configuration

The file lives at `splashdown.local.toml` in the checkout root. It carries `[targets.*]`
tables (shape identical to the recipe's) plus an optional `[settings]` table (see
[Settings](#settings)):

```toml
# Reproduce a bug only this checkout sees:
[targets.simulator.repro-bug]
model = "iPhone 16"
ios   = "17.5"
```

`splash target add <type> <variant>` flags (`src/splashdown/cli.py:267`) are optional, but
only the fields belonging to the selected type are accepted:

| Flag | Field | Used by |
|---|---|---|
| `--model` | `model` | simulator |
| `--ios` | `ios` | simulator (runtime, e.g. `17.5`) |
| `--device` | `device` | emulator (AVD device profile) |
| `--image` | `image` | emulator (system image) |
| `--name` | `name` | simulator/emulator name override; device match by name |
| `--id` | `id` | device: exact udid / adb serial |
| `--platform` | `platform` | device: narrow auto-pick to `ios`/`android` |

All supplied values must be non-empty strings; `platform` is restricted to `ios` or
`android`. The same field rules apply to hand-written recipe, local, and global target
tables.

`splash target remove <type> <variant>` takes `--keep-instance`
(`src/splashdown/cli.py:293`): edit the TOML only, leaving the sim/emulator and registry row
intact. Without it, the prepared TOML is written after successful instance deletion and before
the registry row is removed (no lifecycle effect for physical `device` targets, which have no
instance or registry row).

## Settings

Behavior toggles live in a `[settings]` table, resolved by `load_settings`
(`src/splashdown/recipe.py`). Two sources, highest priority first: this checkout's
`splashdown.local.toml`, then the machine-wide `~/.config/splashdown/config.toml`
(honoring `$XDG_CONFIG_HOME`, resolved at call time so tests can monkeypatch it). A
per-checkout value wins over the global one, which wins over the built-in default. Both
files are read with stdlib `tomllib`, keeping the provisioning read-path dependency-free.

```toml
# splashdown.local.toml (or ~/.config/splashdown/config.toml)
[settings]
prefix_match = false   # default true
```

| Setting | Default | Effect |
|---|---|---|
| `prefix_match` | `true` | `splash run`/`start`/`stop`/`destroy` accept unique-prefix `TYPE`/`VARIANT` args (see [device-targets](device-targets.md)). |

Loading either auxiliary config validates the whole document, not just the selected setting:
only `settings` and `targets` are legal top-level sections, and unknown fields or wrong value
types raise. Recognized settings are whitelisted in `_SETTINGS_SCHEMA`; add new toggles there
and to the `Settings` dataclass.

## Gotchas

- **`splashdown.local.toml` must stay in `.gitignore`.** It holds per-checkout,
  machine-specific device declarations that differ between machines and worktrees — it is
  not meant to be shared. If `.gitignore` is missing the entry, the file is tracked, every
  fresh clone inherits it, and `splash target add` mutating it pollutes `git status`
  permanently. The seeded skeleton's first line announces it is gitignored
  (`src/splashdown/recipe.py:827`), but nothing re-enforces the ignore on every run — verify
  the entry exists. (Matches README "Gotchas".)
- **Add-only, by hard rule.** A local variant whose name duplicates a recipe variant is an
  error in three places — at write time in `target_add`
  (`src/splashdown/devices.py:913`), at read time in `merged_targets`
  (`src/splashdown/recipe.py:988`), and `_prepare_target_remove` refuses to select a recipe
  variant for removal. To change a shared target, edit `splashdown.toml`; to shadow one locally,
  pick a different variant name.
- **Target fields are type-specific.** The CLI exposes all target flags on one parser, but
  `target add simulator ... --device=...` (and equivalent incompatible combinations) fails
  before the local file is written. Hand-written local/global/recipe tables follow the same
  rule; unknown fields are never retained as extensions.
- **Default `remove` destroys state.** `splash target remove` is destructive by default — it
  deletes the sim/emulator and the registry row, not just the TOML table. Pass `--keep-instance`
  for a config-only edit. Ownership and both config files are validated first, and a lifecycle
  error aborts the edit. `--keep-instance` leaves any registry row untouched; the next
  `splash target refresh` drops that now-undeclared row and destroys the retained instance.
- **Malformed config blocks removal.** A missing committed recipe is treated as an empty one, but
  an existing recipe or local file that cannot be parsed aborts the ownership preflight before
  any device or registry operation.

## Why

The committed recipe is the team/agent-shared contract; mutating it for a one-off repro
device would ripple to every teammate and every parallel-agent worktree. A gitignored,
add-only local layer lets a single checkout grow a throwaway target with zero blast radius,
which is exactly UC9's value: bug-repro isolation without touching version control.
