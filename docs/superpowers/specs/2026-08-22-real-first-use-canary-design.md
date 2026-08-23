# Real first-use canary

## Goal

Add a networked smoke test that exercises Splashdown the way a new user does: create a current
Vite application, prove it runs before Splashdown is present, run plain `splash init`, create a
Git worktree, and prove both checkouts run concurrently on different Splashdown-managed ports.

The canary complements the deterministic test suite. `tests/test_e2e_worktrees.py` already covers
real repositories, hooks, worktrees, allocation, and concurrent listeners with a small Python
server. The new test covers the integration seams that fixture-based tests intentionally cannot:
packaging the current source, the latest upstream scaffolder, a real framework runtime, and the
unmodified first-use command.

## User journey under test

The script performs one linear scenario:

1. Install the current Splashdown checkout as a regular package in a temporary virtual
   environment outside the generated application.
2. Scaffold the latest React Vite application with `create-vite@latest` and install its locked
   dependencies.
3. Initialize and commit a Git repository.
4. Start the application with its normal npm development command on an independently selected
   free port, wait for an HTTP response, and stop it. This proves the generated application worked
   before Splashdown changed the repository without assuming Vite's default port is unused.
5. Run plain `splash init` with no preset, loader override, or other behavior-changing option.
6. Verify Vite detection, the generated `WEB_DEV_PORT` resource, initial provisioning, loader
   integration, the Git ignore entry for `splashdown.local.toml`, and the managed post-checkout
   hook. Commit the generated Splashdown configuration and any wiring changes.
7. Add a detached Git worktree. Do not run `splash sync` in it.
8. Verify that the post-checkout hook provisioned the new checkout and that the two registry
   entries contain different `WEB_DEV_PORT` values.
9. Install the same locked npm dependencies in the worktree.
10. Start both Vite applications on their allocated ports with strict port handling, wait for both
    to answer over HTTP, and verify both processes remain alive concurrently.

The scaffolder deliberately remains `create-vite@latest`. This test is an ecosystem compatibility
canary, so an upstream change that breaks Splashdown is a useful result. The script records the
resolved Node, npm, create-vite, and Vite versions so that such a failure is reproducible.

## Entry point and boundaries

The implementation consists of one Vite-specific Bash harness at
`tests/smoke/first-use-vite.sh` and one `just smoke-first-use` recipe. The script owns temporary
environment setup, external commands, process supervision, assertions, diagnostics, and cleanup.
The Just recipe is only a discoverable entry point and does not duplicate test logic.

This first version does not introduce a general framework-driver abstraction. There is one real
scenario, and extracting a reusable test framework before a second scenario exists would add an
untested interface. A later canary may extract shared helpers when its requirements show which
parts are genuinely common.

The live canary is not part of pytest, `just check`, pre-commit, pre-push, or required pull-request
CI. Deterministic pytest coverage may exercise its control flow with fake external tools, but
those paths never contact a Node registry. Contributors run the live canary explicitly before
releases or after changing init, scanning, provisioning, loaders, hooks, or Vite integration.

## Environment isolation

The script creates one validated temporary root containing sibling directories for the Python
environment, generated repository, second worktree, logs, and private application state. The
installed `splash` executable is therefore outside the generated checkout, satisfying the hook's
checkout-executable trust boundary while still testing the current source.

The test sets private `XDG_STATE_HOME` and `XDG_CONFIG_HOME` values for its complete lifetime.
Splashdown allocations, clone trust, loader approvals, and related state cannot affect the
developer's normal state. Git global and system configuration are disabled for the generated
repository, inherited Git configuration is cleared, and `git init` uses an explicitly empty
template. The canary refuses a scaffold that already contains any `.git` entry. A local test
identity is configured before committing. This prevents user hooks, aliases, templates, and
identity settings from changing the scenario.

Network-fetched Node commands receive a minimal environment with a disposable home, temporary
directory, npm cache, and empty user and global configuration. They do not inherit the developer's
npm credentials or unrelated environment variables. The Python installation still uses indexes
and credentials configured for uv. This is credential isolation, not a general filesystem
sandbox, so the canary remains an explicit command rather than an automatic check. Each checkout
gets its own installation; the second uses `npm ci` against the committed lockfile.

The script checks for `git`, `uv`, `node`, `npm`, and `curl` before creating the project. Missing
tools fail with one actionable prerequisite message rather than a later command-not-found error.

## Plain init and loader handling

The canary must call `splash init`, not `splash init --loader none`. Loader discovery is part of
the first-use experience. Splashdown may legitimately select mise, direnv, devbox, or the `none`
fallback depending on the machine.

The script captures the selected loader from init output and accepts every supported result. It
then checks the matching positive outcome:

