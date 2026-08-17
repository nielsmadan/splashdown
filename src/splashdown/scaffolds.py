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
template = "postgres://localhost:5432/myapp_{{ slug(cwd) }}_{{ truncate(hash(cwd_abs), 8) }}"

# Add extra ports as needed, e.g.:
# [resources.STORYBOOK_PORT]
# type  = "port"
# range = [6007, 6100]
"""

_ELECTRON_SCAFFOLD = """\
# splashdown.toml — Electron preset.
# PORT isolates the renderer dev server. ELECTRON_PROFILE_ID lets the main
# process derive an independent sibling of Electron's normal userData path.

[project]
workspace = "single"
loader = "__SPLASH_LOADER__"

[resources.PORT]
type  = "port"
range = [3001, 3100]

[resources.ELECTRON_PROFILE_ID]
type     = "template"
template = "splashdown-{{ truncate(hash(cwd_abs), 12) }}"
writer   = "splashdown-env"
"""

SCAFFOLDS: dict[str, str] = {
    "minimal": _MINIMAL_SCAFFOLD,
    "server": _SERVER_SCAFFOLD,
    "electron": _ELECTRON_SCAFFOLD,
}
