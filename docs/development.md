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
The overview (including project switches and insight acknowledgements), drift, governance and security smoke
checks wait up to 15 seconds for named successful GET responses and pending API
reads before asserting content. The drift aggregate names deployments as well as
both environment bundle reads; a six-second delay specifically holds its real
deployments response. Other real fixture responses also include controlled
six-second minimum response times to guard against headings being mistaken for data
readiness. Upstream fetch time counts toward this minimum; the helper does not add
another six seconds to an already slow response. The insight check waits for the
page's bundle and annotation reads, separately from its direct API contract probe.
The plan-row assertion retains its default five-second content budget; existing
content/probe budgets and disabled retries remain unchanged. Failure probes cover missing data, unexpected
errors retained after a later success, allowed outages and permanently pending
requests. Allowlisted error responses never count as successful data. A drift-specific probe
requires deployments even after bundles succeed, rejects an allowed deployment
outage, and requires a deliberate successful response before readiness. These
checks do not diagnose backend/host latency or extend the 15-second readiness bound.

The shared HTTP conformance runner also owns its managed fixture servers. It
preflights every selected profile port before any launch/case, then checks again
immediately before each spawn. An occupied port fails with exit 2 and its existing
listener remains running. A bounded TCP connect-and-close probe sends no HTTP
request; a timeout or other uncertain connection failure refuses the port too.
The check is not an atomic port handoff: use isolated fixture
ports and avoid concurrent runs on the same ports. Explicit `--base-url` mode still
tests an existing server whose lifetime belongs to its caller.

Managed SIGINT/SIGTERM exits use `128 + signal` after owned cleanup. Signal handling
defers interruption inside child creation/ownership assignment and teardown
sections. A brief interruptible boundary remains before a Python guard becomes
active; delivery there can still prevent cleanup. TERM is sent to the owned server group, with
five seconds of grace; a remaining group receives KILL, with bounded direct-child
reaping and group-exit observation (one second each). This fits within the
development runner's existing ten-second cleanup budget. Launcher exit alone does
not establish descendant exit. An active original error/interruption keeps
precedence if cleanup also raises; a fixed cleanup-incomplete message remains
visible. A kill of the controller, unavailable OS scheduling or an uninterruptible
process still cannot guarantee teardown. Retain failed evidence and verify exact
process ownership before manual cleanup; never terminate a listener just by port.
Process-backed checks cover ordinary failure, startup/teardown signal windows,
resistant descendants, existing listeners and an interrupted source-bound receipt.
Container execution needs a reaping init (for example Docker `--init`); the CLI
does not act as PID 1 or reap adopted orphan descendants. Python CI uses Ubuntu VM
jobs, and its native results must still establish these cases actually passed.
These controlled checks do not replace either actual HTTP conformance edition.

The run-URL smoke still uses real execution enumeration against the unreachable
fixture Temporal service. Single-workflow lists follow the same no-client-retry
policy as the matrix, so a bounded unavailable read is not automatically repeated.
Controlled default/project-scoped regressions require one request to show the
503 error and verify that a deliberate reload can render recovered execution data.
They retain the API-health observer and do not stub the original cold-load listing.

The scoped-review transport check also uses the real HTTP/1 proxy and each actual
control-plane edition. It submits one malformed fixed command and requires the
actual 422 validation response naming `user_decision`,
rather than a browser fetch failure. It never fulfills the operation response or
retries the mutation. Separate unit checks retain body bytes, authorization, request
options and AbortSignal through project URL rewriting, including bodyless GETs
when the optional body getter is unavailable. This negative transport
proof requires no provider credentials and does not establish successful dispatch or lifecycle progress; the native
Temporal and deployed checks remain separate obligations.

## Investigating local API fan-out

Use the read-only browser fixture diagnostic when investigating Python bundle or
validation latency. It starts an owned loopback API on an ephemeral port, reuses
the existing resolver, reads both fixture project IDs, then stops that server.
It is not part of CI acceptance and does not exercise the console proxy, browser
or deliberate six-second fixture delays. From the repository root after setup:

```sh
PYTHONPATH="$PWD/packages/python:$PWD" uv run --project packages/python python \
  -m tools.development.api_fanout_probe --workload overview --concurrency 16 \
  --output /tmp/typeflux-api-overview.json
```

