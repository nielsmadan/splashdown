# Report potential checkout sharing as advisory

**Status:** accepted

Supersedes decision 5 in [0007](0007-bound-device-run-diagnostics.md). Its physical-run network,
launch, signing and process-lifecycle decisions remain unchanged.

## Context

A Watchman ancestor root can serve several intentional checkout users. Fixed Compose host ports,
container names and absolute bind sources likewise establish possible interference or shared data,
not an actual broken managed consumer. Treating those observations as failures rejects intentional
sharing and encourages disruptive daemon or data changes without evidence.

## Decision

1. Doctor reports these concrete sharing risks as advisory warnings. Watchman findings identify
   the ancestor root. Compose findings identify the file, service, value slot and bind source.
   Intentional sharing is valid; isolation guidance is conditional on the user's intended scope.
   A separate proven violation of the declared managed-value contract remains a problem.
2. Observe Watchman only for configured React Native/Expo apps, once at the checkout root. Use
   `watchman --no-spawn --no-local watch-list` with a bounded timeout and refuse checkout-controlled
   executables. Missing optional Watchman skips inspection. A nonzero exit alone cannot establish
   a stopped daemon, so ambiguous failures remain incomplete. A valid empty/local list establishes
   only its watch-root property, not Metro or hot-reload readiness.
3. Keep Compose inspection static and report-only, using the existing YAML value-region seam and
   supported short/long forms. Unsupported or unreadable input is incomplete. Fixed absolute bind
   sources may share data even with distinct ports and project names. Suggest checkout-relative
   binds or ordinary project-scoped named volumes when isolation is intended.
4. Both modes leave files, watches, daemons, containers and data untouched for these checks.
   Guidance distinguishes real recipe declarations from uninspected stored allocations and
   invoking-shell state. Never infer a service's managed port from an arbitrary resource name.

## Consequences

A completed advisory-only doctor run exits 0. Failed applicable inspection remains incomplete at
exit 1, independently of findings already collected. No YAML dependency, Docker/Colima probe,
watch-management action, daemon reset or automatic data migration is needed. User guidance asks
for a scope decision instead of unconditionally deleting a watch or rewriting shared mounts.
