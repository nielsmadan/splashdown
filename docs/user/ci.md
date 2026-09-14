---
title: CI integration
description: Decide when CI needs splashdown and configure fixed service ports without local allocation.
---

# CI integration

Splashdown's job is to hand each *concurrent* checkout its own free ports so they never collide. A CI runner has neither problem. It runs one job at a time, and its service containers listen on fixed, well-known ports (Postgres on 5432, Redis on 6379). There is nothing to coordinate, so **CI usually does not need splashdown at all**.

## The simple path: set the env directly

Point your app at the CI service ports with your normal CI env mechanism. No splashdown involved.

```yaml
# GitHub Actions
jobs:
  test:
    services:
      postgres:
        image: postgres:16
        ports: ["5432:5432"]
    env:
      DATABASE_URL: postgresql://user:pass@localhost:5432/testdb
      REDIS_URL: redis://localhost:6379
    steps:
      - uses: actions/checkout@v4
      - run: uv run pytest
```

Replace `uv run pytest` with the project's normal test command. Or keep the values in a
committed `.env.test` and load that. Either way your app reads the same variable names it reads
locally (`DATABASE_URL`, and so on), just pointed at fixed ports instead of
splashdown-allocated ones.

## Why not install and run `splash` in CI?

You can install it (mise, pipx, or `pip install splashdown`), but running `splash` would *allocate* ports from its ranges, say 9081, which will not match the fixed ports your CI service containers publish. splashdown is built for dynamic, collision-free ports across many local checkouts, the opposite of what CI wants: fixed, predictable ports. Installing it adds a step and produces values you then have to work around. Setting the env directly is simpler and less error-prone.

The one exception is a self-hosted runner executing several jobs at once that must not collide. There, installing splashdown and pinning each job's ports can help. Most CI never hits this.

## If a script hard-requires the env destination

If some command specifically loads the file splashdown writes (for example
`--env-file=splashdown.env`), write that file in the job with the fixed CI ports rather than trying
to reproduce local allocation. The destination is whatever `[project] env_file` names in
`splashdown.toml`, and `splashdown.env` only when that key is absent, so substitute your own path
below:

```yaml
- name: Write the env destination with fixed CI ports
  run: |
    cat > splashdown.env << 'EOF'
    DATABASE_URL=postgresql://user:pass@localhost:5432/testdb
    REDIS_URL=redis://localhost:6379
    EOF
```

Check `[resources]` too. A resource carrying a `writer` of its own goes to that file instead of the
default one, so a job that needs its value has to write that file as well.

Prefer the direct-env approach above when you can. Each CI step runs in a fresh shell, so a step
that writes the file does not export its contents to later steps. Load it explicitly with
`--env-file`, `source <destination>`, or an `env:` block.

## The ignore-coverage note on a fresh clone

Any sync that writes a file names every destination Git would still show:

```
  note: apps/api/.env is not ignored (no rule matches)
```

Init reuses whatever ignore rules are already effective on the machine it runs on, and a rule in
your personal `core.excludesFile` or in this clone's `.git/info/exclude` counts. Neither of those
is committed, so a destination your own setup happens to hide can reach the repository with no rule
protecting it. This note is the only thing that tells the next person, and CI is often where it
first bites: a job that runs `splash` and then asserts a clean tree fails on a generated file
nobody ignored. Add the rule to `.gitignore` and commit it.

## Keep `splashdown.local.toml` gitignored

Init adds the rule for you unless one of your own already covers the file. It holds per-checkout device declarations that vary between machines. If it gets committed, every fresh clone starts with a tracked file that `splash target add` will later mutate, polluting `git status`.

If the file was already tracked when you ran init, init says so and leaves Git tracking alone. An ignore rule does not untrack a file, so run `git rm --cached splashdown.local.toml` yourself.
