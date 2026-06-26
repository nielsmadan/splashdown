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
merged into the recipe on disk:

- The committed recipe is parsed into a `Recipe` (`src/splashdown/recipe.py:227`); its
  `[targets.*]` tables go through the shared `_parse_targets_section`
  (`src/splashdown/recipe.py:196`), which validates target types and variant-name syntax.
- The per-checkout file is parsed into a `LocalConfig` (`src/splashdown/recipe.py:267`),
  using the *same* `_parse_targets_section`. `LocalConfig.load`
  (`src/splashdown/recipe.py:278`) returns an empty config when the file is absent, so a
  checkout with no local variants is the normal case.
- `merged_targets` (`src/splashdown/recipe.py:287`) unions the two catalogs. It copies the
  recipe's per-type variant dicts, then folds in each local variant — and **raises** if a
  `(type, variant)` pair already exists in the recipe bucket. This is the collision rule.
- `resolve_variant` (`src/splashdown/recipe.py:305`) selects one variant from a single
  type's merged catalog for the `run`/`start`/`stop`/`destroy` verbs: explicit name wins,
  else `default`, else the sole variant, else an error listing the choices.

The CLI side:

- `add` writes the variant. `_target_dispatch` (`src/splashdown/commands.py:1464`) collects
  the `--model/--ios/--device/--image/--name/--id/--platform` flags into a field dict and
  calls `target_add` (`src/splashdown/devices.py:710`), which re-checks for collisions
  against *both* the recipe and any existing local variant before appending the table via
  the tomlkit writer `target_add_text` (`src/splashdown/tomlio.py:152`). tomlkit is used so
  comments and unrelated tables in the local file survive the edit.
- `remove` strips the variant. In `_target_dispatch`
  (`src/splashdown/commands.py:1483`), unless `--keep-instance` is set and the type is not a
  physical `device`, it resolves the instance name, destroys the sim/emulator
  (`device_destroy`, suppressing "already gone" errors), drops the registry device row, then
  edits the TOML via `target_remove` (`src/splashdown/devices.py:739`) →
  `target_remove_text` (`src/splashdown/tomlio.py:171`), which prunes now-empty parent
  tables. `target_remove` refuses recipe-declared variants outright.
- Consumers read the union the same way everywhere: `cmd_targets_list` and
  `_load_variant_spec` (`src/splashdown/commands.py:756`) both go through
  `merged_targets`, and `_resolve_variant_for_cli` (`src/splashdown/commands.py:1148`)
  feeds it into `resolve_variant`.

## Key entry points

- `src/splashdown/recipe.py:267` — `LocalConfig`, the per-checkout config type.
- `src/splashdown/recipe.py:278` — `LocalConfig.load` (absent file → empty).
- `src/splashdown/recipe.py:248` — `LOCAL_SKELETON`, the commented template seeded when the
  file is first created.
- `src/splashdown/recipe.py:287` — `merged_targets` (the union + collision error).
- `src/splashdown/recipe.py:305` — `resolve_variant`.
- `src/splashdown/commands.py:1464` — `_target_dispatch` (`add`/`remove` orchestration).
- `src/splashdown/commands.py:756` — `_load_variant_spec` (used by `remove` to find the
  instance to destroy).
- `src/splashdown/devices.py:710` — `target_add` (collision re-check + write).
- `src/splashdown/devices.py:739` — `target_remove` (refuses recipe variants).
- `src/splashdown/tomlio.py:152` / `src/splashdown/tomlio.py:171` — tomlkit writers.
- `src/splashdown/cli.py:244` — `target add` parser and its
  `--model/--ios/--device/--image/--name/--id/--platform` flags.
- `src/splashdown/cli.py:263` — `target remove` parser and `--keep-instance`.

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

`splash target add <type> <variant>` flags (`src/splashdown/cli.py:244`), all optional and
written verbatim as the table's string fields:

| Flag | Field | Used by |
|---|---|---|
| `--model` | `model` | simulator / emulator (device model) |
| `--ios` | `ios` | simulator (runtime, e.g. `17.5`) |
| `--device` | `device` | emulator (AVD device profile) |
| `--image` | `image` | emulator (system image) |
| `--name` | `name` | sim/emulator name override; device: match by name |
| `--id` | `id` | device: exact udid / adb serial |
| `--platform` | `platform` | device: narrow auto-pick to `ios`/`android` |

`splash target remove <type> <variant>` takes `--keep-instance`
(`src/splashdown/cli.py:269`): edit the TOML only, leaving the sim/emulator alive. Without
it, the instance is destroyed (no effect for physical `device` targets, which have no
instance to destroy).

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

`_parse_settings` strictly validates the table — unknown keys and wrong value types raise,
so a typo'd toggle never silently no-ops. Recognized keys are whitelisted in
`_SETTINGS_SCHEMA`; add new toggles there and to the `Settings` dataclass.

## Gotchas

- **`splashdown.local.toml` must stay in `.gitignore`.** It holds per-checkout,
  machine-specific device declarations that differ between machines and worktrees — it is
  not meant to be shared. If `.gitignore` is missing the entry, the file is tracked, every
  fresh clone inherits it, and `splash target add` mutating it pollutes `git status`
  permanently. The seeded skeleton's first line announces it is gitignored
  (`src/splashdown/recipe.py:248`), but nothing re-enforces the ignore on every run — verify
  the entry exists. (Matches README "Gotchas".)
- **Add-only, by hard rule.** A local variant whose name duplicates a recipe variant is an
  error in three places — at write time in `target_add`
  (`src/splashdown/devices.py:710`), at read time in `merged_targets`
  (`src/splashdown/recipe.py:287`), and `target_remove` refuses to delete a recipe variant
  (`src/splashdown/devices.py:739`). To change a shared target, edit `splashdown.toml`; to
  shadow one locally, pick a different variant name.
- **Default `remove` destroys state.** `splash target remove` is destructive by default — it
  deletes the sim/emulator and the registry row, not just the TOML table. Pass
  `--keep-instance` for a config-only edit.
- **The recipe-empty path is silent.** When the recipe can't be loaded, `_load_variant_spec`
  swallows the `ValueError` and proceeds with an empty recipe
  (`src/splashdown/commands.py:762`); a removal can still find and destroy the local
  variant's instance even if the recipe is malformed.

## Why

The committed recipe is the team/agent-shared contract; mutating it for a one-off repro
device would ripple to every teammate and every parallel-agent worktree. A gitignored,
add-only local layer lets a single checkout grow a throwaway target with zero blast radius,
which is exactly UC9's value: bug-repro isolation without touching version control.
