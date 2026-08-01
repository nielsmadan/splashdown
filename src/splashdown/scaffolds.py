"""Preset `splashdown.toml` templates for `splash init <preset>`.

Pure data — no imports, no logic. Deliberately decoupled from `PROFILES`: some presets
(minimal, electron, server) have no detectable framework, and several profiles (vite,
django, springboot, ...) have no stock scaffold because scanner-driven `splash init`
covers them without one.
"""

from __future__ import annotations

_MINIMAL_SCAFFOLD = """\
# splashdown.toml — minimal preset. One uuid slot; no apps, no devices.

[project]
workspace = "single"
loader = "__SPLASH_LOADER__"

[resources.RUN_ID]
type = "uuid"
"""
_RN_SCAFFOLD = """\
# splashdown.toml — React Native preset.

[project]
workspace = "single"
loader = "__SPLASH_LOADER__"

[apps.main]
path = "."
profile = "react-native"
resources = ["RCT_METRO_PORT"]

[resources.RCT_METRO_PORT]
type  = "port"
range = [8082, 8200]

# Uncomment to pick the Xcode scheme / build mode `splash run simulator` builds.
# Needed when your dev environment is scheme-selected (e.g. a *Dev scheme that
# copies .env.development); without it RN CLI builds the project-name scheme.
# [project.ios]
# scheme = "MyAppDev"    # -> react-native run-ios --scheme
# mode   = "Debug"       # -> react-native run-ios --mode (optional)
# [project.android]
# mode   = "developmentDebug"  # -> react-native run-android --mode (optional)

[targets.simulator.default]
model = "iPhone 17"
# ios = "latest"   # implicit; auto-recreate when a newer iOS lands. Pin to e.g.
                   # "18.5" if you want a fixed version that never upgrades. Some
                   # apps (a pod excluding arm64 for the simulator, e.g. Google ML
                   # Kit) can only build on an x86_64 sim — pin ios = "18.5".

[targets.emulator.default]
device = "pixel_9"

# Run on a plugged-in phone with `splash run device`. With one device
# connected, auto-pick resolves it — no config needed. Uncomment to pin a
# specific device by id/name, or to scope auto-pick to one platform.
# [targets.device.default]
# platform = "ios"        # optional: "ios" | "android"
# name     = "My iPhone"  # optional: match by device name
# id       = "..."        # optional: exact udid / adb serial
"""
_FLUTTER_SCAFFOLD = """\
# splashdown.toml — Flutter preset.
# Flutter's `flutter run` auto-assigns the Dart VM / DevTools port on each
# launch; there is no equivalent of RN's RCT_METRO_PORT to pin. Splashdown's
# value for Flutter is per-checkout sim/emulator naming.

[project]
workspace = "single"
loader = "__SPLASH_LOADER__"

[apps.main]
path = "."
profile = "flutter"
resources = []

[targets.simulator.default]
model = "iPhone 17"

[targets.emulator.default]
device = "pixel_9"

# Run on a plugged-in phone with `splash run device`. With one device
# connected, auto-pick resolves it — no config needed. Uncomment to pin a
# specific device by id/name, or to scope auto-pick to one platform.
# [targets.device.default]
# platform = "ios"        # optional: "ios" | "android"
# name     = "My iPhone"  # optional: match by device name
# id       = "..."        # optional: exact udid / adb serial
"""
_SERVER_SCAFFOLD = """\
# splashdown.toml — generic web/server preset for anything that reads PORT from the
# environment (Next.js, Django, FastAPI, Express, Spring Boot, etc.; Rails, Flask,
# Laravel and ASP.NET Core have their own presets because they read a different
# variable). Allocates a free PORT per checkout and a unique DATABASE_URL
# so worktrees don't clobber each other's databases.

[project]
workspace = "single"
loader = "__SPLASH_LOADER__"

[resources.PORT]
type  = "port"
range = [3001, 3100]

[resources.DATABASE_URL]
type     = "template"
template = "postgres://localhost:5432/myapp_{{ slug(cwd) }}"

# Add extra ports as needed, e.g.:
# [resources.STORYBOOK_PORT]
# type  = "port"
# range = [6007, 6100]
"""
_ELECTRON_SCAFFOLD = """\
# splashdown.toml — Electron preset.
# Two per-checkout collisions to solve for parallel Electron dev:
#   1. PORT — the renderer dev server (Vite / Webpack / Parcel / etc.).
#   2. ELECTRON_USER_DATA_DIR — Electron's userData path. By default every
#      instance reads/writes ~/Library/Application Support/<productName>; when
#      two checkouts run side by side they clobber each other's settings,
#      IndexedDB, and SingleInstanceLock. Wire your main process to honour the
#      env var (early, before app.whenReady()):
#         if (process.env.ELECTRON_USER_DATA_DIR) {
#           app.setPath('userData', process.env.ELECTRON_USER_DATA_DIR)
#         }

[project]
workspace = "single"
loader = "__SPLASH_LOADER__"

[resources.PORT]
type  = "port"
range = [3001, 3100]

[resources.ELECTRON_USER_DATA_DIR]
type     = "template"
template = "{{ cwd_abs }}/.electron-userdata"
"""
_IOS_NATIVE_SCAFFOLD = """\
# splashdown.toml — Native iOS preset (Swift/Obj-C + xcodebuild).

[project]
workspace = "single"
loader = "__SPLASH_LOADER__"

[project.ios]
# Required: the Xcode scheme to build.
scheme = "MyApp"
# Optional, defaults shown:
# configuration = "Debug"
# workspace     = "MyApp.xcworkspace"  # auto-detected from root if absent
# project       = "MyApp.xcodeproj"    # auto-detected from root if absent

[apps.main]
path = "."
profile = "ios-native"
resources = []

[targets.simulator.default]
model = "iPhone 17"
"""
_ANDROID_NATIVE_SCAFFOLD = """\
# splashdown.toml — Native Android preset (Kotlin/Java + Gradle).

[project]
workspace = "single"
loader = "__SPLASH_LOADER__"

[project.android]
# Optional, defaults shown:
# module          = "app"
# variant         = "debug"
# application_id  = "com.example.myapp"  # asked from Gradle if not set
# launch_activity = ".MainActivity"      # uses LAUNCHER intent if not set

[apps.main]
path = "."
profile = "android-native"
resources = []

[targets.emulator.default]
device = "pixel_9"
"""
_ASTRO_SCAFFOLD = """\
# splashdown.toml — Astro preset. Astro does not read PORT from the environment,
# so astro.config must consume WEB_DEV_PORT itself:
#     server: { port: Number(process.env.WEB_DEV_PORT) || 4321 }
# `splash doctor --fix` writes that line for you.

[project]
workspace = "single"
loader = "__SPLASH_LOADER__"

[apps.main]
path      = "."
profile   = "astro"
resources = ["WEB_DEV_PORT"]

# Skips Astro's own 4321 so an unwired checkout can't look wired.
[resources.WEB_DEV_PORT]
type  = "port"
range = [4322, 4400]
"""
_COMPOSE_SCAFFOLD = """\
# splashdown.toml — docker-compose preset. COMPOSE_PROJECT_NAME namespaces this
# checkout's containers, networks and volumes so parallel worktrees don't collide.
# Reference the ports from compose with the ${VAR:-default} form:
#     ports:
#       - "${DB_PORT:-5432}:5432"
# and drop `container_name:` so COMPOSE_PROJECT_NAME can do its job.
# Run `docker compose up` in a shell your loader has populated.

[project]
workspace = "single"
loader = "__SPLASH_LOADER__"

[resources.COMPOSE_PROJECT_NAME]
type     = "template"
template = "{{ slug(parent) }}-{{ slug(cwd) }}"

[resources.DB_PORT]
type  = "port"
range = [5433, 5500]

# Add one entry per service that publishes a host port, e.g.:
# [resources.REDIS_PORT]
# type  = "port"
# range = [6380, 6450]
"""

