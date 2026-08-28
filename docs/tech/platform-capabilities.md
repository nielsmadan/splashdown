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
| Finite required tool operation | `device_ios.py`: `xcrun`, `open`; `device_android.py`: `avdmanager`, `sdkmanager`, `adb`; `runners.py`: Xcode/Gradle discovery, app installation, and simulator app launch | Apply a host guard where applicable. Convert launch-time `OSError` to `CapabilityError`; convert a timeout to `DeviceError` naming the operation; preserve nonzero exits as real tool failures. Discovery and list operations get 30 seconds; mutations and simulator app launch get 120 seconds. |
| Intentionally long-running required operation | `device_android.py`: detached emulator plus 60-second wall-clock readiness deadline; `runners.py`: Flutter, `npx`, Xcode/Gradle builds, and physical-device or Android app launches | Preserve tool exit status. Do not impose the finite-operation deadline on a build or attached interactive app process. |
| Best-effort integration or probe | `recipe.py`: Git metadata; `bootstrap.py`: Git clone/worktree state; `commands.py`: Git ignore checks; `hooks.py`: Git, gitignore, and the installed lefthook binary; `wiring.py`: Git hook check; `loaders.py`: loader approval | Return the existing fallback, warning, or boolean result. Missing or non-executable tools must not crash the command. |
| User-authored shell | `provisioning.py`: `run_setup`; `runners.py`: `run_custom_command` | Preserve shell semantics. Setup failures reach the existing clean runtime-error renderer; custom launch commands return the shell exit status. |

The table records the subprocess categories and their current owners. When a new subprocess site
is added, classify it here and audit it against the rules below rather than treating this list as a
permanent call-site inventory.

## Adding a subprocess

Classify the new call before implementing it:

1. For a required fixed executable, add the narrow capability/host guard around the process start.
2. Classify a fixed operation as finite or intentionally long-running. Route finite calls through
   `device_tools.py` with the matching discovery or mutation deadline.
3. For an optional integration, catch `OSError` and keep the documented fallback behavior.
4. For user-authored shell text, preserve shell exit semantics and route errors through the existing
   command boundary.

Never catch `OSError` at the top-level CLI. That would hide filesystem and programming errors that
are unrelated to executable availability.
