---
title: Monorepos
description: Configure splashdown for JavaScript workspaces, mobile apps, native projects, and Compose services.
---

# Monorepos

Setting up splashdown in a multi-app workspace (JS workspaces, React Native / Expo, polyglot
native + web, and docker-compose services) and how `splash init` handles each. See
[The recipe](recipe.md) for the full schema.

## How splash init handles monorepos

`splash init` is *honest*, not *smart*. It auto-scaffolds simple repos where every app maps
unambiguously to a distinct resource name. It *defers* when it detects a collision (for
example, two apps that both want a resource named `PORT`), writing a structure-only recipe
(`[project]` + `[apps.*]` with empty `resources = []` per app) and printing:

```
monorepo detected (N apps) — resources not auto-configured; see https://splashdown.dev/monorepos/
```

That message points here. The patterns below are the copy-pasteable recipes to reach for
when init defers. Hand-author the `[resources.*]` section with distinct env names per app.
The scanner-produced `[project]` and `[apps.*]` sections are already correct.

---

## Electron alongside a renderer

Electron is detected as a secondary capability. An Electron app that uses Vite, Next.js, or
another renderer keeps that renderer's profile and resources. Plain `splash init` then asks once
whether to isolate Electron user data per checkout. The default is No. Non-interactive input and
EOF also select No. Use `--electron-profile=isolated|shared` to make the choice explicit in
automation.

Choosing Yes gives each detected Electron app a stable profile identifier:

```toml
[resources.ELECTRON_PROFILE_ID]
type = "template"
template = "splashdown-{{ truncate(hash(cwd_abs), 12) }}"
writer = "splashdown-env"
```

If more than one workspace app uses Electron, the resource names are mangled per app, such as
`ELECTRON_PROFILE_ID_DESKTOP`, and each value also includes the app name. Each `[apps.*]`
entry lists its matching resource. The identifier always stays in `splashdown.env`, even when a
renderer resource is routed to an app-specific dotenv file.

Init prints the matching main-process integration for each app:

```js
import { mkdirSync } from "node:fs"

const profileId = process.env.ELECTRON_PROFILE_ID_DESKTOP
if (profileId) {
  const userData = `${app.getPath("userData")}-${profileId}`
  mkdirSync(userData, { recursive: true })
  app.setPath("userData", userData)
}
```

Set `userData` before calling `requestSingleInstanceLock()`. The derived directory remains next
to Electron's normal platform-specific profile rather than in the checkout. If init defers to a
structure-only recipe because the monorepo is ambiguous, it does not prompt or add these resources.
Add the template resources manually once you have assigned distinct names.

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
range = [3001, 3100]

[resources.API_PORT]
type = "port"
range = [9000, 9100]
```

**Root orchestrator wiring.** `next dev` reads `PORT`. `nest start` also reads `PORT`.
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

!!! warning "Keep CORS origins on the allocated web port"
    If the backend validates `CORS_ORIGINS`, a static `http://localhost:3000` or
    `http://localhost:5173` fails as soon as splashdown allocates another port. Include the
    actual browser origin, or declare `CORS_ORIGINS` as a template that references the web
    resource, for example `http://localhost:{{ WEB_PORT }}`. A dev proxy hides this during
    normal `/api/` calls, but direct browser requests to the backend still enforce CORS.

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
range = [8082, 8200]

[resources.WEB_PORT]
type = "port"
range = [3001, 3100]

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
place with the right tool (see [Custom run command](devices.md#custom-run-command)):

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
resources = []

[apps.android]
path = "android"
profile = "android-native"
resources = []

[resources.WEB_PORT]
type = "port"
range = [3001, 3100]

[resources.API_PORT]
type = "port"
range = [9000, 9100]

[targets.simulator.default]
model = "iPhone 17"

[targets.emulator.default]
device = "pixel_9"
```

**What each native app needs in addition.** If you drive an `ios-native` app with
`splash run simulator`, `[project.ios] scheme` is required. Plain `splash init` records the only
shared Xcode scheme automatically and asks when several schemes exist. In a non-interactive
setup, pass `splash init --ios-scheme=MyApp`. The resulting configuration is:

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

For a standalone native project, run plain `splash init`. It detects a root Xcode workspace or
project, or root Gradle build files, and emits the relevant native profile and default target.
Native directories inside a JS workspace are not auto-claimed, so use the manual app and project
configuration above for that layout.

---

## docker-compose services

Splashdown never rewrites `compose.yaml` / `docker-compose.yml`. It ships no YAML parser, and
rewriting a format with significant whitespace by regex is not safe enough to do to your files.
What it does instead is allocate the values and tell you exactly which lines to change.

When a compose file sits at the repo root, `splash init` adds a `COMPOSE_PROJECT_NAME` resource:

```toml
[resources.COMPOSE_PROJECT_NAME]
type     = "template"
template = "{{ slug(parent) }}-{{ slug(cwd) }}"
```

That one variable does most of the work. Compose reads it from the environment and uses it to
namespace containers, networks and volumes, so two worktrees of the same repo stop colliding
without either compose file changing. Your loader already exports it on `cd`, so a plain
`docker compose up` picks it up.

Host ports still need an edit, because compose bakes them into the `ports:` mapping. Declare a
resource per published port and reference it with compose's `${VAR:-default}` form:

```toml
[resources.DB_PORT]
type  = "port"
range = [5433, 5500]
```

```yaml
services:
  db:
    image: postgres:16
    ports:
      - "${DB_PORT:-5432}:5432"
```

Drop `container_name:` while you are there. A literal container name defeats
`COMPOSE_PROJECT_NAME`, since it pins the container to one name across every checkout.

`splash doctor` reports what is still hardcoded:

```
✗  compose-hardcoded-ports: compose.yaml hardcodes host ports 5432, 6379; container_name myapp_db
```

Plain `splash init` detects the Compose file and declares `COMPOSE_PROJECT_NAME`
automatically. Add `DB_PORT` yourself when the database needs a pinned host port, as in the
example above.

Splashdown does not allocate a port per service automatically. Which services deserve a pinned
host port is a judgement call, and inventing resource names for every mapping in the file would
produce config you then have to undo.

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
see that value in the parent shell. Any process that needs it must read the `.env` file
directly. Prefer patching the consumer to read `process.env` so all values stay in
`splashdown.env` and the full suite (`status`, `env get`, templated cross-references)
works as expected.

**`splash init --rescan` after adding an app.** When you add a new app to the monorepo,
run `splash init --rescan`. The rescanner updates `[project]` and `[apps.*]` but preserves
the existing `[resources.*]` section, so your hand-authored resource names and comments
survive the rescan. Every field there is validated against the schema, so an unknown key
is an error rather than data the rescan carries forward.

**`apps.*` with empty `resources = []` is intentional.** Native apps (`ios-native`,
`android-native`) allocate no port resources, they use simulator/emulator targets, not
ports. An `[apps.ios]` entry with `resources = []` is correct and expected. It tells
`splash run` which framework to use for the build and launch, even though no env vars are
managed for it.
