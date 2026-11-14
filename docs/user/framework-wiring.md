---
title: Framework wiring
description: Use splash doctor to detect and repair framework configuration that ignores allocated ports.
---

# Framework wiring (`splash doctor`)

Allocating a port doesn't always reach the running process. Most frameworks hardcode the port in one or two config files that override the env var, so splashdown carries per-framework wiring checks that detect those hardcoded points and (where safe) auto-patch them. `splash init` runs the configuration checks after scaffolding, checks the result again, and lists the files it changed, so a normal first setup does not need `splash doctor --fix` for those. The one exception is the `hook` row below. Installing a local hook is activation rather than configuration, so init never runs that check and `splash trust` is what puts the hook in place. `splash doctor` checks each configured app and shared project integration. `splash doctor --fix` authorizes recognized configuration repairs, without installing dependencies or activating hooks.

Checks use each configured app's actual resource names, types, writers, and destinations.
Init rebuilds that context from its final parsed recipe. An app with no managed port gets a
skipped port check. Several arbitrary ports require an established consumer relationship or
manual verification. Scaffold names in the table below are examples, not required names.
Dotenv readers use the resource's actual file destination relative to the app. Shell `envrc`
and non-file writers do not supply a dotenv path. Recognized inherited-value consumers do not
prove that the parent shell has loaded those values.

