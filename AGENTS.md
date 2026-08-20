# AGENTS.md

This file is the single source of truth for coding-agent guidance in this repository.
`CLAUDE.md` imports it.

## What this is

Splashdown is a Python CLI (`splash`) that pins ports, environment values, iOS simulators, and
Android emulators per checkout and coordinates them machine-wide. `README.md` is the authoritative
user-facing description of the behavior, TOML schema, and CLI surface.

## Commands

Tasks run through `just`; CI runs `just check`.

```sh
just check          # ruff + format check + import cycles + mypy + pytest
just test           # pytest -q
just lint           # ruff check
just fmt            # ruff format (writes)
just typecheck      # mypy --strict over src/splashdown
just docs-build     # strict user-docs build into ./site
just docs           # serve the docs site
```

Run one test with `uv run pytest tests/test_registry.py::test_name -q`, or use `-k`. Tests are
split by source module under `tests/`; shared fixtures and helpers live in `tests/conftest.py`.

Use `just install-local`, `just refresh-local`, and `just reset-local` for the real local `splash`
binary. `just refresh-local` forces a no-cache reinstall so uv cannot reuse an old wheel.

## Before declaring done

Run `just check`, not only pytest. For dependency or release changes, also reproduce the release
test install in a clean virtual environment with `pip install build pytest .`.

Coverage uses the `fail_under = 80` value in `pyproject.toml` and is enforced by CI and the
pre-push hook. `just check` intentionally stays fast and does not collect coverage.

This repository's hooks are defined in `lefthook.yml` and installed with `just hooks`. It does not
dogfood Splashdown provisioning: there is no repository `splashdown.toml` or managed
post-checkout hook.

## Architecture

Start with [docs/tech/overview.md](docs/tech/overview.md). It is the current module map and links
to each subsystem's implementation contract. Do not duplicate that catalog here.

The package has an explicit acyclic import policy:

- Internal modules import dependency-free seams such as `constants.py`, `catalog.py`,
  `inventory.py`, and `errors.py`; they never import the package root.
- `__init__.py` is a public re-export hub, including private helpers used by tests. It imports the
  `profiles.py` facade before consumers that need the ordered profile catalog, but no submodule
  depends back on it.
- `profiles.py` assembles implementations from `profiles_web.py`, `profiles_server.py`,
  `profiles_mobile.py`, and `profiles_compose.py`. Launch selection lives in `launching.py`;
  target commands live in `target_commands.py`; status gathering and rendering live in
  `status.py` and `cli_output.py`.
- Pylint's `cyclic-import` check analyzes the package in `just check`. Keep shared exception and
  capability seams dependency-free rather than hiding cycles behind lazy imports.

`tomlio.py` is the sole top-level `tomlkit` importer. Its write-path callers (`commands.py`,
`targets.py`, and `hooks.py`) import it lazily, and `__init__.py` does not re-export it. Reads use
stdlib `tomllib`, keeping bare `splash` and the post-checkout hook path light.

## Load-bearing constraints

- Registry TSV has no escaping. `_tsv_field` must reject tabs, newlines, and carriage returns.
  Registry writes use stable `fcntl` sidecars plus same-directory atomic replacement.
- Registry `_lock` is non-reentrant. Call unlocked helpers while holding a file lock, and keep the
  checkout `operation_lock` outermost around registry changes, output writes, target edits, and
  device lifecycle side effects. `run` releases it before the app process; setup runs afterward.
- Provisioning destinations never follow checkout-controlled links. Generated env files use the
  shared no-symlink, regular-file-only atomic writer; local skeleton creation is create-only and
  rejects symlinks and non-regular entries.
- Wiring checks must not return `ok` for input they did not parse. Strip comments, recognize the
  relevant value slot, and report unrecognized shapes as a problem. Use `_yaml_key_regions` for
  YAML value regions instead of line-only regular expressions.
- `.NET` `launchSettings.json` may contain a UTF-8 BOM and CRLF. Read and write it through
  `_read_launch_settings` so both survive.
- Physical iOS discovery uses `pairingState == "paired"`; a wireless device normally has a
  disconnected tunnel until launch.
- User-facing URLs printed by the CLI are test contracts. Update their assertions with any URL or
  wording change.

## Conventions

- Runtime is Python 3.13; ruff and mypy target 3.11. Strict mypy applies to `src/splashdown`.
- Runtime dependencies are deliberately limited to `argcomplete` and `tomlkit`. Do not add a
  dependency without an explicit evaluation of its supply-chain and Homebrew resource cost.
- Ruff owns lint and formatting. After `ruff check --fix`, run `ruff format`; do not broadly
  disable rules to avoid a local fix.
- Shelling out to PATH tools such as `xcrun`, `adb`, and `git` is intentional; `S603` and `S607`
  are globally ignored.
- New behavior gets tests in the matching `tests/test_<module>.py`.
- Splashdown owns `splashdown.env` wholesale. `splashdown.toml` is committed;
  `splashdown.local.toml` and generated env output are gitignored.
- This is a pre-release, single-user project. Make requested format and CLI changes directly;
  do not add compatibility readers or migration branches unless explicitly asked.
- Be monorepo-honest, not monorepo-smart. When scanning cannot produce a correct multi-app
  recipe, emit the safe structure-only form and direct the user to manual configuration rather
  than guessing.

Release and Homebrew maintenance live in [docs/tech/release.md](docs/tech/release.md). Never tag a
release unless explicitly asked, never hand-edit `CHANGELOG.md`, and keep release version, lock,
and tag ordering as documented there.

## Documentation

Project docs start at [docs/overview.md](docs/overview.md). Feature behavior lives in
`docs/features/`, implementation details in `docs/tech/`, product material in `docs/product/`, and
user guides in `docs/user/`.

The user and builder audiences stay separate. The public site publishes only `docs/user/` via
`mkdocs.yml`; some user/builder duplication is intentional, but duplicate builder explanations
should have one canonical owner and links elsewhere. User prose in `README.md` and `docs/user/`
avoids em dashes and semicolons; builder docs retain their existing style.

After changing behavior, update the relevant docs and run `just docs-build`. Use `doc --update` to
refresh documentation and `doc --review` for a whole-repository audit.
