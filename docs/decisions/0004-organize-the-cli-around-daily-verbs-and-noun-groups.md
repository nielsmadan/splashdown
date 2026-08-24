# 0004: Organize the CLI around daily verbs and noun groups

- Status: Accepted
- Decision date: 2026-06-10
- Recorded: 2026-08-24

## Context

The original flat command list gave rare maintenance operations the same prominence as the daily
workflow. `refresh` named several unrelated actions, resource reconciliation was called
`provision`, and separate cleanup commands split one machine-wide job. The tool was pre-1.0, so a
clear surface was more valuable than retaining aliases.

## Decision

Keep daily target lifecycle verbs and checkout operations at the top level. Use `sync` for resource
reconciliation, keep bare `splash` equivalent to `splash sync`, group catalog management under
`target`, group resolved-value access under `env`, and keep one top-level `gc`. Present the commands
as task-oriented tiers rather than argparse's alphabetical list. Remove superseded names without
compatibility aliases.

## Consequences

- The help screen communicates the normal path from init to sync to run.
- `refresh` has one remaining meaning under target lifecycle management.
- The parser maintains a curated help epilog and a command-name set alongside its subparsers.
- Scripts written against the pre-release command names had to update immediately.

## Related

- [CLI and command architecture](../tech/cli-and-commands.md)
- [CLI reference](../user/cli.md)
