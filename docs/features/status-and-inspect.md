# Inspect checkout state without exposing values

> **Problem:** UC8 — read a checkout's resources, targets, and leaked state without re-deriving
> it from configuration. See [use-cases](../product/use-cases.md) and
> [persona](../product/persona.md).

## Overview

`splash status` reports this checkout's resource keys, live port state, declared targets,
automation trust/bootstrap state, and stale-row hints. `splash status all` widens that view to every
tracked checkout; `--check` revalidates device and checkout health; `--format json` provides the
same report as structured stdout. `splash env` is the scriptable companion for listing keys and
explicitly reading, setting, or releasing one value.

Operational commands hide resolved values by default because registry entries may contain API
tokens, credentials, or database URLs. Add the root-level `--show-values` flag when disclosure is
intentional:

```sh
splash status                         # keys and port state
splash --format json status           # resource objects omit "value"
splash env                            # keys only
splash --format json env              # sorted JSON array of keys
splash --show-values status           # include KEY=VALUE
splash --show-values env              # include KEY=VALUE
splash --format json --show-values env # JSON object of key/value pairs
```

`splash env get KEY` always prints the selected value. It is an explicit disclosure operation and
does not require `--show-values`.

Both output flags are root options and therefore precede the command. `--format` is supported by
sync, status, bare `env`, bare `target`, `target claims`, and `target claim`. `--show-values` is
supported by sync, status, a normal init's first sync, and bare `env`. Other nested env/target
actions and commands reject flags they would otherwise ignore.

## How it works

`commands.cmd_status` is a thin compatibility wrapper. It asks `status.build_status_report` for a
typed `StatusReport`, then passes that report to `cli_output.render_status`. Gathering owns no
text or JSON formatting; rendering owns no registry or device queries.

The report builder selects either the resolved current checkout or `registry.all_checkouts()` and
uses one shared context for health counters, latest-OS caching, and deduplicated capability
warnings. Detailed reports contain typed checkout, resource, and target records:

- Resource records come from `Registry.all_for`. A parseable recipe identifies port resources,
  which get a live `in use` or `free` probe; other keys have no port state.
- Local target records come from the merged recipe, local, and global declarations. The `all`
  detail view uses registry device rows instead. Both consume `device_health`, the same classifier
  used by refresh, so inspection and reconciliation agree about missing, orphaned, drifted, and
  undeclared instances.
- Automation records use the Git private/common directories already owned by bootstrap. A live Git
  checkout reports sync trust, retained bootstrap trust, whether the current recipe declares
  bootstrap, and completion as `not-declared`, `pending`, `complete`, or `invalid`. Non-Git and
  defunct checkouts use no record.
- Capability failures become one warning plus target state `unavailable`; they never increment a
  repair counter.

Compact `status all` text asks the report builder for typed table rows rather than detailed blocks.
The renderer then produces PATH, SUMMARY, and the conditional ISSUE column. `--verbose` requests
the detailed block view. The compact path does not inspect Git or bootstrap state. Text goes to
stderr; JSON goes to stdout.

`--check` adds a typed summary and routes each issue to the operation that can fix it:

- `splash gc` for defunct checkouts;
- `splash target refresh` for orphaned, stale, or undeclared managed devices;
- `splash run` for declared managed devices that have not been created;
- reconnecting or pairing for missing physical hardware.

Without `--check`, local status also reports dead port/KV rows and unfilled `set` resources that
need `splash env set NAME=VALUE`.

## JSON contract

Local status is one checkout object; `status all` wraps checkout objects in `{"checkouts": [...]}`.
`--check` adds `summary`. Every checkout contains:

- `checkout` and `exists`;
- `automation`, either `null` or an object with `sync_trusted`, `bootstrap_trusted`,
  `bootstrap_declared`, and `bootstrap_completion`;
- `resources`, whose default shape is `{key, port_state}` and whose `--show-values` shape also has
  `value`;
- `targets`, with type, variant, source, device name, status, and the health booleans.

This makes the safe schema explicit: changing to JSON format alone does not reveal values.

## Environment commands

- Bare `splash env` prints sorted keys. JSON mode returns a sorted string array. Add
  `--show-values` for `KEY=VALUE` text or a JSON object.
- `splash env get KEY` prints that value, or exits 1 when it is absent.
- `splash env set KEY=VALUE` accepts only a declared `type = "set"` resource. Invalid assignment,
  recipe, declaration, or type input exits 2 before mutation.
- `splash env release [KEY]` releases one key or every registry entry for the target checkout.
- `--checkout PATH` works with the list/get/set/release forms. For an action, it may appear before
  or after the action and takes precedence over root `--cwd`.

Every form canonicalizes the checkout path exactly as provisioning does.

## Key entry points

- `src/splashdown/status.py` — typed reports and all status gathering.
- `src/splashdown/cli_output.py` — text/JSON status, env, sync, and error rendering.
- `src/splashdown/commands.py` — thin `cmd_status` wrapper and env application service.
- `src/splashdown/cli.py` — `--format`, `--show-values`, status options, and dispatch.
- `src/splashdown/registry.py` — `all_for`, `summary_for`, and `all_checkouts`.
- `src/splashdown/bootstrap.py` — Git directory, trust, and completion state readers.

## Gotchas

- Unfilled `set` resources have no registry row, so they appear only in the local footer hint.
- Text status goes to stderr. Use JSON when stdout parsing matters.
- `env get` exits 1 for an absent key rather than printing an empty line.
- Local status describes declared targets; `status all` describes registered targets. A declared
  but never-created variant therefore appears only in the local view.
- The compact table's ISSUE column is conditional. Parse JSON instead of relying on column count.
- Bootstrap trust can remain true while `bootstrap_declared` is false because trust is clone-wide
  and retained across refs and deinit. Completion is checkout-local and only interpreted when the
  current recipe declares bootstrap.

## Why

The worktree-heavy user needs a state view that is useful in terminals, CI, and agent transcripts
without turning routine diagnostics into a credential dump. Keys and health are operational data;
values are disclosed only by an explicit flag or the single-key `env get` operation.
