# 0005: Separate published user docs from builder docs

- Status: Accepted
- Decision date: 2026-07-19
- Recorded: 2026-08-24

## Context

A single long README was serving both new users and contributors. User guides need task-oriented
examples and a navigable site, while contributor and agent docs need concise behavior and
implementation references. Merging those audiences makes either onboarding too terse or builder
documentation too verbose.

## Decision

Keep the README as a landing page and publish only `docs/user/` at splashdown.dev. Keep current
behavior in `docs/features/`, implementation contracts in `docs/tech/`, product material in
`docs/product/`, and rationale in `docs/decisions/`. Enforce the public boundary structurally with
`docs_dir: docs/user` rather than relying only on navigation configuration.

## Consequences

- User and builder docs can optimize for different reading patterns and maintenance cadences.
- Some subject overlap is intentional, but verbatim duplication within one audience remains a
  maintenance error.
- Builder and internal planning material cannot accidentally enter the published site build.
- A behavior change may require separate concise builder and explanatory user updates.

## Related

- [Documentation map](../overview.md)
- [User documentation](../user/index.md)
- [Technical overview](../tech/overview.md)