| Profile | Check | What it ensures |
|---|---|---|
| *(any)* | `hook`, `hook-activation` | Checks configuration and local activation once at the Git project root. Non-Git and nested projects skip these checks. Safe repairs update recognized manager configuration. Missing native hooks and inactive managers require activation by their owning tool or `splash trust` |
| react-native | `rn-metro-config` | `metro.config.js` consumes `RCT_METRO_PORT`. Auto-patches the recognized `port: <N>` literal shape, otherwise prints the exact snippet to paste |
| react-native | `rn-pkg-port` | Recognized React Native commands consume the Metro port. Numeric `--port` arguments become `${RCT_METRO_PORT:-N}`, preserving the original number as fallback and unrelated JSON formatting. Renamed resources need explicit CLI wiring |
| react-native | `rn-xcode-env` | `ios/.xcode.env` exports a splashdown-managed `RCT_METRO_PORT` block that reads your configured environment file. iOS bakes the port into the binary at compile time, so Xcode-GUI builds need this to pick up the per-checkout port. Only a line that assigns or exports `RCT_METRO_PORT` counts, so a dotenv path mentioned for any other reason, such as `export ENVFILE=.env.staging`, is left alone and does not stop the fix. Any spelling that resolves to the configured file counts, including a hand-written conditional, and a reference through a shell variable splashdown does not recognize is reported rather than accepted |
| react-native, expo | `watchman-root` | When Watchman is installed, checks whether it watches an ancestor of the checkout. Runs at the checkout root, including when the app lives in a subdirectory. Report-only |
| vite | `vite-port-wired` | Follows the declared port to `server.port` through a literal config, direct process-env dot/bracket reads, simple destructuring and constant bindings, numeric conversion, and nested fallback expressions led by the managed key. Unrelated or constant-leading expressions need manual verification. A Vite `loadEnv` named import can read inherited values with a matching literal prefix. The default is `VITE_`, `""` admits all keys, and literal string arrays admit matching prefixes (`[]` admits none). A recognized excluded key gets `(process.env.KEY ?? env.KEY)`, preserving the original fallback and unrelated reads. Mentioning the key elsewhere is insufficient. Missing ports are manual-only. Spread, conditional, mutated, or unsupported flows are incomplete |
| astro | `astro-config-port` | `astro.config.*` sets `server.port` from `WEB_DEV_PORT`. Astro never reads `PORT` from the environment, so an unwired config always boots on 4321. Auto-injects the line into a `defineConfig({...})` or bare object export. If a `server:` block already exists it prints the snippet instead, because that block may be nested under `vite:` where the port configures Vite rather than Astro |
| *(any)* | `compose-hardcoded-ports` | Static, report-only inspection of service host ports, literal container names and absolute bind sources in short/long block or flow forms. Potential collisions and shared data are advisory warnings with file/service/value-slot context. Unknown or unreadable shapes are incomplete. Interpolated values and runtime configuration need manual verification. Docker and Colima are never queried |
| springboot | `springboot-application-properties` | Every config Spring may load uses the `server.port=${PORT:8080}` placeholder. Reads YAML's nested `server:` / `port:` form as well as the flat properties spelling, and scans profile-specific files (`application-dev.properties`), since one of those pinning a literal overrides a wired base file. The nested lookup only matches a top-level `server:`, so Actuator's `management.server.port` is not mistaken for the app's, and the last declaration in a file wins, matching Spring. Manual-only (Java config rewrites are too risky to automate) |
| laravel | Vite checks | Checks the asset-server consumer when a Vite config is present. Multiple arbitrary ports require a recognized consumer relationship. The PHP server keeps its native `SERVER_PORT` convention |
| angular | `angular-pkg-port` | `package.json` scripts running `ng serve` pass `--port $WEB_DEV_PORT`. Angular reads no env var for its port at all, and writing a literal into the committed `angular.json` would churn it in every worktree, so the value goes through the npm script, which npm runs via a shell. The variable has to reach the flag: `WEB_DEV_PORT=4200 ng serve` only sets an environment variable, which `ng serve` never reads. A project with no `ng serve` script at all is reported as a problem rather than a pass, since there is then nowhere for the allocated port to go. Auto-patched, replacing any port flag already present rather than appending a second one, and placing it on `ng serve` itself so a compound script does not hand it to whatever follows the `&&`. Wires `npm start`, a bare `ng serve` still uses angular.json's default |
| deno | `deno-port-wired` | Something consumes `PORT`, either a `deno.json` task passing `--port $PORT` or server code reading `Deno.env.get("PORT")`. Deno has no port env var of its own: `deno serve` and `Deno.serve()` both bind 8000 regardless. Auto-patches `deno serve` tasks, inserting the flag *before* the script argument (anything after it goes to the script, not to Deno), and relocates a flag that is already there in the wrong place. The rewrite is scoped to Deno's own flags, so a chained sidecar command keeps its own `--port`. The check applies the same position rule, so an appended `--port $PORT` is reported rather than accepted, and it wants splashdown's own `PORT` rather than any variable whose name happens to contain it. The in-code form is reported with the snippet to paste. `deno.jsonc` is never rewritten, since a JSON round-trip would drop its comments |
| aspnetcore | `aspnet-launch-settings` | No `"commandName": "Project"` profile in `Properties/launchSettings.json` pins `applicationUrl`. `dotnet run` reads that key and it wins over an inherited `ASPNETCORE_HTTP_PORTS`, so the allocated port is silently ignored while it is set. Auto-drops the key (launchSettings is JSON, so the rewrite is safe). `IISExpress` profiles are left alone, since only IIS Express reads theirs. **Requires .NET 8+**: `ASPNETCORE_HTTP_PORTS` does not exist on net6.0/net7.0, so on those target frameworks the check turns report-only and never strips `applicationUrl`, because removing it there would strand the app on the shared default 5000 |

**A ✓ confirms the named check.** File checks strip comments before looking, so a commented-out `// server: { port: process.env.WEB_DEV_PORT }` left over from an earlier attempt does not count as wiring. Where a check cannot make sense of a config at all, it reports a ✗ saying so rather than passing it. A check that raises costs one ✗ and the rest of the run continues.

**Shared Watchman roots.** For React Native and Expo, `watchman-root` queries existing service
state once at the checkout root with `watchman --no-spawn --no-local watch-list` and a
three-second timeout. An ancestor watch is advisory because other checkouts can share it.
Review its scope and users, and choose a checkout-scoped watch when isolation is intended.
Intentional sharing is valid. Both modes leave watches and daemon state untouched.

Missing optional Watchman skips inspection without claiming its roots were checked.
Timeouts, permission failures, protocol errors, invalid responses and nonzero exits leave the
check incomplete. A nonzero exit alone does not establish that the daemon is stopped.
Doctor refuses checkout-controlled Watchman executables, including tools elsewhere in the
checkout when the recipe is nested. The same protection applies to direnv authorization probes.
A valid list with no ancestor confirms
only that watch-root property. It does not verify Metro or hot reload.

