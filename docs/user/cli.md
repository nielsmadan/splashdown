# CLI reference

```
splash                              # sync this checkout (the post-checkout hook runs this)
splash --version
splash sync [--force] [--setup N]   # pick free ports, resolve vars, write splashdown.env
splash status [all]                 # resources + targets + which ports are bound right now
splash init [preset] [--rescan] [--no-sync] [--loader=…] [--overwrite]   # scaffold + first sync
splash doctor [--fix] [--framework=…]

splash run     [type] [variant]     # boot target + build + launch
splash start   [type] [variant]     # boot target (no build/launch)
splash stop    [type] [variant]     # shut down
splash destroy [type] [variant]     # delete this checkout's target instance

splash target                       # list declared targets + live state
splash target add/remove <type> <variant> …
splash target refresh [ios|android] # recreate stale sims/emulators
splash target prune   [ios|android] # destroy sims/emulators splashdown didn't create

splash env                          # list this checkout's resolved values
splash env get KEY | set KEY=VALUE | release [KEY]

splash gc                           # drop dead-checkout entries (ports, vars, sims)

splash completion [bash|zsh]        # print shell-completion script (eval it in your rc)
```

`splash status` answers "what's the state of this checkout?": resolved env vars (with `[in use]` / `[free]` for port-typed resources), declared device variants and whether each is booted, and a count of stale registry rows. `splash sync --force` reallocates ports. The auto-reallocation lives in `Registry.allocate_port`, so plain `splash` does the same thing. `splash init` scans the project, scaffolds the project files, and then runs the first sync so the current checkout has values immediately (`--no-sync` scaffolds only). Use plain `splash init` for framework-detected projects and Compose infrastructure. `splash init --rescan` re-scans the filesystem, useful after adding a new app to a monorepo.

Named presets are limited to choices that project scanning cannot infer:

- `minimal` creates a framework-neutral recipe with a generated run id.
- `server` creates a generic `PORT` and checkout-specific `DATABASE_URL`.
- `electron` creates a renderer `PORT` and opts into checkout-specific Electron user data.

`splash sync --setup NAME` runs the recipe's `[setup.NAME]` commands after resolving and writing resources. Empty or malformed setup declarations fail during recipe validation, before those changes. An unknown requested name or failed command exits 1 after provisioning; resource/output changes and earlier successful setup commands are not rolled back.

`splash env set KEY=VALUE` only accepts keys declared with `type = "set"` in the target checkout's recipe. It rejects invalid assignments, missing or malformed recipes, undeclared keys, and generated or allocated resources with exit 2.

Commands that load configuration validate the complete document before provisioning or project-file mutation. Unknown sections and fields, wrong types, invalid templates, and malformed target definitions exit 1 with a qualified error and no traceback.

`splash target add` applies the same target schema as TOML files before writing: `simulator` accepts `--model`, `--ios`, and `--name`; `emulator` accepts `--device`, `--image`, and `--name`; physical `device` accepts `--id`, `--name`, and `--platform=ios|android`. Supplying a flag for the wrong target type is an error and leaves the local or global config unchanged.
