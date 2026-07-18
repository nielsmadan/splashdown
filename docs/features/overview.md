# Features

**What each splashdown feature does**, kept in sync with the implementation. This is the
behavior reference for contributors and coding agents *building* splashdown: terse, pointer-first
(`file:line`), not a usage tutorial. For human how-to walkthroughs see
[`../user/`](../user/overview.md); for internals see [`../tech/`](../tech/overview.md). Owned by
the `doc` skill; run `doc --update` after feature work to keep it current.

Each doc maps to one or more jobs-to-be-done from [`../product/use-cases.md`](../product/use-cases.md).

## Features
- [ports-and-env.md](ports-and-env.md) — per-checkout dev ports, env vars, uuids, and templated
  values; the core `splash sync` flow and the `[resources.*]` schema. *(UC1)*
- [device-targets.md](device-targets.md) — per-checkout simulators/emulators/physical devices;
  `run`/`start`/`stop`/`destroy`, auto-recreate of `latest` sims, and the committed device matrix
  (incl. lowest-supported-OS coverage). *(UC2, UC4, UC10)*
- [init-and-onboarding.md](init-and-onboarding.md) — `splash init`: scan, scaffold, wire the
  loader + post-checkout hook, first sync; presets; and the fresh-clone onboarding gap. *(UC3, UC6)*
- [framework-wiring.md](framework-wiring.md) — `splash doctor`: detect and (where safe) auto-patch
  framework configs that hardcode the port / override the env var. *(UC5)*
- [resource-cleanup.md](resource-cleanup.md) — `gc`, lazy GC, `target prune`, and `env release`:
  reclaiming ports/sims/vars for deleted checkouts. *(UC7)*
- [status-and-inspect.md](status-and-inspect.md) — `status` (per-checkout / `all` / json / `--check`)
  and `env` (list/get/set/release); the machine-readable surface. *(UC8)*
- [per-checkout-overrides.md](per-checkout-overrides.md) — `splashdown.local.toml` add-only target
  variants; `target add`/`remove`. *(UC9)*

> Monorepo setup is a user how-to (a walkthrough with worked examples), not a feature spec, so
> it lives in [`../user/monorepos.md`](../user/monorepos.md), not here.

## Traceability: problem → requirement → implementation

The chain from the user's problem (the [product](../product/overview.md) layer) through the
requirement (this layer) to the implementation (the [tech](../tech/overview.md) layer). Each
feature doc's header repeats its own up/down links.

| Problem ([use case](../product/use-cases.md)) | Requirement (feature) | Implementation ([tech](../tech/overview.md)) |
|---|---|---|
| UC1 — ports / env / templated values | [ports-and-env](ports-and-env.md) | [provisioning](../tech/provisioning.md), [registry](../tech/registry.md), [recipe-and-templates](../tech/recipe-and-templates.md) |
| UC2, UC4, UC10 — device targets | [device-targets](device-targets.md) | [devices](../tech/devices.md), [registry](../tech/registry.md) |
| UC3, UC6 — init & onboarding | [init-and-onboarding](init-and-onboarding.md) | [scanning-and-extension](../tech/scanning-and-extension.md), [cli-and-commands](../tech/cli-and-commands.md) |
| UC5 — framework wiring | [framework-wiring](framework-wiring.md) | [wiring](../tech/wiring.md) |
| UC7 — resource cleanup | [resource-cleanup](resource-cleanup.md) | [registry](../tech/registry.md), [devices](../tech/devices.md) |
| UC8 — status & inspect | [status-and-inspect](status-and-inspect.md) | [cli-and-commands](../tech/cli-and-commands.md) |
| UC9 — per-checkout overrides | [per-checkout-overrides](per-checkout-overrides.md) | [recipe-and-templates](../tech/recipe-and-templates.md), [devices](../tech/devices.md) |