`overview` selects every workflow in the local environment; `matrix` selects every
workflow/environment combination. Both include project validation and both project
IDs. Four inventory reads determine the complete workload (currently 30 or 60 reads
per round); the two preplanned rounds share one API process. Cold/warm labels
identify order, not independent samples or guaranteed cache states. There are no
automatic retries. Invalid payloads, HTTP errors, timeouts or changed source inputs
fail the run even if later reads succeed. A false validation result is retained
separately from whether the API successfully delivered a well-shaped report.

The output binds source/graph inputs and dependency versions to each round, with
HTTP elapsed time, client queue delay and resolver wall/thread CPU time. Bootstrap
failures produce a minimal failed report with a fixed reason code; missing source
or timing fields remain incomplete evidence. Exit zero
requires every read to complete and finish within 15 seconds of its round's start,
including queue delay; this diagnostic threshold is not a browser-readiness proof.
HTTP inactivity timeouts remain 15 seconds and streamed bodies also have a total
read budget. Caps are 128 reads per round, 8 MiB per response, 256 resolver samples
and 256 collected profile function labels; omitted counts are explicit. Response
bodies, credentials, exception text and request arguments are not saved. Only fixed
operation labels and SDK profile function identities appear in the report.

Use `--concurrency 1 --profile` for standard cProfile summaries in resolver threads.
Concurrent profiling is rejected before starting the API: CPython 3.12 profiler
instances compete for its monitoring slot. Concurrent runs still collect wall and
thread CPU observations without profiling. Profiling adds overhead; cumulative
frame times nest and must not be added together. The top retained frames may be
incomplete when the collection cap is reached; omission counts and
`profileCollectionComplete` make that explicit. Do not infer hottest paths from a
truncated collection. Wall time minus thread CPU time
includes waiting and scheduling and cannot alone attribute a cause.

The diagnostic uses synthetic unreachable Temporal values and scrubs inherited
provider credentials. Run it against this fresh fixture checkout, without private
environment files. It performs no workflow execution, provider calls, runtime
metadata contribution or execution-manifest change. Keep original failed browser
receipts and traces separately; later passing diagnostics do not diagnose them.

For a resource comparison, `tools/development/api_fanout.Dockerfile` builds a local
dependency image using the committed Python lockfile and digest-pinned base/uv
images. It is not a worker/release image. Build with an explicit context containing
only that Dockerfile and tracked `packages/python/{pyproject.toml,uv.lock,README.md,
LICENSE,src}` files. Use the same built image ID for every comparison; retain it,
the Dockerfile hash, dependency versions, workload, exact Docker command, CPU/memory
limits, exit/OOM status and output JSON. Do not overlap with another default profile.
Bind the candidate source and its Git common directory read-only at their existing
absolute paths so Git worktree links and provenance reads behave normally. Set
`PYTHONPATH` to the candidate's `packages/python/src`, `packages/python` and root,
and scope Git's `safe.directory` setting to that checkout through container-only
environment variables. Run with `--network none`, a PID cap, dropped capabilities,
`no-new-privileges`, an owned label/name/CID and a dedicated writable results mount.
Compare `--cpus 4` versus `--cpus 1` with `--memory 2g --memory-swap 2g`; bound each
container to 180 seconds. Retain failures, verify the exact CID's ownership label
before timeout cleanup, and remove only containers created for the comparison.
These API fan-out measurements do not exercise worker recovery; see the
[Docker deployment runbook](deployed-review-verification.md) for separate target evidence.

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
documented offline-to-Temporal starters, recovery/timeout demonstrations and native
delayed-start examples. The delay examples inspect recorded backoff and task
timestamps, same-run completion and validated output, with Python observing a
pending task before its worker starts and TypeScript using an available worker.
It also executes both documented lifecycle-review SDK demos, verifying the
observed gate before a pinned decision and the routed lifecycle/result afterwards.
Operation/HTTP tests separately cover binding, permissions, malformed commands,
unavailable-before-dispatch and uncertain-response reconciliation; these doubles
do not establish live network delivery or deployed recovery.
The lane also runs the existing native compensation cases and both documented
booking/charge adoption scripts: original forward failure, reverse unwind,
partial compensation and a server-acknowledged pending compensation retry finished
by a replacement worker on the same run. Terminal lifecycle queries are observed
while a compatible worker still polls. This is isolated worker replacement, not
deployed OS/server-loss or incompatible-code recovery.
Separate foreground CLI subprocess tests in the Python and TypeScript
lanes use SIGKILL after a completed first-step effect, then verify a fresh command
repeats it and executes from the beginning. Their append-only test logs observe
external effects; they are not runtime checkpoints or a persistence service.
Neither lane proves behavior in a deployed environment.

