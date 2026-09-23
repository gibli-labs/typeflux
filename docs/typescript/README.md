# TypeScript Docs

The TypeScript SDK's prose docs. TypeScript is a **first-class edition**, not a
port in progress — see [Editions](../editions.md) for the parity table, the
shared contracts, and the surfaces where each edition currently leads.

Behavior is **parity, not byte-identical**: schemas are Zod instead of Pydantic,
and everything vendor-shaped (providers, schemas, moderators, prompt registries,
Temporal/observability clients) is **injected as a thin structural transport**
rather than imported by module path. A spec block this runtime does not honor is
**rejected with a pointer error**, never silently ignored.

## Pages

- [Tutorial](tutorial.md) — the TS-first arc from zero to a governed, observed,
  deployed AI workflow, and operating it
- [Concepts](concepts.md) — ownership boundary, package layout, the AI-activity
  contract, execution order, provider controls, manifests
- [Code-Defined Workflows](code-defined-workflows.md) — code-first orchestration,
  the composition primitives (`fanOut` / `withFallback` / `groundWithSearch`), and
  durable child-workflow composition
- [YAML Runtime](yaml.md) — the declarative `typeflux.yaml` runtime, project
  manifests / profiles / policy, the runnable examples, and the permanent
  divergences
- [Observability](observability.md) — the trace/manifest write surface, the
  injected transport, and redaction
- [Privacy & Data Protection](privacy.md) — payload-codec encryption, custom
  redaction rules, and the governance knobs (edition regex-dialect note)
- [Provider Portability](provider-portability.md) — the no-silent-divergence
  contract and the injected-provider wiring
- [Multimodal Content Parts](content-parts.md) — the four part kinds, the
  hashing invariant, and where artifacts get resolved
- [Extending](extending.md) — the five injected seams (provider, prompt
  registry, observer, trace transport, moderator)
- [Control Plane](control-plane.md) — serving a TS project, the
  `ts-plan-argument` binding, and honest degradation
- [Adoption & `engine.lock`](../adoption.md) — setting up a project (modes, layout,
  manifest growth) and pinning the engine (the `@typeflux/*` packages install
  from public npm; a sibling checkout remains supported for development, #866)

## Where the shared reference lives

The `typeflux.yaml` schema, the policy and admission model, the deployment
contract, and the operator surfaces are **language-neutral**. Those pages live
one level up and are currently narrated in Python — their semantics apply to
both editions, and their runnable commands do not:

[Concepts](../concepts.md) · [YAML](../yaml.md) ·
[Control Plane](../control-plane.md) · [Control-Plane Auth](../control-plane-auth.md) ·
[YAML Worker Deployment](../yaml-worker-deployment.md) ·
[Provider Portability](../provider-portability.md) · [Privacy](../privacy.md) ·
[Production Readiness](../production-readiness.md) ·
[Compliance Readiness](../compliance-readiness.md)

Package READMEs carry the API-level detail and adapter examples:
[`@typeflux/temporal`](../../packages/typescript/temporal) ·
[`@typeflux/temporal-worker`](../../packages/typescript/temporal-worker) ·
[`@typeflux/temporal-yaml`](../../packages/typescript/temporal-yaml) ·
[`@typeflux/temporal-controlplane`](../../packages/typescript/temporal-controlplane)

## Known gaps

Tracked, not hidden — the full table is in [Editions](../editions.md):

- No TS trace-reading CLI — use the Python CLI against the same backend, or the
  backend's UI ([#800](https://github.com/gibli-labs/typeflux-temporal/issues/800))
- No shipped generic OTLP observer; the `custom` seam is available
  ([#803](https://github.com/gibli-labs/typeflux-temporal/issues/803))
- `typeflux-project` ships `deploy` and `erase`; other verbs have no TS CLI yet
  ([#799](https://github.com/gibli-labs/typeflux-temporal/issues/799))
