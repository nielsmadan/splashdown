# Product Requirements (PRD)

Product-behavior docs: **what each splashdown feature does for the user**, kept in sync with
the implementation. This is the middle layer of the three-layer model — it sits below
`docs/product/` (user/why, owned by `review-product`) and above the source. Each doc references
the code by `file:line` rather than restating it; run `doc --update` after feature work to keep
these current.

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
- [monorepos.md](monorepos.md) — **Monorepos**: canonical multi-app setups and how splash init defers on ambiguous ones.

## Traceability: problem → requirement → implementation

The chain from the user's problem (the [product](../product/overview.md) layer) through the
requirement (this layer) to the implementation (the [tech](../tech/overview.md) layer). Each PRD
doc's header repeats its own up/down links.

| Problem ([use case](../product/use-cases.md)) | Requirement (PRD) | Implementation ([tech](../tech/overview.md)) |
|---|---|---|
| UC1 — ports / env / templated values | [ports-and-env](ports-and-env.md) | [provisioning](../tech/provisioning.md), [registry](../tech/registry.md), [recipe-and-templates](../tech/recipe-and-templates.md) |
| UC2, UC4, UC10 — device targets | [device-targets](device-targets.md) | [devices](../tech/devices.md), [registry](../tech/registry.md) |
| UC3, UC6 — init & onboarding | [init-and-onboarding](init-and-onboarding.md) | [scanning-and-extension](../tech/scanning-and-extension.md), [cli-and-commands](../tech/cli-and-commands.md) |
| UC5 — framework wiring | [framework-wiring](framework-wiring.md) | [wiring](../tech/wiring.md) |
| UC7 — resource cleanup | [resource-cleanup](resource-cleanup.md) | [registry](../tech/registry.md), [devices](../tech/devices.md) |
| UC8 — status & inspect | [status-and-inspect](status-and-inspect.md) | [cli-and-commands](../tech/cli-and-commands.md) |
| UC9 — per-checkout overrides | [per-checkout-overrides](per-checkout-overrides.md) | [recipe-and-templates](../tech/recipe-and-templates.md), [devices](../tech/devices.md) |
