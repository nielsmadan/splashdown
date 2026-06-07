# Per-Checkout Resource Provisioning — Design Spec

A declarative, transparent tool for automatically provisioning per-checkout machine-local resources (ports, UUIDs, simulator names, container names, DB names, etc.) whenever you `git checkout`, `git clone`, or `git worktree add`. No new CLI wrapper around git; users keep typing native `git` and `mise` commands.

## Problem

Working on multiple parallel checkouts of the same repo (worktrees or sibling clones) routinely produces resource collisions:

- Two worktrees both want Metro port `8081`.
- Two checkouts both want simulator name `myapp` or container name `myapp_postgres`.
- Two test suites both write to the same temp DB.

Per-repo committed config (e.g. `mise.toml` pinning `RCT_METRO_PORT = 8082`) doesn't fix this — both checkouts read the same value and collide again, one level up.

The actual constraint: each *checkout* (absolute path on this machine) needs unique values, but the *spec* of "what kinds of resources this repo needs" lives once in the repo and is committed.

## Design Principles

1. **Transparent**: users keep typing `git worktree add`, `git checkout`, `git clone`, `mise run <task>`. They never invoke this tool by name in day-to-day work.
2. **Declarative recipe**: a single committed file `.worktree.toml` declares *what* resources the repo needs (with types, ranges, templates), not *how* to fetch them.
3. **Lean on existing infra**: git's `post-checkout` hook + `core.hooksPath` + mise's `[tasks]` and `mise.local.toml` env loading. No daemon. No background process. No shell shim.
4. **Per-checkout state machine-local, never committed**: resource values live in a machine-local registry + a gitignored `mise.local.toml` per checkout. Committable artifact is the recipe only.
5. **Composable, not a replacement**: doesn't replace worktrunk, mise, or any allocator — sits as a hook between git and mise, calling existing tools where possible.

## The Transparent Flow

```
1. user: git worktree add ../app.feat feat       (or git clone, or git checkout -b)
2. git fires .githooks/post-checkout              (committed in the repo)
3. post-checkout: exec mise run wt-provision     (one line)
4. provisioner reads .worktree.toml, allocates,  
   templates, writes ./mise.local.toml           (gitignored)
5. user: cd ../app.feat                          (or git auto-cd's there)
6. mise activate picks up mise.local.toml,       (already hooked in .zshrc)
   exports all env vars
7. done. user runs their dev server.             (zero new commands typed)
```

`post-checkout` fires on `git checkout`, `git clone`, AND `git worktree add` — covers every "I just landed in a new working tree" case. `core.hooksPath` is stored in `.git/config`, shared across all worktrees of the repo, so once set in the main checkout, every worktree inherits it.

## Repo Layout (consumer-side)

```
~/repos/myapp/
├── .githooks/
│   └── post-checkout                # 3 lines: exec mise run wt-provision
├── mise.toml                        # committed; has [tasks.wt-provision] + presets
├── .worktree.toml                   # ← the recipe (committed)
├── .scripts/
│   └── worktree-provision.py        # provisioner (~150-250 lines, vendored in)
├── .gitignore                       # includes mise.local.toml
└── (your code...)

~/repos/myapp.feat-x/                # worktree, shares .git config
├── mise.local.toml                  # gitignored, this worktree's resolved vars
└── (your code...)
```

The provisioner is **vendored into the repo** by default rather than a global install. Reasons: (a) every contributor gets it on clone; (b) version is pinned by commit; (c) no global tool to keep updated. Optional global install for power users who want a single source of truth.

## Recipe Format — `.worktree.toml`

Declarative, typed resources with templates and cross-references.

