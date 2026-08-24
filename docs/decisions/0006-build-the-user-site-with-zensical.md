# 0006: Build the user site with Zensical

- Status: Accepted
- Decision date: 2026-07-19
- Recorded: 2026-08-24

## Context

The user site needed search, navigation, theming, and strict link validation without adding a
second JavaScript toolchain. Existing content used MkDocs-style configuration and Markdown,
including template expressions that some frontend-oriented generators interpret. At the time of
the decision, the Material maintainers were moving active development toward Zensical.

## Decision

Use Zensical as a development-only dependency. Keep `mkdocs.yml` as the site configuration, build
only `docs/user/`, and carry the small theme overrides needed for Splashdown's palette and landing
page.

## Consequences

- Documentation commands remain in the Python and uv toolchain.
- Strict builds validate the published navigation and links without affecting runtime dependency
  count.
- The project accepts an early-stage documentation tool and owns a small CSS/template compatibility
  layer that may need adjustment as Zensical evolves.
- A future generator change does not alter the separate-audiences decision.

## Related

- [User documentation](../user/index.md)
- [`mkdocs.yml`](../../mkdocs.yml)
- [`Justfile`](../../Justfile)
