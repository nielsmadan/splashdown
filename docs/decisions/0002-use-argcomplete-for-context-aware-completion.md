# 0002: Use argcomplete for context-aware completion

- Status: Accepted
- Decision date: 2026-06-06
- Recorded: 2026-08-24

## Context

Argparse could describe static choices, but the useful completion candidates are target variants
from the checkout's merged catalog. The common inferred-type form also placed a variant token in
the positional slot originally reserved for a target type. Adding a runtime dependency carried
supply-chain, packaging, and Homebrew maintenance cost, while the CLI is also invoked from a hot
post-checkout path.

## Decision

Use bundled `argcomplete` for bash and zsh completion. Completers reuse the real target catalog,
remain read-only and fail-silent, and import `argcomplete` only during an active completion. Keep
the choice-less positional normalization that lets a lone variant select the inferred target type.

## Consequences

- Completion can suggest checkout-specific variants in the shortest command form.
- A malformed checkout produces no suggestions instead of corrupting the shell command line.
- Normal CLI and hook execution pay no argcomplete import cost.
- `argcomplete` remains an audited runtime dependency and a Homebrew resource.
- Completion for additional shells, registry keys, and checkout paths remains outside this
  decision.

## Related

- [CLI and command architecture](../tech/cli-and-commands.md)
- [Shell completion](../user/shell-completion.md)
