# Splashdown — per-checkout resource provisioner.
# `just` task runner: https://github.com/casey/just

# git-cliff for CHANGELOG.md generation; pinned, run via uvx (no separate install).
git_cliff := "git-cliff@2.13.1"

[private]
default:
    @just --list

# Prepare this checkout for work: dependencies, hooks, then verify.
setup:
    @uv sync
    @lefthook install
    @just doctor

# Verify the tools and checkout state this repo needs.
doctor:
    #!/usr/bin/env bash
    set -uo pipefail
    fail=0
    need() {
        if command -v "$1" >/dev/null 2>&1; then
            printf '  ok       %s\n' "$1"
        else
            printf '  MISSING  %-12s install: %s\n' "$1" "$2"; fail=1
        fi
    }
    need uv "https://docs.astral.sh/uv/getting-started/installation/"
    need lefthook "brew install lefthook"
    if [ -f "$(git rev-parse --git-path hooks/pre-commit)" ]; then
        printf '  ok       git hooks\n'
    else
        printf '  MISSING  %-12s run: just setup\n' 'git hooks'; fail=1
    fi
    [ "$fail" -eq 0 ] && printf 'Everything in place.\n'
    exit $fail

# --- Dev ---

test:
    @uv run pytest tests/ -q

test-verbose:
    @uv run pytest tests/ -v

# Networked compatibility canary: latest Vite + real init + worktree hook + two servers.
# Not part of `check`; use SPLASH_SMOKE_KEEP=1 to retain its temporary workspace and logs.
smoke-first-use:
    @bash tests/smoke/first-use-vite.sh

# Run tests with a coverage report (terminal + HTML in htmlcov/).
coverage:
    @uv run pytest --cov --cov-report=term-missing --cov-report=html -q

# Lint with ruff.
lint:
    @uv run ruff check

# Check the package for circular imports with Pylint.
lint-imports:
    @uv run pylint src/splashdown

# Format the codebase with ruff.
format:
    @uv run ruff format

# Type-check src/splashdown with mypy.
typecheck:
    @uv run mypy

# Run everything CI runs: lint, format check, type check, tests.
check:
    @uv run ruff check
    @uv run ruff format --check
    @uv run pylint src/splashdown
    @uv run mypy
    @uv run pytest -q

# Build sdist + wheel into ./dist
build:
    @rm -rf dist build *.egg-info
    @python3 -m build

# Install the current source as the global `splash` binary via uv.
install-local:
    @uv tool install .
    @echo "Installed: $(which splash)"

# Reinstall current source over the existing splash binary via uv. Use to test
# local changes before tagging a release.
refresh-local:
    @uv tool install --reinstall --force .
    @echo "Refreshed: $(which splash)"

# Remove the locally-installed splash binary.
reset-local:
    @uv tool uninstall splashdown

clean:
    @rm -rf dist build *.egg-info .pytest_cache htmlcov .coverage coverage.xml site

# --- Docs ---
#
# User docs site (Zensical), published to splashdown.dev. Config in mkdocs.yml,
# pages in docs/user/. Zensical is a dev-only dependency (the `docs` group).

# Build the docs site into ./site (strict: fails on broken links/nav).
docs-build:
    @uv run --group docs zensical build -f mkdocs.yml --strict

# Serve the docs locally with live reload (http://localhost:8000).
docs:
    @uv run --group docs zensical serve -f mkdocs.yml

# Needs `vhs` (brew install vhs) + `splash` on PATH; runs in a throwaway temp
# project with an isolated registry, so your real state is untouched.
# Record docs/demo.gif from docs/demo.tape, then overlay captions + arrows
# (pillow/numpy pulled ephemerally via uv, so no project dep is added).
demo:
    @command -v vhs >/dev/null || { echo "install vhs first: brew install vhs"; exit 1; }
    @vhs docs/demo.tape
    @uv run --with pillow --with numpy python docs/annotate_demo.py
    @echo "→ wrote docs/user/assets/demo.gif"

# --- Changelog ---
#
# CHANGELOG.md is generated from feat/fix commits by git-cliff (config in cliff.toml);
# chore/docs/deps are skipped. `release` regenerates it automatically.

# Regenerate CHANGELOG.md from conventional commits.
changelog:
    @uvx {{git_cliff}} -o CHANGELOG.md

[positional-arguments]
release *args:
    python3 scripts/release.py "$@"
