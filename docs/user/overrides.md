---
title: Per-checkout overrides
description: Add local and machine-wide target variants without changing the committed recipe.
---

# Per-checkout overrides: `splashdown.local.toml`

A **gitignored**, per-checkout file. Use it to **add** extra target variants on top of what the recipe declares (never to override or repeat). Each checkout has its own copy. What you add here is local to this worktree/clone.

```toml
# Reproduce a bug only this checkout sees:
[targets.simulator.repro-bug]
model = "iPhone 16"
ios   = "17.5"
```

Name collisions with a recipe-declared variant are an error (pick a different variant name). Add programmatically with:

```sh
splash target add simulator repro-bug --model="iPhone 16" --ios=17.5
```

The local file can also carry a `[settings]` block. See [Settings](settings.md).

`settings` and `targets` are the only top-level sections accepted in this file. Target fields are type-specific: simulators accept `model`, `ios`, and `name`; emulators accept `device`, `image`, and `name`; physical devices accept `id`, `name`, and `platform`. All are optional, but supplied values must be non-empty strings, and `platform` must be `ios` or `android`. The same rules apply to `splash target add`, so an incompatible flag is rejected before the file is changed.

## Machine-wide test devices

To reuse the same devices across every project without re-declaring them per repo, put `[targets.*]` variants in the machine-wide `~/.config/splashdown/config.toml` (honors `$XDG_CONFIG_HOME`). This is aimed at **physical test devices** you carry from project to project.

```toml
# ~/.config/splashdown/config.toml — available in every checkout
[targets.device.my-iphone]
platform = "ios"
name     = "Niels's iPhone"

[targets.device.my-pixel]
platform = "android"
```

Or via CLI (the `--global` flag writes here instead of the local file):

```sh
splash target add device my-iphone --platform=ios --name="Niels's iPhone" --global
splash target remove device my-iphone --global
```

How global variants surface in a project:

- **Physical `device` variants are available in every project** — even one that declares no targets. `splash run` in any repo resolves your global device (it matches connected hardware; nothing is created). This is the main use case.
- **`simulator` / `emulator` variants only surface in projects that already declare that target type** — a global simulator never adds device support to a backend repo.
- **A project's own recipe/local variant always wins** a name collision with a global one, silently. `splash target` shows the source (`global`, or `recipe (shadows global)` for the winner) so you can tell what's in effect.

The same file holds machine-wide `[settings]`. See [Settings](settings.md).

The global file has the same strict document shape as the local file: only `settings` and `targets` are accepted, and target variants use the same type-specific fields. Both files are validated completely when read. Unknown fields or malformed later sections are hard errors before splashdown allocates resources or changes generated project files.
