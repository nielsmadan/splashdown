# Product Docs

User-centric view of splashdown: who it's for, the jobs they come to do, and how well
the product serves them. This is the high-level "why / for whom / what's missing" layer
(upstream of any `docs/prd/`). Start here.

## Current-state artifacts (refined in place)
- [persona.md](persona.md) — the two co-primary personas (mobile-dev-on-worktrees,
  worktree-heavy web/backend dev) and the undecided solo-vs-team adoption context.
- [use-cases.md](use-cases.md) — UC1–UC9, the jobs-to-be-done mapped to the CLI surface.

## Reviews (dated snapshots, newest first)
- [2026-06-23-review.md](2026-06-23-review.md) — first product review (code+docs, no `--live`).
  Headline: strong on the primary jobs; weak at the edges of the happy path (pre-`init`
  traceback, a `set`-error dead-end, unconfirmed `destroy`, and no self-wiring on clone).
