# 0003: Separate inferred frameworks from explicit intent

- Status: Accepted
- Decision date: 2026-08-13
- Recorded: 2026-08-24

## Context

Framework-named init presets duplicated behavior the repository scanner could already infer and
drifted from the Profile catalog. Electron exposed a separate composition problem: it commonly
coexists with Vite or another renderer, and replacing the renderer Profile would discard its
resources and wiring. Silently changing Electron's profile directory was also unsafe because the
application must integrate the value before acquiring its single-instance lock.

## Decision

Named presets express intent that repository contents cannot infer; they are not a second framework
catalog. Framework coverage comes from scanner-selected Profiles. Model Electron as a secondary
capability layered on the primary Profile, and require an explicit isolated/shared choice before
adding `ELECTRON_PROFILE_ID`. Print integration guidance instead of rewriting an arbitrary Electron
entry point.

## Consequences

- Adding framework support does not require a matching preset.
- Renderer resources and wiring remain intact when Electron is present.
- Electron isolation is visible and deterministic, but the application must add a small guarded
  integration before `requestSingleInstanceLock()`.
- Future secondary capabilities should compose with Profiles rather than compete for detection
  precedence.

## Related

- [Init and onboarding](../features/init-and-onboarding.md)
- [Scanning and extension](../tech/scanning-and-extension.md)
- [Framework wiring](../tech/wiring.md)
