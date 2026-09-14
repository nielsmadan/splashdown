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
just format         # ruff format (writes)
just setup          # install dependencies and hooks, then verify the checkout
just doctor         # verify tools and hook installation
just coverage       # pytest with coverage reports
just typecheck      # mypy --strict over src/splashdown
just build-docs     # strict user-docs build into ./site
just serve-docs     # serve the docs site
```

Run one test with `uv run pytest tests/test_registry.py::test_name -q`, or use `-k`. Tests are
split by source module under `tests/`; shared fixtures and helpers live in `tests/conftest.py`.

Use `just install` to install or replace the current-source snapshot as the local `splash`
command. `just install-editable` links it to the checkout so source edits take effect immediately.
`just uninstall` removes the CLI installation and preserves checkout configuration and registry data.

## Before declaring done

Run `just check`, not only pytest. For dependency or release changes, also reproduce the release
test install in a clean virtual environment with `pip install build pytest .`.

Coverage uses the `fail_under = 80` value in `pyproject.toml` and is enforced by CI and the
pre-push hook. `just check` intentionally stays fast and does not collect coverage.

This repository's hooks are defined in `lefthook.yml` and installed with `just setup`. It does not
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

`tomlio.py` is the sole top-level `tomlkit` importer. Its callers (`commands.py`, `targets.py`,
and `loaders.py`) import it lazily, and `__init__.py` does not re-export it. Reads use
stdlib `tomllib`, keeping bare `splash` and the post-checkout hook path light.
`MiseLoader.owns_config` is a read that reuses `remove_mise_file_directive_text` because
ownership is a comment- and whitespace-aware question `tomllib` cannot answer.

## Load-bearing constraints

- Registry TSV has no escaping. `_tsv_field` must reject tabs and every character some reader
  treats as a line break: `\n`, `\r`, `\v`, `\f`, the information separators, NEL, and the
  Unicode line and paragraph separators. Reads go through `split_lines`, which breaks on the
  file's own line ending only, so the write side is what keeps a field from meaning two things.
  Registry writes use stable `fcntl` sidecars plus same-directory atomic replacement.
- Registry `_lock` is non-reentrant. Call unlocked helpers while holding a file lock, and keep the
  checkout `operation_lock` outermost around registry changes, output writes, target edits, and
  device lifecycle side effects. `run` releases it before the app process; setup runs afterward.
- Provisioning destinations never follow checkout-controlled links. Generated env files use the
  shared no-symlink, regular-file-only atomic writer; local skeleton creation is create-only and
  rejects symlinks and non-regular entries.
- Wiring checks and hook-configuration editors must not return `ok` for input they did not parse.
  Strip comments, recognize the relevant value slot, and report unrecognized shapes as a problem.
  Use `yamltext.py`'s `_yaml_key_regions` for YAML value regions instead of line-only regular
  expressions. There is no runtime YAML parser, so an editor that cannot place a shape exactly
  must preserve the file and emit manual instructions; `_pre_commit_analysis` additionally
  re-reads the document it built and abandons the edit unless its own hook parses back out.
- Hook-manager configuration paths follow each tool's own first-match-wins lookup. Never create a
  file that outranks the configuration a project already uses (`prek.toml` over an existing
  `.pre-commit-config.yaml`, a `.simple-git-hooks.json` over a declared `package.json` block).
- `.NET` `launchSettings.json` may contain a UTF-8 BOM and CRLF. Read and write it through
  `_read_launch_settings` so both survive.
- Env destinations are co-owned, so everything outside splashdown's keys survives verbatim.
  `_rewrite` replaces a managed key where it already stands and keeps trailing blank lines. An
  unterminated quoted value is an error whoever opened it.
- `constants.split_lines` is the only line splitter for file content, registry rows included.
  Never reach for `str.splitlines`, which also breaks on vertical tab, form feed, the information
  separators, NEL and the Unicode separators: an editor that rewrites from it promotes any of
  those to a real line break and splices its own block through the value carrying one. Only
  subprocess output and in-tree constant strings may use it.
- A project-owned JSON file is edited by splicing the member splashdown owns, through
  `jsontext.py`. Never render a whole document with `json.dumps` except one splashdown creates;
  that reflows every nested literal the project hand-formatted and is not reverted on `deinit`.
- A project file splashdown declines to edit is reported as
  `warning: left <name> alone: <cause>` at exit 0, with the real cause (`safe_files.UneditablePath`
  carries it, `refusal_reason` unwraps it). `error:` plus a non-zero exit is for a refusal that
  stops the command.
- The recipe cannot say which keys to remove: a deleted resource is absent from `resolved`. The
  checkout's registry rows are that record, passed to `write_outputs` and
  `clear_writer_destinations` as `known_keys`, and `cmd_deinit` reads them before
  `registry.release`.
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
- Splashdown manages its declared keys inside every writer destination and never owns one
  wholesale, including `splashdown.env`. `[project] env_file` selects the default destination.
  `splashdown.toml` is committed; `splashdown.local.toml` and generated env output are gitignored.
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

Cross-cutting rationale lives in [docs/decisions/](docs/decisions/overview.md). Accepted ADRs are
append-only; supersede them with a new record rather than rewriting their history.

The user and builder audiences stay separate. The public site publishes only `docs/user/` via
`mkdocs.yml`; some user/builder duplication is intentional, but duplicate builder explanations
should have one canonical owner and links elsewhere. User prose in `README.md` and `docs/user/`
avoids em dashes and semicolons; builder docs retain their existing style.

After changing behavior, update the relevant docs and run `just build-docs`. Use `doc --update` to
refresh documentation and `doc --review` for a whole-repository audit.
