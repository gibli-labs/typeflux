# Development loop

Use the same fixed checks locally, in CI and in auto-fix. The project graph supplies
bounded context and explains impact; it does not execute commands or select away
mandatory validation. `tools/development/checks.json` defines stable check IDs and
CI display names; the Python runner owns their executable commands.

Graph checks must reference registered execution IDs in `tools/development/checks.json`
(such as `python-3.12`), rather than verifier names (`python`) or CI display names.
Normal graph validation reports unknown IDs with the declaration path, check ID and
registered replacements; missing/malformed working-tree registries fail as well.
Historical base/head reads use their own registry data without executing revision
code. Immutable pre-registry snapshots retain their original job names
for impact, while current worktrees require the registry. These static checks do
not replace required CI or turn missing/skipped/stale/failed evidence into passes.

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
The development runner invokes the installed Playwright CLI directly. On a command
timeout or normal SIGINT, it sends SIGINT to that owned CLI and allows up to the
existing ten-second cleanup budget for Playwright to stop its detached fixture
servers and finish native reports. The original timeout/interruption still fails;
interruption returns 130 after saving the receipt and any reported test identities.
An additional interrupt during the cleanup wait escalates to the owned command
group's forced stop while reaping continues. A group disappearing between a
liveness check and signal delivery is treated as already exited. Cleanup retains
the original command failure or interruption; an interrupt first received during
otherwise normal cleanup is propagated after reaping. No unrelated process search
or port-based termination is used.
This does not mask signals: an interrupt outside the protected wait/signal work,
including during exception handling itself, can still interrupt cleanup.
If the CLI does not exit, the runner force-stops its command group. An uncatchable
kill or stalled teardown cannot guarantee detached-server cleanup or a complete
native report. Keep the failed evidence and verify ownership before manually
stopping leftover processes; the runner never kills by port or reuses an existing
server. Subsequent checks still refuse occupied fixture ports.
The overview (including project switches), drift, governance and security smoke
checks wait up to 15 seconds for named successful GET responses and pending API
reads before asserting content. The drift aggregate names deployments as well as
both environment bundle reads; a six-second delay specifically holds its real
deployments response. Other real fixture responses also include controlled
six-second delays to guard against headings being mistaken for data readiness.
The plan-row assertion retains its default five-second content budget; existing
content/probe budgets and disabled retries remain unchanged. Failure probes cover missing data, unexpected
errors retained after a later success, allowed outages and permanently pending
requests. Allowlisted error responses never count as successful data. A drift-specific probe
requires deployments even after bundles succeed, rejects an allowed deployment
outage, and requires a deliberate successful response before readiness. These
checks do not diagnose backend/host latency or extend the 15-second readiness bound.

The run-URL smoke still uses real execution enumeration against the unreachable
fixture Temporal service. Single-workflow lists follow the same no-client-retry
policy as the matrix, so a bounded unavailable read is not automatically repeated.
Controlled default/project-scoped regressions require one request to show the
503 error and verify that a deliberate reload can render recovered execution data.
They retain the API-health observer and do not stub the original cold-load listing.

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
fake providers. That lane also runs the shared eleven-case local/Temporal
computational fixtures through actual Python and TypeScript workers and local SDK
adapters, including registry conflict admission. The lane also executes both
documented offline-to-Temporal adoption scripts. Separate foreground CLI subprocess tests in the Python and TypeScript
lanes use SIGKILL after a completed first-step effect, then verify a fresh command
repeats it and executes from the beginning. Their append-only test logs observe
external effects; they are not runtime checkpoints or a persistence service.
Neither lane proves behavior in a deployed environment.

Finish local validation before committing with sign-off, pushing once, and opening
the PR. Batch corrections from reviews. Avoid repeated reviewer invocations on work
in progress. Source changes invalidate earlier evidence even when filenames remain
the same. Local journals and receipts are diagnostics, not trusted CI attestations.

## Host interruptions during local verification

Each development command samples wall and monotonic clocks once per second.
Receipts include bounded `hostDiagnostics`: five-second sampling gaps or clock
divergence are reported as a possible host interruption, with at most eight
samples per command and a count of omitted samples. This is available wherever
Python's clocks and threads are supported. Unavailable sampling is recorded with
`available: false`; it does not prevent verification.
The direct Temporal CLI version probe and server-readiness loop are outside these
command observations; an absent gap record cannot diagnose an interruption there.

A gap does not identify its cause. Sleep, CPU contention, debugger pauses and
clock adjustments can look alike; no observed gap does not exclude suspension.
The observer does not read application payloads, change timeout budgets, retry
tests, or convert failed/skipped/missing/stale evidence into a pass. Command errors,
native test identities, source digest and failed receipts remain authoritative.
An interrupt writes a failed receipt before propagating the interruption.

On macOS, explicitly prevent **idle** system sleep for one local invocation:

```sh
uv run --project packages/python python -m tools.development run default --keep-awake
```

This starts `/usr/bin/caffeinate -i -w` for the runner PID and stops that helper on
normal exit, failure or interruption. It changes no persistent power settings and
does not prevent lid-close or explicit system sleep. `localPowerControl` records
whether the helper was requested and started; starting it is not proof that sleep
was prevented. The option is rejected in CI. On other platforms, or if caffeinate
cannot be launched, verification continues with an explicit unsupported/unavailable
message. Use the host's existing power controls if needed; no helper is installed.

Before rerunning a failed check, copy its `.project-graph/evidence/<check-id>/`
directory, console `test-results/` (including retained-on-failure traces), and the
terminal log into an ignored incident directory such as
`.project-graph/diagnostics/incident-name/`. A rerun reuses the normal evidence and
browser output paths. Preserve the original receipt's source digest, command
outcome and failed identities; a later pass must not erase the earlier failure
from the incident record.

