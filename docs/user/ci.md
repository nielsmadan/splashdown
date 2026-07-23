# CI integration

CI runners are ephemeral and have no global registry: `splash` is not installed there, and there's nothing to allocate from. The pattern is to **write `splashdown.env` directly** in the CI job with the fixed ports your service containers expose, so the rest of the job sees the same env-file shape that local dev uses:

```yaml
# GitHub Actions example
- name: Write splashdown.env with fixed CI ports
  run: |
    cat > splashdown.env << 'EOF'
    POSTGRES_PORT=5432
    REDIS_PORT=6379
    DATABASE_URL=postgresql://user:pass@localhost:5432/testdb
    REDIS_URL=redis://localhost:6379
    COMPOSE_PROJECT_NAME=myapp-test-ci
    EOF

- name: Run integration tests
  run: bun --env-file=.env.test --env-file=splashdown.env test tests/
```

Key points:

- **`splash` is never installed or run in CI.** The heredoc produces the same `KEY=VALUE` format splashdown emits locally.
- **Each CI step runs in a fresh shell.** Any step that needs values from `splashdown.env` must load it explicitly, either via `--env-file` flags, `source splashdown.env`, or by exporting the vars as job-level `env:`. A step that writes the file does not automatically export its contents into subsequent steps.
- **`splashdown.local.toml` must be in `.gitignore`.** Its first line says "Gitignored, not committed". The file contains per-checkout device declarations that vary between machines. Once committed, every fresh clone starts with a tracked file that `splash target add` will mutate, polluting `git status` permanently.