_RAILS_SCAFFOLD = """\
# splashdown.toml — Ruby on Rails preset. `rails server` reads PORT straight from
# the environment (railties' server_command, and the generated config/puma.rb does
# `port ENV.fetch("PORT", 3000)`), so no config patching is needed.

[project]
workspace = "single"
loader = "__SPLASH_LOADER__"

[apps.main]
path      = "."
profile   = "rails"
resources = ["PORT", "DATABASE_URL"]

# Skips Rails' own 3000 so an unwired checkout can't look wired.
[resources.PORT]
type  = "port"
range = [3001, 3100]

[resources.DATABASE_URL]
type     = "template"
template = "postgres://localhost:5432/myapp_{{ slug(cwd) }}"
"""

_FLASK_SCAFFOLD = """\
# splashdown.toml — Flask preset. The Flask CLI maps FLASK_<COMMAND>_<OPTION> env
# vars onto command options, so FLASK_RUN_PORT sets the port for `flask run`.
#
# Caveat: this only applies to `flask run`. A `python app.py` entrypoint calling
# app.run() hardcodes 5000 — pass the port through yourself in that case:
#     app.run(port=int(os.environ.get("FLASK_RUN_PORT", 5000)))

[project]
workspace = "single"
loader = "__SPLASH_LOADER__"

[apps.main]
path      = "."
profile   = "flask"
resources = ["FLASK_RUN_PORT", "DATABASE_URL"]

# Skips Flask's own 5000 so an unwired checkout can't look wired.
[resources.FLASK_RUN_PORT]
type  = "port"
range = [5001, 5100]

[resources.DATABASE_URL]
type     = "template"
template = "postgres://localhost:5432/myapp_{{ slug(cwd) }}"
"""