**Compose sharing.** Fixed ports and container names can collide across checkouts. A fixed
absolute bind source can share data even with separate ports and `COMPOSE_PROJECT_NAME` values.
Doctor names the service, slot and source. Use a checkout-relative bind or an ordinary
project-scoped named volume when isolation is intended. No files, mounts or data are changed.
Guidance lists actual declared port resources and whether `COMPOSE_PROJECT_NAME` is declared,
without asserting stored allocations or what the invoking shell loaded. Declare and provision
any resource you intend to use, then pass its values to Compose explicitly or through your loader.

**Launcher tools and SDKs.** Doctor checks the configured Flutter, React Native, Expo, native iOS,
and native Android apps independently. Shared executable requirements are required, while iOS and
Android build routes are alternatives within the same cross-platform app. A working Android tool
route makes missing iOS tooling advisory for that app. A native iOS app still needs its own iOS
route. Missing common tools or all known routes is a problem. An attempted probe that fails stays
incomplete even when another route is available.

These checks establish executable and SDK-file presence, not compatible dependency versions,
successful builds, signing, installation or runtime behavior. Doctor prints the framework's own
diagnostic command for deeper manual validation. It never executes Flutter doctor, npx, Gradle
wrappers, CocoaPods or project configuration to discover prerequisites. Custom run dependencies
and profiles without a built-in device launcher have explicit limited coverage.

Flutter's configured `jdk-dir` and Android Studio bundled JDK selection are currently unsupported
by these static checks. Its Android JDK result stays incomplete even if Java is on `PATH` or
`JAVA_HOME` is set. Run `flutter doctor -v` yourself to verify the selected JDK. Relative
`JAVA_HOME`, `DEVELOPER_DIR`, `ANDROID_HOME` and `ANDROID_SDK_ROOT` values also have explicit
incomplete coverage. Use absolute paths for inspection. Native iOS additionally needs
`xcodebuild` on the app's effective `PATH`.

**CocoaPods installation.** For configured React Native, Expo, Flutter and native iOS apps
with an established Podfile, `pods-manifest` compares `Podfile.lock` and `Pods/Manifest.lock`
as raw bytes. Matching files establish manifest consistency only. They can still match after
Podfile or dependency changes, so the result does not establish dependency freshness or build
readiness. Missing files mean installation is incomplete or has not happened yet. Different
files mean the current installation is inconsistent. Unreadable files report their actual cause.
Each result names the app and paths. An available Android tool route in that same app makes a
missing or mismatched optional iOS installation advisory. Failed inspections remain incomplete.

Doctor uses each app's `ios/` directory, or the native iOS app root. It also recognizes a whole
literal `module.exports = {project: {ios: {sourceDir: 'native/apple'}}}` or equivalent
`export default` React Native configuration, and the parent of a declared native iOS
`[project.ios] workspace` or `project` path. Dynamic configuration and custom platform
implementations require manual verification. CocoaPods path assignments found in native-root
xcconfig files require verification of the effective Xcode configuration. Doctor does not infer
active settings from a standalone file. Other custom layouts have limited coverage. Apps without
an established CocoaPods integration skip this check, including Expo before native generation.

Use the project's normal iOS dependency preparation, or run `pod install` in the Podfile
directory. Doctor suggests `bundle exec pod install` when a relevant Gemfile declares CocoaPods.
The normal run workflow may prepare dependencies itself. Doctor is a standalone troubleshooting
command and adds no launch prerequisite. Both modes leave Pods and lockfiles unchanged.

**Saved destinations.** Device checks cover project and local destinations plus this checkout's
claims. Other global phones and other checkouts' claims do not expand the scope. A phone can be
disconnected and a virtual device can be absent or stopped during a successful check.
When a saved phone omits its platform, a unique matching checkout claim supplies it. Conflicting
claim platforms remain incomplete. Local destination configuration must be a regular file,
and unreadable or refused files leave the recipe's independent destination checks available.

