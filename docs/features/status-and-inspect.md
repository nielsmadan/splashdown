# Inspect checkout state without exposing values

`splash status` inspects the selected project. `splash status all` inspects exactly the sorted
checkout identities in the existing ports, values, devices, and physical-claim registry. A missing
registry is an empty fleet. Inspection never registers the current directory, allocates resources,
consumes claim notices, garbage-collects rows, grants trust, writes output, or runs project commands.

Normal status includes readiness observations. `--verbose` expands text presentation, including
resource/target rows and configuration paths. It does not select additional probes. JSON contains
all observations and is identical with or without verbose. The explicit `local` scope and
`--check` option have been removed.

## Selection and results

Local status uses the nearest recipe within the starting Git worktree, or the exact starting
directory outside Git. All supplied cwd paths must be existing readable directories. A valid,
unallocated project is inspectable. An uninitialized directory reports an incomplete inspection
with a `splash init` hint. Tracked live checkouts with missing or invalid recipes retain their
stored resource names and target rows, while other fleet entries continue. Known deleted fleet
entries remain visible with a `splash gc` finding.

Exit 0 means inspection completed, including actionable findings and unavailable optional probes.
It does not assert readiness. Required input read/parse failures produce exit 1 and partial data.
Usage errors return 2. Interruption returns 130, preserving completed fleet rows when available.
Text reports use stdout and diagnostics use stderr. JSON has one shared result envelope with
`command`, `status`, `exit_code`, `data`, `error`, `warnings`, and `next_steps`.

Both local and fleet results use `data.checkouts`. Each checkout contains:

- `checkout`, `exists`, and `complete`;
- `counts`: counts of stored port, variable, simulator, emulator, and claim rows;
- `resources`: `key`, assignment `state`, and `port_state`. Observed ports also carry `owners`,
  an array of `{pid, command}` records, an empty array for a free port, or null for unknown owners;
- `targets`: type, variant, declaration source, device name, observed status, and health;
- `checks`: `id`, `state`, `message`, `next_steps`, and an optional input/output `path`.

Check states are `ok`, `finding`, `unavailable`, `error`, and `not_applicable`. `error` denotes a
required-input failure and sets checkout `complete` false. Global registry errors are carried in
the envelope diagnostics even when available checkout rows can be inspected completely. Every
finding, unavailable indication, and relevant next step appears in normal text too.

Resource values never enter status's public data, messages, or diagnostics. Use `splash env` or
`splash env get KEY` to read assigned values.

## Observation boundary

- Resources are compared by name against declarations. Missing assignments and obsolete keys are
  findings. Allocated ports are observed without reallocating them. Free ports are ordinary idle
  state. A bounded optional listener snapshot supplies PID and executable names, never argv.
- File writers are compared against stored assignments through the same assignment scanner and
  quoting rules used by provisioning. No recipe resource resolution is performed. Missing or
  stale outputs are findings with a `splash sync` hint. Ambiguous, unreadable, or unsafe output
  destinations are required-input errors. Co-owned content is preserved without writing.
  Registry-only and stdout writers need no generated file.
- Saved Git trust is read strictly. Missing authorization is a finding. Malformed or unreadable
  saved trust is an input error. Bootstrap completion markers are never read: status cannot
  establish that arbitrary preparation commands succeeded previously.
- Integration observations cover the declared shell loader's wiring and post-checkout hook
  configuration/activation. Parseable wiring conflicts are findings, required configuration
  read/parse failures are errors, and failed optional Git probes are unavailable. Loader ownership
  is not approval. Mise/direnv authorization is explicitly unavailable because it is not probed.
  Doctor retains deeper framework and tool diagnostics.
- Local and fleet targets use the same merged declarations plus registered managed instances.
  Missing local/global configuration means no overrides. Malformed or unreadable configuration is
  an input error. Successful platform discovery can identify orphaned, drifted, or undeclared
  instances. Failed or malformed discovery never establishes health or cleanup eligibility.
  Stopped managed devices and absent, never-created lazy targets are normal state. Physical
  absence or ambiguous selection gets an actionable finding. Independent platform availability
  remains visible even when another platform provides a matching physical connection.

## Environment commands

`splash env` lists stored assignments as sorted `KEY=VALUE` lines. `splash env get KEY` returns one
exact, case-sensitive key. An empty listing succeeds, and an assigned empty string prints a blank
line. A missing key exits 1 with `assignment_missing` and a listing hint. JSON listing data contains
`checkout` and `values`; a successful get contains `checkout`, `key`, and `value`.

These reads require no recipe or trust. They retain obsolete assignments, never derive values,
and never create state. Inside Git they select the nearest recipe entry or registry-known identity
without walking beyond the worktree root. Outside Git they query the exact starting directory.
Explicit deleted cwd paths are accepted only when they exactly match a known registry identity.
This exception belongs only to env inspection, not status or mutations.

`splash env set KEY=VALUE` and `splash env release [KEY]` retain their separate mutation behavior.

## Implementation

`registry.read_registry_snapshot` provides immutable available rows without locks or initialization.
`status.build_status_report` returns a shared `CommandResult`; `commands.cmd_status` passes it to
`cli_output.emit_result`, the only final output owner. `render_status_result` handles text only.
`status_targets.TargetObservations` caches successful and failed platform discoveries for one
invocation. The target command family's row types remain in `status.py`.
