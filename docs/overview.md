# Documentation

Map of the `docs/` tree. `README.md` (repo root) is the authoritative user-facing manual;
`AGENTS.md` / `CLAUDE.md` hold agent/contributor guidance. These docs go deeper, split by
**audience and format**.

## Two audiences

- **Using splashdown** (you run `splash`): start at `README.md`; deeper walkthroughs in
  [`user/`](user/overview.md). Verbose, task-oriented, example-driven.
- **Building splashdown** (contributors + coding agents): [`features/`](features/overview.md)
  for *what* each feature does (a terse behavior reference) and [`tech/`](tech/overview.md) for
  *how* it's implemented. Concise, pointer-first (`file:line`).

## Sections
- [user/](user/overview.md): how-to guides for people using the tool (e.g. monorepo setup).
  README links here. Human-format; agents don't load these by default.
- [features/](features/overview.md): what each feature does, kept in sync with the code. The
  behavior reference for contributors and agents. Owned by the `doc` skill.
- [tech/](tech/overview.md): module internals, data flow, cross-cutting patterns (re-export hub,
  import order, hot-path discipline, the TSV registry). Owned by the `doc` skill.
- [product/](product/overview.md): personas, jobs-to-be-done, dated product reviews (the
  why / for-whom layer). Owned by the `review-product` skill.
- [superpowers/](superpowers/): historical design plans and specs (point-in-time records; not
  kept in sync with code).

## Layering
`product/` (who & why) → `features/` (what it does, tracks code) → `tech/` + source in
`src/splashdown/` (how). `user/` is the human-facing view of the same behavior, in walkthrough
form. `doc --update` keeps `features/` and `tech/` in sync with code; `review-product` checks
`product/` ↔ `features/`.
