# Project graph development tool

Run from the repository root, after `uv sync --project packages/python --frozen --extra live --group dev`:

```sh
uv run --project packages/python python -m tools.project_graph validate
uv run --project packages/python python -m tools.project_graph --output .project-graph/snapshot.json build
uv run --project packages/python python -m tools.project_graph --format text context capability.workflow-review
uv run --project packages/python python -m tools.project_graph explain file:clients/console/src/panels/runs.tsx check.review-browser
uv run --project packages/python python -m tools.project_graph impact --base <indexed-base-sha> --head <indexed-head-sha>
uv run --project packages/python python -m tools.project_graph --output .project-graph/collection.json collect
uv run --project packages/python python -m tools.project_graph --output .project-graph/navigation.json benchmark
```

Omitting `--revision`/`--head` reads the working tree, including nonignored new files.
Historical reads use Git objects without checking out or executing that revision.
Snapshots hash tracked input content and executable modes, retain input digests,
and label dirty workspaces. Generated snapshots belong in ignored `.project-graph/`
or CI artifacts, not source control. Dirty results are not release evidence.

## Authoring

YAML catalogs in `project-graph/` describe reviewed semantics. JSON Schema validates
their shape; compilation checks unique identities, references, typed endpoints,
nonempty path selectors, exclusive primary file ownership and test declarations.
Selectors use case-sensitive Python `fnmatch` (a `*` can span `/`). `exclude`
removes paths from a component. Capability membership may be many-to-many.

Package manifests, OpenAPI operations and CODEOWNERS are extracted. Local
workspace package dependencies propagate implementation impact; published pins
remain external dependencies, even when this checkout contains a same-named
package. Dependency constraints retain their manifest group. Lockfile changes
always request broad validation; the graph does not claim to resolve every
transitive lockfile dependency. CODEOWNERS uses last-match semantics for the
repository's current wildcard rules; unsupported pattern syntax fails validation.

The initial component inventory covers the repository broadly; detailed capability
and journey coverage starts with workflow review. Unmapped files are returned
explicitly. File/package/API extraction is not a complete symbol/import index:
dynamic wiring, undeclared cross-component dependencies and deep language-level
references remain limitations. Absence of a path never proves independence.

## Context and impact

Context returns purpose, requirements, implementation, docs/examples and checks,
with inclusion paths and provenance. Output is bounded by item/byte budgets, with
an omitted count and explicit expansion. Graph text is data, not an instruction
or executable command. JSON and text views use the same query implementation.

Impact traverses both base and head graphs, so deleted relationships remain
visible. Cycles are safe. Rename hints are exact-content matches only; both paths
are still considered. API changes reach consumers; workspace dependencies reach
dependents. Documentation links do not propagate like implementation edges.
Unknowns, contracts, tooling, policy and build inputs request broad validation.
Every recommendation is advisory; existing mandatory CI remains authoritative.

`validate` checks static test references, never claims they executed. `collect`
uses pytest, Vitest and Playwright's actual collection interfaces and verifies all
declared identities, including the gated local Temporal tests. It imports test
code, strips credentials from child environments, and refuses a record if inputs
change during collection. Run it only in an unprivileged checkout, like tests;
environment scrubbing is not a security sandbox. Collection is not a pass result.

## Journey evidence

The browser scenarios in `clients/console/e2e/review.spec.ts` run with both control
plane read surfaces and controlled operation responses. They verify the UI's
decision, identity, denial and recovery behavior; they do not establish Temporal
execution. Local Temporal evidence comes from the existing fake-provider harnesses:

```sh
temporal server start-dev --ip 127.0.0.1 --headless
# Build the TS workspace first. Run each harness sequentially against this local server.
TYPEFLUX_LIVE_TEMPORAL=1 uv run --project packages/python pytest packages/python/tests/test_live_ts_binding.py -q
cd packages/typescript/temporal-controlplane
TYPEFLUX_LIVE_TEMPORAL=1 pnpm exec vitest run test/live-operate.test.ts
```

These cover both control planes operating TS executions; they do not imply all
Python workflow runtime variants have been exercised. They acknowledge a review
and then separately observe lifecycle completion, and check foreign binding
refusal. No provider credentials or production services are needed. The workflow
execution manifest and runtime metadata remain unchanged.

## Evaluation

`benchmark` compares graph context with a bounded lexical file-search baseline
over twelve curated navigation tasks. It reports hits, returned context and
elapsed time, including misses. It is a diagnostic with known authoring bias,
not an agent productivity study or evidence for reducing mandatory CI.

The larger study should assemble 30–50 matched development tasks across SDK,
contracts, console, docs and deployment; compare baseline, graph queries and
context assembly with matched models/tools/budgets and repeated trials. Historical
tasks must reconstruct declarations from information available at their original
revision. Keep held-out acceptance tests outside agent context. Record correctness,
human correction time, elapsed time, spend, infrastructure failures, missed
dependencies and successive-change quality. Model/spend approval and environment
rollout evidence remain tracked by issue #46.
