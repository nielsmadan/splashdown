# 0008: Make the env destination configurable and key-scoped

- Status: Accepted
- Decision date: 2026-09-14
- Recorded: 2026-09-14
- Supersedes: the generated-output clause of
  [0001](0001-separate-shared-local-and-generated-state.md)

## Context

ADR 0001 modeled `splashdown.env` as generated output owned wholly by Splashdown, which let the
writer replace the file in one operation. Delivery into a dotenv file the project already reads
was available only as a per-resource `writer` escape hatch, and init picked one automatically by
looking for an existing `.env` or `.env.local`. That made a user-visible routing decision out of a
filesystem accident, and it left two ownership models separated only by filename: wholesale for
`splashdown.env`, key-scoped for everything else.

## Decision

The recipe's `[project] env_file` names the default output destination, `splashdown.env` when
absent, and `splash init --env-file PATH` records the choice. The selected loader is wired to read
that destination. File-presence-based automatic dotenv routing is removed.

Every file destination is key-scoped. Splashdown replaces the keys the recipe declares where they
already stand and preserves every other line, its line endings and its trailing blank lines, for
`splashdown.env` exactly as for a custom path, because a filename alone does not grant wholesale
ownership. A destination Splashdown creates is mode `0600`; one that already exists keeps the mode
its owner chose. Every declared file destination is validated and recorded in one canonical
spelling, `[project] env_file` and `writer = "envfile=PATH"` alike, so `./.env` and `.env` never
become two destinations and the path that is checked for an escape is the path that is written.
A resource with an explicit `writer` keeps its own destination and is not also copied into the
default file.

## Consequences

- A project whose app already reads `.env` keeps its ordinary launch commands, with no per-resource
  writer and no `exec` wrapper.
- The recipe alone cannot say what to remove, because a resource deleted from it is absent from
  the resolved values too. The checkout's registry rows are the record of what Splashdown wrote,
  so both the writer and teardown take them as `known_keys`: a sync drops a key the recipe no
  longer declares from the default destination, and `clear_writer_destinations` removes it there
  too, deleting a file left with nothing else. That reach is the default destination only. A
  resource routed to `envfile=apps/api/.env` and then deleted from the recipe keeps its line in
  that file, including through `deinit`, because nothing records which foreign file a vanished
  resource targeted. A resource whose writer is `none` or `stdout` is excluded from both paths:
  Splashdown delivers no file for it, so a line under that name in the destination is the user's.
- A destination must be parsed rather than replaced, so an ambiguous assignment for a declared key
  is an error rather than a second competing definition.
- A clone with no loader configured writes the destination with nothing sourcing it until the user
  chooses a loader or points `--env-file` at a file the app reads itself.

## Related

- [Ports and environment output](../features/ports-and-env.md)
- [Init and onboarding](../features/init-and-onboarding.md)
- [Provisioning](../tech/provisioning.md)
