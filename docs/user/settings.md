# Settings

Behavior toggles live in a `[settings]` table. Two places can set them, highest priority first:

1. **Per-checkout**: `[settings]` in this checkout's `splashdown.local.toml` (gitignored).
2. **Machine-wide**: `~/.config/splashdown/config.toml` (honors `$XDG_CONFIG_HOME`).

A per-checkout value wins over the global one, which wins over the built-in default.

```toml
# ~/.config/splashdown/config.toml — applies to every checkout on this machine
[settings]
prefix_match = false   # default true
```

| Setting | Default | Effect |
| --- | --- | --- |
| `prefix_match` | `true` | Resolve abbreviated `type`/`variant` args for `splash run`/`start`/`stop`/`destroy` by unique prefix (`splash run sim` → `simulator`). Off = exact names only. |

Unknown keys or wrong value types in a `[settings]` table are a hard error, so a typo never silently no-ops.
