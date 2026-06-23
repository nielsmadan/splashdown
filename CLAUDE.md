# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`splashdown` is a Python CLI (`splash`) that pins per-checkout/per-worktree system resources — dev ports, env vars, iOS simulators, Android emulators — and coordinates them machine-wide so concurrent git checkouts of the same (or unrelated) projects never collide. Read `README.md` for the full user-facing model; it is the authoritative spec for behavior, the TOML schema, and CLI surface.

## Commands

Tasks run through `just` (see `Justfile`). CI runs `just check`.

```sh
just check          # everything CI runs: ruff check + ruff format --check + mypy + pytest
just test           # pytest -q
just lint           # ruff check
just fmt            # ruff format (writes)
just typecheck      # mypy (strict, src/splashdown only)
```

Run a single test: `uv run pytest tests/test_registry.py::test_two_checkouts_get_different_ports -q`
(or filter with `-k`). The suite is split into per-module files under `tests/` (one
`test_<module>.py` per `src/splashdown/` module); shared fixtures (`registry`, `checkout`)
and helpers live in `tests/conftest.py`.

Local install of the source as the real `splash` binary: `just install-local` / `just refresh-local` (reinstall after edits) / `just reset-local`.

Release: `just tag-release-patch|minor|major` bumps `version` in `pyproject.toml`, commits, tags, pushes; `release.yml` then builds, publishes a GitHub release, and updates the Homebrew tap formula. Do not tag releases unless explicitly asked.

## Architecture

All source is in `src/splashdown/`. `__init__.py` is a re-export hub: it defines the path/name constants (`RECIPE_NAME`, `LOCAL_NAME`, `ENV_FILE_NAME`, registry paths) and re-exports nearly every symbol so tests can reach internals as `sd.<name>` and monkeypatch them. Submodules import shared constants from the package root (`from . import RECIPE_NAME`), so import order in `__init__.py` matters (it is dependency-ordered to populate `PROFILES` before use).

The data flow, end to end:

1. **`scanner.py`** — `Scanner` walks the filesystem, detects the workspace (single/pnpm/yarn/npm/cargo/gradle), the shell loader (mise/direnv/devbox), and each app's framework, producing a `ProjectInventory` of `AppInventory` entries. Each app maps to a **Profile** by name. Collisions of resource names across apps are mangled here (e.g. `WEB_DEV_PORT_ADMIN`).
2. **`profiles.py`** — `Profile` = per-framework integration rules: which resources an app wants and which config files need patching. Also holds `SCAFFOLDS` (preset `splashdown.toml` templates for `splash init <preset>`). `PROFILES` is the registry populated at import.
3. **`recipe.py`** — parses `splashdown.toml` (`Recipe`) and `splashdown.local.toml` (`LocalConfig`), plus the template engine (`render_template`, `topo_sort`, `_make_scope`, scope functions like `slug`/`port_hash`/`hash`). `merged_targets` combines recipe + local target variants; `resolve_variant` picks one.
4. **`provisioning.py`** — `provision()` is the core sync: loads the recipe, resolves each resource (allocating ports via the registry, generating uuids, expanding templates in topo order), and `write_*` functions emit `splashdown.env` (or per-`writer` destinations: envfile/envrc/stdout/none).
5. **`registry.py`** — `Registry` is the machine-wide coordinator at `$XDG_STATE_HOME/splashdown/{ports,kv,devices}.tsv`. Flat TSV, `fcntl`-locked. Port allocation considers all other checkouts' pins plus live `bind()` probes; lazy GC drops entries whose checkout directory no longer exists. **TSV has no escaping** — `_tsv_field` rejects tab/newline/CR to prevent row forgery.
6. **`loaders.py`** — `Loader` subclasses (mise/direnv/devbox) idempotently wire `splashdown.env` to be sourced on `cd`. `LOADERS` registry.
7. **`devices.py`** — all sim/emulator/physical-device lifecycle. iOS via `xcrun simctl`/`devicectl`, Android via `avdmanager`/`sdkmanager`/`emulator`/`adb` from `$ANDROID_HOME`. `ensure_fresh_sim` implements the auto-recreate-on-newer-iOS behavior for `ios = "latest"` variants. `detect_framework` picks the launcher (flutter/react-native/expo/xcodebuild/gradle).
8. **`wiring.py`** — `splash doctor`. `WiringCheck`s detect framework config files that hardcode ports/override the env var, and (where safe) auto-patch them (RN metro config, RN package.json scripts, RN `ios/.xcode.env`, Vite config). Risky rewrites (Spring Boot) are report-only.
9. **`cli.py`** — argparse setup, `KNOWN_CMDS`, custom help formatter. `commands.py` holds the `cmd_*` handlers that orchestrate the modules above (init, sync, status, run/start/stop/destroy, target subcommands, gc), including git post-checkout hook installation that coexists with lefthook/husky.
10. **`completion.py`** — argcomplete completers; must be fail-silent (run on every `<Tab>`, never raise/print).

## Conventions

- **Python 3.13** runtime (`requires-python >=3.13`); ruff/mypy target 3.11. Two runtime dependencies: `argcomplete` (shell completion) and `tomlkit` (comment/unknown-key-preserving TOML *writing*). **`tomlio` (which imports tomlkit at its top level) is itself lazy-imported by its callers (`commands.py`/`devices.py`), and never re-exported from `__init__.py`** — so the git-hook hot path (`splash` → `provision()`/`status`, which only *reads* TOML via stdlib `tomllib`) never imports it. Reads stay on `tomllib`. Keep the hot path lightweight (note `__version__` and other costly lookups are lazy in `__init__.py`). Don't add more deps.
- **mypy strict** over `src/splashdown`. **ruff** with a broad rule set (`PL`, `B`, `S`, `SIM`, `SLF`, `RUF`, …); line length is formatter-enforced, not lint-enforced. `# noqa` codes in the tree are intentional — match the existing pattern rather than disabling rules globally.
- Shelling out to PATH tools (`xcrun`, `simctl`, `adb`, `git`) is by design — `S603`/`S607` are globally ignored.
- Tests are plain pytest, split into per-module files under `tests/`, fixture-driven (`registry`, `checkout`, `tmp_path`), heavy on monkeypatching the re-exported symbols. New behavior gets a test in the matching `tests/test_<module>.py` (shared fixtures/helpers in `tests/conftest.py`).
- The four project files splashdown manages: `splashdown.toml` (committed recipe), `splashdown.local.toml` (gitignored, per-checkout add-only target variants), `splashdown.env` (gitignored, generated — never hand-edit), and the loader config. splashdown owns `splashdown.env` wholesale.