_LARAVEL_SCAFFOLD = """\
# splashdown.toml — Laravel preset. A Laravel app runs two dev servers that both
# collide across worktrees:
#   1. SERVER_PORT — `php artisan serve`. Read straight from the environment
#      (ServeCommand defaults the --port option to Env::get('SERVER_PORT')).
#   2. WEB_DEV_PORT — the Vite asset server, shipped with Laravel since 9.x.
#      Vite does NOT read this on its own; wire it in vite.config.js:
#          server: { port: Number(process.env.WEB_DEV_PORT) || 5173 }
#      `splash doctor` reports it if you forget.

[project]
workspace = "single"
loader = "__SPLASH_LOADER__"

[apps.main]
path      = "."
profile   = "laravel"
resources = ["SERVER_PORT", "WEB_DEV_PORT"]

# Skips Laravel's own 8000 so an unwired checkout can't look wired.
[resources.SERVER_PORT]
type  = "port"
range = [8001, 8100]

# Skips Vite's own 5173, likewise.
[resources.WEB_DEV_PORT]
type  = "port"
range = [5174, 5200]
"""

_ASPNETCORE_SCAFFOLD = """\
# splashdown.toml — ASP.NET Core preset.
#
# Requires .NET 8+: ASPNETCORE_HTTP_PORTS does not exist on net6.0/net7.0, which
# ignore it and fall back to 5000. On those target frameworks derive the URL form
# they do read instead:
#     [resources.ASPNETCORE_URLS]
#     type     = "template"
#     template = "http://localhost:{{ ASPNETCORE_HTTP_PORTS }}"
#
# Either way, `dotnet run` reads applicationUrl out of Properties/launchSettings.json
# and it WINS over the environment — the allocated port is ignored while that key is
# set. `splash doctor --fix` drops it from the "commandName": "Project" profiles.

[project]
workspace = "single"
loader = "__SPLASH_LOADER__"

[apps.main]
path      = "."
profile   = "aspnetcore"
resources = ["ASPNETCORE_HTTP_PORTS"]

# Skips .NET's own 5000/5001, and starts above vite's 5174-5200.
[resources.ASPNETCORE_HTTP_PORTS]
type  = "port"
range = [5201, 5300]
"""

