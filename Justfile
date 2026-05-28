# Splashdown — per-checkout resource provisioner.
# `just` task runner: https://github.com/casey/just

[private]
default:
    @just --list

# --- Dev ---

test:
    @python3 -m pytest tests/ -q

test-verbose:
    @python3 -m pytest tests/ -v

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
    @rm -rf dist build *.egg-info .pytest_cache

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
        git add pyproject.toml
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
    fi

    echo "Tagging v$VERSION..."
    git tag "v$VERSION"
    git push origin "$(git rev-parse --abbrev-ref HEAD)" "v$VERSION"
    echo "Tagged and pushed v$VERSION — release workflow triggered."
