# Monorepos

> Covers **UC1** (per-checkout ports / env) and **UC3** (init & onboarding) in a multi-app
> workspace. Audience: engineers setting up splashdown in a monorepo. Persona: the
> parallel-agent developer in `docs/product/persona.md`.
> `README.md` is the authoritative schema reference.

## Contents

- [How splash init handles monorepos](#how-splash-init-handles-monorepos)
- [JS workspace: web + backend](#js-workspace-web--backend)
- [RN/Expo + web + backend](#rnexpo--web--backend)
- [Polyglot: JS workspace + native iOS + Android](#polyglot-js-workspace--native-ios--android)
- [docker-compose services](#docker-compose-services)
- [Gotchas](#gotchas)

---

## How splash init handles monorepos

`splash init` is *honest*, not *smart*. It auto-scaffolds simple repos where every app maps
unambiguously to a distinct resource name. It *defers* when it detects a collision — for
example, two apps that both want a resource named `PORT` — writing a structure-only recipe
(`[project]` + `[apps.*]` with empty `resources = []` per app) and printing:

```
monorepo detected (N apps) — resources not auto-configured; see docs/user/monorepos.md
```

That message points here. The patterns below are the copy-pasteable recipes to reach for
when init defers. Hand-author the `[resources.*]` section with distinct env names per app;
the scanner-produced `[project]` and `[apps.*]` sections are already correct.

---

## JS workspace: web + backend

A typical pnpm/yarn/npm monorepo with a Next.js front end and a Node (NestJS / Express /
Hono) back end.

**Why the collision.** Both `nextjs` and `node-backend` profiles natively read a variable
called `PORT`. A single env-file can hold only one value for a given name, so two apps
with the same resource name is a conflict splashdown refuses to silently paper over.
The fix is to give each app a *distinct* name and let the orchestrator place the value
where each framework expects it.

```toml
[project]
workspace = "pnpm"
loader = "mise"

[apps.web]
path = "apps/web"
profile = "nextjs"
resources = ["WEB_PORT"]

[apps.api]
path = "apps/api"
profile = "node-backend"
resources = ["API_PORT"]

[resources.WEB_PORT]
type = "port"
range = [3000, 3100]

[resources.API_PORT]
type = "port"
range = [9000, 9100]
```

**Root orchestrator wiring.** `next dev` reads `PORT`; `nest start` also reads `PORT`.
Neither reads `WEB_PORT` or `API_PORT` out of the box, so translate in the root dev script:

```json
// package.json (root)
{
  "scripts": {
    "dev": "turbo run dev",
    "dev:web": "PORT=$WEB_PORT next dev",
    "dev:api": "PORT=$API_PORT nest start --watch"
  }
}
```

Or in a Turborepo `turbo.json` pipeline, pass them as task-level env overrides, or prepend
the translation in each app's own `dev` script.

`splashdown.env` (loaded by mise/direnv/devbox) exports `WEB_PORT` and `API_PORT`. Each
app's orchestrator script re-exports the right one as `PORT` just before the framework
process starts.

---

## RN/Expo + web + backend

A full-stack mobile monorepo: a React Native (or Expo) app alongside a Next.js web client
and a Node API.

```toml
[project]
workspace = "pnpm"
loader = "mise"

[apps.mobile]
path = "apps/mobile"
profile = "react-native"
resources = ["RCT_METRO_PORT"]

[apps.web]
path = "apps/web"
profile = "nextjs"
resources = ["WEB_PORT"]

[apps.api]
path = "apps/api"
profile = "node-backend"
resources = ["API_PORT"]

[resources.RCT_METRO_PORT]
type = "port"
range = [8081, 8200]

[resources.WEB_PORT]
type = "port"
range = [3000, 3100]

[resources.API_PORT]
type = "port"
range = [9000, 9100]

[targets.simulator.default]
model = "iPhone 17"

[targets.emulator.default]
device = "pixel_9"
```

`RCT_METRO_PORT` is read natively by the Metro bundler (React Native and Expo both honour
it). `splash doctor --fix` patches `metro.config.js`, the package.json `start` script, and
`ios/.xcode.env` so all three build paths pick up the per-checkout port. See
[framework-wiring.md](framework-wiring.md) for the full wiring details.

Run the mobile app with:

```sh
splash run simulator      # iOS sim — per-checkout instance, never collides with other worktrees
splash run emulator       # Android AVD
```

`splash run` executes at the **repo root** and detects the framework there. In a
monorepo where the mobile app lives in a subdir (`apps/mobile`) and uses yarn/pnpm
rather than `npx`, set a custom run command so the launch happens in the right
place with the right tool (see the "Custom run command" section in `README.md`):

```toml
[project.run]
ios     = "yarn --cwd apps/mobile react-native run-ios --udid {device_id}"
android = "yarn --cwd apps/mobile react-native run-android --deviceId {device_id}"
```

This overrides the built-in launcher; splashdown still reconciles and boots the
declared `[targets.*]` first, then runs your command with the booted device id
injected.

Web and API are launched from the root orchestrator as in the previous pattern.

---

## Polyglot: JS workspace + native iOS + Android

A JS workspace that also contains first-party native `ios/` and `android/` folders. This is
typical of apps that started native and later added a web layer, or cross-platform SDKs that
ship both a web demo and native sample apps.

Declare the native apps explicitly under `[apps.*]`. Splashdown doesn't auto-claim native
folders inside a JS workspace to avoid false-positives on the generated `ios/`/`android/`
directories that React Native and Expo write.

```toml
[project]
workspace = "pnpm"
loader = "mise"

[apps.web]
path = "apps/web"
profile = "nextjs"
resources = ["WEB_PORT"]

[apps.api]
path = "apps/api"
profile = "node-backend"
resources = ["API_PORT"]

[apps.ios]
path = "ios"
profile = "ios-native"

[apps.android]
path = "android"
profile = "android-native"

[resources.WEB_PORT]
type = "port"
range = [3000, 3100]

[resources.API_PORT]
type = "port"
range = [9000, 9100]

[targets.simulator.default]
model = "iPhone 17"

[targets.emulator.default]
device = "pixel_9"
```

**What each native app needs in addition.** Only if you drive the app with `splash run simulator`,
`ios-native` needs a scheme set (it's not required for the recipe to parse or for per-checkout sim
naming):

```toml
[project.ios]
scheme = "MyApp"
# configuration = "Debug"   # optional, default shown
```

`android-native` picks up reasonable defaults from the project structure but you can pin
them:

```toml
[project.android]
# module          = "app"
# variant         = "debug"
# application_id  = "com.example.myapp"
```

Run with:

```sh
splash run simulator    # xcodebuild build → xcrun simctl install/launch
splash run emulator     # ./gradlew :app:installDebug → adb shell am start
```

See the `ios-native` and `android-native` preset scaffolds (`splash init ios-native` /
`splash init android-native`) for the standalone-project recipes; the entries above are the
same fields, merged into a monorepo recipe.

---

## docker-compose services

Splashdown does not patch `docker-compose.yml` / `compose.yaml`. Containerized services
expose ports via the compose `ports:` mapping (host side), not via env vars that splashdown
manages. The right pattern is:

1. Pick a stable host-port range per service in `compose.yaml`:

   ```yaml
   services:
     db:
       image: postgres:16
       ports: ["5433:5432"]   # host 5433 → container 5432
   ```

2. Reference that fixed port in `splashdown.toml` as a template — so worktrees still get
   distinct values if you run multiple stacks simultaneously:

   ```toml
   [resources.DATABASE_URL]
   type     = "template"
   template = "postgres://localhost:5433/myapp_{{ slug(cwd) }}"
   ```

   Or, if you do need compose's host port to vary per worktree, generate it yourself and
   pass it into compose via an env file — but compose reads `.env` at the project level, not
   `splashdown.env`, so you'd need a `writer = "envfile=.env"` resource and care about
   variable name collisions with the rest of your `.env`.

Guidance: keep compose port mappings fixed and let the `DATABASE_URL` (or equivalent) carry
the per-checkout uniqueness through the slug or hash helpers.

---

## Gotchas

**Vite's `loadEnv` doesn't see `splashdown.env`.** Vite's `loadEnv(mode, dir, "")` reads
`.env.*` files from `dir`, not the parent shell environment. Values in `splashdown.env`
(loaded by mise/direnv/devbox) are invisible to Vite unless the config reads
`process.env.WEB_DEV_PORT` directly. `splash doctor --fix` rewrites `env.X` → `process.env.X`
automatically. See [framework-wiring.md](framework-wiring.md) for details.

**`writer = "envfile=..."` breaks the mise / direnv contract.** Per-resource
`writer = "envfile=apps/web/.env"` routes a value directly into an app-level `.env` file
instead of `splashdown.env`. This is the right escape hatch when a build tool can only read
dotenv files (legacy Gradle setup, vendor tooling), but it means mise/direnv/devbox never
see that value in the parent shell — any process that needs it must read the `.env` file
directly. Prefer patching the consumer to read `process.env` so all values stay in
`splashdown.env` and the full suite (`status`, `env get`, templated cross-references)
works as expected.

**`splash init --rescan` after adding an app.** When you add a new app to the monorepo,
run `splash init --rescan`. The rescanner updates `[project]` and `[apps.*]` but preserves
the existing `[resources.*]` section verbatim, so your hand-authored resource names survive
the rescan.

**`apps.*` with empty `resources = []` is intentional.** Native apps (`ios-native`,
`android-native`) allocate no port resources — they use simulator/emulator targets, not
ports. An `[apps.ios]` entry with `resources = []` is correct and expected; it tells
`splash run` which framework to use for the build and launch, even though no env vars are
managed for it.