```toml
# Each [resources.NAME] block declares one env var to provision.

[resources.RCT_METRO_PORT]
type  = "port"
range = [8081, 8200]              # allocator picks lowest free + persisted

[resources.STORYBOOK_PORT]
type  = "port"
range = [6006, 6100]

[resources.RUN_ID]
type = "uuid"                     # fresh uuid4(), persisted per checkout

[resources.SIM_NAME]
type     = "template"
template = "{{ basename(dirname(cwd)) }}/{{ basename(cwd) }}"

[resources.TEST_DB]
type     = "template"
template = "myapp_{{ basename(cwd) }}_{{ uuid()[:8] }}"

[resources.METRO_URL]
type     = "template"
template = "http://localhost:{{ RCT_METRO_PORT }}"   # cross-resource ref

# Optional lifecycle steps — extra side effects on provision.
[setup.flutter]
run = ["mksim", "mkavd"]          # called after env is written

[setup.rn]
run = ["mksim"]
```

### Resource types

| Type | Behavior | Persisted? |
|------|----------|------------|
| `port` | Lowest free port in `range`, taking into account both `lsof` (live) and the registry (pinned-elsewhere) | Yes — registry survives reboots; same checkout always gets same port |
| `uuid` | Fresh UUID4 on first provision; stable thereafter | Yes |
| `template` | Minijinja-style expansion with helpers (`cwd`, `branch`, `basename`, `dirname`, `uuid`, `slug`, other resource names) | Yes — re-evaluated only if recipe changes or `--reprovision` |
| `cwd` | Sugar for `template = "{{ cwd }}"` | Trivially |
| `cwd-slug` | Sanitized cwd basename (for container names, etc.) | Trivially |
| `set` | Manual value provided via `mise run set KEY=VALUE` | Yes |

### Template engine

A small minijinja-style engine. Variables and helpers in scope during template resolution:

- **Identity**: `cwd`, `cwd_abs`, `branch`, `repo`, `parent`
- **String**: `basename`, `dirname`, `slug`, `lower`, `upper`, `truncate(n)`
- **Generators**: `uuid()`, `uuid()[:n]`, `hash(...)`, `port_hash(...)` (deterministic fallback)
- **Cross-resource refs**: any previously-resolved `[resources.X]` is in scope by name. Resolution order = topological sort by reference graph.

### Writers (output targets)

Default writer is `mise` (writes to `./mise.local.toml` under `[env]`). Pluggable per resource:

```toml
[resources.STORYBOOK_PORT]
type   = "port"
range  = [6006, 6100]
writer = "envfile=.env.local"     # write to .env.local instead

[resources.SIM_NAME]
type     = "template"
template = "{{ basename(dirname(cwd)) }}/{{ basename(cwd) }}"
writer   = "stdout"                # just print; consumed by mksim/runsim
```

Built-in writers:

- `mise` (default) — append/update `mise.local.toml` `[env]` block, surgical (preserves user keys), auto `mise trust`
- `envfile=PATH` — KEY=VALUE lines into a file (default `.env.local`)
- `envrc` — append to `.envrc`/`.envrc.local`
- `stdout` — print; doesn't write anywhere
- `none` — registry-only; callers read via `provisioner get`

## Provisioner Script — `.scripts/worktree-provision.py`

Single Python file, stdlib-only (or minijinja optional). ~150-250 LOC.

### Responsibilities

1. **Parse `.worktree.toml`** (via `tomllib`).
2. **Read machine-local registry** at `~/.local/state/wt-provision/registry` (TSV: `port\tkey\tabspath` or similar; one file for ports, optionally separate files for other allocator types).
3. **Lazy GC**: when computing "busy" ports, ignore registry entries whose `abspath` no longer exists on disk. (This is how `git worktree remove` cleanup works without a hook — see Wrinkles below.)
4. **Topo-sort resources** by template cross-references.
5. **For each resource**, in order:
   - `port` → flock the registry, find lowest free in range (not in registry AND not in `lsof`/`netstat`), append entry, unlock.
   - `uuid` → check if already in registry under `(cwd, key)`; if yes recall, else `uuid.uuid4()` and persist.
   - `template` → expand using current scope.
