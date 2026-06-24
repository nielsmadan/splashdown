# Use Cases (Jobs-to-be-done)

> Last refined: 2026-06-23 (rev 2) · Persona: see persona.md
>
> **Changed in rev 2:** added **UC10** (commit the lowest-supported-OS sim as part of the
> repo — already built, now documented) and an **Adjacent / candidate use cases** section
> brainstormed around the new primary persona (the parallel-agent developer). Candidates are
> labelled by how much the product covers today; they are *not* commitments.
>
> **Downstream:** each job below is broken into requirements in [`docs/prd/`](../prd/overview.md)
> (see its traceability matrix), which link on to the implementation in [`docs/tech/`](../tech/overview.md).

Ordered by centrality to the two co-primary personas. Paths cite the CLI surface in
`src/splashdown/cli.py` / `commands.py` and the model in `README.md`.

## UC1 — When I add a worktree, I want free dev ports without editing anything, so unrelated servers never collide (primary · general persona)
- **Trigger**: `git worktree add` / `git checkout` of a project already running splashdown.
- **Path today**: post-checkout hook fires `splash` (bare → `sync`) → `provision()` allocates
  via the machine-wide registry → writes `splashdown.env` → loader (mise/direnv/devbox) sources it.
- **Definition of done**: `pnpm dev` (etc.) binds a free port; no hand-edit, no clash with
  other worktrees/repos.
- **Frequency / stakes**: many times a day; silent wrong-port wastes real debugging time.

## UC2 — When I work in a checkout, I want to boot/build/launch on a sim that belongs to this checkout, so I never confuse builds (primary · mobile persona)
- **Trigger**: `splash run [type] [variant]`.
- **Path today**: `cmd_run` → reconcile sim/emulator (create if missing, named
  `<parent>/<cwd>/<variant>`) → boot → framework launcher (flutter/RN/expo/xcodebuild/gradle).
- **Definition of done**: app installed and launched on the checkout's own device.
- **Frequency / stakes**: many times a day; cross-checkout install confusion is the core pain.

## UC3 — When I adopt splashdown in a project, I want one command to set it all up, so I get value immediately (primary · onboarding, both)
- **Trigger**: `splash init [preset]`.
- **Path today**: scan workspace+frameworks → scaffold `splashdown.toml`(+`.local.toml`) →
  wire loader + post-checkout hook → run wiring checks → first `sync` → print ports.
- **Definition of done**: files written, hook installed, this checkout has live values.
- **Frequency / stakes**: once per project; a bad first run = abandonment.

## UC4 — When Xcode/Android SDK updates, I want my "latest" sims fixed without manual simctl surgery (mobile)
- **Trigger**: `splash run` (auto) or `splash target refresh`; cleanup via `splash target prune`.
- **Path today**: `ios="latest"` variants reconcile on run — destroy stale, recreate in place;
  `prune` deletes sims splashdown didn't create (with `--dry-run`/`--yes`).
- **Definition of done**: current-OS sims exist; the Xcode template pile is gone.
- **Frequency / stakes**: every Xcode bump; high annoyance otherwise. Distinctive value.

## UC5 — When a framework hardcodes its port, I want the allocated port to actually reach the running app, so the env var isn't silently ignored (general)
- **Trigger**: `splash doctor` / `splash doctor --fix` (also auto-run by `init`).
- **Path today**: per-framework `WiringCheck`s detect/patch metro.config, RN package.json
  scripts, RN `ios/.xcode.env`, Vite config; risky ones (Spring Boot) are report-only.
- **Definition of done**: doctor reports ✓; the dev server uses the allocated port.
- **Frequency / stakes**: at setup and after framework config changes; a miss = "why is it
  still on 8081?" confusion.

## UC6 — When I clone a project that uses splashdown, I want it to work on my machine too (team adoption)
- **Trigger**: fresh `git clone` of a repo with a committed `splashdown.toml`.
- **Path today**: the committed recipe + loader config exist, but the **hook is not
  installed by a clone** and the registry/env are per-machine → the teammate must run
  `splash init` (or at least install the hook) themselves.