- mise: the selected mise configuration loads `splashdown.env`;
- direnv: `.envrc` contains Splashdown's managed dotenv block;
- devbox: `devbox.json` contains Splashdown's managed init hook;
- none: init prints the no-loader guidance and still writes the provisioned output.

The server subprocesses do not depend on a parent interactive shell noticing loader activation.
Each launch reads its checkout's generated `splashdown.env` with export enabled, then invokes the
ordinary npm Vite script. This keeps the test deterministic while still exercising loader
selection and wiring during init.

## Port and server contract

The generated Vite template does not automatically consume `WEB_DEV_PORT`. Splashdown's Vite
wiring check intentionally does not insert an arbitrary `server.port` block into an unknown
configuration shape. The canary therefore follows Splashdown's documented launch guidance and
passes the allocated value to Vite:

```sh
npm run dev -- --host 127.0.0.1 --port "$WEB_DEV_PORT" --strictPort
```

The pre-init baseline uses the same command shape with a separately selected free port. It proves
the scaffold itself works without depending on whether port 5173 is already occupied.

`--strictPort` is load-bearing: without it, Vite may silently choose another port and make a
broken allocation appear successful. Binding to `127.0.0.1` keeps the test local. Readiness uses
bounded `curl` polling plus process-liveness checks rather than a fixed sleep. A successful HTTP
response must contain the generated application's root document marker, so an unrelated listener
cannot satisfy the probe.

The post-init phase starts the original checkout and worktree together. Both must answer on their
recorded registry ports while both child processes are still running. The assertion is about the
simultaneous isolation Splashdown promises, not merely two sequential successful launches.

## Assertions

The canary succeeds only when all of these outcomes occur:

1. The current Splashdown source installs and its `splash` executable is the one on `PATH`.
2. The latest Vite React scaffold installs and runs normally before Splashdown initialization.
3. Plain `splash init` detects the application as Vite and reports one supported loader result.
4. `splashdown.toml` declares `WEB_DEV_PORT` for the detected app.
5. The original checkout has a registry allocation and generated output for that resource.
6. `splashdown.local.toml` is ignored and the post-checkout hook is active.
7. Creating the detached worktree generates its resource output without an explicit sync.
8. The worktree has its own registry allocation, different from the original checkout's port.
9. Both Vite servers bind exactly those ports and serve the generated application concurrently.

Assertions are positive and name the expected state. The test does not infer success from the
absence of a warning or from a background process merely surviving for a fixed interval.

## Failure reporting and cleanup

Every phase has a short visible label: prerequisites, Splashdown install, Vite scaffold,
dependency install, baseline launch, init, worktree creation, hook verification, second install,
and concurrent launch. External commands write to phase-specific log files. Normal output stays
short; a failure prints the current phase, relevant log tail, tool versions, selected loader,
allocated ports when known, and the retained workspace path when applicable.

HTTP probes have a fixed deadline. If a server exits before readiness, the test reports its exit
status and server log immediately. A timeout reports the URL and the last server output rather
than only saying that a generic assertion failed.

Exit, interruption, and failure share one cleanup path. It terminates and waits for every server
process before removing the temporary repository and isolated state. `SPLASH_SMOKE_KEEP=1`
preserves the complete temporary root and logs for debugging; cleanup remains the default.

Registry outages, npm registry failures, or an incompatible latest scaffold are reported in the
phase where they occurred. They remain real canary failures, but they never make the ordinary
test suite red because this command is opt-in.

## Automation policy

The initial implementation is local and pre-release only. After the command has demonstrated
stable cleanup, runtime, and diagnostics across several machines, a separate follow-up may add a
non-required GitHub Actions workflow with `workflow_dispatch` and a weekly schedule. That workflow
would be an ecosystem warning, never a pull-request merge gate.

## Documentation

Contributor documentation will list the command, prerequisites, network requirement, expected
runtime and disposable-cache behavior, and debug-retention flag. User documentation does not need
to present the repository's own canary as a Splashdown product feature.

Relevant upstream contracts are Vite's [getting-started guide](https://vite.dev/guide/) and
[`server.strictPort`](https://vite.dev/config/server-options#server-strictport). The npm lockfile
is installed with [`npm ci`](https://docs.npmjs.com/cli/v11/commands/npm-ci) in the second
worktree.

## Out of scope

- Running the networked canary in `just check` or required pull-request CI.
- Pinning create-vite to make an ecosystem canary deterministic.
- Testing every supported framework or loader on every run.
- Automatically rewriting arbitrary Vite configuration to consume `WEB_DEV_PORT`.
- Building a reusable smoke-test framework before a second scenario needs it.
- Adding the surveyed framework profiles. Those remain separate design and implementation tasks;
  the first popularity-weighted batch is Storybook and Capacitor as integrated capabilities, plus
  Streamlit, Gradio, and Quarkus as recognized profiles. Tauri needs its own capability design.