The isolated Temporal lane also exercises the native TypeScript no-poller status
condition with a separately owned loopback CLI/SQLite service and worker. A gate
and acknowledged first activity precede worker stop and service kill/restart. The
same run must produce the actual native code 9 and recognized no-poller details;
the real API then returns 503 with compatible-worker guidance and zero review
signals. A never-polled queue's ordinary deadline does not satisfy that proof.
The test uses the maintained pinned CLI, checks its owned startup output, retains
the service log under Temporal evidence, and cleans up its own processes/data.
This does not establish cloud or arbitrary worker-upgrade recovery. Client/query
error classification adds no runtime metadata or execution-manifest event.

For a separately selected non-production target, use the
[Docker deployment runbook](deployed-review-verification.md). It freezes accepted
main/artifact/graph/image identities before real API/console observations, tests
same-run persistent-service and worker interruption, replay/version/drain and
uncertain-response reconciliation, then verifies owned teardown. It is optional
environment observation, not a new default CI lane or a production recovery claim.
The published record must distinguish the actual passing scope from retained
failures and unknowns; a local report never replaces trusted release evidence.
The [observed result](deployed-review-verification.md#observed-result) records the
passing Python/TypeScript target, retained failures and current promotion limits.

The isolated Temporal lane also runs the existing Python sandboxed YAML workflow,
both child-composition executions, and YAML subworkflow IDs/memo/map and occupied-child
collision proofs against its owned, pinned server with fake providers. Search-attribute
registration must succeed or report the typed already-exists RPC status; unsupported,
permission and backend errors fail without an alternate-server fallback. The existing
15-second registration and 60-second workflow-result deadlines remain unchanged, as
does cleanup of test-owned workflow IDs. Their test identities and workflow assertions remain unchanged; they
are selected explicitly there and marked `live` so the ordinary Python matrix
only runs their non-live structural/unit checks. They do not download an SDK
time-skipping server or convert connection/import/execution failures into skips.
Run the maintained `temporal` verifier to execute them; simply setting
`TYPEFLUX_LIVE_TEMPORAL=1` does not establish server ownership or a passing result.

Native pytest results and their source-bound receipts retain `skipDiagnostics`
for skipped setup/call/teardown reports, including skips after a failed call.
Each entry contains a fixed phase, `reasonCode` and `detailsOmitted` flag. Exact
reviewed prerequisite reasons identify `node-resolver-prerequisite`,
`isolated-temporal-lane-required`, `git-prerequisite` or
`temporal-sdk-prerequisite`; expected failures use `expected-failure`. Everything
else is `unclassified`, with details omitted. No arbitrary reason, exception text,
source snippet or fixture input is copied into these diagnostics. At most three
entries are retained per collected test, with `skipDiagnosticsOmitted` counting
additional reports. Collection-time skips that produce no collected test identity
are outside this per-test record. Codes describe reported prerequisites, not a
diagnosis of infrastructure or product health. Failed status takes precedence,
and a classified skip still cannot satisfy a required passing test.

The historical Temporal skips tracked in [#145](https://github.com/gibli-labs/typeflux/issues/145)
have no retained reason; later passing executions cannot recover that information.
Keep those original reports separate from new evidence. Other explicitly opt-in
live tests remain separate coverage obligations; these required proofs do not certify
every live test.

Graph identity collection uses the installed package-local Vitest and Playwright
CLIs through Node. It does not invoke a package manager or install missing runners:
using pnpm for an npm-managed client can convert its dependency directory and create
a new lockfile, invalidating the source-bound inventory. Missing frozen setup,
missing declared tests and any source changes still fail collection. Keep the
original failed evidence; collection alone never establishes test execution.

Finish local validation before committing with sign-off, pushing once, and opening
the PR. Batch corrections from reviews. Avoid repeated reviewer invocations on work
in progress. Source changes invalidate earlier evidence even when filenames remain
the same. Local journals and receipts are diagnostics, not trusted CI attestations.

The TypeScript deployment-plan Git fixture uses real local repositories under an
isolated synchronous Git environment. It ignores inherited Git configuration,
routing, signing and template controls, uses empty owned config/template files,
and keeps native Git tracing available. The caller's Git environment is restored
on success or failure; all fixture repositories join the file's existing owned
cleanup. The production merged-plan verifier still uses its caller's Git context.
Regression checks use actual commits with poisoned inherited config/routing and
verify restoration after a deliberately raised failure. The original merged,
tampered, unmerged, symlink and outside-repository assertions remain in one case.
The existing five-second test budget and disabled retries are unchanged.

The [#131 investigation](https://github.com/gibli-labs/typeflux/issues/131) reproduced
a timeout with a controlled external hook: two three-second hook executions made
the unchanged case fail after roughly 6.78 seconds, versus a 336 ms isolated
baseline. This establishes a fixture-isolation defect, not the cause of the
historical timeout. Native timings, source identities and original/later outcomes
remain separate; host or suite scheduling is not diagnosed by a later pass.

## Host interruptions during local verification

Each development command and the isolated Temporal preflight/startup phases sample
wall and monotonic clocks once per second using the same observer.
Receipts include bounded `hostDiagnostics`: five-second sampling gaps or clock
divergence are reported as a possible host interruption, with at most eight
samples per observation and a count of omitted samples. This is available wherever
Python's clocks and threads are supported. Unavailable sampling is recorded with
`available: false`; it does not prevent verification.
Command records retain their zero-based `commandIndex`. Fixed operation records
named `temporal-preflight` cover the occupied-port check and CLI version probe;
`temporal-startup` covers server launch and readiness. They do not invent command
indexes or include application payloads. Startup observation ends before the native
test commands, which keep their own command observations. Failure and interruption
still retain observations in the exact-source receipt.

Temporal version stdout/stderr is retained separately in `temporal-version.log`
and `temporal-version.stderr.log`, including failed/interrupted version-command
output, and server output remains in `temporal.log` under
the Temporal evidence directory. Preflight clears these three current log paths
before checking the port, so an early refusal cannot leave a prior invocation's
logs presented as current output. Copy the previous evidence directory before
rerunning, as described below. The CLI 1.7.0 requirement, refusal to reuse an
occupied port, 45-second monotonic readiness deadline, polling and owned-server
cleanup remain unchanged. This adds no version-probe or whole-startup timeout;
the existing readiness deadline starts after launch. Startup cleanup after a failed
readiness operation is outside its observation; gaps there remain unknown.

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

## Remote verification when the local host is unavailable

Finish implementation and focused local validation before publishing a completed
branch. If the local host sleeps or otherwise cannot finish full verification,
retain the failed receipts and record exactly which checks remain unpassed. A
draft PR can provide visibility and the existing remote CI environment while that
verification is incomplete. Do not describe local failures as passing or mark the
PR ready until complete current-source native CI passes and implementation is
finished. Merge still requires separately required review and trusted acceptance
on the current head. This grants no new merge or release authority.

A branch `workflow_dispatch` may supply independent native observations, but it
is not trusted PR/main acceptance. Prefer creating the draft PR before starting
its CI rather than dispatching CI on the same head before PR creation. In the
observed [#165](https://github.com/gibli-labs/typeflux/issues/165) sequence, a later
successful trusted acceptance check stayed in the earlier dispatch suite and was
absent from the PR rollup. A green REST check alone did not unblock protected
main. Keep the run, attempt, head, suite IDs, app identity and failed observations
recorded individually; do not infer protected merge readiness from the native run label.

Use this recovery only when all required native checks and other protected
checks have passed on the current head, and the successful trusted acceptance is
associated with the wrong suite. It is not a retry path for a failing check.
Verify that evidence first. A documented head update (a necessary coherent change, or an explicitly explained signed-off
empty commit) gives the PR fresh check suites. Reconcile prior findings, inspect review identity and obtain required review
for the updated head. Then require all native CI again on that new source/head and verify that the
trusted current-head `CI acceptance` is present in the PR rollup. Never manufacture
a status, delete a failed check, change branch protection, rerun failed product
tests to green or reuse the older head's acceptance. This is an operational
recovery, not a CI-publisher redesign or support guarantee for arbitrary suite
combinations. Exact-main accepted CI is still required before any publication.

## Matched development evaluation

The [graph development pilot](graph-development-study.md) is an explicit paid
experiment, separate from the default development profile and Fable code reviews.
Its frozen protocol, owned offline Docker checks and cost ledger do not establish
full agent productivity, trusted CI or permission to publish.
