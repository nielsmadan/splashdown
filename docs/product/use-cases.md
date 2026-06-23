# Use Cases (Jobs-to-be-done)

> Last refined: 2026-06-23 · Persona: see persona.md

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
