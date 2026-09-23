# @typeflux/temporal-controlplane

```bash
npm install @typeflux/temporal-controlplane   # Node 22
npx typeflux-controlplane serve --registry ./typeflux.projects.yaml --port 8420
```

The **control plane** for the Typeflux TypeScript SDK — the parity of Python's
`typeflux.controlplane` plus the operate slice of `.project`. It turns a
loaded project bundle into the control-plane API's read DTOs, operates
plan-as-argument executions over Temporal, and serves both over HTTP against the
language-neutral [`contracts/controlplane`](../../../contracts/controlplane)
OpenAPI contract (`API_VERSION=1`), so the same generated client and console work
against either the Python or the TypeScript server.

Three layers, each usable on its own:

## 1. Read/validate projections — `ProjectControlPlane`

Pure, in-memory projections over one loaded project bundle (no Temporal, no HTTP):
meta, workflow/environment listings, validation, the resolved-workflow **bundle**
(identity + secret-safe runtime + policy + topology), the **activity catalog**,
policy/profile definitions, workflow **prompt status**, the normalized
**enforcement-events** feed (#723 — admission verdicts from the validation report
merged with bounded runtime moderation-block verdicts read from Langfuse), and the
**github-provenance** projection (#727 — the `/github-provenance` endpoint: HEAD-vs-served
drift + plan→approving-PR, read at request time through an injected GitHub reader seam).
Component profiles are composed into the resolved overlay (#568), so a profiled read reports
the profiled cluster.

The `github_provenance` capability is **false** on this edition: the registry serves local
project checkouts only and records no git source, so the served side is always null and the
surface reports `not_configured` with no network call. The seam and the full read/assembly
logic are ported all the same (behavioral parity), so it lights up automatically if the
registry ever gains git sources — there is deliberately no CLI flag to force it on.

```ts
import { ProjectControlPlane } from "@typeflux/temporal-controlplane";

const cp = new ProjectControlPlane(bundle, { schemas, manifestPath: "acme/typeflux.project.yaml" });
cp.meta();
cp.bundle("review", "prod");                    // resolved workflow bundle for review@prod
cp.validate({ environmentId: "prod" });         // project validation report
```

Status mapping mirrors the contract: an unknown workflow/environment is 404, a
broken spec or undeclared profile selection is 422 (`ProjectControlPlaneError`
carries the status + error name).

## 2. Operate tier — `WorkflowOperations`

`start` / `status` / `submitReview` / `requestCancel` / `migrate` over `@temporalio/client`,
against the `typeflux-binding` contract's `ts-plan-argument` profile: the workflow
type is `typefluxYamlWorkflow`, the derived plan is passed as a start argument, and
identity lives in the execution memo. Every operate op runs a shared prelude that
resolves + profiles the spec, then **composes and admits the selected policy**
(explicit `policy_ids` or the project's target-derived selection, with
`expected_policy_hash` verification, #663) — all before touching Temporal, so a
policy failure is a 422, never a 503. Admission includes the #55 **transitive-closure
check**: a workflow that references sub-workflows (`workflow:` / `map.workflow`) has
every referenced child re-validated against the parent's composed policy
(`policy_subworkflow_closure`), so a non-compliant child fails the parent's start. `status`/`review`/`cancel` verify the
execution's memo binds to this project/workflow and fail closed with a 409
`LifecycleBindingError` otherwise.

The operate tier is bounded: gRPC deadlines ensure a timed-out mutation cannot
land, and an unreachable cluster surfaces as a 503 `TemporalUnavailable`. Input is
validated against the workflow's injected schema before dispatch (422 on
mismatch, Python `_coerce_input` parity).

`migrate` (#204) is the long-drain primitive — terminate-and-resubmit with input
carry-over, addressed like cancel/review (`execution_id` + optional `run_id`).
Every start-leg precondition (same-version, serving workers, open gates,
carried-input validation, frozen version) is preflighted BEFORE the terminate; a
terminate racing an already-closed execution is a 409
`MigrateExecutionClosedError`, and a failed replacement start is a distinguished
500 `MigratePartialError` (the carried input stays intact in the terminated
run's history).

Per-call runtime policy enforcement (moderation blocks, provider/model gating) is
the **worker's** half via `assembleYamlRuntime` — the control plane's share is
admission at start. This CP/worker split matches Python exactly.

## 3. HTTP server — `serve` / CLI

`serve(...)` (and `createServer`/`buildRoutes` for embedding) exposes the read +
operate tiers over the contract's routes, loading a `typeflux.projects.yaml`
registry. Auth is **open by default** (a local server trusts its caller);
bearer-token grants and reverse-proxy actor headers are opt-in.

```sh
# from the repo root — build first (the server runs the compiled dist):
(cd packages/typescript && pnpm -r build)

node packages/typescript/temporal-controlplane/dist/http/serve.js \
  --registry path/to/typeflux.projects.yaml \
  --port 8411 \
  [--host 127.0.0.1] \
  [--auth-token NAME:PERMS:TOKEN]   # repeatable; PERMS comma-separated or *
  [--trust-proxy-auth]              # trust X-Typeflux-Actor / -Permissions from a proxy
  [--langfuse]                      # inject the fetch-based langfuse reader (#573) so the
                                    # connections probe + prompt-status drift go live
```

The embedding host injects the project's activity IO **schemas** (`schemasFor`) —
start validates the caller's input against them, exactly as bundle/catalog do.
It can likewise inject a **langfuse transport** (`langfuseFor`, or the `--langfuse`
flag which builds a `fetch`-based reader from `LANGFUSE_PUBLIC_KEY` /
`LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` env): without it the connections
reachability probe, the prompt-status label-drift tier, and the
**enforcement-events** feed's runtime source degrade honestly (langfuse un-probed /
drift `unknown` / enforcement `not_configured`); with it they resolve live. The CP
core holds no vendor client — the transport is a thin structural seam (#499/#573).

## Conformance

Both editions pass the same executable HTTP suite as required CI checks
(`contracts/controlplane/conformance`, run per-edition via `--edition ts-cp` /
`python-cp`). The suite is the living parity matrix: a case is either byte-identical
across editions or carries an explicit, why-noted variant. This package is exercised
by the `ts-cp` lane end to end.

## Behavioral parity, not byte-identical

Snake_case DTO field names match the Python contract JSON exactly so the shared
generated client works against either server. The Temporal binding profile
(`ts-plan-argument`) diverges from Python's (`python-versioned-type`) **by design**
— a permanent ABI divergence — so a TS-started execution is operated by the TS
worker, and vice versa.

## See also

- [Control-plane concepts (Python)](../../../docs/control-plane.md) and
  [auth](../../../docs/control-plane-auth.md)
- [TypeScript docs](../../../docs/typescript) — concepts, YAML runtime,
  observability, provider portability
- [`@typeflux/temporal-yaml`](../temporal-yaml) — the runtime this control plane
  resolves and operates
