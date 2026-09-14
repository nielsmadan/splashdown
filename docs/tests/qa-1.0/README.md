# 1.0 QA records

Repeatable manual QA procedures for the 1.0 release, plus dated records of every execution.
Automated coverage lives in `tests/`; this directory holds the scenarios that need real external
tooling, real Git events, or real hardware, which the unit suite must never depend on.

- Procedures live in this file, one section per scenario family.
- Results live under [`runs/`](runs/), one file per execution, named `YYYY-MM-DD-<family>.md`.
- A record identifies the tested artifact, the tool versions, the host, the scenario inputs, the
  observed output, and the final state. Keep an original failure alongside its retest rather than
  overwriting it.

`mkdocs.yml` publishes `docs/user/` only, so nothing here reaches the public site.

## GIT-HOOKS: post-checkout integration with external hook managers

Covers INIT-15. Verifies that every automatic adapter forwards Git's three `post-checkout`
arguments (old ref, new ref, branch flag) to `splash hook post-checkout` through a real
`git checkout` and a real `git worktree add`, that unrelated hook jobs survive, that repeated
setup is a no-op, and that local activation happens at `splash trust` rather than `splash init`.

### Prerequisites

| Tool | How it is obtained |
| --- | --- |
| `lefthook` | on `PATH` |
| `pre-commit` | `uv tool install pre-commit` |
| `prek` | `uv tool install prek` |
| `husky` | `npm install husky` into a scratch directory |
| `simple-git-hooks` | `npm install simple-git-hooks` into a scratch directory |
| Overcommit | Ruby gem, recognition-only scenario, no adapter to verify |

### Isolation

Run everything in disposable repositories outside the splashdown checkout. Point
`XDG_STATE_HOME` at a scratch directory so the registry never touches real machine state, and
point `GIT_CONFIG_GLOBAL` at a scratch file so `core.hooksPath` experiments stay local. Install
the candidate as the real `splash` executable and put it first on `PATH`, because the forwarding
command each adapter writes resolves `splash` through `command -v` and refuses an executable
inside the checkout.

```sh
uv venv "$SCRATCH/qa-venv"
uv pip install --python "$SCRATCH/qa-venv/bin/python" -e .
export PATH="$SCRATCH/qa-venv/bin:$PATH"
export XDG_STATE_HOME="$SCRATCH/qa-state"
export GIT_CONFIG_GLOBAL="$SCRATCH/gitconfig" GIT_CONFIG_SYSTEM=/dev/null
```

### Procedure, per adapter

The per-adapter walk below is deliberately written so each manager is met in the arrangement a
real project has, not the one splashdown's editor finds easiest. A single "happy" input per
adapter proves nothing about the branch a project actually takes, so steps 2 and 4 below are
where the input is varied.

1. Create a fresh `git init -b main` repository with a `package.json` and a `vite.config.js`, so
   `splash init` detects a single Vite app with one port resource.
2. Write the manager's own configuration containing at least one unrelated job. For the YAML and
   JSON adapters, run the scenario once per input shape in "Configuration shapes" below rather
   than once in total, because each shape reaches a different branch of the editor.
3. Run `splash init`. Record the wiring message and the configuration file afterwards. Confirm the
   result with the manager's own parser — for pre-commit and prek that means
   `<tool> run --hook-stage post-checkout --all-files` in that repository, which both loads the
   configuration and executes the hook. A file that only looks right is not evidence.
4. Record the **first** init as a byte diff against the pre-init copy, not only the second. The
   editors are idempotent, so a second run is byte-identical whatever the first one did to the
   file; only the first diff shows what was rewritten.
5. Commit, then run `splash init --overwrite` again. Record that the manager configuration shows
   no diff.
6. Run `splash trust`. Record whether the manager's Git hook was installed, and record
   `splash doctor` before and after it: correct configuration whose manager hook is not yet
   installed is a `⚠`, and only an installed hook is a `✓`.
7. Drive `git checkout -b feature`, `git checkout main`, and
   `git worktree add -b wtbranch ../<name>-wt`. Record splashdown's output for each.
8. Read `splashdown.env` in both the primary checkout and the linked worktree. Distinct ports
   prove the worktree-creation event reached splashdown with all three values intact, because
   `is_worktree_creation` requires the old ref to be all zeros, the new ref to be a real object
   id of the same length, and the flag to be `1`.

### Procedure, argument-level probe

The end-to-end procedure proves the values were correct in aggregate. To record the exact three
arguments, repeat steps 1 to 7 with a recording stub named `splash` first on `PATH`:

```sh
#!/bin/sh
printf '%s\n' "ARGC=$# ARGV=[$1] [$2] [$3] [$4] [$5] CWD=$PWD" >> "$SPLASH_PROBE_LOG"
exit 0
```

Keep the stub outside the repository under test, otherwise each adapter's guard refuses it.

### Configuration shapes

Run the pre-commit and prek scenarios against each of these, not only the last one. The first
four are the arrangements an ordinary project arrives with; every one of them took a different
path through the editor, and each must end with a configuration the real tool loads.

| Shape | `.pre-commit-config.yaml` input |
| --- | --- |
| Remote repos only | one `- repo: https://…` entry with `rev` and `hooks`, no `local` repo |
| `repos:` present but empty | `repos:` and nothing under it |
| Four-space sequence indent | the repo list indented four columns instead of two |
| Sequence at column zero | `repos:` with `- repo:` items starting in column 0 |
| Hooks sequence at its key column | `hooks:` with `- id:` items at the same column as `hooks:` |
| Block-indented hooks | `hooks:` with `- id:` items indented under it |
| Comments around the list | a leading `#` comment and a trailing one inside the repo item |

And these must be refused, with the file byte-identical afterwards and manual instructions
printed:

| Shape | Input |
| --- | --- |
| Flow `hooks` | `hooks: []` and `hooks: [{id: lint, …}]` |
| Flow `repos` | `repos: [{repo: local, …}]` |
| Symlinked configuration | `.pre-commit-config.yaml` a symlink to a file outside the checkout |

For prek, run the whole per-adapter procedure twice: once with a `prek.toml`, and once with a
project configured in `.pre-commit-config.yaml` and **no** `prek.toml` (install prek's hook first
so detection resolves to prek). The second is the common case, because prek is marketed as a
drop-in pre-commit replacement. Record that no `prek.toml` was created, because prek's lookup
order would make one outrank the file the project actually uses.

For simple-git-hooks, run the `package.json` scenario against a file with **CRLF line endings,
non-ASCII characters, and hand-formatted nested objects**, and record the first init as a byte
diff. `json.dumps` over a whole document silently normalizes all three, and the idempotency check
in step 4 cannot see it.

### Recognition-only scenarios

Run `splash init` in a repository configured for Overcommit, in one configuring two managers at
the same evidence level, and in one with a custom `core.hooksPath`. Record that the manager
configuration is byte-identical afterwards, that `core.hooksPath` is unchanged, and that the
manual instructions name a command that works in that manager's own syntax.
