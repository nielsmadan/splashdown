# Bound device-run diagnostics to facts Splashdown can establish

**Status:** accepted

## Context

The first physical-iPhone field report
found a split Metro port, device-local URLs, missing local-network configuration, stale Watchman
roots, and manual packager bookkeeping. It also proposed automatic host selection, signing checks,
and detached process ownership. The implementation response separates fixes within the existing
resource/target model from additions that need new ownership and selection contracts.

## Decision

1. A run resolves resources and refreshes their file outputs before device lifecycle work. Its
   launcher receives the resulting environment explicitly, with resource values taking precedence
   over the caller's shell. This closes the UC2/UC5 failure for unattended agent shells.
2. Use advisory network checks for physical runs. Report resource names containing loopback and
   missing or unreadable RN/Expo local-network descriptions before launching. Never infer an
   iPhone's runtime permission state or actual LAN reachability from a source plist.
3. Keep host addresses explicit with the existing `set` and `template` resources for this release.
   The report's interim loopback warning ships; a new automatic `host` type is deferred. A checkout
   has a target catalog, not a persisted active target, and `sync` receives no target selection.
   Rewriting one shared output to localhost after a device run would recreate the original split
   configuration. A default-route address can also select a VPN or an interface the phone cannot
   reach. UC1/UC5/UC6 require a stable, inspectable input before automatic selection is introduced.
4. Report port owners through optional bounded `lsof` inspection for UC8. Defer `run --detach`,
   managed logs, and process stopping. Binding a reserved port does not establish that Splashdown
   started the process. The existing `stop` verb owns simulator/emulator shutdown. Satisfying
   UC2/UC7 and adjacent process-supervision use case CJ requires a separate process record with
   launch identity, process-group ownership, exit state, log retention, and cleanup semantics.
   Neither `stop` nor `gc` may kill an arbitrary process discovered on a port.
5. Add a read-only Watchman ancestor-root check to RN/Expo doctor runs. Query existing watches
   without spawning a daemon or creating/deleting watches. An ancestor watch is a problem even if
   the checkout also has its own watch; malformed or unavailable results stay unverified. Use the
   checkout root for monorepos so a legitimate repository-wide watch is accepted.
6. Defer automated signing-identity and provisioning-profile validation. The current recipe does
   not model the effective team, signing identity, or profile selected by each framework and
   configuration. Parsing literals from a project file would miss xcconfig and build-setting
   overrides. A useful check needs resolved application-target settings and must match team,
   bundle identifier, certificate, device membership, and expiration while recognizing automatic
   signing that can create a profile during build. Unavailable keychains or profile directories
   must be reported as unknown. A blanket missing-profile build blocker would reject valid runs.
   The network portion of the report's incremental preflight ships now; signing remains an
   explicit Xcode preparation step documented in the device guide.

## Consequences

The immediate changes address all five findings at their existing-contract or interim scope.
Automatic host selection, signing/profile diagnostics, and detached process lifecycle remain
unimplemented. This decision records that boundary rather than presenting their absence as a
passing preflight. Further implementation should supersede the relevant decisions here when its
new contracts are established.

Device checks print advisory warnings together and preserve launcher exit codes. They do not
print resolved URLs or credentials. A source-level success means only that the inspected static
description was present. It does not establish that the selected build uses that file.

The network check follows [Apple TN3179](https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy):
apps using the LAN should include a usage description, while access is governed by runtime
permission. This was checked against Apple's documentation on 2026-09-05, not on a physical phone.

Expo gets an explicit `--port` for the allocated Metro port. Source inspection on 2026-09-05 found
that [SDK 55's native-run bundler resolution](https://github.com/expo/expo/blob/sdk-55/packages/@expo/cli/src/run/resolveBundlerProps.ts)
can fall back to 8081 when reusing a server and only the environment supplied the custom port.
Both platform CLIs accept `--port`. The regression tests verify Splashdown's invocation and env
delivery; they do not run Expo or build an iOS app.