- **Definition of done**: teammate's checkout allocates ports / has the hook with no surprise.
- **Frequency / stakes**: once per teammate; matters only if team adoption is the wedge.

## UC7 — When I delete worktrees, I want their reserved ports/sims freed, so the machine doesn't leak resources (both)
- **Trigger**: `splash gc`; lazy GC also runs on next allocation; `splash env release [KEY]`.
- **Path today**: `gc` drops registry entries whose checkout dir is gone (+ orphan sims);
  `env release` frees this checkout's allocations.
- **Definition of done**: stale rows gone; ports reusable.
- **Frequency / stakes**: occasional; low stakes (lazy GC covers most), but discoverability is low.

## UC8 — When I'm unsure of this checkout's state, I want to see its ports/vars/devices and what's free (both)
- **Trigger**: `splash status [all] [--check] [--verbose]`.
- **Path today**: prints resolved vars (`[in use]`/`[free]` for ports), declared variants +
  boot state, and a stale-row count.
- **Definition of done**: user can answer "what does this checkout have right now?"
- **Frequency / stakes**: ad hoc, when debugging a collision.

## UC9 — When only my checkout hits a bug, I want a throwaway device variant just for me (mobile, local)
- **Trigger**: `splash target add simulator repro-bug --model=... --ios=...`.
- **Path today**: writes an add-only variant to gitignored `splashdown.local.toml`;
  `splash run simulator repro-bug` boots it; `target remove` strips it.
- **Definition of done**: a per-checkout variant exists without touching the committed recipe.
- **Frequency / stakes**: occasional; high value for bug repro isolation.

## UC10 — When the team must support an old OS, I want the lowest-supported-OS sim committed in the repo, so backward-compat coverage is reproducible and not tribal knowledge (mobile · already built)
- **Trigger**: `splash run simulator lowest-supported` (or whatever the variant is named).
- **Path today**: the committed recipe declares a pinned variant, e.g.
  `[targets.simulator.lowest-supported]` with `model = "iPhone 12"` + `ios = "17.0"`. Pinned
  variants are deliberately **never** auto-upgraded by `target refresh` (unlike `ios="latest"`)
  — they are explicit version coverage. The per-checkout sim instance is created on demand.
- **Definition of done**: the app boots/launches on the minimum-supported-OS sim, and that
  target lives in version control so anyone — or any agent — gets the same matrix.