6. **Validate** all resource names match `[A-Za-z_][A-Za-z0-9_]*` (env var shape).
7. **Group by writer**, invoke each writer with its resources.
8. **Run `[setup.*]` lifecycle steps** if recipe selects one (via `--preset=flutter` or auto-detect from a `[setup.default]` block).
9. **Print summary** (one line per resource, stderr; machine-readable on stdout via `--format=json`).

### CLI surface (invoked via mise tasks, rarely directly)

```
worktree-provision.py                  # default: provision per .worktree.toml
worktree-provision.py --reprovision    # force re-allocate all (regenerates UUIDs)
worktree-provision.py --gc             # explicit garbage-collect dead entries
worktree-provision.py --list           # show registry
worktree-provision.py --unpin [KEY]    # release this checkout's entries
worktree-provision.py --init           # scaffold .worktree.toml interactively
worktree-provision.py --init --preset=flutter   # flutter-flavored scaffold
worktree-provision.py get KEY          # echo current resolved value (machine-readable)
```

### Concurrency

`fcntl.flock` on the registry file for allocate / unpin paths. Lock-free for read-only ops (`--list`, `get`). Kernel releases the lock on process death so SIGKILL'd shells don't leave stale locks.

### TOML round-tripping

`tomlkit` (preserves comments, formatting) if available, falling back to a careful hand-rolled emitter that escapes `"`, `\`, newlines, and rejects unknown value types so unexpected `mise.local.toml` content can't be silently corrupted.

## Manual Commands — Just `mise` Tasks

The user-facing interface is `mise run <task>`. No new CLI. Tasks live in `mise.toml`:

```toml
[tasks.wt-provision]
description = "Provision per-worktree resources from .worktree.toml"
run = "python3 .scripts/worktree-provision.py"
silent = true

[tasks.setup]
description = "Scaffold .worktree.toml for this repo"
run = "python3 .scripts/worktree-provision.py --init"

[tasks."setup-flutter"]
description = "Scaffold Flutter recipe + boot simulator + boot emulator"
run = "python3 .scripts/worktree-provision.py --init --preset=flutter"

[tasks."setup-rn"]
description = "Scaffold React Native recipe + boot simulator"
run = "python3 .scripts/worktree-provision.py --init --preset=rn"

[tasks."wt-list"]
run = "python3 .scripts/worktree-provision.py --list"

