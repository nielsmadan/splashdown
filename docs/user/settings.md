---
title: Settings
description: Configure splashdown command behavior and machine-wide target defaults.
---

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

Unknown keys or wrong value types in a `[settings]` table are a hard error, so a typo never silently no-ops. `splashdown.local.toml` and the global `config.toml` accept only `[settings]` and `[targets.*]`; the whole file is validated when read, before provisioning or generated-file changes.

The machine-wide `config.toml` can also hold `[targets.*]` variants shared across every project — see [machine-wide test devices](overrides.md#machine-wide-test-devices).
