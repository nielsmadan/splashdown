# User Persona(s)

> Last refined: 2026-06-23 · Sources: `README.md`, `pyproject.toml` (description/keywords),
> `src/splashdown/cli.py` (CLI surface), `src/splashdown/commands.py`, `CLAUDE.md`,
> user input (2026-06-23: both personas co-primary; solo-vs-team adoption undecided).

Splashdown serves two **co-primary** personas (confirmed by the maintainer). They share
the same root pain — concurrent checkouts of the same project collide over machine
resources — but differ in which resource hurts most.

## Mobile-app developer on worktrees (co-primary)
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

## Worktree-heavy web/backend developer (co-primary)
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
