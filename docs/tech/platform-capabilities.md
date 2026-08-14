# Platform capabilities and subprocess boundaries

Splashdown runs both portable coordination code and host-specific development tools. This page
records which subprocesses are required, which are optional probes, and how failures cross the CLI
boundary.

## Capability errors

`CapabilityError` is a `DeviceError` subtype for a host or executable capability that is not
available. Its `capability` field is a stable deduplication key such as `ios`, `android`, `node`, or
`gradle`; the message contains the executable and an actionable installation hint.

Apple device and native-build operations call `require_macos` before starting `xcrun`, `open`, or
`xcodebuild`. Android operations are supported on macOS and Linux when the Android SDK is installed.
Every required fixed-tool start is wrapped by `translate_tool_errors`, which converts only
`OSError` launch failures such as a missing or non-executable binary. A tool that starts and exits
nonzero retains its command-specific error or exit status.

| Target | macOS | Linux |
| --- | --- | --- |
| iOS simulator/device | Xcode required | Unsupported; explicit commands return an actionable error |
| Android emulator/device | Android SDK required | Android SDK required |
| Ports, environment, and config | Supported | Supported |

## Explicit and aggregate commands

An explicitly selected capability is strict. For example, `splash start simulator`, `splash run`
against an iOS target, and `splash target refresh ios` return exit 1 when iOS support is unavailable.
The CLI prints `error: ...` without a Python traceback.

Commands that inspect or reconcile both platforms are resilient. Unscoped `target refresh`,
`target prune`, `gc`, status inspection, and broad physical-device discovery warn once per
capability, skip that platform, and continue supported work. Status renders the affected target as
`unavailable` and does not count it as missing, stale, or orphaned.

Skipped device rows remain in the registry. Deleting a row would falsely claim that cleanup
succeeded and would prevent a later run on a capable host from destroying or reconciling the
instance. `cmd_gc` therefore performs capability-aware device cleanup before calling
`Registry.gc(include_devices=False)` for portable port and key cleanup.

## Subprocess audit

| Category | Modules and sites | Required behavior |
| --- | --- | --- |
| Required fixed tool | `devices.py`: `xcrun`, `open`, `avdmanager`, `sdkmanager`, `emulator`, `adb`; `runners.py`: Flutter, `npx`, `xcodebuild`, `xcrun`, Gradle/`gradlew`, `adb`; `commands.py`: foreign-AVD discovery | Apply a host guard where applicable. Convert launch-time `OSError` to `CapabilityError`. Preserve nonzero exits as real tool failures. |
| Best-effort integration or probe | `recipe.py`: Git metadata; `hooks.py`: Git, lefthook, yarn, and npx hook integration; `wiring.py`: Git hook check; `loaders.py`: loader approval; `commands.py`: gitignore probe | Return the existing fallback, warning, or boolean result. Missing or non-executable tools must not crash the command. |
| User-authored shell | `provisioning.py`: `run_setup`; `runners.py`: `run_custom_command` | Preserve shell semantics. Setup failures reach the existing clean runtime-error renderer; custom launch commands return the shell exit status. |

This inventory covers every `subprocess.call`, `run`, `check_output`, and `Popen` site under
`src/splashdown` as of the audit.

## Adding a subprocess

Classify the new call before implementing it:

1. For a required fixed executable, add the narrow capability/host guard around the process start.
2. For an optional integration, catch `OSError` and keep the documented fallback behavior.
3. For user-authored shell text, preserve shell exit semantics and route errors through the existing
   command boundary.

Never catch `OSError` at the top-level CLI. That would hide filesystem and programming errors that
are unrelated to executable availability.
