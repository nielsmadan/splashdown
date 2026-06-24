# Documentation

Map of the `docs/` tree. `README.md` (repo root) is the authoritative user-facing spec for
behavior, the TOML schema, and the CLI surface; `CLAUDE.md` holds agent/contributor guidance and
the architecture summary. These docs go deeper, by audience.

## Sections
- [tech/](tech/overview.md) — **Technical / architecture**: module internals, the data flow, and
  the cross-cutting patterns (re-export hub, import order, hot-path discipline, the TSV registry).
- [prd/](prd/overview.md) — **Product requirements**: what each feature does for the user, kept in
  sync with the implementation (`file:line` references). Owned by the `doc` skill.
- [product/](product/overview.md) — **Product strategy**: personas, jobs-to-be-done, and dated
  product reviews (the why / for-whom layer). Owned by the `review-product` skill.
- [superpowers/](superpowers/) — historical design **plans** and **specs** (point-in-time records
  of how features were designed; not kept in sync with code).

## Layering
`product/` (user & why) → `prd/` (product behavior, tracks code) → source in `src/splashdown/`.
`doc --update` keeps `prd/` ↔ code in sync; `review-product` checks `product/` ↔ `prd/`.
