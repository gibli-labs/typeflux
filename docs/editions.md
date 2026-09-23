# Editions: Python and TypeScript

Typeflux Temporal ships **two first-class SDKs**. This page is the router: what
is shared, what is edition-specific, and — honestly — which edition currently
leads on which surface.

Read this before picking an edition, and before assuming a feature is missing
because your edition's docs are quieter about it.

## What is shared

The **contracts are language-neutral**. A `typeflux.yaml` spec, a resolved
bundle, an execution manifest, a deployment plan, a policy, and a trace shape
mean the same thing in either edition — they are defined once in
[`contracts/`](../contracts) at `CONTRACT_VERSION=1` and implemented twice.

That has a practical consequence worth stating plainly: **a spec written to the
shared subset runs under either SDK.** The tutorial spec is literally the same
file in both tutorials. What differs is the wiring around it.

The qualifier is real. Edition-specific wiring does **not** port: a spec using
`activities.modules` (Python importlib module loading) or `class:` custom
wiring is deliberately **rejected** by the TS validator with a pointer error
directing you to inject code-defined activity descriptors instead
([#496](https://github.com/gibli-labs/typeflux-temporal/issues/496)) — the root
README's own YAML sample is in that category. Schemas, prompts, providers,
policy, composition, and lifecycle are portable; module loading is not.

The control plane, the [console](../clients/console/README.md), and the
[MCP server](../clients/mcp/README.md) are **edition-aware, not edition-specific** —
they read a project's resolved contracts regardless of which SDK authored it.

## What differs by construction

These are deliberate, permanent divergences — not gaps:

| | Python | TypeScript |
| --- | --- | --- |
| Schemas | Pydantic models | Zod objects |
| Vendor wiring | Imported by module path (`schemas:TicketInput`) | Injected as thin **structural transports** |
| Workflow shape | Dynamic workflow class | Static bundle; the plan is passed as an **argument** |
| Plan identity | Per-workflow Temporal type name | Constant type + `spec_digest` (`workflowPlanDigest`) |

Behavior is **parity, not byte-identical**. A spec block one edition does not
honor is **rejected with a pointer error**, never silently ignored.

## Parity by surface

Status is measured against the shipped code, not against intent.

| Surface | Python | TypeScript | Notes |
| --- | --- | --- | --- |
| Typed AI activities, validation-repair, output checks, hooks | ✅ | ✅ | |
| Providers (OpenAI, Anthropic, Gemini) | ✅ | ✅ | |
| Prompt registries (inline, Langfuse, LangSmith) | ✅ | ✅ | |
| Content parts / artifacts (multimodal) | ✅ | ✅ | All four part kinds both sides |
| YAML runtime + project manifests | ✅ | ✅ | |
| Composition — `parallel`, `when`, sub-workflows, `map` | ✅ | ✅ | |
| Lifecycle review gates + compensation | ✅ | ✅ | |
| Policy, admission, risk tiers | ✅ | ✅ | Full governance parity |
| Privacy — payload codec, custom redaction, erasure | ✅ | ✅ | |
| Deployment plans + Kubernetes rendering | ✅ | ⚠️ | Same plan/render contract and four artifacts; Python's `deploy --base-env-file` / `--hermetic` (reproducible env interpolation) have no TS flag yet |
| Control-plane server | ✅ | ✅ | Same OpenAPI contract, client, and console; TS ships a `typeflux-controlplane` bin |
| Git-sourced project serving + GitHub provenance | ✅ | ❌ | The TS registry serves local checkouts only; the provenance surface reports `not_configured` (seam ported, degrades honestly) |
| Session / prefix caching | ✅ | ✅ | |
| Observability write (Langfuse, LangSmith) | ✅ | ✅ | |
| Generic OTLP observer + vendor-profile seam | ✅ | ⚠️ | TS has the `custom` seam but no shipped OTLP implementation — [#803](https://github.com/gibli-labs/typeflux-temporal/issues/803) |
| Trace-reading CLI (`list/search/inspect/export/diff`) | ✅ | ❌ | TS-emitted traces are read with the Python CLI against the same backend, or the backend UI — [#800](https://github.com/gibli-labs/typeflux-temporal/issues/800) |
| Project CLI verb coverage | ✅ 16 verbs | ⚠️ 2 verbs | TS `typeflux-project` ships `deploy` and `erase`; `submit`/`validate`/`admit` and the rest have no TS CLI yet — [#799](https://github.com/gibli-labs/typeflux-temporal/issues/799) |
| Runnable examples | 16 | 12 | TS: 6 YAML + 1 code-defined + 5 core scenario scripts |

**Summary.** The two editions are peers on the *runtime, governance, privacy, and
deployment* surface. **Python leads on operational tooling** — the trace-reading
CLI, the generic OTLP path, and CLI verb coverage. A TypeScript-only shop can
author, govern, deploy, and run workflows end to end today, but currently reaches
for the Python CLI (or the backend's own UI) for trace forensics.

## Which docs to read

Three groups, and the first one is not edition-specific:

**Shared / language-neutral** — the spec fields, the governance model, and the
operator surfaces mean the same thing in both editions:

- [Concepts](concepts.md) · [YAML Runtime](yaml.md) — field semantics
- [Control Plane](control-plane.md) · [Control-Plane Auth](control-plane-auth.md)
- [YAML Worker Deployment](yaml-worker-deployment.md)
- [Production Readiness](production-readiness.md) · [Compliance Readiness](compliance-readiness.md)
- [Provider Portability](provider-portability.md) · [Privacy](privacy.md)

> These pages are currently **narrated in Python** — their runnable commands use
> `uv run`. The semantics apply to both editions; see the TypeScript page of the
> same name for the TS wiring and commands.

**Python** — [Tutorial](tutorial.md) · [Dependency Management](dependencies.md) ·
[Extending](extending.md) · [Content Parts](content-parts.md) ·
[Live Walkthrough](live.md) · [Observability](observability.md) ·
[Code-Defined Workflows](code-defined-workflows.md)

**TypeScript** — [docs/typescript/](typescript/) — [Tutorial](typescript/tutorial.md) ·
[Concepts](typescript/concepts.md) · [YAML](typescript/yaml.md) ·
[Code-Defined Workflows](typescript/code-defined-workflows.md) ·
[Observability](typescript/observability.md) · [Privacy](typescript/privacy.md) ·
[Provider Portability](typescript/provider-portability.md) ·
[Extending](typescript/extending.md) · [Control Plane](typescript/control-plane.md) ·
[Content Parts](typescript/content-parts.md)

## Why the docs are shaped this way

The TypeScript pages are written as **deltas** against their Python counterparts:
they carry the TS wiring and link up for field semantics. That was the right
shape while TypeScript was catching up, and it keeps the two sets from drifting
apart on the ~7,600 lines of shared spec semantics that would otherwise be
maintained twice.

It has a real cost: a TypeScript reader is handed off mid-topic. The decision for
now is to **keep the delta shape and fix the routing** — this page plus per-page
group headers — rather than fork the shared material into a third edition-neutral
copy that would immediately start drifting.

The longer-term fix, if the delta shape keeps hurting, is to lift the genuinely
language-neutral material (spec fields, policy model, admission, deployment) into
edition-neutral pages that *both* tracks link down into, leaving each edition's
pages to carry only wiring. That is a large, mechanical restructure and is
deliberately **not** bundled with a correctness pass.

Either way, the invariant holds: **a status change in one edition's docs is a
status change in this page's parity table.** Keep them in sync.
