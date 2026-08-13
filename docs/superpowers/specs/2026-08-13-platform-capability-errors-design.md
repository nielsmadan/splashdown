# Platform capability errors

## Goal

Splashdown must run safely on both macOS and Linux. Commands that require a host capability or
external tool must never leak `FileNotFoundError`, `PermissionError`, or another subprocess
traceback. Explicit operations fail with a concise, actionable `error:` message. Commands that
intentionally inspect or reconcile both mobile platforms continue the supported work and warn
once about each unavailable platform.

This work also repairs the four device teardown tests that currently pass on macOS only because
they mock deletion but allow the newly centralized shutdown call to reach `xcrun`.

## Supported host contract

- Port allocation, recipe handling, environment output, scanning, wiring, and other
  platform-neutral commands support macOS and Linux.
- iOS simulators, physical iOS devices, and iOS-native builds require macOS and Xcode. An
  explicitly selected iOS operation on another host fails before launching a subprocess with:
  `iOS <operation> requires macOS and Xcode`.
- Android emulators and physical Android devices support macOS and Linux when the Android SDK
  tools are installed. A missing SDK root or required binary names the missing capability and
  how to configure or install it.
- Framework launchers such as `flutter`, `npx`, `xcodebuild`, `gradle`, and `adb` report a clean
  missing-tool error. Their own non-zero exit statuses remain command failures and are not
  misreported as platform unavailability.
- Windows support is outside this change. The same guarded boundaries still prevent Apple or
  missing-tool operations there from producing a Python traceback.

## Error model

Add a `CapabilityError` subtype of `DeviceError` in `errors.py`. It represents only an operation
that cannot start because the host platform or required executable is unavailable. Ordinary
tool failures remain `DeviceError` or return the tool's exit status as they do now.

Platform and tool checks live beside the subprocess-owning device or runner boundary. Apple
device functions enforce the macOS requirement before any `xcrun`, `xcodebuild`, or `open`
launch. Android lookup functions raise `CapabilityError` for a missing SDK root or binary.
Fixed framework launchers translate executable launch failures into `CapabilityError` with the
tool name and an installation/configuration hint. User-authored `[project] run` and `[setup.*]`
shell commands are not classified as platform capabilities; their failures continue to identify
the configured command.

The top-level CLI already converts `DeviceError` into `error: <message>` and exit status 1, so
explicit operations need no second error renderer. No broad `except OSError` is added at the CLI:
that would hide unrelated filesystem and programming errors.

## Explicit and aggregate behavior

An operation is explicit when the user selected one platform or target, including
`run/start/stop/destroy simulator`, a resolved iOS physical target, `target refresh ios`, and
`target prune ios`. A capability failure propagates to the top-level CLI and returns 1.

The following commands are aggregate because they may encounter both platforms in one run:

- `target refresh` and `target refresh all`
- `target prune` and `target prune all`
- `gc`
- status inspection, including `status --check`
- physical-device discovery without a `platform` filter

Aggregate commands catch only `CapabilityError` at the per-platform or per-row boundary. They
print at most one warning for the same unavailable capability, preserve any registry row they
could not safely reconcile or destroy, and continue processing supported rows. They do not catch
ordinary `DeviceError`: a real failure from an available tool remains visible and non-zero.

Status must not label an iOS row `orphan`, `missing`, or `stale` merely because it is being read
on Linux. It renders the device state as `unavailable`, warns once, and excludes that row from
repair counters. JSON and text output use the same state.

## Subprocess audit

Every subprocess site under `src/splashdown/` is classified during implementation:

1. **Required fixed tool** — device lifecycle and framework launch commands. Guard the host when
   applicable and translate executable launch failure into `CapabilityError`.
2. **Best-effort probe/integration** — Git metadata, hook-manager installation, loader approval,
   and wiring detection. These intentionally retain their documented fallback, warning, or
   boolean behavior and must not crash when the tool is missing.
3. **User-authored shell command** — `[setup.*]` and `[project] run`. Preserve shell semantics,
   but ensure the CLI presents the configured command and exit status without a traceback.

The audit result is recorded in the device and command technical documentation so future
subprocess additions have an explicit error-handling category.

## Test strategy

- Update the four Linux-failing teardown tests to stub both shutdown and deletion and positively
  assert the ordered `shutdown` then `destroy` lifecycle.
- Simulate a non-macOS host and verify an explicit simulator command exits 1, prints the macOS/Xcode
  requirement, and never invokes `xcrun`.
- Exercise mixed aggregate rows and verify unavailable iOS work warns once, Android work still
  runs, and skipped registry rows remain intact.
- Verify explicit `target refresh ios` and `target prune ios` fail rather than skip.
- Verify status reports `unavailable` without inflating orphan/stale/missing repair counters.
- Simulate missing Android SDK and runner executables and verify concise `error:` output without a
  traceback.
- Run the four formerly Linux-only failures, the complete suite with coverage, `just check`, and
  the strict documentation build.

## Non-goals

- Installing Xcode, Android SDKs, or framework tools.
- Making iOS workflows available on Linux.
- Adding Windows as a supported host.
- Converting ordinary non-zero tool exits into capability errors.
- Hiding unexpected filesystem, permission, parsing, or programming errors behind a generic CLI
  catch.
