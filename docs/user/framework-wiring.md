# Framework wiring (`splash doctor`)

Allocating a port doesn't always reach the running process. Most frameworks hardcode the port in one or two config files that override the env var, so splashdown carries per-framework wiring checks that detect those hardcoded points and (where safe) auto-patch them. `splash init` runs the wiring after scaffolding. `splash doctor` re-runs it anytime to verify, and `splash doctor --fix` re-applies the autofixes.

| Profile | Check | What it ensures |
|---|---|---|
| react-native | `rn-hook` | post-checkout fires `splash`, wired through your existing hook manager (lefthook / husky) instead of clobbering `core.hooksPath` |
| react-native | `rn-metro-config` | `metro.config.js` consumes `RCT_METRO_PORT`. Auto-patches the recognized `port: <N>` literal shape, otherwise prints the exact snippet to paste |
| react-native | `rn-pkg-port` | `package.json` `start`/`ios`/`android` scripts don't carry `--port <N>` (which would override the env var), auto-stripped |
| react-native | `rn-xcode-env` | `ios/.xcode.env` exports a splashdown-managed `RCT_METRO_PORT` block. iOS bakes the port into the binary at compile time, so Xcode-GUI builds need this to pick up the per-checkout port |
| vite | `vite-config-process-env` | `vite.config.{ts,js}` reads env vars from `process.env` rather than `loadEnv()`. Auto-rewrites `env.X` → `process.env.X` so splashdown.env loaded by mise/direnv/devbox reaches Vite. A name you already read as `process.env.X` elsewhere is left alone, so the `process.env.X \|\| env.X` fallback chain survives |
| vite | `vite-port-wired` | `vite.config.{ts,js}` names `WEB_DEV_PORT` somewhere, otherwise the allocated port is never consumed. Manual-only (adding a `server.port` block to an arbitrary config is not safe to automate). Any spelling counts, including `process.env["WEB_DEV_PORT"]` and destructuring |
| astro | `astro-config-port` | `astro.config.*` sets `server.port` from `WEB_DEV_PORT`. Astro never reads `PORT` from the environment, so an unwired config always boots on 4321. Auto-injects the line into a `defineConfig({...})` or bare object export; if a `server:` block already exists it prints the snippet instead, because that block may be nested under `vite:` where the port configures Vite rather than Astro |
| *(any)* | `compose-hardcoded-ports` | `compose.yaml` / `docker-compose.yml` uses `${VAR:-default}` for host ports and no literal `container_name`. Manual-only (splashdown ships no YAML parser, and rewriting indentation-sensitive YAML by regex is not safe). Runs at the repo root whatever framework was detected, since compose is infrastructure rather than an app |
| springboot | `springboot-application-properties` | `application.properties` / `application.yml` uses the `server.port=${PORT:8080}` placeholder. Manual-only (Java config rewrites are too risky to automate) |

**Hook-manager coexistence.** `splash` detects lefthook (`lefthook.{yml,yaml}` or in `package.json` devDeps), husky (`.husky/`), or an existing `core.hooksPath`, and wires the post-checkout entry in whichever it finds. Only as a last resort does it own `.githooks/` + `core.hooksPath`.

```sh
splash doctor                    # read-only report (✓/✗ per check)
splash doctor --fix              # apply autofixes; print manual instructions for the rest
splash doctor --framework=react-native   # override detection if needed
```

**Known limitation: RN Android.** Android's Metro port is also baked into the build (via the RN Gradle plugin / `BuildConfig`), with a different mechanism than iOS. Splashdown doesn't currently wire the Android side. For now `yarn android` works (the RN CLI propagates `RCT_METRO_PORT` to Gradle), but bare `gradle assembleDebug` may default to 8081. Tracked as a future check.