[tasks."wt-unpin"]
run = "python3 .scripts/worktree-provision.py --unpin"
```

Presets ship a curated `.worktree.toml` skeleton for that stack (RN: Metro + sim name + DB; Flutter: sim + emulator; Next.js: dev port + Storybook + DB; etc.) plus any lifecycle hooks.

## The Two Wrinkles

### 1. First-time clone bootstrap (chicken-and-egg)

A repo's `.githooks/post-checkout` can't fire on the *first* `git clone` because `core.hooksPath` isn't set yet in the freshly-cloned `.git/config`. Three answers, choose per repo:

- **Repo `./setup` script** the user runs once after cloning. Sets `git config core.hooksPath .githooks` and runs the first provision. Most explicit, zero magic. *Default recommendation.*
- **Global git template directory** (`git config --global init.templateDir ...`) with `.githooks/post-checkout` pre-installed. Once set on the user's machine, every subsequent `git clone` automatically gets the hook. Strong-magic option for personal-dotfiles power users.
- **Postinstall via package manager**: tools like `lefthook`, `husky`, or just a `package.json` postinstall script. Requires a dependency install anyway, so the hook gets wired up "for free" on `npm install`. Best fit for JS/TS projects.

### 2. `git worktree remove` doesn't fire any hook

Confirmed git gap, longstanding. **Solution: lazy garbage collection inside the provisioner.** When computing the busy-port set, the provisioner skips registry entries whose absolute path no longer exists on disk. Result:

- Deleting a worktree (with `git worktree remove ../foo` or even `rm -rf ../foo`) silently frees its ports the next time anyone allocates.
- No wrapper around `git worktree remove`, no daemon, no cron, no user action needed.
- Cost: one `os.path.exists()` per registry entry during allocation, ~microsecond.

Optionally, `.githooks/post-checkout` also runs `worktree-provision.py --gc` on every invocation to keep the registry tidy on disk. Costs ~ms.

## Comparison to Existing Tools

| Tool | What it owns | Overlap |
|------|--------------|---------|
| **worktrunk** | git worktree lifecycle, hooks, deterministic `hash_port` filter, status dashboard | None — composes via `post-start` hook calling `mise run wt-provision` |
| **port-selector** | directory-keyed port registry, lowest-free allocator | The port-allocation primitive only. This spec swallows it into the provisioner (or shells out) |
| **react-native-worktree** | RN-specific worktree + Metro port + sim/device mutex | Overlap only for RN preset; this spec covers the same ground generically + supports other stacks |
| **Conductor / Emdash** | Mac apps for parallel agents; `setup`/`run`/`teardown` shell scripts | Different surface (GUI); composes if needed |
| **devcontainer.json** | One-container-per-workspace lifecycle | Different shape — assumes container isolation; doesn't solve host-port collisions |
| **mise** | Per-dir env loading, tasks, hooks | This spec's *host*. Built on top, never duplicates |
| **direnv** | Per-dir env loading via `.envrc` | Alternative host; `writer = "envrc"` makes the spec direnv-compatible |

The unique territory: **declarative typed resources with allocators + writers + cross-resource templates + transparent git-hook integration**. No tool covers this combination.

## Build Estimate

| Piece | LOC | Effort |
|-------|-----|--------|
| `worktree-provision.py` core (parser + registry + flock) | ~100 | half day |
| Allocator (port) | ~40 | 1-2 hours |
| Template engine + cross-resource resolution | ~50 | half day |
| Writers (mise, envfile, envrc, stdout) | ~60 | half day |
| `--init` / preset scaffolding | ~50 | 1-2 hours |
| Lifecycle hooks (`[setup.*]`) | ~30 | 1-2 hours |
| Tests (pytest) | ~200 | 1 day |
| README / spec / examples | — | half day |
| **Total** | **~500-700** | **2-3 days focused** |

Stdlib-only is achievable; optional deps for nicer DX: `tomlkit` (TOML round-trip), `minijinja` (richer templates).

## Open Questions

1. **Single tool or family?** Should sim/AVD provisioning (the `[setup.*]` lifecycle) live in this tool or stay in separate tools (`mksim`, `mkavd`) that the recipe just *calls*? Lean: call out, don't subsume. Keeps the core small.
2. **Recipe inheritance/composition** — should `.worktree.toml` support `extends = "..."` to layer presets? Useful for monorepos with multiple subprojects.
3. **Cross-worktree communication** — should worktrees be able to read each other's resources? E.g. one worktree exposing `STAGING_API_URL` for another to consume. Probably yes via `provisioner get --checkout=PATH KEY`.
4. **Reverse-proxy integration** — Galactic-style `client.feat-auth.localhost` routing. Possible feature: a `writer = "caddy"` or `writer = "traefik"` that writes routing config alongside the env var.
5. **Distribution** — vendored Python script in each repo vs. installable via `pipx install <tool>` vs. compiled Rust binary via Homebrew. Likely start vendored (zero adoption friction), add `pipx` later, only go Rust if performance demands it (unlikely for this workload).
6. **Naming** — see separate brainstorm.

## Positioning

> "worktrunk for worktrees, [this tool] for the resources inside them."

Not a replacement, a complement. Layers cleanly above worktrunk (or below — `post-checkout` hook fires before any of worktrunk's `post-start`). Layers above `port-selector` (could even shell out to it). Layers above mise. The unique value is the *declarative resource manifest* and the *git-hook transparency*.

If positioned right, the project leads with "no new commands to learn" and "your recipe is committed; your machine state isn't." Both are clear wins versus the current pile of imperative-shell-out worktree orchestrators.
