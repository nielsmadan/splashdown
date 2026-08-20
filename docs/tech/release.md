# Release and distribution

Splashdown publishes Python artifacts through GitHub Releases and distributes the CLI from the
`nielsmadan/homebrew-tap` Homebrew tap. The release recipes in `Justfile`, the GitHub workflow in
`.github/workflows/release.yml`, and the remote tap formula form one pipeline.

## Release commands

`just release` is the normal path. git-cliff derives the next version from commits since the last
tag: `feat` produces a minor release, `fix` a patch release, and a breaking change a minor release
while the project is on `0.x`. `just tag-release-patch`, `just tag-release-minor`, and
`just tag-release-major` force a bump when the derived version is not appropriate.

The recipe updates `pyproject.toml`, regenerates `uv.lock` and `CHANGELOG.md`, commits those files,
and creates the release tag. The GitHub workflow then builds the package, publishes a GitHub
release, and updates the Homebrew formula. Do not run a release recipe unless a release was
explicitly requested.

## Version and lock ordering

The version update must run `uv lock` before staging `pyproject.toml` and `uv.lock`. A commit fires
the pre-commit hook, whose `uv run` can notice a changed project version and regenerate the lock as
a side effect. If the lock was not generated and staged first, the tag can carry a stale
`uv.lock`.

A stale lock at an already-published tag does not change the published artifacts: the release
workflow builds with pip and does not consume `uv.lock`. Fix it with a follow-up lockfile commit;
never replace a public tag for that reason.

## Changelog and tags

`just changelog` regenerates `CHANGELOG.md` with the git-cliff version pinned in `Justfile`.
`CHANGELOG.md` is generated output and must not be edited by hand. git-cliff includes `feat` and
`fix` changes and omits internal `chore`, docs, CI, dependency, and merge commits. Mark breaking
changes with `feat!:` / `fix!:` or a `BREAKING CHANGE:` footer so they appear in the breaking
changes section.

Release tags pass `-m` intentionally. The maintainer has `tag.gpgsign = true`; without an explicit
message, an annotated tag opens an editor and stalls the non-interactive recipe.

## Clean-environment verification

For dependency or release-workflow changes, reproduce the workflow's test installation in a clean
virtual environment with `pip install build pytest .`. The trailing project argument matters: it
installs Splashdown and its runtime dependencies before running tests.

The workflow pins `astral-sh/setup-uv` to an exact v8 release because v8 has no moving `@v8` tag.
Keep it on an exact version.

## Homebrew tap

The canonical formula is `Formula/splashdown.rb` in
[`nielsmadan/homebrew-tap`](https://github.com/nielsmadan/homebrew-tap). The tap also contains the
Juggler cask; no reference formula is kept in this repository. The `HOMEBREW_TAP_TOKEN` GitHub
secret must have write access to the tap repository.

The remote formula must exist before the first release. The workflow clones the tap, rewrites the
formula, and pushes it; it cannot create a missing formula from a local reference copy.

The formula has a top-level source `sha256` and separate hashes inside Python dependency resource
blocks. The source rewrite must stay anchored to the two-space-indented top-level line:

```sh
sed -i "s|^  sha256 \"[^\"]*\"|  sha256 \"${TARBALL_SHA}\"|" Formula/splashdown.rb
```

An unanchored global replacement corrupts the dependency hashes.

Homebrew manages the Python runtime and vendored dependency resources for users. When Homebrew
retires the current Python major, update the formula's `python@3.X` dependency and rebuild its
resources.
