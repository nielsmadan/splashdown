# Release and distribution

Splashdown publishes Python artifacts through GitHub Releases and distributes the CLI from the
`nielsmadan/homebrew-tap` Homebrew tap. The release recipes in `Justfile`, the GitHub workflow in
`.github/workflows/release.yml`, the helper scripts in `scripts/`, and the formula template in
`packaging/homebrew/splashdown.rb.tmpl` form one pipeline.

## Release commands

Releases use [curtains](https://github.com/nielsmadan/curtaincall); install it first with
`brew install nielsmadan/tap/curtaincall`. `just release` forwards its arguments to curtains, which
reads the policy in `scripts/release.json`. It derives a proposed version with the pinned git-cliff
configuration: `feat` produces a minor release, `fix` a patch release, and a breaking change a
minor release while on `0.x`. It runs `just check`, `just coverage`, and `just build`, then shows
the version, affected files, push destination, and publication steps. Enter `y` to confirm, enter a
different version or bump and then confirm, or press Enter to cancel. Release edits begin only after
confirmation.

```sh
just release
just release minor
just release 1.4.0
just release --dry-run
just release patch --yes
```

`patch`, `minor`, `major`, and exact versions all use the same confirmation flow. `--dry-run`
inspects local and remote Git state and calculates a proposal without publishing. It skips the
tool check and every policy command, including the quality checks, build, and git-cliff suggestion,
so its proposal is the conventional-commit estimate. `--yes` explicitly bypasses the prompt;
unattended invocations otherwise fail. A release with no automatic bump requires an explicit
version or bump.

Run from a clean `main` checkout with complete history and Git push access to the repository.
Every historical `vMAJOR.MINOR.PATCH` tag must exist both locally and on origin and peel to the
same commit, not only the latest. Curtains never fetches: the fetched `origin/main` must be an
ancestor of `HEAD`, so fetch first when origin may have moved. Existing local commits are included
in the push. A real release requires Git, Python 3.13+, Just, and uv/uvx. `scripts/release.json`
declares checks and preparation steps; curtains uses Git for remote inspection, tagging, and
pushing.

The recipe updates `pyproject.toml`, regenerates `uv.lock` and `CHANGELOG.md`, commits those files,
and atomically pushes `main` with its annotated release tag. The GitHub workflow then verifies the
tag, builds and tests the package, publishes a GitHub release, and updates the Homebrew formula.
The command finishes after the push and prints workflow and release links. Publication runs
asynchronously; local success confirms the Git push, and the linked workflow reports publication
success or failure. CI uses `gh` to create the GitHub release record and upload artifacts, which
Git cannot do. Do not run a release unless explicitly requested.

Failed preparation or push leaves local changes/commits/tags available for inspection. Failed
publication leaves the remote tag in place; inspect the linked workflow, address the cause, and
retry by running the `Release` workflow manually from `main` with the existing tag. The publish
job is idempotent: it recreates only missing or undrafted release state and never rewrites a
published tag or asset.

## Workflow structure

The workflow has two jobs. `verify` validates that the tag is annotated, identifies the pushed
commit, and is contained in `origin/main`; checks out the exact tagged source; runs the full
quality gate (Ruff, Pylint import cycles, mypy, pytest with branch coverage); builds the sdist and
then the wheel from that sdist; installs both artifacts into isolated virtual environments and
checks `splash --version` in each; verifies the locked Homebrew resources and generated formula;
and saves a release bundle (artifacts, source checksum, formula, and git-cliff notes) as a workflow
artifact. `publish` revalidates the tag against the verified commit,
creates or reconciles the GitHub release through `scripts/release_workflow.py publish`, and
validates and pushes the bundled Homebrew formula.

Every action in the repository's workflows is pinned to a full commit SHA. Keep pins exact when
updating.

## Version and lock ordering

The version update must run `uv lock` before staging `pyproject.toml` and `uv.lock`. A commit fires
the pre-commit hook, whose `uv run` can notice a changed project version and regenerate the lock as
a side effect. If the lock was not generated and staged first, the tag can carry a stale
`uv.lock`.

The release workflow uses frozen uv dependencies for verification and `uv.lock` to generate Homebrew
runtime resources. A stale runtime graph or project version fails resource generation. Fix a stale
lock with a new commit and release; never replace a published tag.

## Changelog and tags

`just changelog` regenerates `CHANGELOG.md` with the git-cliff version pinned in `Justfile`.
`CHANGELOG.md` is generated output and must not be edited by hand. git-cliff includes `feat` and
`fix` changes and omits internal `chore`, docs, CI, dependency, and merge commits. Mark breaking
changes with `feat!:` / `fix!:` or a `BREAKING CHANGE:` footer so they appear in the breaking
changes section. `cliff.toml` only treats strict `vMAJOR.MINOR.PATCH` tags as releases, matching
the workflow's tag validation.

Release tags pass `-m` intentionally. The maintainer has `tag.gpgsign = true`; without an explicit
message, an annotated tag opens an editor and stalls the non-interactive recipe.

## Clean-environment verification

For dependency or release-workflow changes, verify a test installation in a clean
virtual environment with `pip install build pytest pytest-cov pyyaml .`. PyYAML validates generated hook
configuration in tests. The trailing project argument matters: it installs Splashdown and its
runtime dependencies before running `python -m pytest tests/ scripts/ -q`.

Also reproduce the workflow's isolated artifact installation:

```sh
just build
uv venv /tmp/splash-verify
uv pip install --python /tmp/splash-verify/bin/python dist/splashdown-*.whl
/tmp/splash-verify/bin/splash --version
```

Repeat with the `.tar.gz` sdist to cover both artifacts.

## Homebrew tap

The canonical formula is `Formula/splashdown.rb` in
[`nielsmadan/homebrew-tap`](https://github.com/nielsmadan/homebrew-tap), rendered from the in-repo
template `packaging/homebrew/splashdown.rb.tmpl`. The tap also contains the Juggler cask. The
`HOMEBREW_TAP_TOKEN` GitHub secret must have write access to the tap repository.

`scripts/homebrew_resources.py` generates the formula from the release version, downloaded
Splashdown source checksum, and the runtime closure in `uv.lock`. It checks the project dependency
names and constraints against the lock and requires registry sources. Development and documentation
packages are excluded. Conditional or ambiguous runtime graphs fail until an explicit Homebrew
policy is implemented.

Registry source archives must match their locked SHA256. Flyrail is distributed as `pyflyrail`
on PyPI, with the `flyrail` import package. Its published source archive installs from the archive
root, like the other runtime resources.

Before publication, the workflow actually stages every verified resource and installs its source
into a new environment using `--no-deps --no-binary=:all: --ignore-installed --no-compile`.
Build isolation remains enabled, matching Homebrew's Python helper defaults. It then installs
Splashdown without dependency resolution, runs `pip check`, and exercises CLI help. The generated
formula explicitly stages each resource at its project root, installs the main
project, and generates shell completions. The tap update copies the complete generated formula,
so an old dependency resource cannot survive a main-package version update.

The template contains placeholders for the version, source checksum, resource blocks, and resource
installation steps. The generator fills resource data from `uv.lock`; do not pin dependencies a
second time in the template. `scripts/render_formula.py` validates the bundled formula's version
and source checksum, refuses an unrecognized existing source URL, and preserves an already newer
formula. The workflow runs `ruby -c` before publication and again before committing to the tap.

For local verification, `--artifact-map` accepts a JSON object mapping public resource URLs to
`{"path": "...", "sha256": "..."}` transport inputs. Each input is checked against its supplied
checksum and every input also retains its locked checksum check. `--verify-install` names a
new disposable environment. Keep these maps, environments, downloaded sources, and any generated
formula in ignored temporary storage. This mode does not publish or modify the tap.

Homebrew manages the Python runtime and vendored dependency resources for users. When Homebrew
retires the current Python major, update the template's `python@3.X` dependency and rebuild its
resources. Python wheel and sdist metadata use the `pyflyrail` version requirement, so pip installs
resolve Flyrail from PyPI.
