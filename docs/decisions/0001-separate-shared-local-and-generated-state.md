# 0001: Separate shared, local, and generated state

- Status: Accepted
- Decision date: 2026-05-20
- Recorded: 2026-08-24

## Context

The original file model mixed resolved values into a mise-owned file. That coupled Splashdown to
one loader and required surgical preservation of another tool's configuration. Team configuration,
checkout-specific choices, and generated values also have different ownership and version-control
lifecycles.

## Decision

Use three layers with distinct owners:

- `splashdown.toml` is the committed team contract for apps, resources, and shared target variants.
- `splashdown.local.toml` is a gitignored, add-only checkout layer for local settings and target
  variants. Collisions with the committed catalog are errors.
- `splashdown.env` is generated output owned wholly by Splashdown.

Loader configuration points at `splashdown.env`; it does not store resolved state. Writers that
merge values into foreign dotenv or envrc files remain explicit escape hatches.

## Consequences

- Team intent, personal overrides, and generated values cannot silently overwrite one another.
- Generated output can be replaced atomically without preserving foreign content.
- Env delivery is loader-agnostic, but projects carry a local file and a generated file in addition
  to the committed recipe.
- One-off targets need distinct names rather than overriding shared variants.

## Related

- [Per-checkout overrides](../features/per-checkout-overrides.md)
- [Ports and environment output](../features/ports-and-env.md)
- [Provisioning](../tech/provisioning.md)
