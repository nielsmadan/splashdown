# User Persona(s)

> Last refined: 2026-06-23 (rev 2) · Sources: `README.md`, `pyproject.toml`
> (description/keywords), `src/splashdown/cli.py` (CLI surface), `commands.py`, `CLAUDE.md`,
> user input (2026-06-23 rev 1: two co-primary personas, adoption undecided; rev 2: the
> sharpest target is **developers driving LLM coding agents** — many agents running at once,
> each in its own worktree).
>
> **Changed in rev 2:** promoted a new primary persona — *the parallel-agent developer* —
> above the two prior personas, which now describe the *kind of work* that persona does
> (mobile vs web/backend) rather than competing top-level audiences. The original personas
> are retained as secondary because a solo human without agents is still a valid user; the
> emphasis shifted per the maintainer's direction.

## The parallel-agent developer (primary)
- **Who they are**: a developer who runs **multiple LLM coding agents concurrently**
  (Claude Code, Codex, Cursor, etc.), each in its own git worktree, each working a different
  branch/feature/PR at the same time. Comfortable orchestrating; spends more time reviewing
  and steering than typing. Works on web, backend, *or* mobile — see the two secondary
  personas for the resource flavor.
- **Why they're here**: with N agents live, the machine is shared N ways and **nobody is
  watching each one**. Every agent's `npm run dev` wants port 3000; every agent's
  `npm run ios` wants the same booted simulator; every agent points at the same dev
  database. Collisions don't surface as a tidy "address in use" a human notices — the agent
  *interprets* the failure, then burns time and tokens "debugging" a phantom bug that is
  really a resource clash, or two agents silently talk to the *same* dev server/sim/DB and
  corrupt each other's state. They want every worktree to be a **hermetic sandbox** so agents
  can run in parallel without interfering — automatically, with no human in the loop per task.
- **Environment**: macOS or Linux laptop (macOS for sims). 3–10+ worktrees and agent
  sessions open at once; worktrees are **created and destroyed rapidly** as tasks start and
  finish. The human is supervising the fleet, not babysitting any single checkout.
- **What they value** (ranked): 1) hermetic isolation per worktree (zero cross-talk),
  2) zero-touch automation (it must happen on worktree create / `cd`, because the agent won't
  run a setup step it doesn't know about), 3) machine-readable, predictable outputs the agent
  can consume, 4) no leaked resources as worktrees churn.
- **Constraints & frustrations**: agents don't notice or recover from collisions gracefully;
  a port clash becomes an expensive wild-goose chase. Manual per-agent setup defeats the
  point of parallelism. There's no shared, machine-wide notion of "what's already taken."
- **Success looks like**: spin up a worktree, point an agent at it, and it *just works* in
  isolation — its own ports, its own sim, its own DB — with the human never touching resource
  config and never seeing two agents collide.

## Mobile-app developer on worktrees (secondary — work flavor)
- **Who they are**: iOS/Android developer using React Native, Expo, Flutter, or native
  (Swift/Kotlin). Comfortable in the terminal; lives in Xcode / Android Studio /
  `simctl` / `adb`. On macOS (sims need it); some Android-only work on Linux.
- **Why they're here**: they keep multiple checkouts/worktrees of an app (feature branch,
  hotfix, PR review) and can't tell which simulator has which build, or two worktrees
  fight over one simulator/emulator and the Metro port. They want each checkout to own
  its **own** named sim/emulator + dev ports, automatically.
- **Environment**: macOS laptop, Xcode installed, `$ANDROID_HOME` set. Switches branches
  and worktrees several times a day. Re-runs after every Xcode/SDK update.
- **What they value** (ranked): 1) correctness/no-collisions, 2) zero manual bookkeeping
  (don't make me track sim names), 3) resilience to Xcode/SDK churn, 4) speed.
- **Constraints & frustrations**: `simctl`/`avdmanager` are verbose and stateful; after an
  Xcode update old sims break and the template pile grows; nothing ties a sim to a checkout.
- **Success looks like**: `splash run` in any checkout boots the right sim, builds, and
  launches — and after an OS bump, `splash target refresh` just fixes everything.

## Worktree-heavy web/backend developer (secondary — work flavor)
- **Who they are**: full-stack / backend dev on Vite, Next.js, Node, Django, FastAPI,
  Spring Boot, or a pnpm/yarn/cargo/gradle monorepo. Uses git worktrees heavily for
  parallel dev, e2e runs, and PR review. Uses mise / direnv / devbox for env loading.
- **Why they're here**: spinning up a second worktree means dev-server port clashes
  (`5173`, `8080`, …) and hand-editing `.env`. They want free ports picked automatically,
  machine-wide, so unrelated projects also never collide.
- **Environment**: macOS or Linux. Multiple repos and worktrees open at once. Already has
  an env loader wired (or wants splashdown to wire one).
- **What they value** (ranked): 1) it just works on `cd`/checkout (no manual sync),
  2) doesn't fight their existing loader/hook setup, 3) machine-wide coordination,
  4) stays out of the way.
- **Constraints & frustrations**: frameworks hardcode ports in config files that override
  env vars (Vite `loadEnv`, RN metro/xcode, Spring `application.properties`); a bare env
  var isn't enough.
- **Success looks like**: add a worktree, `cd` in, `pnpm dev` runs on a free port with no
  edits; `splash doctor` confirms the wiring.

## Adoption context (undecided — review for both)
The maintainer hasn't decided whether the wedge is **solo** (a dev configures it for their
own machine) or **team** (a team commits `splashdown.toml` so everyone gets consistent
ports/device targets). The product already supports both: `splashdown.toml` is committed
(team), `splashdown.local.toml` + the registry are per-checkout (solo). Findings flag where
the two diverge — notably onboarding a *second* teammate vs. a *second* worktree.