Android observation queries only an existing ADB server, without running the adb client or starting
or resetting a server. A refused connection is an idle observation. Timeouts, permission failures
and invalid protocol responses remain incomplete. Host inventory does not verify hardware IDs.
A USB transport with the same text as a saved ID is explicitly unverified, and wireless transport
addresses never replace saved IDs. Unsupported server endpoint forms are reported without trying
the default endpoint.

Live iOS and virtual-device state observation is currently unsupported under doctor's no-start
boundary. Doctor shows saved identities without asserting connection, pairing, existence or running
state. A paired idle iOS tunnel is normal. The selected destination is still validated by `splash run`.

**Selected loader.** Doctor checks the loader named in your recipe and the configured environment
output path. A valid directive you wrote yourself counts as configured. `loader = "none"` skips
loader checks. Missing loader tools and malformed or conflicting configuration are problems.
`--fix` can add or update a recognized directive, but it never approves or activates a loader.

For direnv, doctor reads `direnv status --json` without evaluating `.envrc` and reports whether
that exact file is allowed. After an edit, it checks again because direnv approval may be
invalidated. Probe failures leave the check incomplete. Mise and devbox authorization observation
is explicitly unsupported. No result establishes what your current shell has loaded.

**Hook-manager coexistence.** `splash` integrates automatically with Husky, Lefthook, pre-commit, prek, and simple-git-hooks. It adds or updates only its own entry, so your other hook jobs and scripts stay exactly as they are. With no manager, `splash trust` installs an untracked hook in Git's common hooks directory, which is shared by the repository's worktrees. Doctor leaves this installation manual.

To decide who owns the event, `splash` looks first at `core.hooksPath`, then at the hooks already installed in the directory Git actually runs, then at the manager configuration files in your checkout, and last at what `package.json` declares. It never changes `core.hooksPath`, and it never installs a hook manager for you. A configured but inactive integration is a problem, even after `--fix` successfully repairs its configuration. Activate it with the owning tool or `splash trust`. Overcommit, an unrecognized `core.hooksPath`, and a checkout that configures two managers with equal weight are all left untouched. In those cases `splash` prints the exact entry to add yourself, written in that manager's own syntax.

```sh
splash doctor                    # read-only findings and summary
splash doctor --fix              # eligible configuration repairs and manual guidance
splash doctor --format json      # one structured result, including partial findings
```

Doctor uses the nearest selected `splashdown.toml` and visits every declared app at its configured
path and profile, including repeated profiles. Shared project checks run once. Correct a profile
in the recipe itself. There are no app/check selectors or framework overrides. With no declared
apps, an explicit `project.framework` describes an app at the project root. Doctor does not guess
apps from nearby files.

A missing recipe produces a diagnostic and suggests `splash init`. Invalid configuration reports
the actual parse or schema cause. Both modes stop there and preserve the files.

Findings distinguish warnings, problems, incomplete inspections, and skipped checks. Exit 0 means
completed checks found no unresolved problems, including advisory warnings and verified repairs.
Problems or incomplete applicable checks exit 1, usage errors exit 2, and interruption exits 130.
A report with no applicable checks says so without claiming the project is healthy. Text findings
and summaries use stdout. Operational errors use stderr. JSON returns one result with all available
findings, changed paths, and repair verification, even when a later check fails or is interrupted.
If interruption prevents complete change accounting, the report keeps known changed paths and
marks the affected repair incomplete and unverified.

Plain doctor creates no registry or lock files and consumes no notices. Eligible repairs acquire
the checkout coordination lock, reread their configuration, and retain changed-file information if
a repair only partly completes. Framework repairs use the configured resource contract.
Hook activation always remains a separate action.

Framework and hook-manager autofixes edit only regular files inside the checkout. A symlinked
destination, a symlinked parent directory, or another non-regular entry is rejected. The native
Git hook gets the same final-file protection in Git's resolved hooks directory. Existing modes are
preserved and accepted changes replace the directory entry atomically, so a link is never followed.

**Known limitation: RN Android.** Android's Metro port is also baked into the build (via the RN Gradle plugin / `BuildConfig`), with a different mechanism than iOS. Splashdown doesn't currently wire the Android side. For now `yarn android` works (the RN CLI propagates `RCT_METRO_PORT` to Gradle), but bare `gradle assembleDebug` may default to 8081. Tracked as a future check.
