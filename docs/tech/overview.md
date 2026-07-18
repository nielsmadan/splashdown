# Technical / Architecture

How splashdown is built — module internals, the data flow, and the cross-cutting patterns
that constrain the whole package. This is the implementation layer; for what each feature
does for the user, see [`../features/overview.md`](../features/overview.md). `CLAUDE.md` (repo root)
carries the short contributor summary; these docs go deeper, per subsystem.

## Module docs
- [registry.md](registry.md) — `registry.py`: the machine-wide `fcntl`-locked TSV coordinator
  (ports/kv/devices), port allocation, lazy GC, row-forgery prevention.
- [provisioning.md](provisioning.md) — `provisioning.py`: `provision()` resource resolution and
  the output writers.
- [recipe-and-templates.md](recipe-and-templates.md) — `recipe.py` + `tomlio.py`: recipe/local
  parsing, the AST-sandbox template engine, topo sort, and comment-preserving TOML writes.
- [scanning-and-extension.md](scanning-and-extension.md) — `scanner.py` + `profiles.py` +
  `loaders.py`: project detection and the Profile/Loader extension points.
- [devices.md](devices.md) — `devices.py`: sim/emulator/physical-device lifecycle and framework
  launchers.
- [wiring.md](wiring.md) — `wiring.py`: the `splash doctor` framework-wiring checks and autopatch.
- [cli-and-commands.md](cli-and-commands.md) — `cli.py` + `commands.py` + `completion.py`: entry,
  parse, dispatch, the `cmd_*` handlers, and git-hook installation.

## Data flow (end to end)
`scanner.py` (detect → `ProjectInventory`) → `profiles.py` (per-framework rules + `PROFILES`) →
`recipe.py` (parse + template engine) → `provisioning.py` (`provision()` resolves resources) →
`registry.py` (machine-wide allocation). Alongside: `loaders.py` wires the env loader, `devices.py`
runs sims/emulators, `wiring.py` patches framework configs, and `cli.py`/`commands.py` are the
entry + orchestration. `src/splashdown/__init__.py` is the seam that ties them together.

## Cross-cutting patterns (read before editing any module)
- **Re-export hub.** `src/splashdown/__init__.py` defines the path/name constants
  (`RECIPE_NAME`, `LOCAL_NAME`, `ENV_FILE_NAME`, registry paths) and re-exports nearly every
  symbol — including private `_`-prefixed helpers — so tests reach internals as `sd.<name>` and
  monkeypatch them. Consequence: renaming a private helper is a breaking change to the hub list
  and the test suite.
- **Import order matters.** Submodules import shared constants from the package root
  (`from . import RECIPE_NAME`); `__init__.py` is ordered so `PROFILES` is populated before use.
  Several real backward edges (registry↔devices↔recipe, loaders→commands, wiring→scanner/commands)
  are broken with **in-function lazy imports** — moving one to module scope risks an `ImportError`.
- **Hot-path discipline.** Bare `splash` (the post-checkout hook) runs `provision()`/`status`,
  which only *read* TOML via stdlib `tomllib`. `tomlkit` (TOML *writing*) is imported at
  `tomlio.py` top level, but `tomlio` itself is lazy-imported by its callers and never re-exported,
  so the read path never loads it. `__version__` and other costly lookups are lazy in `__init__.py`.
  Keep the two-dependency floor (`argcomplete`, `tomlkit`) and the read path light.
- **TSV registry contract.** All machine-wide state is flat, `fcntl`-locked TSV with **no
  escaping** — `_tsv_field` rejects tab/newline/CR to prevent row forgery. See registry.md.

## Why this shape
Python for zero install friction (brew vendors `python@3.13`); a flat TSV + `fcntl` instead of a
DB so the registry is dependency-free and inspectable; an AST-walking template sandbox (not `eval`)
because recipes run automatically from the post-checkout hook on potentially untrusted checkouts.
