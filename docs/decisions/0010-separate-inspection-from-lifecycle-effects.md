# Separate inspection from lifecycle effects

**Status:** accepted

## Context

The CLI foundation and environment/status review require inspection to work without preparing a
checkout. Constructing the mutable registry creates state, and reading through lifecycle helpers
can consume notices, remove stale claims, or hide discovery failures behind defaults. Those
behaviors make an inspection unreliable when the user is diagnosing a failed preparation.

## Decision

1. Read environment assignments and status through an immutable snapshot of existing registry
   files. Preserve valid rows when independent files fail, without creating locks, changing modes,
   consuming notices, or collecting stale entries. Existing per-file atomic replacement remains
   the persistence contract; a snapshot does not promise a transaction across files.
2. Separate checkout selection from recipe validation. Env inspection uses the nearest recipe or
   registry-known identity within the current Git worktree and exact selection outside Git. Only
   an explicit env cwd that exactly matches a registered deleted identity bypasses the existing-
   directory prerequisite. Other commands keep their own prerequisites. An incomplete registry
   snapshot cannot safely choose an env identity, so env fails rather than guessing.
3. Status reports observable state with the same bounded checks in every format. Findings and
   unavailable optional probes are successful inspections; unreadable required inputs make the
   report incomplete. Fleet inspection retains available results and continues other checkouts.
   Missing or stopped lazy targets are ordinary states, and failed discovery cannot establish
   health or cleanup eligibility. Saved preparation completion is not evidence that arbitrary
   project commands succeeded.
4. Keep final result emission outside command effects and locks. Migrated families return the
   shared success/error/partial result and emit one envelope. Each remaining family must migrate
   its handler and final output together, especially init's existing failure emission. Parser and
   pre-handler errors can use the common boundary before those legacy handlers start.
5. Keep resource values in explicit env reads. Status exposes names, allocation state, observations,
   and actionable checks without including resolved values. Explicit stdout writers retain their
   separate raw-data contract.

## Consequences

Inspection remains useful after recipe removal, failed output writing, and partial registry read
failures. Snapshot decoding is stricter than the legacy mutation readers, so corrupt state is an
error rather than an empty successful result. Optional discovery is cached for one invocation;
status does not repair anything or replace doctor's deeper tool/framework checks.

AI, env inspection, and status adopt the shared result boundary first. This does not establish
complete migration of preparation, trust, hooks, resource mutations, or the remaining command
families. Current APIs and migration constraints live in [CLI and commands](../tech/cli-and-commands.md),
the [registry snapshot contract](../tech/registry.md#read-only-inspection-snapshot), and
[inspection behavior](../features/status-and-inspect.md).
