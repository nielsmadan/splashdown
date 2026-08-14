"""Intent preset templates for `splash init <preset>`.

Framework-derived recipes come from scanner-driven init. These complete recipes cover
choices project inspection cannot infer safely.
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

_SERVER_SCAFFOLD = """\
# splashdown.toml — generic web/server preset for anything that reads PORT from the
# environment. Allocates a free PORT per checkout and a unique DATABASE_URL so
# worktrees don't clobber each other's databases.

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

SCAFFOLDS: dict[str, str] = {
    "minimal": _MINIMAL_SCAFFOLD,
    "server": _SERVER_SCAFFOLD,
    "electron": _ELECTRON_SCAFFOLD,
}
