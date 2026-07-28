# The recipe: `splashdown.toml`

The committed file. Its top-level sections are `[project]`, `[apps.*]`, `[resources.*]`, `[targets.*]` (for mobile), and `[setup.*]`. The scanner produces a working version.

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

Each resource type has a small, strict shape:

| Type | Fields |
| --- | --- |
| `port` | Required `range = [LO, HI]`, where both values are integers and `1 <= LO <= HI <= 65535` |
| `template` | Required string `template` |
| `set` | Optional string `default` |
| `uuid`, `cwd`, `cwd-slug` | No type-specific fields |

Every resource also accepts the optional `writer` field. Fields belonging to another resource type are errors.

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

The same pattern gives every checkout its own database inside one shared Postgres container, with no new resource type. A database name needs no machine-wide coordination the way a port does, so a plain function of the checkout directory is enough and stays stable across reallocations:

```toml
[resources.DB_NAME]
type     = "template"
template = "myapp_{{ slug(cwd) }}"
writer   = "envfile=apps/api/.env"
```

Three things to know before you add it:

1. `slug()` lowercases and turns every non-alphanumeric run into a hyphen, so `myapp_{{ slug(cwd) }}` on a checkout named `myapp.feat-x` gives `myapp_myapp-feat-x`, mixing underscores and hyphens. That is safe only if whatever creates the database quotes the identifier. Use `lower(...)` plus `truncate(hash(cwd_abs), 8)` instead if you need a stricter character set.
2. The first sync takes over any hand-set `DB_NAME=` line already in `apps/api/.env` and replaces it with the computed value. Other keys in that file are left alone. Check the file before you add the resource.
3. There is no per-checkout exception. The resource applies to every checkout including your primary one, so the base database from your compose file simply goes unused there. You cannot express "compute this only in worktrees".

Splashdown writes the name. Creating the database is your app's job, typically a `CREATE DATABASE IF NOT EXISTS`-style step on first connect, or a `[setup.*]` block.

Optional setup blocks run explicitly through `splash sync --setup NAME`:

```toml
[setup.dev]
run = [
  "docker compose up -d",
  "python manage.py migrate",
]
```

`run` accepts one non-empty command string or a non-empty array of non-empty strings. It is the only field accepted in a setup block. Commands run sequentially from the checkout root with resolved resources added to their environment. The requested setup name must exist. Execution stops at the first failed command and exits 1. Resource allocation and output-file writes happen before setup execution starts and are not rolled back if a command fails.

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

Target types and their compatible fields:

| Type | Optional fields |
| --- | --- |
| `simulator` | `model`, `ios`, `name` |
| `emulator` | `device`, `image`, `name` |
| `device` | `id`, `name`, `platform` (`ios` or `android`) |

All supplied target values must be non-empty strings. A field for the wrong type, such as `image` on a simulator, is an error.

## Validation

Splashdown validates the complete recipe whenever it loads it. Unknown sections or fields, wrong value types, unknown workspace/loader/profile names, malformed targets, and invalid resource definitions are hard errors. `[apps.NAME]` must contain `path`, `profile`, and a unique `resources` list whose entries are declared under `[resources]`.

`[project]` accepts `workspace`, `loader`, `framework`, `run`, `ios`, and `android`. `run` is either one non-empty command string or a table containing `ios` and/or `android` commands. The `ios` table accepts `scheme`, `mode`, `configuration`, `workspace`, and `project`; the `android` table accepts `mode`, `module`, `variant`, `application_id`, and `launch_activity`. All supplied leaf values are non-empty strings. App profiles use a built-in profile name or `unknown`.

Templates are also checked up front: expressions must use the documented restricted syntax, every referenced name must exist, and resource dependency cycles are rejected. Validation finishes before registry allocation or generated-file updates, so a mistake anywhere in the document cannot leave a partially provisioned checkout. Errors identify the source and qualified field, for example `splashdown.toml: [resources.PORT.range] ...`.

For per-checkout variants layered on top of this recipe, see [Per-checkout overrides](overrides.md).