Compare the recorded gaps with browser trace event gaps and host logs. On macOS,
`pmset -g log` can help correlate sleep/wake events with the invocation's local time
window. Logs can contain private machine details; keep them local or redact them
before sharing. Treat the cause as unknown unless supported by observations. Fix
application/readiness regressions separately, and rerun unchanged inputs after
addressing host conditions to obtain fresh evidence. Required CI remains full.

## CI acceptance and release

Pull requests must target `main` to obtain accepted CI evidence. For dependent
changes, follow [the dependency PR workflow](dependent-pull-requests.md): merge
prerequisites first, refresh the dependent branch against current main, and obtain
fresh checks and review for its actual head and integration revision. Feature-base
PRs are not an accepted merge path, even if their ordinary CI jobs pass.

The `CI` workflow runs on PR merge snapshots and main commits. It uploads evidence
for every required check and both browser traces on failures. A separate
`workflow_run` workflow loads the acceptance implementation from main. Maintainers
can also dispatch a fresh evaluation on main for an existing CI run/attempt; both
paths check out only main and use the same validator.
It reads GitHub run/job metadata and bounded JSON inside artifacts; candidate Git
objects are inspected without checking out or executing candidate source.

Acceptance requires exact run/attempt/source identity, all base and proposed job
obligations, the trusted registry, and every declared journey test in every mapped
lane. Test output is still candidate-produced data; the gate does not independently
reexecute it. Protected automation paths and review remain part of the trust model.
Keep existing test IDs while retiring their declarations, then remove the old tests
in a subsequent change; a PR cannot erase its own base obligations.

When the validator reaches job validation, it retains a unique JSON file under
`.project-graph/accepted/job-observations/` for that evaluation, including run ID,
attempt, source revisions, expected/missing/non-passing trusted job names and the
observed job identities, statuses and conclusions. Required jobs are listed first;
at most 100 jobs and 128 characters per observed text field are retained, with
omitted-job and truncated-field counts. These limits affect diagnostics only.
The trusted-job failure summary also includes missing/non-passing names and their
retained statuses. The summary JSON is capped at 16,000 characters, with a
separate omitted-entry count (or a truncation notice for an oversized trusted
registry); the observation file retains its own bounded detail. No step logs or freeform payloads are copied into this record.

The acceptance workflow uploads these files even on failure in a separate
`ci-job-observations-<CI-run>-<CI-attempt>-<acceptance-run>-<acceptance-attempt>`
artifact. Accepted evidence and the explorer use
`accepted-ci-<CI-run>-<CI-attempt>-<acceptance-run>-<acceptance-attempt>`.
Neither upload overwrites earlier artifacts. To preserve a rejected evaluation,
create a **fresh acceptance run**, rather than using GitHub's **Re-run jobs**:

```sh
# Use the original CI run ID and its current attempt, as positive decimal integers.
gh workflow run ci-acceptance.yml --repo gibli-labs/typeflux --ref main \
  -f run_id=CI_RUN_ID -f attempt=CI_ATTEMPT
```

The command reevaluates existing CI evidence; it does not rerun tests, retry
automatically, publish a release or deploy. Dispatches on other refs are skipped.
Same-run acceptance reruns are rejected before validation, and their evidence is
ineligible for release; recover with a fresh dispatch. This cannot prevent GitHub
from removing artifacts when an external rerun is requested, so archive first.
The original workflow/check/logs and diagnostic artifacts remain separate from the
fresh run, subject to normal GitHub retention and external deletion. Archive the
original evidence before any operation that might remove it. For local
investigations, copy the output directory before reevaluation: unique observation
files are retained locally, but `ci-receipt.json` and the explorer reuse their paths.

The [#139 probe](https://github.com/gibli-labs/typeflux/issues/139#issuecomment-6664464521)
did not reproduce a same-name upload conflict: both attempts succeeded with
`overwrite: false`. After a rerun of the same workflow run, however, both fixed-name
and attempt-specific original artifact IDs returned 404. A separate fresh run left
the previous run's artifact accessible. Unique names alone therefore did not prove
retention across GitHub reruns; this is observed API behavior, not a diagnosis of
GitHub's storage implementation.
An observation records only the metadata seen at that evaluation. A later success
does not explain an earlier rejection, prove eventual consistency or replace its
failure evidence. Observations do not retry jobs, grant acceptance, relax required
checks or authorize a release.

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
revision. It selects the latest main CI run and its current attempt, then the latest
matching main acceptance evaluation by the title `CI acceptance <CI-run> attempt <CI-attempt>`.
The history query starts at the native CI run's creation time; skipped non-main
dispatches are excluded. Artifact discovery is scoped to the selected workflow run,
so unrelated uploads under the same name cannot block its owned evidence.
That evaluation must be completed and successful, come from the trusted main
acceptance workflow's first attempt, and own exactly one bounded unexpired artifact with the full
run/attempt name. A failed, queued, incomplete or missing latest evaluation never
falls back to an older success. The receipt still binds the original CI run/attempt,
exact source revision/tree and passing journeys. Evaluator source must be identical
to or an ancestor of the explicit `refs/heads/main` commit, resolved through the Git
reference API and compared by SHA; a same-named tag cannot supply unreviewed source.
Its artifact receipt attaches that
verification to artifact byte hashes. Legacy canonical artifact names are not a
fallback; dispatch a fresh evaluation on main if only those exist. If the underlying
native CI evidence is expired/missing, run fresh full CI on that main revision; PR merge
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
