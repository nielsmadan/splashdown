# Product Review — agent persona + adjacent use cases — 2026-06-23

> Method: codebase + docs + `--multi` (external advisors) · no `--live` · Persona: see
> persona.md (rev 2) · Use cases: see use-cases.md (rev 2). This is the *second* review dated
> 2026-06-23; the first (`2026-06-23-review.md`) covered first-run/CLI-consistency UX.

## Summary
Reframing the primary persona to **the parallel-agent developer** (many LLM agents, each in its
own worktree, running semi-autonomously) doesn't just add an audience — it sharpens what
splashdown *is*. For a human, a port clash is a noticed annoyance; for a fleet of agents it's an
invisible, expensive failure (tokens burned "debugging" a phantom bug; two agents silently
corrupting one shared DB/sim). splashdown is unusually well-positioned for this — its
machine-wide `fcntl`-locked registry and per-checkout env emission are exactly the right
substrate. The single biggest realization from the brainstorm (and unanimous across three
external advisors): **ports and sims are only the *visible* collisions; the silent ones are
shared caches, daemons, test runners, and credentials — and most of those are just more env-var
resources splashdown can already emit.** So the highest-ROI move is to **ship "hermetic
worktree" recipe presets + reposition around the agent persona**, not to build a big new engine.

## What works well (for this persona)
- **Right substrate already exists** — the machine-wide locked registry (`registry.py`,
  `_lock` + `bind()` probes) and arbitrary per-checkout env emission mean many "new" use cases
  are recipe/preset work, not engine work.
- **Zero-touch by design** — the post-checkout hook runs `splash` automatically, which is
  essential for the agent persona (an agent won't run a setup step it doesn't know about).
- **Templating already isolates names** — `myapp_{{ slug(cwd) }}`, uuids, per-checkout values
  cover the *naming* half of stateful isolation today.
- **Pinned device variants are version-coverage-as-code** — UC10 works now; just undocumented.

## Findings (opportunities, rated by value to the agent persona)

### Critical (defines the wedge)
- **Reposition the product around parallel agents** (effort: S — narrative/docs) — the README
  leads with worktrees/ports generically; the agent-fleet framing is the sharpest, most current
  articulation of the same value. This reframing should land in README + persona.md (done).
- **Ship a "hermetic worktree" preset (CH/CL/CM)** (effort: S) — bundle a recipe scaffold that
  sets per-worktree `GRADLE_USER_HOME` / `NPM_CONFIG_CACHE` / `XDG_CACHE_HOME` / `TMPDIR` /
  `PLAYWRIGHT_BROWSERS_PATH` / `DERIVED_DATA_DIR` / `COMPOSE_PROJECT_NAME` / `LOG_DIR`. These are
  the collisions that actually corrupt parallel agents, and splashdown can emit them **today** —
  the gap is a preset + docs, not the engine. Highest ROI.

### High (genuinely-new capability, strong fit)
- **`splash lease` / `release` — mutex for scarce shared resources (CB)** (effort: M–L) — the one
  physical iPhone, a GPU, a rate-limited key, a single staging slot. The locked registry is the
  right primitive; this turns splashdown from *allocator* into *arbiter*. All three advisors rated
  it top-tier.
- **Owned lifecycle for per-worktree stateful services (CA)** (effort: M) — actually create/destroy
  the `myapp_<slug>` Postgres DB / Docker container / bucket prefix (via `[setup.*]` + a teardown
  on gc, or a helper), not just template the name. State corruption is worse than a port clash.

### Medium (valuable, partly latent)
- **Agent-first contract: `splash check` + stable JSON (CC)** (effort: S–M) — exit 0 iff the
  worktree is fully provisioned, so an agent can gate its own work; document the JSON shape as a
  contract, not "JSON that happens to print."
- **Display/worker allocation for parallel test runners (CI)** (effort: S–M) — a new `display`
  resource type (allocate `:99`-style numbers exactly like ports) plus per-worktree test DB / output
  dirs. Gemini's specific catch.
