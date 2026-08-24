# Contributing to splashdown

Thanks for your interest. splashdown is a small, solo-maintained project, so contributions are
handled on a best-effort basis, but issues and pull requests are welcome.

## Getting started

You need [uv](https://docs.astral.sh/uv/) and [just](https://github.com/casey/just).

```sh
uv sync --group dev        # install dev dependencies
just hooks                 # install the git hooks (lefthook)
just check                 # ruff + import cycles + format-check + mypy + tests
```

`just check` is exactly what CI runs. Keep it green before opening a pull request.

## Real first-use canary

`just check` is deterministic and does not contact a Node registry. The separate first-use
canary installs the current Splashdown source, scaffolds `create-vite@latest`, runs plain
`splash init`, creates a worktree through the managed post-checkout hook, and starts both Vite
applications on their allocated ports:

```sh
just smoke-first-use
```

It requires Git, uv, Node, npm, curl, and network access, and can take several minutes. Its Node
commands use a disposable home and npm cache with a minimal environment, so they do not inherit
your npm credentials. The Python installation still uses indexes and credentials configured for
uv. This is not an OS-level filesystem sandbox; run it only when you intend to exercise the
current public Python and npm packages. Run it before a release and after changing init, scanning,
provisioning, loaders, hooks, or Vite support. It is intentionally not a required pull-request
check because `create-vite@latest` and the package registries are external moving parts.
Keep the harness Vite-specific until a second real scenario demonstrates which boundaries are
actually reusable.

Failed runs print the relevant log tail. Preserve the complete temporary repository and logs for
inspection with:

```sh
SPLASH_SMOKE_KEEP=1 just smoke-first-use
```

## Making changes

- **Tests**: new behavior gets a test in the matching `tests/test_<module>.py`. Run `just test`
  (or the full `just check`).
- **Types**: the source is `mypy --strict` over `src/splashdown`. Run `just typecheck`.
- **Docs**: user docs live in `docs/user/` and publish to [splashdown.dev](https://splashdown.dev).
  Preview with `just docs`, build with `just docs-build`.
- **Architecture**: `AGENTS.md` is the canonical guide to how the codebase fits together (modules,
  data flow, gotchas). Read it before a non-trivial change.

## Commit messages

Use one of three types, no scopes:

- `feat:` a user-noticeable addition
- `fix:` a fix for something that was not working
- `chore:` everything else (refactors, tests, docs, internal)

`feat` and `fix` appear in the [changelog](CHANGELOG.md). `chore` is skipped. Mark a breaking
change with `feat!:` / `fix!:` or a `BREAKING CHANGE:` footer, and it surfaces under **Breaking
Changes**. Keep the subject short and the body shorter, or omit the body entirely.

## Reporting bugs and security issues

- **Bugs and feature requests**: open a
  [GitHub issue](https://github.com/nielsmadan/splashdown/issues).
- **Security vulnerabilities**: do not open a public issue. See [SECURITY.md](SECURITY.md).
