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
      - run: <your test command>
```

Or keep the values in a committed `.env.test` and load that. Either way your app reads the same variable names it reads locally (`DATABASE_URL`, and so on), just pointed at fixed ports instead of splashdown-allocated ones.

## Why not install and run `splash` in CI?

You can install it (mise, pipx, or `pip install splashdown`), but running `splash` would *allocate* ports from its ranges, say 9081, which will not match the fixed ports your CI service containers publish. splashdown is built for dynamic, collision-free ports across many local checkouts, the opposite of what CI wants: fixed, predictable ports. Installing it adds a step and produces values you then have to work around. Setting the env directly is simpler and less error-prone.

The one exception is a self-hosted runner executing several jobs at once that must not collide. There, installing splashdown and pinning each job's ports can help. Most CI never hits this.

## If a script hard-requires `splashdown.env`

If some command specifically loads `splashdown.env` (for example `--env-file=splashdown.env`), write the file in the job with the fixed CI ports rather than trying to reproduce local allocation:

```yaml
- name: Write splashdown.env with fixed CI ports
  run: |
    cat > splashdown.env << 'EOF'
    DATABASE_URL=postgresql://user:pass@localhost:5432/testdb
    REDIS_URL=redis://localhost:6379
    EOF
```

Prefer the direct-env approach above when you can. Each CI step runs in a fresh shell, so a step that writes the file does not export its contents to later steps. Load it explicitly with `--env-file`, `source splashdown.env`, or an `env:` block.

## Keep `splashdown.local.toml` gitignored

Its first line says "Gitignored, not committed". It holds per-checkout device declarations that vary between machines. If it gets committed, every fresh clone starts with a tracked file that `splash target add` will later mutate, polluting `git status`.