- **Per-worktree daemon/process supervision & teardown (CJ)** (effort: M–L) — track PIDs/process
  groups; stop leftover dev servers/daemons (8 Gradle daemons OOM the host); reclaim ports the
  registry thinks are free. New capability; pairs with churn-GC (CD).
- **Credential/test-account pool allocation (CK)** (effort: M) — lease a distinct test user / API
  key per worktree from a pool (port-style allocation applied to secrets); keep secrets out of the
  committed recipe.

### Suggestions (lower priority / mostly covered)
- **One `splash up` bootstrap verb (CE)** — mostly packaging over init+sync+`[setup.*]`; overlaps
  the prior review's H1.
- **Fleet view (CF)** — `splash status all` already exists; add a used/free summary later.
- **Reproduce-another-agent's-env (CG)** — export/import polish; templating already gets close.
- **Churn leak-proofing (CD)** — lazy GC already covers ports/sims; reframe toward *container/
  service* cleanup and "ephemeral" resources rather than ports.
- **Document UC10** in the README's use-case framing (it's in the recipe schema but not sold as a job).

## Prioritized recommendations
1. **Reframe positioning + add a hermetic-worktree preset** (Critical, S) — biggest narrative and
   practical win for the agent persona; mostly docs + a scaffold over the existing engine.
2. **`splash lease`/`release`** (High, M–L) — the standout *new* capability; arbitrates the
   resources that can't be per-worktree.
3. **Owned stateful-service lifecycle (CA)** (High, M) — makes the worktree a *truly* hermetic
   sandbox (DB/container, not just a name).
   <br>*(Then:)* `splash check` + JSON contract (CC) and the `display` resource type (CI) as
   smaller agent-ergonomics wins.

## Second opinion (--multi)
Queried Codex (GPT-5.5), Antigravity (Gemini 3.1 Pro), and OpenCode (DeepSeek v3.2), read-only.

- **Consensus on ranking**: all three put **CA (stateful services)** and **CB (leases)** at the
  top; agreed **CE is packaging**, **CF/CG are lower-value** (human observability, not the core
  collision problem). Split on **CD**: Codex/DeepSeek say it matters more under agents, Gemini
  calls it mostly redundant given existing lazy GC — resolved by reframing CD toward *service*
  cleanup, not ports.
- **Unanimous missing category** (the most valuable output): **shared caches/daemons/test-runners/
  credentials**, not ports/sims. Every advisor independently named per-worktree cache/temp
  isolation (`~/.gradle`, `~/.npm`, `DerivedData`, Playwright, `TMPDIR`), daemon/process leakage
  (Gradle/Metro/TS servers OOM), test-runner collisions, and secret/test-account pooling. → added
  as **CH, CI, CJ, CK, CL, CM** in use-cases.md.
- **Advisor-specific adds**: Gemini — allocate **X display numbers like ports** (`DISPLAY=:99`) and
  cap daemon heap per worktree. Codex — `COMPOSE_PROJECT_NAME` + container budgets, and per-checkout
  **log isolation**. DeepSeek — an **`ephemeral = true`** resource flag that auto-cleans on agent
  exit (not just worktree deletion), and host-networking beyond TCP (UDP, Unix sockets, named pipes).
- **Convergent meta-point**: the advisors and I independently landed on the same wedge framing —
  *most of these are env vars splashdown already emits, so lead with presets + docs; reserve new
  engine work for leases, the display type, and process supervision.* High confidence.

## Verify with users / open questions
- **No `--live`** — opportunities reasoned from code + docs + advisor review, not a running fleet.
  The "what breaks with 8 agents" failure modes are well-established but not measured here.
- **Validate demand with real agent users** — confirm which silent collision (caches vs DB vs
  test-runner vs credentials) actually bites most in practice before committing engine work; the
  preset (CH) is cheap enough to ship and learn from first.
- **Security review needed for CK** (credential pooling) and CB (leasing secrets) before building —
  keep secrets out of the committed recipe.
- **Scope creep risk** — these candidates could balloon splashdown from "resource coordinator" into
  "agent sandbox manager." Worth an explicit decision on how far toward process/container/secret
  management the tool should go vs. staying a coordinator that emits env + leases.
