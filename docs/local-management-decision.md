# Local execution and management surfaces

Decision for [#94](https://github.com/gibli-labs/typeflux/issues/94): retain
foreground CLI/SDK-only execution for Typeflux Local. Existing authoring and
observation tools remain useful, but this decision adds no managed Local start,
run-status API or console/MCP execution service. Users who need durable handles,
human review or independent worker lifetimes use the existing Temporal route.

## Ownership, lifetime and results

The application owns its async SDK call or foreground `typeflux-local` process.
The SDK returns the validated result or raises/rejects; the caller must await it.
The CLI prints validated result JSON and exits zero on success, and reports
failure with a nonzero exit. There is no asynchronous acceptance receipt that
promises a later queryable Local run. An external scheduler may invoke these
entry points, but owns triggering, overlap, retries and missed invocations; see
the [native delay/external-trigger guide](temporal-adoption.md).

Progress exists in the owning process. A normal cancellation/interruption is
best effort within the executor, provider and cleanup contracts. A forced kill
can prevent cleanup; cancellation does not undo completed external effects or
prove an in-flight external request stopped. Restarting runs from step one and
can repeat effects. Application idempotency and outcome reconciliation remain
necessary. This decision adds no checkpoint, daemon, queue or rollback mechanism.

Local correlation IDs, spans and execution manifests can help explain an
invocation. An exported observation can outlive its process, but cannot resume
that process or serve as a Temporal workflow/run handle. A retained trace is
not an execution-history database that the Local runtime can replay.

## Capability matrix

| Surface | Supported meaning for Local | Execution management boundary |
| --- | --- | --- |
| Python/TypeScript Local SDK | Validated foreground YAML execution through the shared activity pipeline | The calling application owns the task, result and process lifetime |
| `typeflux-local` in either edition | Foreground workflow/project/environment invocation with JSON result and command exit status | No detached start, status polling or resume token |
| YAML loading, shared planning, preflight and execution manifest | Inspect the supported computation and reject unsupported requirements across its closure | A definition/manifest is planned computation, not a live run record |
| Existing static MCP docs/schema/scaffold resources and authoring aids | Read instructions or obtain reviewable source content under each tool's existing prerequisites | They do not launch or own a Typeflux Local workflow |
| Existing observation backend or exported trace/manifest | Inspect records that were actually emitted, subject to its configuration, retention and redaction | No reconstructed live status, cancellation, review or recovery from a Local correlation ID |
| Control-plane HTTP, console and MCP run operations | No managed Local execution selected by this decision | Existing run start/status/enumeration, signals/queries, review, cancellation, migration, workers and drain are Temporal-backed |
| Temporal-shaped bundle, resolved-plan or deployment projections | Do not assume Local support from a successful YAML inventory/shape read | Existing projections requiring a Temporal task queue/identity retain their capability refusal |

Inventory or authoring access does not imply every control-plane projection
supports Local. Both server editions currently retain Temporal-specific bundle
and operational projections. The console and MCP consume those existing
contracts; there is no new Local capability flag or HTTP response schema here.
Use the [Local runtime guide](local-runtime.md), rather than a Temporal worker or
run-management command, to select an executable Local entry point.

## Errors and target identity

Existing engine/capability checks remain authoritative. Unsupported Local
features or Temporal-only entry points fail explicitly, with their existing
workflow/path and engine guidance, before constructing the corresponding runtime
service. Configuration, schema, binding, permission and backend errors retain
their existing contracts and ordering. A Local selection never silently starts
a Temporal workflow, skips a review gate, or routes a Temporal mutation to a
foreground process. The existing authenticated operator boundary still applies
to Temporal control-plane operations.

No Local correlation ID is accepted as a new resumable handle. Native Temporal
project/workflow/version/run binding stays on the Temporal path; a trace link or
source definition does not establish that binding. See the existing
[control-plane guide](control-plane.md) and [authorization boundary](control-plane-auth.md)
for those operations.

## MCP's existing `managed-local` name

The MCP backend named `managed-local` predates Typeflux Local. Its
`startManagedLocal` helper starts an in-process TypeScript **control-plane HTTP
server**, binds an ephemeral loopback port and closes that server on disposal.
The backend's `runtime: python|typescript` chooses a resolver edition; it does not
select `runtime.engine: local` or start a workflow worker. Pure-YAML reads and
full bundle/schema resolution have different prerequisites in that backend.
See the [MCP README](../clients/mcp/README.md).

Owning that HTTP server does not supply Local workflow execution, a daemon that
survives MCP exit, a run registry or recovery. Attach mode likewise uses the
capabilities and permissions of its actual configured control plane. Existing
authoring/read tools should continue to describe their real scope.

## Why retain this boundary

The CLI/SDK already supply the simple execution path without a runtime service.
Wrapping them in a managed API would require an explicit owner, cancellation and
process-exit rules, result delivery, redaction and truthful status contracts. A
detached or post-exit status promise would expand the product into persistence
or orchestration, which users can obtain through Temporal when needed. That
expansion is not required by this decision.

Any later managed Local proposal must establish user value and its exact
lifetime/ownership contract first. If selected, it must update both server
editions, normative OpenAPI, generated client, console, MCP, conformance and
meaningful process-exit/error tests together. No partial implementation is
authorized or promised here.

This is a documentation/product-boundary decision. It adds no API, runtime
metadata contributor or execution-manifest schema. Client/control-plane events
remain outside execution manifests. Existing Local capability and Temporal
entry-point refusal tests supply behavioral coverage; graph declarations and
this matrix are descriptions, not evidence that a new surface was implemented.
