# Profiles and loaders

Two extension points decide what `splash init` produces.

A **Profile** is the per-framework integration rules: what resources this kind of app wants, and what config files (if any) need patching so the values reach the running process. The Vite Profile, for example, emits `WEB_DEV_PORT` (and `API_DEV_PORT` if it sees a `server.proxy` block) and rewrites `vite.config.{ts,js}` to read `process.env.X` instead of `loadEnv()`. The Spring Boot Profile emits `PORT` and checks that `application.properties` uses the `server.port=${PORT:8080}` placeholder. The mobile Profiles (`react-native`, `expo`, `flutter`, `ios-native`, `android-native`) bring in the existing per-framework wiring checks.

A **Loader** is the per-shell-env-tool wiring: how `splashdown.env` gets sourced into your shell when you `cd` into the project. Splashdown supports three: `mise` (sets `_.file = "splashdown.env"` in `mise.toml`), `direnv` (appends `dotenv_if_exists splashdown.env` between sentinel markers in `.envrc`), `devbox` (adds an `init_hook` entry in `devbox.json`). All three are idempotent and reversible.

mise and direnv only load a config once you trust or allow it. Splashdown runs `mise trust` or `direnv allow` only when `init` created that loader file itself. It never approves a pre-existing or inherited config during `init`, `sync`, or the post-checkout hook. Review and approve those files with the loader's normal command. This matters in a new worktree because the inherited config has a new path and may need a separate approval.

When no loader config is found in the repo, splashdown checks whether one is installed on your PATH and wires the first it finds, in the order mise, direnv, devbox. A fresh clone has no config file yet, so without this step splashdown would write `splashdown.env` and nothing would ever read it. A loader config already in the repo always wins over an installed binary.

When nothing is installed either, splashdown picks `none` and routes the generated values straight into a dotenv file the project already reads (`.env` if present, else `.env.local`), provided at least one app actually reads dotenv files (Next.js, Django, FastAPI, Node backends). For apps that only see values exported into the process environment (Vite, Spring Boot, mobile), a plain dotenv file can't reach them, so splashdown keeps generating `splashdown.env` and prints how to source it or which loader to install.

Force any choice with `splash init --loader=mise|direnv|devbox|none`. `--loader none` is the explicit opt-out from auto-wiring, and `splash init --rescan` preserves it rather than re-detecting over the top.

**Override at any layer.** Edit `[project] workspace`, `[project] loader`, `[apps.<name>] profile`, or any `[resources.*]` table. Values must name a supported built-in workspace, loader, or profile; unknown names and fields are errors. Splashdown picks up a valid change on the next sync and never re-scans unless you ask. `splash init --rescan` re-runs the scanner against the current filesystem (e.g. after you add a new app to the monorepo).

**Multi-instance collisions** are mangled at scan time. Two Vite apps both want `WEB_DEV_PORT`. The scanner renames them `WEB_DEV_PORT_ADMIN` / `WEB_DEV_PORT_CUSTOMER` based on the app names, so the recipe stays unambiguous.

**Unknown framework.** An app whose framework splashdown doesn't recognize gets `profile = "unknown"`: no resources allocated for it, no wiring attempted. The rest of the project still works. To add support, contribute a Profile upstream.

## The `writer` field (power-user escape hatch)

Resources route to `splashdown.env` by default. That's what mise/direnv/devbox load. For consumers that can't read `process.env` (legacy build systems, vendor tooling, frameworks splashdown doesn't have a Profile for yet), set `writer` on the resource:

```toml
[resources.LEGACY_PORT]
type   = "port"
range  = [9999, 10100]
writer = "envfile=path/to/legacy/.env"
```

Available writers: `splashdown-env` (default), `envfile=PATH` (a .env-format file, preserving non-managed lines), `envrc` (writes `.envrc.local`), `stdout` (echoes), and `none` (registry-only, no file output).

An `envfile=` path must be non-empty, relative to the checkout root, and stay inside the checkout. Absolute paths, `..` traversal, bare `envfile`, and other writer spellings are rejected during recipe validation, before any resource is allocated.

Most of the time the framework Profile handles routing implicitly and `writer` stays unset. Use it when no Profile covers your consumer yet.
