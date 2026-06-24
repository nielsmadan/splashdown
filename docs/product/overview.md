# Product Docs

User-centric view of splashdown: who it's for, the jobs they come to do, and how well
the product serves them. This is the high-level "why / for whom / what's missing" layer
(upstream of any `docs/prd/`). Start here.

## Current-state artifacts (refined in place)
- [persona.md](persona.md) — **primary: the parallel-agent developer** (many LLM agents, each in
  its own worktree); secondary: mobile and web/backend work flavors. (rev 2, 2026-06-23)
- [use-cases.md](use-cases.md) — UC1–UC10 (the jobs mapped to the CLI surface) plus an
  **Adjacent / candidate use cases** section (CA–CM) brainstormed for the agent persona. (rev 2)

## Reviews (dated snapshots, newest first)
- [2026-06-23-agent-persona-and-adjacent-use-cases.md](2026-06-23-agent-persona-and-adjacent-use-cases.md)
  — agent-persona reframe + adjacent-use-case brainstorm (code+docs+`--multi`). Headline: lead
  with the parallel-agent framing; the silent collisions are caches/daemons/test-runners/
  credentials (most are env vars splashdown already emits → ship presets), and `splash lease` +
  owned stateful-service lifecycle are the standout new capabilities.
- [2026-06-23-review.md](2026-06-23-review.md) — first product review (code+docs, no `--live`).
  Headline: strong on the primary jobs; weak at the edges of the happy path (pre-`init`
  traceback, a `set`-error dead-end, unconfirmed `destroy`, and no self-wiring on clone).
