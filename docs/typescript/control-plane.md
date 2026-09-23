# Control Plane (TypeScript)

> The [Control Plane](../control-plane.md) and
> [Control-Plane Auth](../control-plane-auth.md) pages are the **shared**
> reference — the API contract, the permission model, and the operator concepts
> are language-neutral. This page covers what is specific to serving a
> **TypeScript** project.

The control-plane API is defined once, in
[`contracts/controlplane`](../../contracts/controlplane) at `API_VERSION=1`. The
**same generated client and the same console work against either edition's
server** — snake_case DTO field names match exactly.

## Serving a TypeScript project

`@typeflux/temporal-controlplane` ships a `typeflux-controlplane` bin:

```bash
pnpm install
pnpm -r --filter "./packages/typescript/**" build

node packages/typescript/temporal-controlplane/dist/http/serve.js \
  --registry path/to/typeflux.projects.yaml \
  --port 8411 \
  [--host 127.0.0.1] \
  [--auth-token NAME:PERMS:TOKEN] \
  [--trust-proxy-auth] \
  [--langfuse]
```

The package declares this entry point as a `typeflux-controlplane` bin, so an
installed dependency can invoke it by name. From a **checkout** there is no root
importer linking that bin onto the path — and the package is unpublished — so
run the built `dist/http/serve.js` directly, as above.

Auth is **open by default** — a local server trusts its caller. Bearer-token
grants and reverse-proxy actor headers are opt-in, exactly as in the Python
server. Do not expose an unauthenticated control plane; see
[Control-Plane Auth](../control-plane-auth.md).

## Three layers

The package is usable a layer at a time, not only as a server:

1. **`ProjectControlPlane`** — pure in-memory projections over a loaded project
   bundle. No Temporal, no HTTP. Meta, workflow/environment listings, validation,
   the resolved workflow bundle, activity catalog, policy/profile definitions,
   prompt status, the enforcement-events feed, and GitHub provenance.
2. **`WorkflowOperations`** — `start` / `status` / `submitReview` /
   `requestCancel` / `migrate` over `@temporalio/client`. Every operate call
   composes and **admits** the selected policy *before* touching Temporal, so a
   policy failure is a 422, never a 503.
3. **`serve` / `createServer` / `buildRoutes`** — the HTTP surface over both.

API-level detail lives in the
[package README](../../packages/typescript/temporal-controlplane/README.md).

## What differs from the Python server

**The Temporal binding profile is a permanent divergence.** TypeScript uses
`ts-plan-argument`: the workflow type is the constant `typefluxYamlWorkflow`, the
derived plan is passed as a start argument, and identity lives in the execution
memo. Python uses `python-versioned-type`. This is an **ABI divergence by
design** — a TS-started execution is operated by the TS worker, and a
Python-started one by the Python worker. They are not interchangeable at
runtime, even though they speak the same HTTP contract.

**`github_provenance` reports `not_configured`.** The TS registry serves local
project checkouts only and records no git source, so the capability is `false`
on this edition and the surface degrades honestly with no network call. The seam
and the full read logic are ported, so it lights up automatically if the
registry gains git sources — there is deliberately no flag to force it on.

**Vendor transports are injected, not imported.** The control-plane core holds
no vendor client. The embedding host injects activity IO schemas (`schemasFor`)
and, optionally, a Langfuse reader (`langfuseFor`, or the `--langfuse` flag).
Without the Langfuse transport the connections probe, the prompt-status
label-drift tier, and the enforcement feed's runtime source **degrade honestly**
(`un-probed` / `unknown` / `not_configured`) rather than failing or pretending.

## Conformance

Both editions pass the same executable HTTP suite as required CI checks
(`contracts/controlplane/conformance`, run per edition via `--edition ts-cp` and
`--edition python-cp`). The suite is the living parity matrix: a case is either
byte-identical across editions or carries an explicit, why-noted variant.

## Console and MCP

The [console](../../clients/console/README.md) and the
[MCP server](../../clients/mcp/README.md) are **edition-aware**, not
edition-specific — they read a project's resolved contracts regardless of which
SDK authored it. The console's e2e suite deliberately includes a
`typescript`-runtime project to prove honest degradation when a Python server
cannot resolve it. `typeflux-mcp` can run a managed-local TS project
(`TYPEFLUX_RUNTIME=typescript`) or attach to any control plane.

## See also

- [Control Plane](../control-plane.md) · [Control-Plane Auth](../control-plane-auth.md) — shared concepts
- [`@typeflux/temporal-controlplane`](../../packages/typescript/temporal-controlplane/README.md) — API detail
- [YAML Runtime (TypeScript)](yaml.md) — the runtime this control plane resolves
- [Editions](../editions.md) — parity table and current gaps