_ANGULAR_SCAFFOLD = """\
# splashdown.toml — Angular preset.
#
# Angular reads NO environment variable for its dev-server port — only angular.json
# or `--port`. Writing the allocated port into the committed angular.json would churn
# it in every worktree, so the port is passed through the npm script instead:
#     "start": "ng serve --port $WEB_DEV_PORT"
# npm runs scripts through a shell, so the value your loader exports expands.
# `splash doctor --fix` rewrites the script for you.
#
# Caveat: this wires `npm start`. A bare `ng serve` still uses angular.json's default.

[project]
workspace = "single"
loader = "__SPLASH_LOADER__"

[apps.main]
path      = "."
profile   = "angular"
resources = ["WEB_DEV_PORT"]

# Skips Angular's own 4200 so an unwired checkout can't look wired.
[resources.WEB_DEV_PORT]
type  = "port"
range = [4201, 4300]
"""

_NUXT_SCAFFOLD = """\
# splashdown.toml — Nuxt preset. `nuxt dev` reads NUXT_PORT (and PORT) straight from
# the environment, so no config patching is needed. NUXT_PORT is used here rather than
# the generic PORT so a sibling backend in a monorepo can keep PORT for itself.

[project]
workspace = "single"
loader = "__SPLASH_LOADER__"

[apps.main]
path      = "."
profile   = "nuxt"
resources = ["NUXT_PORT"]

# Skips Nuxt's own 3000 so an unwired checkout can't look wired.
[resources.NUXT_PORT]
type  = "port"
range = [3001, 3100]
"""

_DENO_SCAFFOLD = """\
# splashdown.toml — Deno preset.
#
# Deno reads no PORT of its own: `deno serve` and `Deno.serve()` both bind 8000 no
# matter what the environment says. Something has to consume the value — either pass
# it from the deno.json task:
#     "dev": "deno serve --port $PORT --allow-net server.ts"
# (the flag must come BEFORE the script argument; anything after it is passed to the
# script, not to Deno), or read it where the server starts:
#     Deno.serve({ port: Number(Deno.env.get("PORT")) || 8000 }, handler)
# `splash doctor --fix` handles the `deno serve` task form; the code form is manual.

[project]
workspace = "single"
loader = "__SPLASH_LOADER__"

[apps.main]
path      = "."
profile   = "deno"
resources = ["PORT"]

# Skips Deno.serve's own 8000 so an unwired checkout can't look wired.
[resources.PORT]
type  = "port"
range = [8001, 8100]
"""

SCAFFOLDS: dict[str, str] = {
    "minimal": _MINIMAL_SCAFFOLD,
    "astro": _ASTRO_SCAFFOLD,
    "compose": _COMPOSE_SCAFFOLD,
    "react-native": _RN_SCAFFOLD,
    "rn": _RN_SCAFFOLD,
    "flutter": _FLUTTER_SCAFFOLD,
    "ios-native": _IOS_NATIVE_SCAFFOLD,
    "android-native": _ANDROID_NATIVE_SCAFFOLD,
    "electron": _ELECTRON_SCAFFOLD,
    "rails": _RAILS_SCAFFOLD,
    "flask": _FLASK_SCAFFOLD,
    "laravel": _LARAVEL_SCAFFOLD,
    "aspnetcore": _ASPNETCORE_SCAFFOLD,
    "angular": _ANGULAR_SCAFFOLD,
    "nuxt": _NUXT_SCAFFOLD,
    "deno": _DENO_SCAFFOLD,
    "server": _SERVER_SCAFFOLD,
    "nextjs": _SERVER_SCAFFOLD,  # historical alias for server
}
