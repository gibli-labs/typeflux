# Development loop

Use the same fixed checks locally, in CI and in auto-fix. The project graph supplies
bounded context and explains impact; it does not execute commands or select away
mandatory validation. `tools/development/checks.json` defines stable check IDs and
CI display names; the Python runner owns their executable commands.

## Reproducible setup

Use Node **24.22.1**, pnpm **12.15.0**, uv **0.13.5**, and Python **3.12** for the
primary loop. CI additionally tests Python 3.11–3.15. Install these tools using their
normal installers, then run from the repository root:

```sh
uv run --project packages/python python -m tools.development sync all
```

Setup uses the committed uv/pnpm/npm lockfiles. It builds SDK and generated-client
dependencies, installs Chromium, and installs checksum-pinned Temporal CLI 1.7.0
under ignored `.project-graph/bin/`. On a Linux host, install Playwright's documented
system prerequisites once (`npm exec --prefix clients/console -- playwright
install-deps chromium`); CI uses the version-matched, digest-pinned Playwright image.
The console consumes the candidate client through a local package dependency, so a
contract/client/console change can be completed in one PR. The public client tarball
still has its independent installation check when released.

Narrow setup scopes are `python`, `typescript`, `web`, `mcp`, `browser`, `temporal`,
and `graph`. A scope installs prerequisites; it does not certify any tests passed.
No provider/API credentials are needed for the default checks. Port 7233 must be
free for isolated Temporal verification: the runner refuses to reuse another server.

Browser fixtures run separately from Temporal. The Python API receives fixed local
Temporal address/namespace values so all example environment bundles can be read
without credentials. Both browser editions assert background API responses and
wait for pending reads; deliberate outage and invalid-fixture responses are scoped
explicitly in `clients/console/e2e/fixtures.ts` and the relevant specs. The bundle
matrix also checks every fixture workflow/environment under both project IDs.
The drift/governance smoke checks wait up to 15 seconds for named successful GET
responses and pending API reads before asserting content. Their real fixture
responses include a controlled six-second delay to guard against headings being
mistaken for data readiness. Content assertions retain the default five-second
budget; retries remain disabled. Failure probes cover missing data, unexpected
errors retained after a later success, allowed outages and permanently pending
requests. Allowlisted error responses never count as successful data.

## Implement and verify

1. Read `AGENTS.md`, the issue and relevant source. Retrieve graph context for a
   modeled capability; use code search when the graph cannot answer the question.
2. Record intent/acceptance and progress in a local graph task journal for work
   spanning sessions. Reconcile any pending external effects before retrying.
3. Implement a coherent change. For a defect, demonstrate the failing behavior
   and then the fix. Generated contract types come from the normative OpenAPI file;
   never hand-edit generated types or preserve obsolete pre-1.0 shims by default.
4. Run focused checks, then the shared full profile:

```sh
uv run --project packages/python python -m tools.development list
uv run --project packages/python python -m tools.development run python web
uv run --project packages/python python -m tools.development run default
uv run --project packages/python python -m tools.project_graph report --base STARTING_SHA
```

`default` runs Python quality/build/non-live tests, TypeScript build/typecheck/tests,
client generation/drift, console tests/build, MCP checks, both HTTP conformance and
browser editions, isolated Temporal progression, and graph validation/collection.
It stops at a failed check; inspect its receipt and rerun the affected checks after
fixing it. Use smaller validation for documentation-only changes. CI remains full.

Receipts and native reports live in `.project-graph/evidence/<check-id>/`. Each receipt
binds the source commit and input digest to commands and actual per-test outcomes.
Collection, execution, and passing assertions are distinct. Skipped, missing,
failed, or stale tests cannot establish a passing journey. Browser operation tests
use controlled responses; the separate Temporal lane uses real local execution and
fake providers. Neither proves behavior in a deployed environment.

Finish local validation before committing with sign-off, pushing once, and opening
the PR. Batch corrections from reviews. Avoid repeated reviewer invocations on work
in progress. Source changes invalidate earlier evidence even when filenames remain
the same. Local journals and receipts are diagnostics, not trusted CI attestations.

## CI acceptance and release

The `CI` workflow runs on PR merge snapshots and main commits. It uploads evidence
for every required check and both browser traces on failures. A separate
`workflow_run` workflow loads the acceptance implementation from the default branch.
It reads GitHub run/job metadata and bounded JSON inside artifacts; candidate Git
objects are inspected without checking out or executing candidate source.

Acceptance requires exact run/attempt/source identity, all base and proposed job
obligations, the trusted registry, and every declared journey test in every mapped
lane. Test output is still candidate-produced data; the gate does not independently
reexecute it. Protected automation paths and review remain part of the trust model.
Keep existing test IDs while retiring their declarations, then remove the old tests
in a subsequent change; a PR cannot erase its own base obligations.

Main protection requires `CI acceptance`, Dependency Review, DCO, and the Python,
TypeScript and Actions CodeQL checks from GitHub Actions. The settings checker
preserves unrelated protection settings:

```sh
uv run --project packages/python python -m tools.development.protection
# Maintainer-authorized reconciliation, only after acceptance passes on main:
uv run --project packages/python python -m tools.development.protection --apply
```

Initial migration: existing required checks protect the introducing PR; once its
main CI and trusted acceptance pass, apply the new required-check contract. Do not
configure an unavailable check and strand that PR. Subsequent policy changes follow
the same checked, deliberate retirement procedure.

Every release first downloads accepted **main** CI evidence for its exact source
revision. Its artifact receipt attaches that verification to artifact byte hashes.
Expired/missing evidence requires fresh full CI on that main revision; PR merge
receipts cannot stand in for squash-merged source. Registry publication remains
behind `npm`, `pypi` or `testpypi` environments. Target-environment smoke, observation,
recovery and worker-drain evidence remains separate; no automatic deployment is added.

## Skills and auto-fix

Repository-maintained skill instructions are under `skills/`. To update the three
installed workflow skills without replacing helper scripts or UI metadata:

```sh
python scripts/install_development_skills.py
python scripts/install_development_skills.py --check
```

The auto-fix wrapper uses the same full profile and records intent, verification,
push and PR effects in the task journal. Hosted dispatch accepts an optional graph
capability. Its label/protected-path and environment approval rules remain in force;
architecture and public-contract changes use the normal reviewed development loop.
Use explicit Codex `--sandbox read-only` for planning and `--sandbox workspace-write`
for implementation. Obsolete flags are rejected rather than translated into broader
permissions. See [auto-fix details](auto-fix.md).

Graph-assisted productivity and safe selective CI remain hypotheses. Compare matched
tasks for correctness, review effort, time, cost and missed impact before expanding
automation. Passing this development loop does not complete the larger evaluation or
deployment obligations tracked in issue #46.
