# Splashdown — per-checkout resource provisioner.
# `just` task runner: https://github.com/casey/just

# git-cliff for CHANGELOG.md generation; pinned, run via uvx (no separate install).
git_cliff := "git-cliff@2.13.1"

[private]
default:
    @just --list

# --- Dev ---

test:
    @uv run pytest tests/ -q

test-verbose:
    @uv run pytest tests/ -v

# Run tests with a coverage report (terminal + HTML in htmlcov/).
cov:
    @uv run pytest --cov --cov-report=term-missing --cov-report=html -q

# Lint with ruff.
lint:
    @uv run ruff check

# Format the codebase with ruff.
fmt:
    @uv run ruff format

# Type-check src/splashdown with mypy.
typecheck:
    @uv run mypy

# Run everything CI runs: lint, format check, type check, tests.
check:
    @uv run ruff check
    @uv run ruff format --check
    @uv run mypy
    @uv run pytest -q

# Install git hooks (lefthook).
hooks:
    @lefthook install

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
# chore/docs/deps are skipped. `tag-release` regenerates it automatically.

# Regenerate CHANGELOG.md from conventional commits.
changelog:
    @uvx {{git_cliff}} -o CHANGELOG.md

# --- Release ---
#
# Usage:
#   just tag-release-patch    # 0.1.0 -> 0.1.1
#   just tag-release-minor    # 0.1.0 -> 0.2.0
#   just tag-release-major    # 0.1.0 -> 1.0.0
#   just tag-release          # tag whatever's in pyproject.toml (if ahead of latest tag)
#
# Bumps `version = ...` in pyproject.toml, commits, tags, pushes.
# The release.yml workflow takes it from there: builds, creates GitHub release,
# updates Formula in homebrew-tap.

tag-release-patch:
    @just tag-release patch

tag-release-minor:
    @just tag-release minor

tag-release-major:
    @just tag-release major

tag-release bump="":
    #!/usr/bin/env bash
    set -euo pipefail

    LATEST_TAG=$(git tag --sort=-v:refname | head -1 | sed 's/^v//')
    PY_VERSION=$(grep '^version' pyproject.toml | head -1 | sed -E 's/^version *= *"([^"]+)".*/\1/')

    if [ -n "{{bump}}" ]; then
        BASE="${LATEST_TAG:-$PY_VERSION}"
        MAJOR=$(echo "$BASE" | cut -d. -f1)
        MINOR=$(echo "$BASE" | cut -d. -f2)
        PATCH=$(echo "$BASE" | cut -d. -f3)
        case "{{bump}}" in
            patch) PATCH=$((PATCH + 1)) ;;
            minor) MINOR=$((MINOR + 1)); PATCH=0 ;;
            major) MAJOR=$((MAJOR + 1)); MINOR=0; PATCH=0 ;;
            *) echo "Error: bump must be patch, minor, or major"; exit 1 ;;
        esac
        VERSION="$MAJOR.$MINOR.$PATCH"
        echo "Bumping version: ${LATEST_TAG:-(none)} -> $VERSION"
        sed -i '' "s/^version = \".*\"/version = \"$VERSION\"/" pyproject.toml
        uv lock  # keep uv.lock's splashdown version in sync (else the pre-commit hook regenerates it post-commit, leaving it uncommitted)
        uvx {{git_cliff}} --tag "v$VERSION" -o CHANGELOG.md
        git add pyproject.toml uv.lock CHANGELOG.md
        git commit -m "chore: bump version to $VERSION"
    else
        VERSION="$PY_VERSION"
        if [ -z "$VERSION" ]; then
            echo "Error: no version in pyproject.toml"; exit 1
        fi
        if [ -n "$LATEST_TAG" ]; then
            HIGHER=$(printf '%s\n' "$LATEST_TAG" "$VERSION" | sort -V | tail -1)
            if [ "$VERSION" = "$LATEST_TAG" ] || [ "$HIGHER" = "$LATEST_TAG" ]; then
                echo "Error: pyproject.toml version ($VERSION) is not newer than latest tag (v$LATEST_TAG)."
                echo "Run: just tag-release-patch / -minor / -major"
                exit 1
            fi
        fi

        uvx {{git_cliff}} --tag "v$VERSION" -o CHANGELOG.md
        if ! git diff --quiet -- CHANGELOG.md 2>/dev/null || ! git ls-files --error-unmatch CHANGELOG.md >/dev/null 2>&1; then
            git add CHANGELOG.md
            git commit -m "chore: update changelog for v$VERSION"
        fi
    fi

    echo "Tagging v$VERSION..."
    git tag -m "splashdown $VERSION" "v$VERSION"   # -m avoids the editor prompt for signed/annotated tags
    git push origin "$(git rev-parse --abbrev-ref HEAD)" "v$VERSION"
    echo "Tagged and pushed v$VERSION — release workflow triggered."

# git-cliff picks the next version from commits since the last tag (feat -> minor,
# fix -> patch, breaking -> minor on 0.x); tag-release-* forces a specific bump instead.
# Auto-bump pyproject + CHANGELOG.md, then commit, tag, and push a release.
release:
    #!/usr/bin/env bash
    set -euo pipefail

    NEXT=$(uvx {{git_cliff}} --bumped-version)          # e.g. v0.12.0
    VERSION="${NEXT#v}"
    LATEST_TAG=$(git tag --sort=-v:refname | grep -E '^v[0-9]' | head -1 || true)

    if [ "$NEXT" = "$LATEST_TAG" ]; then
        echo "Nothing to release: no feat/fix commits since ${LATEST_TAG:-the last tag}."
        exit 1
    fi
    echo "Auto-release: ${LATEST_TAG:-(none)} -> $NEXT"

    sed -i '' "s/^version = \".*\"/version = \"$VERSION\"/" pyproject.toml
    uv lock  # keep uv.lock in sync (see Dev tooling -> Release flow in AGENTS.md)
    uvx {{git_cliff}} --tag "$NEXT" -o CHANGELOG.md
    git add pyproject.toml uv.lock CHANGELOG.md
    git commit -m "chore: bump version to $VERSION"

    git tag -m "splashdown $VERSION" "$NEXT"   # -m avoids the editor prompt for signed/annotated tags
    git push origin "$(git rev-parse --abbrev-ref HEAD)" "$NEXT"
    echo "Tagged and pushed $NEXT — release workflow triggered."
