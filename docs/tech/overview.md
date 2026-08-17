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
- [recipe-and-templates.md](recipe-and-templates.md) — `recipe.py` + `tomlio.py`: recipe/local/global
  parsing, the AST-sandbox template engine, topo sort, and comment-preserving TOML writes.
- [scanning-and-extension.md](scanning-and-extension.md) — `scanner.py` + `profiles.py` +
  `agentdocs.py` + `loaders.py`: project detection, extension points, and generated agent
  guidance.
- [devices.md](devices.md) — `devices.py`: sim/emulator/physical-device lifecycle and framework
  device operations; `launching.py` owns framework selection and launch dispatch.
- [wiring.md](wiring.md) — `wiring.py`: framework-wiring checks and autopatches;
  `doctor.py` owns check selection, execution, and rendering.
- [cli-and-commands.md](cli-and-commands.md) — `cli.py` + `commands.py` + `hooks.py` +
  `completion.py`: entry, parse, dispatch, the `cmd_*` handlers, and git-hook installation.
- [platform-capabilities.md](platform-capabilities.md) — host support, capability errors, and the
  audited subprocess-failure contract.
- [bootstrap.md](bootstrap.md) — clone trust, worktree completion, lifecycle locking, and the
  creation-only post-checkout execution path.

## Data flow (end to end)
`scanner.py` (detect → `ProjectInventory`) → `profiles.py` (per-framework rules registered in
`catalog.py`) →
`recipe.py` (parse + template engine) → `provisioning.py` (`provision()` resolves resources) →
`registry.py` (machine-wide allocation). Alongside: `loaders.py` wires the env loader, `devices.py`
runs sims/emulators, `launching.py` dispatches app launchers, `wiring.py` defines framework checks,
`doctor.py` orchestrates them, and `agentdocs.py` derives and
synchronizes sentinel-managed `AGENTS.md`/`CLAUDE.md` guidance during init, rescan, and deinit.
`hooks.py` owns git-hook, gitignore, and mise-directive wiring and is consumed directly by
`loaders.py`, `wiring.py`, and `commands.py`. `cli.py`/`commands.py` are the entry + orchestration.
`bootstrap.py` owns Git-scoped trust/completion state and coordinates its lifecycle locks.
`src/splashdown/__init__.py` is the seam that ties them together.

## Cross-cutting patterns (read before editing any module)
- **Re-export hub.** `src/splashdown/__init__.py` re-exports the path/name constants from
  `constants.py` (`RECIPE_NAME`, `LOCAL_NAME`, `ENV_FILE_NAME`, registry paths) and nearly every
  symbol — including private `_`-prefixed helpers — so tests reach internals as `sd.<name>` and
  monkeypatch them. Consequence: renaming a private helper is a breaking change to the hub list
  and the test suite.
- **Acyclic internal imports.** Internal modules never import the package root. Dependency-free
  `constants.py`, `catalog.py`, and `inventory.py` provide shared seams; `launching.py` sits above
  runners/profiles, and `doctor.py` sits above profiles/wiring. Pylint's `cyclic-import` checker
  analyzes the whole package and fails CI if a backward edge is introduced. `__init__.py` still
  imports `profiles.py` first so the ordered profile catalog is populated before public consumers
  are re-exported, but no submodule depends back on it.
- **Hot-path discipline.** Bare `splash` and the post-checkout event handler use the read and
  provision path, which only *reads* TOML via stdlib `tomllib`. `tomlkit` (TOML *writing*) is imported at
  `tomlio.py` top level, but `tomlio` itself is lazy-imported by its callers and never re-exported,
  so the read path never loads it. `__version__` and other costly lookups are lazy in `__init__.py`.
  Keep the two-dependency floor (`argcomplete`, `tomlkit`) and the read path light.
- **TSV registry contract.** All machine-wide state is flat TSV, protected by stable sidecar
  `fcntl` locks and committed with same-directory atomic replacement. Checkout-scoped operation
  locks use a bounded hash-shard set to serialize registry changes, output-file writes, target
  edits, and device lifecycle side effects. The format has **no escaping** — `_tsv_field` rejects
  tab/newline/CR to prevent row forgery. See registry.md.

## Why this shape
Python for zero install friction (brew vendors `python@3.13`); a flat TSV + `fcntl` instead of a
DB so the registry is dependency-free and inspectable; an AST-walking template sandbox (not `eval`)
because clone trust covers recipes from future refs processed by the post-checkout hook.