- **Frequency / stakes**: per release / per backward-compat pass. The value is that the
  **device matrix is code**, not someone's memory; this is fully supported today and only
  needs documenting (README shows the recipe shape; it's absent from this use-case list).

---

# Adjacent / candidate use cases

Brainstormed around the parallel-agent persona (persona.md). Each notes **coverage today** so
"document it" is distinguished from "build it." These are candidates for discussion, **not
commitments**. Cross-references to the prior review (`2026-06-23-review.md`) where relevant.

## CA — Per-agent isolated stateful services (DB / Redis / Docker container / queue / object-store prefix)
- **Job**: "When my agent runs, give its worktree its *own* database/container/bucket prefix so
  it never shares state with another agent's worktree."
- **Coverage today (partial)**: `template` resources already mint per-checkout names/URLs
  (README's `postgres://…/myapp_{{ slug(cwd) }}`), and `uuid`/`set` resources cover other ids.
  What's missing is *creating/destroying the actual service* — nothing spins up the
  `myapp_<slug>` Postgres DB or Docker container, or tears it down on `gc`.
- **Opportunity**: document a `[setup.*]` recipe that creates/migrates the per-slug DB on sync
  (and a teardown on gc), or add a Docker-aware helper/writer. **Highest-leverage gap for the
  agent persona** — ports alone aren't a hermetic sandbox; stateful deps are where agents
  actually corrupt each other. Size M.

## CB — Lease / mutex for a scarce *shared* resource (the one physical iPhone, a GPU, a rate-limited API key, a single staging slot)
- **Job**: "When several agents need the resource that *can't* be per-worktree, I want them to
  take turns safely instead of clobbering each other."
- **Coverage today (none)**: splashdown hands out *distinct* resources; it does not arbitrate a
  *shared singular* one. But the machine-wide `fcntl`-locked registry is exactly the right
  substrate for a cross-checkout mutex/semaphore.
- **Opportunity**: `splash lease <name> [--wait] [--slots N]` / `splash release <name>` — a
  machine-wide lock agents acquire before touching the physical device / staging DB /
  rate-limited key. **The most novel adjacent capability** for multi-agent work; turns
  splashdown from "allocator" into "resource arbiter." Size M–L.

## CC — Agent-first programmatic contract (stable JSON, exit codes, "am I ready?")
- **Job**: "When my agent needs to know or verify its environment, give it a machine-readable
  answer, not human prose to parse."
- **Coverage today (partial)**: `--format json` on `status`, `splash env get KEY` (exit 1 if
  absent). Not framed/documented as a stable contract; no single "dump my whole env as JSON" or
  "verify this worktree is fully provisioned" call.
- **Opportunity**: document the JSON contract + add `splash env --json` and a `splash check`
  (exit 0 iff this worktree is fully provisioned) so an agent can gate its own work. Size S–M.

## CD — Leak-proofing under high worktree churn
- **Job**: "When agents create and tear down worktrees all day, don't let orphaned
  sims/containers/ports pile up."
- **Coverage today (partial)**: lazy GC reclaims ports/kv on next allocation; `splash gc`
  reclaims sims/orphans — but an agent won't *run* `gc`, and git has no post-worktree-remove
  hook.
- **Opportunity**: scheduled/background `splash gc`, a `splash gc --watch`, or harness-level
  wiring; document the churn story explicitly. Size S–M. (Extends the lazy-GC model the registry
  already has.)

## CE — One command to make a fresh worktree agent-ready
- **Job**: "When my harness spins up a worktree, run *one* command that allocates ports, writes
  env, wires the loader, and runs project setup (migrate/seed) so the agent lands on a prepared
  sandbox."
- **Coverage today (partial)**: `init` + `sync` + `[setup.*]` (`run_setup`) already do most of
  this, but it isn't packaged as *the* agent-bootstrap entrypoint, and `init` re-scans/scaffolds
  (wrong for an already-configured clone — see prior review **H1 / UC6**).
- **Opportunity**: a `splash up` / `splash install` bootstrap verb. Overlaps the prior review's
  H1; the agent persona raises its priority. Size S–M.

## CF — Fleet view: what every agent has allocated right now
- **Job**: "When I'm supervising 8 agents, show me every worktree's ports/sims/DBs and what's
  still free, in one place."
- **Coverage today (partial)**: `splash status all` already prints a one-row-per-checkout table;
  there's no machine-wide free/used *summary* tuned for fleet supervision.
- **Opportunity**: `splash status --global` / `splash fleet` (used vs free per range, across all
  checkouts). Elevates the prior review's M5; the agent persona makes it more valuable. Size M.

## CG — Reproduce another agent's exact environment
- **Job**: "When an agent reports a bug on a specific sim/port/seed, let me reproduce its exact
  resource setup."
- **Coverage today (partial)**: committed recipe + add-only `splashdown.local.toml` give
  determinism; `splash target add` writes local variants. No "export/pin/import my current
  allocation" handoff.
- **Opportunity**: share/import `splashdown.local.toml`, or a pin/export of the current
  allocation. Size S. (Mostly covered; mainly a documentation + small-ergonomics gap.)

---

### Candidates surfaced by external advisors (Codex, Gemini, DeepSeek — 2026-06-23, --multi)

All three independently converged on the same missing category: **ports and sims are only the
visible collisions; the silent ones are shared caches, daemons, test runners, and credentials.**
A key realization from the synthesis — **most of these are just more env-var/template resources
splashdown already emits** (`GRADLE_USER_HOME`, `TMPDIR`, `COMPOSE_PROJECT_NAME=…{{ slug(cwd) }}`,
a per-worktree log dir). So the highest-ROI move is often **ship opinionated "hermetic worktree"
recipe presets + document the pattern**, not new engine code. The genuinely-new engine
capabilities are leases (CB), a display-number resource type (CI), and process supervision (CJ).

## CH — Per-worktree cache / temp / build-state isolation  *(consensus #1 miss)*
- **Job**: "When 8 agents run `npm install` / `gradle build` / `xcodebuild` / Playwright at once,
  don't let them thrash and corrupt the *shared global* caches."
- **What breaks**: agents share `~/.gradle`, `~/.npm`, pnpm store, Vite/Next `.cache`, Metro
  haste map, CocoaPods + Xcode `DerivedData`, Playwright browser cache, `$TMPDIR` — causing lock
  timeouts, cache corruption, and phantom build failures the agent then "debugs."
- **Coverage today (latent)**: splashdown already emits arbitrary env vars per checkout — it can
  set `GRADLE_USER_HOME`, `NPM_CONFIG_CACHE`, `XDG_CACHE_HOME`, `TMPDIR`, `PLAYWRIGHT_BROWSERS_PATH`,
  `DERIVED_DATA_DIR` to per-worktree paths *today*. The gap is **a preset + documentation**, not
  the engine. Size S (preset/docs) — highest ROI for the agent persona.

## CI — Test-runner isolation, including display/worker allocation
- **Job**: "When agents run e2e/unit suites in parallel, give each its own test DB, output dirs,
  and headless-browser/display slots."
- **What breaks**: shared `TEST_DATABASE_URL`, colliding Playwright/Cypress debug ports and
  screenshot/video/coverage dirs, and on Linux a shared X display (`DISPLAY`/Xvfb).
- **Coverage today (partial→new)**: env/template resources cover `TEST_DATABASE_URL` and output
  dirs now; **a new `display`-type resource** (allocate `:99`-style display numbers exactly like
  ports — Gemini's point) would be a small, natural addition to the allocator. Size S–M.

## CJ — Per-worktree daemon / process supervision & teardown
- **Job**: "When an agent finishes (or abandons) a task, stop the dev server / emulator / compose
  stack / language-server / file-watcher it left running, so the machine doesn't fill with zombies."
- **What breaks**: 8 lingering Gradle daemons / Metro bundlers / TS servers OOM the host;
  orphaned dev servers hold ports the registry thinks are free.
- **Coverage today (none)**: splashdown tracks *resources*, not *processes*. New capability:
  track PIDs/process-groups per checkout and `splash stop`/`gc` them; optionally cap daemon heap
  / disable persistent daemons per worktree. Size M–L. (Pairs with CD's churn story.)

## CK — Credential / test-account pool allocation
- **Job**: "When agents need a real external credential (test user, API key, OAuth app) but
  sharing one causes rate-limits or state collisions, hand each worktree a distinct one from a pool."
- **What breaks**: a single shared dev token gets rate-limited or has its test state mutated by a
  sibling agent; one OAuth callback/webhook URL can't serve N worktrees.
- **Coverage today (none/partial)**: this is **port-style pool allocation applied to a list of
  secrets** + per-worktree callback URLs (templating covers the URL; the *pool lease* is new and
  overlaps CB). Size M. Security-sensitive — keep secrets out of the committed recipe.

## CL — Container / compose isolation & budgets
- **Job**: "Give each worktree its own compose project so agents' Docker stacks don't collide, and
  cap total container resource use so 8 agents don't exhaust the host."
- **Coverage today (latent)**: `COMPOSE_PROJECT_NAME = "{{ slug(cwd) }}"` is expressible as a
  template resource *today* (doc gap); container creation/teardown is the CA lifecycle gap; CPU/mem
  *budgets* are new (lease-style quotas). Size S (compose name) → M (budgets).

## CM — Log / output isolation & discovery
- **Job**: "Give each worktree its own log directory so I can inspect one agent's failure without
  untangling 8 agents' interleaved scrollback."
- **Coverage today (latent)**: a per-worktree `LOG_DIR`/log path is just another templated env var
  splashdown can emit — preset + docs. Size S.
