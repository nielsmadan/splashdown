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
