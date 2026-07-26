# The recipe: `splashdown.toml`

The committed file. Four kinds of top-level tables: `[project]`, `[apps.*]`, `[resources.*]`, and (for mobile) `[targets.*]`. The scanner produces a working version. Edit freely.

```toml
[project]
workspace = "pnpm"             # single | pnpm | yarn | npm | cargo | gradle
loader    = "mise"             # mise | direnv | devbox | none

[apps.api]
path      = "apps/api"
profile   = "node-backend"     # vite | nextjs | node-backend | django | fastapi |
                               # springboot | react-native | expo | flutter |
                               # ios-native | android-native | unknown
resources = ["PORT"]

[apps.web-admin]
path      = "apps/web-admin"
profile   = "vite"
resources = ["WEB_DEV_PORT", "API_DEV_PORT"]

[resources.PORT]
type  = "port"
range = [9081, 9100]           # globally-coordinated lowest-free

[resources.WEB_DEV_PORT]
type  = "port"
range = [5174, 5200]

[resources.API_DEV_PORT]
type     = "template"
template = "{{ PORT }}"        # Vite's /api proxy must hit the api's actual port
```

Resource types: `port`, `uuid`, `template`, `cwd`, `cwd-slug`, `set`.
Template scope: `cwd`, `cwd_abs`, `branch`, `repo`, `parent`, `basename`, `dirname`, `slug`, `lower`, `upper`, `truncate`, `uuid`, `hash`, `port_hash`, plus prior resolved resources.

Templates are derived values and re-render on every sync. Referenced resource changes therefore propagate immediately. For a stable generated component, declare it separately as `type = "uuid"` and reference that resource from the template; calling `uuid()` directly in a template creates a new value on every sync.

`set` resources hold manually supplied values:

```toml
[resources.API_TOKEN]
type = "set"
# default = "local-development-token"   # optional
```

Set one with `splash env set API_TOKEN=VALUE`. The command requires the resource to be declared as `type = "set"`; it rejects missing or malformed recipes, undeclared keys, and generated or allocated resource types. Without a default, sync exits 1 until a value is set. Manual values persist across syncs, including `--force`; `splash env release API_TOKEN` clears one.

A common pattern for consumers that need a stable short identifier (e.g. Docker Compose project names have a practical length limit):

```toml
[resources.COMPOSE_PROJECT_NAME]
type     = "template"
template = "myapp-test-{{ truncate(hash(cwd_abs), 8) }}"
# → "myapp-test-352e9e09" — stable per checkout path, 8-char truncated SHA256
```

Optional setup blocks run explicitly through `splash sync --setup NAME`:

```toml
[setup.dev]
run = [
  "docker compose up -d",
  "python manage.py migrate",
]
```

`run` accepts one command string or a non-empty array of strings. Commands run sequentially from the checkout root with resolved resources added to their environment. The requested setup name must exist; an unknown name or malformed `run` value exits 1. Execution stops at the first failed command and also exits 1. Resource allocation and output-file writes happen before setup starts and are not rolled back if a command fails.

**For mobile**, the recipe also declares a `[targets.*]` catalog: the simulator and emulator variants the team agrees this project supports. Sim *instances* are created lazily per checkout, named `<parent>/<cwd>/<variant>`. With `ios = "latest"` (the default), the sim is auto-recreated whenever a newer iOS lands. Pin an explicit version like `ios = "18.5"` for fixed coverage.

```toml
[targets.simulator.default]
model = "iPhone 17"

[targets.simulator.lowest-supported]
model = "iPhone 12"
ios   = "17.0"

[targets.emulator.default]
device = "pixel_9"
```

For a **plugged-in phone**, declare a `device` target (or just rely on auto-pick). Unlike sims/emulators, splashdown doesn't create or own physical hardware, it discovers what's connected and hands the native id to the launcher. All fields are optional. With one device connected, no config is needed at all.

```toml
[targets.device.default]
# platform = "ios"        # scope auto-pick to one platform: "ios" | "android"
# name     = "My iPhone"  # match by device name
# id       = "..."        # exact udid / adb serial
```

Target types: `simulator`, `emulator`, `device`.

For per-checkout variants layered on top of this recipe, see [Per-checkout overrides](overrides.md).
