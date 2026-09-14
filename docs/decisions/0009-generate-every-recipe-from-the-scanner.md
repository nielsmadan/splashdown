# 0009: Generate every recipe from the scanner and opt into capabilities in the recipe

- Status: Accepted
- Decision date: 2026-09-12
- Recorded: 2026-09-14
- Supersedes: the named-preset clause and the Electron isolated/shared clause of
  [0003](0003-separate-inferred-frameworks-from-explicit-intent.md)

## Context

ADR 0003 kept named presets as a way to express intent the scanner cannot infer, and gave Electron
user-data isolation an explicit isolated/shared choice at init time. Both held until the 1.0
command review examined what each actually produced.

The presets were not templates for a project. `minimal`, `server`, and `electron` were complete
static recipes with their own generation and integration path (`_cmd_init_preset`, `scaffolds.py`),
carrying no app or target inventory and assuming things the scanner would never guess, such as
PostgreSQL behind the broad name `server`. `init electron` also overlapped scanner-driven Electron
detection, so the same project had two different ways to be configured.

The Electron choice was worse than redundant. Isolation only takes effect when the application's
main process reads `ELECTRON_PROFILE_ID` and calls `app.setPath("userData", …)` before
`requestSingleInstanceLock()`. Init could write the resource and print the snippet, but it could
not make the change, so a user who answered "isolated" got a recipe that claimed a capability the
app did not have. The printed snippet was the only place the two halves appeared together, and it
appeared at the one moment the user could not act on it.

## Decision

Scanner-driven generation is the only way `splash init` produces a recipe. There is no preset
positional and no second generation path. Recipes worth starting from are documentation: the
generic port, the templated Postgres database name, and the Electron profile identifier live in
`docs/user/recipe.md`, where `tests/test_recipe.py` extracts every fenced TOML block and validates
it under the strict `Recipe` validator, so the published examples cannot drift from the schema.

An optional capability whose effect depends on an application-side change is not an init question
and not init-generated output. Init keeps detecting Electron and keeps generating the renderer's
resources, and when an Electron app is present it prints a pointer to the documented isolation
recipe. It does not ask, does not write `ELECTRON_PROFILE_ID`, and no longer prints the
main-process snippet, because a snippet is inert without the resource declaration beside it. The
resource and the required main-process change are documented together as one opt-in.

What ADR 0003 decided about detection is unchanged: framework coverage comes from
scanner-selected Profiles, Electron remains a secondary capability layered on the primary Profile
rather than replacing it, and Splashdown still never rewrites an arbitrary Electron entry point.

## Consequences

- Init has one code path to reason about and one set of behaviors to test. A custom project edits
  the generated recipe, by hand or with an agent, instead of picking a canned one.
- The explicit way to bypass app detection is gone. A project the scanner reads poorly starts from
  the structure-only recipe and the documented examples.
- The user docs are load-bearing for onboarding, not decorative, and the recipe page's examples
  are covered by tests.
- Isolation is now a deliberate two-step opt-in. A user who declares the resource and skips the
  main-process change gets an inert environment variable, which the page says plainly.
- A future secondary capability that needs application code follows the same shape: detect it,
  point at the documented recipe, and let the recipe declare it.

## Related

- [Init and onboarding](../features/init-and-onboarding.md)
- [Scanning and extension](../tech/scanning-and-extension.md)
- [Recipe reference](../user/recipe.md)
