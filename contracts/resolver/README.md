# Resolver interface

[`resolver.v1.json`](resolver.v1.json) names the one genuinely language-bound
control-plane layer — **resolution** — as a contract-shaped interface (epic
[#619](https://github.com/gibli-labs/typeflux-temporal/issues/619)).

HTTP, auth, and the registry are language-neutral; projections are pure
functions both editions implement; Temporal operations become neutral via
binding drivers ([`../temporal-binding/`](../temporal-binding/)). What
remains bound to a language runtime is importing a project's schema and
activity modules to produce resolved DTOs. Naming that seam is what lets one
control plane serve mixed-language projects — and it is exactly the boundary
a future hosted control plane needs (a resolver agent running next to
project code, the control plane hosted elsewhere).

## The closed set

The four READ operations plus `resolve_plan` (#642 — the raw plan a
plan-as-argument start dispatches; binding-profile-scoped, see the
document's `profile_note`) are the **complete** language-bound surface, verified
route by route: environment/policy/profile reads are pure YAML; deployment
verification reuses `resolve_bundle`; and the six routes that import project
modules today for other reasons (`executions`, `versions`, `workers`,
`connections`, `correlation`) do so only to derive resolved identity or
effective config — registered workflow type, spec digest, task queue,
runtime/observability config — all of which `resolve_bundle` already
returns (`BundleWorkflowIdentity`, `runtime_effective`, `links`). Behind
this interface those routes decompose into `resolve_bundle` output plus
binding-driver visibility operations
([`../temporal-binding/`](../temporal-binding/)) or neutral probes; the
Python CP refactor (slice 3) makes that decomposition real.

## No new shapes

Every operation's response is a **named reference into the control-plane
contract** (`../controlplane/openapi.v1.json` components): resolution
returns exactly what the API serves. The suite-integrity gates on both SDKs
cross-check every `response_schema` against the OpenAPI document, so a
resolver operation can neither invent a shape nor dangle when the API
contract changes. Errors ride the `ApiError` taxonomy for the same reason —
a control plane forwards resolver failures, it does not translate them.

## Naming across bindings

The contract's snake_case operation and parameter names are normative for
the **wire transport** and for cross-binding correspondence (each binding's
conformance tests map its surface back to these names). In-process bindings
use their language's idiomatic casing — Python keeps snake_case keywords,
TypeScript uses camelCase options — the correspondence, not the spelling,
is the contract.

## Transport-shaped on purpose

In-process resolvers (Python: epic slice 3; TypeScript: slice 4) bind the
operations directly. The subprocess resolver landed with #642: the Python CP
spawns the TS edition's `resolver-stdio.js` (newline-delimited JSON, the
document's `subprocess_framing`) and multiplexes resolvers per runtime — one
control plane serves both editions, including plan-as-argument starts. A
network agent later is an implementation swap of the same envelope, not a
rearchitecture.

## Change process

A change here is an interface change. Adding an operation requires wiring it
in the same PR (an implementation in at least the Python resolver) or the
document change is rejected — spec model = wired.
