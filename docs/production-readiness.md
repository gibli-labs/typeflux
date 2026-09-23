# Production Reference Architecture & Operator Readiness

This is the single page that answers: **can a team run Typeflux safely, and
where?** It turns the concept, deployment, control-plane, and compliance docs
into one operator checklist with a clear shipped / partial / gap verdict per
item, and a reference architecture to deploy against.

It is an **operator preview** guide. Typeflux runs safely today in development
and internal staging; the former regulated-production blockers (payload
encryption at rest, configurable PII catalog, data-subject erasure) have
shipped, and the remaining gaps for specific regimes are named below and
tracked.

> Keep this checklist and the [Compliance Readiness](compliance-readiness.md)
> matrix in sync — they cover the same features for different readers, and a
> status change in one is a status change in both.

> **Edition scope.** This checklist and its commands are narrated for the
> **Python** SDK (`uv run …`). Most controls below rest on the shared,
> language-neutral contracts and hold for the TypeScript SDK too, but **not all
> of them do** — item 11's Git-sourced serving is Python-only (the TS registry
> serves local checkouts and reports GitHub provenance as `not_configured`), and
> TS-only shops additionally lack a trace-reading CLI and a generic OTLP
> observer. Verify per-surface against the
> [Editions parity table](editions.md#parity-by-surface) before treating a row
> here as a TypeScript guarantee; TS wiring and commands are in the
> [TS tutorial](typescript/tutorial.md) and
> [`@typeflux/temporal-yaml`](../packages/typescript/temporal-yaml).

## Can you run it here?

| Environment | Verdict | What it takes |
| --- | --- | --- |
| **Local development** | ✅ Yes | `serve` binds `127.0.0.1` and trusts the caller; fake/dev providers; no secrets needed. |
| **Internal staging / operator preview** | ✅ Yes, once configured | Real Temporal + provider + Langfuse; secrets via `value_from`; control-plane behind auth (token or proxy); digest-pinned worker images; deployment plans approved by PR. |
| **Regulated production** (PCI / HIPAA / GDPR-erasure / FedRAMP) | ⚠️ Regime-dependent | Payload encryption at rest, the configurable PII catalog, and data-subject erasure **shipped** ([#188](https://github.com/gibli-labs/typeflux-temporal/issues/188), [#715](https://github.com/gibli-labs/typeflux-temporal/issues/715); items 12–14 below). Remaining gaps are regime-specific: prompt-side redaction / in-VPC provider path and data residency (HIPAA, FedRAMP, localization), per-execution spec pinning, full authoring provenance, and ETL lineage (EU AI Act, SOX, GDPR records); unified SSO and secret-manager integration are partial. See [Compliance Readiness](compliance-readiness.md) for the full per-regime mapping. |

## Reference architecture

```
              author in code + YAML                  approve by PR
  repo  ──────────────────────────────►  deployment plan (digest-pinned, secret-free)
   │                                              │
   │  project validate / preflight (CI)           │  project deploy --plan-out
   ▼                                              ▼
 ┌──────────────┐   poll task queue   ┌─────────────────────────┐
 │ Temporal     │◄────────────────────│ Workers (K8s)           │
 │ Cloud or     │   activities/heartbeat│  digest-pinned image   │
 │ self-hosted  │────────────────────►│  secrets via value_from │
 └──────────────┘                     │  per-(env,provider) queue│
   ▲      ▲                           └───────────┬─────────────┘
   │      │ traces (opt-in)                       │ provider calls
   │      ▼                                        ▼
   │  ┌──────────┐                          ┌────────────┐
   │  │ Langfuse │  redacted at egress      │ LLM provider│
   │  └──────────┘                          └────────────┘
   │
   │  inspect / start / review / cancel (authorized)
 ┌─┴───────────────────────┐
 │ Control-plane API + console (behind auth: token or reverse-proxy SSO) │
 └───────────────────────────────────────────────────────────────────────┘
```

- **Temporal** — Temporal Cloud or self-hosted; one namespace per environment.
  Workers poll per-environment (and per-provider-variant) task queues; workflow
  versioning keeps old and new workers safe on the same queue.
- **Workers (Kubernetes)** — digest-pinned images, secrets injected as env/files
  and referenced with `value_from` (never interpolated), scaled by task-queue
  replicas. See [YAML Worker Deployment](yaml-worker-deployment.md).
- **Providers** — provider variants run on separate task queues; the
  no-silent-divergence contract guards truncation and capability gaps. See
  [Provider Portability](provider-portability.md).
- **Observability** — Langfuse is **opt-in** (`runtime.observability.type:
  langfuse`); traces are regex-redacted at the SDK boundary before egress. See
  [Observability](observability.md).
- **Deployment** — operators generate a secret-free, digest-pinned deployment
  plan and **approve it by merging a PR**; promotion is a reviewed CLI step, not
  a console mutation.
- **Control plane** — the API/console is **open by default on localhost**;
  expose it only behind built-in token auth or a reverse-proxy SSO. See
  [Control-Plane Auth](control-plane-auth.md).

## Readiness checklist

Status legend: **Shipped** (exists today) · **Partial** (foundation exists, gaps
remain) · **Gap** (designed or discussed, not built).

| # | Item | Status | How to verify | Tracking |
| --- | --- | --- | --- | --- |
| 1 | **Policies** — project policy YAML, composition, hash enforcement at deploy/startup | Shipped | `project validate --environment <env>`; a mismatched `TYPEFLUX_EXPECTED_POLICY_HASH` fails closed | [#129](https://github.com/gibli-labs/typeflux-temporal/issues/129) (closed) |
| 2 | **Secret references** — `value_from.env`/`.file`; secret-free contract JSON | Shipped | Loading **warns** on an inline secret; deployment-plan generation and the opt-in `require_secret_references` policy **reject** it; bundle/plan JSON carries only source kind + name | [YAML](yaml.md) |
| 3 | **Observability** — Langfuse traces, generation observations, token usage | Shipped | `runtime.observability.type: langfuse`; trace inspect/diff CLI | [Observability](observability.md) |
| 4 | **Redaction** — regex PII redaction before egress; reviewer identity/notes never in `typeflux.*` metadata | Shipped | Default rules mask email/phone/SSN/card (Luhn); `redaction.exclude_paths` | [Observability](observability.md), [Auth](control-plane-auth.md) |
| 5 | **Control-plane auth / RBAC** — permission boundary, token + reverse-proxy SSO, capability-gated console | Shipped | `--auth-token` / `--trust-proxy-auth`; read-only token is 403'd on `start`; `/meta` reports capabilities | [#292](https://github.com/gibli-labs/typeflux-temporal/issues/292) |
| 6 | **Worker task queues** — per-workflow/env/provider queues; versioning-safe rollout | Shipped | `task_queue` in YAML; deploy plan pins the queue | [Worker Deployment](yaml-worker-deployment.md) |
| 7 | **Image digest pinning** — plans require a digest; mutable images rejected | Shipped | Plan generation **fails** on a non-`@sha256` image unless `--allow-mutable-image`; prod smoke omits that flag | [Worker Deployment](yaml-worker-deployment.md) |
| 8 | **GitHub PR approval** — deployment plans approved by merging a PR | Shipped | Plan committed → CI validates → merge → promote; **code-enforceable (#790)**: `deploy --apply --require-merged-plan` (or `TYPEFLUX_REQUIRE_MERGED_PLAN=1`, the CI-recommended posture) refuses unless the plan's exact bytes are merged on the remote default branch | [Control Plane](control-plane.md) |
| 9 | **Provider portability** — no-silent-divergence (truncation guards, preflight capability matrix) | Shipped | Anthropic/OpenAI truncation raises; unsupported artifact kind fails preflight | [#176](https://github.com/gibli-labs/typeflux-temporal/issues/176) |
| 10 | **Heartbeating / cancellation** — cooperative heartbeat + cancel for long provider calls | Shipped | `heartbeat_timeout_seconds` in YAML; a cancelled run aborts at the next checkpoint | [#208](https://github.com/gibli-labs/typeflux-temporal/issues/208) |
| 11 | **Multi-project + Git-sourced serving** — registry, project-scoped routes, `/refresh` | Shipped | `serve --registry`; `POST /api/v1/projects/{project}/refresh` | [Control Plane](control-plane.md) |
| 12 | **Payload encryption at rest** — Temporal data-converter/codec so PII is encrypted in history | Shipped | AES-256-GCM [`payload_codec`](privacy.md#payload-codec--encryption-at-rest) in YAML; fail-closed on an unset/short key; `require_payload_codec` policy gate | [#188](https://github.com/gibli-labs/typeflux-temporal/issues/188), [Privacy](privacy.md) |
| 13 | **Configurable PII catalog** — jurisdiction-specific rules beyond the four defaults | Shipped | [`observability.redaction.custom_rules`](privacy.md#custom-redaction-rules) + `require_custom_rules` policy gate; prompt-side (pre-provider) redaction remains a gap tracked in [Compliance Readiness](compliance-readiness.md) | [#188](https://github.com/gibli-labs/typeflux-temporal/issues/188), [Privacy](privacy.md) |
| 14 | **Data-subject erasure** — across Temporal/Langfuse/cache | Shipped | [`typeflux erase` + `ErasureReceipt`](privacy.md#retention--erasure) (#715): per-subject crypto-shred + `DeleteWorkflowExecution`, Langfuse trace deletion, cache invalidation, dry-run-first CLI; exported artifacts/manifests and provider logs remain caller-owned (the receipt marks them unreachable); retention TTLs remain namespace-admin-owned (documented pattern, not built) | [#715](https://github.com/gibli-labs/typeflux-temporal/issues/715), [Privacy](privacy.md) |
| 15 | **Unified SSO across observability + control surfaces** — single IdP | Partial | Control-plane auth ships a reverse-proxy SSO integration point; Langfuse auth is its own deployment; no unified IdP layer | [Auth](control-plane-auth.md) |

Items 12–14 — the former regulated-production blockers — **shipped** via
[#188](https://github.com/gibli-labs/typeflux-temporal/issues/188) and
[#715](https://github.com/gibli-labs/typeflux-temporal/issues/715). The
remaining regime-specific gaps are prompt-side redaction / the in-VPC provider
path, data residency, per-execution spec pinning, full authoring provenance,
and ETL lineage, with unified SSO (item 15) and secret-manager integration
partial — see [Compliance Readiness](compliance-readiness.md) for which
regimes each blocks. Payload encryption applies only where `payload_codec` is
configured: without it, treat PII as visible in Temporal history and provider
logs.

## Validation commands

All non-live — they need no Temporal, provider, or Langfuse connection, and are
exactly what CI runs.

```bash
cd packages/python

# Resolve references, env vars, the workflow graph, and offline manifests.
uv run typeflux-project validate typeflux.project.yaml --environment <env>

# Fail fast on YAML/import/prompt-resolution errors before a worker polls.
uv run python -m typeflux.yaml.run path/to/typeflux.yaml --preflight

# The full gate CI enforces:
uv run pytest -q -m "not live"          # unit + integration, no live services
uv run ruff check . ../../scripts && uv run ruff format --check . ../../scripts
uv run mypy src/typeflux
uv lock --check                          # lockfile freshness
# The emitted control-plane schema must match the normative contract
# (contracts/controlplane/openapi.v1.json, #616); structured diff on drift:
uv run typeflux-controlplane conformance
```

**Non-live test expectations.** The default suite (`-m "not live"`) runs with no
external services and no optional extras beyond what the package declares;
provider, Temporal, and Langfuse calls are faked. Live tests are opt-in and
excluded from the default gate, so a green local run that depends on
locally-set credentials can still differ from CI — keep new tests non-live
unless they are explicitly marked `live`.

## Generating a deployment (operator-reviewed)

```bash
uv run --project packages/python typeflux-project deploy typeflux.project.yaml \
  --environment <env> --workflow <id> --policy <policy> \
  --image ghcr.io/org/worker@sha256:<digest> --plan-out deployments/
```

The plan is secret-free and digest-pinned. Commit it, open a PR (CI validates
it), merge — the merged review **is** the approval — then promote with the
`promote` command the console/plan prints. The console assists plan generation
but writes nothing itself.

## Related

- [Compliance Readiness](compliance-readiness.md) — the certification matrix and
  what each gap blocks.
- [YAML Worker Deployment](yaml-worker-deployment.md) — images, secrets, task
  queues, Kubernetes.
- [Control Plane](control-plane.md) and [Control-Plane Auth](control-plane-auth.md)
  — the API/console and its authorization boundary.
- [Concepts](concepts.md) and [YAML Runtime](yaml.md) — the model and the spec.
